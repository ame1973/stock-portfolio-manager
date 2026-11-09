"""Explicit basis pins and private-safe historical declaration DTOs, never adoption grants."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from spm_api.action_models import JsonRatio
from spm_api.cited_models import CitationInput
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.errors import Model
from spm_api.observation_models import PublicCitation


class BasisActionPin(Model):
    model_config = ConfigDict(frozen=True)

    action_id: UUID
    version: Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]


class DividendBasisCreate(Model):
    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    source_sha256: Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
    share_basis_date: IsoDate
    source_basis: Literal["AFTER_EFFECTIVE_DATE_ACTIONS"]
    target_basis: Literal["PRE_EX_DATE_EVENTS"]
    complete: Annotated[bool, Field(strict=True)]
    split_only: Annotated[bool, Field(strict=True)]
    actions: Annotated[tuple[BasisActionPin, ...], Field(max_length=100)]
    citation: CitationInput = Field(repr=False)

    @field_validator("actions")
    @classmethod
    def unique_pins(cls, value: tuple[BasisActionPin, ...]) -> tuple[BasisActionPin, ...]:
        if len({pin.action_id for pin in value}) != len(value):
            raise ValueError("An adopted action can be selected only once")
        return tuple(sorted(value, key=lambda pin: str(pin.action_id)))

    @model_validator(mode="after")
    def explicit_scope(self) -> Self:
        if not self.complete or not self.split_only:
            raise ValueError("Declare the complete split-only scope explicitly")
        return self


class BasisActionSnapshot(Model):
    action_id: UUID
    action_root_id: UUID
    security_id: UUID
    observed_version: int
    action_type: Literal["SPLIT", "REVERSE_SPLIT"]
    effective_date: IsoDate
    ratio_numerator: JsonRatio
    ratio_denominator: JsonRatio
    observed_status: Literal["CONFIRMED"]
    observed_lifecycle_status: Literal["CURRENT"]


class DividendBasisRecord(Model):
    id: UUID
    observation_id: UUID
    source_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    security_id: UUID
    ex_date: IsoDate
    share_basis_date: IsoDate
    source_basis: Literal["AFTER_EFFECTIVE_DATE_ACTIONS"]
    target_basis: Literal["PRE_EX_DATE_EVENTS"]
    complete: Literal[True]
    split_only: Literal[True]
    actions: tuple[BasisActionSnapshot, ...]
    citation: PublicCitation
    created_at: AwareDatetime
    scope: Literal["BASIS_DECLARATION_ONLY"] = "BASIS_DECLARATION_ONLY"
    canonical_adoption: Literal["NOT_ADOPTED"] = "NOT_ADOPTED"
    current_use_scope: Literal["REVALIDATION_REQUIRED_FOR_NEW_ADOPTION"] = (
        "REVALIDATION_REQUIRED_FOR_NEW_ADOPTION"
    )
    market_history: Literal["OWNER_ASSERTED_NOT_VERIFIED"] = "OWNER_ASSERTED_NOT_VERIFIED"
