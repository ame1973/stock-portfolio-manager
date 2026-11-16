"""String-exact owner fee-estimate contracts; they do not confirm a charge."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.decimal_types import IsoDate, NonNegativeMoney
from spm_api.dividend_event_service import Reason, Version
from spm_api.dividend_tax_models import AdvanceableOrdinal, Ordinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.master_models import Currency


class FeeSettingWrite(Model):
    revision_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    expected_head_no: AdvanceableOrdinal
    expected_head_id: UUID | None = Field(repr=False)
    effective_from: IsoDate
    fee_status: Literal["ESTIMATED", "UNKNOWN"]
    fee: NonNegativeMoney | None = Field(repr=False)
    fee_currency: Currency | None
    reason: Reason = Field(repr=False)

    @model_validator(mode="after")
    def explicit_decision(self) -> Self:
        if (self.expected_head_no == "0") != (self.expected_head_id is None):
            raise ValueError("The observed setting head is incomplete")
        if self.fee_status == "ESTIMATED":
            if self.fee is None or self.fee_currency is None:
                raise ValueError("An estimated fee requires an amount and currency")
        elif self.fee is not None:
            raise ValueError("An unknown fee cannot supply an amount")
        return self


class FeeSettingRecord(Model):
    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    setting_no: PositiveOrdinal
    previous_id: UUID | None = Field(repr=False)
    effective_from: IsoDate
    fee_status: Literal["ESTIMATED", "UNKNOWN"]
    fee: NonNegativeMoney | None = Field(repr=False)
    fee_currency: Currency | None
    reason: Reason = Field(repr=False)
    created_at: AwareDatetime
    scope: Literal["OWNER_ESTIMATE_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]

    @model_validator(mode="after")
    def consistent_record(self) -> Self:
        if (self.setting_no == "1") != (self.previous_id is None):
            raise ValueError("The setting predecessor is inconsistent")
        if self.fee_status == "ESTIMATED":
            if self.fee is None or self.fee_currency is None:
                raise ValueError("The estimated fee is incomplete")
        elif self.fee is not None:
            raise ValueError("The unknown fee cannot contain an amount")
        return self


class FeeSettingSelection(Model):
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    effective_on: IsoDate
    head_no: Ordinal
    head_id: UUID | None = Field(repr=False)
    fee_status: Literal["MISSING", "UNKNOWN", "ESTIMATED"]
    selected: FeeSettingRecord | None = Field(repr=False)
    selected_scope: Literal["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]
    revision_rejection: Literal["NOT_REJECTED", "REJECTED"]
    scope: Literal["OWNER_ESTIMATE_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]

    @model_validator(mode="after")
    def consistent_selection(self) -> Self:
        if (self.head_no == "0") != (self.head_id is None):
            raise ValueError("The observed setting head is incomplete")
        if self.selected is None:
            if self.fee_status != "MISSING" or self.selected_scope != "NO_SELECTED_SETTING":
                raise ValueError("The missing selection is inconsistent")
        elif (
            self.fee_status != self.selected.fee_status
            or int(self.selected.setting_no) > int(self.head_no)
            or (self.selected.setting_no == self.head_no) != (self.selected.id == self.head_id)
            or self.selected.effective_from > self.effective_on
            or (self.selected.event_id, self.selected.revision_id, self.selected.revision_no)
            != (self.event_id, self.revision_id, self.revision_no)
            or self.selected.account_id != self.account_id
            or self.selected_scope
            != (
                "ACCOUNT_SCOPE_MATCH"
                if (
                    self.selected.portfolio_id,
                    self.selected.broker_id,
                    self.selected.account_version,
                    self.selected.broker_version,
                )
                == (self.portfolio_id, self.broker_id, self.account_version, self.broker_version)
                else "ACCOUNT_SCOPE_CHANGED"
            )
        ):
            raise ValueError("The selected setting is inconsistent")
        return self
