"""Projection fee provenance upgrades preserve all existing financial evidence."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_fee_writer as fee_writer
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from spm_api.dividend_projection_history import history
from tests.conftest import _check_isolated_database
from tests.helpers import database_snapshot
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_fee_writer import intent as fee_intent
from tests.test_dividend_projection_tax_migration import populate
from tests.test_schedule_migration import upgrade_to

HEAD = "0026_dividend_projection_fee"
PREVIOUS = "0025_dividend_fee_settings"
TABLE = "dividend_projection_versions"
ACTION = "MIGRATE_DIVIDEND_PROJECTION_FEE"


@pytest.fixture(autouse=True)
def private_database(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _check_isolated_database(settings, tmp_path, tmp_path_factory.getbasetemp())


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def populate_previous(engine: Engine) -> tuple[Any, str, dict[str, Any]]:
    event, account_id, old_projection = populate(engine)
    with engine.connect().execution_options(writer=True) as con, con.begin():
        row = con.exec_driver_sql(
            "SELECT a.portfolio_id,a.broker_id,a.version AS account_version,"
            "b.version AS broker_version FROM accounts a JOIN brokers b ON b.id=a.broker_id "
            "WHERE a.id=?",
            (account_id,),
        ).one()
        book = {
            "event": event,
            "account_id": UUID(account_id),
            "portfolio_id": UUID(row.portfolio_id),
            "broker_id": UUID(row.broker_id),
            "account_version": row.account_version,
            "broker_version": row.broker_version,
        }
        fee_writer.save(con, OWNER, fee_intent(book), request_id=uuid4())
    return event, account_id, old_projection


def test_current_head_adds_empty_fee_projection_provenance(engine: Engine) -> None:
    assert schema_head() == "0030_direct_receipts"
    with unit_of_work(engine) as con:
        columns = {
            row.name
            for row in con.exec_driver_sql("PRAGMA table_info(dividend_projection_versions)")
        }
        assert {
            "fee_provenance_version",
            "fee_effective_on",
            "fee_observed_head_no",
            "fee_observed_head_id",
            "fee_selected_setting_id",
            "fee_estimate_status",
        } <= columns
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_projection_versions").scalar_one()
            == 0
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_preserves_receipts_fees_old_projection_hashes_and_guards(
    previous_engine: Engine,
) -> None:
    event, account_id, old_projection = populate_previous(previous_engine)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before[TABLE] and before["dividend_fee_settings"]
    assert before["dividend_entitlements"] and before["dividend_receipts"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(before) == set(after)
    for table, rows in before.items():
        if table not in {"alembic_version", "audit_events", TABLE}:
            assert after[table] == rows
    assert after[TABLE] == [(*row, None, None, None, None, None, None) for row in before[TABLE]]
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    upgraded = schema(previous_engine)
    assert all(item in upgraded for item in definitions if item[1] != TABLE)
    with unit_of_work(previous_engine) as con:
        retained = history(con, OWNER, event.event_id, UUID(account_id))
        assert retained is not None and retained[0].version.id == UUID(old_projection["id"])
        assert retained[0].fee_provenance is None
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_upgrade_retains_exact_private_pre_migration_backup(
    previous_engine: Engine, settings: Settings
) -> None:
    populate_previous(previous_engine)
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
def test_failed_upgrade_rolls_back_columns_schema_audits_and_all_facts(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_projection_fee_insert BEFORE INSERT ON settings "
                "BEGIN SELECT 1; END"
            )
        else:
            for owner in (OWNER, OTHER):
                con.exec_driver_sql(
                    "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                    "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
                    (str(owner),),
                )
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_projection_fee_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_nonempty_fee_projection_history_refuses_downgrade_without_mutation(
    previous_engine: Engine,
) -> None:
    populate_previous(previous_engine)
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
