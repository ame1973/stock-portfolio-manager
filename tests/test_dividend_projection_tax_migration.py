"""Projection tax provenance upgrades preserve retained financial history."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_tax_service as tax
from spm_api.audit import stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from spm_api.dividend_projection_history import history
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_writer import AT, manual, next_intent, terms
from tests.test_dividend_projection_history import append
from tests.test_schedule_migration import upgrade_to

HEAD = "0022_dividend_projection_tax"
PREVIOUS = "0021_dividend_tax_settings"
TABLE = "dividend_projection_versions"
ACTION = "MIGRATE_DIVIDEND_PROJECTION_TAX"


@pytest.fixture(autouse=True)
def private_database(settings: Settings, tmp_path: Path) -> None:
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def populate(engine: Engine) -> tuple[Any, str, dict[str, Any]]:
    event = manual(terms=terms(dps="1"))
    portfolio, broker, account = (str(uuid4()) for _ in range(3))
    with engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (str(OWNER), "synthetic-only", stamp(AT), stamp(AT)),
        )
        insert(
            con,
            "portfolios",
            {"id": portfolio, "owner_id": str(OWNER), "name": portfolio, "created_at": stamp(AT)},
        )
        insert(con, "brokers", {"id": broker, "owner_id": str(OWNER), "name": broker})
        insert(
            con,
            "accounts",
            {
                "id": account,
                "portfolio_id": portfolio,
                "broker_id": broker,
                "name": account,
                "default_currency": "USD",
                "created_at": stamp(AT),
            },
        )
        event_writer.save(con, OWNER, event, at=AT, request_id=uuid4())
        old = append(con, event, account)
        tax.save(
            con,
            OWNER,
            tax.SettingIntent(
                id=uuid4(),
                event_id=event.event_id,
                revision_id=event.revision_id,
                revision_no=1,
                account_id=UUID(account),
                portfolio_id=UUID(portfolio),
                broker_id=UUID(broker),
                account_version=1,
                broker_version=1,
                expected_setting_no=0,
                effective_from="2026-01-01",
                rate="0",
                reason=REASON,
            ),
            request_id=uuid4(),
        )
    return event, account, old


def test_head_adds_no_business_table_or_assumed_tax_rate(engine: Engine) -> None:
    assert schema_head() == HEAD
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_tax_settings").scalar_one() == 0
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_projection_versions").scalar_one()
            == 0
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_preserves_old_rows_audits_receipts_and_existing_guards(
    previous_engine: Engine,
) -> None:
    event, account, old = populate(previous_engine)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before[TABLE] and before["dividend_entitlements"] and before["dividend_receipts"]
    assert before["dividend_tax_settings"] and before["audit_events"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(before) == set(after)
    for table, rows in before.items():
        if table in {"alembic_version", "audit_events", TABLE}:
            continue
        assert after[table] == rows
    assert after[TABLE] == [(*row, None, None, None, None, None, None) for row in before[TABLE]]
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    upgraded_definitions = schema(previous_engine)
    assert all(item in upgraded_definitions for item in definitions if item[1] != TABLE)
    with unit_of_work(previous_engine) as con:
        retained = history(con, OWNER, event.event_id, UUID(account))
        assert retained is not None and retained[0].version.id == UUID(old["id"])
        assert retained[0].tax_provenance is None
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    with (
        pytest.raises(events.EventError, match="DIVIDEND_EVENT_PROJECTION_REQUIRED"),
        previous_engine.connect().execution_options(writer=True) as con,
        con.begin(),
    ):
        event_writer.save(con, OWNER, next_intent(event, "VERIFY"), at=AT, request_id=uuid4())
    assert database_snapshot(previous_engine) == after
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_backup_is_an_exact_private_pre_upgrade_snapshot(
    previous_engine: Engine, settings: Settings
) -> None:
    populate(previous_engine)
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
def test_failed_upgrade_rolls_back_columns_schema_audits_and_data(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_projection_tax_insert BEFORE INSERT ON settings "
                "BEGIN SELECT 1; END"
            )
        else:
            for owner in (OWNER, OTHER):
                con.exec_driver_sql(
                    "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                    "VALUES (?,?,?,?)",
                    (str(owner), "synthetic-only", stamp(AT), stamp(AT)),
                )
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_projection_tax_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_nonempty_history_rejects_downgrade_without_mutation(previous_engine: Engine) -> None:
    populate(previous_engine)
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
