"""Read direct recorded receipts without treating audit shape as payment or eligibility."""

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_event_service as events
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.ledger_service import post_cash, reverse_cash
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_migration import ORIGINS, TABLE, receipt_values
from tests.test_dividend_direct_receipt_migration import scenario as scenario
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, owned_account, submit


def add_receipt(
    engine: Engine,
    scenario: tuple[Any, str],
    *,
    forged_audit: bool = False,
    override_reason: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    event, account = scenario
    value = receipt_values(event, account, **changes)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, value)
        scope = con.exec_driver_sql(
            "SELECT a.portfolio_id,a.broker_id,a.version,p.version,p.data_revision,b.version "
            "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
            "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account,),
        ).one()
        cash_id = post_cash(
            con,
            account=account,
            day=value["received_date"],
            currency=value["currency"],
            amount=value["received_micro"],
            fx=value["fx_rate_hkd_e12"],
            booked=value["received_hkd_micro"],
            kind="DIVIDEND_RECEIPT",
            source_type="DIVIDEND_DIRECT_RECEIPT",
            source_id=value["id"],
            note=value["note"],
        )
        cash = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (cash_id,))
            .mappings()
            .one()
        )
        receipt = (
            con.exec_driver_sql(f"SELECT * FROM {TABLE} WHERE id=?", (value["id"],))
            .mappings()
            .one()
        )
        at = now()
        origin: dict[str, Any] = {
            "receipt_id": value["id"],
            "owner_id": str(OWNER),
            "event_id": value["event_id"],
            "revision_id": value["revision_id"],
            "revision_no": event.revision_no,
            "account_id": account,
            "portfolio_id": scope[0],
            "broker_id": scope[1],
            "account_version": scope[2],
            "portfolio_version": scope[3],
            "portfolio_data_revision": scope[4],
            "broker_version": scope[5],
            "cash_entry_id": cash_id,
            "fx_source": (
                "IDENTITY"
                if value["currency"] == "HKD"
                else "Synthetic rate"
                if value["fx_rate_hkd_e12"] is not None
                else None
            ),
            "actual_hkd_reason": override_reason,
            "request_id": str(uuid4()),
            "created_at": stamp(at),
        }
        payload = direct._creation_payload(receipt, origin, cash, REASON)
        audit(
            con,
            str(OWNER),
            "dividend_direct_receipt",
            value["id"],
            "DIVIDEND_DIRECT_RECEIPT_RECORDED",
            request_id=origin["request_id"],
            after={"synthetic": "incorrect fingerprint"} if forged_audit else payload,
            reason=REASON,
            at=at,
        )
        origin["audit_id"] = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_direct_receipt' "
            "AND entity_id=? AND action='DIVIDEND_DIRECT_RECEIPT_RECORDED'",
            (value["id"],),
        ).scalar_one()
        insert(con, ORIGINS, origin)
    return {**value, "origin": origin, "payload": payload}


def read(engine: Engine, scenario: tuple[Any, str]) -> Any:
    event, account = scenario
    with unit_of_work(engine) as con:
        return direct.history(con, OWNER, event.event_id, UUID(account))


def test_owned_empty_history_is_not_a_confirmed_zero(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    before = database_snapshot(engine)
    assert read(engine, scenario) == ()
    assert database_snapshot(engine) == before
    event, account = scenario
    with unit_of_work(engine) as con:
        assert direct.history(con, OTHER, event.event_id, UUID(account)) is None
        assert direct.history(con, OWNER, event.event_id, uuid4()) is None


def test_matched_direct_receipt_has_exact_values_and_no_financial_grant(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    saved = add_receipt(
        engine,
        scenario,
        fx_rate_hkd_e12=7_800_000_000_000,
        received_hkd_micro=46_800_000,
    )
    before = database_snapshot(engine)
    values = read(engine, scenario)
    assert values is not None and len(values) == 1
    item = values[0]
    assert item.receipt.id == UUID(saved["id"])
    assert item.revision_id == scenario[0].revision_id and item.revision_no == 1
    assert str(item.receipt.received) == "6.000000"
    assert item.receipt.tax is None and str(item.receipt.fee) == "0.000000"
    assert str(item.receipt.fx_rate_hkd) == "7.800000000000"
    assert str(item.receipt.received_hkd) == "46.800000"
    assert item.receipt.cash_scope == "MATCHING_CASH_RECORDED_UNVERIFIED"
    assert item.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert item.history_completeness == "UNVERIFIED"
    assert item.eligibility_scope == "NOT_ESTABLISHED"
    assert item.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert "6.000000" not in repr(item) and "synthetic-private" not in repr(item)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("changes", "received", "hkd"),
    [
        ({"received_micro": 0}, "0.000000", None),
        ({"received_micro": 9_223_372_036_854_775_807}, "9223372036854.775807", None),
        ({"received_micro": -9_223_372_036_854_775_807}, "-9223372036854.775807", None),
        (
            {
                "currency": "HKD",
                "fx_rate_hkd_e12": 1_000_000_000_000,
                "received_hkd_micro": 6_000_000,
            },
            "6.000000",
            "6.000000",
        ),
        (
            {"fx_rate_hkd_e12": 7_800_000_000_000, "received_hkd_micro": 46_123_456},
            "6.000000",
            "46.123456",
        ),
    ],
)
def test_exact_zero_limits_identity_and_reasoned_actual_hkd_are_preserved(
    engine: Engine,
    scenario: tuple[Any, str],
    changes: dict[str, Any],
    received: str,
    hkd: str | None,
) -> None:
    override = REASON if changes.get("received_hkd_micro") == 46_123_456 else None
    add_receipt(engine, scenario, override_reason=override, **changes)
    values = read(engine, scenario)
    assert values is not None and len(values) == 1
    item = values[0]
    assert str(item.receipt.received) == received
    assert (
        str(item.receipt.received_hkd) if item.receipt.received_hkd is not None else None
    ) == hkd
    assert item.origin.actual_hkd_reason == override


def test_later_same_owner_account_move_and_archive_preserve_original_origin(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    _, account = scenario
    original = add_receipt(engine, scenario)
    portfolio, broker = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {"id": portfolio, "owner_id": str(OWNER), "name": portfolio, "created_at": stamp()},
        )
        insert(con, "brokers", {"id": broker, "owner_id": str(OWNER), "name": broker})
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,status='ARCHIVED',version=version+1 "
            "WHERE id=?",
            (portfolio, broker, account),
        )
    before = database_snapshot(engine)
    values = read(engine, scenario)
    assert values is not None and len(values) == 1
    assert values[0].origin.portfolio_id == UUID(original["origin"]["portfolio_id"])
    assert values[0].origin.broker_id == UUID(original["origin"]["broker_id"])
    assert values[0].origin.account_version == 1
    assert values[0].current_use_scope == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


def test_unsealed_direct_receipt_fails_closed_instead_of_disappearing(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(*scenario))
    before = database_snapshot(engine)
    with pytest.raises(
        direct.DirectReceiptHistoryError, match="^DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def test_shaped_but_wrong_creation_fingerprint_is_unavailable(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_receipt(engine, scenario, forged_audit=True)
    before = database_snapshot(engine)
    with pytest.raises(
        direct.DirectReceiptHistoryError, match="^DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def test_cash_and_fx_shape_are_not_a_substitute_for_exact_arithmetic(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_receipt(
        engine,
        scenario,
        fx_rate_hkd_e12=7_800_000_000_000,
        received_hkd_micro=1,
    )
    before = database_snapshot(engine)
    with pytest.raises(
        direct.DirectReceiptHistoryError, match="^DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def test_later_duplicate_creation_audit_is_not_silently_ignored(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = add_receipt(engine, scenario)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_direct_receipt",
            value["id"],
            "DIVIDEND_DIRECT_RECEIPT_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "late duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(
        direct.DirectReceiptHistoryError, match="^DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def void_receipt(engine: Engine, value: dict[str, Any], *, forged_audit: bool = False) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = (
            con.exec_driver_sql(f"SELECT * FROM {TABLE} WHERE id=?", (value["id"],))
            .mappings()
            .one()
        )
        original = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE id=?", (value["origin"]["cash_entry_id"],)
            )
            .mappings()
            .one()
        )
        reversal_id = reverse_cash(con, original, reason=REASON)
        reversal = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (reversal_id,))
            .mappings()
            .one()
        )
        at, request = now(), str(uuid4())
        payload = direct._void_payload(receipt, reversal, request, REASON, stamp(at))
        audit(
            con,
            str(OWNER),
            "dividend_direct_receipt",
            value["id"],
            "DIVIDEND_DIRECT_RECEIPT_VOIDED",
            request_id=request,
            before=value["payload"],
            after={"synthetic": "incorrect void fingerprint"} if forged_audit else payload,
            reason=REASON,
            at=at,
        )
        con.exec_driver_sql(
            f"UPDATE {TABLE} SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(at), value["id"]),
        )


def test_audited_void_retains_original_cash_and_exact_reversal(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = add_receipt(engine, scenario)
    void_receipt(engine, value)
    before = database_snapshot(engine)
    values = read(engine, scenario)
    assert values is not None and len(values) == 1
    assert values[0].receipt.status == "VOID" and values[0].receipt.version == 2
    assert values[0].receipt.cash_scope == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert values[0].audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert database_snapshot(engine) == before


def test_different_event_and_account_histories_do_not_mix(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    _, second_account = owned_account(engine)
    other_event = manual()
    submit(engine, other_event)
    add_receipt(engine, scenario)
    add_receipt(engine, (event, second_account), received_micro=2_000_000)
    add_receipt(engine, (other_event, account), received_micro=3_000_000)
    first = read(engine, scenario)
    second = read(engine, (event, second_account))
    third = read(engine, (other_event, account))
    assert first and second and third
    assert [str(items[0].receipt.received) for items in (first, second, third)] == [
        "6.000000",
        "2.000000",
        "3.000000",
    ]


def test_invalid_scope_requires_a_transaction_and_uuids(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with (
        engine.connect() as con,
        pytest.raises(direct.DirectReceiptHistoryError, match="^INVALID_DIRECT_RECEIPT_SCOPE$"),
    ):
        direct.history(con, OWNER, event.event_id, UUID(account))
    with (
        unit_of_work(engine) as con,
        pytest.raises(direct.DirectReceiptHistoryError, match="^INVALID_DIRECT_RECEIPT_SCOPE$"),
    ):
        direct.history(con, OWNER, event.event_id, "not-a-uuid")  # type: ignore[arg-type]


def test_one_wal_snapshot_does_not_mix_a_later_direct_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    original_get = events.get
    appended = False

    def concurrent_append(*args: Any, **kwargs: Any) -> Any:
        nonlocal appended
        result = original_get(*args, **kwargs)
        if not appended:
            appended = True
            add_receipt(engine, scenario)
        return result

    with patch.object(events, "get", side_effect=concurrent_append):
        assert read(engine, scenario) == ()
    result = read(engine, scenario)
    assert result is not None and len(result) == 1


@pytest.mark.parametrize(
    "kind",
    ["wrong_void_digest", "duplicate_void", "unreversed_void_audit"],
)
def test_inconsistent_void_audits_never_become_valid_history(
    engine: Engine, scenario: tuple[Any, str], kind: str
) -> None:
    value = add_receipt(engine, scenario)
    if kind != "unreversed_void_audit":
        void_receipt(engine, value, forged_audit=kind == "wrong_void_digest")
    if kind != "wrong_void_digest":
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                str(OWNER),
                "dividend_direct_receipt",
                value["id"],
                "DIVIDEND_DIRECT_RECEIPT_VOIDED",
                request_id=str(uuid4()),
                before={"synthetic": "unexpected"},
                after={"synthetic": "unexpected"},
                reason=REASON,
            )
    before = database_snapshot(engine)
    with pytest.raises(
        direct.DirectReceiptHistoryError, match="^DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("target", "changes"),
    [
        ("receipt", {"event_id": str(uuid4())}),
        ("receipt", {"revision_id": str(uuid4())}),
        ("receipt", {"created_at": "2000-01-01T00:00:00.000000Z"}),
        ("receipt", {"note": "synthetic-mismatched note"}),
        ("origin", {"receipt_id": str(uuid4())}),
        ("origin", {"event_id": str(uuid4())}),
        ("origin", {"revision_id": str(uuid4())}),
        ("origin", {"revision_no": 2}),
        ("origin", {"account_id": str(uuid4())}),
        ("origin", {"owner_id": str(OTHER)}),
        ("origin", {"retained_portfolio_owner": str(OTHER)}),
        ("origin", {"retained_broker_owner": str(OTHER)}),
        ("origin", {"account_version": 999}),
        ("origin", {"portfolio_version": 999}),
        ("origin", {"broker_version": 999}),
        ("origin", {"portfolio_data_revision": 999}),
        ("origin", {"created_at": "2000-01-01T00:00:00.000000Z"}),
        ("origin", {"audit_id": str(uuid4())}),
        ("cash", {"source_type": "OTHER"}),
        ("cash", {"note": "synthetic-mismatched note"}),
        ("legacy_collision", {}),
        ("origin_scope_mismatch", {}),
    ],
)
def test_corrupt_retained_scope_or_cross_table_identity_fails_closed(
    engine: Engine, scenario: tuple[Any, str], target: str, changes: dict[str, Any]
) -> None:
    add_receipt(engine, scenario)
    before = database_snapshot(engine)
    event, account = scenario
    with unit_of_work(engine) as con:
        execute = con.execute

        def corrupt(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = execute(statement, *args, **kwargs)
            sql = str(statement)
            if target == "receipt" and "FROM dividend_direct_receipts receipt" in sql:
                rows = [dict(row) for row in result.mappings().all()]
                assert len(rows) == 1
                rows[0].update(changes)
                fake = MagicMock()
                fake.mappings.return_value.all.return_value = rows
                return fake
            if target == "origin" and "SELECT o.*,p.owner_id" in sql:
                row = dict(result.mappings().one())
                row.update(changes)
                fake = MagicMock()
                fake.mappings.return_value.one.return_value = row
                return fake
            if target == "cash" and "SELECT * FROM cash_ledger_entries WHERE id=" in sql:
                row = dict(result.mappings().one())
                row.update(changes)
                fake = MagicMock()
                fake.mappings.return_value.one.return_value = row
                return fake
            if (
                target == "origin_scope_mismatch"
                and "SELECT 1 FROM dividend_direct_receipt_origins o" in sql
            ):
                fake = MagicMock()
                fake.first.return_value = (1,)
                return fake
            if target == "legacy_collision" and "SELECT 1 FROM dividend_receipts WHERE id=" in sql:
                fake = MagicMock()
                fake.first.return_value = (1,)
                return fake
            return result

        with (
            patch.object(con, "execute", side_effect=corrupt),
            pytest.raises(
                direct.DirectReceiptHistoryError, match="^DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
            ),
        ):
            direct.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before
