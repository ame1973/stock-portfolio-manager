"""Exact native cash/expense contracts; recorded balances are not live FX valuations."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, model_validator

from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Completeness, Currency, Name, Note


def _past_or_today(value: str) -> str:
    if value > date.today().isoformat():
        raise ValueError("Cash and expenses record actual movements, not future plans")
    return value


CashDate = Annotated[IsoDate, AfterValidator(_past_or_today)]
ManualCashKind = Literal["DEPOSIT", "WITHDRAW", "ADJUSTMENT"]
BookingQuality = Literal["OK", "MISSING_FX"]
BalanceQuality = Literal["COMPLETE", "PARTIAL", "UNKNOWN", "BEFORE_OPENING", "NEEDS_REVIEW"]


class BookingInput(Model):
    account_id: UUID
    currency: Currency
    amount: Money
    fx_rate_hkd: FxRate | None = None
    actual_hkd: Money | None = None
    reason: ChangeReason | None = None
    note: Note = ""

    @model_validator(mode="after")
    def booking_evidence(self) -> "BookingInput":
        if Decimal(self.amount) == 0:
            raise ValueError("A cash or expense amount must be nonzero")
        if self.actual_hkd is not None and self.reason is None:
            raise ValueError("Actual HKD booking needs an evidence explanation")
        return self


class CashEntryCreate(BookingInput):
    effective_date: CashDate
    kind: ManualCashKind
    external_flow: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def cash_direction(self) -> "CashEntryCreate":
        amount = Decimal(self.amount)
        if (self.kind == "DEPOSIT" and amount <= 0) or (self.kind == "WITHDRAW" and amount >= 0):
            raise ValueError("Deposits are positive and withdrawals are negative")
        if self.kind == "ADJUSTMENT" and self.reason is None:
            raise ValueError("A non-external adjustment requires an explanation")
        expected = self.kind != "ADJUSTMENT"
        if self.external_flow is not None and self.external_flow != expected:
            raise ValueError("External flow classification must match the financial operation")
        return self


class CashEntryRevision(CashEntryCreate):
    reason: ChangeReason


class VoidFinancial(Model):
    reason: ChangeReason


class CashEntry(Model):
    id: UUID
    account_id: UUID
    portfolio_id: UUID
    effective_date: IsoDate
    kind: str
    currency: Currency
    amount: Money
    fx_rate_hkd: FxRate | None
    booked_hkd: Money | None
    external_flow: bool
    source_type: str
    source_id: str
    entry_role: Literal["PRIMARY", "REVERSAL"]
    reverses_entry_id: UUID | None
    reversed_by_id: UUID | None
    supersedes_id: UUID | None
    superseded_by_id: UUID | None
    status: Literal["ACTIVE", "SUPERSEDED", "VOID", "REVERSAL"]
    editable: bool
    quality: BookingQuality
    note: str
    version: int
    created_at: str


class ExpenseCreate(BookingInput):
    date: CashDate
    category: Name

    @model_validator(mode="after")
    def refund_evidence(self) -> "ExpenseCreate":
        if Decimal(self.amount) < 0 and self.reason is None:
            raise ValueError("A negative expense is an explicit refund and needs an explanation")
        return self


class ExpenseRevision(ExpenseCreate):
    reason: ChangeReason


class Expense(Model):
    id: UUID
    portfolio_id: UUID
    account_id: UUID
    date: IsoDate
    category: str
    amount: Money
    currency: Currency
    fx_rate_hkd: FxRate | None
    hkd_amount: Money | None
    status: Literal["ACTIVE", "SUPERSEDED", "VOID"]
    supersedes_id: UUID | None
    quality: BookingQuality
    note: str
    version: int
    created_at: str


class CurrencyCashBalance(Model):
    currency: Currency
    recorded_balance: Money
    balance: Money | None
    balance_quality: BalanceQuality
    historical_booked_hkd_net: Money | None
    historical_booking_quality: BookingQuality
    unvalued_entries: Annotated[int, Field(ge=0)]


class AccountCashBalances(Model):
    account_id: UUID
    as_of: IsoDate
    cash_completeness: Completeness
    balances: list[CurrencyCashBalance]


class OpeningCashUpdate(Model):
    opening_cash: NonNegativeMoney | None
    opening_cash_date: CashDate | None = None
    opening_fx_rate_hkd: FxRate | None = None
    reason: ChangeReason
    note: Note = ""

    @model_validator(mode="after")
    def complete_assertion(self) -> "OpeningCashUpdate":
        if self.opening_cash is None:
            if self.opening_cash_date is not None or self.opening_fx_rate_hkd is not None:
                raise ValueError("Clearing an opening assertion cannot retain its date/rate")
        elif self.opening_cash_date is None:
            raise ValueError("An opening assertion requires an explicit effective date")
        return self


class OpeningCashRecord(Model):
    account_id: UUID
    currency: Currency
    opening_cash: NonNegativeMoney | None
    opening_cash_date: IsoDate | None
    fx_rate_hkd: FxRate | None
    booked_hkd: Money | None
    entry_id: UUID | None
    predecessor_id: UUID | None
    version: int
    cash_completeness: Completeness
    quality: Literal["UNSET", "OK", "MISSING_FX", "NEEDS_REVIEW"]
