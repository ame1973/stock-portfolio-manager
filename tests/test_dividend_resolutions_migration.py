"""Rejection-resolution migration preserves history and keeps SQL publication fenced."""

import sqlite3
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_resolution as resolutions
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER, entitlement
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent, owned_account
from tests.test_dividend_projection_migration import snapshot_values
from tests.test_dividend_projection_tax_migration import populate
from tests.test_dividend_rejection_migration import previous_engine as previous_engine
from tests.test_dividend_rejection_resolution import reject, resolve
from tests.test_dividend_rejection_service import case as case
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_schedule_migration import upgrade_to

HEAD = "0024_dividend_resolutions"
PREVIOUS = "0023_dividend_rejections"
TABLE = "dividend_rejection_resolutions"
ACTION = "MIGRATE_DIVIDEND_RESOLUTIONS"


def seed_previous(engine: Engine) -> None:
    event, _, _ = populate(engine)
    upgrade_to(engine, PREVIOUS)
    with engine.connect().execution_options(writer=True) as con, con.begin():
        rejection_writer.save(con, OWNER, rejection_intent(event), request_id=uuid4())


def test_current_head_starts_without_inferred_resolutions(engine: Engine) -> None:
    assert schema_head() == HEAD
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_preserves_prior_financial_facts_and_retains_unresolved_rejection(
    previous_engine: Engine,
) -> None:
    seed_previous(previous_engine)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["dividend_event_rejections"] and before["dividend_entitlements"]
    assert before["dividend_projection_versions"] and before["dividend_receipts"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    for table in set(before) - {"audit_events", "alembic_version"}:
        assert after[table] == before[table]
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    # The prior publication trigger is deliberately replaced, not removed without a successor.
    assert all(
        item in schema(previous_engine)
        for item in definitions
        if item[1] != "trg_dividend_rejection_event_publication"
    )
    with unit_of_work(previous_engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_backup_is_the_exact_private_pre_upgrade_snapshot(
    previous_engine: Engine, settings: Settings
) -> None:
    seed_previous(previous_engine)
    before = database_snapshot(previous_engine)
    migrate(settings)
    backups = list(settings.data_dir.glob("pre-migration-*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in before.items():
            escaped = table.replace('"', '""')
            assert con.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall() == rows
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS,)
    after = database_snapshot(previous_engine)
    migrate(settings)
    assert database_snapshot(previous_engine) == after
    assert list(settings.data_dir.glob("pre-migration-*.sqlite3")) == backups


@pytest.mark.parametrize("failure", ["late-ddl", "second-owner-audit"])
def test_failed_migration_rolls_back_schema_replaced_trigger_audits_and_facts(
    previous_engine: Engine, failure: str
) -> None:
    upgrade_to(previous_engine, PREVIOUS)
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_resolutions_delete BEFORE DELETE ON settings "
                "BEGIN SELECT 1; END"
            )
        else:
            for owner in (OWNER, OTHER):
                con.exec_driver_sql(
                    "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                    "VALUES (?,?,?,?)",
                    (str(owner), "synthetic-only", stamp(), stamp()),
                )
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_resolution_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_nonempty_upgrade_cannot_be_downgraded(previous_engine: Engine) -> None:
    seed_previous(previous_engine)
    upgrade_to(previous_engine, HEAD)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with (
        pytest.raises(RuntimeError, match="validated pre-upgrade backup"),
        previous_engine.connect() as con,
    ):
        config = migration_config()
        config.attributes["connection"] = con
        command.downgrade(config, PREVIOUS)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


@pytest.mark.parametrize("operation", ["update", "delete", "replace_id", "replace_rejection"])
def test_resolution_rows_are_immutable_even_with_replacement(
    engine: Engine, case: Any, operation: str
) -> None:
    rejected = reject(engine, case)
    resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "update":
            con.exec_driver_sql(f"UPDATE {TABLE} SET reason=reason")
        elif operation == "delete":
            con.exec_driver_sql(f"DELETE FROM {TABLE}")
        else:
            row = con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()
            values = dict(row)
            if operation == "replace_rejection":
                values["id"] = str(uuid4())
            columns = ",".join(values)
            marks = ",".join("?" for _ in values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES ({marks})",
                tuple(values.values()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-uuid"),
        ("owner_id", str(OTHER)),
        ("event_id", str(uuid4())),
        ("rejection_id", str(uuid4())),
        ("rejected_revision_id", str(uuid4())),
        ("successor_revision_id", str(uuid4())),
        ("successor_revision_no", 3),
        ("reason", "A different synthetic reason"),
        ("request_id", str(uuid4())),
        ("audit_id", str(uuid4())),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2020-01-01T00:00:00.000000Z"),
    ],
)
def test_sql_requires_exact_audited_resolution_scope_and_rolls_back_staging(
    engine: Engine, case: Any, field: str, value: Any
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def corrupt(*args: Any, **kwargs: Any) -> Any:
            if "INSERT INTO dividend_rejection_resolutions(" in str(args[0]):
                values = dict(args[1])
                values[field] = value
                return execute(args[0], values)
            return execute(*args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=corrupt),
            pytest.raises(IntegrityError),
        ):
            event_writer.save(
                con,
                OWNER,
                next_intent(case, "VERIFY", resolve_rejection_id=rejected.id),
                at=datetime.now(UTC),
                request_id=uuid4(),
            )
    assert database_snapshot(engine) == before


def test_sql_shaped_false_resolution_audit_is_not_accepted_by_application(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    original_append = resolutions.append
    inserted = False

    def forged(*args: Any, **kwargs: Any) -> None:
        nonlocal inserted
        with patch.object(resolutions, "_payload", return_value={"synthetic": "false"}):
            original_append(*args, **kwargs)
        inserted = True

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(resolutions, "append", side_effect=forged),
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        event_writer.save(
            con,
            OWNER,
            next_intent(case, "VERIFY", resolve_rejection_id=rejected.id),
            at=datetime.now(UTC),
            request_id=uuid4(),
        )
    assert inserted
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("financial_history", ["entitlement", "projection"])
def test_sql_publication_guard_still_blocks_resolution_with_historical_financial_facts(
    engine: Engine, case: Any, financial_history: str
) -> None:
    _, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        if financial_history == "entitlement":
            claim = entitlement({"id": str(case.revision_id), "currency": "USD"}, account)
            claim["created_at"] = stamp()
            insert(con, "dividend_entitlements", claim)
        else:
            snapshot = snapshot_values(
                con,
                {"id": str(case.event_id)},
                {"id": str(case.revision_id), "currency": "USD"},
                account,
            )
            insert(con, "dividend_projection_versions", snapshot)
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def bypass_application_check(*args: Any, **kwargs: Any) -> Any:
            if "SELECT 1 FROM dividend_entitlements ent" in str(args[0]):
                result = MagicMock()
                result.first.return_value = None
                return result
            return execute(*args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=bypass_application_check),
            pytest.raises(IntegrityError, match="DIVIDEND_EVENT_REJECTION_PENDING"),
        ):
            event_writer.save(
                con,
                OWNER,
                next_intent(case, "VERIFY", resolve_rejection_id=rejected.id),
                at=datetime.now(UTC),
                request_id=uuid4(),
            )
    assert database_snapshot(engine) == before
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejections.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"),
    ):
        rejections.require_clear(con, OWNER, case.event_id, revision_id=case.revision_id, version=1)
