"""Read retained dividend evidence reviews without granting economic authority."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
from spm_api.audit import canonical, digest, stamp


class EvidenceReviewError(ValueError):
    """Use fixed errors without disclosing retained owner or event information."""


@dataclass(frozen=True, slots=True)
class EvidenceReview:
    id: UUID = field(repr=False)
    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    portfolio_id: UUID | None = field(repr=False)
    account_id: None
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]
    severity: Literal["WARNING"]
    reason_code: Literal["DIVIDEND_SOURCE_CONFLICT", "DIVIDEND_INCOMPLETE"]
    conflicting_fields: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    created_at: datetime = field(repr=False)
    resolved_at: datetime | None = field(repr=False)
    resolved_by_revision_id: UUID | None = field(repr=False)
    acknowledgement_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


@dataclass(frozen=True, slots=True)
class EvidenceReviewHistory:
    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    items: tuple[EvidenceReview, ...] = field(repr=False)
    scope: Literal["RETAINED_EVIDENCE_REVIEWS_ONLY"] = "RETAINED_EVIDENCE_REVIEWS_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


def history(con: Connection, owner: UUID, event_id: UUID) -> EvidenceReviewHistory | None:
    """Validate retained event-evidence review rows in one caller-owned snapshot.

    This does not establish completeness, authenticate acknowledgments, resolve a review,
    create a reconciliation task or authorize a financial revision.
    """
    if not con.in_transaction() or not isinstance(owner, UUID) or not isinstance(event_id, UUID):
        raise EvidenceReviewError("INVALID_DIVIDEND_EVIDENCE_REVIEW_SCOPE")
    try:
        current = events.get(con, owner, event_id)
        if current is None:
            return None
        rows = (
            con.execute(
                text("""
                    SELECT r.*,p.owner_id AS portfolio_owner FROM review_items r
                    LEFT JOIN portfolios p ON p.id=r.portfolio_id
                    WHERE r.event_id=:event AND r.kind='DIVIDEND_EVIDENCE'
                    ORDER BY r.created_at,r.id
                """),
                {"event": str(event_id)},
            )
            .mappings()
            .all()
        )
        # ponytail: verify each full revision chain; add a verified bulk reader if it grows.
        revisions: dict[int, events.ReviewedEvent] = {current.record.revision_no: current}

        def revision(number: int) -> events.ReviewedEvent:
            if number not in revisions:
                result = events.get(con, owner, event_id, version=number)
                if result is None:
                    raise ValueError("Missing retained revision")
                revisions[number] = result
            return revisions[number]

        result: list[EvidenceReview] = []
        scopes: dict[int, set[UUID | None]] = {}
        for row in rows:
            identifier = events._uuid(row["id"])
            portfolio = (
                events._uuid(row["portfolio_id"]) if row["portfolio_id"] is not None else None
            )
            wire = row["detail_json"]
            if not isinstance(wire, str) or len(wire.encode("utf-8")) > 4096:
                raise ValueError("Invalid retained review")
            detail = json.loads(wire)
            if not isinstance(detail, dict):
                raise ValueError("Invalid retained review")
            physical = events._uuid(detail.get("revision_id"))
            number = con.execute(
                text(
                    "SELECT revision_no FROM dividend_event_revisions "
                    "WHERE id=:id AND event_id=:event"
                ),
                {"id": str(physical), "event": str(event_id)},
            ).scalar_one_or_none()
            if type(number) is not int or not 1 <= number <= current.record.revision_no:
                raise ValueError("Invalid retained revision")
            reviewed = revision(number)
            if reviewed.record.revision_id != physical:
                raise ValueError("Mismatched retained revision")
            code, expected = writer._review_content(reviewed.record, reviewed.assessment)
            fields, reasons = expected["conflicting_fields"], expected["incomplete_reasons"]
            if (
                code not in ("DIVIDEND_SOURCE_CONFLICT", "DIVIDEND_INCOMPLETE")
                or not isinstance(fields, list)
                or any(not isinstance(value, str) for value in fields)
                or not isinstance(reasons, list)
                or any(not isinstance(value, str) for value in reasons)
            ):
                raise ValueError("Invalid review content")
            existing = scopes.setdefault(number, set())
            if portfolio in existing or (existing and (portfolio is None or None in existing)):
                raise ValueError("Duplicated retained review scope")
            existing.add(portfolio)
            created = events._time(row["created_at"])
            if (
                row["account_id"] is not None
                or (portfolio is not None and row["portfolio_owner"] != str(owner))
                or row["severity"] != "WARNING"
                or row["reason_code"] != code
                or wire != canonical(expected)
                or row["created_at"] != stamp(reviewed.record.created_at)
            ):
                raise ValueError("Inconsistent retained review")
            audits = (
                con.execute(
                    text("""
                        SELECT * FROM audit_events WHERE entity_type='review_item' AND entity_id=:id
                        AND action IN ('DIVIDEND_EVIDENCE_REVIEW_CREATED',
                                       'DIVIDEND_EVIDENCE_REVIEW_RESOLVED')
                    """),
                    {"id": str(identifier)},
                )
                .mappings()
                .all()
            )
            creations = [
                item for item in audits if item["action"] == "DIVIDEND_EVIDENCE_REVIEW_CREATED"
            ]
            closures = [
                item for item in audits if item["action"] == "DIVIDEND_EVIDENCE_REVIEW_RESOLVED"
            ]
            if len(creations) != 1:
                raise ValueError("Invalid review creation audit")
            creation = creations[0]
            events._uuid(creation["id"])
            events._time(creation["created_at"])
            if (
                creation["owner_id"] != str(owner)
                or creation["request_id"] != str(reviewed.record.request_id)
                or creation["reason"] != reviewed.record.reason
                or creation["before_sha256"] is not None
                or creation["after_sha256"]
                != digest(
                    canonical(
                        writer._review_audit_values(reviewed.record, row["portfolio_id"], code)
                    )
                )
            ):
                raise ValueError("Mismatched review creation audit")
            status = row["status"]
            resolved_at = None
            resolved_by = None
            if status in ("OPEN", "ACKNOWLEDGED"):
                if (
                    row["resolved_at"] is not None
                    or closures
                    or number != current.record.revision_no
                ):
                    raise ValueError("Inconsistent active review")
            elif status == "RESOLVED":
                if number >= current.record.revision_no or len(closures) != 1:
                    raise ValueError("Invalid review resolution")
                successor = revision(number + 1).record
                resolved_at = events._time(row["resolved_at"])
                resolved_by = successor.revision_id
                closure = closures[0]
                events._uuid(closure["id"])
                events._time(closure["created_at"])
                valid_before = {
                    digest(canonical({"status": value})) for value in ("OPEN", "ACKNOWLEDGED")
                }
                if (
                    row["resolved_at"] != stamp(successor.created_at)
                    or closure["owner_id"] != str(owner)
                    or closure["request_id"] != str(successor.request_id)
                    or closure["reason"] != successor.reason
                    or closure["before_sha256"] not in valid_before
                    or closure["after_sha256"]
                    != digest(canonical({"status": "RESOLVED", "revision_id": str(resolved_by)}))
                ):
                    raise ValueError("Mismatched review resolution audit")
            else:
                raise ValueError("Unsupported review state")
            result.append(
                EvidenceReview(
                    identifier,
                    event_id,
                    physical,
                    number,
                    portfolio,
                    None,
                    cast(Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"], status),
                    "WARNING",
                    cast(Literal["DIVIDEND_SOURCE_CONFLICT", "DIVIDEND_INCOMPLETE"], code),
                    tuple(fields),
                    tuple(reasons),
                    created,
                    resolved_at,
                    resolved_by,
                )
            )
        return EvidenceReviewHistory(
            event_id, current.record.revision_id, current.record.revision_no, tuple(result)
        )
    except (events.EventError, ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise EvidenceReviewError("DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE") from None
