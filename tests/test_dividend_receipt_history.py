"""Retained receipt links must preserve stable event identity without granting income."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, RowMapping

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_history as history
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.ledger_service import post_cash
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OTHER, OWNER, add_posted_receipt, reverse_receipt
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT, manual, next_intent, owned_account, submit


def claim(revision_id: UUID, account: str, *, net: int) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "revision_id": str(revision_id),
        "account_id": account,
        "eligible_shares_e8": 1_000_000_000,
        "gross_micro": net,
        "expected_net_micro": net,
        "currency": "USD",
        "quality": "ACCRUED",
        "projection_version": 0,
        "created_at": "2026-11-09T12:00:03.000000Z",
    }


def prepared(engine: Engine) -> tuple[Any, Any, UUID, dict[str, Any], dict[str, Any]]:
    _, account = owned_account(engine)
    first = manual()
    submit(engine, first)
    second = next_intent(first, "VERIFY")
    submit(engine, second, at=AT + timedelta(seconds=1))
    older, newer = (
        claim(first.revision_id, account, net=10_000_000),
        claim(second.revision_id, account, net=11_000_000),
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", older)
        insert(con, "dividend_entitlements", newer)
    return first, second, UUID(account), older, newer


def test_receipt_links_follow_historical_entitlements_to_one_stable_event(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, second, account, older, newer = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        receipt = add_posted_receipt(con, older, fx=7_800_000_000_000)
        missing_cash = {
            "id": str(uuid4()),
            "entitlement_id": newer["id"],
            "account_id": str(account),
            "received_date": "2026-01-10",
            "received_micro": 2_000_000,
            "currency": "USD",
            "created_at": "2026-11-09T12:00:03.000000Z",
        }
        insert(con, "dividend_receipts", missing_cash)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = history.history(con, OWNER, first.event_id, account)
    assert result is not None and result.event_id == first.event_id and result.account_id == account
    assert result.current_revision_id == second.revision_id
    assert result.history_completeness == "UNVERIFIED"
    assert result.scope == "UNVERIFIED_RECEIPT_HISTORY_ONLY"
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert [item.revision_no for item in result.entitlements] == [1, 2]
    old, new = result.entitlements
    assert old.id == UUID(older["id"]) and old.revision_id == first.revision_id
    assert old.recorded_quality == "ACCRUED" and old.scope == "LEGACY_ENTITLEMENT_NOT_VERIFIED"
    assert new.id == UUID(newer["id"]) and new.revision_id == second.revision_id
    assert old.expected_net == Decimal("10.000000") and new.expected_net == Decimal("11.000000")
    assert old.receipts[0].id == UUID(receipt["id"])
    assert old.receipts[0].received == Decimal("6.000000")
    assert old.receipts[0].fx_rate_hkd == Decimal("7.800000000000")
    assert old.receipts[0].cash_scope == "MATCHING_CASH_RECORDED_UNVERIFIED"
    assert "2026-01-09" not in repr(old.receipts[0]) and "6.000000" not in repr(old.receipts[0])
    assert new.receipts[0].cash_scope == "CASH_NOT_RECORDED"
    assert "6.000000" not in repr(result) and "7.800000000000" not in repr(result)
    assert database_snapshot(engine) == before


def test_voided_receipt_keeps_original_and_exact_reversal_link_without_a_grant(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, older, _ = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        receipt = add_posted_receipt(con, older, fx=7_800_000_000_000)
        reverse_receipt(con, receipt)
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(), receipt["id"]),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = history.history(con, OWNER, first.event_id, account)
    assert result is not None
    saved = result.entitlements[0].receipts[0]
    assert saved.status == "VOID" and saved.received == Decimal("6.000000")
    assert saved.cash_scope == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert database_snapshot(engine) == before


def test_active_receipt_with_recorded_cash_reversal_is_unavailable(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, older, _ = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        receipt = add_posted_receipt(con, older, fx=7_800_000_000_000)
        reverse_receipt(con, receipt)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(history.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        history.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before


def mocked_cash(
    *,
    amount: object = 6_000_000,
    linked: list[dict[str, Any]] | None = None,
    primary_changes: dict[str, Any] | None = None,
    receipt_changes: dict[str, Any] | None = None,
    reversal_changes: dict[str, Any] | None = None,
) -> tuple[Connection, RowMapping]:
    receipt_id, account_id, cash_id = (str(uuid4()) for _ in range(3))
    receipt = {
        "id": receipt_id,
        "account_id": account_id,
        "received_date": "2026-01-09",
        "received_micro": 6_000_000,
        "currency": "USD",
        "fx_rate_hkd_e12": None,
        "received_hkd_micro": None,
        "status": "ACTIVE",
    }
    primary = {
        "id": cash_id,
        "account_id": account_id,
        "effective_date": "2026-01-09",
        "currency": "USD",
        "amount_micro": amount,
        "fx_rate_hkd_e12": None,
        "booked_hkd_micro": None,
        "kind": "DIVIDEND_RECEIPT",
        "source_type": "DIVIDEND_RECEIPT",
        "source_id": receipt_id,
        "entry_role": "PRIMARY",
        "external_flow": 0,
        "created_at": "2026-11-09T12:00:00.000000Z",
        "reverses_entry_id": None,
    }
    primary.update(primary_changes or {})
    receipt.update(receipt_changes or {})
    rows = [primary]
    if reversal_changes is not None:
        reversal = {
            **primary,
            "id": str(uuid4()),
            "amount_micro": -6_000_000,
            "entry_role": "REVERSAL",
            "kind": "REVERSAL",
            "reverses_entry_id": cash_id,
            **reversal_changes,
        }
        rows.append(reversal)
        receipt["status"] = "VOID"
        if linked is None:
            linked = [reversal]
    first = MagicMock()
    first.mappings.return_value.all.return_value = rows
    second = MagicMock()
    second.mappings.return_value.all.return_value = linked or []
    con = MagicMock(spec=Connection)
    con.execute.side_effect = [first, second]
    return cast(Connection, con), cast(RowMapping, receipt)


def test_matching_cash_does_not_hide_a_foreign_source_reversal() -> None:
    stray = {"id": str(uuid4()), "source_type": "OTHER", "source_id": str(uuid4())}
    con, receipt = mocked_cash(linked=[stray])
    with pytest.raises(ValueError):
        history._cash_scope(con, receipt)


def test_cash_numeric_affinity_cannot_turn_a_real_into_an_exact_integer() -> None:
    con, receipt = mocked_cash(amount=6_000_000.0)
    with pytest.raises(ValueError):
        history._cash_scope(con, receipt)


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_role": "OTHER"},
        {"source_type": "OTHER"},
        {"source_id": str(uuid4())},
        {"account_id": str(uuid4())},
        {"effective_date": "2026-01-10"},
        {"currency": "HKD"},
        {"amount_micro": 1},
        {"amount_micro": True},
        {"fx_rate_hkd_e12": 1.0},
        {"booked_hkd_micro": 0.0},
        {"kind": "ADJUSTMENT"},
        {"external_flow": 1},
        {"external_flow": False},
        {"reverses_entry_id": str(uuid4())},
    ],
)
def test_inconsistent_primary_cash_fails_closed(changes: dict[str, Any]) -> None:
    con, receipt = mocked_cash(primary_changes=changes)
    with pytest.raises(ValueError):
        history._cash_scope(con, receipt)


@pytest.mark.parametrize(
    "changes",
    [
        {"reverses_entry_id": str(uuid4())},
        {"source_type": "OTHER"},
        {"source_id": str(uuid4())},
        {"account_id": str(uuid4())},
        {"effective_date": "2026-01-10"},
        {"currency": "HKD"},
        {"amount_micro": -1},
        {"fx_rate_hkd_e12": 1},
        {"booked_hkd_micro": 0},
        {"kind": "ADJUSTMENT"},
        {"external_flow": 1},
    ],
)
def test_inconsistent_reversal_cash_fails_closed(changes: dict[str, Any]) -> None:
    con, receipt = mocked_cash(reversal_changes=changes)
    with pytest.raises(ValueError):
        history._cash_scope(con, receipt)


def test_missing_cash_and_missing_reversal_are_not_inferred_as_zero() -> None:
    con, receipt = mocked_cash(receipt_changes={"status": "VOID"})
    assert history._cash_scope(con, receipt) == "CASH_REVERSAL_NOT_RECORDED"
    con, receipt = mocked_cash()
    empty = MagicMock()
    empty.mappings.return_value.all.return_value = []
    cast(Any, con).execute.side_effect = [empty]
    assert history._cash_scope(con, receipt) == "CASH_NOT_RECORDED"


@pytest.mark.parametrize("day", [None, 0, "20260109", "2026-02-30", "0000-01-01"])
def test_invalid_or_noncanonical_recorded_dates_fail_closed(day: object) -> None:
    with pytest.raises(ValueError):
        history._day(day)


def test_unsupported_currency_and_noninteger_scales_are_not_coerced() -> None:
    with pytest.raises(ValueError):
        history._currency("CNY")
    for value in (True, 1.0, "1", -1, 2**63):
        with pytest.raises(ValueError):
            history._decimal(value, 6, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"account_id": str(uuid4())},
        {"source": "SYNTHETIC"},
        {"status": "SUPERSEDED"},
        {"version": 0},
        {"version": 1.0},
        {"created_at": "private"},
        {"status": "ACTIVE", "voided_at": "2026-11-09T12:00:01.000000Z"},
        {"status": "VOID", "voided_at": None},
        {"status": "VOID", "version": 1, "voided_at": "2026-11-09T12:00:01.000000Z"},
        {"status": "VOID", "version": 2, "voided_at": "2026-11-08T12:00:00.000000Z"},
        {"received_date": "2026-02-30"},
        {"received_micro": 1.0},
        {"tax_micro": -1},
        {"fee_micro": -1},
        {"currency": "CNY"},
        {"fx_rate_hkd_e12": 0},
    ],
)
def test_malformed_receipt_rows_fail_closed(changes: dict[str, Any]) -> None:
    con, raw = mocked_cash()
    row = {
        **dict(raw),
        "source": "MANUAL",
        "version": 1,
        "voided_at": None,
        "tax_micro": 0,
        "fee_micro": 0,
        "created_at": "2026-11-09T12:00:00.000000Z",
        **changes,
    }
    with pytest.raises(ValueError):
        history._receipt(con, cast(RowMapping, row), UUID(raw["account_id"]))


def test_receipts_keep_their_own_currency_without_cross_currency_netting(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, older, _ = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        usd = add_posted_receipt(con, older, fx=None)
        hkd = {
            "id": str(uuid4()),
            "entitlement_id": older["id"],
            "account_id": str(account),
            "received_date": "2026-01-10",
            "received_micro": 2_000_000,
            "currency": "HKD",
            "fx_rate_hkd_e12": 1_000_000_000_000,
            "received_hkd_micro": 2_000_000,
            "created_at": "2026-11-09T12:00:03.000000Z",
        }
        insert(con, "dividend_receipts", hkd)
        post_cash(
            con,
            account=str(account),
            day="2026-01-10",
            currency="HKD",
            amount=2_000_000,
            fx=1_000_000_000_000,
            booked=2_000_000,
            kind="DIVIDEND_RECEIPT",
            source_type="DIVIDEND_RECEIPT",
            source_id=hkd["id"],
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = history.history(con, OWNER, first.event_id, account)
    assert result is not None
    receipts = result.entitlements[0].receipts
    assert [(item.id, item.currency, item.received) for item in receipts] == [
        (UUID(usd["id"]), "USD", Decimal("6.000000")),
        (UUID(hkd["id"]), "HKD", Decimal("2.000000")),
    ]
    assert receipts[0].received_hkd is None and receipts[1].received_hkd == Decimal("2.000000")
    assert all(item.audit_scope == "RECEIPT_AUDIT_NOT_VERIFIED" for item in receipts)
    assert database_snapshot(engine) == before


def test_repeated_reads_preserve_one_wal_snapshot_while_another_writer_appends(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, older, _ = prepared(engine)

    def append() -> dict[str, Any]:
        with unit_of_work(engine, write=True) as writer:
            return add_posted_receipt(writer, older, fx=None)

    with unit_of_work(engine) as con:
        before = history.history(con, OWNER, first.event_id, account)
        with ThreadPoolExecutor(max_workers=1) as pool:
            appended = pool.submit(append).result(timeout=10)
        assert history.history(con, OWNER, first.event_id, account) == before
    with unit_of_work(engine) as con:
        after = history.history(con, OWNER, first.event_id, account)
    assert before is not None and after is not None
    assert before.entitlements[0].receipts == ()
    assert after.entitlements[0].receipts[0].id == UUID(appended["id"])
    assert after.entitlements[0].receipts[0].fx_rate_hkd is None
    assert after.entitlements[0].receipts[0].received_hkd is None


def test_foreign_account_link_cannot_be_hidden_by_the_selected_account(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, _, _ = prepared(engine)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        real = con.execute

        def synthetic(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "SELECT 1 FROM dividend_receipts receipt" in str(statement):
                result = MagicMock()
                result.first.return_value = (1,)
                return result
            return real(statement, *args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=synthetic),
            pytest.raises(
                history.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"
            ),
        ):
            history.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before


def test_missing_historical_event_revision_fails_with_a_fixed_code(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, _, _ = prepared(engine)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        current = events.get(con, OWNER, first.event_id)
        with (
            patch.object(events, "get", side_effect=[current, None]),
            pytest.raises(
                history.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"
            ),
        ):
            history.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field,value",
    [("quality", "INVALID"), ("currency", "HKD"), ("tax_rate_snapshot_ppm", 1_000_001)],
)
def test_invalid_legacy_entitlement_is_not_silently_adopted(
    engine: Engine, source: dict[str, Any], field: str, value: object
) -> None:
    first, _, account, _, _ = prepared(engine)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        real = con.execute

        def synthetic(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = real(statement, *args, **kwargs)
            if "SELECT ent.*,revision.revision_no FROM dividend_entitlements" in str(statement):
                rows = [dict(row) for row in result.mappings().all()]
                rows[0][field] = value
                replacement = MagicMock()
                replacement.mappings.return_value.all.return_value = rows
                return replacement
            return result

        with (
            patch.object(con, "execute", side_effect=synthetic),
            pytest.raises(
                history.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"
            ),
        ):
            history.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before


def test_missing_scope_is_not_a_zero_receipt_and_requires_a_real_transaction(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, _, account, _, _ = prepared(engine)
    _, foreign = owned_account(engine, OTHER)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert history.history(con, OTHER, first.event_id, account) is None
        assert history.history(con, OWNER, first.event_id, UUID(foreign)) is None
        assert history.history(con, OWNER, uuid4(), account) is None
    with (
        engine.connect() as con,
        pytest.raises(history.ReceiptHistoryError, match="^INVALID_DIVIDEND_RECEIPT_SCOPE$"),
    ):
        history.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before
