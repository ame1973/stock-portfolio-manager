"""Explicit candidate review DTOs; comparison is historical and never canonical income."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator

from spm_api.decimal_types import Dps
from spm_api.dividend_candidate_service import CandidateDecision, CandidateMatch, Reason, Version
from spm_api.dividend_evidence import Confidence, DividendTerms, TermField
from spm_api.errors import Model
from spm_api.providers.base import AdjustmentBasis, OriginGroup


class CandidateWrite(Model):
    """Full explicit review intent; owner, candidate and revision IDs are server-owned."""

    model_config = ConfigDict(frozen=True)

    state: Literal["OPEN", "DISMISSED"]
    terms: DividendTerms
    matches: Annotated[tuple[CandidateMatch, ...], Field(max_length=100)]
    change_reason: Reason

    @field_validator("matches")
    @classmethod
    def unique_sorted_matches(cls, value: tuple[CandidateMatch, ...]) -> tuple[CandidateMatch, ...]:
        return CandidateDecision.unique_sorted_matches(value)


class CandidateEvidenceCheck(Model):
    observation_id: UUID
    supporting_fields: tuple[TermField, ...]
    conflicting_fields: tuple[TermField, ...]
    incomplete_reasons: tuple[str, ...]
    comparison_dps: Dps | None
    comparison_basis: AdjustmentBasis
    basis_assertion_id: UUID | None


class CandidateAssessment(Model):
    status: Confidence
    independent_origins: tuple[OriginGroup, ...]
    eligible_observation_ids: tuple[UUID, ...]
    conflicting_fields: tuple[TermField, ...]
    incomplete_reasons: tuple[str, ...]
    checks: tuple[CandidateEvidenceCheck, ...]


class CandidateRecord(Model):
    id: UUID
    revision_id: UUID
    version: Version
    state: Literal["OPEN", "DISMISSED"]
    terms: DividendTerms
    matches: tuple[CandidateMatch, ...]
    change_reason: Reason
    created_at: AwareDatetime
    assessment: CandidateAssessment
    scope: Literal["CANDIDATE_REVIEW_ONLY"] = "CANDIDATE_REVIEW_ONLY"
    canonical_adoption: Literal["NOT_ADOPTED"] = "NOT_ADOPTED"
    canonical_adoption_scope: Literal["RESPONSE_ONLY_NOT_LINKAGE"] = Field(
        default="RESPONSE_ONLY_NOT_LINKAGE",
        description="The marker describes this review response, not current or historical links.",
    )
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
