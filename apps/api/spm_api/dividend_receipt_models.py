"""Private retained receipt records without economic or audit verification."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney
from spm_api.dividend_event_service import Version
from spm_api.dividend_receipt_history import CashScope
from spm_api.dividend_tax_models import PositiveOrdinal
from spm_api.errors import Model
from spm_api.master_models import Currency


class RecordedReceipt(Model):
    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    entitlement_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    current_revision_id: UUID = Field(repr=False)
    received_date: IsoDate = Field(repr=False)
    recorded_amount: Money = Field(repr=False)
    currency: Currency
    recorded_tax: NonNegativeMoney | None = Field(repr=False)
    recorded_fee: NonNegativeMoney | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    received_hkd: Money | None = Field(repr=False)
    source: Literal["MANUAL", "IMPORT"]
    status: Literal["ACTIVE", "VOID"]
    version: PositiveOrdinal
    created_at: AwareDatetime = Field(repr=False)
    voided_at: AwareDatetime | None = Field(repr=False)
    cash_scope: CashScope
    audit_scope: Literal["RECEIPT_AUDIT_NOT_VERIFIED"]
    history_completeness: Literal["UNVERIFIED"]
    scope: Literal["UNVERIFIED_RECEIPT_HISTORY_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]

    @model_validator(mode="after")
    def consistent_lifecycle(self) -> Self:
        if (
            (self.status == "ACTIVE" and self.voided_at is not None)
            or (self.status == "VOID" and (self.voided_at is None or self.version == "1"))
            or (self.voided_at is not None and self.voided_at < self.created_at)
            or (
                self.status == "ACTIVE"
                and self.cash_scope
                in {"CASH_REVERSAL_NOT_RECORDED", "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"}
            )
            or (self.status == "VOID" and self.cash_scope == "MATCHING_CASH_RECORDED_UNVERIFIED")
        ):
            raise ValueError("The retained receipt lifecycle is inconsistent")
        return self
