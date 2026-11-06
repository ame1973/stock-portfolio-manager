"""Managed jobs retain old queue/facts and reject forged state, plans and checkpoints."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.job_service as jobs
from spm_api.audit import canonical, digest
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import NOW, insert, seed_evidence
from tests.test_action_opening_migration import schema
from tests.test_jobs import active, record
from tests.test_jobs import queued as queued
from tests.test_provider_persistence import FINISH, START, fetched
from tests.test_provider_persistence import book as book
from tests.test_transfer_migration import dimensions, seed_buy

pytestmark = pytest.mark.anyio


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0008_provider_evidence")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
            seed_evidence(con, "owner", "stock")
            insert(
                con,
                "jobs",
                {
                    "id": "legacy-job",
                    "owner_id": "owner",
                    "kind": "DIVIDEND_SYNC",
                    "dedupe_key": "retained-legacy-intent",
                    "status": "RUNNING",
                    "progress_done": 1,
                    "progress_total": 2,
                    "attempt_count": 1,
                    "max_attempts": 4,
                    "lease_owner": "legacy-worker",
                    "lease_expires_at": NOW,
                    "request_json": '{"legacy":"retained exactly"}',
                    "created_at": NOW,
                    "started_at": NOW,
                },
            )
        yield engine
    finally:
        engine.dispose()


def test_upgrade_retains_every_old_column_without_adopting_legacy_job_state(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.connect() as con:
        names = (
            con.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'").scalars().all()
        )
        fields = {
            name: [row[1] for row in con.exec_driver_sql(f'PRAGMA table_info("{name}")')]
            for name in names
        }
    before = database_snapshot(old_engine)
    migrate(settings)
    with unit_of_work(old_engine) as con:
        for name, columns in fields.items():
            if name not in {"audit_events", "alembic_version"}:
                assert [
                    tuple(row)
                    for row in con.exec_driver_sql(
                        f'SELECT {",".join(columns)} FROM "{name}" ORDER BY rowid'
                    )
                ] == before[name]
        assert con.exec_driver_sql(
            "SELECT runtime_version,version,lease_generation,request_sha256,updated_at FROM jobs"
        ).one() == (0, 1, 0, None, None)
        assert (
            con.exec_driver_sql("SELECT requested_cursor FROM provider_runs").scalar_one() is None
        )
        assert con.exec_driver_sql("SELECT count(*) FROM job_items").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM job_checkpoints").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_SOURCE_JOBS'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
    accepted = database_snapshot(old_engine)
    migrate(settings)
    assert database_snapshot(old_engine) == accepted
    with unit_of_work(old_engine, write=True) as con:
        assert jobs.claim(con, uuid4(), at=START) is None
    assert database_snapshot(old_engine) == accepted


def test_late_ddl_failure_restores_original_job_source_facts_and_head(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        con.exec_driver_sql("CREATE INDEX idx_job_items_ready ON provider_status(provider_id)")
    before, original_schema = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises(OperationalError):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == original_schema
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0008_provider_evidence"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"runtime_version": 0},
        {"kind": "FAKE_SYNC"},
        {"status": "SUCCEEDED"},
        {"progress_done": 1},
        {"progress_total": 101},
        {"progress_total": 1.5},
        {"attempt_count": 1},
        {"max_attempts": 5},
        {"request_sha256": "not-a-hash"},
        {"lease_generation": 1},
        {"lease_owner": "worker"},
        {"lease_expires_at": "bad"},
        {"result_json": '{"success":true}'},
        {"version": 2},
        {"updated_at": "2026-02-30T00:00:00.000000Z"},
        {"updated_at": "2026-01-10T24:00:00.000000Z"},
        {"created_at": "0000-01-01T00:00:00.000000Z"},
    ],
)
async def test_new_jobs_require_sealed_initial_control_state(
    queued: Any, engine: Engine, changes: dict[str, Any]
) -> None:
    values = {**queued["job"], "id": str(uuid4()), "dedupe_key": digest(str(uuid4())), **changes}
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "jobs", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "inline-key",
        "nested-key",
        "missing-profile-hash",
        "duplicate-provider",
        "missing-force",
        "wrong-force",
        "float-version",
        "null-operation",
        "duplicate-operation",
        "empty-symbols",
        "bad-date",
        "year-zero",
        "extra-window",
        "unknown-provider",
    ],
)
async def test_sql_plan_whitelist_and_json_types_cannot_be_bypassed(
    queued: Any, engine: Engine, problem: str
) -> None:
    payload = json.loads(queued["job"]["request_json"])
    if problem == "inline-key":
        payload["api_key"] = "private"
    elif problem == "nested-key":
        payload["providers"][0]["api_key"] = "private"
    elif problem == "missing-profile-hash":
        del payload["providers"][0]["profile_sha256"]
    elif problem == "duplicate-provider":
        payload["providers"] *= 2
    elif problem == "missing-force":
        del payload["force"]
    elif problem == "wrong-force":
        payload["force"] = 1
    elif problem == "float-version":
        payload["schema_version"] = 1.0
    elif problem == "null-operation":
        payload["operations"] = [None]
    elif problem == "duplicate-operation":
        payload["operations"] *= 2
    elif problem == "empty-symbols":
        payload["security_ids"] = []
    elif problem == "bad-date":
        payload["window"] = {"start": "2026-02-30", "end": "2026-03-01"}
    elif problem == "year-zero":
        payload["window"]["start"] = "0000-01-01"
    elif problem == "extra-window":
        payload["window"]["api_key"] = "private"
    else:
        payload["providers"][0]["provider_id"] = "foreign"
    values = {
        **queued["job"],
        "id": str(uuid4()),
        "dedupe_key": digest(str(uuid4())),
        "request_json": canonical(payload),
        "request_sha256": digest(canonical(payload)),
    }
    before = database_snapshot(engine)
    with pytest.raises((IntegrityError, OperationalError)), unit_of_work(engine, write=True) as con:
        insert(con, "jobs", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": "other"},
        {"kind": "ANOTHER_JOB"},
        {"dedupe_key": "replacement"},
        {"request_json": "{}"},
        {"request_sha256": "f" * 64},
        {"progress_total": 2},
        {"lease_generation": 2},
        {"lease_owner": "another-worker"},
        {"started_at": None},
        {"status": "QUEUED"},
        {"attempt_count": 1.5},
        {"last_error_code": "PRIVATE_UPSTREAM_TEXT"},
    ],
)
async def test_job_identity_lineage_and_transition_controls_are_not_arbitrary_updates(
    queued: Any, engine: Engine, changes: dict[str, Any]
) -> None:
    lease = active(engine)
    before = database_snapshot(engine)
    updates = {"version": job_value(engine, lease.job_id)["version"] + 1, **changes}
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"UPDATE jobs SET {','.join(f'{key}=?' for key in updates)} WHERE id=?",
            (*updates.values(), str(lease.job_id)),
        )
    assert database_snapshot(engine) == before


def job_value(engine: Engine, identifier: UUID) -> dict[str, Any]:
    with unit_of_work(engine) as con:
        return dict(
            con.exec_driver_sql("SELECT * FROM jobs WHERE id=?", (str(identifier),))
            .mappings()
            .one()
        )


async def test_raw_sql_cannot_skip_item_or_job_to_success(queued: Any, engine: Engine) -> None:
    lease = active(engine)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE job_items SET status='SUCCEEDED',lease_generation=?,version=version+1 "
            "WHERE id=?",
            (lease.generation, str(queued["item"])),
        )
    assert database_snapshot(engine) == before
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE jobs SET status='SUCCEEDED',progress_done=1,finished_at=updated_at,"
            "lease_owner=NULL,lease_expires_at=NULL,version=version+1 WHERE id=?",
            (str(lease.job_id),),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("table", ["jobs", "job_items"])
async def test_control_history_is_not_deleted(queued: Any, engine: Engine, table: str) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_JOB_HISTORY"),
        unit_of_work(engine, write=True) as con,
    ):
        con.exec_driver_sql(f"DELETE FROM {table}")
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_committed_checkpoints_are_append_only_including_noop(
    queued: Any, book: dict[str, Any], engine: Engine, operation: str
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    record(engine, lease, queued["item"], book, await fetched(book))
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_JOB_CHECKPOINT"),
        unit_of_work(engine, write=True) as con,
    ):
        con.exec_driver_sql(
            "UPDATE job_checkpoints SET sequence=sequence"
            if operation == "UPDATE"
            else "DELETE FROM job_checkpoints"
        )
    assert database_snapshot(engine) == before


async def test_cursorless_attempt_fingerprint_stays_compatible_with_pre_job_capture(
    queued: Any, book: dict[str, Any], engine: Engine
) -> None:
    lease = active(engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, lease, queued["item"], at=START)
    page = await fetched(book)
    receipt = record(engine, lease, queued["item"], book, page)
    prepared = book["prepared"]
    expected = digest(
        canonical(
            {
                "context_id": str(prepared.id),
                "context_sha256": digest(canonical(prepared.metadata())),
                "job_id": str(lease.job_id),
                "page": page.model_dump(mode="json"),
                "started_at": START.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "finished_at": FINISH.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "duration_ms": 12,
            }
        )
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT response_sha256,requested_cursor FROM provider_runs WHERE id=?",
            (str(receipt.run_id),),
        ).one() == (expected, None)
