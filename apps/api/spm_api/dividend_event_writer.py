"""Append owned event decisions; HTTP authority and economic projections remain external."""

import json
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
from spm_api.action_models import EvidenceUrl
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.decimal_types import to_scaled
from spm_api.dividend_evidence import DividendTerms, EvidenceAssessment
from spm_api.errors import Model
from spm_api.master_service import invalidate_portfolio
from spm_api.providers.base import CitationReference


class EventIntent(Model):
    """Explicit decision identities; the caller remains responsible for HTTP authorization."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    revision_id: UUID
    revision_no: events.Version
    kind: events.DecisionKind
    terms: DividendTerms | None = Field(default=None, repr=False)
    candidate_id: UUID | None = None
    candidate_version: events.Version | None = None
    citation: CitationReference | None = Field(default=None, repr=False)
    source_url: EvidenceUrl | None = Field(default=None, repr=False)
    reason: events.Reason = Field(repr=False)

    @model_validator(mode="after")
    def explicit_decision(self) -> Self:
        if self.kind == "CANDIDATE":
            if (
                self.candidate_id is None
                or self.candidate_version is None
                or self.terms is not None
                or self.citation is not None
                or self.source_url is not None
            ):
                raise ValueError("A candidate decision requires only an explicit candidate version")
        elif (
            self.candidate_id is not None
            or self.candidate_version is not None
            or self.citation is None
            or (self.kind == "MANUAL") != (self.terms is not None)
        ):
            raise ValueError("A manual decision requires an explicit reference and matching terms")
        return self


def _writer(con: Connection, owner_id: UUID) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise events.EventError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner_id, UUID):
        raise events.EventError("INVALID_DIVIDEND_EVENT_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner_id)}
        ).first()
        is None
    ):
        raise events.EventError("DIVIDEND_EVENT_OWNER_UNAVAILABLE")


def _replay_matches(
    con: Connection,
    intent: EventIntent,
    record: events.EventRevision,
    at: datetime,
    request_id: UUID,
) -> bool:
    if (
        record.event_id != intent.event_id
        or record.revision_id != intent.revision_id
        or record.revision_no != intent.revision_no
        or record.decision_kind != intent.kind
        or record.created_at != at
        or record.request_id != request_id
        or record.reason != intent.reason
        or record.citation != intent.citation
        or record.source_url != intent.source_url
    ):
        return False
    if intent.kind == "MANUAL":
        return record.terms == intent.terms
    if intent.kind == "CANDIDATE":
        row = con.execute(
            text("SELECT candidate_id,revision_no FROM dividend_candidate_revisions WHERE id=:id"),
            {"id": str(record.candidate_revision_id)},
        ).first()
        return bool(
            row is not None
            and row.candidate_id == str(intent.candidate_id)
            and row.revision_no == intent.candidate_version
        )
    return True


def _candidate(
    con: Connection, owner_id: UUID, intent: EventIntent, at: datetime
) -> candidates.ReviewedCandidate:
    assert intent.candidate_id is not None and intent.candidate_version is not None
    try:
        candidate = candidates.require_current(
            con, owner_id, intent.candidate_id, version=intent.candidate_version
        )
    except candidates.CandidateError as error:
        raise events.EventError(str(error)) from None
    if candidate.record.created_at > at:
        raise events.EventError("DIVIDEND_EVENT_TIME_CONFLICT")
    if (
        con.execute(
            text("""
            SELECT 1 FROM dividend_revision_decisions d
            JOIN dividend_candidate_revisions c ON c.id=d.candidate_revision_id
            WHERE c.candidate_id=:candidate AND d.event_id!=:event LIMIT 1
        """),
            {"candidate": str(intent.candidate_id), "event": str(intent.event_id)},
        ).first()
        is not None
    ):
        raise events.EventError("DIVIDEND_EVENT_CANDIDATE_ALREADY_USED")
    return candidate


def _record(
    con: Connection,
    owner_id: UUID,
    intent: EventIntent,
    at: datetime,
    request_id: UUID,
    previous: events.EventRevision | None,
) -> tuple[events.EventRevision, EvidenceAssessment | None]:
    assessment: EvidenceAssessment | None = None
    candidate_id: UUID | None = None
    evidence: tuple[events.EventEvidence, ...] = ()
    if intent.kind == "CANDIDATE":
        if previous is not None and previous.manual_locked:
            raise events.EventError("DIVIDEND_EVENT_LOCKED")
        if previous is not None and previous.review_status == "WITHDRAWN":
            raise events.EventError("DIVIDEND_EVENT_WITHDRAWN")
        candidate = _candidate(con, owner_id, intent, at)
        terms = candidate.record.decision.terms
        assessment = candidate.assessment
        candidate_id = candidate.record.decision.id
        evidence = tuple(
            events.EventEvidence(
                observation_id=check.observation_id, fields=tuple(sorted(check.supporting_fields))
            )
            for check in assessment.checks
        )
        status: events.ReviewStatus = assessment.status
        locked = False
    elif intent.kind == "MANUAL":
        assert intent.terms is not None
        terms = intent.terms
        status = "PROVISIONAL" if terms.adjustment_basis == "AS_DECLARED" else "INCOMPLETE"
        locked = False
    else:
        if previous is None:
            raise events.EventError("DIVIDEND_EVENT_NOT_FOUND")
        if previous.review_status == "WITHDRAWN":
            raise events.EventError("DIVIDEND_EVENT_WITHDRAWN")
        terms = previous.terms
        if intent.kind == "VERIFY":
            if terms.adjustment_basis != "AS_DECLARED":
                raise events.EventError("DIVIDEND_EVENT_UNRESOLVED_BASIS")
            status, locked = "VERIFIED", True
        else:
            status, locked = "WITHDRAWN", previous.manual_locked
    try:
        record = events.EventRevision(
            owner_id=owner_id,
            event_id=intent.event_id,
            revision_id=intent.revision_id,
            revision_no=intent.revision_no,
            terms=terms,
            review_status=status,
            manual_locked=locked,
            decision_kind=intent.kind,
            candidate_revision_id=candidate_id,
            citation=intent.citation,
            source_url=intent.source_url,
            reason=intent.reason,
            request_id=request_id,
            created_at=at,
            evidence=evidence,
        )
    except (ValueError, TypeError, PydanticSerializationError):
        raise events.EventError("INVALID_DIVIDEND_EVENT_DECISION") from None
    return record, assessment


def _portfolios(con: Connection, owner_id: UUID, security_id: UUID) -> list[str]:
    # ponytail: scan retained security facts; add security indexes if projection volume warrants it.
    return list(
        con.execute(
            text("""
            SELECT p.id FROM portfolios p JOIN (
                SELECT portfolio_id FROM trades WHERE security_id=:security
                UNION SELECT portfolio_id FROM security_transfers WHERE security_id=:security
                UNION SELECT portfolio_id FROM positions WHERE security_id=:security
            ) affected ON affected.portfolio_id=p.id
            WHERE p.owner_id=:owner ORDER BY p.id
        """),
            {"owner": str(owner_id), "security": str(security_id)},
        ).scalars()
    )


def _review_content(
    record: events.EventRevision, assessment: EvidenceAssessment | None
) -> tuple[str, dict[str, object]]:
    if record.review_status not in {"CONFLICT", "INCOMPLETE"}:
        raise ValueError("A review must refer to unresolved evidence")
    code = (
        "DIVIDEND_SOURCE_CONFLICT" if record.review_status == "CONFLICT" else "DIVIDEND_INCOMPLETE"
    )
    return code, {
        "scope": "EVENT_EVIDENCE_REVIEW",
        "revision_id": str(record.revision_id),
        "conflicting_fields": sorted(assessment.conflicting_fields) if assessment else [],
        "incomplete_reasons": (
            sorted(assessment.incomplete_reasons) if assessment else ["UNRESOLVED_ADJUSTMENT"]
        ),
    }


def _review_audit_values(
    record: events.EventRevision, portfolio: str | None, code: str
) -> dict[str, object]:
    return {
        "event_id": str(record.event_id),
        "revision_id": str(record.revision_id),
        "portfolio_id": portfolio,
        "reason_code": code,
        "status": "OPEN",
    }


def _close_reviews(con: Connection, record: events.EventRevision, *, at: str) -> None:
    rows = (
        con.execute(
            text("""
        SELECT r.id,r.portfolio_id,r.account_id,r.status,r.severity,r.reason_code,
               r.detail_json,r.created_at,r.resolved_at,p.owner_id
        FROM review_items r LEFT JOIN portfolios p ON p.id=r.portfolio_id
        WHERE r.event_id=:event AND r.kind='DIVIDEND_EVIDENCE'
          AND r.status IN ('OPEN','ACKNOWLEDGED')
    """),
            {"event": str(record.event_id)},
        )
        .mappings()
        .all()
    )
    historical: dict[str, events.ReviewedEvent] = {}
    for row in rows:
        try:
            wire = row["detail_json"]
            if not isinstance(wire, str) or len(wire.encode("utf-8")) > 4096:
                raise ValueError
            detail = json.loads(wire)
            if not isinstance(detail, dict) or not isinstance(detail.get("revision_id"), str):
                raise ValueError
            identifier = UUID(detail["revision_id"])
            if str(identifier) != detail["revision_id"]:
                raise ValueError
            key = str(identifier)
            if key not in historical:
                version = con.execute(
                    text("""
                    SELECT revision_no FROM dividend_event_revisions
                    WHERE id=:id AND event_id=:event
                """),
                    {"id": key, "event": str(record.event_id)},
                ).scalar()
                if type(version) is not int or not 1 <= version < record.revision_no:
                    raise ValueError
                prior = events.get(con, record.owner_id, record.event_id, version=version)
                if prior is None:
                    raise ValueError
                historical[key] = prior
            prior = historical[key]
            code, expected_detail = _review_content(prior.record, prior.assessment)
            creation = con.execute(
                text("""
                SELECT owner_id,request_id,reason,before_sha256,after_sha256 FROM audit_events
                WHERE entity_type='review_item' AND entity_id=:id
                  AND action='DIVIDEND_EVIDENCE_REVIEW_CREATED'
            """),
                {"id": row["id"]},
            ).all()
            expected_audit = (
                str(record.owner_id),
                str(prior.record.request_id),
                prior.record.reason,
                None,
                digest(canonical(_review_audit_values(prior.record, row["portfolio_id"], code))),
            )
            if (
                str(UUID(row["id"])) != row["id"]
                or canonical(expected_detail) != wire
                or row["reason_code"] != code
                or row["severity"] != "WARNING"
                or row["account_id"] is not None
                or row["resolved_at"] is not None
                or row["created_at"] != stamp(prior.record.created_at.astimezone(UTC))
                or (row["portfolio_id"] is not None and row["owner_id"] != str(record.owner_id))
                or len(creation) != 1
                or tuple(creation[0]) != expected_audit
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE") from None
        con.execute(
            text("UPDATE review_items SET status='RESOLVED',resolved_at=:at WHERE id=:id"),
            {"at": at, "id": row["id"]},
        )
        audit(
            con,
            str(record.owner_id),
            "review_item",
            row["id"],
            "DIVIDEND_EVIDENCE_REVIEW_RESOLVED",
            request_id=str(record.request_id),
            before={"status": row["status"]},
            after={"status": "RESOLVED", "revision_id": str(record.revision_id)},
            reason=record.reason,
        )


def _reviews(
    con: Connection,
    record: events.EventRevision,
    assessment: EvidenceAssessment | None,
    portfolios: list[str],
    *,
    at: str,
) -> None:
    _close_reviews(con, record, at=at)
    if record.review_status not in {"CONFLICT", "INCOMPLETE"}:
        return
    code, detail = _review_content(record, assessment)
    targets: list[str | None] = [*portfolios] if portfolios else [None]
    for portfolio in targets:
        identifier = str(uuid4())
        con.execute(
            text("""
            INSERT INTO review_items(id,portfolio_id,event_id,kind,severity,reason_code,
                detail_json,created_at)
            VALUES (:id,:portfolio,:event,'DIVIDEND_EVIDENCE','WARNING',:code,:detail,:at)
        """),
            {
                "id": identifier,
                "portfolio": portfolio,
                "event": str(record.event_id),
                "code": code,
                "detail": canonical(detail),
                "at": at,
            },
        )
        audit(
            con,
            str(record.owner_id),
            "review_item",
            identifier,
            "DIVIDEND_EVIDENCE_REVIEW_CREATED",
            request_id=str(record.request_id),
            after=_review_audit_values(record, portfolio, code),
            reason=record.reason,
        )


def save(
    con: Connection,
    owner_id: UUID,
    intent: EventIntent,
    *,
    at: datetime,
    request_id: UUID,
) -> events.ReviewedEvent:
    """Append an audited decision; callers still owe session/CSRF and atomic projections."""
    _writer(con, owner_id)
    try:
        if (
            not isinstance(intent, EventIntent)
            or not isinstance(request_id, UUID)
            or not isinstance(at, datetime)
            or at.utcoffset() is None
        ):
            raise ValueError
        intent = EventIntent.model_validate(intent.model_dump(warnings="error"))
        at = at.astimezone(UTC)
        created_at = stamp(at)
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise events.EventError("INVALID_DIVIDEND_EVENT_DECISION") from None

    existing = con.execute(
        text("SELECT event_id,revision_no FROM dividend_event_revisions WHERE id=:id"),
        {"id": str(intent.revision_id)},
    ).first()
    if existing is not None:
        if existing.event_id != str(intent.event_id):
            raise events.EventError("DIVIDEND_EVENT_REVISION_CONFLICT")
        if type(existing.revision_no) is not int or not 1 <= existing.revision_no <= 2_147_483_647:
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        original = events.get(con, owner_id, intent.event_id, version=existing.revision_no)
        if original is None or not _replay_matches(con, intent, original.record, at, request_id):
            raise events.EventError("DIVIDEND_EVENT_REVISION_CONFLICT")
        return original

    current = events.get(con, owner_id, intent.event_id)
    previous = current.record if current is not None else None
    if previous is None:
        if (
            con.execute(
                text("SELECT 1 FROM dividend_events WHERE id=:id"), {"id": str(intent.event_id)}
            ).first()
            is not None
        ):
            raise events.EventError("DIVIDEND_EVENT_NOT_FOUND")
        if intent.revision_no != 1:
            raise events.EventError("DIVIDEND_EVENT_VERSION_CONFLICT")
    else:
        if intent.revision_no != previous.revision_no + 1:
            raise events.EventError("DIVIDEND_EVENT_VERSION_CONFLICT")
        if at < previous.created_at:
            raise events.EventError("DIVIDEND_EVENT_TIME_CONFLICT")
        # ponytail: fence every existing snapshot until atomic projection rebuilding is installed.
        if (
            con.execute(
                text("""
                SELECT 1 FROM dividend_entitlements ent
                JOIN dividend_event_revisions r ON r.id=ent.revision_id
                WHERE r.event_id=:id LIMIT 1
            """),
                {"id": str(intent.event_id)},
            ).first()
            is not None
        ):
            raise events.EventError("DIVIDEND_EVENT_PROJECTION_REQUIRED")
    record, assessment = _record(con, owner_id, intent, at, request_id, previous)
    if previous is not None and previous.terms.security_id != record.terms.security_id:
        raise events.EventError("DIVIDEND_EVENT_SECURITY_CHANGED")
    if (
        con.execute(
            text("SELECT 1 FROM securities WHERE id=:id"),
            {"id": str(record.terms.security_id)},
        ).first()
        is None
    ):
        raise events.EventError("DIVIDEND_EVENT_SECURITY_UNAVAILABLE")
    if previous is None:
        con.execute(
            text("""
            INSERT INTO dividend_events(id,security_id,event_identity_key,workflow_status,
                version,created_at,provenance_version)
            VALUES (:id,:security,:id,'INCOMPLETE',1,:at,1)
        """),
            {
                "id": str(record.event_id),
                "security": str(record.terms.security_id),
                "at": created_at,
            },
        )
        con.execute(
            text("""
            INSERT INTO dividend_event_controls(event_id,owner_id,request_id,created_at)
            VALUES (:event,:owner,:request,:at)
        """),
            {
                "event": str(record.event_id),
                "owner": str(owner_id),
                "request": str(request_id),
                "at": created_at,
            },
        )
    con.execute(
        text("""
        INSERT INTO dividend_event_revisions(id,event_id,revision_no,event_type,ex_date,
            announce_date,record_date,pay_date,dps_e8,currency,adjustment_basis,review_status,
            manual_locked,reason,created_at)
        VALUES (:id,:event,:version,:type,:ex,:announce,:record,:pay,:dps,:currency,:basis,
            :status,:locked,:reason,:at)
    """),
        {
            "id": str(record.revision_id),
            "event": str(record.event_id),
            "version": record.revision_no,
            "type": record.terms.event_type,
            "ex": record.terms.ex_date,
            "announce": record.terms.announce_date,
            "record": record.terms.record_date,
            "pay": record.terms.pay_date,
            "dps": to_scaled(record.terms.dps or "", 8),
            "currency": record.terms.currency,
            "basis": record.terms.adjustment_basis,
            "status": record.review_status,
            "locked": int(record.manual_locked),
            "reason": record.reason,
            "at": created_at,
        },
    )
    con.execute(
        text("""
        INSERT INTO dividend_revision_decisions(revision_id,event_id,owner_id,decision_kind,
            candidate_revision_id,evidence_count,citation,source_url,request_id,created_at)
        VALUES (:revision,:event,:owner,:kind,:candidate,:count,:citation,:url,:request,:at)
    """),
        {
            "revision": str(record.revision_id),
            "event": str(record.event_id),
            "owner": str(owner_id),
            "kind": record.decision_kind,
            "candidate": str(record.candidate_revision_id)
            if record.candidate_revision_id
            else None,
            "count": len(record.evidence),
            "citation": record.citation,
            "url": record.source_url,
            "request": str(request_id),
            "at": created_at,
        },
    )
    for link in record.evidence:
        con.execute(
            text("""
            INSERT INTO dividend_evidence_links(revision_id,observation_id,field_names_json)
            VALUES (:revision,:observation,:fields)
        """),
            {
                "revision": str(record.revision_id),
                "observation": str(link.observation_id),
                "fields": canonical(list(link.fields)),
            },
        )
    audit(
        con,
        str(owner_id),
        "dividend_event_revision",
        str(record.revision_id),
        "DIVIDEND_EVENT_REVISED",
        request_id=str(request_id),
        before=events._audit_values(previous) if previous is not None else None,
        after=events._audit_values(record),
        reason=record.reason,
    )
    result = con.execute(
        text("""
        UPDATE dividend_events SET current_revision_id=:revision,
            workflow_status=:status,version=:new
        WHERE id=:event AND version=:old
    """),
        {
            "revision": str(record.revision_id),
            "status": record.review_status,
            "new": record.revision_no,
            "event": str(record.event_id),
            "old": previous.revision_no if previous is not None else 1,
        },
    )
    if result.rowcount != 1:
        raise events.EventError("DIVIDEND_EVENT_VERSION_CONFLICT")
    portfolios = _portfolios(con, owner_id, record.terms.security_id)
    _reviews(con, record, assessment, portfolios, at=created_at)
    day = record.terms.ex_date
    assert day is not None
    if previous is not None and previous.terms.ex_date is not None:
        day = min(day, previous.terms.ex_date)
    for portfolio in portfolios:
        invalidate_portfolio(con, portfolio, str(record.revision_id), "DIVIDEND_EVENT_CHANGED", day)
    saved = events.get(con, owner_id, record.event_id, version=record.revision_no)
    if saved is None:
        raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
    return saved
