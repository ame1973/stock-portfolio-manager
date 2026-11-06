"""The source schedule controller never reads private files, spends quota or changes finance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.job_service as jobs
import spm_api.schedule_service as schedules
from spm_api.audit import canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.job_models import ProviderSelection
from spm_api.schedule_models import DueSchedule, SourceScheduleCreate, SourceScheduleUpdate
from tests.helpers import database_snapshot
from tests.test_provider_persistence import START
from tests.test_provider_persistence import book as book
from tests.test_schedule_migration import DUE, policy

pytestmark = pytest.mark.anyio
SELECTED = (ProviderSelection(provider_id="eodhd", profile_sha256="a" * 64),)


def definition(book: dict[str, Any], **changes: Any) -> SourceScheduleCreate:
    return SourceScheduleCreate(name="Synthetic owned schedule", policy=policy(book, **changes))


def create(engine: Engine, book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    with unit_of_work(engine, write=True) as con:
        return schedules.create(
            con,
            UUID(book["owner"]["id"]),
            definition(book, **changes),
            at=START,
            request_id="schedule-create-test",
        )


def revise(
    engine: Engine,
    row: dict[str, Any],
    *,
    enabled: bool = True,
    at: datetime | None = None,
    **changes: Any,
) -> dict[str, Any]:
    body = SourceScheduleUpdate.model_validate(
        {
            "name": row["name"],
            "policy": schedules.policy_from_row(row),
            "enabled": enabled,
            "reason": "Explicitly changed the synthetic source scheduling policy.",
            **changes,
        }
    )
    with unit_of_work(engine, write=True) as con:
        return schedules.revise(
            con,
            UUID(row["owner_id"]),
            UUID(row["id"]),
            body,
            version=row["version"],
            at=at if at is not None else datetime.fromisoformat(row["updated_at"]),
            request_id="schedule-update-test",
        )


def due(engine: Engine, row: dict[str, Any], *, at: Any = DUE) -> Any:
    with unit_of_work(engine) as con:
        return schedules.due_schedule(con, UUID(row["owner_id"]), UUID(row["id"]), at=at)


def materialize(engine: Engine, tick: Any, *, at: Any = DUE, selected: Any = SELECTED) -> Any:
    with unit_of_work(engine, write=True) as con:
        return schedules.materialize(con, tick, selected, at=at, request_id="schedule-tick-test")


async def test_create_is_disabled_canonical_owned_and_audited_without_other_writes(
    book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    row = create(engine, book)
    assert row["enabled"] == 0 and row["version"] == 1 and row["next_run_at"] is None
    encoded = canonical(definition(book).policy.model_dump(mode="json"))
    assert row["policy_json"] == encoded and row["policy_sha256"] == digest(encoded)
    after = database_snapshot(engine)
    mutable = {"source_schedules", "audit_events"}
    assert {key: rows for key, rows in before.items() if key not in mutable} == {
        key: rows for key, rows in after.items() if key not in mutable
    }
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT owner_id,request_id FROM audit_events WHERE action='SOURCE_SCHEDULE_CREATED'"
        ).one() == (row["owner_id"], "schedule-create-test")


async def test_enable_coalesces_one_latest_due_slot_and_preserves_all_financial_facts(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    assert row["next_run_at"] == stamp(DUE) and row["version"] == 2
    at = DUE + timedelta(days=4000, seconds=5)
    before = database_snapshot(engine)
    tick = due(engine, row, at=at)
    assert tick is not None and tick.expected_run_at == DUE
    assert tick.due_at == at.replace(second=0) and not tick.initial
    result = materialize(engine, tick, at=at)
    assert result["outcome"] == "QUEUED" and result["sequence"] == 1
    with unit_of_work(engine) as con:
        job = jobs.owned_job(con, UUID(row["owner_id"]), UUID(result["job_id"]))
        assert job["attempt_count"] == 0 and job["runtime_version"] == 1
        assert '"force":false' in job["request_json"]
        assert '"operations":["DIVIDENDS"]' in job["request_json"]
        assert (
            con.exec_driver_sql("SELECT count(*) FROM source_schedule_occurrences").scalar_one()
            == 1
        )
        assert schedules.due_schedule(con, UUID(row["owner_id"]), UUID(row["id"]), at=at) is None
    after = database_snapshot(engine)
    mutable = {
        "source_schedules",
        "source_schedule_occurrences",
        "jobs",
        "job_items",
        "audit_events",
    }
    assert {key: rows for key, rows in before.items() if key not in mutable} == {
        key: rows for key, rows in after.items() if key not in mutable
    }


async def test_replay_returns_original_receipt_after_pause_rotation_and_cancellation(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    first = materialize(engine, tick)
    with unit_of_work(engine, write=True) as con:
        current = schedules.owned_schedule(con, UUID(row["owner_id"]), UUID(row["id"]))
        jobs.request_cancel(con, UUID(row["owner_id"]), UUID(first["job_id"]), at=DUE)
    revise(engine, current, enabled=False)
    before = database_snapshot(engine)
    rotated = (ProviderSelection(provider_id="eodhd", profile_sha256="b" * 64),)
    assert materialize(engine, tick, at=DUE + timedelta(days=20), selected=rotated) == first
    assert materialize(engine, tick, selected=()) == first
    assert database_snapshot(engine) == before


async def test_actual_concurrent_writers_materialize_one_job_and_receipt(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: materialize(engine, tick), range(2)))
    assert results[0] == results[1]
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


async def test_backpressure_skip_advances_slot_but_never_creates_another_job_or_round(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book, backfill_from_date="2000-01-01"))
    first = materialize(engine, due(engine, row))
    at = DUE + timedelta(days=1)
    tick = due(engine, row, at=at)
    assert tick is not None and not tick.initial
    before = database_snapshot(engine)
    skipped = materialize(engine, tick, at=at, selected=())
    assert skipped["outcome"] == "SKIPPED_IN_FLIGHT"
    assert skipped["job_id"] is None and skipped["blocking_job_id"] == first["job_id"]
    after = database_snapshot(engine)
    mutable = {"source_schedules", "source_schedule_occurrences", "audit_events"}
    assert {key: rows for key, rows in before.items() if key not in mutable} == {
        key: rows for key, rows in after.items() if key not in mutable
    }


async def test_stale_paused_tick_cannot_enqueue_anything(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    revise(engine, row, enabled=False)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_TICK_STALE"):
        materialize(engine, tick)
    assert database_snapshot(engine) == before


async def test_revision_requires_the_observed_version(book: dict[str, Any], engine: Engine) -> None:
    row = create(engine, book)
    revise(engine, row)
    before = database_snapshot(engine)
    with pytest.raises(ApiError) as caught:
        revise(engine, row, enabled=False)
    assert caught.value.status == 412
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["create", "revise", "materialize"])
async def test_controller_writes_require_writer_uow(
    book: dict[str, Any], engine: Engine, operation: str
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    before = database_snapshot(engine)
    with (
        pytest.raises(schedules.ScheduleError, match="WRITE_UNIT_OF_WORK_REQUIRED"),
        unit_of_work(engine) as con,
    ):
        if operation == "create":
            schedules.create(con, UUID(row["owner_id"]), definition(book), at=START)
        elif operation == "revise":
            schedules.revise(
                con,
                UUID(row["owner_id"]),
                UUID(row["id"]),
                SourceScheduleUpdate(
                    **definition(book).model_dump(),
                    enabled=False,
                    reason="Explicit synthetic pause reason.",
                ),
                version=2,
                at=START,
            )
        else:
            schedules.materialize(con, tick, SELECTED, at=DUE)
    assert database_snapshot(engine) == before


async def test_late_audit_failure_rolls_back_the_entire_materialization(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("""
            CREATE TRIGGER synthetic_controller_audit_failure BEFORE INSERT ON audit_events
            WHEN NEW.action='SOURCE_SCHEDULE_OCCURRED'
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_AUDIT_FAILURE'); END
        """)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        materialize(engine, tick)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", [{"market": "US"}, {"security_ids": [uuid4()]}])
async def test_creation_checks_real_catalogue_market_not_quote_currency(
    book: dict[str, Any], engine: Engine, change: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_SECURITY_SCOPE"):
        create(engine, book, **change)
    assert database_snapshot(engine) == before


async def test_foreign_owner_get_and_materialization_cannot_adopt_a_schedule(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    foreign = tick.model_copy(update={"owner_id": uuid4()})
    before = database_snapshot(engine)
    with (
        pytest.raises(schedules.ScheduleError, match="SCHEDULE_NOT_FOUND"),
        unit_of_work(engine) as con,
    ):
        schedules.owned_schedule(con, foreign.owner_id, tick.schedule_id)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_NOT_FOUND"):
        materialize(engine, foreign)
    assert database_snapshot(engine) == before


async def test_corrupt_policy_hash_fails_closed_without_echo_or_repair(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
        con.exec_driver_sql(
            "UPDATE source_schedules SET policy_sha256=? WHERE id=?", ("f" * 64, row["id"])
        )
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="^SCHEDULE_POLICY_UNAVAILABLE$"):
        due(engine, row)
    assert database_snapshot(engine) == before


async def test_unaligned_stored_next_slot_is_not_silently_accepted(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE source_schedules SET next_run_at=?,version=version+1 WHERE id=?",
            (stamp(DUE + timedelta(seconds=1)), row["id"]),
        )
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="^SCHEDULE_SLOT_UNAVAILABLE$"):
        due(engine, row, at=DUE + timedelta(minutes=1))
    assert database_snapshot(engine) == before


async def test_edit_at_the_exact_materialized_instant_never_reuses_the_same_slot(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    materialize(engine, due(engine, row))
    with unit_of_work(engine) as con:
        current = schedules.owned_schedule(con, UUID(row["owner_id"]), UUID(row["id"]))
    changed = revise(engine, current, name="Explicit future schedule name")
    assert changed["next_run_at"] == stamp(DUE + timedelta(days=1))
    assert changed["last_sequence"] == 1


@pytest.mark.parametrize("limit", [True, 0, -1, 101, 1.0, "25"])
async def test_due_scan_is_bounded_and_strictly_typed(
    book: dict[str, Any], engine: Engine, limit: Any
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(schedules.ScheduleError, match="INVALID_SCHEDULE_LIMIT"),
        unit_of_work(engine) as con,
    ):
        schedules.due_ids(con, at=DUE, limit=limit)
    assert database_snapshot(engine) == before


async def test_due_candidates_are_ordered_bounded_active_and_read_only(
    book: dict[str, Any], engine: Engine
) -> None:
    first = revise(engine, create(engine, book))
    with unit_of_work(engine, write=True) as con:
        second = schedules.create(
            con,
            UUID(first["owner_id"]),
            SourceScheduleCreate(name="Second candidate", policy=policy(book)),
            at=START,
        )
    second = revise(engine, second)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert schedules.due_ids(con, at=START) == ()
        expected = sorted([first["id"], second["id"]])
        assert schedules.due_ids(con, at=DUE, limit=1) == (
            (UUID(first["owner_id"]), UUID(expected[0])),
        )
        assert [str(identifier) for _, identifier in schedules.due_ids(con, at=DUE)] == expected
        assert (
            schedules.due_schedule(con, UUID(first["owner_id"]), UUID(first["id"]), at=START)
            is None
        )
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (first["owner_id"],))
    with unit_of_work(engine) as con:
        assert schedules.due_ids(con, at=DUE) == ()


@pytest.mark.parametrize("operation", ["create", "revise", "due", "materialize", "scan"])
async def test_controller_clock_must_be_aware_before_any_write(
    book: dict[str, Any], engine: Engine, operation: str
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    before = database_snapshot(engine)
    at = DUE.replace(tzinfo=None)
    with pytest.raises(schedules.ScheduleError, match="AWARE_SCHEDULE_TIME_REQUIRED"):
        if operation == "create":
            with unit_of_work(engine, write=True) as con:
                schedules.create(con, UUID(row["owner_id"]), definition(book), at=at)
        elif operation == "revise":
            revise(engine, row, at=at)
        elif operation == "due":
            due(engine, row, at=at)
        elif operation == "scan":
            with unit_of_work(engine) as con:
                schedules.due_ids(con, at=at)
        else:
            materialize(engine, tick, at=at)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["create", "revise", "materialize", "due"])
async def test_inactive_owner_is_fenced_before_controller_changes(
    book: dict[str, Any], engine: Engine, operation: str
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (row["owner_id"],))
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_OWNER_UNAVAILABLE"):
        if operation == "create":
            create(engine, book)
        elif operation == "revise":
            revise(engine, row, enabled=False)
        elif operation == "due":
            due(engine, row)
        else:
            materialize(engine, tick)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "selected",
    [
        (),
        (ProviderSelection(provider_id="yahoo", profile_sha256=None),),
        (SELECTED[0], SELECTED[0]),
    ],
)
async def test_new_tick_requires_exact_server_profile_selection_scope(
    book: dict[str, Any], engine: Engine, selected: tuple[ProviderSelection, ...]
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="INVALID_SCHEDULE_SELECTION"):
        materialize(engine, tick, selected=selected)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"initial": True},
        {"expected_run_at": DUE + timedelta(seconds=1)},
        {"due_at": DUE - timedelta(days=1)},
        {"version": 1},
    ],
)
async def test_forged_or_outdated_fences_never_create_partial_work(
    book: dict[str, Any], engine: Engine, change: dict[str, Any]
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row).model_copy(update=change)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_TICK_STALE"):
        materialize(engine, tick)
    assert database_snapshot(engine) == before


async def test_a_tick_crossing_another_due_slot_must_be_reevaluated(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_TICK_STALE"):
        materialize(engine, tick, at=DUE + timedelta(days=1))
    assert database_snapshot(engine) == before
    newest = due(engine, row, at=DUE + timedelta(days=1))
    result = materialize(engine, newest, at=DUE + timedelta(days=1))
    assert result["due_at"] == stamp(DUE + timedelta(days=1)) and result["sequence"] == 1


async def test_conflicting_replay_cannot_relabel_the_original_frozen_receipt(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    original = materialize(engine, tick)
    before = database_snapshot(engine)
    altered = tick.model_copy(update={"due_at": DUE + timedelta(days=1)})
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_TICK_CONFLICT"):
        materialize(engine, altered)
    assert materialize(engine, tick, at=START) == original
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["create", "revise"])
async def test_definition_audit_failure_rolls_back_the_metadata_mutation(
    book: dict[str, Any], engine: Engine, operation: str
) -> None:
    row = create(engine, book)
    action = "SOURCE_SCHEDULE_CREATED" if operation == "create" else "SOURCE_SCHEDULE_CHANGED"
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(f"""
            CREATE TRIGGER synthetic_definition_audit_failure BEFORE INSERT ON audit_events
            WHEN NEW.action='{action}' BEGIN SELECT RAISE(ABORT,'SYNTHETIC_AUDIT_FAILURE'); END
        """)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        if operation == "create":
            with unit_of_work(engine, write=True) as con:
                schedules.create(
                    con,
                    UUID(row["owner_id"]),
                    SourceScheduleCreate(name="Failed definition", policy=policy(book)),
                    at=START,
                )
        else:
            revise(engine, row)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "encoded", [None, "{", "[]", '{"api_key":"synthetic-never-an-actual-key"}']
)
async def test_untrusted_policy_reads_fail_with_fixed_codes(
    book: dict[str, Any], engine: Engine, encoded: Any
) -> None:
    row = create(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="^SCHEDULE_POLICY_UNAVAILABLE$"):
        schedules.policy_from_row({**row, "policy_json": encoded})
    assert database_snapshot(engine) == before


async def test_equivalent_noncanonical_json_is_never_silently_rehashed(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create(engine, book)
    encoded = definition(book).policy.model_dump_json(indent=2)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="^SCHEDULE_POLICY_UNAVAILABLE$"):
        schedules.policy_from_row({**row, "policy_json": encoded, "policy_sha256": digest(encoded)})
    assert database_snapshot(engine) == before


async def test_initial_backfill_is_not_reissued_after_a_cancelled_first_job(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book, backfill_from_date="2000-01-01"))
    first = materialize(engine, due(engine, row))
    with unit_of_work(engine, write=True) as con:
        jobs.request_cancel(con, UUID(row["owner_id"]), UUID(first["job_id"]), at=DUE)
    tick = due(engine, row, at=DUE + timedelta(days=1))
    result = materialize(engine, tick, at=DUE + timedelta(days=1))
    assert result["kind"] == "REGULAR" and result["window_start"] == "2025-12-13"
    with unit_of_work(engine) as con:
        current = schedules.owned_schedule(con, UUID(row["owner_id"]), UUID(row["id"]))
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="INITIAL_BACKFILL_ALREADY_QUEUED"):
        revise(engine, current, policy=policy(book, backfill_from_date="1999-01-01"))
    assert database_snapshot(engine) == before


async def test_clock_regression_and_exhausted_enable_do_not_mutate_metadata(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_CLOCK_REGRESSION"):
        revise(engine, row, at=START - timedelta(seconds=1))
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_TIME_EXHAUSTED"):
        revise(engine, row, at=datetime.max.replace(tzinfo=UTC))
    assert database_snapshot(engine) == before


async def test_final_calendar_occurrence_retains_receipt_without_a_future_pointer(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    at = datetime.max.replace(tzinfo=UTC)
    tick = due(engine, row, at=at)
    result = materialize(engine, tick, at=at)
    assert result["next_run_at"] is None and result["window_end"] == "9999-12-31"
    assert due(engine, row, at=at) is None
    before = database_snapshot(engine)
    assert materialize(engine, tick, at=at, selected=()) == result
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["expected_run_at", "due_at"])
async def test_internal_due_snapshot_rejects_numeric_epoch_coercion(
    book: dict[str, Any], engine: Engine, field: str
) -> None:
    row = revise(engine, create(engine, book))
    value = due(engine, row).model_dump()
    value[field] = DUE.timestamp()
    with pytest.raises(ValidationError):
        DueSchedule.model_validate(value)


@pytest.mark.parametrize("version", [True, 0, 2147483648, 1.0, "2"])
async def test_owner_metadata_version_cannot_be_coerced(
    book: dict[str, Any], engine: Engine, version: Any
) -> None:
    row = create(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="INVALID_SCHEDULE_VERSION"):
        revise(engine, {**row, "version": version})
    assert database_snapshot(engine) == before


async def test_new_materialization_rejects_a_regressed_control_clock(
    book: dict[str, Any], engine: Engine
) -> None:
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="SCHEDULE_CLOCK_REGRESSION"):
        materialize(engine, tick, at=START - timedelta(seconds=1))
    assert database_snapshot(engine) == before


async def test_controller_does_not_read_private_profiles_or_make_provider_requests(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("Private provider I/O is forbidden in schedule writer callbacks")

    monkeypatch.setattr(
        "spm_api.providers.configuration.ProviderConfigurationStore.load", forbidden
    )
    monkeypatch.setattr("httpx.AsyncClient.send", forbidden)
    row = revise(engine, create(engine, book))
    tick = due(engine, row)
    first = materialize(
        engine, tick, selected=(ProviderSelection(provider_id="eodhd", profile_sha256=None),)
    )
    assert materialize(engine, tick, selected=()) == first


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True},
        {"owner_id": str(uuid4())},
        {"force": False},
        {"api_key": "synthetic-not-an-actual-key"},
        {"timezone": "UTC"},
    ],
)
def test_creation_body_cannot_enable_or_smuggle_server_controls(changes: dict[str, Any]) -> None:
    valid = definition({"security": {"id": str(uuid4())}}).model_dump(mode="json")
    with pytest.raises(ValidationError):
        SourceScheduleCreate.model_validate({**valid, **changes})


@pytest.mark.parametrize("changes", [{"enabled": 1}, {"enabled": "false"}, {"reason": "short"}])
def test_revision_requires_strict_enablement_and_a_reason(changes: dict[str, Any]) -> None:
    valid = definition({"security": {"id": str(uuid4())}}).model_dump(mode="json")
    with pytest.raises(ValidationError):
        SourceScheduleUpdate.model_validate(
            {
                **valid,
                "enabled": False,
                "reason": "Explicit synthetic policy change.",
                **changes,
            }
        )


async def test_unrepresentable_recurring_window_fails_without_partial_work(
    book: dict[str, Any], engine: Engine
) -> None:
    at = datetime(1, 1, 1, tzinfo=UTC)
    with unit_of_work(engine, write=True) as con:
        row = schedules.create(
            con,
            UUID(book["owner"]["id"]),
            SourceScheduleCreate(
                name="Finite calendar lower bound", policy=policy(book, first_date="0001-01-01")
            ),
            at=at,
        )
    row = revise(engine, row, at=at)
    slot = datetime.fromisoformat(row["next_run_at"])
    tick = due(engine, row, at=slot)
    before = database_snapshot(engine)
    with pytest.raises(schedules.ScheduleError, match="^SCHEDULE_WINDOW_OUT_OF_RANGE$"):
        materialize(engine, tick, at=slot)
    assert database_snapshot(engine) == before
