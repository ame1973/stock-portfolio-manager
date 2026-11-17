"""Explicit wire pins for a recorded projection, not a financial grant."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spm_api.decimal_types import FxRate, IsoDate
from spm_api.dividend_eligibility import EligibilityRule
from spm_api.dividend_event_service import Reason
from spm_api.dividend_projection_writer import Source
from spm_api.dividend_tax_models import AdvanceableOrdinal, Ordinal, PositiveOrdinal
from spm_api.errors import Model


class ProjectionWrite(Model):
    revision_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    expected_data_revision: Ordinal
    expected_projection_no: AdvanceableOrdinal
    expected_projection_id: UUID | None = Field(repr=False)
    as_of: IsoDate
    eligibility_rule: EligibilityRule
    tax_effective_on: IsoDate
    expected_tax_head_no: Ordinal
    expected_tax_head_id: UUID | None = Field(repr=False)
    expected_tax_selected_id: UUID | None = Field(repr=False)
    fee_effective_on: IsoDate
    expected_fee_head_no: Ordinal
    expected_fee_head_id: UUID | None = Field(repr=False)
    expected_fee_selected_id: UUID | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    fx_source: Source | None = Field(repr=False)
    fee_rate_event: FxRate | None = Field(repr=False)
    fee_rate_source: Source | None = Field(repr=False)
    confirmation: Literal["RECORDED_PROJECTION_ONLY"]
    confirm_revision_fence: Literal[True] = Field(
        description=(
            "A retained projection currently blocks event revision, withdrawal and rejection "
            "until atomic reconciliation is available."
        )
    )
    reason: Reason = Field(repr=False)

    @field_validator("confirm_revision_fence", mode="before")
    @classmethod
    def confirm_fence(cls, value: object) -> object:
        if value is not True:
            raise ValueError("The event revision fence must be explicitly acknowledged")
        return value

    @field_validator("fx_source", "fee_rate_source")
    @classmethod
    def canonical_source(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("A source must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def complete_pins(self) -> Self:
        for number, head, selected in (
            (self.expected_projection_no, self.expected_projection_id, None),
            (self.expected_tax_head_no, self.expected_tax_head_id, self.expected_tax_selected_id),
            (self.expected_fee_head_no, self.expected_fee_head_id, self.expected_fee_selected_id),
        ):
            if (number == "0") != (head is None) or (number == "0" and selected is not None):
                raise ValueError("The observed projection inputs are incomplete")
        if (self.fx_rate_hkd is None) != (self.fx_source is None) or (
            self.fee_rate_event is None
        ) != (self.fee_rate_source is None):
            raise ValueError("An exchange rate and its source must be supplied together")
        return self
