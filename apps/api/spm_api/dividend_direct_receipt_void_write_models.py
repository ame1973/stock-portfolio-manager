"""Explicit direct receipt void DTOs; these models do not install a mutation endpoint."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.dividend_event_service import Reason, Version
from spm_api.dividend_tax_models import AdvanceableOrdinal, PositiveOrdinal
from spm_api.errors import Model


class _DirectReceiptVoidScope(Model):
    """Shared exact observed and original pins, without an entitlement substitute."""

    event_id: UUID = Field(repr=False)
    observed_revision_id: UUID = Field(repr=False)
    observed_revision_no: Version
    original_revision_id: UUID = Field(repr=False)
    original_revision_no: Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    cash_entry_id: UUID = Field(repr=False)

    @model_validator(mode="after")
    def consistent_revisions(self) -> Self:
        if self.original_revision_no > self.observed_revision_no or (
            self.original_revision_id == self.observed_revision_id
        ) != (self.original_revision_no == self.observed_revision_no):
            raise ValueError("The original and observed direct receipt revisions are inconsistent")
        return self


class DirectReceiptVoidWrite(_DirectReceiptVoidScope):
    """Explicit current scope and original cash; receipt identity/version belong to path/header."""

    expected_data_revision: AdvanceableOrdinal
    reason: Reason = Field(repr=False)
    confirmation: Literal["VOID_RECORDED_CASH"]


class DirectReceiptVoided(_DirectReceiptVoidScope):
    """Original reversal metadata, not current account scope or verified payment."""

    id: UUID = Field(repr=False)
    origin_portfolio_id: UUID = Field(repr=False)
    portfolio_data_revision: AdvanceableOrdinal
    reversal_entry_id: UUID = Field(repr=False)
    receipt_basis: Literal["DIRECT_EVENT_ACCOUNT"]
    status: Literal["VOID"]
    version: Literal["2"]
    created_at: AwareDatetime = Field(repr=False)
    voided_at: AwareDatetime = Field(repr=False)
    audit_scope: Literal["RECORDED_AUDIT_MATCHED_ONLY"]
    scope: Literal["HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_reversal(self) -> Self:
        if self.cash_entry_id == self.reversal_entry_id or self.voided_at < self.created_at:
            raise ValueError("The recorded direct receipt reversal is inconsistent")
        return self
