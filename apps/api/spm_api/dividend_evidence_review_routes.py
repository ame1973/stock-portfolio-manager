"""Owner-scoped, private reads of retained dividend evidence-review history."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_models as models
from spm_api.audit import canonical, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error
from spm_api.pagination import PageDep, paginate, validate_time_page
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}",
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
    rows = (
        con.execute(
            text("""
                SELECT r.*,rev.revision_no AS observed_revision_no,next.id AS successor_id
                FROM review_items r
                LEFT JOIN dividend_event_revisions rev
                  ON rev.id=json_extract(r.detail_json,'$.revision_id') AND rev.event_id=r.event_id
                LEFT JOIN dividend_event_revisions next
                  ON next.event_id=r.event_id AND next.revision_no=rev.revision_no+1
                WHERE r.event_id=:event AND r.kind='DIVIDEND_EVIDENCE'
                ORDER BY r.created_at,r.id
            """),
            {"event": str(retained.event_id)},
        )
        .mappings()
        .all()
    )
    if len(rows) != len(retained.items):
        raise ValueError("Inconsistent retained review count")
    records: dict[str, models.EvidenceReviewRecord] = {}
    for row, item in zip(rows, retained.items, strict=True):
        if not isinstance(item, reviews.EvidenceReview):
            raise ValueError("Invalid retained review")
        identifier = str(item.id)
        detail = {
            "scope": "EVENT_EVIDENCE_REVIEW",
            "revision_id": str(item.revision_id),
            "conflicting_fields": list(item.conflicting_fields),
            "incomplete_reasons": list(item.incomplete_reasons),
        }
        successor = str(item.resolved_by_revision_id) if item.resolved_by_revision_id else None
        if (
            identifier in records
            or row["id"] != identifier
            or row["event_id"] != str(retained.event_id)
            or item.event_id != retained.event_id
            or row["account_id"] is not None
            or item.account_id is not None
            or row["portfolio_id"] != (str(item.portfolio_id) if item.portfolio_id else None)
            or row["observed_revision_no"] != item.revision_no
            or row["severity"] != item.severity
            or row["status"] != item.status
            or row["reason_code"] != item.reason_code
            or row["detail_json"] != canonical(detail)
            or row["created_at"] != stamp(item.created_at)
            or row["resolved_at"] != (stamp(item.resolved_at) if item.resolved_at else None)
            or (row["successor_id"] if item.status == "RESOLVED" else None) != successor
            or item.acknowledgement_scope != "NOT_ESTABLISHED"
        ):
            raise ValueError("Inconsistent retained review result")
        if item.status == "RESOLVED":
            if (
                item.revision_no >= retained.revision_no
                or item.revision_id == retained.revision_id
                or (item.revision_no + 1 == retained.revision_no)
                != (item.resolved_by_revision_id == retained.revision_id)
            ):
                raise ValueError("Inconsistent retained review revision")
        elif (item.revision_id, item.revision_no) != (
            retained.revision_id,
            retained.revision_no,
        ) or row["successor_id"] is not None:
            raise ValueError("Inconsistent active review revision")
        records[identifier] = _record(item)
    return records


def _record(item: reviews.EvidenceReview) -> models.EvidenceReviewRecord:
    return models.EvidenceReviewRecord(
        id=item.id,
        event_id=item.event_id,
        revision_id=item.revision_id,
        revision_no=item.revision_no,
        portfolio_id=item.portfolio_id,
        status=item.status,
        severity=item.severity,
        reason_code=item.reason_code,
        conflicting_fields=list(item.conflicting_fields),
        incomplete_reasons=list(item.incomplete_reasons),
        created_at=item.created_at,
        resolved_at=item.resolved_at,
        resolved_by_revision_id=item.resolved_by_revision_id,
        acknowledgement_scope=item.acknowledgement_scope,
        scope="RETAINED_EVIDENCE_REVIEW_ONLY",
    )


@router.get("/evidence-reviews", operation_id="list_dividend_evidence_reviews")
def evidence_reviews(
    event_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> models.EvidenceReviewPage:
    validate_time_page(page)
    try:
        with unit_of_work(request.app.state.engine) as con:
            retained = _owned(con, auth.owner.id, event_id)
            records = _checked_rows(con, retained)
            rows = (
                con.execute(
                    text(r"""
                        SELECT id,created_at FROM review_items
                        WHERE event_id=:event AND kind='DIVIDEND_EVIDENCE'
                          AND (id LIKE :search ESCAPE '\'
                            OR reason_code LIKE :search ESCAPE '\')
                          AND (:cursor_value IS NULL OR (created_at,id)<(:cursor_value,:cursor_id))
                        ORDER BY created_at DESC,id DESC LIMIT :limit
                    """),
                    {**page.params(), "event": str(event_id)},
                )
                .mappings()
                .all()
            )

            def convert(row: RowMapping) -> models.EvidenceReviewRecord:
                item = records[row["id"]]
                if row["created_at"] != stamp(item.created_at):
                    raise ValueError("Inconsistent selected review")
                return item

            selected = paginate(rows, page, "created_at", convert)
            return models.EvidenceReviewPage(
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
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None
