"""Owner-scoped, private reads of retained dividend evidence-review history."""

import sqlite3
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_integrity as integrity
import spm_api.dividend_evidence_review_models as models
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error
from spm_api.pagination import PageDep, paginate, validate_time_page
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends",
    tags=["Dividend evidence reviews"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE", "The review history cannot be read safely"
    )


def _owned(con: Connection, owner: UUID, event_id: UUID) -> reviews.EvidenceReviewHistory:
    retained = reviews.history(con, owner, event_id)
    if retained is None:
        raise ApiError(404, "NOT_FOUND", "Dividend event not found")
    current = events.get(con, owner, event_id)
    if (
        not isinstance(retained, reviews.EvidenceReviewHistory)
        or current is None
        or (retained.event_id, retained.revision_id, retained.revision_no)
        != (event_id, current.record.revision_id, current.record.revision_no)
        or retained.scope != "RETAINED_EVIDENCE_REVIEWS_ONLY"
        or retained.history_completeness != "UNVERIFIED"
        or retained.current_use_scope != "REVALIDATION_REQUIRED"
        or retained.eligibility_scope != "NOT_ESTABLISHED"
    ):
        raise ValueError("Inconsistent retained evidence review history")
    return retained


def _checked_rows(
    con: Connection, retained: reviews.EvidenceReviewHistory
) -> dict[str, models.EvidenceReviewRecord]:
    return integrity.checked_rows(con, retained)


def _sqlite(con: Connection) -> sqlite3.Connection:
    raw = con.connection.driver_connection
    if type(raw) is not sqlite3.Connection:
        raise ValueError("Unsupported review history connection")
    return raw


@router.get("/events/{event_id}/evidence-reviews", operation_id="list_dividend_evidence_reviews")
def evidence_reviews(
    event_id: UUID,
    request: Request,
    auth: AuthDep,
    page: PageDep,
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"] | None = None,
    severity: Literal["INFO", "WARNING", "CRITICAL"] | None = None,
) -> models.EvidenceReviewPage:
    validate_time_page(page)
    try:
        with unit_of_work(request.app.state.engine) as con:
            raw = _sqlite(con)
            changes = raw.total_changes
            retained = _owned(con, auth.owner.id, event_id)
            records = _checked_rows(con, retained)
            rows = (
                con.execute(
                    text(r"""
                        SELECT id,created_at,status,severity FROM review_items
                        WHERE event_id=:event AND kind='DIVIDEND_EVIDENCE'
                          AND (:status IS NULL OR status=:status)
                          AND (:severity IS NULL OR severity=:severity)
                          AND (id LIKE :search ESCAPE '\'
                            OR reason_code LIKE :search ESCAPE '\')
                          AND (:cursor_value IS NULL OR (created_at,id)<(:cursor_value,:cursor_id))
                        ORDER BY created_at DESC,id DESC LIMIT :limit
                    """),
                    {
                        **page.params(),
                        "event": str(event_id),
                        "status": status,
                        "severity": severity,
                    },
                )
                .mappings()
                .all()
            )

            def convert(row: RowMapping) -> models.EvidenceReviewRecord:
                item = records[row["id"]]
                if (
                    row["created_at"] != stamp(item.created_at)
                    or row["status"] != item.status
                    or row["severity"] != item.severity
                ):
                    raise ValueError("Inconsistent selected review")
                return item

            selected = paginate(rows, page, "created_at", convert)
            result = models.EvidenceReviewPage(
                event_id=retained.event_id,
                revision_id=retained.revision_id,
                revision_no=retained.revision_no,
                items=selected.items,
                next_cursor=selected.next_cursor,
                scope=retained.scope,
                history_completeness=retained.history_completeness,
                current_use_scope=retained.current_use_scope,
                eligibility_scope=retained.eligibility_scope,
                document_verification="NOT_ESTABLISHED",
            )
            # A callback could otherwise hide a changed row from the filtered query.
            if raw.total_changes != changes:
                raise ValueError("Review history changed during the read")
            return result
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None


@router.get("/evidence-reviews", operation_id="list_dividend_evidence_review_queue")
def evidence_review_queue(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"] | None = None,
    severity: Literal["INFO", "WARNING", "CRITICAL"] | None = None,
) -> models.EvidenceReviewQueuePage:
    validate_time_page(page)
    try:
        with unit_of_work(request.app.state.engine) as con:
            raw = _sqlite(con)
            changes = raw.total_changes
            rows = (
                con.execute(
                    text(r"""
                        SELECT r.id,r.event_id,r.created_at,r.status,r.severity
                        FROM review_items r JOIN dividend_events e ON e.id=r.event_id
                        LEFT JOIN dividend_event_controls c ON c.event_id=e.id
                        WHERE r.kind='DIVIDEND_EVIDENCE' AND (c.owner_id=:owner OR EXISTS(
                            SELECT 1 FROM dividend_event_revisions previous
                            JOIN audit_events a ON a.entity_id=previous.id
                            WHERE previous.event_id=e.id AND a.owner_id=:owner
                              AND a.entity_type='dividend_event_revision'
                              AND a.action='DIVIDEND_EVENT_REVISED'))
                          AND (:status IS NULL OR r.status=:status)
                          AND (:severity IS NULL OR r.severity=:severity)
                          AND (r.id LIKE :search ESCAPE '\'
                            OR r.event_id LIKE :search ESCAPE '\'
                            OR r.reason_code LIKE :search ESCAPE '\')
                          AND (:cursor_value IS NULL
                            OR (r.created_at,r.id)<(:cursor_value,:cursor_id))
                        ORDER BY r.created_at DESC,r.id DESC LIMIT :limit
                    """),
                    {
                        **page.params(),
                        "owner": str(auth.owner.id),
                        "status": status,
                        "severity": severity,
                    },
                )
                .mappings()
                .all()
            )
            histories: dict[
                UUID, tuple[reviews.EvidenceReviewHistory, dict[str, models.EvidenceReviewRecord]]
            ] = {}

            def convert(row: RowMapping) -> models.EvidenceReviewQueueRecord:
                identifier = events._uuid(row["event_id"])
                if identifier not in histories:
                    try:
                        retained = _owned(con, auth.owner.id, identifier)
                    except ApiError as error:
                        if error.status != 404:
                            raise
                        raise ValueError("Inconsistent selected event") from None
                    histories[identifier] = (retained, _checked_rows(con, retained))
                retained, records = histories[identifier]
                item = records[row["id"]]
                if (
                    row["created_at"] != stamp(item.created_at)
                    or row["status"] != item.status
                    or row["severity"] != item.severity
                ):
                    raise ValueError("Inconsistent selected review")
                return models.EvidenceReviewQueueRecord(
                    **item.model_dump(),
                    current_revision_id=retained.revision_id,
                    current_revision_no=retained.revision_no,
                )

            # Verify the extra row too; it determines whether a continuation is offered.
            checked = {row["id"]: convert(row) for row in rows}
            selected = paginate(rows, page, "created_at", lambda row: checked[row["id"]])
            result = models.EvidenceReviewQueuePage(
                items=selected.items,
                next_cursor=selected.next_cursor,
                scope="RETAINED_EVIDENCE_REVIEW_QUEUE_ONLY",
                history_completeness="UNVERIFIED",
                current_use_scope="REVALIDATION_REQUIRED",
                eligibility_scope="NOT_ESTABLISHED",
                document_verification="NOT_ESTABLISHED",
            )
            if raw.total_changes != changes:
                raise ValueError("Review queue changed during the read")
            return result
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None
