"""Exact direct-cash input pins without entitlement, cash defaults or a financial grant."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from spm_api.dividend_combined_receipt_models import CombinedRecordedReceipt
from spm_api.dividend_event_service import ReviewStatus, Version
from spm_api.dividend_tax_models import Ordinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.master_models import ArchiveStatus


class DirectReceiptInputs(Model):
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    workflow_status: ReviewStatus
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_status: ArchiveStatus
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_data_revision: Ordinal
    rejection_state: Literal["NOT_REJECTED", "REJECTED"]
    rejection_id: UUID | None = Field(repr=False)
    receipt_basis: Literal["DIRECT_EVENT_ACCOUNT"]
    scope: Literal["DIRECT_RECORDED_RECEIPT_INPUTS_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_rejection(self) -> Self:
        if (self.rejection_state == "REJECTED") != (self.rejection_id is not None):
            raise ValueError("The recorded rejection pin is inconsistent")
        return self


class DirectReceiptVoidInputs(Model):
    current: DirectReceiptInputs = Field(repr=False)
    receipt: CombinedRecordedReceipt = Field(repr=False)
    cash_entry_id: UUID = Field(repr=False)
    origin_portfolio_id: UUID = Field(repr=False)
    scope: Literal["DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY"]

    @model_validator(mode="after")
    def consistent_direct_scope(self) -> Self:
        if self.receipt.receipt_basis != "DIRECT_EVENT_ACCOUNT" or (
            self.current.event_id,
            self.current.account_id,
            self.current.revision_id,
            self.current.revision_no,
        ) != (
            self.receipt.event_id,
            self.receipt.account_id,
            self.receipt.current_revision_id,
            self.receipt.current_revision_no,
        ):
            raise ValueError("The direct receipt void input scope is inconsistent")
        return self
