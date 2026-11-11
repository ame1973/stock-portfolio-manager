"""String-exact owner tax-estimate contracts; these do not confirm withholding."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, model_validator

from spm_api.decimal_types import INT64_MAX, IsoDate, Rate
from spm_api.dividend_event_service import Reason, Version
from spm_api.errors import Model


def _ordinal(value: str) -> str:
    number = int(value)
    if str(number) != value or number > INT64_MAX:
        raise ValueError("A canonical INT64 decimal string is required")
    return value


def _positive(value: str) -> str:
    if value == "0":
        raise ValueError("A positive version is required")
    return value


def _advanceable(value: str) -> str:
    if int(value) == INT64_MAX:
        raise ValueError("The setting version cannot advance")
    return value


Ordinal = Annotated[
    str,
    StringConstraints(strict=True, max_length=19, pattern=r"^(0|[1-9][0-9]{0,18})$"),
    AfterValidator(_ordinal),
]
PositiveOrdinal = Annotated[Ordinal, AfterValidator(_positive)]
AdvanceableOrdinal = Annotated[Ordinal, AfterValidator(_advanceable)]


class TaxSettingWrite(Model):
    revision_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    expected_head_no: AdvanceableOrdinal
    expected_head_id: UUID | None = Field(repr=False)
    effective_from: IsoDate
    rate: Rate | None = Field(repr=False)
    reason: Reason = Field(repr=False)

    @model_validator(mode="after")
    def explicit_head(self) -> Self:
        if (self.expected_head_no == "0") != (self.expected_head_id is None):
            raise ValueError("The observed setting head is incomplete")
        return self


class TaxSettingRecord(Model):
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
    tax_status: Literal["ESTIMATED", "UNKNOWN"]
    rate: Rate | None = Field(repr=False)
    reason: Reason = Field(repr=False)
    created_at: AwareDatetime
    scope: Literal["OWNER_ESTIMATE_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]

    @model_validator(mode="after")
    def consistent_record(self) -> Self:
        if (self.setting_no == "1") != (self.previous_id is None):
            raise ValueError("The setting predecessor is inconsistent")
        if (self.tax_status == "ESTIMATED") != (self.rate is not None):
            raise ValueError("The setting rate is inconsistent")
        return self


class TaxSettingSelection(Model):
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
    tax_status: Literal["MISSING", "UNKNOWN", "ESTIMATED"]
    selected: TaxSettingRecord | None = Field(repr=False)
    selected_scope: Literal["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]
    scope: Literal["OWNER_ESTIMATE_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]

    @model_validator(mode="after")
    def consistent_selection(self) -> Self:
        if (self.head_no == "0") != (self.head_id is None):
            raise ValueError("The observed setting head is incomplete")
        if self.selected is None:
            if self.tax_status != "MISSING" or self.selected_scope != "NO_SELECTED_SETTING":
                raise ValueError("The missing selection is inconsistent")
        elif (
            self.tax_status != self.selected.tax_status
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
