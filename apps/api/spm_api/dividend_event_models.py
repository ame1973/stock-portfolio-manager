"""Owner event-decision views; retained evidence does not prove a financial entitlement."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from spm_api.action_models import EvidenceUrl
from spm_api.candidate_models import CandidateAssessment
from spm_api.decimal_types import Dps, IsoDate
from spm_api.dividend_event_service import (
    DecisionKind,
    EventEvidence,
    Reason,
    ReviewStatus,
    Version,
)
from spm_api.errors import Model
from spm_api.master_models import Currency
from spm_api.providers.base import AdjustmentBasis, CitationReference, EventType


class ManualEventCreate(Model):
    security_id: UUID = Field(repr=False)
    event_type: EventType
    ex_date: IsoDate
    announce_date: IsoDate | None = None
    record_date: IsoDate | None = None
    pay_date: IsoDate | None = None
    dps: Dps = Field(repr=False)
    currency: Currency
    adjustment_basis: AdjustmentBasis
    citation: CitationReference = Field(repr=False)
    source_url: EvidenceUrl | None = Field(default=None, repr=False)
    reason: Reason = Field(repr=False)


class ManualEventRevision(ManualEventCreate):
    revision_id: UUID = Field(repr=False)
    confirm_unlock: bool = Field(default=False, strict=True)
    confirm_reopen: bool = Field(default=False, strict=True)


class CandidateEventSelection(Model):
    candidate_id: UUID = Field(repr=False)
    candidate_revision_id: UUID = Field(repr=False)
    candidate_version: Version
    reason: Reason = Field(repr=False)


class CandidateEventCreate(CandidateEventSelection):
    confirmation: Literal["CREATE_DISTINCT_EVENT"]


class CandidateEventRevision(CandidateEventSelection):
    revision_id: UUID = Field(repr=False)
    confirmation: Literal["MATCH_EXISTING_EVENT"]


class DividendWithdrawal(Model):
    revision_id: UUID = Field(repr=False)
    confirmation: Literal["WITHDRAW_EVENT"]
    citation: CitationReference = Field(repr=False)
    source_url: EvidenceUrl | None = Field(default=None, repr=False)
    reason: Reason = Field(repr=False)


class AcceptDividendRevision(Model):
    revision_id: UUID = Field(repr=False)
    decision: Literal["ACCEPT_REVISION"]
    citation: CitationReference = Field(repr=False)
    evidence_url: EvidenceUrl | None = Field(default=None, repr=False)
    reason: Reason = Field(repr=False)


class EventReference(Model):
    citation: CitationReference = Field(repr=False)
    url: EvidenceUrl | None = Field(repr=False)
    url_withheld: bool


class EventFields(Model):
    security_id: UUID = Field(repr=False)
    event_type: EventType
    ex_date: IsoDate
    announce_date: IsoDate | None
    record_date: IsoDate | None
    pay_date: IsoDate | None
    dps: Dps = Field(repr=False)
    currency: Currency
    adjustment_basis: AdjustmentBasis
    workflow_status: ReviewStatus
    manual_locked: bool
    decision_kind: DecisionKind
    candidate_revision_id: UUID | None = Field(repr=False)
    independent_origin_groups: Annotated[int, Field(ge=0)] | None
    assessment: CandidateAssessment | None = Field(repr=False)
    evidence: tuple[EventEvidence, ...] = Field(repr=False)
    reference: EventReference | None = Field(repr=False)
    reason: Reason = Field(repr=False)
    created_at: AwareDatetime
    scope: Literal["OWNER_EVENT_DECISION_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    document_verification: Literal["NOT_ESTABLISHED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]


class DividendEventRecord(EventFields):
    id: UUID = Field(repr=False)
    current_revision_id: UUID = Field(repr=False)
    version: Version


class DividendEventRevisionRecord(EventFields):
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
