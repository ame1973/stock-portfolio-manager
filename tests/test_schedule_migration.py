"""Source schedules retain old facts and enforce owned, append-only atomic occurrences."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.job_service as jobs
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from spm_api.job_models import ProviderSelection, SourceJobPlan
from spm_api.schedule_models import SourceSchedulePolicy
from spm_api.schedule_time import next_due, occurrence_window
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import NOW, insert, seed_evidence
from tests.test_action_opening_migration import schema
from tests.test_provider_persistence import START
from tests.test_provider_persistence import book as book
from tests.test_transfer_migration import dimensions, seed_buy

pytestmark = pytest.mark.anyio
DUE = START.replace(hour=9, minute=15)
MATERIALIZED = DUE + timedelta(minutes=1)


def upgrade_to(engine: Engine, target: str) -> None:
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        command.upgrade(config, target)


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0008_provider_evidence")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
            seed_evidence(con, "owner", "stock")
            insert(
                con,
                "settings",
                {
                    "owner_id": "owner",
                    "key": "source_schedules",
                    "value_json": '{"enabled":true,"api_key":"synthetic-retained-not-a-key"}',
                    "updated_at": NOW,
                },
            )
            insert(
                con,
                "jobs",
                {
                    "id": "legacy-source-job",
                    "owner_id": "owner",
                    "kind": "DIVIDEND_SYNC",
                    "dedupe_key": "legacy-source-schedule",
                    "status": "QUEUED",
                    "request_json": '{"schedule":"must not be adopted"}',
                    "created_at": NOW,
                },
            )
        upgrade_to(engine, "0010_upstream_budgets")
        yield engine
    finally:
        engine.dispose()


def test_upgrade_preserves_every_old_value_and_never_adopts_settings_or_jobs(
    old_engine: Engine, settings: Settings
) -> None:
    before = database_snapshot(old_engine)
    migrate(settings)
    after = database_snapshot(old_engine)
    assert {
        key: rows for key, rows in before.items() if key not in {"audit_events", "alembic_version"}
    } == {key: after[key] for key in before if key not in {"audit_events", "alembic_version"}}
    assert after["source_schedules"] == after["source_schedule_occurrences"] == []
    with unit_of_work(old_engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_SOURCE_SCHEDULES'"
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    migrate(settings)
    assert database_snapshot(old_engine) == after


def test_late_ddl_failure_restores_full_schema_facts_and_head(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        con.exec_driver_sql("CREATE INDEX idx_source_schedule_due ON provider_status(provider_id)")
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises(OperationalError):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0010_upstream_budgets"
        )


def policy(book: dict[str, Any], **changes: Any) -> SourceSchedulePolicy:
    return SourceSchedulePolicy.model_validate(
        {
            "security_ids": [book["security"]["id"]],
            "provider_ids": ["eodhd"],
            "market": "HK",
            "first_date": "2026-01-01",
            "local_run_time": "17:15",
            "cadence": "DAILY",
            "lookback_days": 30,
            **changes,
        }
    )


def values(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    encoded = canonical(policy(book).model_dump(mode="json"))
    return {
        "id": str(uuid4()),
        "owner_id": book["owner"]["id"],
        "name": "Synthetic source schedule",
        "policy_json": encoded,
        "policy_sha256": digest(encoded),
        "enabled": 0,
        "version": 1,
        "next_run_at": None,
        "last_sequence": 0,
        "last_due_at": None,
        "last_materialized_at": None,
        "last_job_id": None,
        "initial_job_id": None,
        "created_at": stamp(START),
        "updated_at": stamp(START),
        **changes,
    }


def create_schedule(engine: Engine, book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    result = values(book, **changes)
    with unit_of_work(engine, write=True) as con:
        insert(con, "source_schedules", result)
    return result


def enable(engine: Engine, identifier: str) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE source_schedules SET enabled=1,next_run_at=?,version=version+1 WHERE id=?",
            (stamp(DUE), identifier),
        )


async def test_new_schedule_is_disabled_and_does_not_queue_spend_or_touch_finance(
    book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    row = create_schedule(engine, book)
    after = database_snapshot(engine)
    assert {key: value for key, value in after.items() if key != "source_schedules"} == {
        key: value for key, value in before.items() if key != "source_schedules"
    }
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT enabled,next_run_at,last_sequence FROM source_schedules WHERE id=?",
            (row["id"],),
        ).one() == (0, None, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"owner_id": str(uuid4())},
        {"name": " "},
        {"name": "x" * 101},
        {"enabled": 1},
        {"enabled": 2},
        {"version": 2},
        {"version": 1.5},
        {"policy_sha256": "g" * 64},
        {"next_run_at": stamp(DUE)},
        {"last_sequence": 1},
        {"last_job_id": str(uuid4())},
        {"initial_job_id": str(uuid4())},
        {"created_at": "2026-02-30T00:00:00.000000Z"},
        {"updated_at": "0000-01-01T00:00:00.000000Z"},
    ],
)
async def test_initial_controls_reject_forged_state_without_any_writes(
    book: dict[str, Any], engine: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "source_schedules", values(book, **changes))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"scope": "FINANCIAL_SYNC"},
        {"calendar_basis": "CONFIRMED_TRADING_DAYS"},
        {"market": "US"},
        {"security_ids": []},
        {"security_ids": [str(uuid4())]},
        {"provider_ids": ["manual"]},
        {"provider_ids": ["eodhd", "eodhd"]},
        {"operations": ["DIVIDENDS", "DIVIDENDS"]},
        {"operations": [None]},
        {"operations": ["RECEIPTS"]},
        {"cadence": "CRON"},
        {"weekday": 0},
        {"cadence": "WEEKLY", "weekday": None},
        {"lookback_days": True},
        {"lookback_days": 1.5},
        {"lookback_days": 3661},
        {"lookback_months": 24},
        {"first_date": "2026-02-30"},
        {"backfill_from_date": "2027-01-01"},
        {"local_run_time": "17:60"},
        {"api_key": "synthetic-never-a-real-key"},
        {"timezone": "UTC"},
    ],
)
async def test_sql_policy_whitelist_types_and_catalogue_market_cannot_be_bypassed(
    book: dict[str, Any], engine: Engine, changes: dict[str, Any]
) -> None:
    encoded = canonical({**policy(book).model_dump(mode="json"), **changes})
    before = database_snapshot(engine)
    with pytest.raises((IntegrityError, OperationalError)), unit_of_work(engine, write=True) as con:
        insert(
            con,
            "source_schedules",
            values(book, policy_json=encoded, policy_sha256=digest(encoded)),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "assignment",
    [
        "owner_id='foreign'",
        "id='replacement'",
        "version=version+2",
        "last_sequence=1",
        "last_due_at='2026-01-10T09:15:00.000000Z'",
        "enabled=1",
        "next_run_at='bad'",
        "created_at='2026-01-01T00:00:00.000000Z'",
        "updated_at='2025-12-31T00:00:00.000000Z'",
        f"policy_sha256='{'f' * 64}'",
    ],
)
async def test_metadata_updates_are_cas_and_cannot_forge_occurrence_progress(
    book: dict[str, Any], engine: Engine, assignment: str
) -> None:
    row = create_schedule(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"UPDATE source_schedules SET version=version+1,{assignment} WHERE id=?", (row["id"],)
        )
    assert database_snapshot(engine) == before


async def test_schedule_identity_cannot_be_deleted(book: dict[str, Any], engine: Engine) -> None:
    row = create_schedule(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DELETE FROM source_schedules WHERE id=?", (row["id"],))
    assert database_snapshot(engine) == before


def enqueue_occurrence(
    engine: Engine,
    book: dict[str, Any],
    schedule: dict[str, Any],
    *,
    due: datetime = DUE,
    blocking_job: str | None = None,
    late_audit: bool = False,
    changes: dict[str, Any] | None = None,
    plan_changes: dict[str, Any] | None = None,
) -> tuple[str, str]:
    selected = SourceSchedulePolicy.model_validate_json(schedule["policy_json"])
    materialized = due + timedelta(minutes=1)
    following = next_due(selected, materialized)
    occurrence, job_id = str(uuid4()), blocking_job
    with unit_of_work(engine, write=True) as con:
        current = (
            con.exec_driver_sql("SELECT * FROM source_schedules WHERE id=?", (schedule["id"],))
            .mappings()
            .one()
        )
        initial = selected.backfill_from_date is not None and current["initial_job_id"] is None
        window = occurrence_window(selected, due, initial=initial)
        if job_id is None:
            plan = SourceJobPlan.model_validate(
                {
                    "security_ids": selected.security_ids,
                    "providers": [
                        ProviderSelection(provider_id=p, profile_sha256=None)
                        for p in selected.provider_ids
                    ],
                    "window": window,
                    "operations": selected.operations,
                    "force": False,
                    **(plan_changes or {}),
                }
            )
            job_id = jobs.enqueue(
                con, UUID(book["owner"]["id"]), plan, intent_id=uuid4(), at=materialized
            )["id"]
        insert(
            con,
            "source_schedule_occurrences",
            {
                "id": occurrence,
                "schedule_id": schedule["id"],
                "owner_id": schedule["owner_id"],
                "sequence": current["last_sequence"] + 1,
                "schedule_version": current["version"],
                "expected_run_at": current["next_run_at"],
                "due_at": stamp(due),
                "materialized_at": stamp(materialized),
                "next_run_at": stamp(following) if following else None,
                "policy_json": schedule["policy_json"],
                "policy_sha256": schedule["policy_sha256"],
                "window_start": window.start,
                "window_end": window.end,
                "kind": "INITIAL_BACKFILL" if initial else "REGULAR",
                "outcome": "SKIPPED_IN_FLIGHT" if blocking_job else "QUEUED",
                "job_id": None if blocking_job else job_id,
                "blocking_job_id": blocking_job,
                **(changes or {}),
            },
        )
        if late_audit:
            audit(
                con, schedule["owner_id"], "source_schedule", schedule["id"], "SCHEDULE_TICK_TEST"
            )
    return occurrence, job_id


async def test_immutable_occurrence_atomically_advances_the_owned_schedule(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    occurrence, job = enqueue_occurrence(engine, book, row)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT version,last_sequence,last_due_at,last_materialized_at,"
            "last_job_id,initial_job_id FROM source_schedules WHERE id=?",
            (row["id"],),
        ).one() == (3, 1, stamp(DUE), stamp(MATERIALIZED), job, None)
        assert con.exec_driver_sql(
            "SELECT outcome,job_id FROM source_schedule_occurrences WHERE id=?", (occurrence,)
        ).one() == ("QUEUED", job)
    for operation in (
        "UPDATE source_schedule_occurrences SET outcome='SKIPPED_IN_FLIGHT'",
        "DELETE FROM source_schedule_occurrences",
    ):
        before = database_snapshot(engine)
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(f"{operation} WHERE id=?", (occurrence,))
        assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": str(uuid4())},
        {"sequence": 2},
        {"schedule_version": 1},
        {"expected_run_at": stamp(START)},
        {"due_at": stamp(START)},
        {"materialized_at": stamp(START)},
        {"next_run_at": stamp(MATERIALIZED)},
        {"policy_sha256": "f" * 64},
        {"window_start": "2020-01-01"},
        {"window_end": "2026-01-09"},
        {"kind": "INITIAL_BACKFILL"},
        {"outcome": "SKIPPED_IN_FLIGHT"},
        {"blocking_job_id": str(uuid4())},
    ],
)
async def test_forged_occurrences_roll_back_the_job_and_all_parent_progress(
    book: dict[str, Any], engine: Engine, changes: dict[str, Any]
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, row, changes=changes)
    assert database_snapshot(engine) == before


async def test_late_audit_failure_rolls_back_job_occurrence_progress_and_finance(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_schedule_audit_failure BEFORE INSERT ON audit_events "
            "WHEN NEW.action='SCHEDULE_TICK_TEST' "
            "BEGIN SELECT RAISE(ABORT,'SYNTHETIC_AUDIT_FAILURE'); END"
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, row, late_audit=True)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"lookback_days": None, "lookback_months": 1},
        {"lookback_days": None, "lookback_months": 24},
        {"backfill_from_date": "2000-01-01"},
        {"cadence": "WEEKLY", "weekday": 5},
        {"operations": ["SPLITS"]},
        {"operations": ["SPLITS", "DIVIDENDS"], "provider_ids": ["yahoo", "eodhd"]},
    ],
)
async def test_explicit_policy_variants_keep_exact_frozen_windows(
    book: dict[str, Any], engine: Engine, change: dict[str, Any]
) -> None:
    selected = policy(book, **change)
    encoded = canonical(selected.model_dump(mode="json"))
    row = create_schedule(engine, book, policy_json=encoded, policy_sha256=digest(encoded))
    enable(engine, row["id"])
    occurrence, job = enqueue_occurrence(engine, book, row)
    with unit_of_work(engine) as con:
        saved = (
            con.exec_driver_sql(
                "SELECT * FROM source_schedule_occurrences WHERE id=?", (occurrence,)
            )
            .mappings()
            .one()
        )
        expected = occurrence_window(selected, DUE, initial=True)
        assert (saved["window_start"], saved["window_end"]) == (expected.start, expected.end)
        assert saved["policy_json"] == encoded and saved["policy_sha256"] == digest(encoded)
        assert con.exec_driver_sql(
            "SELECT initial_job_id FROM source_schedules WHERE id=?", (row["id"],)
        ).scalar_one() == (job if selected.backfill_from_date else None)


@pytest.mark.parametrize(
    "plan_changes",
    [
        {"force": True},
        {"operations": ["SPLITS"]},
        {"window": {"start": "2026-01-01", "end": "2026-01-10"}},
        {"providers": [{"provider_id": "yahoo", "profile_sha256": None}]},
    ],
)
async def test_occurrence_cannot_relabel_a_different_source_job(
    book: dict[str, Any], engine: Engine, plan_changes: dict[str, Any]
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, row, plan_changes=plan_changes)
    assert database_snapshot(engine) == before


async def test_in_flight_job_blocks_duplicates_and_skip_keeps_all_spending_unchanged(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    _, job = enqueue_occurrence(engine, book, row)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, row, due=DUE + timedelta(days=1))
    assert database_snapshot(engine) == before
    skipped, _ = enqueue_occurrence(
        engine, book, row, due=DUE + timedelta(days=1), blocking_job=job
    )
    after = database_snapshot(engine)
    mutable = {"source_schedules", "source_schedule_occurrences"}
    assert {key: rows for key, rows in after.items() if key not in mutable} == {
        key: rows for key, rows in before.items() if key not in mutable
    }
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT outcome,job_id,blocking_job_id FROM source_schedule_occurrences WHERE id=?",
            (skipped,),
        ).one() == ("SKIPPED_IN_FLIGHT", None, job)
        assert con.exec_driver_sql(
            "SELECT last_sequence,last_job_id FROM source_schedules WHERE id=?", (row["id"],)
        ).one() == (2, job)


async def test_cancelled_initial_backfill_is_not_automatically_reissued(
    book: dict[str, Any], engine: Engine
) -> None:
    encoded = canonical(policy(book, backfill_from_date="2000-01-01").model_dump(mode="json"))
    row = create_schedule(engine, book, policy_json=encoded, policy_sha256=digest(encoded))
    enable(engine, row["id"])
    _, initial_job = enqueue_occurrence(engine, book, row)
    with unit_of_work(engine, write=True) as con:
        jobs.request_cancel(con, UUID(book["owner"]["id"]), UUID(initial_job), at=MATERIALIZED)
    occurrence, _ = enqueue_occurrence(engine, book, row, due=DUE + timedelta(days=1))
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT kind,window_start FROM source_schedule_occurrences WHERE id=?", (occurrence,)
        ).one() == ("REGULAR", "2025-12-13")
        assert (
            con.exec_driver_sql(
                "SELECT initial_job_id FROM source_schedules WHERE id=?", (row["id"],)
            ).scalar_one()
            == initial_job
        )
    changed = canonical(policy(book, backfill_from_date="1999-01-01").model_dump(mode="json"))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE source_schedules SET policy_json=?,policy_sha256=?,version=version+1 "
            "WHERE id=?",
            (changed, digest(changed), row["id"]),
        )
    assert database_snapshot(engine) == before


async def test_pause_rename_and_stale_cas_do_not_cancel_or_rewrite_existing_work(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    enqueue_occurrence(engine, book, row)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        changed = con.exec_driver_sql(
            "UPDATE source_schedules SET enabled=0,next_run_at=NULL,name=?,version=version+1 "
            "WHERE id=? AND owner_id=? AND version=3",
            ("Explicitly paused schedule", row["id"], row["owner_id"]),
        )
        assert changed.rowcount == 1
        assert (
            con.exec_driver_sql(
                "UPDATE source_schedules SET enabled=1,next_run_at=?,version=version+1 "
                "WHERE id=? AND version=3",
                (stamp(DUE), row["id"]),
            ).rowcount
            == 0
        )
    after = database_snapshot(engine)
    assert {key: rows for key, rows in before.items() if key != "source_schedules"} == {
        key: rows for key, rows in after.items() if key != "source_schedules"
    }
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, row, due=DUE + timedelta(days=1))
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["insert", "enable", "occurrence"])
async def test_inactive_owner_cannot_create_enable_or_materialize(
    book: dict[str, Any], engine: Engine, operation: str
) -> None:
    row = create_schedule(engine, book)
    job = None
    if operation == "occurrence":
        enable(engine, row["id"])
        _, job = enqueue_occurrence(engine, book, row)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (row["owner_id"],))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        if operation == "insert":
            create_schedule(engine, book, name="Another disabled schedule")
        elif operation == "enable":
            enable(engine, row["id"])
        else:
            enqueue_occurrence(engine, book, row, due=DUE + timedelta(days=1), blocking_job=job)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("encoded", ["{", "[]", "null", '{"api_key":"synthetic"}', " " * 16385])
async def test_malformed_or_unbounded_policy_is_rejected_without_changes(
    book: dict[str, Any], engine: Engine, encoded: str
) -> None:
    before = database_snapshot(engine)
    with pytest.raises((IntegrityError, OperationalError)), unit_of_work(engine, write=True) as con:
        insert(con, "source_schedules", values(book, policy_json=encoded))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "due,expected_start",
    [
        (DUE.replace(year=2028, month=3, day=31), "2028-02-29"),
        (DUE.replace(year=2030, month=3, day=31), "2030-02-28"),
        (DUE.replace(year=2030, month=2, day=28), "2030-01-28"),
    ],
)
async def test_sql_calendar_month_clipping_agrees_with_the_time_core(
    book: dict[str, Any], engine: Engine, due: datetime, expected_start: str
) -> None:
    selected = policy(book, lookback_days=None, lookback_months=1)
    encoded = canonical(selected.model_dump(mode="json"))
    row = create_schedule(engine, book, policy_json=encoded, policy_sha256=digest(encoded))
    enable(engine, row["id"])
    occurrence, _ = enqueue_occurrence(engine, book, row, due=due)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT window_start FROM source_schedule_occurrences WHERE id=?", (occurrence,)
            ).scalar_one()
            == expected_start
        )


async def test_us_late_dst_slot_keeps_the_prior_local_date(
    book: dict[str, Any], engine: Engine
) -> None:
    with unit_of_work(engine, write=True) as con:
        security = dict(
            con.exec_driver_sql("SELECT * FROM securities WHERE id=?", (book["security"]["id"],))
            .mappings()
            .one()
        )
        exchange = con.exec_driver_sql(
            "SELECT code FROM exchanges WHERE market='US' ORDER BY code LIMIT 1"
        ).scalar_one()
        security.update(id=str(uuid4()), exchange_code=exchange, display_ticker="SCHED")
        insert(con, "securities", security)
    local = {**book, "security": security}
    selected = policy(local, market="US", local_run_time="23:45")
    encoded = canonical(selected.model_dump(mode="json"))
    row = create_schedule(engine, local, policy_json=encoded, policy_sha256=digest(encoded))
    enable(engine, row["id"])
    due = DUE.replace(month=3, day=9, hour=3, minute=45)
    occurrence, _ = enqueue_occurrence(engine, local, row, due=due)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT due_at,window_end FROM source_schedule_occurrences WHERE id=?", (occurrence,)
        ).one() == ("2026-03-09T03:45:00.000000Z", "2026-03-08")


async def test_calendar_exhaustion_has_no_fabricated_future_run(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    enqueue_occurrence(engine, book, row, due=DUE.replace(year=9999, month=12, day=31))
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT enabled,next_run_at,last_sequence FROM source_schedules WHERE id=?",
            (row["id"],),
        ).one() == (1, None, 1)


@pytest.mark.parametrize("field", ["id", "sequence", "schedule_version", "due_at"])
async def test_occurrence_identity_and_slot_cannot_be_materialized_twice(
    book: dict[str, Any], engine: Engine, field: str
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    occurrence, job = enqueue_occurrence(engine, book, row)
    with unit_of_work(engine, write=True) as con:
        jobs.request_cancel(con, UUID(row["owner_id"]), UUID(job), at=MATERIALIZED)
        original = dict(
            con.exec_driver_sql(
                "SELECT * FROM source_schedule_occurrences WHERE id=?", (occurrence,)
            )
            .mappings()
            .one()
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(
            engine, book, row, due=DUE + timedelta(days=1), changes={field: original[field]}
        )
    assert database_snapshot(engine) == before


async def test_one_job_cannot_be_attached_to_two_schedules(
    book: dict[str, Any], engine: Engine
) -> None:
    first = create_schedule(engine, book)
    second = create_schedule(engine, book, name="Independent second schedule")
    enable(engine, first["id"])
    enable(engine, second["id"])
    _, job = enqueue_occurrence(engine, book, first)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, second, changes={"job_id": job})
    assert database_snapshot(engine) == before


async def test_backpressure_never_consumes_a_new_initial_backfill_marker(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    _, job = enqueue_occurrence(engine, book, row)
    encoded = canonical(policy(book, backfill_from_date="2000-01-01").model_dump(mode="json"))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE source_schedules SET policy_json=?,policy_sha256=?,version=version+1 "
            "WHERE id=?",
            (encoded, digest(encoded), row["id"]),
        )
    changed = {**row, "policy_json": encoded, "policy_sha256": digest(encoded)}
    skipped, _ = enqueue_occurrence(
        engine, book, changed, due=DUE + timedelta(days=1), blocking_job=job
    )
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT initial_job_id FROM source_schedules WHERE id=?", (row["id"],)
            ).scalar_one()
            is None
        )
        assert con.exec_driver_sql(
            "SELECT kind,outcome FROM source_schedule_occurrences WHERE id=?", (skipped,)
        ).one() == ("INITIAL_BACKFILL", "SKIPPED_IN_FLIGHT")


async def test_occurrence_requires_the_complete_committed_job_item_scope(
    book: dict[str, Any], engine: Engine
) -> None:
    row = create_schedule(engine, book)
    enable(engine, row["id"])
    selected = policy(book)
    with unit_of_work(engine, write=True) as con:
        template = jobs.enqueue(
            con,
            UUID(row["owner_id"]),
            SourceJobPlan(
                security_ids=selected.security_ids,
                providers=(ProviderSelection(provider_id="eodhd", profile_sha256=None),),
                window=occurrence_window(selected, DUE),
                operations=selected.operations,
            ),
            intent_id=uuid4(),
            at=MATERIALIZED,
        )
        incomplete = dict(template)
        incomplete.update(id=str(uuid4()), dedupe_key=digest(str(uuid4())))
        insert(con, "jobs", incomplete)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError):
        enqueue_occurrence(engine, book, row, changes={"job_id": incomplete["id"]})
    assert database_snapshot(engine) == before
