"""Additive, inert tax-setting storage preserves retained financial facts."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_event_writer as event_writer
from spm_api.audit import audit, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import owned_account as historical_account
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT, manual, next_intent, owned_account, submit
from tests.test_dividend_projection_migration import snapshot_values
from tests.test_schedule_migration import upgrade_to

HEAD = "0021_dividend_tax_settings"
PREVIOUS = "0020_dividend_projection_history"
TABLE = "dividend_tax_settings"
ACTION = "MIGRATE_DIVIDEND_TAX_SETTINGS"


@pytest.fixture(autouse=True)
def private_database(settings: Settings, tmp_path: Path) -> None:
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())


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
            "dividend_tax_setting",
            identifier,
            "DIVIDEND_TAX_SETTING_RECORDED",
            request_id=request,
            before={"synthetic": "previous"} if previous else None,
            after={"synthetic": "shape is not a verified tax rate"},
            reason=REASON,
        )
    audit_id = con.exec_driver_sql(
        "SELECT id FROM audit_events WHERE entity_type='dividend_tax_setting' AND entity_id=?",
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
        "tax_status": "ESTIMATED",
        "rate_ppm": 300000,
        "request_id": request,
        "audit_id": audit_id,
        "created_at": stamp(created),
        **changes,
    }


def test_head_starts_empty_without_setting_a_rate(engine: Engine) -> None:
    assert schema_head() == "0022_dividend_projection_tax"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_retains_all_previous_facts_and_audits(previous_engine: Engine) -> None:
    intent = manual()
    portfolio, broker, account = (str(uuid4()) for _ in range(3))
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
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
        event_writer.save(con, OWNER, intent, at=AT, request_id=uuid4())
        snapshot = snapshot_values(
            con,
            {"id": str(intent.event_id)},
            {"id": str(intent.revision_id), "currency": "USD"},
            account,
        )
        insert(con, "dividend_projection_versions", snapshot)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["dividend_receipts"] and before["dividend_entitlements"]
    assert before["audit_events"] and before["dividend_projection_versions"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {k: v for k, v in before.items() if k not in {"alembic_version", "audit_events"}} == {
        k: after[k] for k in before if k not in {"alembic_version", "audit_events"}
    }
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


def test_cli_preserves_a_private_exact_pre_upgrade_backup(
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
def test_failure_rolls_back_schema_and_all_facts(previous_engine: Engine, failure: str) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_tax_settings_delete "
                "BEFORE DELETE ON settings BEGIN SELECT 1; END"
            )
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_tax_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_known_zero_and_explicit_unknown_remain_distinct_immutable_records(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        zero = setting_values(con, intent, account, rate_ppm=0)
        insert(con, TABLE, zero)
        unknown = setting_values(
            con, intent, account, previous=zero, tax_status="UNKNOWN", rate_ppm=None
        )
        insert(con, TABLE, unknown)
    after = database_snapshot(engine)
    assert {k: v for k, v in before.items() if k not in {TABLE, "audit_events"}} == {
        k: v for k, v in after.items() if k not in {TABLE, "audit_events"}
    }
    assert len(after[TABLE]) == len(before[TABLE]) + 2
    assert len(after["audit_events"]) == len(before["audit_events"]) + 2
    with unit_of_work(engine, write=True) as con:
        rows = con.exec_driver_sql(
            f"SELECT setting_no,previous_id,tax_status,rate_ppm FROM {TABLE} ORDER BY setting_no"
        ).all()
        assert [tuple(row) for row in rows] == [
            (1, None, "ESTIMATED", 0),
            (2, zero["id"], "UNKNOWN", None),
        ]
        for sql in (
            f"UPDATE {TABLE} SET rate_ppm=1 WHERE id=:id",
            f"DELETE FROM {TABLE} WHERE id=:id",
        ):
            with pytest.raises(IntegrityError), con.begin_nested():
                con.exec_driver_sql(sql, {"id": zero["id"]})
        columns = ",".join(zero)
        with pytest.raises(IntegrityError), con.begin_nested():
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES "
                f"({','.join('?' for _ in zero)})",
                tuple(zero.values()),
            )
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    "changes",
    [
        {"rate_ppm": -1},
        {"rate_ppm": 1000001},
        {"rate_ppm": 0.5},
        {"rate_ppm": None},
        {"tax_status": "UNKNOWN"},
        {"tax_status": "CONFIRMED"},
        {"effective_from": "2026-02-30"},
        {"effective_from": "0000-01-01"},
        {"effective_from": "2026-1-01"},
        {"revision_no": 0},
        {"revision_no": 2},
        {"event_id": str(uuid4())},
        {"revision_id": str(uuid4())},
        {"owner_id": str(OTHER)},
        {"account_version": 2},
        {"broker_version": 2},
        {"account_id": str(uuid4())},
        {"portfolio_id": str(uuid4())},
        {"broker_id": str(uuid4())},
        {"setting_no": 2},
        {"previous_id": str(uuid4())},
        {"created_at": "2020-01-01T00:00:00.000000Z"},
        {"id": "not-a-canonical-uuid"},
        {"request_id": "bad-request"},
    ],
)
def test_invalid_scope_rate_date_or_chain_rolls_back_audit(
    engine: Engine, scenario: tuple[Any, str], changes: dict[str, Any]
) -> None:
    intent, account = scenario
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, setting_values(con, intent, account, **changes))
    assert database_snapshot(engine) == before


def test_other_owner_account_and_a_missing_or_duplicate_audit_are_rejected(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    _, foreign = owned_account(engine, OTHER)
    before = database_snapshot(engine)
    bad_scopes: tuple[dict[str, Any], ...] = (
        {"account_id": foreign},
        {"audit_id": str(uuid4())},
    )
    with unit_of_work(engine, write=True) as con:
        for changes in bad_scopes:
            with pytest.raises(IntegrityError), con.begin_nested():
                insert(con, TABLE, setting_values(con, intent, account, **changes))
        with pytest.raises(IntegrityError), con.begin_nested():
            values = setting_values(con, intent, account)
            audit(
                con,
                str(OWNER),
                "dividend_tax_setting",
                values["id"],
                "DIVIDEND_TAX_SETTING_RECORDED",
                request_id=values["request_id"],
                after={"synthetic": "duplicate"},
                reason=REASON,
            )
            insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_old_revision_cannot_receive_a_new_setting_after_event_revision(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        original = setting_values(con, intent, account)
        insert(con, TABLE, original)
    new = next_intent(intent, "VERIFY")
    submit(engine, new, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, setting_values(con, intent, account, previous=original))
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        first_new = setting_values(con, new, account, tax_status="UNKNOWN", rate_ppm=None)
        insert(con, TABLE, first_new)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 2
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0


def test_competing_first_versions_commit_only_one_audit_and_setting(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    intent, account = scenario
    before = database_snapshot(engine)

    def attempt() -> bool:
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, setting_values(con, intent, account))
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert {k: v for k, v in before.items() if k not in {TABLE, "audit_events"}} == {
        k: v for k, v in after.items() if k not in {TABLE, "audit_events"}
    }


def test_downgrade_does_not_delete_tax_history(previous_engine: Engine) -> None:
    # Exercise the historical tax migration's guard, not the newer head's guard.
    upgrade_to(previous_engine, HEAD)
    intent = manual()
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (str(OWNER), "synthetic-only", stamp(AT), stamp(AT)),
        )
        account = historical_account(con)
        event_writer.save(con, OWNER, intent, at=AT, request_id=uuid4())
        insert(con, TABLE, setting_values(con, intent, account))
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with previous_engine.begin() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated pre-upgrade backup"):
            command.downgrade(config, PREVIOUS)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions
