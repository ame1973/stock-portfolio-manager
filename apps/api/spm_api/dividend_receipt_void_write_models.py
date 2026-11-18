"""Protected receipt void request and original historical response contracts."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.dividend_event_service import Reason, Version
from spm_api.dividend_tax_models import AdvanceableOrdinal, PositiveOrdinal
from spm_api.errors import Model


class ReceiptVoidWrite(Model):
    """Explicit current scope and original cash pins for a recorded reversal."""

    event_id: UUID = Field(repr=False)
    observed_revision_id: UUID = Field(repr=False)
    observed_revision_no: Version
    entitlement_id: UUID = Field(repr=False)
    original_revision_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    expected_data_revision: AdvanceableOrdinal
    cash_entry_id: UUID = Field(repr=False)
    reason: Reason = Field(repr=False)
    confirmation: Literal["VOID_RECORDED_CASH"]


class ReceiptVoided(Model):
    """Original void response, not proof of payment or a current-use grant."""

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    observed_revision_id: UUID = Field(repr=False)
    observed_revision_no: Version
    entitlement_id: UUID = Field(repr=False)
    original_revision_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    origin_portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_data_revision: AdvanceableOrdinal
    cash_entry_id: UUID = Field(repr=False)
    reversal_entry_id: UUID = Field(repr=False)
    status: Literal["VOID"]
    version: Literal["2"]
    created_at: AwareDatetime = Field(repr=False)
    voided_at: AwareDatetime = Field(repr=False)
    audit_scope: Literal["RECORDED_AUDIT_MATCHED_ONLY"]
    scope: Literal["HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_reversal(self) -> Self:
        if self.cash_entry_id == self.reversal_entry_id or self.voided_at < self.created_at:
            raise ValueError("The recorded receipt reversal is inconsistent")
        return self
