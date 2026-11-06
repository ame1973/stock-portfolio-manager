"""The real leader materializes only explicit due source intent, outside private I/O writers."""

import asyncio
import json
from datetime import timedelta
from threading import Barrier, Event
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.job_service as jobs
import spm_api.schedule_service as schedules
import spm_api.sync_service as source_api
import spm_worker.dispatcher as dispatcher
import spm_worker.main as worker_main
import spm_worker.scheduler as scheduler
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.schedule_models import SourceScheduleCreate
from spm_worker.dispatcher import dispatch_once
from tests.helpers import database_snapshot
from tests.test_provider_configuration import KEY
from tests.test_provider_persistence import START
from tests.test_provider_persistence import book as book
from tests.test_schedule_migration import DUE, policy
from tests.test_schedule_service import create, revise
from tests.test_source_dispatcher import Clock, response

pytestmark = pytest.mark.anyio


async def run_once(engine: Engine, book: dict[str, Any], **options: Any) -> Any:
    return await scheduler.schedule_once(engine, book["settings"], clock=Clock(DUE), **options)


async def test_startup_without_explicit_due_schedules_is_zero_write_and_zero_private_io(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        source_api, "selections", lambda *_: pytest.fail("Unsolicited profile read")
    )
    before = database_snapshot(engine)
    assert await run_once(engine, book) is None
    assert database_snapshot(engine) == before
    row = create(engine, book)
    before = database_snapshot(engine)
    assert await run_once(engine, book) is None
    assert database_snapshot(engine) == before
    revise(engine, row)
    before = database_snapshot(engine)
    assert await scheduler.schedule_once(engine, book["settings"], clock=Clock(START)) is None
    assert database_snapshot(engine) == before


async def test_due_profile_preparation_is_outside_writer_and_materializes_only_one_job(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = revise(engine, create(engine, book))
    original = source_api.selections
    calls = []

    def prepare(*args: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 0
        calls.append("PROFILE_PREPARED")
        return original(*args)

    monkeypatch.setattr(source_api, "selections", prepare)
    before = database_snapshot(engine)
    assert await run_once(engine, book) is None
    assert calls == ["PROFILE_PREPARED"]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 1
        assert (
            con.exec_driver_sql("SELECT count(*) FROM source_schedule_occurrences").scalar_one()
            == 1
        )
        plan = json.loads(con.exec_driver_sql("SELECT request_json FROM jobs").scalar_one())
        assert plan["providers"][0]["profile_sha256"] is not None
        assert plan["operations"] == ["DIVIDENDS"] and plan["force"] is False
        assert (
            con.exec_driver_sql(
                "SELECT last_sequence FROM source_schedules WHERE id=?", (row["id"],)
            ).scalar_one()
            == 1
        )
    after = database_snapshot(engine)
    changed = {
        "source_schedules",
        "source_schedule_occurrences",
        "jobs",
        "job_items",
        "audit_events",
    }
    assert {name: rows for name, rows in before.items() if name not in changed} == {
        name: rows for name, rows in after.items() if name not in changed
    }
    monkeypatch.setattr(source_api, "selections", lambda *_: pytest.fail("Completed slot reloaded"))
    assert await run_once(engine, book) is None
    assert database_snapshot(engine) == after


async def test_scheduled_job_reuses_real_adapter_fenced_quota_and_financial_isolation(
    book: dict[str, Any], engine: Engine
) -> None:
    revise(engine, create(engine, book))
    before = database_snapshot(engine)
    await run_once(engine, book)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        with unit_of_work(engine, write=True) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        return response()

    assert await dispatch_once(
        engine, book["settings"], uuid4(), clock=Clock(DUE), transport=httpx.MockTransport(handle)
    )
    assert calls == ["/api/div/0005.HK"]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status,attempt_count FROM jobs").one() == (
            "SUCCEEDED",
            1,
        )
        assert con.exec_driver_sql(
            "SELECT dps_e8,currency,adjustment_basis FROM dividend_observations"
        ).one() == (90000000, "USD", "UNKNOWN")
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_outcomes").scalar_one() == 1
    mutable = {
        "source_schedules",
        "source_schedule_occurrences",
        "jobs",
        "job_items",
        "audit_events",
        "provider_fetch_contexts",
        "provider_runs",
        "dividend_observations",
        "provider_observation_sightings",
        "job_checkpoints",
        "provider_budgets",
        "upstream_attempts",
        "upstream_outcomes",
    }
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name not in mutable} == {
        name: rows for name, rows in after.items() if name not in mutable
    }


async def test_in_flight_backpressure_skips_without_private_loading_or_new_spending(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    await run_once(engine, book)
    monkeypatch.setattr(
        source_api, "selections", lambda *_: pytest.fail("Blocked work reloaded profiles")
    )
    before = database_snapshot(engine)
    await scheduler.schedule_once(engine, book["settings"], clock=Clock(DUE + timedelta(days=1)))
    after = database_snapshot(engine)
    allowed = {"source_schedules", "source_schedule_occurrences", "audit_events"}
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT outcome FROM source_schedule_occurrences ORDER BY sequence"
        ).scalars().all() == ["QUEUED", "SKIPPED_IN_FLIGHT"]


async def test_stopped_scheduler_never_reads_files_or_starts_a_write(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    monkeypatch.setattr(source_api, "selections", lambda *_: pytest.fail("Stopped profile read"))
    before = database_snapshot(engine)
    assert await run_once(engine, book, stopping=lambda: True) is None
    assert database_snapshot(engine) == before


async def test_stop_during_private_preparation_cannot_enqueue_after_sigterm_intent(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    stopped = Event()
    original = source_api.selections

    def prepare(*args: Any) -> Any:
        result = original(*args)
        stopped.set()
        return result

    monkeypatch.setattr(source_api, "selections", prepare)
    before = database_snapshot(engine)
    await run_once(engine, book, stopping=stopped.is_set)
    assert database_snapshot(engine) == before


async def test_owner_pause_during_profile_loading_invalidates_the_frozen_tick(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = revise(engine, create(engine, book))
    original = source_api.selections

    def prepare(*args: Any) -> Any:
        result = original(*args)
        revise(engine, row, enabled=False, at=DUE)
        return result

    monkeypatch.setattr(source_api, "selections", prepare)
    await run_once(engine, book)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT enabled,version FROM source_schedules").one() == (0, 3)
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 0
        assert (
            con.exec_driver_sql("SELECT count(*) FROM source_schedule_occurrences").scalar_one()
            == 0
        )


async def test_new_slot_during_profile_preparation_requires_fresh_coalescing(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    clock = Clock(DUE)
    original = source_api.selections

    def prepare(*args: Any) -> Any:
        result = original(*args)
        clock.current += timedelta(days=1)
        return result

    monkeypatch.setattr(source_api, "selections", prepare)
    before = database_snapshot(engine)
    await scheduler.schedule_once(engine, book["settings"], clock=clock)
    assert database_snapshot(engine) == before
    monkeypatch.setattr(source_api, "selections", original)
    await scheduler.schedule_once(engine, book["settings"], clock=clock)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT due_at,sequence FROM source_schedule_occurrences"
        ).one() == (stamp(DUE + timedelta(days=1)), 1)


async def test_bounded_keyset_scans_do_not_starve_healthy_definitions_after_corrupt_ones(
    book: dict[str, Any], engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    for index in range(3):
        with unit_of_work(engine, write=True) as con:
            row = schedules.create(
                con,
                UUID(book["owner"]["id"]),
                SourceScheduleCreate(
                    name=f"Synthetic bounded schedule {index}", policy=policy(book)
                ),
                at=START,
            )
        revise(engine, row)
    with unit_of_work(engine, write=True) as con:
        first = con.exec_driver_sql(
            "SELECT id FROM source_schedules ORDER BY next_run_at,id LIMIT 1"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
        con.exec_driver_sql(
            "UPDATE source_schedules SET policy_sha256=? WHERE id=?", ("f" * 64, first)
        )
    cursor = await run_once(engine, book, limit=1)
    assert cursor is not None
    cursor = await run_once(engine, book, limit=1, after=cursor)
    cursor = await run_once(engine, book, limit=1, after=cursor)
    assert await run_once(engine, book, limit=1, after=cursor) is None
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 2
        assert (
            con.exec_driver_sql("SELECT count(*) FROM source_schedule_occurrences").scalar_one()
            == 2
        )
    assert "SOURCE_SCHEDULE_EVALUATION_FAILED" in caplog.text
    assert "ffffffff" not in caplog.text and KEY.decode() not in caplog.text


async def test_corrupt_retained_progress_cannot_be_advanced_by_the_automatic_worker(
    book: dict[str, Any], engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    revise(engine, create(engine, book))
    await run_once(engine, book)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
        con.exec_driver_sql("UPDATE source_schedules SET last_sequence=2")
    before = database_snapshot(engine)
    await scheduler.schedule_once(engine, book["settings"], clock=Clock(DUE + timedelta(days=1)))
    assert database_snapshot(engine) == before
    assert "SOURCE_SCHEDULE_EVALUATION_FAILED" in caplog.text


async def test_candidate_failure_does_not_abort_an_independent_schedule_or_echo_private_data(
    book: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for index in range(2):
        with unit_of_work(engine, write=True) as con:
            row = schedules.create(
                con,
                UUID(book["owner"]["id"]),
                SourceScheduleCreate(
                    name=f"Synthetic independent schedule {index}", policy=policy(book)
                ),
                at=START,
            )
        revise(engine, row)
    original = source_api.selections
    calls = 0

    def prepare(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private-schedule-failure:" + KEY.decode())
        return original(*args)

    monkeypatch.setattr(source_api, "selections", prepare)
    await run_once(engine, book)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 1
    assert "SOURCE_SCHEDULE_EVALUATION_FAILED" in caplog.text
    assert "private-schedule-failure" not in caplog.text and KEY.decode() not in caplog.text


@pytest.mark.parametrize("checkpoint", [2, 3, 5])
async def test_stop_at_candidate_preparation_and_writer_boundaries_is_zero_write(
    book: dict[str, Any], engine: Engine, checkpoint: int
) -> None:
    revise(engine, create(engine, book))
    calls = 0

    def halted() -> bool:
        nonlocal calls
        calls += 1
        return calls >= checkpoint

    before = database_snapshot(engine)
    await run_once(engine, book, stopping=halted)
    assert calls == checkpoint and database_snapshot(engine) == before


async def test_concurrent_evaluators_share_one_frozen_occurrence_and_do_not_duplicate_audits(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    original, barrier = source_api.selections, Barrier(2)

    def prepare(*args: Any) -> Any:
        result = original(*args)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(source_api, "selections", prepare)
    await asyncio.gather(run_once(engine, book), run_once(engine, book))
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 1
        assert (
            con.exec_driver_sql("SELECT count(*) FROM source_schedule_occurrences").scalar_one()
            == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='SOURCE_SCHEDULE_OCCURRED'"
            ).scalar_one()
            == 1
        )


async def test_late_occurrence_audit_failure_rolls_back_every_new_job_and_progress_field(
    book: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    revise(engine, create(engine, book))
    original = audit

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private-late-audit:" + KEY.decode())

    monkeypatch.setattr(schedules, "audit", broken)
    before = database_snapshot(engine)
    await run_once(engine, book)
    assert database_snapshot(engine) == before
    assert "SOURCE_SCHEDULE_EVALUATION_FAILED" in caplog.text
    assert "private-late-audit" not in caplog.text and KEY.decode() not in caplog.text
    monkeypatch.setattr(schedules, "audit", original)
    await run_once(engine, book)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT last_sequence FROM source_schedules").scalar_one() == 1


async def test_pause_after_candidate_scan_is_rechecked_before_preparation(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = revise(engine, create(engine, book))
    original = schedules.due_ids

    def scan(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        revise(engine, row, enabled=False, at=DUE)
        return result

    monkeypatch.setattr(schedules, "due_ids", scan)
    monkeypatch.setattr(source_api, "selections", lambda *_: pytest.fail("Paused profile read"))
    await run_once(engine, book)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 0


async def test_cancellation_of_the_initial_queue_never_repeats_its_backfill(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book, backfill_from_date="2025-12-01"))
    await run_once(engine, book)
    with unit_of_work(engine, write=True) as con:
        job = con.exec_driver_sql("SELECT id FROM jobs").scalar_one()
        jobs.request_cancel(con, UUID(row["owner_id"]), UUID(job), at=DUE)
    await scheduler.schedule_once(engine, book["settings"], clock=Clock(DUE + timedelta(days=1)))
    with unit_of_work(engine) as con:
        assert [
            tuple(row)
            for row in con.exec_driver_sql(
                "SELECT kind,window_start FROM source_schedule_occurrences ORDER BY sequence"
            )
        ] == [("INITIAL_BACKFILL", "2025-12-01"), ("REGULAR", "2025-12-13")]
        assert con.exec_driver_sql(
            "SELECT initial_job_id,last_sequence FROM source_schedules"
        ).one() == (job, 2)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


async def test_blocker_finishing_after_read_hint_retries_instead_of_inventing_a_skip(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = revise(engine, create(engine, book))
    await run_once(engine, book)
    original = schedules.blocking_job
    stopped: list[str] = []

    def blocker(con: Any, owner: UUID, identifier: UUID) -> Any:
        result = original(con, owner, identifier)
        if result is not None and not con.get_execution_options().get("writer") and not stopped:
            with unit_of_work(engine, write=True) as other:
                jobs.request_cancel(other, owner, UUID(result), at=DUE)
            stopped.append(result)
        return result

    monkeypatch.setattr(schedules, "blocking_job", blocker)
    tomorrow = Clock(DUE + timedelta(days=1))
    await scheduler.schedule_once(engine, book["settings"], clock=tomorrow)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT last_sequence FROM source_schedules WHERE id=?", (row["id"],)
            ).scalar_one()
            == 1
        )
    monkeypatch.setattr(schedules, "blocking_job", original)
    await scheduler.schedule_once(engine, book["settings"], clock=tomorrow)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT outcome FROM source_schedule_occurrences ORDER BY sequence"
        ).scalars().all() == ["QUEUED", "QUEUED"]


async def test_unconfigured_selection_stays_pinned_after_key_appearance_and_spends_nothing(
    book: dict[str, Any], engine: Engine
) -> None:
    revise(engine, create(engine, book))
    book["key"].unlink()
    await run_once(engine, book)
    with unit_of_work(engine) as con:
        plan = json.loads(con.exec_driver_sql("SELECT request_json FROM jobs").scalar_one())
        assert plan["providers"][0]["profile_sha256"] is None
    book["key"].touch(mode=0o600)
    book["key"].write_bytes(KEY)
    assert ProviderConfigurationStore(book["settings"]).load("eodhd").error_code is None
    assert await dispatch_once(
        engine,
        book["settings"],
        uuid4(),
        clock=Clock(DUE),
        transport=httpx.MockTransport(lambda _: pytest.fail("Null pinned selection made HTTP")),
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT status,attempt_count,last_error_code FROM job_items"
        ).one() == ("SKIPPED", 0, "UNCONFIGURED")
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0


@pytest.mark.parametrize(
    "after",
    [
        (),
        ("invalid", uuid4()),
        (stamp(DUE), "not-a-uuid"),
        (DUE.isoformat(), uuid4()),
        (stamp(DUE), uuid4(), "extra"),
        "invalid",
    ],
)
async def test_internal_keyset_rejects_invalid_cursors_before_binding(
    book: dict[str, Any], engine: Engine, after: Any
) -> None:
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(schedules.ScheduleError, match="^INVALID_SCHEDULE_CURSOR$"),
    ):
        schedules.due_ids(con, at=DUE, after=after)
    assert database_snapshot(engine) == before


async def test_real_leader_heartbeat_and_dispatch_continue_during_private_preparation(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    stop, release = Event(), Event()
    entered, dispatched = asyncio.Event(), asyncio.Event()
    gates: asyncio.Queue[None] = asyncio.Queue()
    beats: asyncio.Queue[None] = asyncio.Queue()
    wall = [100]
    loop = asyncio.get_running_loop()
    original_sleep, original_heartbeat = asyncio.sleep, worker_main.heartbeat
    original_schedule, original_prepare = scheduler.schedule_once, source_api.selections

    async def delay(seconds: float) -> None:
        if seconds == 5:
            await gates.get()
        else:
            await original_sleep(0)

    def prepare(*args: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10)
        return original_prepare(*args)

    def heartbeat(*args: Any) -> None:
        original_heartbeat(*args)
        beats.put_nowait(None)

    async def schedule(*args: Any, **kwargs: Any) -> Any:
        return await original_schedule(*args, **kwargs, clock=Clock(DUE))

    async def work(*args: Any, **kwargs: Any) -> bool:
        if entered.is_set():
            dispatched.set()
        result = await dispatch_once(
            *args, **kwargs, clock=Clock(DUE), transport=httpx.MockTransport(lambda _: response())
        )
        if result:
            stop.set()
            gates.put_nowait(None)
        return result

    monkeypatch.setattr("spm_worker.main.time.time", lambda: wall[0])
    monkeypatch.setattr(worker_main, "heartbeat", heartbeat)
    monkeypatch.setattr(asyncio, "sleep", delay)
    monkeypatch.setattr(source_api, "selections", prepare)
    monkeypatch.setattr(scheduler, "schedule_once", schedule)
    monkeypatch.setattr(dispatcher, "dispatch_once", work)
    task = asyncio.create_task(worker_main.run_leader(book["settings"], engine, uuid4(), stop))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        await beats.get()
        await asyncio.wait_for(dispatched.wait(), timeout=10)
        wall[0] = 131
        assert not worker_main.healthy(book["settings"].data_dir)
        gates.put_nowait(None)
        await beats.get()
        assert worker_main.healthy(book["settings"].data_dir)
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=10)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status FROM jobs").scalar_one() == "SUCCEEDED"


async def test_real_leader_materializes_an_independent_schedule_during_provider_io(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise(engine, create(engine, book))
    await run_once(engine, book)
    with unit_of_work(engine, write=True) as con:
        second = schedules.create(
            con,
            UUID(book["owner"]["id"]),
            SourceScheduleCreate(name="Synthetic independent during I/O", policy=policy(book)),
            at=START,
        )
    revise(engine, second)
    stop, release = Event(), asyncio.Event()
    entered, queued = asyncio.Event(), asyncio.Event()
    gates: asyncio.Queue[None] = asyncio.Queue()
    original_sleep, original_schedule = asyncio.sleep, scheduler.schedule_once

    async def delay(seconds: float) -> None:
        if seconds == 5:
            await gates.get()
        else:
            await original_sleep(0)

    async def handle(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return response()

    async def schedule(*args: Any, **kwargs: Any) -> Any:
        result = await original_schedule(*args, **kwargs, clock=Clock(DUE))
        with unit_of_work(engine) as con:
            if con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 2:
                queued.set()
        return result

    async def work(*args: Any, **kwargs: Any) -> bool:
        result = await dispatch_once(
            *args, **kwargs, clock=Clock(DUE), transport=httpx.MockTransport(handle)
        )
        if result:
            stop.set()
            gates.put_nowait(None)
        return result

    monkeypatch.setattr(asyncio, "sleep", delay)
    monkeypatch.setattr(scheduler, "schedule_once", schedule)
    monkeypatch.setattr(dispatcher, "dispatch_once", work)
    task = asyncio.create_task(worker_main.run_leader(book["settings"], engine, uuid4(), stop))
    await asyncio.wait_for(entered.wait(), timeout=10)
    await asyncio.wait_for(queued.wait(), timeout=10)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM source_schedule_occurrences").scalar_one()
            == 2
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
    release.set()
    await asyncio.wait_for(task, timeout=10)


async def test_supervisor_scan_failure_is_private_and_does_not_kill_the_leader(
    book: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop, wake = Event(), asyncio.Event()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        wake.set()
        raise RuntimeError("private-scan-failure:" + KEY.decode())

    async def idle(*args: Any, **kwargs: Any) -> bool:
        await wake.wait()
        return False

    async def delay(seconds: float) -> None:
        await wake.wait()
        stop.set()

    monkeypatch.setattr(scheduler, "schedule_once", broken)
    monkeypatch.setattr(dispatcher, "dispatch_once", idle)
    monkeypatch.setattr(asyncio, "sleep", delay)
    await worker_main.run_leader(book["settings"], engine, uuid4(), stop)
    assert "SOURCE_SCHEDULE_SCAN_FAILED" in caplog.text
    assert "private-scan-failure" not in caplog.text and KEY.decode() not in caplog.text
