"""Additive projection history preserves legacy facts without granting financial use."""

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
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api import dividend_event_service as events
from spm_api.audit import audit
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import (
    OTHER,
    OWNER,
    REASON,
    manual_revision,
    owned_account,
    posted_receipt,
    publish,
    staged,
)
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT as WRITER_AT
from tests.test_dividend_event_writer import manual, next_intent, submit
from tests.test_dividend_event_writer import owned_account as writer_account
from tests.test_schedule_migration import upgrade_to

HEAD = "0020_dividend_projection_history"
PREVIOUS = "0019_trade_correction_scope"
TABLE = "dividend_projection_versions"
ACTION = "MIGRATE_DIVIDEND_PROJECTION_HISTORY"


@pytest.fixture(autouse=True)
def exclusive_temporary_database(settings: Settings, tmp_path: Path) -> None:
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def previous_engine(old_engine: Engine, tmp_path: Path) -> Iterator[Engine]:
    assert old_engine.url.database is not None
    assert Path(old_engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def snapshot_values(
    con: Connection,
    event: dict[str, Any],
    revision: dict[str, Any],
    account: str,
    *,
    previous: dict[str, Any] | None = None,
    request_id: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    identifier, request = str(uuid4()), request_id or str(uuid4())
    audit(
        con,
        str(OWNER),
        "dividend_projection",
        identifier,
        "DIVIDEND_PROJECTION_CREATED",
        request_id=request,
        after={"synthetic": "well-shaped but not an authenticated economic projection"},
        reason=REASON,
    )
    entry = con.exec_driver_sql(
        "SELECT id,created_at FROM audit_events WHERE entity_type='dividend_projection' "
        "AND entity_id=?",
        (identifier,),
    ).one()
    scope = con.exec_driver_sql(
        "SELECT p.id AS portfolio_id,b.id AS broker_id,a.version AS account_version,"
        "b.version AS broker_version,p.data_revision FROM accounts a "
        "JOIN portfolios p ON p.id=a.portfolio_id JOIN brokers b ON b.id=a.broker_id "
        "WHERE a.id=?",
        (account,),
    ).one()
    return {
        "id": identifier,
        "owner_id": str(OWNER),
        "event_id": event["id"],
        "revision_id": revision["id"],
        "portfolio_id": scope.portfolio_id,
        "account_id": account,
        "broker_id": scope.broker_id,
        "account_version": scope.account_version,
        "broker_version": scope.broker_version,
        "projection_no": 1 if previous is None else previous["projection_no"] + 1,
        "previous_id": None if previous is None else previous["id"],
        "state": "EXPECTED",
        "as_of": "2026-01-08",
        "eligibility_rule": "ORDINARY_PRE_EX_DATE",
        "portfolio_data_revision": scope.data_revision,
        "retired_trade_scope": "NO_RETIRED_TRADES",
        "creation_audit_scope": "NO_RECORDED_CREATION_GAP_REVIEW",
        "history_completeness": "UNVERIFIED",
        "eligible_shares_e8": 1000000000,
        "gross_micro": 10000000,
        "tax_micro": 0,
        "fee_micro": 0,
        "expected_net_micro": 10000000,
        "rounding_delta_micro": 0,
        "currency": revision["currency"],
        "fx_rate_hkd_e12": 7800000000000,
        "fx_source": "MANUAL:synthetic",
        "expected_hkd_micro": 78000000,
        "tax_rate_snapshot_ppm": 0,
        "fee_original_micro": 0,
        "fee_original_currency": revision["currency"],
        "fee_rate_event_e12": 1000000000000,
        "fee_rate_source": "IDENTITY",
        "quality": "OK",
        "request_id": request,
        "audit_id": entry.id,
        "created_at": entry.created_at,
        **changes,
    }


def staged_event(
    con: Connection, source: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    event, revision, _ = staged(con, source, manual=True, status="PROVISIONAL")
    publish(con, event, revision)
    return event, revision, owned_account(con)


def test_head_adds_an_empty_projection_history(engine: Engine) -> None:
    assert schema_head() == "0021_dividend_tax_settings"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_preserves_legacy_receipts_facts_and_existing_guards(
    previous_engine: Engine,
) -> None:
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["dividend_receipts"] and before["dividend_entitlements"]
    assert before["audit_events"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {
        key: rows for key, rows in before.items() if key not in {"alembic_version", "audit_events"}
    } == {key: after[key] for key in before if key not in {"alembic_version", "audit_events"}}
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(item in schema(previous_engine) for item in definitions)
    with unit_of_work(previous_engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_upgrade_retains_a_private_exact_pre_migration_backup(
    previous_engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    assert previous_engine.url.database is not None
    assert Path(previous_engine.url.database).resolve().is_relative_to(tmp_path.resolve())
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
def test_failed_upgrade_rolls_back_schema_and_every_old_fact(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_projection_versions_delete "
                "BEFORE DELETE ON settings BEGIN SELECT 1; END"
            )
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_projection_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_same_revision_can_append_a_second_immutable_projection_without_a_financial_grant(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        first = snapshot_values(con, event, revision, account)
        insert(con, TABLE, first)
        second = snapshot_values(
            con, event, revision, account, previous=first, gross_micro=11000000
        )
        insert(con, TABLE, second)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            f"SELECT projection_no,previous_id,gross_micro FROM {TABLE} ORDER BY projection_no"
        ).all()
        assert [tuple(row) for row in rows] == [(1, None, 10000000), (2, first["id"], 11000000)]
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_entitlements").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0
    with unit_of_work(engine, write=True) as con:
        for statement in (
            f"UPDATE {TABLE} SET gross_micro=1 WHERE id=:id",
            f"DELETE FROM {TABLE} WHERE id=:id",
        ):
            with pytest.raises(IntegrityError), con.begin_nested():
                con.exec_driver_sql(statement, {"id": first["id"]})
        columns = ",".join(first)
        with pytest.raises(IntegrityError), con.begin_nested():
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES "
                f"({','.join(':' + key for key in first)})",
                first,
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "invalid"),
        ("owner_id", str(uuid4())),
        ("event_id", str(uuid4())),
        ("revision_id", str(uuid4())),
        ("portfolio_id", str(uuid4())),
        ("account_id", str(uuid4())),
        ("broker_id", str(uuid4())),
        ("account_version", 0),
        ("account_version", 1.5),
        ("broker_version", 0),
        ("broker_version", 1.5),
        ("projection_no", 0),
        ("projection_no", 2),
        ("projection_no", 1.5),
        ("previous_id", str(uuid4())),
        ("state", "VERIFIED"),
        ("as_of", "2026-02-30"),
        ("eligibility_rule", "INFERRED"),
        ("portfolio_data_revision", -1),
        ("retired_trade_scope", "VERIFIED"),
        ("creation_audit_scope", "VERIFIED"),
        ("history_completeness", "COMPLETE"),
        ("eligible_shares_e8", 0),
        ("gross_micro", -1),
        ("tax_micro", 1.5),
        ("fee_micro", -1),
        ("expected_net_micro", 1.5),
        ("rounding_delta_micro", 1.5),
        ("currency", "HKD"),
        ("fx_rate_hkd_e12", 0),
        ("fx_source", ""),
        ("expected_hkd_micro", 1.5),
        ("tax_rate_snapshot_ppm", 1000001),
        ("fee_original_micro", -1),
        ("fee_original_currency", "EUR"),
        ("fee_rate_event_e12", 0),
        ("fee_rate_source", ""),
        ("quality", "VERIFIED"),
        ("request_id", str(uuid4())),
        ("audit_id", str(uuid4())),
        ("created_at", "2026-02-30T12:00:00.000000Z"),
    ],
)
def test_invalid_projection_shape_or_scope_is_rejected(
    engine: Engine, source: dict[str, Any], field: str, value: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        values = snapshot_values(con, event, revision, account)
    values[field] = value
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field", ["portfolio_id", "broker_id", "account_version", "broker_version"]
)
def test_projection_requires_exact_current_account_and_broker_scope(
    engine: Engine, source: dict[str, Any], field: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        other = owned_account(con)
        other_scope = con.exec_driver_sql(
            "SELECT portfolio_id,broker_id FROM accounts WHERE id=?", (other,)
        ).one()
        values = snapshot_values(con, event, revision, account)
    values[field] = (
        other_scope.portfolio_id
        if field == "portfolio_id"
        else other_scope.broker_id
        if field == "broker_id"
        else values[field] + 1
    )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_historical_projection_retains_its_original_account_associations(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        values = snapshot_values(con, event, revision, account)
        insert(con, TABLE, values)
        other = owned_account(con)
        destination = con.exec_driver_sql(
            "SELECT portfolio_id,broker_id FROM accounts WHERE id=?", (other,)
        ).one()
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,name='Moved synthetic account',"
            "version=version+1 WHERE id=?",
            (destination.portfolio_id, destination.broker_id, account),
        )
    with unit_of_work(engine) as con:
        original = con.exec_driver_sql(
            f"SELECT portfolio_id,broker_id,account_version,broker_version FROM {TABLE} WHERE id=?",
            (values["id"],),
        ).one()
        current = con.exec_driver_sql(
            "SELECT portfolio_id,broker_id,version FROM accounts WHERE id=?", (account,)
        ).one()
    assert tuple(original) == (
        values["portfolio_id"],
        values["broker_id"],
        values["account_version"],
        values["broker_version"],
    )
    assert (current.portfolio_id, current.broker_id) == tuple(destination)
    assert current.version == values["account_version"] + 1
    assert original.portfolio_id != current.portfolio_id
    assert original.broker_id != current.broker_id


def test_changed_broker_fee_fences_a_stale_version_without_portfolio_invalidation(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        stale = snapshot_values(con, event, revision, account)
        con.exec_driver_sql(
            "UPDATE brokers SET dividend_fee_micro=1000000,version=version+1 WHERE id=?",
            (stale["broker_id"],),
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, stale)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        current = snapshot_values(
            con,
            event,
            revision,
            account,
            fee_original_currency="HKD",
            fee_original_micro=1000000,
            fee_micro=130000,
            fee_rate_event_e12=130000000000,
            fee_rate_source="MANUAL:synthetic fee FX",
            expected_net_micro=9870000,
            expected_hkd_micro=76986000,
        )
        assert current["portfolio_data_revision"] == stale["portfolio_data_revision"]
        assert current["broker_version"] == stale["broker_version"] + 1
        insert(con, TABLE, current)


@pytest.mark.parametrize(
    "changes",
    [
        {"gross_micro": None},
        {"quality": None},
        {"fee_original_currency": None},
        {"fx_rate_hkd_e12": None},
        {"expected_hkd_micro": None},
        {"fx_source": "IDENTITY"},
        {"fee_original_currency": "HKD"},
        {"fee_rate_event_e12": 999999999999},
        {"state": "NO_POSITION"},
        {"eligibility_rule": "REQUIRES_REVIEW"},
        {"expected_net_micro": -1, "quality": "OK"},
        {"quality": "MISSING_FX"},
    ],
)
def test_incomplete_or_misclassified_expected_values_fail_closed(
    engine: Engine, source: dict[str, Any], changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        values = snapshot_values(con, event, revision, account, **changes)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["NO_POSITION", "NEEDS_REVIEW", "CANCELLED"])
def test_non_amount_states_are_explicit_not_financial_zero(
    engine: Engine, source: dict[str, Any], state: str
) -> None:
    empty = {
        key: None
        for key in (
            "eligible_shares_e8",
            "gross_micro",
            "tax_micro",
            "fee_micro",
            "expected_net_micro",
            "rounding_delta_micro",
            "fx_rate_hkd_e12",
            "fx_source",
            "expected_hkd_micro",
            "tax_rate_snapshot_ppm",
            "fee_original_micro",
            "fee_original_currency",
            "fee_rate_event_e12",
            "fee_rate_source",
            "quality",
        )
    }
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        values = snapshot_values(con, event, revision, account, state=state, **empty)
        insert(con, TABLE, values)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            f"SELECT state,eligible_shares_e8,expected_net_micro,expected_hkd_micro FROM {TABLE}"
        ).one()
        assert tuple(row) == (state, None, None, None)


def test_unknown_usd_fx_stays_null_and_negative_net_stays_in_review(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        missing = snapshot_values(
            con,
            event,
            revision,
            account,
            fx_rate_hkd_e12=None,
            fx_source=None,
            expected_hkd_micro=None,
            quality="MISSING_FX",
        )
        insert(con, TABLE, missing)
        negative = snapshot_values(
            con,
            event,
            revision,
            account,
            previous=missing,
            fx_rate_hkd_e12=None,
            fx_source=None,
            expected_hkd_micro=None,
            expected_net_micro=-1000000,
            quality="NEEDS_REVIEW",
        )
        insert(con, TABLE, negative)
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            f"SELECT fx_rate_hkd_e12,expected_hkd_micro,quality FROM {TABLE} ORDER BY projection_no"
        ).all()
        assert [tuple(row) for row in rows] == [
            (None, None, "MISSING_FX"),
            (None, None, "NEEDS_REVIEW"),
        ]


def test_hkd_identity_and_explicit_cross_currency_fee_snapshots(
    engine: Engine, source: dict[str, Any]
) -> None:
    hkd_source = {**source, "candidate": {**source["candidate"], "currency": "HKD"}}
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, hkd_source)
        first = snapshot_values(
            con,
            event,
            revision,
            account,
            fx_rate_hkd_e12=1000000000000,
            fx_source="IDENTITY",
            expected_hkd_micro=10000000,
        )
        insert(con, TABLE, first)
        second = snapshot_values(
            con,
            event,
            revision,
            account,
            previous=first,
            fx_rate_hkd_e12=1000000000000,
            fx_source="IDENTITY",
            expected_hkd_micro=10000000,
            fee_original_currency="USD",
            fee_original_micro=1000000,
            fee_rate_event_e12=7900000000000,
            fee_rate_source="MANUAL:synthetic fee FX",
        )
        insert(con, TABLE, second)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            f"SELECT fee_original_currency,fee_rate_event_e12 FROM {TABLE} "
            "ORDER BY projection_no DESC LIMIT 1"
        ).one() == ("USD", 7900000000000)


@pytest.mark.parametrize(
    ("event_currency", "fee_currency", "missing_hkd_fx"),
    [("USD", "HKD", False), ("USD", "HKD", True), ("HKD", "USD", False)],
)
def test_zero_cross_currency_fee_needs_no_invented_conversion_rate(
    engine: Engine,
    source: dict[str, Any],
    event_currency: str,
    fee_currency: str,
    missing_hkd_fx: bool,
) -> None:
    event_source = {**source, "candidate": {**source["candidate"], "currency": event_currency}}
    fx: dict[str, Any] = {}
    if event_currency == "HKD":
        fx = {
            "fx_rate_hkd_e12": 1000000000000,
            "fx_source": "IDENTITY",
            "expected_hkd_micro": 10000000,
        }
    elif missing_hkd_fx:
        fx = {
            "fx_rate_hkd_e12": None,
            "fx_source": None,
            "expected_hkd_micro": None,
            "quality": "MISSING_FX",
        }
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, event_source)
        values = snapshot_values(
            con,
            event,
            revision,
            account,
            fee_original_currency=fee_currency,
            fee_original_micro=0,
            fee_micro=0,
            fee_rate_event_e12=None,
            fee_rate_source=None,
            **fx,
        )
        insert(con, TABLE, values)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            f"SELECT fee_original_currency,fee_original_micro,fee_micro,fee_rate_event_e12,"
            f"fee_rate_source,quality FROM {TABLE} WHERE id=?",
            (values["id"],),
        ).one()
        assert tuple(row) == (
            fee_currency,
            0,
            0,
            None,
            None,
            "MISSING_FX" if missing_hkd_fx else "OK",
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"fee_original_micro": 1},
        {"fee_micro": 1},
        {"fee_original_currency": "USD"},
        {"fee_rate_event_e12": 1000000000000},
        {"fee_rate_source": "MANUAL:synthetic"},
    ],
)
def test_missing_fee_rate_is_allowed_only_for_explicit_zero_cross_currency_amounts(
    engine: Engine, source: dict[str, Any], changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
    before = database_snapshot(engine)
    zero_fee = {
        "fee_original_currency": "HKD",
        "fee_original_micro": 0,
        "fee_micro": 0,
        "fee_rate_event_e12": None,
        "fee_rate_source": None,
        **changes,
    }
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = snapshot_values(con, event, revision, account, **zero_fee)
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_failed_insert_rolls_back_new_audit_and_preserves_old_records(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = snapshot_values(con, event, revision, account, eligible_shares_e8=0)
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_snapshot_cannot_be_appended_to_a_superseded_event_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, old, account = staged_event(con, source)
        current, _ = manual_revision(con, event, source, number=2, dps_e8=110000000)
        publish(con, event, current, version=2)
        stale = snapshot_values(con, event, old, account)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, stale)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, snapshot_values(con, event, current, account))


@pytest.mark.parametrize(
    "recorded_at",
    [datetime(2026, 11, 9, 11, 59, 59, tzinfo=UTC), datetime(2026, 11, 9, 12, 0, 1, tzinfo=UTC)],
)
def test_snapshot_time_cannot_precede_event_revision_or_correlated_audit(
    engine: Engine, source: dict[str, Any], recorded_at: datetime
) -> None:
    with unit_of_work(engine, write=True) as con:
        with patch("spm_api.audit.now", return_value=datetime(2026, 11, 10, tzinfo=UTC)):
            event, revision, account = staged_event(con, source)
        with patch("spm_api.audit.now", return_value=recorded_at):
            early = snapshot_values(con, event, revision, account)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, early)
    assert database_snapshot(engine) == before
    with (
        unit_of_work(engine, write=True) as con,
        patch("spm_api.audit.now", return_value=datetime(2026, 11, 10, 0, 0, 1, tzinfo=UTC)),
    ):
        insert(con, TABLE, snapshot_values(con, event, revision, account))


def test_projection_rejects_a_noncanonical_source_audit_time(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        with patch("spm_api.audit.stamp", return_value="2026-11-10 12:00:00"):
            event, revision, account = staged_event(con, source)
        values = snapshot_values(con, event, revision, account)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_existing_snapshot_fences_event_writer_but_not_historical_replay(
    engine: Engine, source: dict[str, Any]
) -> None:
    _, account = writer_account(engine)
    first, request = manual(), uuid4()
    original = submit(engine, first, request_id=request)
    with unit_of_work(engine, write=True) as con:
        snapshot = snapshot_values(
            con,
            {"id": str(first.event_id)},
            {"id": str(first.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, snapshot)
    before = database_snapshot(engine)
    assert submit(engine, first, request_id=request) == original
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        submit(engine, next_intent(first, "VERIFY"), at=WRITER_AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before


def test_existing_snapshot_fences_direct_event_publication_and_rolls_back_staging(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        insert(con, TABLE, snapshot_values(con, event, revision, account))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        next_revision, _ = manual_revision(con, event, source, number=2, dps_e8=110000000)
        publish(con, event, next_revision, version=2)
    assert database_snapshot(engine) == before


def test_snapshots_coexist_with_managed_receipt_cash_and_locked_fx(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        recorded = posted_receipt(con, source, fx=7800000000000)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        event, revision = recorded["event"], recorded["revision"]
        account = recorded["receipt"]["account_id"]
        first = snapshot_values(con, event, revision, account)
        insert(con, TABLE, first)
        second = snapshot_values(
            con,
            event,
            revision,
            account,
            previous=first,
            gross_micro=11000000,
            expected_net_micro=11000000,
            expected_hkd_micro=85800000,
        )
        insert(con, TABLE, second)
    after = database_snapshot(engine)
    assert {key: value for key, value in before.items() if key not in {TABLE, "audit_events"}} == {
        key: value for key, value in after.items() if key not in {TABLE, "audit_events"}
    }
    assert len(after[TABLE]) == 2 and len(after["audit_events"]) == len(before["audit_events"]) + 2
    with unit_of_work(engine) as con:
        receipt = con.exec_driver_sql(
            "SELECT entitlement_id,received_micro,fx_rate_hkd_e12,received_hkd_micro "
            "FROM dividend_receipts WHERE id=?",
            (recorded["receipt"]["id"],),
        ).one()
        assert tuple(receipt) == (recorded["entitlement"]["id"], 6000000, 7800000000000, 46800000)
        cash = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_id=? AND entry_role='PRIMARY'",
            (recorded["receipt"]["id"],),
        ).one()
        assert tuple(cash) == (6000000, 7800000000000, 46800000)


def test_changed_portfolio_revision_rejects_an_obsolete_snapshot(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        obsolete = snapshot_values(con, event, revision, account)
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=data_revision+1 "
            "WHERE id=(SELECT portfolio_id FROM accounts WHERE id=?)",
            (account,),
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, obsolete)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, snapshot_values(con, event, revision, account))


def test_foreign_owned_account_cannot_become_an_event_projection(
    engine: Engine, source: dict[str, Any]
) -> None:
    _, foreign_account = writer_account(engine, OTHER)
    with unit_of_work(engine, write=True) as con:
        event, revision, _ = staged_event(con, source)
        invalid = snapshot_values(con, event, revision, foreign_account)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, invalid)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("collision", ["projection_no", "request_id"])
def test_replace_cannot_overwrite_a_distinct_snapshot_with_a_conflicting_key(
    engine: Engine, source: dict[str, Any], collision: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        first = snapshot_values(con, event, revision, account)
        insert(con, TABLE, first)
        other = snapshot_values(
            con,
            event,
            revision,
            account,
            previous=first,
            request_id=first["request_id"] if collision == "request_id" else None,
        )
        if collision == "projection_no":
            other.update(projection_no=1, previous_id=None)
    before = database_snapshot(engine)
    columns = ",".join(other)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES "
            f"({','.join(':' + key for key in other)})",
            other,
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("with_previous", [False, True])
def test_competing_snapshots_commit_at_most_one_audit_and_version(
    engine: Engine, source: dict[str, Any], with_previous: bool
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        previous = snapshot_values(con, event, revision, account) if with_previous else None
        if previous is not None:
            insert(con, TABLE, previous)
    before = database_snapshot(engine)
    start = Barrier(2)

    def attempt(_: int) -> str:
        start.wait(timeout=5)
        try:
            with unit_of_work(engine, write=True) as con:
                values = snapshot_values(con, event, revision, account, previous=previous)
                insert(con, TABLE, values)
            return "inserted"
        except IntegrityError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["inserted", "rejected"]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert {key: value for key, value in before.items() if key not in {TABLE, "audit_events"}} == {
        key: value for key, value in after.items() if key not in {TABLE, "audit_events"}
    }


def test_exact_int64_extremes_are_stored_without_float_conversion(
    engine: Engine, source: dict[str, Any]
) -> None:
    maximum, minimum = 2**63 - 1, -(2**63)
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        values = snapshot_values(
            con,
            event,
            revision,
            account,
            eligible_shares_e8=maximum,
            gross_micro=maximum,
            tax_micro=maximum,
            fee_micro=maximum,
            expected_net_micro=minimum,
            rounding_delta_micro=minimum,
            fx_rate_hkd_e12=maximum,
            expected_hkd_micro=minimum,
            fee_original_micro=maximum,
            quality="NEEDS_REVIEW",
        )
        insert(con, TABLE, values)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            f"SELECT eligible_shares_e8,expected_net_micro,rounding_delta_micro,"
            f"fx_rate_hkd_e12,expected_hkd_micro,typeof(expected_net_micro) FROM {TABLE}"
        ).one()
        assert tuple(row) == (maximum, minimum, minimum, maximum, minimum, "integer")


@pytest.mark.parametrize(
    ("field", "value"),
    [("gross_micro", "9223372036854775808"), ("expected_net_micro", "-9223372036854775809")],
)
def test_int64_overflow_is_not_accepted_as_sqlite_real(
    engine: Engine, source: dict[str, Any], field: str, value: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        invalid = snapshot_values(con, event, revision, account)
        invalid[field] = value
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, invalid)
    assert database_snapshot(engine) == before


def test_nonempty_history_cannot_be_downgraded_or_deleted(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        event, revision, account = staged_event(con, source)
        insert(con, TABLE, snapshot_values(con, event, revision, account))
    before, definitions = database_snapshot(engine), schema(engine)
    with (
        pytest.raises(RuntimeError, match="Restore a validated pre-upgrade backup"),
        engine.connect() as con,
    ):
        config = migration_config()
        config.attributes["connection"] = con
        command.downgrade(config, PREVIOUS)
    assert database_snapshot(engine) == before and schema(engine) == definitions
