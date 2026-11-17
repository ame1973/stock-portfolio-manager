"""Exact recorded input pins, without current-use or eligibility authority."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from spm_api.decimal_types import IsoDate
from spm_api.dividend_eligibility import EligibilityRule
from spm_api.dividend_event_service import Version
from spm_api.dividend_tax_models import Ordinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.master_models import Currency


class ProjectionInputSelection(Model):
    effective_on: IsoDate
    head_no: Ordinal
    head_id: UUID | None = Field(repr=False)
    selected_id: UUID | None = Field(repr=False)
    status: Literal["MISSING", "UNKNOWN", "ESTIMATED"]
    selected_scope: Literal["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]
    scope: Literal["OWNER_ESTIMATE_ONLY"]

    @model_validator(mode="after")
    def consistent_selection(self) -> Self:
        if (
            (self.head_no == "0") != (self.head_id is None)
            or (self.status == "MISSING") != (self.selected_id is None)
            or (self.selected_scope == "NO_SELECTED_SETTING") != (self.selected_id is None)
            or (self.head_no == "0" and self.selected_id is not None)
        ):
            raise ValueError("The recorded estimate selection is inconsistent")
        return self


class ProjectionInputs(Model):
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    ex_date: IsoDate
    currency: Currency
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_data_revision: Ordinal
    projection_head_no: Ordinal
    projection_head_id: UUID | None = Field(repr=False)
    as_of: IsoDate
    eligibility_rule: EligibilityRule
    tax: ProjectionInputSelection = Field(repr=False)
    fee: ProjectionInputSelection = Field(repr=False)
    rejection_state: Literal["NOT_REJECTED", "REJECTED"]
    scope: Literal["RECORDED_PROJECTION_INPUTS_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    source_scope: Literal["CURRENT_SOURCE_NOT_REVALIDATED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]

    @model_validator(mode="after")
    def consistent_head(self) -> Self:
        if (self.projection_head_no == "0") != (self.projection_head_id is None):
            raise ValueError("The recorded projection head is inconsistent")
        return self
