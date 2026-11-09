"""Read owned, audited dividend-event history without granting current adoption."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_candidate_service as candidates
from spm_api.action_models import EvidenceUrl
from spm_api.audit import canonical, digest, stamp
from spm_api.decimal_types import from_scaled, to_scaled
from spm_api.dividend_evidence import DividendTerms, EvidenceAssessment, TermField
from spm_api.errors import ApiError, Model
from spm_api.master_models import ChangeReason
from spm_api.providers.base import CitationReference

Version = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
Reason = Annotated[ChangeReason, Field(pattern=r"^[^\x00]+$")]
ReviewStatus = Literal[
    "PROVISIONAL", "CORROBORATED", "VERIFIED", "CONFLICT", "INCOMPLETE", "WITHDRAWN"
]
DecisionKind = Literal["CANDIDATE", "MANUAL", "VERIFY", "WITHDRAW"]


class EventError(ValueError):
    """Fixed codes only; do not expose source records, references or retained decisions."""


class EventEvidence(Model):
    """Fields supported by one explicitly associated original source observation."""

    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    fields: Annotated[tuple[TermField, ...], Field(max_length=8)]

    @field_validator("fields")
    @classmethod
    def sorted_unique(cls, value: tuple[TermField, ...]) -> tuple[TermField, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Evidence fields must be unique")
        return tuple(sorted(value))


class EventRevision(Model):
    """Private historical decision; it does not authorize HTTP or current economic use."""

    model_config = ConfigDict(frozen=True)

    owner_id: UUID = Field(repr=False)
    event_id: UUID
    revision_id: UUID
    revision_no: Version
    terms: DividendTerms
    review_status: ReviewStatus
    manual_locked: bool = Field(strict=True)
    decision_kind: DecisionKind
    candidate_revision_id: UUID | None
    citation: CitationReference | None = Field(repr=False)
    source_url: EvidenceUrl | None = Field(repr=False)
    reason: Reason = Field(repr=False)
    request_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True)]
    evidence: Annotated[tuple[EventEvidence, ...], Field(max_length=100)] = Field(repr=False)

    @model_validator(mode="after")
    def complete_identity_and_links(self) -> "EventRevision":
        if self.terms.ex_date is None or self.terms.dps is None or self.terms.currency is None:
            raise ValueError("An event requires explicit ex-date, DPS and currency")
        if (
            tuple(sorted(self.evidence, key=lambda value: str(value.observation_id)))
            != self.evidence
        ):
            raise ValueError("Evidence must be sorted by original observation identity")
        if len({value.observation_id for value in self.evidence}) != len(self.evidence):
            raise ValueError("Evidence observation identities must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ReviewedEvent:
    record: EventRevision = field(repr=False)
    assessment: EvidenceAssessment | None = field(repr=False)


def _scope(owner_id: UUID, identifier: UUID, version: int | None) -> None:
    if (
        not isinstance(owner_id, UUID)
        or not isinstance(identifier, UUID)
        or (version is not None and (type(version) is not int or not 1 <= version <= 2_147_483_647))
    ):
        raise EventError("INVALID_DIVIDEND_EVENT_SCOPE")


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("Expected canonical UUID")
    result = UUID(value)
    if str(result) != value:
        raise ValueError("Expected canonical UUID")
    return result


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected canonical UTC timestamp")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None or stamp(result.astimezone(UTC)) != value:
        raise ValueError("Expected canonical UTC timestamp")
    return result


def _audit_values(record: EventRevision) -> dict[str, object]:
    citation = {"citation": record.citation, "source_url": record.source_url}
    return {
        "event_id": str(record.event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "terms": record.terms.model_dump(mode="json"),
        "review_status": record.review_status,
        "manual_locked": record.manual_locked,
        "decision_kind": record.decision_kind,
        "candidate_revision_id": (
            str(record.candidate_revision_id) if record.candidate_revision_id is not None else None
        ),
        "citation_sha256": (
            digest(canonical(citation))
            if record.citation is not None or record.source_url
            else None
        ),
        "evidence": [item.model_dump(mode="json") for item in record.evidence],
        "created_at": stamp(record.created_at.astimezone(UTC)),
    }


def _owned_parent(con: Connection, owner_id: UUID, identifier: UUID) -> RowMapping | None:
    row = (
        con.execute(
            text("""
        SELECT e.*,c.owner_id AS control_owner_id,c.request_id AS control_request_id,
               c.created_at AS control_created_at
        FROM dividend_events e LEFT JOIN dividend_event_controls c ON c.event_id=e.id
        WHERE e.id=:id
    """),
            {"id": str(identifier)},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row["control_owner_id"] != str(owner_id):
        retained = con.execute(
            text("""
            SELECT 1 FROM dividend_event_revisions r JOIN audit_events a ON a.entity_id=r.id
            WHERE r.event_id=:id AND a.owner_id=:owner AND a.entity_type='dividend_event_revision'
              AND a.action='DIVIDEND_EVENT_REVISED' LIMIT 1
        """),
            {"id": str(identifier), "owner": str(owner_id)},
        ).first()
        if retained is None:
            return None
        raise ValueError("Inconsistent retained ownership")
    _uuid(row["security_id"])
    _uuid(row["control_request_id"])
    _uuid(row["current_revision_id"])
    if (
        type(row["provenance_version"]) is not int
        or row["provenance_version"] != 1
        or _uuid(row["id"]) != identifier
        or _uuid(row["control_owner_id"]) != owner_id
        or row["event_identity_key"] != row["id"]
        or _time(row["created_at"]) != _time(row["control_created_at"])
        or type(row["version"]) is not int
        or not 1 <= row["version"] <= 2_147_483_647
    ):
        raise ValueError("Inconsistent event control")
    return row


def _history_scope(con: Connection, parent: RowMapping) -> None:
    summary = con.execute(
        text("""
        SELECT count(*) AS total,min(r.revision_no) AS first_version,
          max(r.revision_no) AS last_version,
          sum(CASE WHEN typeof(r.revision_no)!='integer' THEN 1 ELSE 0 END)
                AS invalid_revisions,
          sum(CASE WHEN d.revision_id IS NULL OR d.event_id IS NOT :id
                OR d.owner_id IS NOT :owner OR d.created_at IS NOT r.created_at THEN 1 ELSE 0 END)
                AS invalid_decisions,
          max(CASE WHEN r.revision_no=1 THEN d.request_id END) AS first_request,
          sum(CASE WHEN (SELECT count(*) FROM audit_events a
                WHERE a.entity_type='dividend_event_revision' AND a.entity_id=r.id
                  AND a.action='DIVIDEND_EVENT_REVISED')!=1 THEN 1 ELSE 0 END) AS invalid_audits
        FROM dividend_event_revisions r
        LEFT JOIN dividend_revision_decisions d ON d.revision_id=r.id
        WHERE r.event_id=:id
    """),
        {"id": parent["id"], "owner": parent["control_owner_id"]},
    ).one()
    current = con.execute(
        text("""
        SELECT id,review_status FROM dividend_event_revisions
        WHERE event_id=:id AND revision_no=:version
    """),
        {"id": parent["id"], "version": parent["version"]},
    ).first()
    if (
        summary.total != parent["version"]
        or summary.first_version != 1
        or summary.last_version != summary.total
        or summary.invalid_revisions != 0
        or summary.invalid_decisions != 0
        or summary.invalid_audits != 0
        or summary.first_request != parent["control_request_id"]
        or current is None
        or current.id != parent["current_revision_id"]
        or current.review_status != parent["workflow_status"]
    ):
        raise ValueError("Inconsistent event history scope")


def _links(con: Connection, revision_id: str) -> tuple[EventEvidence, ...]:
    rows = con.execute(
        text("""
        SELECT observation_id,field_names_json FROM dividend_evidence_links
        WHERE revision_id=:revision ORDER BY observation_id LIMIT 101
    """),
        {"revision": revision_id},
    ).all()
    if len(rows) > 100:
        raise ValueError("Too many evidence links")
    result: list[EventEvidence] = []
    for observation_id, wire in rows:
        if not isinstance(wire, str) or len(wire.encode("utf-8")) > 4096:
            raise ValueError("Invalid evidence fields")
        fields = json.loads(wire)
        if not isinstance(fields, list):
            raise ValueError("Invalid evidence fields")
        item = EventEvidence(observation_id=_uuid(observation_id), fields=tuple(fields))
        if canonical(list(item.fields)) != wire:
            raise ValueError("Noncanonical evidence fields")
        result.append(item)
    return tuple(result)


def _record(con: Connection, parent: RowMapping, version: int) -> EventRevision:
    row = (
        con.execute(
            text("""
        SELECT r.*,d.revision_id AS decision_revision_id,d.event_id AS decision_event_id,
          d.owner_id AS decision_owner_id,d.decision_kind,d.candidate_revision_id,d.evidence_count,
          d.citation,d.source_url,d.request_id,d.created_at AS decision_created_at
        FROM dividend_event_revisions r
        JOIN dividend_revision_decisions d ON d.revision_id=r.id
        WHERE r.event_id=:event AND r.revision_no=:version
    """),
            {"event": parent["id"], "version": version},
        )
        .mappings()
        .one()
    )
    if type(row["manual_locked"]) is not int or row["manual_locked"] not in (0, 1):
        raise ValueError("Invalid manual lock")
    links = _links(con, row["id"])
    record = EventRevision(
        owner_id=_uuid(row["decision_owner_id"]),
        event_id=_uuid(row["event_id"]),
        revision_id=_uuid(row["id"]),
        revision_no=row["revision_no"],
        terms=DividendTerms(
            security_id=_uuid(parent["security_id"]),
            event_type=row["event_type"],
            ex_date=row["ex_date"],
            announce_date=row["announce_date"],
            record_date=row["record_date"],
            pay_date=row["pay_date"],
            dps=from_scaled(row["dps_e8"], 8),
            currency=row["currency"],
            adjustment_basis=row["adjustment_basis"],
        ),
        review_status=row["review_status"],
        manual_locked=bool(row["manual_locked"]),
        decision_kind=row["decision_kind"],
        candidate_revision_id=(
            _uuid(row["candidate_revision_id"])
            if row["candidate_revision_id"] is not None
            else None
        ),
        citation=row["citation"],
        source_url=row["source_url"],
        reason=row["reason"],
        request_id=_uuid(row["request_id"]),
        created_at=_time(row["created_at"]),
        evidence=links,
    )
    if (
        str(record.owner_id) != parent["control_owner_id"]
        or str(record.event_id) != parent["id"]
        or str(record.revision_id) != row["decision_revision_id"]
        or str(record.event_id) != row["decision_event_id"]
        or record.created_at < _time(parent["created_at"])
        or stamp(record.created_at.astimezone(UTC)) != row["decision_created_at"]
        or record.reason != row["reason"]
        or record.citation != row["citation"]
        or record.source_url != row["source_url"]
        or to_scaled(record.terms.dps or "", 8) != row["dps_e8"]
        or type(row["evidence_count"]) is not int
        or row["evidence_count"] != len(links)
    ):
        raise ValueError("Inconsistent event revision mirrors")
    return record


def _lineage(con: Connection, record: EventRevision) -> EvidenceAssessment | None:
    if record.decision_kind == "CANDIDATE":
        if record.candidate_revision_id is None or record.manual_locked:
            raise ValueError("Invalid candidate decision")
        row = (
            con.execute(
                text("""
            SELECT candidate_id,revision_no FROM dividend_candidate_revisions WHERE id=:id
        """),
                {"id": str(record.candidate_revision_id)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("Missing candidate revision")
        candidate = candidates.get(
            con, record.owner_id, _uuid(row["candidate_id"]), version=row["revision_no"]
        )
        if candidate is None:
            raise ValueError("Missing audited candidate history")
        decision, assessment = candidate.record.decision, candidate.assessment
        links = tuple(
            EventEvidence(
                observation_id=check.observation_id, fields=tuple(sorted(check.supporting_fields))
            )
            for check in assessment.checks
        )
        if (
            decision.id != record.candidate_revision_id
            or decision.state != "OPEN"
            or decision.terms != record.terms
            or assessment.status != record.review_status
            or links != record.evidence
        ):
            raise ValueError("Inconsistent candidate decision")
        return assessment
    if record.candidate_revision_id is not None or record.evidence or record.citation is None:
        raise ValueError("Invalid manual decision")
    if record.decision_kind == "MANUAL":
        if record.manual_locked or record.review_status not in {
            "PROVISIONAL",
            "CONFLICT",
            "INCOMPLETE",
        }:
            raise ValueError("Invalid manual status")
        if record.terms.adjustment_basis != "AS_DECLARED" and record.review_status != "INCOMPLETE":
            raise ValueError("Unresolved adjustment cannot be accepted")
    elif record.decision_kind == "VERIFY":
        if (
            record.review_status != "VERIFIED"
            or not record.manual_locked
            or record.terms.adjustment_basis != "AS_DECLARED"
        ):
            raise ValueError("Invalid verification")
    elif record.decision_kind == "WITHDRAW" and record.review_status != "WITHDRAWN":
        raise ValueError("Invalid withdrawal")
    return None


def _audited(con: Connection, record: EventRevision, previous: EventRevision | None) -> None:
    rows = con.execute(
        text("""
        SELECT owner_id,request_id,reason,before_sha256,after_sha256 FROM audit_events
        WHERE entity_type='dividend_event_revision' AND entity_id=:id
          AND action='DIVIDEND_EVENT_REVISED'
    """),
        {"id": str(record.revision_id)},
    ).all()
    expected = (
        str(record.owner_id),
        str(record.request_id),
        record.reason,
        digest(canonical(_audit_values(previous))) if previous is not None else None,
        digest(canonical(_audit_values(record))),
    )
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise ValueError("Inconsistent event audit")


def get(
    con: Connection, owner_id: UUID, identifier: UUID, *, version: int | None = None
) -> ReviewedEvent | None:
    """Read audited retained history; later source/action changes do not grant new use."""
    _scope(owner_id, identifier, version)
    try:
        parent = _owned_parent(con, owner_id, identifier)
        if parent is None:
            return None
        _history_scope(con, parent)
        selected = parent["version"] if version is None else version
        if selected > parent["version"]:
            return None
        previous: EventRevision | None = None
        result: ReviewedEvent | None = None
        # ponytail: scan the retained chain; add verified checkpoints if histories grow large.
        for number in range(1, parent["version"] + 1):
            record = _record(con, parent, number)
            assessment = _lineage(con, record)
            if previous is not None and previous.created_at > record.created_at:
                raise ValueError("Event revisions are not chronological")
            if (
                record.decision_kind == "CANDIDATE"
                and previous is not None
                and (previous.manual_locked or previous.review_status == "WITHDRAWN")
            ):
                raise ValueError("A candidate cannot overwrite a locked or withdrawn event")
            if record.decision_kind in {"VERIFY", "WITHDRAW"}:
                if previous is None or previous.terms != record.terms:
                    raise ValueError("Decision must preserve the prior terms")
                if previous.review_status == "WITHDRAWN":
                    raise ValueError("A withdrawn event requires an explicit manual revision")
                if (
                    record.decision_kind == "WITHDRAW"
                    and previous.manual_locked != record.manual_locked
                ):
                    raise ValueError("Withdrawal cannot change the manual lock")
            _audited(con, record, previous)
            previous = record
            if number == selected:
                result = ReviewedEvent(record, assessment)
        return result
    except (ValueError, TypeError, KeyError, OverflowError, ApiError, PydanticSerializationError):
        raise EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE") from None
