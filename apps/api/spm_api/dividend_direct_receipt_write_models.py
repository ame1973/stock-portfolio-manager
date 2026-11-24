"""Explicit direct-cash HTTP DTOs; declaring these models does not install a writer."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.decimal_types import IsoDate, Money, from_scaled
from spm_api.dividend_event_service import Reason, Version
from spm_api.dividend_receipt_terms import ReceiptComponents, calculate_components
from spm_api.dividend_receipt_writer import ReceiptNote
from spm_api.dividend_tax_models import AdvanceableOrdinal, PositiveOrdinal


class DirectReceiptCreate(ReceiptComponents):
    """A stable client receipt ID, explicit observed pins and a separate fence acknowledgment."""

    receipt_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
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
    confirm_revision_fence: bool = Field(strict=True)

    @model_validator(mode="after")
    def explicit_cash_and_fence(self) -> Self:
        if not self.confirm_revision_fence:
            raise ValueError("Recording cash fences event decisions until reconciliation exists")
        calculate_components(self)
        return self


class DirectReceiptCreated(ReceiptComponents):
    """Original recorded creation cash, not the current lifecycle or verified payment."""

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_data_revision: AdvanceableOrdinal
    cash_entry_id: UUID = Field(repr=False)
    received_date: IsoDate = Field(repr=False)
    received_hkd: Money | None = Field(repr=False)
    receipt_basis: Literal["DIRECT_EVENT_ACCOUNT"]
    source: Literal["MANUAL"]
    status: Literal["ACTIVE"]
    version: Literal["1"]
    created_at: AwareDatetime = Field(repr=False)
    audit_scope: Literal["RECORDED_AUDIT_MATCHED_ONLY"]
    scope: Literal["HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_original_cash(self) -> Self:
        booking = calculate_components(self)
        rate = (
            from_scaled(booking.fx_rate_hkd_e12, 12)
            if booking.fx_rate_hkd_e12 is not None
            else None
        )
        hkd = (
            from_scaled(booking.received_hkd_micro, 6)
            if booking.received_hkd_micro is not None
            else None
        )
        if (self.fx_rate_hkd, self.fx_source, self.received_hkd) != (rate, booking.fx_source, hkd):
            raise ValueError("The original direct receipt cash components are inconsistent")
        return self
