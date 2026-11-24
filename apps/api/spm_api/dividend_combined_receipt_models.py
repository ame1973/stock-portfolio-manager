"""Exact private combined receipt records, not payment or eligibility verification."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.decimal_types import INT64_MAX, FxRate, IsoDate, Money, NonNegativeMoney, to_scaled
from spm_api.dividend_event_service import Version
from spm_api.dividend_receipt_history import CashScope
from spm_api.dividend_tax_models import PositiveOrdinal
from spm_api.errors import Model
from spm_api.master_models import Currency


class CombinedRecordedReceipt(Model):
    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    receipt_basis: Literal["LEGACY_ENTITLEMENT", "DIRECT_EVENT_ACCOUNT"]
    entitlement_id: UUID | None = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    current_revision_id: UUID = Field(repr=False)
    current_revision_no: Version
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
    audit_scope: Literal["RECEIPT_AUDIT_NOT_VERIFIED", "RECORDED_AUDIT_MATCHED_ONLY"]
    scope: Literal["UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_recorded_basis_and_lifecycle(self) -> Self:
        direct = self.receipt_basis == "DIRECT_EVENT_ACCOUNT"
        matched = self.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
        expected_cash = (
            "MATCHING_CASH_RECORDED_UNVERIFIED"
            if self.status == "ACTIVE"
            else "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
        )
        if (
            direct != (self.entitlement_id is None)
            or self.revision_no > self.current_revision_no
            or (self.revision_id == self.current_revision_id)
            != (self.revision_no == self.current_revision_no)
            or (self.status == "ACTIVE") != (self.voided_at is None)
            or (self.voided_at is not None and self.voided_at < self.created_at)
            or (self.status == "VOID" and self.version == "1")
            or (
                self.status == "ACTIVE"
                and self.cash_scope
                in {"CASH_REVERSAL_NOT_RECORDED", "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"}
            )
            or (self.status == "VOID" and self.cash_scope == "MATCHING_CASH_RECORDED_UNVERIFIED")
            or (
                matched
                and (
                    self.cash_scope != expected_cash
                    or self.version != ("1" if self.status == "ACTIVE" else "2")
                )
            )
            or (direct and (not matched or self.source != "MANUAL"))
        ):
            raise ValueError("The combined recorded receipt scope is inconsistent")
        if matched:
            amount = to_scaled(self.recorded_amount, 6)
            hkd = to_scaled(self.received_hkd, 6) if self.received_hkd is not None else None
            if (
                amount < -INT64_MAX
                or (hkd is not None and hkd < -INT64_MAX)
                or (self.fx_rate_hkd is None) != (hkd is None)
                or (
                    self.currency == "HKD"
                    and (
                        self.fx_rate_hkd != "1.000000000000"
                        or self.received_hkd != self.recorded_amount
                    )
                )
                or (
                    hkd is not None
                    and (
                        (amount > 0 and hkd < 0)
                        or (amount < 0 and hkd > 0)
                        or (amount == 0 and hkd != 0)
                    )
                )
            ):
                raise ValueError("The audited recorded cash components are inconsistent")
        return self
