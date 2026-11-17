"""Normalize explicit receipt cash and FX terms without authorizing or posting income."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from spm_api.cash_models import CashDate
from spm_api.decimal_types import FxRate, Money, NonNegativeMoney, to_scaled
from spm_api.dividend_event_service import Reason
from spm_api.errors import Model
from spm_api.finance import conversion_rate, lock_money
from spm_api.ledger_service import reversible_integer
from spm_api.master_models import Currency

Source = Annotated[str, Field(strict=True, min_length=1, max_length=100, pattern=r"^[^\x00]+$")]


class ReceiptTerms(Model):
    """Explicit recorded values, not proof of ownership, payment or eligibility."""

    model_config = ConfigDict(frozen=True)

    received_date: CashDate = Field(repr=False)
    received_amount: Money = Field(repr=False)
    currency: Currency
    tax: NonNegativeMoney | None = Field(repr=False)
    fee: NonNegativeMoney | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    fx_source: Source | None = Field(repr=False)
    actual_hkd: Money | None = Field(repr=False)
    actual_hkd_reason: Reason | None = Field(repr=False)

    @field_validator("fx_source")
    @classmethod
    def canonical_source(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("A rate source must not have surrounding whitespace")
        return value

    @model_validator(mode="after")
    def explicit_components(self) -> Self:
        reversible_integer(to_scaled(self.received_amount, 6))
        if self.actual_hkd is not None:
            reversible_integer(to_scaled(self.actual_hkd, 6))
        if (self.fx_rate_hkd is None) != (self.fx_source is None):
            raise ValueError("A historical rate and its source must be supplied together")
        if self.currency == "HKD":
            if self.fx_rate_hkd is not None and (
                Decimal(self.fx_rate_hkd) != 1 or self.fx_source != "IDENTITY"
            ):
                raise ValueError("HKD conversion must use identity")
        elif self.fx_source == "IDENTITY":
            raise ValueError("A foreign conversion needs an explicit source")
        if (self.actual_hkd is None) != (self.actual_hkd_reason is None):
            raise ValueError("An actual HKD override and its reason must be supplied together")
        if self.actual_hkd is not None and self.currency == "USD" and self.fx_rate_hkd is None:
            raise ValueError("An actual HKD override still needs its historical FX rate")
        return self


@dataclass(frozen=True, slots=True)
class ReceiptBooking:
    """Calculated scaled values only; this is not a durable cash entry or financial grant."""

    received_micro: int = field(repr=False)
    tax_micro: int | None = field(repr=False)
    fee_micro: int | None = field(repr=False)
    fx_rate_hkd_e12: int | None = field(repr=False)
    fx_source: str | None = field(repr=False)
    received_hkd_micro: int | None = field(repr=False)
    actual_hkd_override: bool


def calculate_receipt(intent: ReceiptTerms) -> ReceiptBooking:
    """Value the recorded net receipt, never subtracting its informational tax/fee twice."""
    if not isinstance(intent, ReceiptTerms):
        raise ValueError("INVALID_DIVIDEND_RECEIPT_TERMS")
    intent = ReceiptTerms.model_validate(
        {name: getattr(intent, name) for name in ReceiptTerms.model_fields}
    )
    amount = reversible_integer(to_scaled(intent.received_amount, 6))
    tax = to_scaled(intent.tax, 6) if intent.tax is not None else None
    fee = to_scaled(intent.fee, 6) if intent.fee is not None else None
    rate = conversion_rate(
        intent.currency, Decimal(intent.fx_rate_hkd) if intent.fx_rate_hkd is not None else None
    )
    if rate is None:
        return ReceiptBooking(amount, tax, fee, None, None, None, False)
    locked = lock_money(
        Decimal(intent.received_amount),
        intent.currency,
        rate,
        actual_hkd=Decimal(intent.actual_hkd) if intent.actual_hkd is not None else None,
        reason=intent.actual_hkd_reason,
    )
    return ReceiptBooking(
        amount,
        tax,
        fee,
        to_scaled(format(rate, "f"), 12),
        "IDENTITY" if intent.currency == "HKD" else intent.fx_source,
        reversible_integer(to_scaled(format(locked.hkd, "f"), 6)),
        intent.actual_hkd is not None,
    )
