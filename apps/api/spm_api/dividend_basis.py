"""Exact comparison-only share-basis derivation from an explicit complete split assertion."""

from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticSerializationError

from spm_api.action_models import EvidenceUrl, Ratio
from spm_api.decimal_types import INT64_MAX, IsoDate, from_scaled, to_scaled
from spm_api.errors import Model
from spm_api.master_models import ChangeReason
from spm_api.providers.base import (
    AdjustmentBasis,
    CitationReference,
    DividendObservation,
    EvidenceCitation,
)


class SplitReference(Model):
    """An asserted current adopted-action snapshot; a future writer must check its real row."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    root_id: UUID
    security_id: UUID
    version: Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
    action_type: Literal["SPLIT", "REVERSE_SPLIT"]
    effective_date: IsoDate
    ratio_numerator: Ratio
    ratio_denominator: Ratio
    status: Literal["CONFIRMED"]
    lifecycle_status: Literal["CURRENT"]

    @model_validator(mode="after")
    def direction(self) -> Self:
        if (self.action_type == "SPLIT" and self.ratio_numerator <= self.ratio_denominator) or (
            self.action_type == "REVERSE_SPLIT" and self.ratio_numerator >= self.ratio_denominator
        ):
            raise ValueError("The adopted split direction must match its exact rational terms")
        return self


class SplitBasisAssertion(EvidenceCitation):
    """A cited declaration, not authorization, market completeness or adopted-action proof."""

    citation: CitationReference = Field(repr=False)
    reason: ChangeReason = Field(repr=False)
    url: EvidenceUrl | None = Field(default=None, repr=False)
    observation_id: UUID
    source_sha256: Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
    security_id: UUID
    ex_date: IsoDate
    share_basis_date: IsoDate
    source_basis: Literal["AFTER_EFFECTIVE_DATE_ACTIONS"]
    target_basis: Literal["PRE_EX_DATE_EVENTS"]
    complete: Annotated[bool, Field(strict=True)]
    split_only: Annotated[bool, Field(strict=True)]
    splits: Annotated[tuple[SplitReference, ...], Field(max_length=100)]

    @field_validator("reason")
    @classmethod
    def reason_without_nul(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("The basis explanation cannot contain NUL")
        return value

    @model_validator(mode="after")
    def complete_scope(self) -> Self:
        if not self.complete or not self.split_only:
            raise ValueError("Declare complete split-only basis evidence explicitly")
        if len({split.id for split in self.splits}) != len(self.splits) or len(
            {split.root_id for split in self.splits}
        ) != len(self.splits):
            raise ValueError("An action or its alternate revision cannot appear twice")
        for split in self.splits:
            day = split.effective_date
            in_window = (
                self.ex_date <= day <= self.share_basis_date
                if self.share_basis_date >= self.ex_date
                else self.share_basis_date < day < self.ex_date
            )
            if split.security_id != self.security_id or not in_window:
                raise ValueError("The action must belong to the exact asserted basis interval")
        return self


@dataclass(frozen=True, slots=True)
class BasisResult:
    dps: str | None
    adjustment_basis: AdjustmentBasis
    issues: frozenset[str]
    assertion_id: UUID | None


def normalize_basis(
    observation_id: UUID,
    observation: DividendObservation,
    assertion: SplitBasisAssertion | None = None,
) -> BasisResult:
    """Derive exact pre-ex-date units; never mutate an observation or infer missing basis facts."""
    try:
        if not isinstance(observation_id, UUID) or not isinstance(observation, DividendObservation):
            raise ValueError
        source = DividendObservation.model_validate(observation.model_dump(warnings="error"))
        if assertion is not None:
            if not isinstance(assertion, SplitBasisAssertion):
                raise ValueError
            assertion = SplitBasisAssertion.model_validate(assertion.model_dump(warnings="error"))
            if (
                source.adjustment_basis != "SPLIT_ADJUSTED"
                or assertion.observation_id != observation_id
                or assertion.source_sha256 != source.normalized_sha256
                or assertion.security_id != source.security_id
                or assertion.ex_date != source.ex_date
            ):
                raise ValueError
    except (ValueError, TypeError, PydanticSerializationError):
        raise ValueError("INVALID_DIVIDEND_BASIS_ASSERTION") from None

    identifier = assertion.id if assertion is not None else None
    if source.adjustment_basis == "AS_DECLARED":
        return BasisResult(source.dps, "AS_DECLARED", frozenset(), None)
    unresolved = frozenset({"UNRESOLVED_ADJUSTMENT"})
    if assertion is None or source.dps is None:
        return BasisResult(source.dps, source.adjustment_basis, unresolved, identifier)

    # Dated source units include that day's actions; eligibility is strictly pre-ex-date.
    scaled = Fraction(to_scaled(source.dps, 8))
    for split in assertion.splits:
        factor = Fraction(split.ratio_numerator, split.ratio_denominator)
        scaled = (
            scaled * factor if assertion.share_basis_date >= assertion.ex_date else scaled / factor
        )
    issue = (
        "NORMALIZED_DPS_OVERFLOW"
        if scaled > INT64_MAX
        else "NORMALIZED_DPS_PRECISION"
        if scaled.denominator != 1
        else None
    )
    if issue is not None:
        return BasisResult(source.dps, "SPLIT_ADJUSTED", unresolved | {issue}, identifier)
    return BasisResult(from_scaled(scaled.numerator, 8), "AS_DECLARED", frozenset(), identifier)
