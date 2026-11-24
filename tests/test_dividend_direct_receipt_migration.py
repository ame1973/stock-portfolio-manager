"""Direct receipt storage must not invent a legacy entitlement or hide recorded cash."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_receipt_history as receipt_history
from spm_api.audit import audit, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import schema_head, unit_of_work
from spm_api.ledger_service import post_cash, reverse_cash
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT, manual, next_intent, owned_account, submit
from tests.test_dividend_receipt_history import claim
from tests.test_dividend_rejection_migration import rejection_values
from tests.test_schedule_migration import upgrade_to

PREVIOUS = "0029_receipt_fx_guard"
HEAD = "0030_direct_receipts"
TABLE = "dividend_direct_receipts"
ORIGINS = "dividend_direct_receipt_origins"


@pytest.fixture
def scenario(engine: Engine, source: dict[str, Any]) -> tuple[Any, str]:
    _, account = owned_account(engine)
    event = manual()
    submit(engine, event)
    return event, account


def receipt_values(event: Any, account: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "event_id": str(event.event_id),
        "revision_id": str(event.revision_id),
        "account_id": account,
        "received_date": "2026-01-09",
        "received_micro": 6_000_000,
        "currency": "USD",
        "tax_micro": None,
        "fee_micro": 0,
        "fx_rate_hkd_e12": None,
        "received_hkd_micro": None,
        "note": "synthetic-private receipt",
        "created_at": stamp(),
        **changes,
    }


def seal(con: Connection, receipt: dict[str, Any], *, request: str | None = None) -> dict[str, Any]:
    scope = con.exec_driver_sql(
        "SELECT a.portfolio_id,a.broker_id,a.version,p.version,p.data_revision,b.version "
        "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
        "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
        (receipt["account_id"],),
    ).one()
    cash = post_cash(
        con,
        account=receipt["account_id"],
        day=receipt["received_date"],
        currency=receipt["currency"],
        amount=receipt["received_micro"],
        fx=receipt["fx_rate_hkd_e12"],
        booked=receipt["received_hkd_micro"],
        kind="DIVIDEND_RECEIPT",
        source_type="DIVIDEND_DIRECT_RECEIPT",
        source_id=receipt["id"],
        note=receipt["note"],
    )
    request = request or str(uuid4())
    audit(
        con,
        str(OWNER),
        "dividend_direct_receipt",
        receipt["id"],
        "DIVIDEND_DIRECT_RECEIPT_RECORDED",
        request_id=request,
        after={"synthetic": "shape is not authentication"},
        reason=REASON,
    )
    recorded = con.exec_driver_sql(
        "SELECT id,created_at FROM audit_events WHERE entity_type='dividend_direct_receipt' "
        "AND entity_id=? AND action='DIVIDEND_DIRECT_RECEIPT_RECORDED'",
        (receipt["id"],),
    ).one()
    return {
        "receipt_id": receipt["id"],
        "owner_id": str(OWNER),
        "event_id": receipt["event_id"],
        "revision_id": receipt["revision_id"],
        "revision_no": con.exec_driver_sql(
            "SELECT revision_no FROM dividend_event_revisions WHERE id=?", (receipt["revision_id"],)
        ).scalar_one(),
        "account_id": receipt["account_id"],
        "portfolio_id": scope[0],
        "broker_id": scope[1],
        "account_version": scope[2],
        "portfolio_version": scope[3],
        "portfolio_data_revision": scope[4],
        "broker_version": scope[5],
        "cash_entry_id": cash,
        "fx_source": (
            "IDENTITY"
            if receipt["currency"] == "HKD"
            else "Synthetic historical rate"
            if receipt["fx_rate_hkd_e12"] is not None
            else None
        ),
        "actual_hkd_reason": None,
        "request_id": request,
        "audit_id": recorded.id,
        "created_at": recorded.created_at,
    }


def test_upgrade_adds_empty_tables_without_adopting_existing_receipts(old_engine: Engine) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before = database_snapshot(old_engine)
    upgrade_to(old_engine, HEAD)
    after = database_snapshot(old_engine)
    assert schema_head() == HEAD
    assert after[TABLE] == [] and after[ORIGINS] == []
    for name, rows in before.items():
        if name not in {"alembic_version", "audit_events"}:
            assert after[name] == rows
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action='MIGRATE_DIRECT_DIVIDEND_RECEIPTS'"
        ).scalar_one() == len(before["owners"])
    upgrade_to(old_engine, HEAD)
    assert database_snapshot(old_engine) == after


def test_failed_migration_rolls_back_all_ddl_and_data(old_engine: Engine) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before, definitions = database_snapshot(old_engine), schema(old_engine)

    def fail_during_ddl(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "CREATE TABLE dividend_direct_receipt_origins" in statement:
            raise RuntimeError("synthetic migration failure")

    sqlalchemy_event.listen(old_engine, "before_cursor_execute", fail_during_ddl)
    try:
        with pytest.raises(RuntimeError, match="synthetic migration failure"):
            upgrade_to(old_engine, HEAD)
    finally:
        sqlalchemy_event.remove(old_engine, "before_cursor_execute", fail_during_ddl)
    assert database_snapshot(old_engine) == before
    assert schema(old_engine) == definitions
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_cli_keeps_a_private_pre_upgrade_backup(old_engine: Engine, settings: Settings) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before = database_snapshot(old_engine)
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
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert con.exec_driver_sql(f"SELECT count(*) FROM {ORIGINS}").scalar_one() == 0


def test_cash_can_be_linked_to_event_without_an_entitlement_or_projection(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    value = receipt_values(event, account)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
        origin = seal(con, value)
        insert(con, ORIGINS, origin)
    after = database_snapshot(engine)
    assert after["dividend_entitlements"] == before["dividend_entitlements"]
    assert after["dividend_projection_versions"] == before["dividend_projection_versions"]
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after[ORIGINS]) == len(before[ORIGINS]) + 1
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            f"SELECT tax_micro,fee_micro,fx_rate_hkd_e12,received_hkd_micro "
            f"FROM {TABLE} WHERE id=?",
            (value["id"],),
        ).one()
        assert tuple(row) == (None, 0, None, None)
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"event_id": str(uuid4())},
        {"revision_id": str(uuid4())},
        {"account_id": str(uuid4())},
        {"received_date": "2026-02-30"},
        {"received_micro": -9_223_372_036_854_775_808},
        {"tax_micro": -1},
        {"fee_micro": -1},
        {"status": "VOID"},
        {"version": 2},
        {"currency": "HKD"},
        {"currency": "HKD", "fx_rate_hkd_e12": 1_000_000_000_000, "received_hkd_micro": 1},
        {"fx_rate_hkd_e12": 7_800_000_000_000},
        {"fx_rate_hkd_e12": 7_800_000_000_000, "received_hkd_micro": -1},
    ],
)
def test_invalid_receipt_facts_fail_without_writes(
    engine: Engine, scenario: tuple[Any, str], changes: dict[str, Any]
) -> None:
    value = receipt_values(*scenario, **changes)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
    assert database_snapshot(engine) == before


def test_foreign_account_and_cross_event_revision_are_rejected(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    _, foreign = owned_account(engine, OTHER)
    other = manual()
    submit(engine, other)
    for changes in ({"account_id": foreign}, {"revision_id": str(other.revision_id)}):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            insert(con, TABLE, receipt_values(event, account, **changes))


def test_direct_cash_guard_requires_matching_receipt_fields(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = receipt_values(*scenario)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        post_cash(
            con,
            account=value["account_id"],
            day=value["received_date"],
            currency="USD",
            amount=value["received_micro"] + 1,
            fx=None,
            booked=None,
            kind="DIVIDEND_RECEIPT",
            source_type="DIVIDEND_DIRECT_RECEIPT",
            source_id=value["id"],
        )
    assert database_snapshot(engine) == before


def test_new_history_fences_revision_and_old_receipt_reader_fails_closed(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(event, account))
        assert event_writer._has_financial_history(con, event.event_id)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        submit(engine, next_intent(event, "VERIFY"), at=AT)
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            receipt_history.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"
        ),
    ):
        receipt_history.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_missing_direct_storage_never_turns_into_an_empty_history_or_safe_transition(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def unavailable(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "FROM dividend_direct_receipts" in str(statement):
                raise SQLAlchemyError("synthetic unavailable storage")
            return execute(statement, *args, **kwargs)

        with patch.object(con, "execute", side_effect=unavailable):
            with pytest.raises(SQLAlchemyError, match="synthetic unavailable storage"):
                event_writer.save(
                    con, OWNER, next_intent(event, "VERIFY"), at=AT, request_id=uuid4()
                )
            with pytest.raises(
                receipt_history.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"
            ):
                receipt_history.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_receipt_cannot_be_replaced_deleted_or_voided_without_audited_reversal(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = receipt_values(*scenario)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
        origin = seal(con, value)
        insert(con, ORIGINS, origin)
    before = database_snapshot(engine)
    for sql in (
        f"DELETE FROM {TABLE} WHERE id=?",
        f"UPDATE {TABLE} SET received_micro=1,version=2 WHERE id=?",
        f"UPDATE {TABLE} SET status='VOID',version=2,voided_at=created_at WHERE id=?",
        f"DELETE FROM {ORIGINS} WHERE receipt_id=?",
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(sql, (value["id"],))
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        cash = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE id=?", (origin["cash_entry_id"],)
            )
            .mappings()
            .one()
        )
        reverse_cash(con, cash, reason=REASON)
        audit(
            con,
            str(OWNER),
            "dividend_direct_receipt",
            value["id"],
            "DIVIDEND_DIRECT_RECEIPT_VOIDED",
            request_id=str(uuid4()),
            before={"synthetic": "original"},
            after={"synthetic": "void"},
            reason=REASON,
        )
        con.exec_driver_sql(
            f"UPDATE {TABLE} SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(), value["id"]),
        )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            f"SELECT status,version,received_micro FROM {TABLE} WHERE id=?", (value["id"],)
        ).one() == ("VOID", 2, 6_000_000)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": str(OTHER)},
        {"event_id": str(uuid4())},
        {"revision_id": str(uuid4())},
        {"revision_no": 2},
        {"revision_no": 0},
        {"account_id": str(uuid4())},
        {"portfolio_id": str(uuid4())},
        {"broker_id": str(uuid4())},
        {"account_version": 2},
        {"portfolio_version": 2},
        {"broker_version": 2},
        {"portfolio_data_revision": -1},
        {"portfolio_data_revision": 123},
        {"cash_entry_id": str(uuid4())},
        {"request_id": str(uuid4())},
        {"audit_id": str(uuid4())},
        {"created_at": "2026-01-01T00:00:00.000000Z"},
        {"created_at": "2026-02-30T00:00:00.000000Z"},
        {"fx_source": "Synthetic rate without FX"},
        {"actual_hkd_reason": REASON},
        {"actual_hkd_reason": "too short"},
    ],
)
def test_invalid_origin_rolls_back_receipt_cash_and_audit(
    engine: Engine, scenario: tuple[Any, str], changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        value = receipt_values(*scenario)
        insert(con, TABLE, value)
        origin = seal(con, value)
        insert(con, ORIGINS, {**origin, **changes})
    assert database_snapshot(engine) == before


def test_origin_cannot_bind_another_owned_event_or_account(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    _, second_account = owned_account(engine)
    second_event = manual()
    submit(engine, second_event)
    before = database_snapshot(engine)
    for changes in (
        {"account_id": second_account},
        {"event_id": str(second_event.event_id), "revision_id": str(second_event.revision_id)},
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            value = receipt_values(*scenario)
            insert(con, TABLE, value)
            insert(con, ORIGINS, {**seal(con, value), **changes})
        assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["account", "portfolio", "broker", "data", "reversal"])
def test_origin_rechecks_current_scope_and_cash_before_sealing(
    engine: Engine, scenario: tuple[Any, str], change: str
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        value = receipt_values(*scenario)
        insert(con, TABLE, value)
        origin = seal(con, value)
        if change == "reversal":
            cash = (
                con.exec_driver_sql(
                    "SELECT * FROM cash_ledger_entries WHERE id=?", (origin["cash_entry_id"],)
                )
                .mappings()
                .one()
            )
            reverse_cash(con, cash, reason=REASON)
        else:
            table, column, identifier = {
                "account": ("accounts", "version", value["account_id"]),
                "portfolio": ("portfolios", "version", origin["portfolio_id"]),
                "broker": ("brokers", "version", origin["broker_id"]),
                "data": ("portfolios", "data_revision", origin["portfolio_id"]),
            }[change]
            con.exec_driver_sql(f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (identifier,))
        insert(con, ORIGINS, origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("currency", "amount", "rate", "booked"),
    [
        ("USD", 0, None, None),
        ("USD", 6_000_000, 7_800_000_000_000, 46_800_000),
        ("USD", -6_000_000, 7_800_000_000_000, -46_800_000),
        ("HKD", 6_000_000, 1_000_000_000_000, 6_000_000),
        ("HKD", 0, 1_000_000_000_000, 0),
    ],
)
def test_direct_receipts_preserve_explicit_fx_and_signs(
    engine: Engine,
    scenario: tuple[Any, str],
    currency: str,
    amount: int,
    rate: int | None,
    booked: int | None,
) -> None:
    value = receipt_values(
        *scenario,
        currency=currency,
        received_micro=amount,
        fx_rate_hkd_e12=rate,
        received_hkd_micro=booked,
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
        origin = seal(con, value)
        insert(con, ORIGINS, origin)
        recorded = con.exec_driver_sql(
            f"SELECT received_micro,fx_rate_hkd_e12,received_hkd_micro FROM {TABLE} WHERE id=?",
            (value["id"],),
        ).one()
        assert tuple(recorded) == (amount, rate, booked)


def test_direct_and_legacy_receipt_ids_never_overlap(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    entitlement = claim(event.revision_id, account, net=10_000_000)
    direct = receipt_values(event, account)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
        insert(con, TABLE, direct)
    before = database_snapshot(engine)
    legacy = {
        "id": direct["id"],
        "entitlement_id": entitlement["id"],
        "account_id": account,
        "received_date": direct["received_date"],
        "received_micro": direct["received_micro"],
        "currency": "USD",
        "tax_micro": None,
        "fee_micro": None,
        "created_at": stamp(),
    }
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_receipts", legacy)
    assert database_snapshot(engine) == before


def test_direct_receipt_sql_fences_event_and_rejection_even_without_an_origin(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(event, account))
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="DIRECT_RECEIPT_RECONCILIATION_REQUIRED"),
        unit_of_work(engine, write=True) as con,
    ):
        con.exec_driver_sql(
            "UPDATE dividend_events SET current_revision_id=? WHERE id=?",
            (str(uuid4()), str(event.event_id)),
        )
    assert database_snapshot(engine) == before
    with (
        pytest.raises(IntegrityError, match="DIRECT_RECEIPT_RECONCILIATION_REQUIRED"),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, "dividend_event_rejections", rejection_values(con, event))
    assert database_snapshot(engine) == before


def test_direct_receipt_history_fence_is_scoped_to_event_and_account(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    _, other_account = owned_account(engine)
    other_event = manual()
    submit(engine, other_event)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(event, account))
    with unit_of_work(engine) as con:
        assert receipt_history.history(con, OWNER, event.event_id, UUID(other_account)) is not None
        assert receipt_history.history(con, OWNER, other_event.event_id, UUID(account)) is not None
        assert receipt_history.history(con, OTHER, event.event_id, UUID(account)) is None


def test_existing_legacy_receipt_id_cannot_be_reused_by_direct_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    entitlement = claim(event.revision_id, account, net=10_000_000)
    direct = receipt_values(event, account)
    legacy = {
        "id": direct["id"],
        "entitlement_id": entitlement["id"],
        "account_id": account,
        "received_date": direct["received_date"],
        "received_micro": direct["received_micro"],
        "currency": "USD",
        "tax_micro": None,
        "fee_micro": None,
        "created_at": stamp(),
    }
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
        insert(con, "dividend_receipts", legacy)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, direct)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("target", [TABLE, ORIGINS])
def test_replace_cannot_erase_receipt_or_origin(
    engine: Engine, scenario: tuple[Any, str], target: str
) -> None:
    value = receipt_values(*scenario)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
        origin = seal(con, value)
        insert(con, ORIGINS, origin)
    before = database_snapshot(engine)
    fields = value if target == TABLE else origin
    columns, marks = ",".join(fields), ",".join("?" for _ in fields)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO {target} ({columns}) VALUES ({marks})", tuple(fields.values())
        )
    assert database_snapshot(engine) == before


def test_direct_origin_request_cannot_be_reused_for_another_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    first = receipt_values(*scenario)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, first)
        origin = seal(con, first)
        insert(con, ORIGINS, origin)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        second = receipt_values(*scenario)
        insert(con, TABLE, second)
        insert(con, ORIGINS, seal(con, second, request=origin["request_id"]))
    assert database_snapshot(engine) == before


def test_void_audit_cannot_predate_original_or_reversal(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = receipt_values(*scenario)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
        origin = seal(con, value)
        insert(con, ORIGINS, origin)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        cash = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE id=?", (origin["cash_entry_id"],)
            )
            .mappings()
            .one()
        )
        reverse_cash(con, cash, reason=REASON)
        audit(
            con,
            str(OWNER),
            "dividend_direct_receipt",
            value["id"],
            "DIVIDEND_DIRECT_RECEIPT_VOIDED",
            request_id=str(uuid4()),
            before={"synthetic": "original"},
            after={"synthetic": "void"},
            reason=REASON,
            at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        con.exec_driver_sql(
            f"UPDATE {TABLE} SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(), value["id"]),
        )
    assert database_snapshot(engine) == before


def test_archived_account_cannot_start_a_new_direct_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?", (account,)
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(event, account))
    assert database_snapshot(engine) == before


def test_competing_receipts_with_one_request_commit_only_one_cash_fact(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    before = database_snapshot(engine)
    barrier, request = Barrier(2), str(uuid4())

    def attempt() -> bool:
        barrier.wait(timeout=15)
        try:
            with unit_of_work(engine, write=True) as con:
                receipt = receipt_values(*scenario)
                insert(con, TABLE, receipt)
                insert(con, ORIGINS, seal(con, receipt, request=request))
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == [False, True]
    after = database_snapshot(engine)
    changed = {TABLE, ORIGINS, "cash_ledger_entries", "audit_events"}
    assert {key: value for key, value in after.items() if key not in changed} == {
        key: value for key, value in before.items() if key not in changed
    }
    assert all(len(after[key]) == len(before[key]) + 1 for key in changed)


def test_competing_receipt_and_event_revision_cannot_both_commit(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    successor = next_intent(event, "VERIFY")
    barrier = Barrier(2)
    before = database_snapshot(engine)

    def post() -> bool:
        barrier.wait(timeout=15)
        try:
            with unit_of_work(engine, write=True) as con:
                value = receipt_values(event, account)
                insert(con, TABLE, value)
                insert(con, ORIGINS, seal(con, value))
            return True
        except IntegrityError:
            return False

    def revise() -> bool:
        barrier.wait(timeout=15)
        try:
            submit(engine, successor, at=AT + timedelta(seconds=1))
            return True
        except events.EventError as error:
            assert str(error) == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        posted, revised = pool.submit(post), pool.submit(revise)
        did_post, did_revise = posted.result(timeout=30), revised.result(timeout=30)
    assert did_post != did_revise
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + int(did_post)
    assert len(after[ORIGINS]) == len(before[ORIGINS]) + int(did_post)
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + int(did_post)
    with unit_of_work(engine) as con:
        current = events.get(con, OWNER, event.event_id)
    assert current is not None
    assert current.record.revision_id == (
        successor.revision_id if did_revise else event.revision_id
    )
