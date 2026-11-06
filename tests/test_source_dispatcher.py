"""The real worker dispatches only fenced, quota-reserved HTTP outside database transactions."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.job_service as jobs
import spm_api.providers.yfinance as sdk_bridge
import spm_worker.dispatcher as dispatcher
import spm_worker.main as worker_main
from spm_api.db import make_engine, unit_of_work
from spm_api.job_models import SourceJobPlan, WorkError
from spm_api.providers.budgets import BudgetSnapshot
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.symbols import FetchWindow, resolve_symbol
from spm_worker.dispatcher import dispatch_once
from tests.helpers import create, database_snapshot
from tests.test_jobs import active, financial_same, massive_work, plan_for
from tests.test_provider_configuration import KEY, policy, provision
from tests.test_provider_persistence import START, WINDOW, fetched
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


@dataclass
class Clock:
    current: datetime = START

    def __call__(self) -> datetime:
        return self.current


def enqueue(book: dict[str, Any], engine: Engine, plan: SourceJobPlan | None = None) -> UUID:
    with unit_of_work(engine, write=True) as con:
        row = jobs.enqueue(
            con, UUID(book["owner"]["id"]), plan or plan_for(book), intent_id=uuid4(), at=START
        )
    return UUID(row["id"])


def state(engine: Engine, identifier: UUID) -> dict[str, Any]:
    with unit_of_work(engine) as con:
        return dict(
            con.exec_driver_sql("SELECT * FROM jobs WHERE id=?", (str(identifier),))
            .mappings()
            .one()
        )


def response(payload: Any = None, *, status: int = 200) -> httpx.Response:
    if payload is None:
        payload = [{"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "USD"}]
    return httpx.Response(
        status,
        stream=httpx.ByteStream(json.dumps(payload).encode()),
        headers={"content-type": "application/json"},
    )


def unchanged_finances(before: Any, after: Any) -> None:
    quota = {"provider_budgets", "upstream_attempts", "upstream_outcomes"}
    financial_same(
        {t: rows for t, rows in before.items() if t not in quota},
        {t: rows for t, rows in after.items() if t not in quota},
    )


async def test_worker_reserves_before_real_adapter_io_and_commits_the_tail_atomically(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)
    before = database_snapshot(engine)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        with unit_of_work(engine, write=True) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
            assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        return response()

    assert await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert calls == ["/api/div/0005.HK"] and state(engine, identifier)["status"] == "SUCCEEDED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT dps_e8,currency,adjustment_basis FROM dividend_observations"
        ).one() == (90000000, "USD", "UNKNOWN")
        assert con.exec_driver_sql(
            "SELECT status,attempt_count,round_open FROM job_items"
        ).one() == ("SUCCEEDED", 1, 0)
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "COMPLETED"
        )
    unchanged_finances(before, database_snapshot(engine))


async def test_idle_worker_does_not_write_or_enable_traffic(
    book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    assert not await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP")),
    )
    assert database_snapshot(engine) == before


async def test_null_queued_selection_cannot_activate_after_configuration_appears(
    book: dict[str, Any], engine: Engine
) -> None:
    plan = plan_for(book).model_dump(mode="json")
    plan["providers"][0]["profile_sha256"] = None
    identifier = enqueue(book, engine, SourceJobPlan.model_validate(plan))
    assert await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP")),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT status,last_error_code,attempt_count FROM job_items"
        ).one() == ("SKIPPED", "UNCONFIGURED", 0)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


async def test_worker_parks_retries_and_exhausts_only_four_logical_attempts(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)
    clock = Clock()
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return response(status=503)

    for attempt in range(1, 5):
        assert await dispatch_once(
            engine, book["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
        )
        row = state(engine, identifier)
        assert row["attempt_count"] == attempt
        if attempt < 4:
            assert row["status"] == "RETRYING" and row["lease_owner"] is None
            deadline = datetime.fromisoformat(row["next_run_at"])
            assert deadline >= clock.current + timedelta(seconds=2 ** (attempt - 1))
            accepted = database_snapshot(engine)
            assert not await dispatch_once(
                engine,
                book["settings"],
                uuid4(),
                clock=clock,
                transport=httpx.MockTransport(handle),
            )
            assert database_snapshot(engine) == accepted
            clock.current = deadline
        else:
            assert row["status"] == "FAILED"
    assert len(calls) == 4
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 4
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 4
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 0


async def test_configuration_changed_during_http_discards_response_without_refunding(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        book["profile"].write_text(json.dumps(policy(version=2)))
        return response()

    assert await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT disposition,reason FROM upstream_outcomes").one() == (
            "ABANDONED",
            "CONFIG_CHANGED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1


async def test_cancelled_held_response_is_not_captured_and_spending_remains(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        with unit_of_work(engine, write=True) as con:
            jobs.request_cancel(con, UUID(book["owner"]["id"]), identifier, at=START)
        return response()

    assert await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "CANCELLED"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "CANCELLED"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1


def massive_page(index: int, next_index: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "OK",
        "results": [
            {
                "id": f"synthetic-{index}",
                "ticker": "BRK.B",
                "ex_dividend_date": "2026-01-08",
                "cash_amount": "0.1",
                "currency": "USD",
            }
        ],
    }
    if next_index is not None:
        body["next_url"] = f"https://api.massive.com/stocks/v1/dividends?cursor={next_index}"
    return body


async def provider_case(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    provider: Any,
) -> dict[str, Any]:
    await create(
        client,
        f"securities/{book['security']['id']}/provider-symbols",
        {
            "provider_id": provider,
            "provider_symbol": "0005.HK",
            "effective_from": "2020-01-01",
        },
    )
    provision(book["settings"], provider)
    loaded = ProviderConfigurationStore(book["settings"]).load(provider)
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(book["security"]["id"]), provider, WINDOW).segments[0]
    prepared = prepare_fetch(UUID(book["owner"]["id"]), loaded, segment, "DIVIDENDS")
    return {**book, "prepared": prepared}


async def test_worker_follows_every_massive_page_with_the_original_context_and_one_round(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "0"))
        calls.append(index)
        return response(massive_page(index, index + 1 if index < 2 else None))

    assert await dispatch_once(
        engine, case["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert calls == [0, 1, 2] and state(engine, UUID(case["job"]["id"]))["status"] == "SUCCEEDED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_fetch_contexts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 3
        assert con.exec_driver_sql("SELECT attempt_count FROM job_items").scalar_one() == 1
        assert con.exec_driver_sql(
            "SELECT requested_cursor FROM provider_runs ORDER BY rowid"
        ).scalars().all() == [
            None,
            "/stocks/v1/dividends?cursor=1",
            "/stocks/v1/dividends?cursor=2",
        ]


async def test_mid_page_budget_wait_reopens_the_engine_and_resumes_the_same_round(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine, minute=1, day=20)
    clock, calls = Clock(), []

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "0"))
        calls.append(index)
        return response(massive_page(index, 1 if index == 0 else None))

    assert await dispatch_once(
        engine, case["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
    )
    row = state(engine, UUID(case["job"]["id"]))
    assert row["status"] == "RETRYING" and row["attempt_count"] == 1
    assert row["next_run_at"] == "2026-01-10T00:01:00.000000Z"
    engine.dispose()
    reopened = make_engine(case["settings"].database_path)
    try:
        clock.current += timedelta(seconds=60)
        assert await dispatch_once(
            reopened, case["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
        )
        final = state(reopened, UUID(case["job"]["id"]))
        assert final["status"] == "SUCCEEDED" and final["attempt_count"] == 1
        assert final["lease_generation"] == 2 and calls == [0, 1]
        with unit_of_work(reopened) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 2
            assert (
                con.exec_driver_sql("SELECT count(*) FROM provider_fetch_contexts").scalar_one()
                == 1
            )
    finally:
        reopened.dispose()


async def test_later_page_failures_keep_the_prefix_and_finish_partial_at_four_rounds(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine, minute=100, day=100)
    identifier, clock, calls = UUID(case["job"]["id"]), Clock(), []

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "0"))
        calls.append(index)
        return response(massive_page(0, 1)) if index == 0 else response(status=503)

    for round_number in range(1, 5):
        assert await dispatch_once(
            engine, case["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
        )
        row = state(engine, identifier)
        assert row["attempt_count"] == round_number
        if round_number < 4:
            assert row["status"] == "RETRYING"
            clock.current = datetime.fromisoformat(row["next_run_at"])
        else:
            assert row["status"] == "PARTIAL"
    assert calls == [0, 1, 1, 1, 1]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 1


async def test_returned_longer_cursor_cycle_keeps_valid_last_page_without_another_request(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "0"))
        calls.append(index)
        return response(massive_page(index, 1 if index == 2 else index + 1))

    await dispatch_once(
        engine, case["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert calls == [0, 1, 2]
    assert state(engine, UUID(case["job"]["id"]))["status"] == "PARTIAL"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT last_error_code FROM job_items").scalar_one()
            == "CURSOR_CYCLE"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 3


async def test_page_ceiling_rejects_the_257th_request_before_spending(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine, minute=1000, day=1000)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "0"))
        calls.append(index)
        return response(massive_page(index, index + 1))

    await dispatch_once(
        engine, case["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert calls == list(range(256))
    assert state(engine, UUID(case["job"]["id"]))["status"] == "PARTIAL"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT last_error_code FROM job_items").scalar_one()
            == "PAGE_LIMIT"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 256
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 256


@pytest.mark.parametrize(
    "change", ["profile-missing", "key-missing", "disabled", "unacknowledged", "invalid-json"]
)
async def test_unavailable_private_configuration_skips_before_any_quota_spend(
    book: dict[str, Any], engine: Engine, change: str
) -> None:
    identifier = enqueue(book, engine)
    if change == "profile-missing":
        book["profile"].unlink()
    elif change == "key-missing":
        book["key"].unlink()
    else:
        body = policy(enabled=False) if change == "disabled" else policy(terms_acknowledged=False)
        book["profile"].write_text("invalid" if change == "invalid-json" else json.dumps(body))
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP")),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status,last_error_code FROM job_items").one() == (
            "SKIPPED",
            "UNCONFIGURED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_budgets").scalar_one() == 0


async def test_profile_pin_change_before_io_is_failure_without_a_request(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)
    book["profile"].write_text(json.dumps(policy(version=2)))
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP")),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT last_error_code,attempt_count FROM job_items").one() == (
            "CONFIG_CHANGED",
            0,
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


async def test_secret_rewrite_during_http_is_discarded_without_capture(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        book["key"].write_bytes(b"x" * len(KEY))
        return response()

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT disposition,reason FROM upstream_outcomes").one() == (
            "ABANDONED",
            "CONFIG_CHANGED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


async def test_peer_appearance_during_http_invalidates_shared_budget_snapshot(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await provider_case(book, client, engine, "yahoo")
    identifier = enqueue(case, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        provision(
            book["settings"],
            "yfinance",
            body=policy("yfinance", requests_per_minute=1, requests_per_day=1),
        )
        return response(
            {
                "chart": {
                    "error": None,
                    "result": [
                        {
                            "meta": {"symbol": "0005.HK", "exchangeTimezoneName": "Asia/Hong_Kong"},
                            "events": {},
                        }
                    ],
                }
            }
        )

    await dispatch_once(
        engine, case["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT disposition,reason FROM upstream_outcomes").one() == (
            "ABANDONED",
            "CONFIG_CHANGED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


async def test_alias_version_changed_while_http_is_held_discards_the_response(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE provider_symbols SET version=version+1 WHERE id=?",
                (str(book["prepared"].segment.alias.id),),
            )
        return response()

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT last_error_code FROM job_items").scalar_one()
            == "ALIAS_CHANGED"
        )
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "ABANDONED"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


async def test_lease_expiry_drops_a_held_result_and_recovery_spends_a_new_reservation(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier, clock, calls = enqueue(book, engine), Clock(), []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            clock.current += timedelta(seconds=90)
        return response()

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
    await dispatch_once(
        engine, book["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 2 and state(engine, identifier)["status"] == "SUCCEEDED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 2
        assert con.exec_driver_sql(
            "SELECT disposition FROM upstream_outcomes ORDER BY rowid"
        ).scalars().all() == ["ABANDONED", "COMPLETED"]
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 1


async def test_one_unmapped_symbol_does_not_stop_the_other_symbol_or_change_finances(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    unavailable = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0700",
            "name": "Synthetic unmapped symbol",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    plan = plan_for(book).model_copy(
        update={"security_ids": (UUID(book["security"]["id"]), UUID(unavailable["id"]))}
    )
    identifier = enqueue(book, engine, plan)
    before, calls = database_snapshot(engine), []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return response()

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    row = state(engine, identifier)
    assert row["status"] == "PARTIAL" and row["progress_done"] == 2 and len(calls) == 1
    unchanged_finances(before, database_snapshot(engine))


async def test_two_dispatchers_cannot_send_the_same_job_twice(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        entered.set()
        await release.wait()
        return response()

    first = asyncio.create_task(
        dispatch_once(
            engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
        )
    )
    await entered.wait()
    assert not await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    release.set()
    assert await first and len(calls) == 1 and state(engine, identifier)["status"] == "SUCCEEDED"


async def test_unexpected_adapter_exception_never_persists_private_details(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("private-provider-body:" + KEY.decode())

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "FAILED"
    snapshot = database_snapshot(engine)
    assert KEY.decode() not in repr(snapshot) and "private-provider-body" not in repr(snapshot)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT last_error_code FROM job_items").scalar_one()
            == "WORKER_ERROR"
        )
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "ABANDONED"
        )


async def test_absolute_retry_after_parks_without_clamping_or_sleep(
    book: dict[str, Any], engine: Engine
) -> None:
    identifier = enqueue(book, engine)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            stream=httpx.ByteStream(b"{}"),
            headers={
                "Retry-After": "Fri, 31 Dec 9999 23:59:59 GMT",
                "content-type": "application/json",
            },
        )

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    row = state(engine, identifier)
    assert row["status"] == "RETRYING" and row["next_run_at"] == "9999-12-31T23:59:59.000000Z"
    accepted = database_snapshot(engine)
    assert not await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Early retry")),
    )
    assert database_snapshot(engine) == accepted


@pytest.mark.parametrize("stop_at,expected_spends", [(1, 0), (2, 1)])
async def test_shutdown_observed_during_preflight_or_after_grant_starts_no_http(
    book: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stop_at: int,
    expected_spends: int,
) -> None:
    identifier = enqueue(book, engine)
    original = BudgetSnapshot.unchanged
    stopped, checks = False, 0

    def checked(snapshot: BudgetSnapshot, store: ProviderConfigurationStore) -> bool:
        nonlocal stopped, checks
        checks += 1
        if checks == stop_at:
            stopped = True
        return original(snapshot, store)

    monkeypatch.setattr(BudgetSnapshot, "unchanged", checked)
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        stopping=lambda: stopped,
        transport=httpx.MockTransport(lambda _: pytest.fail("Stopped worker started HTTP")),
    )
    assert state(engine, identifier)["status"] == "RUNNING" and checks == stop_at
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one()
            == expected_spends
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


async def test_missing_sdk_is_detected_before_any_request_or_quota_reservation(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = await provider_case(book, client, engine, "yfinance")
    identifier = enqueue(case, engine)

    def unavailable(name: str) -> str:
        raise PackageNotFoundError("synthetic-missing-sdk")

    monkeypatch.setattr(sdk_bridge, "version", unavailable)
    await dispatch_once(
        engine,
        case["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("SDK readiness spent quota")),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status,last_error_code FROM job_items").one() == (
            "SKIPPED",
            "UNCONFIGURED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


@pytest.mark.parametrize("provider", ["manual", "hkex_evidence"])
async def test_background_manual_sources_require_explicit_cited_input_not_fake_fetches(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, provider: str
) -> None:
    case = await provider_case(book, client, engine, provider)
    identifier = enqueue(case, engine)
    await dispatch_once(
        engine,
        case["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Manual source fetched a document")),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status,last_error_code FROM job_items").one() == (
            "SKIPPED",
            "UNSUPPORTED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_budgets").scalar_one() == 0


@pytest.mark.parametrize("operation", ["SPLITS", "DIVIDENDS"])
async def test_unsupported_operation_or_market_cannot_spend_a_request(
    book: dict[str, Any], engine: Engine, operation: str
) -> None:
    provision(book["settings"], "alphavantage")
    loaded = ProviderConfigurationStore(book["settings"]).load("alphavantage")
    assert loaded.revision is not None
    plan = SourceJobPlan.model_validate(
        {
            "security_ids": [book["security"]["id"]],
            "providers": [
                {
                    "provider_id": "alphavantage",
                    "profile_sha256": loaded.revision.profile_sha256,
                }
            ],
            "window": WINDOW.model_dump(),
            "operations": [operation],
        }
    )
    identifier = enqueue(book, engine, plan)
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Unsupported work fetched")),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status,last_error_code FROM job_items").one() == (
            "SKIPPED",
            "UNSUPPORTED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


async def test_split_dispatch_preserves_every_int64_ratio_digit_without_ledger_adoption(
    book: dict[str, Any], engine: Engine
) -> None:
    plan = plan_for(book).model_copy(update={"operations": ("SPLITS",)})
    identifier = enqueue(book, engine, plan)
    before = database_snapshot(engine)
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(
            lambda _: response(
                [
                    {
                        "date": "2026-01-08",
                        "split": "9223372036854775807/9223372036854775806",
                    }
                ]
            )
        ),
    )
    assert state(engine, identifier)["status"] == "SUCCEEDED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT ratio_num,ratio_den,effect_scope FROM split_observations"
        ).one() == (9223372036854775807, 9223372036854775806, "PROVIDER_ADJUSTMENT_ONLY")
    unchanged_finances(before, database_snapshot(engine))


@pytest.mark.parametrize("outcome", ["empty", "invalid", "partial"])
async def test_empty_invalid_and_partial_pages_have_distinct_truthful_results(
    book: dict[str, Any], engine: Engine, outcome: str
) -> None:
    identifier = enqueue(book, engine)
    body: Any = [] if outcome == "empty" else {"invalid": True}
    if outcome == "partial":
        body = [
            {"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "USD"},
            {"date": "not-a-date", "value": "0.5"},
        ]
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: response(body)),
    )
    assert (
        state(engine, identifier)["status"]
        == {"empty": "SUCCEEDED", "invalid": "FAILED", "partial": "PARTIAL"}[outcome]
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == (
            1 if outcome == "partial" else 0
        )
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == (
            1 if outcome == "empty" else 0
        )
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_events").scalar_one() == 0


async def test_contiguous_dated_alias_segments_are_both_processed_without_symbol_guessing(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE provider_symbols SET effective_to='2026-01-15',version=version+1 WHERE id=?",
            (str(book["prepared"].segment.alias.id),),
        )
    await create(
        client,
        f"securities/{book['security']['id']}/provider-symbols",
        {
            "provider_id": "eodhd",
            "provider_symbol": "0005.NEW.HK",
            "effective_from": "2026-01-16",
        },
    )
    identifier, calls = enqueue(book, engine), []

    def handle(request: httpx.Request) -> httpx.Response:
        first = request.url.params["from"] == "2026-01-01"
        calls.append((request.url.path, request.url.params["from"], request.url.params["to"]))
        return response(
            [{"date": "2026-01-08" if first else "2026-01-20", "value": "0.1", "currency": "USD"}]
        )

    await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), transport=httpx.MockTransport(handle)
    )
    assert state(engine, identifier)["status"] == "SUCCEEDED"
    assert calls == [
        ("/api/div/0005.HK", "2026-01-01", "2026-01-15"),
        ("/api/div/0005.NEW.HK", "2026-01-16", "2026-01-31"),
    ]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_fetch_contexts").scalar_one() == 2
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 2
        assert con.exec_driver_sql("SELECT attempt_count FROM job_items").scalar_one() == 1


async def test_lease_is_renewed_before_io_without_changing_generation_or_work_round(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine)
    clock = Clock()

    def handle(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("cursor", "0"))
        clock.current += timedelta(seconds=40)
        return response(massive_page(index, index + 1 if index < 2 else None))

    await dispatch_once(
        engine, case["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
    )
    row = state(engine, UUID(case["job"]["id"]))
    assert row["status"] == "SUCCEEDED" and row["lease_generation"] == row["attempt_count"] == 1
    with unit_of_work(engine) as con:
        expiries = (
            con.exec_driver_sql("SELECT expires_at FROM upstream_attempts ORDER BY rowid")
            .scalars()
            .all()
        )
    assert expiries == [
        "2026-01-10T00:01:30.000000Z",
        "2026-01-10T00:01:30.000000Z",
        "2026-01-10T00:02:50.000000Z",
    ]


@pytest.mark.parametrize("change", ["profile", "alias"])
async def test_changed_configuration_after_a_parked_prefix_finishes_partial_without_a_new_round(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, change: str
) -> None:
    case = await massive_work(book, client, engine, minute=1, day=20)
    clock = Clock()
    await dispatch_once(
        engine,
        case["settings"],
        uuid4(),
        clock=clock,
        transport=httpx.MockTransport(lambda _: response(massive_page(0, 1))),
    )
    if change == "profile":
        provision(
            case["settings"],
            "massive",
            body=policy("massive", version=2, requests_per_minute=1, requests_per_day=20),
        )
    else:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE provider_symbols SET version=version+1 WHERE id=?",
                (str(case["prepared"].segment.alias.id),),
            )
    clock.current += timedelta(seconds=60)
    await dispatch_once(
        engine,
        case["settings"],
        uuid4(),
        clock=clock,
        transport=httpx.MockTransport(lambda _: pytest.fail("Changed prefix issued HTTP")),
    )
    row = state(engine, UUID(case["job"]["id"]))
    assert row["status"] == "PARTIAL" and row["attempt_count"] == 1
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT last_error_code FROM job_items").scalar_one() == (
            "CONFIG_CHANGED" if change == "profile" else "ALIAS_CHANGED"
        )


@pytest.mark.parametrize(
    "end,rounds,outcome",
    [
        ("2026-01-15", 4, "PARTIAL"),
        ("2026-01-31", 1, "SUCCEEDED"),
        ("2026-01-31", 4, "SUCCEEDED"),
    ],
)
async def test_retained_checkpoint_recovery_finishes_tail_or_exhausted_prefix_without_new_io(
    book: dict[str, Any], engine: Engine, end: str, rounds: int, outcome: str
) -> None:
    identifier, clock = enqueue(book, engine), Clock()
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        item = UUID(
            con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=?", (str(identifier),)
            ).scalar_one()
        )
        jobs.begin_item(con, lease, item, at=clock())
        for _ in range(rounds - 1):
            until = clock.current + timedelta(seconds=1)
            jobs.retry_item(
                con, lease, item, at=clock(), until=until, error=WorkError.TRANSIENT_ERROR
            )
            clock.current = until
            jobs.begin_item(con, lease, item, at=clock())
    segment = book["prepared"].segment.model_copy(
        update={"window": FetchWindow(start=WINDOW.start, end=end)}
    )
    prepared = prepare_fetch(
        UUID(book["owner"]["id"]), book["prepared"].configuration, segment, "DIVIDENDS"
    )
    page = await fetched({**book, "prepared": prepared})
    with unit_of_work(engine, write=True) as con:
        jobs.record_page(
            con,
            lease,
            item,
            prepared,
            page,
            run_id=uuid4(),
            started_at=clock(),
            finished_at=clock(),
            duration_ms=0,
            at=clock(),
        )
    clock.current = lease.expires_at
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=clock,
        transport=httpx.MockTransport(lambda _: pytest.fail("Retained tail refetched")),
    )
    row = state(engine, identifier)
    assert row["status"] == outcome and row["attempt_count"] == rounds
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 1


async def test_shutdown_between_pages_retains_prefix_and_restart_does_not_refetch_it(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = await massive_work(book, client, engine)
    clock, calls, stopped = Clock(), [], False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal stopped
        index = int(request.url.params.get("cursor", "0"))
        calls.append(index)
        stopped = True
        return response(massive_page(index, 1 if index == 0 else None))

    await dispatch_once(
        engine,
        case["settings"],
        uuid4(),
        clock=clock,
        stopping=lambda: stopped,
        transport=httpx.MockTransport(handle),
    )
    assert calls == [0] and state(engine, UUID(case["job"]["id"]))["status"] == "RUNNING"
    clock.current += timedelta(seconds=90)
    await dispatch_once(
        engine, case["settings"], uuid4(), clock=clock, transport=httpx.MockTransport(handle)
    )
    row = state(engine, UUID(case["job"]["id"]))
    assert calls == [0, 1] and row["status"] == "SUCCEEDED" and row["attempt_count"] == 2


async def test_stopped_worker_cannot_claim_a_job(book: dict[str, Any], engine: Engine) -> None:
    enqueue(book, engine)
    before = database_snapshot(engine)
    assert not await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(), stopping=lambda: True
    )
    assert database_snapshot(engine) == before


async def test_real_leader_keeps_heartbeat_fresh_while_provider_io_is_held(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue(book, engine)
    stop, entered, release = Event(), asyncio.Event(), asyncio.Event()
    gates: asyncio.Queue[None] = asyncio.Queue()
    beats: asyncio.Queue[None] = asyncio.Queue()
    wall = [100]
    original_sleep, original_heartbeat = asyncio.sleep, worker_main.heartbeat

    async def delay(seconds: float) -> None:
        if seconds == 5:
            await gates.get()
        else:
            await original_sleep(0)

    def heartbeat(*args: Any) -> None:
        original_heartbeat(*args)
        beats.put_nowait(None)

    async def handle(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return response()

    async def work(*args: Any, **kwargs: Any) -> bool:
        result = await dispatch_once(*args, **kwargs, transport=httpx.MockTransport(handle))
        stop.set()
        gates.put_nowait(None)
        return result

    monkeypatch.setattr("spm_worker.main.time.time", lambda: wall[0])
    monkeypatch.setattr(worker_main, "heartbeat", heartbeat)
    monkeypatch.setattr(asyncio, "sleep", delay)
    monkeypatch.setattr(dispatcher, "dispatch_once", work)
    task = asyncio.create_task(worker_main.run_leader(book["settings"], engine, uuid4(), stop))
    await beats.get()
    await entered.wait()
    wall[0] = 131
    assert not worker_main.healthy(book["settings"].data_dir)
    gates.put_nowait(None)
    await beats.get()
    assert worker_main.healthy(book["settings"].data_dir) and not task.done()
    release.set()
    await task


async def test_supervisor_error_log_never_includes_exception_details(
    book: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop, wake = Event(), asyncio.Event()

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("private-worker-failure:" + KEY.decode())

    async def delay(seconds: float) -> None:
        if seconds == 1:
            stop.set()
            wake.set()
        else:
            await wake.wait()

    monkeypatch.setattr(dispatcher, "dispatch_once", broken)
    monkeypatch.setattr(asyncio, "sleep", delay)
    await worker_main.run_leader(book["settings"], engine, uuid4(), stop)
    assert "SOURCE_DISPATCH_FAILED" in caplog.text
    assert KEY.decode() not in caplog.text and "private-worker-failure" not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_errors_open_circuit_without_retrying_or_creating_dividends(
    book: dict[str, Any], engine: Engine, status: int
) -> None:
    identifier = enqueue(book, engine)
    await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(),
        transport=httpx.MockTransport(lambda _: response(status=status)),
    )
    assert state(engine, identifier)["status"] == "FAILED"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT circuit_state FROM provider_budgets").scalar_one() == "OPEN"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 0
