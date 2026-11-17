"""Dated dividend-fee estimates preserve unknowns and existing financial facts."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import audit, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import savepoint, schema_head, unit_of_work
from tests.conftest import _check_isolated_database
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, next_intent, owned_account, submit
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_schedule_migration import upgrade_to

HEAD = "0025_dividend_fee_settings"
PREVIOUS = "0024_dividend_resolutions"
TABLE = "dividend_fee_settings"
ACTION = "MIGRATE_DIVIDEND_FEE_SETTINGS"


@pytest.fixture(autouse=True)
def private_database(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _check_isolated_database(settings, tmp_path, tmp_path_factory.getbasetemp())


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


@pytest.fixture
def scenario(engine: Engine, source: dict[str, Any]) -> tuple[Any, str]:
    _, account = owned_account(engine)
    intent = manual()
    submit(engine, intent)
    return intent, account


def setting_values(
    con: Connection,
    intent: Any,
    account: str,
    *,
    previous: dict[str, Any] | None = None,
    **changes: Any,
) -> dict[str, Any]:
    scope = con.exec_driver_sql(
        "SELECT a.portfolio_id,a.broker_id,a.version AS account_version,"
        "b.version AS broker_version FROM accounts a JOIN brokers b ON b.id=a.broker_id "
        "WHERE a.id=?",
        (account,),
    ).one()
    identifier, request = str(uuid4()), str(uuid4())
    created = datetime.now(UTC) + timedelta(seconds=1)
    with patch("spm_api.audit.now", return_value=created):
        audit(
            con,
            str(OWNER),
            "dividend_fee_setting",
            identifier,
            "DIVIDEND_FEE_SETTING_RECORDED",
            request_id=request,
            before={"synthetic": "previous"} if previous else None,
            after={"synthetic": "shape is not verified broker fees"},
            reason=REASON,
        )
    audit_id = con.exec_driver_sql(
        "SELECT id FROM audit_events WHERE entity_type='dividend_fee_setting' AND entity_id=?",
        (identifier,),
    ).scalar_one()
    return {
        "id": identifier,
        "owner_id": str(OWNER),
        "event_id": str(intent.event_id),
        "revision_id": str(intent.revision_id),
        "revision_no": intent.revision_no,
        "portfolio_id": scope.portfolio_id,
        "account_id": account,
        "broker_id": scope.broker_id,
        "account_version": scope.account_version,
        "broker_version": scope.broker_version,
        "setting_no": 1 if previous is None else previous["setting_no"] + 1,
        "previous_id": None if previous is None else previous["id"],
        "effective_from": "2026-01-01",
        "fee_status": "ESTIMATED",
        "fee_micro": 0,
        "fee_currency": "HKD",
        "request_id": request,
        "audit_id": audit_id,
        "created_at": stamp(created),
        **changes,
    }


def test_fee_setting_migration_starts_empty_without_backfilling_a_broker_default(
    engine: Engine,
) -> None:
    assert schema_head() == "0028_receipt_origins"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_fee_settings").scalar_one() == 0
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_preserves_all_legacy_facts_audits_and_schema(previous_engine: Engine) -> None:
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert (
        before["dividend_receipts"] and before["dividend_entitlements"] and before["audit_events"]
    )
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {
        key: value
        for key, value in before.items()
        if key not in {"alembic_version", "audit_events"}
    } == {key: after[key] for key in before if key not in {"alembic_version", "audit_events"}}
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(item in schema(previous_engine) for item in definitions)
    with unit_of_work(previous_engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_upgrade_keeps_an_exact_private_pre_migration_backup(
    previous_engine: Engine, settings: Settings
) -> None:
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
def test_failed_fee_upgrade_rolls_back_schema_and_every_old_fact(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_fee_settings_delete "
                "BEFORE DELETE ON settings BEGIN SELECT 1; END"
            )
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_fee_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_explicit_zero_unknown_and_nonzero_are_distinct_immutable_assertions(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        zero = setting_values(con, intent, account)
        insert(con, TABLE, zero)
        unknown = setting_values(
            con,
            intent,
            account,
            previous=zero,
            fee_status="UNKNOWN",
            fee_micro=None,
            fee_currency=None,
        )
        insert(con, TABLE, unknown)
        unknown_currency = setting_values(
            con,
            intent,
            account,
            previous=unknown,
            fee_status="UNKNOWN",
            fee_micro=None,
            fee_currency="USD",
        )
        insert(con, TABLE, unknown_currency)
        estimate = setting_values(
            con,
            intent,
            account,
            previous=unknown_currency,
            fee_micro=1_234_567,
            fee_currency="USD",
            effective_from="2026-02-01",
        )
        insert(con, TABLE, estimate)
    after = database_snapshot(engine)
    assert {key: value for key, value in before.items() if key not in {TABLE, "audit_events"}} == {
        key: value for key, value in after.items() if key not in {TABLE, "audit_events"}
    }
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT setting_no,previous_id,effective_from,fee_status,fee_micro,fee_currency "
            "FROM dividend_fee_settings ORDER BY setting_no"
        ).all()
    assert [tuple(row) for row in rows] == [
        (1, None, "2026-01-01", "ESTIMATED", 0, "HKD"),
        (2, zero["id"], "2026-01-01", "UNKNOWN", None, None),
        (3, unknown["id"], "2026-01-01", "UNKNOWN", None, "USD"),
        (4, unknown_currency["id"], "2026-02-01", "ESTIMATED", 1_234_567, "USD"),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "invalid"},
        {"fee_status": None},
        {"fee_status": "CONFIRMED"},
        {"fee_status": "ESTIMATED", "fee_micro": None},
        {"fee_status": "ESTIMATED", "fee_currency": None},
        {"fee_status": "ESTIMATED", "fee_micro": -1},
        {"fee_status": "ESTIMATED", "fee_micro": 1.5},
        {"fee_status": "ESTIMATED", "fee_currency": "CNY"},
        {"fee_status": "ESTIMATED", "fee_currency": "HKD\x00private"},
        {"fee_status": "UNKNOWN", "fee_micro": 0, "fee_currency": None},
        {"fee_status": "UNKNOWN", "fee_micro": None, "fee_currency": "CNY"},
        {"effective_from": "2026-02-30"},
        {"effective_from": "2026-1-01"},
        {"effective_from": "0000-01-01"},
        {"created_at": "2026-02-30T00:00:00.000000Z"},
        {"created_at": "2000-01-01T00:00:00.000000Z"},
        {"created_at": "9999-01-01T00:00:00.000000Z"},
        {"owner_id": str(OTHER)},
        {"event_id": str(uuid4())},
        {"revision_id": str(uuid4())},
        {"revision_no": 0},
        {"revision_no": 2},
        {"portfolio_id": str(uuid4())},
        {"broker_id": str(uuid4())},
        {"account_version": 0},
        {"account_version": 2},
        {"broker_version": 0},
        {"broker_version": 2},
        {"setting_no": 0},
        {"setting_no": 2},
        {"previous_id": str(uuid4())},
        {"request_id": "invalid"},
        {"audit_id": str(uuid4())},
    ],
)
def test_fee_setting_sql_rejects_malformed_or_inconsistent_scope(
    engine: Engine, scenario: tuple[Any, str], changes: dict[str, Any]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        values = setting_values(con, intent, account, **changes)
        with pytest.raises(IntegrityError):
            insert(con, TABLE, values)


def test_fee_settings_cannot_be_updated_deleted_or_replaced(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        values = setting_values(con, intent, account)
        insert(con, TABLE, values)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        for statement in (
            f"UPDATE {TABLE} SET fee_micro=1 WHERE id=:id",
            f"DELETE FROM {TABLE} WHERE id=:id",
        ):
            with pytest.raises(IntegrityError), savepoint(con):
                con.exec_driver_sql(statement, {"id": values["id"]})
        columns = ",".join(values)
        placeholders = ",".join(":" + key for key in values)
        with pytest.raises(IntegrityError), savepoint(con):
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES ({placeholders})", values
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("duplicate", ["event", "fee"])
def test_duplicate_audits_block_fee_setting_without_read_repairs(
    engine: Engine, scenario: tuple[Any, str], duplicate: str
) -> None:
    intent, account = scenario
    if duplicate == "event":
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                str(OWNER),
                "dividend_event_revision",
                str(intent.revision_id),
                "DIVIDEND_EVENT_REVISED",
                request_id=str(uuid4()),
                after={"synthetic": "late duplicate"},
                reason=REASON,
            )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con, pytest.raises(IntegrityError), savepoint(con):
        values = setting_values(con, intent, account)
        if duplicate == "fee":
            audit(
                con,
                str(OWNER),
                "dividend_fee_setting",
                values["id"],
                "DIVIDEND_FEE_SETTING_RECORDED",
                request_id=str(uuid4()),
                after={"synthetic": "duplicate"},
                reason=REASON,
            )
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_competing_first_fee_settings_cannot_create_two_heads(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    barrier = Barrier(2)
    before = database_snapshot(engine)

    def write() -> bool:
        barrier.wait(timeout=10)
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, setting_values(con, intent, account))
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(outcomes) == [False, True]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1


def test_new_event_revision_neither_inherits_nor_rewrites_an_earlier_fee_setting(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        first = setting_values(con, intent, account, fee_micro=100_000, fee_currency="USD")
        insert(con, TABLE, first)
    successor = next_intent(intent, "VERIFY")
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, OWNER, successor, at=datetime.now(UTC), request_id=uuid4())
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(IntegrityError), savepoint(con):
            insert(con, TABLE, setting_values(con, intent, account, previous=first))
        second = setting_values(
            con, successor, account, fee_status="UNKNOWN", fee_micro=None, fee_currency=None
        )
        insert(con, TABLE, second)
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT revision_id,setting_no,previous_id,fee_status,fee_micro,fee_currency "
            "FROM dividend_fee_settings ORDER BY revision_no"
        ).all()
    assert [tuple(row) for row in rows] == [
        (str(intent.revision_id), 1, None, "ESTIMATED", 100_000, "USD"),
        (str(successor.revision_id), 1, None, "UNKNOWN", None, None),
    ]


def test_current_rejection_blocks_new_fee_setting_without_modifying_the_rejection(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        rejection_writer.save(con, OWNER, rejection_intent(intent), request_id=uuid4())
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con, pytest.raises(IntegrityError), savepoint(con):
        insert(con, TABLE, setting_values(con, intent, account))
    assert database_snapshot(engine) == before
