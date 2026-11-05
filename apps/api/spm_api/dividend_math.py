"""Dividend arithmetic and stable-event reconciliation, independent of provider workflows."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from spm_api.finance import (
    ONE,
    ZERO,
    FinanceError,
    Quality,
    conversion_rate,
    exact_decimal,
    round_money,
)
from spm_api.master_models import Currency

ReceiptState = Literal["NOT_RECEIVED", "PARTIAL", "RECEIVED", "OVER_RECEIVED", "NEEDS_REVIEW"]


@dataclass(frozen=True, slots=True)
class Accrual:
    event_id: str
    account_id: str
    currency: Currency
    gross_local: Decimal
    tax_local: Decimal
    fee_local: Decimal
    net_local: Decimal
    booked_hkd: Decimal | None
    rounding_delta_local: Decimal
    quality: Quality

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.event_id, self.account_id)
        ):
            raise FinanceError("INVALID_EVENT", "Stable event and account identities are required")
        for value in (self.gross_local, self.tax_local, self.fee_local):
            exact_decimal(value, 6, nonnegative=True)
        exact_decimal(self.net_local, 6)
        exact_decimal(self.rounding_delta_local, 6)
        if self.booked_hkd is not None:
            exact_decimal(self.booked_hkd, 6)
        conversion_rate(self.currency, None)


def accrue(
    event_id: str,
    account_id: str,
    currency: Currency,
    eligible_quantity: Decimal,
    dps: Decimal,
    tax_rate: Decimal,
    fee: Decimal,
    rate: Decimal | None = None,
) -> Accrual:
    """Calculate a declared accrual, not an actual receipt or a verification assertion."""
    if not event_id or not account_id:
        raise FinanceError("INVALID_EVENT", "Stable event and account identities are required")
    quantity = exact_decimal(eligible_quantity, 8, nonnegative=True)
    dps = exact_decimal(dps, 8, nonnegative=True)
    tax_rate = exact_decimal(tax_rate, 6, nonnegative=True)
    fee = exact_decimal(fee, 6, nonnegative=True)
    if tax_rate > ONE:
        raise FinanceError("INVALID_FINANCIAL_VALUE", "A tax fraction cannot exceed one")
    fx = conversion_rate(currency, rate)
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        gross = quantity * dps
        tax = gross * tax_rate
        net = gross - tax - fee
        # Derive HKD before quantizing the separate persisted monetary components.
        hkd = round_money(net * fx) if fx is not None else None
        quality: Quality = "NEEDS_REVIEW" if net < ZERO else "MISSING_FX" if fx is None else "OK"
        gross_money, tax_money, net_money = round_money(gross), round_money(tax), round_money(net)
        return Accrual(
            event_id,
            account_id,
            currency,
            gross_money,
            tax_money,
            fee,
            net_money,
            hkd,
            net_money - (gross_money - tax_money - fee),
            quality,
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    id: str
    event_id: str
    account_id: str
    currency: Currency
    amount: Decimal
    active: bool = True

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or not value.strip()
                for value in (self.id, self.event_id, self.account_id)
            )
            or type(self.active) is not bool
        ):
            raise FinanceError(
                "INVALID_RECEIPT", "Stable receipt/event/account identities are required"
            )
        exact_decimal(self.amount, 6)
        conversion_rate(self.currency, None)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    accrual: Accrual
    received_local: Decimal | None
    outstanding_local: Decimal | None
    receipt_state: ReceiptState
    quality: Quality


def reconcile(
    accruals: Sequence[Accrual], receipts: Sequence[Receipt]
) -> tuple[Reconciliation, ...]:
    """Reconcile one current accrual per stable event/account with historical receipts."""
    current: dict[tuple[str, str], Accrual] = {}
    for accrual in accruals:
        key = accrual.event_id, accrual.account_id
        if key in current:
            raise FinanceError(
                "DUPLICATE_ENTITLEMENT", "Select one active entitlement per event/account"
            )
        current[key] = accrual
    by_event: dict[tuple[str, str], list[Receipt]] = {}
    seen: set[str] = set()
    for receipt in receipts:
        if receipt.id in seen:
            raise FinanceError("DUPLICATE_RECEIPT", "A receipt cannot be counted twice")
        seen.add(receipt.id)
        if receipt.active:
            key = receipt.event_id, receipt.account_id
            if key not in current:
                raise FinanceError(
                    "NEEDS_REVIEW", "An actual receipt has no current matching entitlement"
                )
            by_event.setdefault(key, []).append(receipt)
    results: list[Reconciliation] = []
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        for key, accrual in current.items():
            active = by_event.get(key, [])
            if any(receipt.currency != accrual.currency for receipt in active):
                results.append(Reconciliation(accrual, None, None, "NEEDS_REVIEW", "NEEDS_REVIEW"))
                continue
            received = round_money(sum((receipt.amount for receipt in active), ZERO))
            outstanding = round_money(accrual.net_local - received)
            if accrual.net_local < ZERO or any(receipt.amount < ZERO for receipt in active):
                state: ReceiptState = "NEEDS_REVIEW"
            elif not active:
                state = "NOT_RECEIVED"
            elif outstanding < ZERO:
                state = "OVER_RECEIVED"
            elif outstanding == ZERO:
                state = "RECEIVED"
            else:
                state = "PARTIAL"
            quality: Quality = (
                "NEEDS_REVIEW" if state in {"NEEDS_REVIEW", "OVER_RECEIVED"} else accrual.quality
            )
            results.append(Reconciliation(accrual, received, outstanding, state, quality))
    return tuple(results)
