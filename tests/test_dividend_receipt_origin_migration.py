"""Add receipt provenance without adopting or rewriting any historical financial facts."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.audit import audit, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import post_cash
from tests.conftest import _check_isolated_database
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER, REASON, reverse_receipt
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, owned_account, submit
from tests.test_dividend_receipt_history import claim, prepared
from tests.test_dividend_receipt_unknown_migration import populated
from tests.test_schedule_migration import upgrade_to

PREVIOUS = "0027_receipt_unknowns"
HEAD = "0028_receipt_origins"
TABLE = "dividend_receipt_origins"
ACTION = "MIGRATE_DIVIDEND_RECEIPT_ORIGINS"


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
def scenario(engine: Engine, source: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    _, account = owned_account(engine)
    event = manual()
    submit(engine, event)
    entitlement = claim(event.revision_id, account, net=10_000_000)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
    return event, account, entitlement


def origin_values(
    con: Connection,
    scenario: tuple[Any, str, dict[str, Any]],
    *,
    currency: str = "USD",
    amount: int = 6_000_000,
    rate: int | None = 7_800_000_000_000,
    booked: int | None = 46_800_000,
    fx_source: str | None = "Synthetic historical rate",
    actual_hkd_reason: str | None = None,
    audit_changes: dict[str, Any] | None = None,
    **changes: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event, account, entitlement = scenario
    scope = con.exec_driver_sql(
        "SELECT a.portfolio_id,a.broker_id,a.version AS account_version,"
        "p.version AS portfolio_version,p.data_revision,b.version AS broker_version "
        "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
        "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
        (account,),
    ).one()
    receipt = {
        "id": str(uuid4()),
        "entitlement_id": entitlement["id"],
        "account_id": account,
        "received_date": "2026-01-09",
        "received_micro": amount,
        "currency": currency,
        "tax_micro": None,
        "fee_micro": 0,
        "fx_rate_hkd_e12": rate,
        "received_hkd_micro": booked,
        "created_at": stamp(),
    }
    insert(con, "dividend_receipts", receipt)
    cash = post_cash(
        con,
        account=account,
        day=receipt["received_date"],
        currency=currency,  # type: ignore[arg-type]
        amount=receipt["received_micro"],
        fx=rate,
        booked=booked,
        kind="DIVIDEND_RECEIPT",
        source_type="DIVIDEND_RECEIPT",
        source_id=receipt["id"],
    )
    request = str(uuid4())
    parameters: dict[str, Any] = {
        "owner_id": str(OWNER),
        "entity_type": "dividend_receipt",
        "entity_id": receipt["id"],
        "action": "DIVIDEND_RECEIPT_RECORDED",
        "request_id": request,
        "after": {"synthetic": "shape alone is not financial authority"},
    }
    parameters.update(audit_changes or {})
    audit(con, **parameters)
    row = con.exec_driver_sql(
        "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=? AND action=?",
        (parameters["entity_type"], parameters["entity_id"], parameters["action"]),
    ).one()
    return receipt, {
        "receipt_id": receipt["id"],
        "owner_id": str(OWNER),
        "event_id": str(event.event_id),
        "observed_revision_id": str(event.revision_id),
        "observed_revision_no": event.revision_no,
        "account_id": account,
        "portfolio_id": scope.portfolio_id,
        "broker_id": scope.broker_id,
        "account_version": scope.account_version,
        "portfolio_version": scope.portfolio_version,
        "broker_version": scope.broker_version,
        "portfolio_data_revision": scope.data_revision,
        "cash_entry_id": cash,
        "fx_source": fx_source,
        "actual_hkd_reason": actual_hkd_reason,
        "request_id": request,
        "audit_id": row.id,
        "created_at": row.created_at,
        **changes,
    }


def test_current_schema_starts_with_no_inferred_receipt_origins(engine: Engine) -> None:
    assert schema_head() == "0031_dividend_rebuild"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize(
    ("currency", "rate", "booked", "source_name", "override_reason"),
    [
        ("USD", None, None, None, None),
        ("USD", 7_800_000_000_000, 46_800_000, "Synthetic rate", None),
        ("USD", 7_800_000_000_000, 46_123_456, "Synthetic rate", REASON),
        ("HKD", 1_000_000_000_000, 6_000_000, "IDENTITY", None),
        ("HKD", 1_000_000_000_000, 6_000_000, "IDENTITY", REASON),
    ],
)
def test_explicit_rate_unknown_identity_and_override_are_retained_without_grant(
    engine: Engine,
    scenario: tuple[Any, str, dict[str, Any]],
    currency: str,
    rate: int | None,
    booked: int | None,
    source_name: str | None,
    override_reason: str | None,
) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        receipt, origin = origin_values(
            con,
            scenario,
            currency=currency,
            rate=rate,
            booked=booked,
            fx_source=source_name,
            actual_hkd_reason=override_reason,
        )
        insert(con, TABLE, origin)
    after = database_snapshot(engine)
    allowed = {TABLE, "dividend_receipts", "cash_ledger_entries", "audit_events"}
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert all(len(after[name]) == len(before[name]) + 1 for name in allowed)
    with unit_of_work(engine) as con:
        saved = con.exec_driver_sql(
            "SELECT r.tax_micro,r.fee_micro,r.fx_rate_hkd_e12,r.received_hkd_micro,"
            "o.fx_source,o.actual_hkd_reason FROM dividend_receipts r "
            "JOIN dividend_receipt_origins o ON o.receipt_id=r.id WHERE r.id=?",
            (receipt["id"],),
        ).one()
        assert tuple(saved) == (None, 0, rate, booked, source_name, override_reason)


def test_original_entitlement_revision_is_not_replaced_by_the_observed_head(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, second, account, older, _ = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        receipt, origin = origin_values(con, (second, str(account), older))
        insert(con, TABLE, origin)
        saved = con.exec_driver_sql(
            "SELECT entitlement.revision_id,origin.observed_revision_id "
            "FROM dividend_receipt_origins origin "
            "JOIN dividend_receipts receipt ON receipt.id=origin.receipt_id "
            "JOIN dividend_entitlements entitlement ON entitlement.id=receipt.entitlement_id "
            "WHERE receipt.id=?",
            (receipt["id"],),
        ).one()
        assert tuple(saved) == (str(first.revision_id), str(second.revision_id))


def test_origin_cannot_precede_its_recorded_entitlement(
    engine: Engine, source: dict[str, Any]
) -> None:
    _, account = owned_account(engine)
    event = manual()
    submit(engine, event)
    entitlement = claim(event.revision_id, account, net=10_000_000)
    entitlement["created_at"] = "9999-01-01T00:00:00.000000Z"
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, origin = origin_values(con, (event, account, entitlement))
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["account", "portfolio", "broker", "data_revision", "reversal"])
def test_scope_or_cash_change_after_capture_cannot_attach_origin(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], change: str
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        receipt, origin = origin_values(con, scenario)
        if change == "reversal":
            reverse_receipt(con, receipt)
        else:
            table, column, identifier = {
                "account": ("accounts", "version", origin["account_id"]),
                "portfolio": ("portfolios", "version", origin["portfolio_id"]),
                "broker": ("brokers", "version", origin["broker_id"]),
                "data_revision": ("portfolios", "data_revision", origin["portfolio_id"]),
            }[change]
            con.exec_driver_sql(f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (identifier,))
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_id": "invalid"},
        {"owner_id": str(OTHER)},
        {"event_id": str(uuid4())},
        {"observed_revision_id": str(uuid4())},
        {"observed_revision_no": 2},
        {"observed_revision_no": 0},
        {"account_id": str(uuid4())},
        {"portfolio_id": str(uuid4())},
        {"broker_id": str(uuid4())},
        {"account_version": 2},
        {"portfolio_version": 2},
        {"broker_version": 2},
        {"portfolio_data_revision": -1},
        {"portfolio_data_revision": 999},
        {"cash_entry_id": str(uuid4())},
        {"request_id": str(uuid4())},
        {"audit_id": str(uuid4())},
        {"created_at": "2026-01-01T00:00:00.000000Z"},
        {"created_at": "2026-02-30T00:00:00.000000Z"},
        {"fx_source": None},
        {"fx_source": ""},
        {"fx_source": " IDENTITY"},
        {"fx_source": "IDENTITY"},
        {"fx_source": "synthetic\x00secret"},
        {"fx_source": "x" * 101},
        {"actual_hkd_reason": "too short"},
        {"actual_hkd_reason": " synthetic reason with whitespace "},
        {"actual_hkd_reason": "synthetic\x00private reason"},
    ],
)
def test_invalid_origin_scope_rolls_back_receipt_cash_and_audit(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, origin = origin_values(con, scenario, **changes)
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("currency", "rate", "booked", "source_name", "override_reason"),
    [
        ("USD", None, None, "Synthetic rate", None),
        ("USD", None, None, None, REASON),
        ("USD", 7_800_000_000_000, None, "Synthetic rate", None),
        ("USD", None, 1, None, None),
        ("HKD", None, None, None, None),
        ("HKD", 1_000_000_000_000, 6_000_000, "Synthetic rate", None),
        ("HKD", 2_000_000_000_000, 6_000_000, "IDENTITY", None),
        ("HKD", 1_000_000_000_000, 6_100_000, "IDENTITY", REASON),
        ("USD", 7_800_000_000_000, -1, "Synthetic rate", REASON),
        ("USD", 7_800_000_000_000, -1, "Synthetic rate", None),
    ],
)
def test_inconsistent_rate_source_and_hkd_value_fail_closed(
    engine: Engine,
    scenario: tuple[Any, str, dict[str, Any]],
    currency: str,
    rate: int | None,
    booked: int | None,
    source_name: str | None,
    override_reason: str | None,
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, origin = origin_values(
            con,
            scenario,
            currency=currency,
            rate=rate,
            booked=booked,
            fx_source=source_name,
            actual_hkd_reason=override_reason,
        )
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("amount", "booked"),
    [
        (0, 0),
        (6_000_000, 0),
        (-6_000_000, 0),
        (-6_000_000, -46_800_000),
        (INT64_MAX, INT64_MAX),
        (-INT64_MAX, -INT64_MAX),
    ],
)
def test_matching_signed_or_zero_cash_can_retain_an_override(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], amount: int, booked: int
) -> None:
    with unit_of_work(engine, write=True) as con:
        _, origin = origin_values(
            con,
            scenario,
            amount=amount,
            rate=1_000_000_000_000,
            booked=booked,
            actual_hkd_reason=REASON,
        )
        insert(con, TABLE, origin)


def test_negative_native_cash_cannot_have_positive_hkd_origin(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, origin = origin_values(
            con, scenario, amount=-6_000_000, booked=1, actual_hkd_reason=REASON
        )
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("booked", [-1, 1])
def test_zero_native_cash_cannot_have_nonzero_hkd_origin(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], booked: int
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, origin = origin_values(con, scenario, amount=0, booked=booked, actual_hkd_reason=REASON)
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "audit_changes",
    [
        {"owner_id": str(OTHER)},
        {"entity_type": "other"},
        {"entity_id": str(uuid4())},
        {"action": "OTHER"},
        {"request_id": str(uuid4())},
        {"before": {"synthetic": "not a creation"}},
        {"after": None},
    ],
)
def test_creation_audit_must_match_receipt_owner_action_and_request(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], audit_changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, origin = origin_values(con, scenario, audit_changes=audit_changes)
        insert(con, TABLE, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_origin_cannot_be_mutated_deleted_or_replaced(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        _, origin = origin_values(con, scenario)
        insert(con, TABLE, origin)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "UPDATE":
            con.exec_driver_sql(
                f"UPDATE {TABLE} SET fx_source='Other' WHERE receipt_id=?", (origin["receipt_id"],)
            )
        elif operation == "DELETE":
            con.exec_driver_sql(f"DELETE FROM {TABLE} WHERE receipt_id=?", (origin["receipt_id"],))
        else:
            columns = ",".join(origin)
            marks = ",".join("?" for _ in origin)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES ({marks})",
                tuple(origin.values()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("column", ["cash_entry_id", "audit_id"])
def test_unique_cash_or_audit_cannot_be_replaced_by_another_receipt(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]], column: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        _, first = origin_values(con, scenario)
        insert(con, TABLE, first)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        _, second = origin_values(con, scenario, **{column: first[column]})
        columns = ",".join(second)
        marks = ",".join("?" for _ in second)
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES ({marks})",
            tuple(second.values()),
        )
    assert database_snapshot(engine) == before


def test_competing_origin_inserts_leave_exactly_one_original(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]]
) -> None:
    with unit_of_work(engine, write=True) as con:
        _, origin = origin_values(con, scenario)
    before = database_snapshot(engine)
    barrier = Barrier(2)

    def compete() -> bool:
        barrier.wait(timeout=15)
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, origin)
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: compete(), range(2))) == [False, True]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == 1
    assert {key: value for key, value in before.items() if key != TABLE} == {
        key: value for key, value in after.items() if key != TABLE
    }


def test_void_preserves_original_origin_cash_and_locked_rate(
    engine: Engine, scenario: tuple[Any, str, dict[str, Any]]
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt, origin = origin_values(con, scenario)
        insert(con, TABLE, origin)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        reverse_receipt(con, receipt)
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(), receipt["id"]),
        )
    after = database_snapshot(engine)
    assert after[TABLE] == before[TABLE]
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_type='DIVIDEND_RECEIPT' AND source_id=? ORDER BY entry_role",
            (receipt["id"],),
        ).all()
        assert [tuple(row) for row in rows] == [
            (6_000_000, 7_800_000_000_000, 46_800_000),
            (-6_000_000, 7_800_000_000_000, -46_800_000),
        ]


def test_upgrade_does_not_relabel_receipts_or_modify_cash_fx_or_audits(
    previous_engine: Engine,
) -> None:
    populated(previous_engine)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["dividend_receipts"] and before["cash_ledger_entries"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == set(before) | {TABLE} and after[TABLE] == []
    for table, rows in before.items():
        if table not in {"alembic_version", "audit_events"}:
            assert after[table] == rows
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


def test_cli_backup_preserves_original_unknowns_and_all_prior_facts(
    previous_engine: Engine, settings: Settings
) -> None:
    populated(previous_engine)
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


@pytest.mark.parametrize("after_first_audit", [False, True])
def test_failed_origin_migration_rolls_back_table_audits_and_all_facts(
    previous_engine: Engine, after_first_audit: bool
) -> None:
    populated(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one() >= 2
        condition = (
            f"AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')"
            if after_first_audit
            else ""
        )
        con.exec_driver_sql(f"""
            CREATE TRIGGER synthetic_receipt_origin_upgrade_abort BEFORE INSERT ON audit_events
            WHEN NEW.action='{ACTION}' {condition}
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_FAILURE'); END
        """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_downgrade_refuses_without_removing_provenance(previous_engine: Engine) -> None:
    populated(previous_engine)
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
