"""Upstream upgrades preserve legacy facts and guard append-only spending/outcome provenance."""

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
from spm_api.job_models import ProviderSelection, SourceJobPlan
from spm_api.providers.symbols import FetchWindow
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import NOW, insert
from tests.test_action_opening_migration import schema
from tests.test_provider_persistence import START
from tests.test_provider_persistence import book as book
from tests.test_transfer_migration import dimensions, seed_buy
from tests.test_upstream_execution import complete, new_work, reserve

pytestmark = pytest.mark.anyio


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
            insert(
                con,
                "jobs",
                {
                    "id": "legacy-job",
                    "owner_id": "owner",
                    "kind": "OLD_IMPORT",
                    "dedupe_key": "retained-old-job",
                    "status": "RUNNING",
                    "attempt_count": 1,
                    "request_json": '{"legacy":"unchanged"}',
                    "created_at": NOW,
                    "lease_owner": "legacy-worker",
                    "lease_expires_at": NOW,
                },
            )
        upgrade_to(engine, "0009_source_jobs")
        with engine.connect().execution_options(writer=True) as con, con.begin():
            owner = dict(
                con.exec_driver_sql("SELECT * FROM owners WHERE id='owner'").mappings().one()
            )
            owner["id"] = str(uuid4())
            insert(con, "owners", owner)
            security = dict(
                con.exec_driver_sql("SELECT * FROM securities WHERE id='stock'").mappings().one()
            )
            security.update(id=str(uuid4()), display_ticker="BUDG")
            insert(con, "securities", security)
            plan = SourceJobPlan(
                security_ids=(UUID(security["id"]),),
                providers=(ProviderSelection(provider_id="eodhd", profile_sha256="a" * 64),),
                window=FetchWindow(start="2026-01-01", end="2026-01-31"),
                operations=("DIVIDENDS",),
            )
            job = jobs.enqueue(con, UUID(owner["id"]), plan, intent_id=uuid4(), at=START)
            lease = jobs.claim(con, uuid4(), at=START)
            assert lease is not None
            jobs.start(con, lease, at=START)
            con.exec_driver_sql(
                """
                UPDATE job_items SET status='RUNNING',attempt_count=1,lease_generation=1,
                    version=version+1 WHERE job_id=?
            """,
                (job["id"],),
            )
            con.exec_driver_sql(
                "UPDATE jobs SET attempt_count=1,version=version+1 WHERE id=?", (job["id"],)
            )
        yield engine
    finally:
        engine.dispose()


def test_upgrade_preserves_all_original_columns_and_derives_only_the_open_round(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.connect() as con:
        names = (
            con.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'").scalars().all()
        )
        columns = {
            name: [row[1] for row in con.exec_driver_sql(f'PRAGMA table_info("{name}")')]
            for name in names
        }
        owners = con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one()
    before = database_snapshot(old_engine)
    migrate(settings)
    with unit_of_work(old_engine) as con:
        for name, fields in columns.items():
            if name not in {"audit_events", "alembic_version"}:
                assert [
                    tuple(row)
                    for row in con.exec_driver_sql(
                        f'SELECT {",".join(fields)} FROM "{name}" ORDER BY rowid'
                    )
                ] == before[name]
        assert con.exec_driver_sql("SELECT round_open FROM job_items").scalar_one() == 1
        for name in ("provider_budgets", "upstream_attempts", "upstream_outcomes"):
            assert con.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_UPSTREAM_BUDGETS'"
            ).scalar_one()
            == owners
        )
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    accepted = database_snapshot(old_engine)
    migrate(settings)
    assert database_snapshot(old_engine) == accepted


@pytest.mark.parametrize("problem", ["corrupt-round", "late-ddl"])
def test_failed_upgrade_restores_complete_schema_head_and_all_fact_values(
    old_engine: Engine, settings: Settings, problem: str
) -> None:
    with old_engine.begin() as con:
        if problem == "corrupt-round":
            con.exec_driver_sql("DROP TRIGGER trg_job_item_update")
            con.exec_driver_sql("UPDATE job_items SET attempt_count=0")
        else:
            con.exec_driver_sql(
                "CREATE INDEX idx_upstream_origin_window ON provider_status(provider_id)"
            )
    before, original_schema = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises((RuntimeError, OperationalError)):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == original_schema
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0009_source_jobs"
        )


@pytest.fixture
async def reserved(book: dict[str, Any], engine: Engine) -> dict[str, Any]:
    case = new_work(book, engine)
    decision = reserve(engine, case)
    return {**case, "attempt": decision.attempt}


@pytest.fixture
async def valid_attempt_values(reserved: dict[str, Any], engine: Engine) -> dict[str, Any]:
    second = new_work(reserved, engine)
    with unit_of_work(engine, write=True) as con:
        jobs.begin_item(con, second["lease"], second["item"], at=START)
        values = dict(con.exec_driver_sql("SELECT * FROM upstream_attempts").mappings().one())
        values.update(
            id=str(uuid4()),
            job_id=second["job"]["id"],
            item_id=str(second["item"]),
            lease_generation=second["lease"].generation,
            request_sha256=digest("synthetic-schema-reservation"),
        )
        nested = con.begin_nested()
        insert(con, "upstream_attempts", values)
        nested.rollback()
    return values


@pytest.mark.parametrize(
    "changes",
    [
        {"origin_group": "YAHOO"},
        {"provider_id": "yahoo"},
        {"owner_id": "other"},
        {"context_id": "missing"},
        {"lease_generation": 2},
        {"work_attempt": 2},
        {"work_attempt": 1.5},
        {"cursor_in": "/stocks/v1/dividends?api_key=private"},
        {"minute_limit": 0},
        {"day_limit": 100},
        {"policy_sha256": "private"},
        {"request_sha256": "bad"},
        {"reserved_at": "2026-01-09T00:00:00.000000Z"},
        {"reserved_at": "2026-02-30T00:00:00.000000Z"},
        {"expires_at": "2026-01-10T00:00:00.000000Z"},
        {"expires_at": "9999-12-31T23:59:59.999999Z"},
    ],
)
async def test_new_spend_requires_matching_fence_context_policy_and_exact_time(
    valid_attempt_values: dict[str, Any], engine: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "upstream_attempts", {**valid_attempt_values, **changes})
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["inline-key", "member-key", "foreign-origin", "higher-limit", "missing-hash"]
)
async def test_budget_policy_cannot_smuggle_secrets_or_change_shared_origin(
    valid_attempt_values: dict[str, Any], engine: Engine, problem: str
) -> None:
    values = dict(valid_attempt_values)
    payload = json.loads(values["policy_json"])
    if problem == "inline-key":
        payload["api_key"] = "private"
    elif problem == "member-key":
        payload["members"][0]["api_key"] = "private"
    elif problem == "foreign-origin":
        payload["members"][0]["provider_id"] = "yahoo"
    elif problem == "higher-limit":
        payload["requests_per_minute"] += 1
    else:
        del payload["members"][0]["profile_sha256"]
    values["policy_json"] = canonical(payload)
    values["policy_sha256"] = digest(values["policy_json"])
    before = database_snapshot(engine)
    with pytest.raises((IntegrityError, OperationalError)), unit_of_work(engine, write=True) as con:
        insert(con, "upstream_attempts", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("table", ["upstream_attempts", "upstream_outcomes"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_spending_and_outcomes_are_append_only_even_for_noop(
    reserved: dict[str, Any], engine: Engine, table: str, operation: str
) -> None:
    from tests.test_provider_persistence import fetched

    complete(engine, reserved, reserved["attempt"].id, await fetched(reserved))
    key = "id" if table == "upstream_attempts" else "attempt_id"
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_UPSTREAM_ATTEMPT"),
        unit_of_work(engine, write=True) as con,
    ):
        con.exec_driver_sql(
            f"UPDATE {table} SET {key}={key}" if operation == "UPDATE" else f"DELETE FROM {table}"
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"disposition": "COMPLETED", "reason": None},
        {"disposition": "ABANDONED", "reason": "LEASE_EXPIRED"},
        {"disposition": "CANCELLED", "reason": "JOB_CANCELLED"},
        {"disposition": "ABANDONED", "reason": "private raw failure"},
    ],
)
async def test_outcomes_cannot_claim_a_missing_run_or_active_lease_expiry(
    reserved: dict[str, Any], engine: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(
            con,
            "upstream_outcomes",
            {
                "attempt_id": str(reserved["attempt"].id),
                "finished_at": "2026-01-10T00:00:01.000000Z",
                **changes,
            },
        )
    assert database_snapshot(engine) == before


async def test_budget_history_cannot_be_deleted_to_reset_limits(
    reserved: Any, engine: Engine
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_UPSTREAM_BUDGET_HISTORY"),
        unit_of_work(engine, write=True) as con,
    ):
        con.exec_driver_sql("DELETE FROM provider_budgets")
    assert database_snapshot(engine) == before
