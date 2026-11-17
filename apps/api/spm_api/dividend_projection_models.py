"""Owner-visible retained projection records; these never grant economic use."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney, Quantity, Rate
from spm_api.dividend_eligibility import EligibilityRule
from spm_api.dividend_eligibility_service import TradeCreationAuditScope, TradeHistoryAuditScope
from spm_api.dividend_event_service import Reason, Version
from spm_api.dividend_projection import ProjectionState
from spm_api.dividend_tax_models import Ordinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.finance import Quality
from spm_api.master_models import Currency


class ProjectionProvenance(Model):
    effective_on: IsoDate
    selected_setting_id: UUID | None = Field(repr=False)
    observed_head_no: Ordinal
    observed_head_id: UUID | None = Field(repr=False)
    status: Literal["MISSING", "UNKNOWN", "ESTIMATED"]
    scope: Literal["OWNER_ESTIMATE_ONLY"]

    @model_validator(mode="after")
    def consistent_selection(self) -> Self:
        if (
            (self.observed_head_no == "0") != (self.observed_head_id is None)
            or (self.status == "MISSING") != (self.selected_setting_id is None)
            or (self.observed_head_no == "0" and self.selected_setting_id is not None)
        ):
            raise ValueError("The retained estimate selection is inconsistent")
        return self


class ProjectionAmounts(Model):
    currency: Currency
    gross_local: NonNegativeMoney = Field(repr=False)
    tax_local: NonNegativeMoney = Field(repr=False)
    fee_local: NonNegativeMoney = Field(repr=False)
    net_local: Money = Field(repr=False)
    rounding_delta_local: Money = Field(repr=False)
    estimated_hkd: Money | None = Field(repr=False)
    quality: Quality


class ProjectionRecord(Model):
    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_data_revision: Ordinal
    projection_no: PositiveOrdinal
    previous_id: UUID | None = Field(repr=False)
    state: ProjectionState
    as_of: IsoDate
    ex_date: IsoDate
    timing_scope: Literal[
        "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY", "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY"
    ]
    currency: Currency
    eligibility_rule: EligibilityRule
    retired_trade_scope: TradeHistoryAuditScope
    creation_audit_scope: TradeCreationAuditScope
    recorded_shares: Quantity | None = Field(repr=False)
    estimated_amounts: ProjectionAmounts | None = Field(repr=False)
    tax_rate: Rate | None = Field(repr=False)
    fee_original: NonNegativeMoney | None = Field(repr=False)
    fee_currency: Currency | None
    fee_rate_event: FxRate | None = Field(repr=False)
    fee_rate_source: str | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    fx_source: str | None = Field(repr=False)
    tax_provenance: ProjectionProvenance | None = Field(repr=False)
    fee_provenance: ProjectionProvenance | None = Field(repr=False)
    created_at: AwareDatetime
    reason: Reason = Field(repr=False)
    history_completeness: Literal["UNVERIFIED"]
    scope: Literal["UNVERIFIED_PROJECTION_HISTORY_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]

    @model_validator(mode="after")
    def consistent_record(self) -> Self:
        expected_timing = (
            "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY"
            if self.as_of < self.ex_date
            else "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY"
        )
        if (
            (self.projection_no == "1") != (self.previous_id is None)
            or self.previous_id == self.id
            or self.timing_scope != expected_timing
            or (self.state == "EXPECTED") != (self.estimated_amounts is not None)
            or (
                self.estimated_amounts is not None
                and self.estimated_amounts.currency != self.currency
            )
            or (self.estimated_amounts is not None and self.recorded_shares is None)
            or (
                self.estimated_amounts is not None
                and (
                    self.tax_rate is None or self.fee_original is None or self.fee_currency is None
                )
            )
            or (
                self.estimated_amounts is None
                and any(
                    value is not None
                    for value in (
                        self.recorded_shares,
                        self.tax_rate,
                        self.fee_original,
                        self.fee_currency,
                        self.fee_rate_event,
                        self.fee_rate_source,
                        self.fx_rate_hkd,
                        self.fx_source,
                    )
                )
            )
            or (self.fee_rate_event is None) != (self.fee_rate_source is None)
            or (self.fx_rate_hkd is None) != (self.fx_source is None)
        ):
            raise ValueError("The retained projection is inconsistent")
        return self
