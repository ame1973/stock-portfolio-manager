"""Explicit protected recorded-cash creation inputs and historical responses."""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney, from_scaled
from spm_api.dividend_event_service import Reason, Version
from spm_api.dividend_receipt_terms import ReceiptComponents, Source, calculate_components
from spm_api.dividend_receipt_writer import ReceiptNote
from spm_api.dividend_tax_models import AdvanceableOrdinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.master_models import Currency


class ReceiptCreate(ReceiptComponents):
    """The client supplies a stable receipt identity and every observed scope pin."""

    receipt_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    entitlement_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    expected_data_revision: AdvanceableOrdinal
    received_date: IsoDate = Field(repr=False)
    note: ReceiptNote = Field(repr=False)
    reason: Reason = Field(repr=False)
    confirmation: Literal["RECORDED_CASH_ONLY"]

    @model_validator(mode="after")
    def valid_cash_components(self) -> Self:
        calculate_components(self)
        return self


class ReceiptCreated(Model):
    """The original creation response, which may no longer describe the current lifecycle."""

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    entitlement_id: UUID = Field(repr=False)
    original_revision_id: UUID = Field(repr=False)
    original_revision_no: Version
    observed_revision_id: UUID = Field(repr=False)
    observed_revision_no: Version
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_data_revision: AdvanceableOrdinal
    cash_entry_id: UUID = Field(repr=False)
    received_date: IsoDate = Field(repr=False)
    received_amount: Money = Field(repr=False)
    currency: Currency
    tax: NonNegativeMoney | None = Field(repr=False)
    fee: NonNegativeMoney | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    fx_source: Source | None = Field(repr=False)
    received_hkd: Money | None = Field(repr=False)
    actual_hkd_override: bool = Field(strict=True)
    source: Literal["MANUAL"]
    status: Literal["ACTIVE"]
    version: Literal["1"]
    created_at: AwareDatetime = Field(repr=False)
    audit_scope: Literal["RECORDED_AUDIT_MATCHED_ONLY"]
    scope: Literal["HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_cash(self) -> Self:
        if (
            self.original_revision_no > self.observed_revision_no
            or (self.original_revision_no == self.observed_revision_no)
            != (self.original_revision_id == self.observed_revision_id)
            or (self.fx_rate_hkd is None) != (self.fx_source is None)
            or (self.fx_rate_hkd is None) != (self.received_hkd is None)
            or (self.actual_hkd_override and self.received_hkd is None)
            or (self.fx_source is not None and self.fx_source != self.fx_source.strip())
        ):
            raise ValueError("The recorded receipt response is inconsistent")
        if self.received_hkd is not None:
            amount, booked = Decimal(self.received_amount), Decimal(self.received_hkd)
            if (amount == 0 and booked != 0) or (amount < 0 < booked) or (booked < 0 < amount):
                raise ValueError("The recorded receipt cash signs are inconsistent")
        if self.currency == "HKD":
            if (
                self.fx_rate_hkd != "1.000000000000"
                or self.fx_source != "IDENTITY"
                or self.received_hkd != self.received_amount
            ):
                raise ValueError("The recorded HKD identity is inconsistent")
        elif self.fx_source == "IDENTITY":
            raise ValueError("Foreign currency requires a nonidentity source")
        if not self.actual_hkd_override:
            components = ReceiptComponents(
                received_amount=self.received_amount,
                currency=self.currency,
                tax=self.tax,
                fee=self.fee,
                fx_rate_hkd=self.fx_rate_hkd,
                fx_source=self.fx_source,
                actual_hkd=None,
                actual_hkd_reason=None,
            )
            booking = calculate_components(components)
            booked_hkd = (
                from_scaled(booking.received_hkd_micro, 6)
                if booking.received_hkd_micro is not None
                else None
            )
            if self.received_hkd != booked_hkd:
                raise ValueError("The recorded receipt valuation is inconsistent")
        return self
