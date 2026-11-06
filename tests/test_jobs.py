"""Real SQLite queue control, independent work, fenced source checkpoints and crash recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.job_service as jobs
from spm_api.config import Settings
from spm_api.db import make_engine, unit_of_work
from spm_api.job_models import JobLease, ProviderSelection, SourceJobPlan, WorkError
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.symbols import FetchWindow, resolve_symbol
from tests.helpers import create, database_snapshot
from tests.test_provider_configuration import KEY, policy, provision
from tests.test_provider_persistence import (
    FINISH,
    START,
    WINDOW,
    assert_financial_unchanged,
    fetched,
)
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


def plan_for(book: dict[str, Any], **changes: Any) -> SourceJobPlan:
    configuration = book["prepared"].configuration
    return SourceJobPlan(
        security_ids=(UUID(book["security"]["id"]),),
        providers=(
            ProviderSelection(
                provider_id=configuration.provider_id,
                profile_sha256=configuration.revision.profile_sha256,
            ),
        ),
        window=WINDOW,
        operations=("DIVIDENDS",),
        **changes,
    )


@pytest.fixture
async def queued(book: dict[str, Any], engine: Engine) -> dict[str, Any]:
    plan = plan_for(book)
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=START)
        item = con.exec_driver_sql(
            "SELECT id FROM job_items WHERE job_id=?", (job["id"],)
        ).scalar_one()
    return {"job": job, "plan": plan, "item": UUID(item), "owner": UUID(book["owner"]["id"])}


def active(engine: Engine, *, at: datetime = START, worker: UUID | None = None) -> JobLease:
    with unit_of_work(engine, write=True) as con:
        lease = jobs.claim(con, worker or uuid4(), at=at)
        assert lease is not None
        jobs.start(con, lease, at=at)
    return lease


def job_state(engine: Engine, queued: dict[str, Any]) -> dict[str, Any]:
    with unit_of_work(engine) as con:
        return jobs.owned_job(con, queued["owner"], UUID(queued["job"]["id"]))


def record(
    engine: Engine,
    lease: JobLease,
    item: UUID,
    book: dict[str, Any],
    page: Any,
    *,
    run: UUID | None = None,
    at: datetime = START + timedelta(seconds=1),
    **extra: Any,
) -> Any:
    with unit_of_work(engine, write=True) as con:
        return jobs.record_page(
            con,
            lease,
            item,
            book["prepared"],
            page,
            run_id=run or uuid4(),
            started_at=START,
            finished_at=FINISH,
            duration_ms=12,
            at=at,
            **extra,
        )


def financial_same(before: Any, after: Any) -> None:
    control = {"jobs", "job_items", "job_checkpoints"}
    assert_financial_unchanged(
        {name: rows for name, rows in before.items() if name not in control},
        {name: rows for name, rows in after.items() if name not in control},
    )


async def massive_work(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    *,
    minute: int = 5,
    day: int = 50,
) -> dict[str, Any]:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "BRK.B",
            "name": "Synthetic cursor source",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "massive",
            "provider_symbol": "BRK.B",
            "effective_from": "2020-01-01",
        },
    )
    provision(
        book["settings"],
        "massive",
        body=policy("massive", requests_per_minute=minute, requests_per_day=day),
    )
    loaded = ProviderConfigurationStore(book["settings"]).load("massive")
    assert loaded.error_code is None and loaded.revision is not None
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(security["id"]), "massive", WINDOW).segments[0]
    prepared = prepare_fetch(UUID(book["owner"]["id"]), loaded, segment, "DIVIDENDS")
    current = {**book, "security": security, "prepared": prepared}
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, prepared.owner_id, plan_for(current), intent_id=uuid4(), at=START)
        item = UUID(
            con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=?", (job["id"],)
            ).scalar_one()
        )
    return {**current, "job": job, "item": item}


async def test_actual_massive_pages_pin_input_cursor_and_checkpoint_without_restarting_stock(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    current = await massive_work(book, client, engine)
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, current["item"], at=START)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        second = request.url.params.get("cursor") == "second"
        body = {
            "status": "OK",
            "results": [
                {
                    "id": "second" if second else "first",
                    "ticker": "BRK.B",
                    "ex_dividend_date": "2026-01-20" if second else "2026-01-08",
                    "cash_amount": "0.2" if second else "0.1",
                    "currency": "USD",
                }
            ],
        }
        if not second:
            body["next_url"] = "https://api.massive.com/stocks/v1/dividends?cursor=second"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(body).encode()),
            headers={"content-type": "application/json"},
        )

    async with configured_adapter(
        current["prepared"].configuration, transport=httpx.MockTransport(respond)
    ) as adapter:
        first = await adapter.fetch_dividends(current["prepared"].segment)
        assert first.next_cursor is not None
        record(engine, lease, current["item"], current, first)
        with (
            pytest.raises(jobs.JobError, match="JOB_WINDOW_NOT_PROCESSED"),
            unit_of_work(engine, write=True) as con,
        ):
            jobs.finish_item(
                con, lease, current["item"], at=START + timedelta(seconds=1), status="SUCCEEDED"
            )
        second = await adapter.fetch_dividends(current["prepared"].segment, first.next_cursor)
    run = uuid4()
    record(engine, lease, current["item"], current, second, run=run, cursor_in=first.next_cursor)
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(
            con, lease, current["item"], at=START + timedelta(seconds=2), status="SUCCEEDED"
        )
        jobs.finish(con, lease, at=START + timedelta(seconds=2))
        assert con.exec_driver_sql("SELECT attempt_count FROM job_items").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT requested_cursor FROM provider_runs WHERE id=?", (str(run),)
            ).scalar_one()
            == first.next_cursor
        )
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_ATTEMPT_CONFLICT"):
        record(
            engine,
            lease,
            current["item"],
            current,
            second,
            run=run,
            cursor_in="/stocks/v1/dividends?cursor=changed",
        )
    assert database_snapshot(engine) == before
    assert len(calls) == 2


@pytest.mark.parametrize(
    "cursor",
    [
        "https://foreign.invalid/page",
        "/stocks/v1/splits?cursor=wrong-op",
        "/stocks/v1/dividends?api%5Fkey=private",
        f"/stocks/v1/dividends?cursor={KEY.decode()}",
    ],
)
async def test_requested_cursor_cannot_store_private_or_foreign_references(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, cursor: str
) -> None:
    current = await massive_work(book, client, engine)
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, current["item"], at=START)
    async with configured_adapter(
        current["prepared"].configuration,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                stream=httpx.ByteStream(b'{"status":"OK","results":[]}'),
                headers={"content-type": "application/json"},
            )
        ),
    ) as adapter:
        page = await adapter.fetch_dividends(current["prepared"].segment)
    before = database_snapshot(engine)
    with pytest.raises(ValueError):
        record(engine, lease, current["item"], current, page, cursor_in=cursor)
    assert database_snapshot(engine) == before


async def test_partial_pages_preserve_evidence_without_a_false_complete_checkpoint(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    page = await fetched(
        book,
        payload=[
            {"date": "2026-01-08", "unadjustedValue": "0.9"},
            {"date": "invalid", "value": "0.1"},
        ],
    )
    receipt = record(engine, lease, queued["item"], book, page)
    assert receipt.observations_new == 1
    with unit_of_work(engine, write=True) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 0
        jobs.finish_item(
            con,
            lease,
            queued["item"],
            at=START + timedelta(seconds=2),
            status="PARTIAL",
            error=WorkError.PARSE_ERROR,
        )
        jobs.finish(con, lease, at=START + timedelta(seconds=2))
    assert job_state(engine, queued)["status"] == "PARTIAL"


async def test_no_source_evidence_cannot_claim_partial_processing(
    queued: Any, engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    before = database_snapshot(engine)
    with (
        pytest.raises(jobs.JobError, match="JOB_PARTIAL_EVIDENCE_REQUIRED"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.finish_item(
            con, lease, queued["item"], at=START, status="PARTIAL", error=WorkError.PARSE_ERROR
        )
    assert database_snapshot(engine) == before


async def test_stale_worker_response_is_fenced_before_any_source_write_after_reclaim(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    first = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, first, queued["item"], at=START)
    held = await fetched(book)
    active(engine, at=START + timedelta(seconds=90))
    before = database_snapshot(engine)
    with pytest.raises(jobs.JobError, match="JOB_LEASE_LOST"):
        record(engine, first, queued["item"], book, held, at=START + timedelta(seconds=91))
    assert database_snapshot(engine) == before


async def test_repeated_crashes_are_bounded_and_another_work_unit_can_still_start(
    book: dict[str, Any], engine: Engine
) -> None:
    original = plan_for(book)
    plan = original.model_copy(
        update={
            "providers": (
                *original.providers,
                ProviderSelection(provider_id="manual", profile_sha256=None),
            )
        }
    )
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=START)
        items = tuple(
            UUID(value)
            for value in con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=? ORDER BY ordinal", (job["id"],)
            ).scalars()
        )
    for index in range(4):
        at = START + timedelta(seconds=index * 90)
        lease = active(engine, at=at)
        with unit_of_work(engine, write=True) as con:
            assert jobs.begin_item(con, lease, items[0], at=at)["attempt_count"] == index + 1
    at = START + timedelta(seconds=360)
    successor = active(engine, at=at)
    with unit_of_work(engine, write=True) as con:
        failed = con.exec_driver_sql(
            "SELECT status,attempt_count,last_error_code FROM job_items WHERE id=?",
            (str(items[0]),),
        ).one()
        assert failed == ("FAILED", 4, "RETRIES_EXHAUSTED")
        assert jobs.begin_item(con, successor, items[1], at=at)["attempt_count"] == 1
        jobs.finish_item(
            con, successor, items[1], at=at, status="SKIPPED", error=WorkError.UNCONFIGURED
        )
        jobs.finish(con, successor, at=at)
        final = jobs.owned_job(con, successor.owner_id, successor.job_id)
        assert final["status"] == "FAILED" and final["attempt_count"] == 4


async def test_inactive_owner_job_is_not_dispatched(queued: Any, engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(queued["owner"]),))
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert jobs.claim(con, uuid4(), at=START) is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("profile_hash", [None, "f" * 64])
@pytest.mark.parametrize("partial", [False, True])
async def test_unconfigured_or_changed_plan_cannot_capture_even_partial_or_failed_pages(
    book: dict[str, Any], engine: Engine, profile_hash: str | None, partial: bool
) -> None:
    plan = plan_for(book).model_copy(
        update={"providers": (ProviderSelection(provider_id="eodhd", profile_sha256=profile_hash),)}
    )
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=START)
        item = UUID(
            con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=?", (job["id"],)
            ).scalar_one()
        )
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, item, at=START)
    page = (
        await fetched(
            book,
            payload=[
                {"date": "2026-01-08", "unadjustedValue": "0.9"},
                {"date": "invalid", "value": "0.1"},
            ],
        )
        if partial
        else await fetched(book, status=503)
    )
    before = database_snapshot(engine)
    with pytest.raises(jobs.JobError, match="JOB_CONFIGURATION_CHANGED"):
        record(engine, lease, item, book, page)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("reason", [WorkError.QUOTA_WAIT, WorkError.CIRCUIT_OPEN])
@pytest.mark.parametrize("previous_failures", [0, 3])
async def test_mid_pagination_wait_resumes_the_same_logical_round_even_at_the_attempt_limit(
    book: dict[str, Any], engine: Engine, reason: WorkError, previous_failures: int
) -> None:
    plan = plan_for(book)
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=START)
        item = UUID(
            con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=?", (job["id"],)
            ).scalar_one()
        )
    lease = active(engine)
    for index in range(previous_failures):
        at = START + timedelta(seconds=index)
        with unit_of_work(engine, write=True) as con:
            jobs.begin_item(con, lease, item, at=at)
            jobs.retry_item(
                con,
                lease,
                item,
                at=at,
                until=at + timedelta(seconds=1),
                error=WorkError.TRANSIENT_ERROR,
            )
    at = START + timedelta(seconds=previous_failures)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, item, at=at)
    for start, end, event, seconds in [
        ("2026-01-01", "2026-01-15", "2026-01-08", 5),
        ("2026-01-16", "2026-01-31", "2026-01-20", 31),
    ]:
        with unit_of_work(engine) as con:
            segment = resolve_symbol(
                con, UUID(book["security"]["id"]), "eodhd", FetchWindow(start=start, end=end)
            ).segments[0]
        current = {
            **book,
            "prepared": prepare_fetch(
                lease.owner_id, book["prepared"].configuration, segment, "DIVIDENDS"
            ),
        }
        page = await fetched(current, payload=[{"date": event, "unadjustedValue": "0.9"}])
        record(engine, lease, item, current, page, at=START + timedelta(seconds=seconds))
        if end == "2026-01-15":
            with unit_of_work(engine, write=True) as con:
                jobs.defer_item(
                    con,
                    lease,
                    item,
                    at=START + timedelta(seconds=6),
                    until=START + timedelta(seconds=30),
                    error=reason,
                )
                jobs.park(con, lease, at=START + timedelta(seconds=6))
            lease = active(engine, at=START + timedelta(seconds=30))
            with unit_of_work(engine, write=True) as con:
                assert (
                    jobs.begin_item(con, lease, item, at=START + timedelta(seconds=30))[
                        "attempt_count"
                    ]
                    == previous_failures + 1
                )
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(con, lease, item, at=START + timedelta(seconds=32), status="SUCCEEDED")
        jobs.finish(con, lease, at=START + timedelta(seconds=32))
        final = jobs.owned_job(con, lease.owner_id, lease.job_id)
    assert final["attempt_count"] == previous_failures + 1
    assert json.loads(final["result_json"])["pages_committed"] == 2


async def test_sealed_plan_canonical_sets_and_private_extras_rejected(book: dict[str, Any]) -> None:
    first = UUID(book["security"]["id"])
    second = uuid4()
    plan = SourceJobPlan(
        security_ids=(second, first, second),
        providers=(
            ProviderSelection(provider_id="yahoo", profile_sha256=None),
            ProviderSelection(provider_id="eodhd", profile_sha256="a" * 64),
        ),
        window=WINDOW,
        operations=("SPLITS", "DIVIDENDS"),
    )
    assert plan.security_ids == tuple(sorted((first, second), key=str))
    assert [provider.provider_id for provider in plan.providers] == ["eodhd", "yahoo"]
    assert plan.operations == ("DIVIDENDS", "SPLITS")
    attribute = "force"
    with pytest.raises(ValidationError):
        setattr(plan, attribute, True)
    with pytest.raises(ValidationError):
        SourceJobPlan.model_validate({**plan.model_dump(mode="json"), "api_key": "private"})
    with pytest.raises(ValidationError):
        SourceJobPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "providers": [
                    {"provider_id": "eodhd", "profile_sha256": "a" * 64, "api_key": "private"},
                ],
            }
        )
    with pytest.raises(ValidationError):
        SourceJobPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "providers": [
                    {"provider_id": "eodhd", "profile_sha256": "a" * 64},
                    {"provider_id": "eodhd", "profile_sha256": "b" * 64},
                ],
            }
        )


@pytest.mark.parametrize(
    "change",
    [
        {"security_ids": []},
        {"providers": []},
        {"operations": []},
        {"operations": ["QUOTES"]},
        {"force": 1},
        {"schema_version": 2},
        {"scope": "VERIFIED_INCOME"},
        {"window": {"start": "2026-02-30", "end": "2026-03-01"}},
    ],
)
async def test_invalid_plans_are_not_coerced(book: dict[str, Any], change: Any) -> None:
    with pytest.raises(ValidationError):
        SourceJobPlan.model_validate({**plan_for(book).model_dump(mode="json"), **change})


async def test_enqueue_replay_is_zero_write_and_changed_intent_conflicts(
    book: dict[str, Any], engine: Engine
) -> None:
    owner, intent, plan = UUID(book["owner"]["id"]), uuid4(), plan_for(book)
    with unit_of_work(engine, write=True) as con:
        first = jobs.enqueue(con, owner, plan, intent_id=intent, at=START)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert (
            jobs.enqueue(con, owner, plan, intent_id=intent, at=START + timedelta(days=1)) == first
        )
    assert database_snapshot(engine) == before
    with (
        pytest.raises(jobs.JobError, match="JOB_INTENT_CONFLICT"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.enqueue(
            con, owner, plan.model_copy(update={"force": True}), intent_id=intent, at=START
        )
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        second = jobs.enqueue(con, owner, plan, intent_id=uuid4(), at=START)
    assert first["id"] != second["id"] and first["status"] == "QUEUED"
    financial_same(before, database_snapshot(engine))


async def test_missing_owner_security_and_read_uow_write_nothing(
    book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(jobs.JobError, match="JOB_OWNER_UNAVAILABLE"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.enqueue(con, uuid4(), plan_for(book), intent_id=uuid4(), at=START)
    with (
        pytest.raises(jobs.JobError, match="JOB_SECURITY_NOT_FOUND"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.enqueue(
            con,
            UUID(book["owner"]["id"]),
            plan_for(book).model_copy(update={"security_ids": (uuid4(),)}),
            intent_id=uuid4(),
            at=START,
        )
    with (
        pytest.raises(jobs.JobError, match="WRITE_UNIT_OF_WORK_REQUIRED"),
        unit_of_work(engine) as con,
    ):
        jobs.enqueue(con, UUID(book["owner"]["id"]), plan_for(book), intent_id=uuid4(), at=START)
    assert database_snapshot(engine) == before


async def test_two_real_concurrent_claims_cannot_own_one_job(queued: Any, engine: Engine) -> None:
    barrier = Barrier(2)

    def take() -> JobLease | None:
        barrier.wait()
        with unit_of_work(engine, write=True) as con:
            return jobs.claim(con, uuid4(), at=START)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(lambda _: take(), range(2)))
    assert sum(lease is not None for lease in leases) == 1
    assert job_state(engine, queued)["lease_generation"] == 1


async def test_expiry_same_worker_fencing_and_restart_keep_original_start(
    queued: Any, engine: Engine, settings: Settings
) -> None:
    first = active(engine)
    attribute = "generation"
    with pytest.raises(FrozenInstanceError):
        setattr(first, attribute, 2)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, first, queued["item"], at=START)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert jobs.claim(con, uuid4(), at=START + timedelta(seconds=90, microseconds=-1)) is None
    assert database_snapshot(engine) == before
    engine.dispose()
    reopened = make_engine(settings.database_path)
    try:
        second = active(reopened, worker=first.worker_id, at=START + timedelta(seconds=90))
        assert second.generation == first.generation + 1 and second.worker_id == first.worker_id
        before = database_snapshot(reopened)
        with (
            pytest.raises(jobs.JobError, match="JOB_LEASE_LOST"),
            unit_of_work(reopened, write=True) as con,
        ):
            jobs.begin_item(con, first, queued["item"], at=START + timedelta(seconds=91))
        assert database_snapshot(reopened) == before
        assert job_state(reopened, queued)["started_at"] == "2026-01-10T00:00:00.000000Z"
        with unit_of_work(reopened) as con:
            assert con.exec_driver_sql("SELECT status,attempt_count FROM job_items").one() == (
                "RETRYING",
                1,
            )
    finally:
        reopened.dispose()


async def test_renew_never_shortens_and_old_snapshot_keeps_same_generation(
    queued: Any, engine: Engine
) -> None:
    lease = active(engine)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert jobs.renew(con, lease, at=START, lease_seconds=30).expires_at == lease.expires_at
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        later = jobs.renew(con, lease, at=START + timedelta(seconds=20), lease_seconds=90)
        assert later.expires_at > lease.expires_at
        jobs.begin_item(con, lease, queued["item"], at=START + timedelta(seconds=21))
    before = database_snapshot(engine)
    with (
        pytest.raises(jobs.JobError, match="JOB_CLOCK_REGRESSION"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.renew(con, lease, at=START + timedelta(seconds=19))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("seconds", [True, 0, 29, 301, 90.0])
async def test_lease_duration_is_bounded_strict_and_does_not_spend_attempts(
    queued: Any, engine: Engine, seconds: Any
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(jobs.JobError, match="INVALID_LEASE_DURATION"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.claim(con, uuid4(), at=START, lease_seconds=seconds)
    assert database_snapshot(engine) == before


async def test_naive_and_overflowing_job_times_are_not_invented(
    queued: Any, engine: Engine
) -> None:
    before = database_snapshot(engine)
    for at in (START.replace(tzinfo=None), datetime.max.replace(tzinfo=UTC)):
        with pytest.raises(jobs.JobError), unit_of_work(engine, write=True) as con:
            jobs.claim(con, uuid4(), at=at)
    assert database_snapshot(engine) == before


async def test_source_capture_checkpoint_success_and_replay_leave_financial_facts_unchanged(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    page = await fetched(book)
    run = uuid4()
    first = record(engine, lease, queued["item"], book, page, run=run)
    committed = database_snapshot(engine)
    assert record(engine, lease, queued["item"], book, page, run=run) == first
    assert database_snapshot(engine) == committed
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(
            con, lease, queued["item"], at=START + timedelta(seconds=2), status="SUCCEEDED"
        )
        jobs.finish(con, lease, at=START + timedelta(seconds=2))
    state = job_state(engine, queued)
    assert state["status"] == "SUCCEEDED" and state["progress_done"] == state["progress_total"] == 1
    assert json.loads(state["result_json"])["observations_new"] == 1
    assert json.loads(state["result_json"])["pages_committed"] == 1
    assert state["lease_owner"] is state["lease_expires_at"] is None
    committed = database_snapshot(engine)
    assert record(engine, lease, queued["item"], book, page, run=run) == first
    assert database_snapshot(engine) == committed
    financial_same(before, committed)


async def test_success_without_a_complete_processed_window_is_rejected(
    queued: Any, engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    before = database_snapshot(engine)
    with (
        pytest.raises(jobs.JobError, match="JOB_WINDOW_NOT_PROCESSED"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.finish_item(con, lease, queued["item"], at=START, status="SUCCEEDED")
    assert database_snapshot(engine) == before
    with (
        pytest.raises(jobs.JobError, match="JOB_NOT_FINISHED"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.finish(con, lease, at=START)
    assert database_snapshot(engine) == before


async def test_held_fetched_response_cannot_commit_after_cancel_and_other_owner_cannot_cancel(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    held = await fetched(book)
    before = database_snapshot(engine)
    with (
        pytest.raises(jobs.JobError, match="JOB_NOT_FOUND"),
        unit_of_work(engine, write=True) as con,
    ):
        jobs.request_cancel(con, uuid4(), lease.job_id, at=START)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        assert (
            jobs.request_cancel(con, lease.owner_id, lease.job_id, at=START)["status"]
            == "CANCEL_REQUESTED"
        )
    cancelled = database_snapshot(engine)
    with pytest.raises(jobs.JobError, match="JOB_LEASE_LOST"):
        record(engine, lease, queued["item"], book, held)
    assert database_snapshot(engine) == cancelled
    with unit_of_work(engine, write=True) as con:
        jobs.acknowledge_cancel(con, lease, at=START + timedelta(seconds=1))
    assert job_state(engine, queued)["status"] == "CANCELLED"
    assert job_state(engine, queued)["progress_done"] == 0
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


@pytest.mark.parametrize("leased", [False, True])
async def test_queued_and_expired_cancellation_is_recovered_without_running(
    queued: Any, engine: Engine, leased: bool
) -> None:
    if leased:
        lease = active(engine)
        with unit_of_work(engine, write=True) as con:
            jobs.request_cancel(con, lease.owner_id, lease.job_id, at=START)
            assert jobs.claim(con, uuid4(), at=START + timedelta(seconds=90)) is None
    else:
        with unit_of_work(engine, write=True) as con:
            jobs.request_cancel(con, queued["owner"], UUID(queued["job"]["id"]), at=START)
    state = job_state(engine, queued)
    assert state["status"] == "CANCELLED" and state["progress_done"] == state["attempt_count"] == 0
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert jobs.request_cancel(con, queued["owner"], UUID(state["id"]), at=START) == state
    assert database_snapshot(engine) == before


async def test_cancellation_keeps_a_committed_source_checkpoint_and_its_replay(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    page, run = await fetched(book), uuid4()
    receipt = record(engine, lease, queued["item"], book, page, run=run)
    with unit_of_work(engine, write=True) as con:
        jobs.request_cancel(con, lease.owner_id, lease.job_id, at=START + timedelta(seconds=2))
        jobs.acknowledge_cancel(con, lease, at=START + timedelta(seconds=2))
    before = database_snapshot(engine)
    assert record(engine, lease, queued["item"], book, page, run=run) == receipt
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 1


async def test_retry_is_durable_not_early_and_preserves_success_after_reclaim(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    failed = await fetched(book, status=503)
    record(engine, lease, queued["item"], book, failed)
    with unit_of_work(engine, write=True) as con:
        jobs.retry_item(
            con,
            lease,
            queued["item"],
            at=START + timedelta(seconds=2),
            until=START + timedelta(seconds=10),
            error=WorkError.TRANSIENT_ERROR,
        )
        jobs.park(con, lease, at=START + timedelta(seconds=2))
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert jobs.claim(con, uuid4(), at=START + timedelta(seconds=10, microseconds=-1)) is None
    assert database_snapshot(engine) == before
    second = active(engine, at=START + timedelta(seconds=10))
    with unit_of_work(engine, write=True) as con:
        assert (
            jobs.begin_item(con, second, queued["item"], at=START + timedelta(seconds=10))[
                "attempt_count"
            ]
            == 2
        )
    record(
        engine, second, queued["item"], book, await fetched(book), at=START + timedelta(seconds=11)
    )
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(
            con, second, queued["item"], at=START + timedelta(seconds=12), status="SUCCEEDED"
        )
        jobs.finish(con, second, at=START + timedelta(seconds=12))
    state = job_state(engine, queued)
    assert state["status"] == "SUCCEEDED" and state["attempt_count"] == 2
    assert state["last_error_code"] is None
    assert json.loads(state["result_json"])["pages_committed"] == 1
    financial_same(before, database_snapshot(engine))


async def test_four_bounded_work_attempts_fail_without_a_fifth_request(
    queued: Any, engine: Engine
) -> None:
    lease = active(engine)
    for index in range(4):
        at = START + timedelta(seconds=index)
        with unit_of_work(engine, write=True) as con:
            assert jobs.begin_item(con, lease, queued["item"], at=at)["attempt_count"] == index + 1
            jobs.retry_item(
                con,
                lease,
                queued["item"],
                at=at,
                until=at + timedelta(seconds=1),
                error=WorkError.TRANSIENT_ERROR,
            )
    with unit_of_work(engine, write=True) as con:
        jobs.finish(con, lease, at=START + timedelta(seconds=4))
    assert job_state(engine, queued)["status"] == "FAILED"
    assert job_state(engine, queued)["attempt_count"] == 4
    assert job_state(engine, queued)["last_error_code"] == "RETRIES_EXHAUSTED"


@pytest.mark.parametrize("reason", [WorkError.QUOTA_WAIT, WorkError.CIRCUIT_OPEN])
async def test_quota_or_circuit_wait_does_not_consume_work_attempts_or_clamp_deadline(
    queued: Any, engine: Engine, reason: WorkError
) -> None:
    lease = active(engine)
    deadline = datetime.max.replace(tzinfo=UTC)
    with unit_of_work(engine, write=True) as con:
        jobs.defer_item(con, lease, queued["item"], at=START, until=deadline, error=reason)
        jobs.park(con, lease, at=START)
    state = job_state(engine, queued)
    assert state["status"] == "RETRYING" and state["attempt_count"] == 0
    assert state["next_run_at"] == "9999-12-31T23:59:59.999999Z"
    with unit_of_work(engine, write=True) as con:
        assert jobs.claim(con, uuid4(), at=START + timedelta(days=1000)) is None


async def test_different_stock_failure_does_not_prevent_other_stock_success(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0700",
            "name": "Synthetic second source",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "eodhd",
            "provider_symbol": "0700.HK",
            "effective_from": "2020-01-01",
        },
    )
    plan = plan_for(book).model_copy(
        update={"security_ids": (UUID(book["security"]["id"]), UUID(security["id"]))}
    )
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=START)
        units = {
            row["security_id"]: UUID(row["id"])
            for row in con.exec_driver_sql(
                "SELECT id,security_id FROM job_items WHERE job_id=?", (job["id"],)
            ).mappings()
        }
    lease = active(engine)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(
            con,
            lease,
            units[book["security"]["id"]],
            at=START,
            status="FAILED",
            error=WorkError.SYMBOL_NOT_FOUND,
        )
        jobs.begin_item(con, lease, units[security["id"]], at=START)
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(security["id"]), "eodhd", WINDOW).segments[0]
    second = {
        **book,
        "prepared": prepare_fetch(
            lease.owner_id, book["prepared"].configuration, segment, "DIVIDENDS"
        ),
    }
    record(engine, lease, units[security["id"]], second, await fetched(second))
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(
            con, lease, units[security["id"]], at=START + timedelta(seconds=2), status="SUCCEEDED"
        )
        jobs.finish(con, lease, at=START + timedelta(seconds=2))
        row = jobs.owned_job(con, lease.owner_id, lease.job_id)
    assert row["status"] == "PARTIAL" and row["progress_done"] == row["progress_total"] == 2
    assert json.loads(row["result_json"])["failures"] == 1
    assert json.loads(row["result_json"])["observations_new"] == 1
    financial_same(before, database_snapshot(engine))


async def test_only_one_work_item_can_run_in_one_job(book: dict[str, Any], engine: Engine) -> None:
    original = plan_for(book)
    plan = original.model_copy(
        update={
            "providers": (
                *original.providers,
                ProviderSelection(provider_id="manual", profile_sha256=None),
            )
        }
    )
    with unit_of_work(engine, write=True) as con:
        job = jobs.enqueue(con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=START)
        items = tuple(
            UUID(value)
            for value in con.exec_driver_sql(
                "SELECT id FROM job_items WHERE job_id=? ORDER BY ordinal", (job["id"],)
            ).scalars()
        )
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, items[0], at=START)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, items[1], at=START)
    assert database_snapshot(engine) == before


async def test_atomic_checkpoint_failure_rolls_back_run_observation_and_audit(
    queued: Any, book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    page = await fetched(book)
    before = database_snapshot(engine)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Injected job audit failure")

    monkeypatch.setattr(jobs, "audit", fail)
    with pytest.raises(RuntimeError, match="Injected job audit failure"):
        record(engine, lease, queued["item"], book, page)
    assert database_snapshot(engine) == before


async def test_checkpoint_gap_rolls_back_even_the_already_inserted_source_page(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    window = FetchWindow(start="2026-01-02", end="2026-01-31")
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(book["security"]["id"]), "eodhd", window).segments[0]
    changed = {
        **book,
        "prepared": prepare_fetch(
            lease.owner_id, book["prepared"].configuration, segment, "DIVIDENDS"
        ),
    }
    page = await fetched(changed)
    before = database_snapshot(engine)
    with pytest.raises(jobs.JobError, match="JOB_CHECKPOINT_GAP"):
        record(engine, lease, queued["item"], changed, page)
    assert database_snapshot(engine) == before


async def test_contiguous_alias_windows_are_required_and_success_waits_for_the_tail(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    original = book["prepared"]
    for start, end, event in [
        ("2026-01-01", "2026-01-15", "2026-01-08"),
        ("2026-01-16", "2026-01-31", "2026-01-20"),
    ]:
        with unit_of_work(engine) as con:
            segment = resolve_symbol(
                con, UUID(book["security"]["id"]), "eodhd", FetchWindow(start=start, end=end)
            ).segments[0]
        current = {
            **book,
            "prepared": prepare_fetch(
                original.owner_id, original.configuration, segment, "DIVIDENDS"
            ),
        }
        page = await fetched(
            current, payload=[{"date": event, "unadjustedValue": "0.9", "currency": "USD"}]
        )
        record(engine, lease, queued["item"], current, page)
        if end == "2026-01-15":
            with (
                pytest.raises(jobs.JobError, match="JOB_WINDOW_NOT_PROCESSED"),
                unit_of_work(engine, write=True) as con,
            ):
                jobs.finish_item(
                    con, lease, queued["item"], at=START + timedelta(seconds=1), status="SUCCEEDED"
                )
    with unit_of_work(engine, write=True) as con:
        jobs.finish_item(
            con, lease, queued["item"], at=START + timedelta(seconds=2), status="SUCCEEDED"
        )
        jobs.finish(con, lease, at=START + timedelta(seconds=2))
    result = json.loads(job_state(engine, queued)["result_json"])
    assert result["pages_committed"] == result["observations_new"] == 2
