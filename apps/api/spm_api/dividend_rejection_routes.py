"""Protected owner reads of retained rejection decisions, without economic grants."""

from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_models as models
import spm_api.dividend_rejection_service as rejections
from spm_api.db import unit_of_work
from spm_api.dividend_event_models import EventReference
from spm_api.errors import ApiError, Error
from spm_api.pagination import Page, PageDep, paginate, validate_version_page
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events",
    tags=["Dividend rejection decisions"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE", "The rejection history cannot be read safely"
    )


def _owned(
    con: Connection, owner: UUID, event_id: UUID
) -> tuple[events.ReviewedEvent, tuple[rejections.RejectionRecord, ...]]:
    retained = rejections.history(con, owner, event_id)
    if retained is None:
        raise ApiError(404, "NOT_FOUND", "Dividend event not found")
    current = events.get(con, owner, event_id)
    if current is None:
        raise ValueError("Missing current event")
    return current, retained


def _decision(record: rejections.RejectionRecord) -> models.RejectionDecision:
    link = urlsplit(record.source_url) if record.source_url is not None else None
    withheld = bool(link is not None and (link.query or link.fragment))
    return models.RejectionDecision(
        id=record.id,
        event_id=record.event_id,
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        decision="REJECT_REVISION",
        reference=EventReference(
            citation=record.citation,
            url=None if withheld else record.source_url,
            url_withheld=withheld,
        ),
        reason=record.reason,
        created_at=record.created_at,
        scope="OWNER_REVISION_REJECTION_ONLY",
        document_verification="NOT_ESTABLISHED",
        eligibility_scope="NOT_ESTABLISHED",
    )


def _item(
    con: Connection,
    owner: UUID,
    current: events.ReviewedEvent,
    records: dict[str, rejections.RejectionRecord],
    row: RowMapping,
) -> models.RejectionHistoryItem:
    record = records[row["id"]]
    if row["revision_no"] != record.revision_no:
        raise ValueError("Inconsistent rejection selection")
    resolution = None
    if record.revision_no < current.record.revision_no:
        successor = events.get(con, owner, record.event_id, version=record.revision_no + 1)
        if (
            successor is None
            or successor.resolution is None
            or successor.resolution.rejection_id != record.id
        ):
            raise ValueError("Missing rejection successor")
        resolution = models.RejectionResolution(
            id=successor.resolution.id,
            successor_revision_id=successor.record.revision_id,
            successor_revision_no=successor.record.revision_no,
        )
    return models.RejectionHistoryItem(decision=_decision(record), resolution=resolution)


@router.get("/{event_id}/rejection", operation_id="get_dividend_rejection_state")
def current_rejection(event_id: UUID, request: Request, auth: AuthDep) -> models.RejectionState:
    try:
        with unit_of_work(request.app.state.engine) as con:
            current, retained = _owned(con, auth.owner.id, event_id)
            record = next(
                (item for item in retained if item.revision_id == current.record.revision_id), None
            )
            return models.RejectionState(
                event_id=event_id,
                revision_id=current.record.revision_id,
                revision_no=current.record.revision_no,
                state="REJECTED" if record is not None else "NOT_REJECTED",
                rejection=_decision(record) if record is not None else None,
                scope="OWNER_REJECTION_STATE_ONLY",
                current_use_scope="REVALIDATION_REQUIRED",
                document_verification="NOT_ESTABLISHED",
                eligibility_scope="NOT_ESTABLISHED",
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


@router.get("/{event_id}/rejections", operation_id="list_dividend_rejections")
def rejection_history(
    event_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[models.RejectionHistoryItem]:
    validate_version_page(page)
    try:
        with unit_of_work(request.app.state.engine) as con:
            current, retained = _owned(con, auth.owner.id, event_id)
            records = {str(record.id): record for record in retained}
            rows = (
                con.execute(
                    text(r"""
                    SELECT id,revision_no FROM dividend_event_rejections
                    WHERE event_id=:event
                      AND (id LIKE :search ESCAPE '\'
                        OR CAST(revision_no AS TEXT) LIKE :search ESCAPE '\')
                      AND (:cursor_value IS NULL OR (revision_no,id)<(:cursor_value,:cursor_id))
                    ORDER BY revision_no DESC,id DESC LIMIT :limit
                """),
                    {
                        **page.params(),
                        "event": str(event_id),
                        "cursor_value": int(page.cursor_value) if page.cursor_value else None,
                    },
                )
                .mappings()
                .all()
            )
            # ponytail: recheck successor chains per page; batch if long histories become common.
            return paginate(
                rows,
                page,
                "revision_no",
                lambda row: _item(con, auth.owner.id, current, records, row),
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
