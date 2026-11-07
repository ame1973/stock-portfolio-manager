"""Compare explicitly matched dividend evidence without merging, verifying or adopting it."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field
from pydantic_core import PydanticSerializationError

from spm_api.decimal_types import Dps, IsoDate, to_scaled
from spm_api.dividend_basis import BasisResult, SplitBasisAssertion, normalize_basis
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Currency
from spm_api.providers.base import (
    AdjustmentBasis,
    DividendObservation,
    EventType,
    OriginGroup,
    independent_vendor_origins,
)

Confidence = Literal["PROVISIONAL", "CORROBORATED", "CONFLICT", "INCOMPLETE"]
TermField = Literal[
    "ex_date",
    "announce_date",
    "record_date",
    "pay_date",
    "event_type",
    "currency",
    "dps",
    "adjustment_basis",
]
FIELDS: tuple[TermField, ...] = (
    "ex_date",
    "announce_date",
    "record_date",
    "pay_date",
    "event_type",
    "currency",
)


class DividendTerms(Model):
    """Proposed terms can remain incomplete; they are not a financial event revision."""

    model_config = ConfigDict(frozen=True)

    security_id: UUID
    ex_date: IsoDate | None = None
    announce_date: IsoDate | None = None
    record_date: IsoDate | None = None
    pay_date: IsoDate | None = None
    event_type: EventType = "UNKNOWN"
    dps: Dps | None = None
    currency: Currency | None = None
    adjustment_basis: AdjustmentBasis = "UNKNOWN"


class EvidenceMatch(Model):
    """A caller's explicit candidate association, not proof of matching or HTTP authority."""

    model_config = ConfigDict(frozen=True)

    candidate_id: UUID
    observation_id: UUID
    observation: DividendObservation = Field(repr=False)
    reason: Annotated[ChangeReason, Field(pattern=r"^[^\x00]+$")] = Field(repr=False)
    basis_assertion: SplitBasisAssertion | None = Field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    observation_id: UUID
    supporting_fields: frozenset[TermField]
    conflicting_fields: frozenset[TermField]
    incomplete_reasons: frozenset[str]
    comparison_dps: str | None
    comparison_basis: AdjustmentBasis
    basis_assertion_id: UUID | None


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    status: Confidence
    independent_origins: frozenset[OriginGroup]
    eligible_observation_ids: tuple[UUID, ...]
    conflicting_fields: frozenset[TermField]
    incomplete_reasons: frozenset[str]
    checks: tuple[EvidenceCheck, ...]


def _missing(terms: DividendTerms) -> frozenset[str]:
    reasons = {
        f"MISSING_{field.upper()}"
        for field in ("ex_date", "dps", "currency")
        if getattr(terms, field) is None
    }
    if terms.adjustment_basis != "AS_DECLARED":
        reasons.add("UNRESOLVED_ADJUSTMENT")
    return frozenset(reasons)


def _known(value: object) -> bool:
    return value is not None and value != "UNKNOWN"


def _amounts_agree(values: Sequence[str]) -> bool:
    scaled = [to_scaled(value, 8) for value in values]
    low, high = min(scaled), max(scaled)
    # Apply the absolute 1e-6 / relative 1e-4 tolerance in both directions, exactly.
    return (high - low) * 10_000 <= max(1_000_000, low)


def _source_terms(match: EvidenceMatch, basis: BasisResult) -> DividendTerms:
    fields = {field: getattr(match.observation, field) for field in DividendTerms.model_fields}
    return DividendTerms.model_validate(
        {**fields, "dps": basis.dps, "adjustment_basis": basis.adjustment_basis}
    )


def _check(
    terms: DividendTerms, match: EvidenceMatch, source: DividendTerms, basis: BasisResult
) -> EvidenceCheck:
    supporting: set[TermField] = set()
    conflicting: set[TermField] = set()
    for field in FIELDS:
        proposed, observed = getattr(terms, field), getattr(source, field)
        if _known(proposed) and _known(observed):
            (supporting if proposed == observed else conflicting).add(field)
    if terms.adjustment_basis == source.adjustment_basis == "AS_DECLARED":
        supporting.add("adjustment_basis")
        if (
            terms.currency is not None
            and terms.currency == source.currency
            and terms.dps is not None
            and source.dps is not None
        ):
            (supporting if _amounts_agree([terms.dps, source.dps]) else conflicting).add("dps")
    return EvidenceCheck(
        match.observation_id,
        frozenset(supporting),
        frozenset(conflicting),
        _missing(source) | basis.issues,
        source.dps,
        source.adjustment_basis,
        basis.assertion_id,
    )


def assess(
    candidate_id: UUID, terms: DividendTerms, matches: Sequence[EvidenceMatch]
) -> EvidenceAssessment:
    """Assess at most 100 explicitly associated assertions; never infer their event identity."""
    try:
        if not isinstance(candidate_id, UUID) or not isinstance(terms, DividendTerms):
            raise ValueError
        if len(matches) > 100 or any(not isinstance(match, EvidenceMatch) for match in matches):
            raise ValueError
        # Frozen models and model_copy are not validation of caller-supplied internal envelopes.
        terms = DividendTerms.model_validate(terms.model_dump(warnings="error"))
        checked = tuple(
            EvidenceMatch.model_validate(match.model_dump(warnings="error")) for match in matches
        )
        if len({match.observation_id for match in checked}) != len(checked) or any(
            match.candidate_id != candidate_id or match.observation.security_id != terms.security_id
            for match in checked
        ):
            raise ValueError
        assertions = [
            match.basis_assertion.id for match in checked if match.basis_assertion is not None
        ]
        if len(set(assertions)) != len(assertions):
            raise ValueError
        checked = tuple(sorted(checked, key=lambda match: str(match.observation_id)))
        bases = tuple(
            normalize_basis(match.observation_id, match.observation, match.basis_assertion)
            for match in checked
        )
        views = tuple(
            _source_terms(match, basis) for match, basis in zip(checked, bases, strict=True)
        )
    except (ValueError, TypeError, PydanticSerializationError):
        raise ValueError("INVALID_DIVIDEND_COMPARISON") from None

    sources = [terms, *views]
    conflicts: set[TermField] = {
        field
        for field in FIELDS
        if len({getattr(source, field) for source in sources if _known(getattr(source, field))}) > 1
    }
    amounts: dict[Currency, list[str]] = {}
    for source in sources:
        if (
            source.adjustment_basis == "AS_DECLARED"
            and source.currency is not None
            and source.dps is not None
        ):
            amounts.setdefault(source.currency, []).append(source.dps)
    if any(not _amounts_agree(values) for values in amounts.values()):
        conflicts.add("dps")

    missing = _missing(terms)
    checks = tuple(
        _check(terms, match, view, basis)
        for match, view, basis in zip(checked, views, bases, strict=True)
    )
    eligible = tuple(
        match
        for match, check in zip(checked, checks, strict=True)
        if not missing and not check.incomplete_reasons and not check.conflicting_fields
    )
    origins = independent_vendor_origins(match.observation.provider_id for match in eligible)
    issues = set(missing).union(*(check.incomplete_reasons for check in checks))
    if not checked:
        issues.add("NO_MATCHED_EVIDENCE")
    elif not eligible:
        issues.add("NO_SUPPORTING_EVIDENCE")
    status: Confidence = (
        "CONFLICT"
        if conflicts
        else "INCOMPLETE"
        if missing or not eligible
        else "CORROBORATED"
        if len(origins) >= 2
        else "PROVISIONAL"
    )
    return EvidenceAssessment(
        status,
        origins,
        tuple(match.observation_id for match in eligible),
        frozenset(conflicts),
        frozenset(issues),
        checks,
    )
