"""Read owned audited dividend events without granting present economic use."""

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_event_models as models
import spm_api.dividend_event_service as events
from spm_api.candidate_models import CandidateAssessment, CandidateEvidenceCheck
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.dividend_evidence import EvidenceAssessment
from spm_api.errors import ApiError, Error
from spm_api.master_service import security_row
from spm_api.pagination import Page, PageDep, paginate, validate_date_page, validate_version_page
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events",
    tags=["Dividend event decisions"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE", "The event cannot be read safely")


def _owned(
    con: Connection, owner: UUID, event_id: UUID, *, version: int | None = None
) -> events.ReviewedEvent:
    result = events.get(con, owner, event_id, version=version)
    if result is None:
        raise ApiError(404, "NOT_FOUND", "Dividend event not found")
    return result


def _assessment(value: EvidenceAssessment | None) -> CandidateAssessment | None:
    if value is None:
        return None
    return CandidateAssessment(
        status=value.status,
        independent_origins=tuple(sorted(value.independent_origins)),
        eligible_observation_ids=value.eligible_observation_ids,
        conflicting_fields=tuple(sorted(value.conflicting_fields)),
        incomplete_reasons=tuple(sorted(value.incomplete_reasons)),
        checks=tuple(
            CandidateEvidenceCheck(
                observation_id=check.observation_id,
                supporting_fields=tuple(sorted(check.supporting_fields)),
                conflicting_fields=tuple(sorted(check.conflicting_fields)),
                incomplete_reasons=tuple(sorted(check.incomplete_reasons)),
                comparison_dps=check.comparison_dps,
                comparison_basis=check.comparison_basis,
                basis_assertion_id=check.basis_assertion_id,
            )
            for check in value.checks
        ),
    )


def _fields(review: events.ReviewedEvent) -> dict[str, Any]:
    record = review.record
    terms = record.terms
    reference = None
    if record.citation is not None:
        link = urlsplit(record.source_url) if record.source_url is not None else None
        withheld = bool(link is not None and (link.query or link.fragment))
        reference = models.EventReference(
            citation=record.citation,
            url=None if withheld else record.source_url,
            url_withheld=withheld,
        )
    return {
        **terms.model_dump(),
        "workflow_status": record.review_status,
        "manual_locked": record.manual_locked,
        "decision_kind": record.decision_kind,
        "candidate_revision_id": record.candidate_revision_id,
        "independent_origin_groups": (
            len(review.assessment.independent_origins) if review.assessment is not None else None
        ),
        "assessment": _assessment(review.assessment),
        "evidence": record.evidence,
        "reference": reference,
        "reason": record.reason,
        "created_at": record.created_at,
        "scope": "OWNER_EVENT_DECISION_ONLY",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "document_verification": "NOT_ESTABLISHED",
        "eligibility_scope": "NOT_ESTABLISHED",
    }


def _current(review: events.ReviewedEvent) -> models.DividendEventRecord:
    record = review.record
    return models.DividendEventRecord(
        id=record.event_id,
        current_revision_id=record.revision_id,
        version=record.revision_no,
        **_fields(review),
    )


def _revision(review: events.ReviewedEvent) -> models.DividendEventRevisionRecord:
    record = review.record
    return models.DividendEventRevisionRecord(
        event_id=record.event_id,
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        **_fields(review),
    )


def _current_row(con: Connection, owner: UUID, row: RowMapping) -> models.DividendEventRecord:
    identifier = UUID(row["id"])
    if str(identifier) != row["id"] or row["joined_security_id"] != row["security_id"]:
        raise ValueError("Invalid event identity")
    review = _owned(con, owner, identifier)
    record = review.record
    if (
        str(record.revision_id) != row["revision_id"]
        or record.revision_no != row["version"]
        or str(record.terms.security_id) != row["security_id"]
        or record.review_status != row["workflow_status"]
        or record.terms.ex_date != row["ex_date"]
    ):
        raise ValueError("Inconsistent event selection")
    return _current(review)


def _history_row(
    con: Connection, owner: UUID, identifier: UUID, row: RowMapping
) -> models.DividendEventRevisionRecord:
    number = row["revision_no"]
    if type(number) is not int:
        raise ValueError("Invalid revision number")
    review = _owned(con, owner, identifier, version=number)
    if str(review.record.revision_id) != row["id"]:
        raise ValueError("Inconsistent revision identity")
    return _revision(review)


@router.get("", operation_id="list_dividend_events")
def list_events(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    security_id: UUID | None = None,
    workflow_status: events.ReviewStatus | None = None,
    from_date: IsoDate | None = None,
    to_date: IsoDate | None = None,
) -> Page[models.DividendEventRecord]:
    validate_date_page(page, from_date, to_date)
    try:
        with unit_of_work(request.app.state.engine) as con:
            if security_id is not None:
                security_row(con, str(security_id))
            rows = (
                con.execute(
                    text(r"""
                    SELECT e.id,e.security_id,e.current_revision_id,e.version,e.workflow_status,
                      r.id AS revision_id,r.ex_date,s.id AS joined_security_id
                    FROM dividend_events e
                    LEFT JOIN dividend_event_controls c ON c.event_id=e.id
                    LEFT JOIN dividend_event_revisions r ON r.id=e.current_revision_id
                    LEFT JOIN securities s ON s.id=e.security_id
                    WHERE (c.owner_id=:owner OR EXISTS(
                        SELECT 1 FROM dividend_event_revisions previous
                        JOIN audit_events a ON a.entity_id=previous.id
                        WHERE previous.event_id=e.id AND a.owner_id=:owner
                          AND a.entity_type='dividend_event_revision'
                          AND a.action='DIVIDEND_EVENT_REVISED'))
                      AND (:security IS NULL OR e.security_id=:security)
                      AND (:status IS NULL OR e.workflow_status=:status)
                      AND (:from_date IS NULL OR r.ex_date>=:from_date)
                      AND (:to_date IS NULL OR r.ex_date<=:to_date)
                      AND (e.id LIKE :search ESCAPE '\'
                        OR s.display_ticker LIKE :search ESCAPE '\'
                        OR s.name LIKE :search ESCAPE '\')
                      AND (:cursor_value IS NULL OR (r.ex_date,e.id)<(:cursor_value,:cursor_id))
                    ORDER BY r.ex_date DESC,e.id DESC LIMIT :limit
                """),
                    {
                        **page.params(),
                        "owner": str(auth.owner.id),
                        "security": str(security_id) if security_id is not None else None,
                        "status": workflow_status,
                        "from_date": from_date,
                        "to_date": to_date,
                    },
                )
                .mappings()
                .all()
            )
            # ponytail: verify each event chain; batch verified checkpoints if volumes grow.
            return paginate(
                rows, page, "ex_date", lambda row: _current_row(con, auth.owner.id, row)
            )
    except events.EventError:
        raise _unavailable() from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.get("/{event_id}", operation_id="get_dividend_event")
def get_event(
    event_id: UUID, request: Request, response: Response, auth: AuthDep
) -> models.DividendEventRecord:
    try:
        with unit_of_work(request.app.state.engine) as con:
            result = _current(_owned(con, auth.owner.id, event_id))
        response.headers["ETag"] = f'"{result.version}"'
        return result
    except events.EventError:
        raise _unavailable() from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.get("/{event_id}/revisions", operation_id="list_dividend_event_revisions")
def revisions(
    event_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[models.DividendEventRevisionRecord]:
    validate_version_page(page)
    try:
        with unit_of_work(request.app.state.engine) as con:
            _owned(con, auth.owner.id, event_id)
            rows = (
                con.execute(
                    text(r"""
                    SELECT id,revision_no FROM dividend_event_revisions
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
            # ponytail: rescan each audited revision; add a single-pass reader if histories grow.
            return paginate(
                rows,
                page,
                "revision_no",
                lambda row: _history_row(con, auth.owner.id, event_id, row),
            )
    except events.EventError:
        raise _unavailable() from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None
