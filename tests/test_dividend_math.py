"""Stable-event dividend income is distinct from actual receipt cash."""

from dataclasses import replace
from decimal import Decimal as D
from typing import Any

import pytest

from spm_api.dividend_math import Receipt, accrue, reconcile
from spm_api.finance import ZERO, FinanceError, Position, net_asset_value, value_position


def test_revision_reconciles_existing_receipts_by_stable_event_and_account() -> None:
    original = accrue("event", "A", "USD", D("10"), D("1"), ZERO, ZERO, D("7.8"))
    receipt = Receipt("historical-receipt", "event", "A", "USD", D("6"))
    revised = accrue("event", "A", "USD", D("10"), D("1.1"), ZERO, ZERO, D("7.8"))
    before = reconcile([original], [receipt])[0]
    after = reconcile([revised], [receipt])[0]
    assert before.outstanding_local == D("4")
    assert after.accrual.net_local == D("11")
    assert after.received_local == D("6")
    assert after.outstanding_local == D("5")
    assert after.receipt_state == "PARTIAL"
    assert receipt.amount == D("6")
    assert original.net_local == D("10")
    assert revised.booked_hkd is not None
    assert revised.booked_hkd == D("85.8")
    assert value_position(
        Position(), None, "HKD", recognized_dividend_hkd=revised.booked_hkd
    ).total_pnl_hkd == D("85.8")
    assert net_asset_value(ZERO, D("46.8"), "COMPLETE", receivables_hkd=D("39")).value == D("85.8")
    with pytest.raises(FinanceError) as duplicate:
        reconcile([original, revised], [receipt])
    assert duplicate.value.code == "DUPLICATE_ENTITLEMENT"


def test_partial_receipts_and_voids_do_not_leak_between_accounts() -> None:
    first = accrue("event", "A", "HKD", D("10"), D("1.1"), ZERO, ZERO)
    second = accrue("event", "B", "HKD", D("10"), D("1.1"), ZERO, ZERO)
    receipts = [
        Receipt("one", "event", "A", "HKD", D("6")),
        Receipt("two", "event", "A", "HKD", D("2")),
        Receipt("void", "event", "A", "HKD", D("100"), active=False),
    ]
    actual = reconcile([first, second], receipts)
    assert actual[0].received_local == D("8")
    assert actual[0].outstanding_local == D("3")
    assert actual[1].received_local == ZERO
    assert actual[1].outstanding_local == D("11")
    assert actual[1].receipt_state == "NOT_RECEIVED"


def test_distinct_events_are_not_coalesced_by_date_or_event_type() -> None:
    regular = accrue("regular", "A", "HKD", D("10"), D("0.25"), ZERO, ZERO)
    special = accrue("special", "A", "HKD", D("10"), D("0.05"), ZERO, ZERO)
    records = reconcile([regular, special], [Receipt("cash", "regular", "A", "HKD", D("2.5"))])
    assert len(records) == 2
    assert records[0].receipt_state == "RECEIVED"
    assert records[1].receipt_state == "NOT_RECEIVED"
    assert sum((record.accrual.net_local for record in records), ZERO) == D("3")


def test_cross_currency_cash_and_overpayment_require_reconciliation_not_clamping() -> None:
    accrual = accrue("event", "A", "USD", D("10"), D("1.1"), ZERO, ZERO, D("7.8"))
    mixed = reconcile([accrual], [Receipt("foreign", "event", "A", "HKD", D("46.8"))])[0]
    assert mixed.received_local is None
    assert mixed.outstanding_local is None
    assert mixed.quality == "NEEDS_REVIEW"
    overpaid = reconcile([accrual], [Receipt("extra", "event", "A", "USD", D("12"))])[0]
    assert overpaid.outstanding_local == D("-1")
    assert overpaid.receipt_state == "OVER_RECEIVED"
    assert overpaid.quality == "NEEDS_REVIEW"


def test_negative_expected_net_is_preserved_and_missing_fx_stays_null() -> None:
    accrual = accrue("event", "A", "USD", D("1"), D("0.1"), ZERO, D("1"), D("7.8"))
    assert accrual.net_local == D("-0.9")
    assert accrual.booked_hkd == D("-7.02")
    assert accrual.quality == "NEEDS_REVIEW"
    assert reconcile([accrual], [])[0].receipt_state == "NEEDS_REVIEW"
    missing = accrue("event", "A", "USD", D("10"), D("1"), ZERO, ZERO)
    assert missing.net_local == D("10")
    assert missing.booked_hkd is None
    assert missing.quality == "MISSING_FX"
    assert reconcile([missing], [])[0].receipt_state == "NOT_RECEIVED"


def test_dividend_conversion_uses_unrounded_calculation_and_discloses_component_rounding() -> None:
    tiny = accrue("event", "A", "USD", D("0.00000010"), D("0.5"), ZERO, ZERO, D("20"))
    assert tiny.net_local == ZERO
    assert tiny.booked_hkd == D("0.000001")
    rounded = accrue("event", "A", "HKD", D("1"), D("0.000001"), D("0.5"), ZERO)
    assert rounded.gross_local == D("0.000001")
    assert rounded.tax_local == D("0.000001")
    assert rounded.net_local == D("0.000001")
    assert rounded.rounding_delta_local == D("0.000001")


def test_receipts_are_not_implicitly_created_and_duplicate_or_orphan_cash_is_rejected() -> None:
    accrual = accrue("event", "A", "HKD", D("1"), ZERO, ZERO, ZERO)
    assert reconcile([accrual], [])[0].receipt_state == "NOT_RECEIVED"
    explicit = Receipt("zero", "event", "A", "HKD", ZERO)
    assert reconcile([accrual], [explicit])[0].receipt_state == "RECEIVED"
    with pytest.raises(FinanceError) as duplicate:
        reconcile([accrual], [explicit, explicit])
    assert duplicate.value.code == "DUPLICATE_RECEIPT"
    with pytest.raises(FinanceError) as orphan:
        reconcile([], [explicit])
    assert orphan.value.code == "NEEDS_REVIEW"
    assert reconcile([], [replace(explicit, active=False)]) == ()


@pytest.mark.parametrize("value", [0.5, True, D("NaN"), D("Infinity"), D("0.0000001")])
def test_actual_receipt_money_is_exact_and_never_float(value: Any) -> None:
    with pytest.raises(FinanceError):
        Receipt("cash", "event", "A", "HKD", value)


@pytest.mark.parametrize("rate", [D("1.000001"), D("-0.1"), D("0.3000001"), 0.3])
def test_tax_snapshot_is_a_bounded_exact_fraction(rate: Any) -> None:
    with pytest.raises(FinanceError):
        accrue("event", "A", "HKD", D("10"), D("1"), rate, ZERO)
