"""Preserve installed receipt origins while fencing impossible HKD cash relationships."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, event
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from tests.conftest import _check_isolated_database
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import REASON
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_receipt_origin_migration import TABLE, origin_values
from tests.test_dividend_receipt_unknown_migration import populated
from tests.test_schedule_migration import upgrade_to

PREVIOUS = "0028_receipt_origins"
HEAD = "0029_receipt_fx_guard"
ACTION = "MIGRATE_DIVIDEND_RECEIPT_FX_GUARD"
TRIGGER = "trg_dividend_receipt_origin_fx_guard"


@pytest.fixture(autouse=True)
def private_database(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _check_isolated_database(settings, tmp_path, tmp_path_factory.getbasetemp())


@pytest.fixture
def prior(old_engine: Engine) -> Iterator[tuple[Engine, tuple[Any, str, dict[str, Any]]]]:
    upgrade_to(old_engine, "0027_receipt_unknowns")
    event, account, entitlement, _ = populated(old_engine)
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine, (event, account, entitlement)


def add_old_origin(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], **changes: Any
) -> None:
    with engine.connect().execution_options(writer=True) as con, con.begin():
        _, origin = origin_values(con, scenario, **changes)
        insert(con, TABLE, origin)


def test_current_schema_has_origin_fx_guard_and_no_additional_table(engine: Engine) -> None:
    assert schema_head() == "0031_dividend_rebuild"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE type='trigger' AND name=?", (TRIGGER,)
            ).scalar_one()
            == 1
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_keeps_valid_prior_origins_and_every_financial_fact(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]],
) -> None:
    engine, scenario = prior
    add_old_origin(engine, scenario)
    add_old_origin(engine, scenario, rate=None, booked=None, fx_source=None)
    before, definitions = database_snapshot(engine), schema(engine)
    upgrade_to(engine, HEAD)
    after = database_snapshot(engine)
    assert set(after) == set(before)
    for table, rows in before.items():
        if table not in {"alembic_version", "audit_events"}:
            assert after[table] == rows
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(item in schema(engine) for item in definitions)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    upgrade_to(engine, HEAD)
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    "changes",
    [
        {
            "currency": "HKD",
            "rate": 1_000_000_000_000,
            "booked": 6_100_000,
            "fx_source": "IDENTITY",
            "actual_hkd_reason": REASON,
        },
        {"booked": -1, "actual_hkd_reason": REASON},
        {"amount": -6_000_000, "booked": 1, "actual_hkd_reason": REASON},
        {"amount": 0, "booked": 1, "actual_hkd_reason": REASON},
        {"amount": 0, "booked": -1, "actual_hkd_reason": REASON},
    ],
)
def test_inconsistent_old_origin_refuses_upgrade_without_deleting_or_relabeling_it(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]], changes: dict[str, Any]
) -> None:
    engine, scenario = prior
    add_old_origin(engine, scenario, **changes)
    before, definitions = database_snapshot(engine), schema(engine)
    with pytest.raises(RuntimeError, match="inconsistent HKD value"):
        upgrade_to(engine, HEAD)
    assert database_snapshot(engine) == before and schema(engine) == definitions


def test_deferred_migration_cannot_promote_a_stale_wal_snapshot(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]],
) -> None:
    engine, scenario = prior
    ready, committed = Event(), Event()

    def pause_before_ddl(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "CREATE TRIGGER trg_dividend_receipt_origin_fx_guard" in statement:
            ready.set()
            assert committed.wait(timeout=15)

    event.listen(engine, "before_cursor_execute", pause_before_ddl)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            migrating = executor.submit(upgrade_to, engine, HEAD)
            assert ready.wait(timeout=15)
            try:
                add_old_origin(engine, scenario, booked=-1, actual_hkd_reason=REASON)
            finally:
                committed.set()
            with pytest.raises(OperationalError) as error:
                migrating.result(timeout=30)
        assert getattr(error.value.orig, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY_SNAPSHOT
    finally:
        event.remove(engine, "before_cursor_execute", pause_before_ddl)
    with engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == PREVIOUS
        )
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_receipt_origins").scalar_one() == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE name=?", (TRIGGER,)
            ).scalar_one()
            == 0
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"


def test_inconsistent_insert_racing_upgrade_cannot_cross_the_guard(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]],
) -> None:
    engine, scenario = prior
    before = database_snapshot(engine)
    barrier = Barrier(2)

    def migrate_first() -> bool:
        barrier.wait(timeout=15)
        try:
            # Match the CLI's BEGIN IMMEDIATE before scanning old origins.
            with engine.connect().execution_options(writer=True) as con:
                config = migration_config()
                config.attributes["connection"] = con
                command.upgrade(config, HEAD)
            return True
        except RuntimeError as error:
            assert "inconsistent HKD value" in str(error)
            return False

    def insert_first() -> bool:
        barrier.wait(timeout=15)
        try:
            add_old_origin(engine, scenario, booked=-1, actual_hkd_reason=REASON)
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        migrating, inserting = executor.submit(migrate_first), executor.submit(insert_first)
        migrated, inserted = migrating.result(), inserting.result()
    assert migrated != inserted
    after = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        version = con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        assert version == (HEAD if migrated else PREVIOUS)
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == (len(before["owners"]) if migrated else 0)
    assert len(after[TABLE]) == len(before[TABLE]) + int(inserted)
    if migrated:
        for name in ("dividend_receipts", "cash_ledger_entries", TABLE):
            assert after[name] == before[name]


def test_cli_backup_preserves_prior_origin_before_upgrade(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]], settings: Settings
) -> None:
    engine, scenario = prior
    add_old_origin(engine, scenario)
    before = database_snapshot(engine)
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


def test_cli_refusal_retains_prior_database_and_private_backup(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]], settings: Settings
) -> None:
    engine, scenario = prior
    add_old_origin(engine, scenario, booked=-1, actual_hkd_reason=REASON)
    before, definitions = database_snapshot(engine), schema(engine)
    with pytest.raises(RuntimeError, match="inconsistent HKD value"):
        migrate(settings)
    assert database_snapshot(engine) == before and schema(engine) == definitions
    backups = list(settings.data_dir.glob("pre-migration-*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS,)
        for table, rows in before.items():
            escaped = table.replace('"', '""')
            assert con.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall() == rows


@pytest.mark.parametrize("after_first_audit", [False, True])
def test_late_migration_failure_rolls_back_guard_and_audits(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]], after_first_audit: bool
) -> None:
    engine, scenario = prior
    add_old_origin(engine, scenario)
    with engine.connect().execution_options(writer=True) as con, con.begin():
        assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one() >= 2
        condition = (
            f"AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')"
            if after_first_audit
            else ""
        )
        con.exec_driver_sql(f"""
            CREATE TRIGGER synthetic_origin_fx_upgrade_abort BEFORE INSERT ON audit_events
            WHEN NEW.action='{ACTION}' {condition}
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_FAILURE'); END
        """)
    before, definitions = database_snapshot(engine), schema(engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(engine, HEAD)
    assert database_snapshot(engine) == before and schema(engine) == definitions


def test_downgrade_requires_backup_without_removing_guard(
    prior: tuple[Engine, tuple[Any, str, dict[str, Any]]],
) -> None:
    engine, scenario = prior
    add_old_origin(engine, scenario)
    upgrade_to(engine, HEAD)
    before, definitions = database_snapshot(engine), schema(engine)
    with pytest.raises(RuntimeError, match="validated pre-upgrade backup"), engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        command.downgrade(config, PREVIOUS)
    assert database_snapshot(engine) == before and schema(engine) == definitions
