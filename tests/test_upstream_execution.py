"""Real SQLite upstream spending is reserved before I/O and never refunded on unknown outcomes."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.job_service as jobs
from spm_api.db import unit_of_work
from spm_api.master_models import ProviderId
from spm_api.providers.budgets import TOKEN_UNITS, load_budget
from spm_api.providers.execution import abandon_attempt, complete_attempt, reserve_attempt
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.symbols import FetchWindow, resolve_symbol
from tests.helpers import create, database_snapshot
from tests.test_jobs import active, financial_same, plan_for
from tests.test_provider_configuration import policy, provision
from tests.test_provider_persistence import START, fetched
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


def new_work(book: dict[str, Any], engine: Engine, *, at: datetime = START) -> dict[str, Any]:
    with unit_of_work(engine, write=True) as con:
        row = jobs.enqueue(con, UUID(book["owner"]["id"]), plan_for(book), intent_id=uuid4(), at=at)
        item = UUID(
            con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=?", (row["id"],)
            ).scalar_one()
        )
    return {
        **book,
        "job": row,
        "item": item,
        "lease": active(engine, at=at),
        "limits": load_budget(book["store"], book["prepared"].configuration.provider_id).limits,
    }


def configured(book: dict[str, Any], minute: int, day: int) -> dict[str, Any]:
    provision(book["settings"], body=policy(requests_per_minute=minute, requests_per_day=day))
    loaded = book["store"].load("eodhd")
    old = book["prepared"]
    return {**book, "prepared": prepare_fetch(old.owner_id, loaded, old.segment, "DIVIDENDS")}


def reserve(
    engine: Engine, case: dict[str, Any], *, at: datetime = START, identifier: UUID | None = None
) -> Any:
    with unit_of_work(engine, write=True) as con:
        return reserve_attempt(
            con,
            case["lease"],
            case["item"],
            case["prepared"],
            case["limits"],
            attempt_id=identifier or uuid4(),
            at=at,
        )


def complete(
    engine: Engine, case: dict[str, Any], attempt: UUID, page: Any, *, at: datetime | None = None
) -> Any:
    with unit_of_work(engine, write=True) as con:
        reserved = datetime.fromisoformat(
            con.exec_driver_sql(
                "SELECT reserved_at FROM upstream_attempts WHERE id=?", (str(attempt),)
            ).scalar_one()
        )
        at = at or reserved + timedelta(seconds=1)
        return complete_attempt(
            con,
            case["lease"],
            attempt,
            case["prepared"],
            page,
            started_at=reserved,
            finished_at=reserved + timedelta(milliseconds=12),
            duration_ms=12,
            at=at,
            jitter_ms=0,
        )


async def test_reservation_precedes_http_and_replays_without_spending_or_reissuing(
    book: dict[str, Any], engine: Engine
) -> None:
    case = new_work(book, engine)
    before = database_snapshot(engine)
    identifier = uuid4()
    decision = reserve(engine, case, identifier=identifier)
    assert decision.attempt is not None and decision.attempt.created
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert (
            con.exec_driver_sql("SELECT tokens_num FROM provider_budgets").scalar_one()
            == 4 * TOKEN_UNITS
        )
        assert con.exec_driver_sql("SELECT attempt_count,round_open FROM job_items").one() == (1, 1)
    committed = database_snapshot(engine)
    replay = reserve(engine, case, identifier=identifier, at=START + timedelta(seconds=1))
    assert replay.attempt is not None and not replay.attempt.created
    assert database_snapshot(engine) == committed
    page = await fetched(case)
    result = complete(engine, case, identifier, page)
    assert result.receipt.observations_new == 1 and not result.replayed
    committed = database_snapshot(engine)
    assert complete(engine, case, identifier, page).replayed
    assert database_snapshot(engine) == committed
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status,progress_done FROM jobs").one() == (
            "SUCCEEDED",
            1,
        )
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "COMPLETED"
        )
    extra = {"provider_budgets", "upstream_attempts", "upstream_outcomes"}
    financial_same(
        {t: rows for t, rows in before.items() if t not in extra},
        {t: rows for t, rows in committed.items() if t not in extra},
    )


async def test_minute_bucket_waits_for_exact_refill_without_spending_a_work_round(
    book: dict[str, Any], engine: Engine
) -> None:
    book = configured(book, 1, 10)
    first = new_work(book, engine)
    grant = reserve(engine, first)
    complete(engine, first, grant.attempt.id, await fetched(first))
    second = new_work(book, engine, at=START + timedelta(seconds=1))
    denied = reserve(engine, second, at=START + timedelta(seconds=1))
    assert denied.attempt is None and denied.retry_at == START + timedelta(seconds=60)
    with unit_of_work(engine, write=True) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert (
            jobs.owned_job(con, second["lease"].owner_id, second["lease"].job_id)["attempt_count"]
            == 0
        )
        jobs.park(con, second["lease"], at=START + timedelta(seconds=1))
    second["lease"] = active(engine, at=denied.retry_at)
    allowed = reserve(engine, second, at=denied.retry_at)
    assert allowed.attempt.created


async def test_rolling_daily_spending_survives_cancellation_without_early_reset(
    book: dict[str, Any], engine: Engine
) -> None:
    book = configured(book, 100, 1)
    first = new_work(book, engine)
    grant = reserve(engine, first)
    with unit_of_work(engine, write=True) as con:
        jobs.request_cancel(con, first["lease"].owner_id, first["lease"].job_id, at=START)
        abandon_attempt(con, first["lease"], grant.attempt.id, at=START)
    second = new_work(book, engine, at=START + timedelta(hours=1))
    denied = reserve(engine, second, at=START + timedelta(hours=1))
    assert denied.attempt is None
    assert denied.retry_at == START + timedelta(days=1, microseconds=1)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "CANCELLED"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


async def test_expired_unknown_attempt_is_abandoned_but_still_counts_against_the_day(
    book: dict[str, Any], engine: Engine
) -> None:
    case = new_work(book, engine)
    old = reserve(engine, case)
    original_lease = case["lease"]
    case["lease"] = active(engine, at=START + timedelta(seconds=90))
    current = reserve(engine, case, at=START + timedelta(seconds=90))
    assert current.attempt.created and current.attempt.id != old.attempt.id
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 2
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "ABANDONED"
        )
    page = await fetched(case)
    before = database_snapshot(engine)
    case["lease"] = original_lease
    with pytest.raises(jobs.JobError):
        complete(engine, case, old.attempt.id, page, at=START + timedelta(seconds=91))
    assert database_snapshot(engine) == before


async def test_only_one_pending_attempt_per_item_and_different_id_does_not_double_spend(
    book: dict[str, Any], engine: Engine
) -> None:
    case = new_work(book, engine)
    reserve(engine, case)
    before = database_snapshot(engine)
    with pytest.raises(jobs.JobError, match="UPSTREAM_ATTEMPT_IN_FLIGHT"):
        reserve(engine, case)
    assert database_snapshot(engine) == before


async def test_rate_limit_absolute_deadline_is_shared_and_durable(
    book: dict[str, Any], engine: Engine
) -> None:
    first = new_work(book, engine)
    grant = reserve(engine, first)
    page = await fetched(first, status=429)
    page = page.model_copy(update={"retry_at": "9999-12-31T23:59:59.999999Z"})
    result = complete(engine, first, grant.attempt.id, page)
    assert result.retry_at.year == 9999
    with unit_of_work(engine, write=True) as con:
        jobs.park(con, first["lease"], at=START + timedelta(seconds=1))
    second = new_work(book, engine, at=START + timedelta(seconds=2))
    denied = reserve(engine, second, at=START + timedelta(seconds=2))
    assert denied.attempt is None and denied.retry_at == result.retry_at
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1


def cancel_case(engine: Engine, case: dict[str, Any], at: datetime) -> None:
    lease = case["lease"]
    with unit_of_work(engine, write=True) as con:
        row = jobs.owned_job(con, lease.owner_id, lease.job_id)
        if row["status"] in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
            return
        jobs.request_cancel(con, lease.owner_id, lease.job_id, at=at)
        jobs.acknowledge_cancel(con, lease, at=at)


async def trip_circuit(book: dict[str, Any], engine: Engine) -> tuple[dict[str, Any], datetime]:
    book = configured(book, 100, 100)
    for index in range(5):
        at = START + timedelta(seconds=index * 3)
        case = new_work(book, engine, at=at)
        grant = reserve(engine, case, at=at)
        complete(
            engine,
            case,
            grant.attempt.id,
            await fetched(case, status=503),
            at=at + timedelta(seconds=1),
        )
        cancel_case(engine, case, at + timedelta(seconds=1))
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT circuit_state,consecutive_failures,circuit_until FROM provider_budgets"
        ).one()
    assert row[0:2] == ("OPEN", 5)
    return book, datetime.fromisoformat(row[2])


async def test_circuit_has_one_half_open_probe_and_only_that_probe_can_close_it(
    book: dict[str, Any], engine: Engine
) -> None:
    book, deadline = await trip_circuit(book, engine)
    probe = new_work(book, engine, at=START + timedelta(seconds=14))
    wait = reserve(engine, probe, at=START + timedelta(seconds=14))
    assert wait.attempt is None and wait.retry_at == deadline
    with unit_of_work(engine, write=True) as con:
        jobs.park(con, probe["lease"], at=START + timedelta(seconds=14))
    probe["lease"] = active(engine, at=deadline)
    grant = reserve(engine, probe, at=deadline)
    assert grant.attempt.created
    other = new_work(book, engine, at=deadline)
    denied = reserve(engine, other, at=deadline)
    assert denied.attempt is None and denied.reason == "CIRCUIT_PROBE"
    complete(
        engine, probe, grant.attempt.id, await fetched(probe), at=deadline + timedelta(seconds=1)
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT circuit_state,probe_attempt_id,consecutive_failures FROM provider_budgets"
        ).one() == ("CLOSED", None, 0)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 6


async def test_crashed_probe_remains_spent_and_reopens_cooldown_without_new_request(
    book: dict[str, Any], engine: Engine
) -> None:
    book, deadline = await trip_circuit(book, engine)
    probe = new_work(book, engine, at=deadline)
    grant = reserve(engine, probe, at=deadline)
    expiry = grant.attempt.expires_at
    probe["lease"] = active(engine, at=expiry)
    denied = reserve(engine, probe, at=expiry)
    assert denied.attempt is None and denied.retry_at == expiry + timedelta(seconds=60)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 6
        assert (
            con.exec_driver_sql(
                "SELECT disposition FROM upstream_outcomes WHERE attempt_id=?",
                (str(grant.attempt.id),),
            ).scalar_one()
            == "ABANDONED"
        )
        assert (
            con.exec_driver_sql("SELECT circuit_state FROM provider_budgets").scalar_one() == "OPEN"
        )


async def test_clock_regression_waits_for_high_water_without_refunding_spending(
    book: dict[str, Any], engine: Engine
) -> None:
    first = new_work(book, engine)
    grant = reserve(engine, first)
    complete(
        engine, first, grant.attempt.id, await fetched(first), at=START + timedelta(seconds=10)
    )
    second = new_work(book, engine, at=START + timedelta(seconds=1))
    with unit_of_work(engine) as con:
        before = tuple(con.exec_driver_sql("SELECT * FROM provider_budgets").one())
    denied = reserve(engine, second, at=START + timedelta(seconds=1))
    assert denied.attempt is None and denied.retry_at == START + timedelta(seconds=10)
    with unit_of_work(engine) as con:
        assert tuple(con.exec_driver_sql("SELECT * FROM provider_budgets").one()) == before
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1


async def test_limit_increase_does_not_mint_credits_and_decrease_keeps_rolling_history(
    book: dict[str, Any], engine: Engine
) -> None:
    low = configured(book, 1, 10)
    first = new_work(low, engine)
    grant = reserve(engine, first)
    with unit_of_work(engine, write=True) as con:
        abandon_attempt(con, first["lease"], grant.attempt.id, at=START, reason="CONFIG_CHANGED")
    higher = configured(book, 60, 10)
    second = new_work(higher, engine, at=START + timedelta(seconds=1))
    denied = reserve(engine, second, at=START + timedelta(seconds=1))
    assert denied.attempt is None
    assert denied.retry_at == START + timedelta(seconds=1, microseconds=983334)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT minute_limit,tokens_num FROM provider_budgets"
        ).one() == (60, 1000000)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1


async def test_lower_daily_limit_waits_for_enough_old_spends_not_only_the_first(
    book: dict[str, Any], engine: Engine
) -> None:
    initial = configured(book, 100, 3)
    for index in range(3):
        at = START + timedelta(seconds=index)
        case = new_work(initial, engine, at=at)
        grant = reserve(engine, case, at=at)
        with unit_of_work(engine, write=True) as con:
            abandon_attempt(con, case["lease"], grant.attempt.id, at=at, reason="CONFIG_CHANGED")
    lower = configured(book, 100, 1)
    case = new_work(lower, engine, at=START + timedelta(seconds=3))
    denied = reserve(engine, case, at=START + timedelta(seconds=3))
    assert denied.attempt is None
    assert denied.retry_at == START + timedelta(days=1, seconds=2, microseconds=1)


@pytest.mark.parametrize("phase", ["reserve", "complete"])
async def test_late_upstream_audit_failure_rolls_back_every_new_step_not_prior_spend(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import spm_api.providers.execution as execution

    case = new_work(book, engine)
    grant = reserve(engine, case) if phase == "complete" else None
    page = await fetched(case)
    before = database_snapshot(engine)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Injected upstream audit failure")

    monkeypatch.setattr(execution, "audit", fail)
    with pytest.raises(RuntimeError, match="Injected upstream audit failure"):
        if grant is None:
            reserve(engine, case)
        else:
            complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["started-before-budget", "finished-in-future", "naive"])
async def test_captured_http_timing_cannot_precede_its_budget_or_come_from_the_future(
    book: dict[str, Any], engine: Engine, problem: str
) -> None:
    case = new_work(book, engine)
    grant = reserve(engine, case)
    page = await fetched(case)
    started = START - timedelta(microseconds=1) if problem == "started-before-budget" else START
    finished = (
        START + timedelta(days=1)
        if problem == "finished-in-future"
        else START + timedelta(milliseconds=12)
    )
    if problem == "naive":
        started = started.replace(tzinfo=None)
    before = database_snapshot(engine)
    with pytest.raises(ValueError), unit_of_work(engine, write=True) as con:
        complete_attempt(
            con,
            case["lease"],
            grant.attempt.id,
            case["prepared"],
            page,
            started_at=started,
            finished_at=finished,
            duration_ms=12,
            at=START + timedelta(seconds=1),
            jitter_ms=0,
        )
    assert database_snapshot(engine) == before


async def test_shared_yahoo_wrappers_cannot_obtain_separate_upstream_allowances(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    providers: tuple[ProviderId, ...] = ("yahoo", "yfinance")
    for provider in providers:
        await create(
            client,
            f"securities/{book['security']['id']}/provider-symbols",
            {
                "provider_id": provider,
                "provider_symbol": "0005.HK",
                "effective_from": "2020-01-01",
            },
        )
        provision(
            book["settings"],
            provider,
            body=policy(
                provider,
                requests_per_minute=1 if provider == "yfinance" else 10,
                requests_per_day=1 if provider == "yfinance" else 10,
            ),
        )
    cases = []
    for index, provider in enumerate(providers):
        loaded = book["store"].load(provider)
        with unit_of_work(engine) as con:
            segment = resolve_symbol(
                con, UUID(book["security"]["id"]), provider, book["prepared"].segment.window
            ).segments[0]
        current = {
            **book,
            "prepared": prepare_fetch(UUID(book["owner"]["id"]), loaded, segment, "DIVIDENDS"),
        }
        case = new_work(current, engine, at=START + timedelta(seconds=index))
        cases.append(case)
        grant = reserve(engine, case, at=START + timedelta(seconds=index))
        if index == 0:
            assert grant.attempt.created
            with unit_of_work(engine, write=True) as con:
                abandon_attempt(
                    con, case["lease"], grant.attempt.id, at=START, reason="CONFIG_CHANGED"
                )
        else:
            assert grant.attempt is None and grant.retry_at == START + timedelta(
                days=1, microseconds=1
            )
    assert cases[0]["limits"] == cases[1]["limits"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT origin_group FROM provider_budgets").scalar_one() == "YAHOO"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1


async def test_concurrent_jobs_serialize_one_shared_available_request(
    book: dict[str, Any], engine: Engine
) -> None:
    book = configured(book, 1, 1)
    cases = [new_work(book, engine), new_work(book, engine)]
    barrier = Barrier(2)

    def take(case: dict[str, Any]) -> Any:
        barrier.wait()
        return reserve(engine, case)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(take, cases))
    assert sum(result.attempt is not None and result.attempt.created for result in decisions) == 1
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT tokens_num FROM provider_budgets").scalar_one() == 0


async def test_real_daily_budget_does_not_reset_at_utc_midnight(
    book: dict[str, Any], engine: Engine
) -> None:
    book = configured(book, 100, 1)
    at = START.replace(hour=23, minute=59, second=59)
    first = new_work(book, engine, at=at)
    grant = reserve(engine, first, at=at)
    with unit_of_work(engine, write=True) as con:
        abandon_attempt(con, first["lease"], grant.attempt.id, at=at, reason="CONFIG_CHANGED")
    second = new_work(book, engine, at=at + timedelta(seconds=1))
    denied = reserve(engine, second, at=at + timedelta(seconds=1))
    assert denied.attempt is None and denied.retry_at == at + timedelta(days=1, microseconds=1)


async def test_budgeted_prefix_pauses_and_resumes_without_another_logical_attempt(
    book: dict[str, Any], engine: Engine
) -> None:
    book = configured(book, 1, 5)
    with unit_of_work(engine) as con:
        segment = resolve_symbol(
            con,
            UUID(book["security"]["id"]),
            "eodhd",
            FetchWindow(start="2026-01-01", end="2026-01-15"),
        ).segments[0]
    first = {
        **book,
        "prepared": prepare_fetch(
            UUID(book["owner"]["id"]), book["prepared"].configuration, segment, "DIVIDENDS"
        ),
    }
    case = new_work(first, engine)
    grant = reserve(engine, case)
    complete(engine, case, grant.attempt.id, await fetched(case))
    with unit_of_work(engine) as con:
        tail = resolve_symbol(
            con,
            UUID(book["security"]["id"]),
            "eodhd",
            FetchWindow(start="2026-01-16", end="2026-01-31"),
        ).segments[0]
    case["prepared"] = prepare_fetch(
        case["lease"].owner_id, book["prepared"].configuration, tail, "DIVIDENDS"
    )
    denied = reserve(engine, case, at=START + timedelta(seconds=1))
    assert denied.retry_at == START + timedelta(seconds=60)
    with unit_of_work(engine, write=True) as con:
        jobs.park(con, case["lease"], at=START + timedelta(seconds=1))
    case["lease"] = active(engine, at=denied.retry_at)
    tail_grant = reserve(engine, case, at=denied.retry_at)
    page = await fetched(case, payload=[{"date": "2026-01-20", "unadjustedValue": "0.9"}])
    complete(engine, case, tail_grant.attempt.id, page)
    with unit_of_work(engine) as con:
        row = jobs.owned_job(con, case["lease"].owner_id, case["lease"].job_id)
        assert row["status"] == "SUCCEEDED" and row["attempt_count"] == 1
        result = json.loads(row["result_json"])
        assert result["provider_attempts"] == result["provider_http_recorded"] == 2
        assert result["provider_attempts_unresolved"] == 0


async def test_exhausted_retry_after_a_committed_prefix_is_partial_not_whole_window_failure(
    book: dict[str, Any], engine: Engine
) -> None:
    book = configured(book, 100, 100)
    with unit_of_work(engine) as con:
        segment = resolve_symbol(
            con,
            UUID(book["security"]["id"]),
            "eodhd",
            FetchWindow(start="2026-01-01", end="2026-01-15"),
        ).segments[0]
    current = {
        **book,
        "prepared": prepare_fetch(
            UUID(book["owner"]["id"]), book["prepared"].configuration, segment, "DIVIDENDS"
        ),
    }
    case = new_work(current, engine)
    grant = reserve(engine, case)
    complete(engine, case, grant.attempt.id, await fetched(case))
    with unit_of_work(engine) as con:
        tail = resolve_symbol(
            con,
            UUID(book["security"]["id"]),
            "eodhd",
            FetchWindow(start="2026-01-16", end="2026-01-31"),
        ).segments[0]
    case["prepared"] = prepare_fetch(
        case["lease"].owner_id, book["prepared"].configuration, tail, "DIVIDENDS"
    )
    at = START + timedelta(seconds=2)
    for _ in range(4):
        current_grant = reserve(engine, case, at=at)
        result = complete(
            engine,
            case,
            current_grant.attempt.id,
            await fetched(case, status=503),
            at=at + timedelta(seconds=1),
        )
        at = result.retry_at
    with unit_of_work(engine) as con:
        row = jobs.owned_job(con, case["lease"].owner_id, case["lease"].job_id)
        assert row["status"] == "PARTIAL" and row["attempt_count"] == 4
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 5
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 1


async def test_transient_symbol_failure_does_not_impose_global_backoff_before_circuit_threshold(
    book: dict[str, Any], engine: Engine
) -> None:
    first = new_work(book, engine)
    grant = reserve(engine, first)
    complete(engine, first, grant.attempt.id, await fetched(first, status=503))
    second = new_work(book, engine, at=START + timedelta(seconds=1))
    decision = reserve(engine, second, at=START + timedelta(seconds=1))
    assert decision.attempt is not None and decision.attempt.created
