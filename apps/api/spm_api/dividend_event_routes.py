"""Protected event decisions without granting present economic use."""

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_models as models
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
from spm_api.audit import audit, canonical, digest, now
from spm_api.candidate_models import CandidateAssessment, CandidateEvidenceCheck
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.dividend_evidence import DividendTerms, EvidenceAssessment
from spm_api.errors import ApiError, Error
from spm_api.master_service import security_row
from spm_api.mutations import check_version, mutate
from spm_api.pagination import Page, PageDep, paginate, validate_date_page, validate_version_page
from spm_api.security import Auth, AuthDep, DividendEventVersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/events",
    tags=["Dividend event decisions"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE", "The event cannot be read safely")


def _write_failure(error: events.EventError) -> ApiError:
    code = str(error)
    if code in {"DIVIDEND_EVENT_NOT_FOUND", "DIVIDEND_EVENT_SECURITY_UNAVAILABLE"}:
        return ApiError(404, "NOT_FOUND", "Dividend event or security not found")
    if code == "DIVIDEND_EVENT_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code == "CANDIDATE_NOT_FOUND":
        return ApiError(404, "NOT_FOUND", "Dividend candidate not found")
    if code == "CANDIDATE_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code == "CANDIDATE_VERSION_CONFLICT":
        return ApiError(412, "PRECONDITION_FAILED", "The observed candidate changed; reload it")
    if code in {
        "CANDIDATE_DISMISSED",
        "CANDIDATE_BASIS_CHANGED",
        "CANDIDATE_SOURCE_CHANGED",
        "DIVIDEND_EVENT_CANDIDATE_ALREADY_USED",
        "DIVIDEND_EVENT_LOCKED",
        "DIVIDEND_EVENT_WITHDRAWN",
    }:
        return ApiError(409, code, "The candidate cannot be adopted into this event")
    if code in {"INVALID_DIVIDEND_EVENT_SCOPE", "INVALID_DIVIDEND_EVENT_DECISION"}:
        return ApiError(422, code, "The dividend event decision is invalid")
    if code == "DIVIDEND_EVENT_VERSION_CONFLICT":
        return ApiError(412, "PRECONDITION_FAILED", "The observed event changed; reload it")
    if code in {
        "DIVIDEND_EVENT_PROJECTION_REQUIRED",
        "DIVIDEND_EVENT_SECURITY_CHANGED",
        "DIVIDEND_EVENT_REVISION_CONFLICT",
        "DIVIDEND_EVENT_TIME_CONFLICT",
        "DIVIDEND_EVENT_VERSION_EXHAUSTED",
    }:
        return ApiError(409, code, "The dividend event decision cannot be accepted")
    return _unavailable()


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


def _terms(body: models.ManualEventCreate) -> DividendTerms:
    return DividendTerms.model_validate(body.model_dump(include=set(DividendTerms.model_fields)))


def _binding(
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: events.EventRevision,
) -> dict[str, object]:
    return {
        "event_id": str(record.event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "route": f"{request.method} {request.url.path}",
        "key_sha256": digest(headers.key),
        "intent_sha256": digest(canonical(payload)),
    }


def _observed(
    con: Connection, owner: UUID, identifier: UUID, version: int, revision_id: UUID
) -> events.EventRevision:
    previous = _owned(con, owner, identifier).record
    check_version(previous.revision_no, version)
    if previous.revision_id != revision_id:
        raise ApiError(412, "PRECONDITION_FAILED", "The observed event changed; reload it")
    if version == 2_147_483_647:
        raise events.EventError("DIVIDEND_EVENT_VERSION_EXHAUSTED")
    return previous


def _audit_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: events.EventRevision,
) -> None:
    audit(
        con,
        str(auth.owner.id),
        "dividend_event_revision",
        str(record.revision_id),
        "DIVIDEND_EVENT_HTTP_INTENT",
        request_id=str(record.request_id),
        after=_binding(request, headers, payload, record),
    )


def _check_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: events.EventRevision,
) -> None:
    rows = con.execute(
        text("""
        SELECT owner_id,request_id,reason,before_sha256,after_sha256 FROM audit_events
        WHERE entity_type='dividend_event_revision' AND entity_id=:revision
          AND action='DIVIDEND_EVENT_HTTP_INTENT'
    """),
        {"revision": str(record.revision_id)},
    ).all()
    expected = (
        str(auth.owner.id),
        str(record.request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, record))),
    )
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")


def _write_manual(
    request: Request,
    headers: WriteDep,
    body: models.ManualEventCreate,
    *,
    identifier: UUID | None,
    version: int | None,
) -> models.DividendEventRecord:
    payload = body.model_dump(mode="json")
    if version is not None:
        payload = {"event_version": version, **payload}
    terms = _terms(body)

    def action(con: Connection, auth: Auth) -> models.DividendEventRecord:
        if identifier is None:
            event_id, next_version = uuid4(), 1
        else:
            assert version is not None and isinstance(body, models.ManualEventRevision)
            previous = _observed(con, auth.owner.id, identifier, version, body.revision_id)
            if previous.review_status == "WITHDRAWN" and not body.confirm_reopen:
                raise ApiError(
                    409, "DIVIDEND_EVENT_REOPEN_CONFIRMATION_REQUIRED", "Confirm reopening"
                )
            if previous.manual_locked and not body.confirm_unlock:
                raise ApiError(
                    409, "DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED", "Confirm unlocking"
                )
            event_id, next_version = identifier, version + 1
        intent = writer.EventIntent(
            event_id=event_id,
            revision_id=uuid4(),
            revision_no=next_version,
            kind="MANUAL",
            terms=terms,
            citation=body.citation,
            source_url=body.source_url,
            reason=body.reason,
        )
        saved = writer.save(
            con, auth.owner.id, intent, at=now(), request_id=UUID(request.state.request_id)
        )
        result = _current(saved)
        _audit_intent(con, auth, request, headers, payload, saved.record)
        return result

    def replay(con: Connection, auth: Auth, cached: models.DividendEventRecord) -> None:
        expected = 1 if version is None else version + 1
        if cached.version != expected or (identifier is not None and cached.id != identifier):
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        historical = events.get(con, auth.owner.id, cached.id, version=expected)
        if historical is None or _current(historical) != cached:
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        record = historical.record
        if (
            record.decision_kind != "MANUAL"
            or record.terms != terms
            or record.citation != body.citation
            or record.source_url != body.source_url
            or record.reason != body.reason
            or record.review_status
            != ("PROVISIONAL" if terms.adjustment_basis == "AS_DECLARED" else "INCOMPLETE")
        ):
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        if version is not None:
            assert isinstance(body, models.ManualEventRevision)
            previous = events.get(con, auth.owner.id, cached.id, version=version)
            if (
                previous is None
                or previous.record.revision_id != body.revision_id
                or (previous.record.manual_locked and not body.confirm_unlock)
                or (previous.record.review_status == "WITHDRAWN" and not body.confirm_reopen)
            ):
                raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, record)

    try:
        return mutate(
            request,
            headers,
            payload,
            models.DividendEventRecord,
            action,
            status=201 if identifier is None else 200,
            reauth=True,
            validate_replay=replay,
        )
    except events.EventError as error:
        raise _write_failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


def _write_candidate(
    request: Request,
    headers: WriteDep,
    body: models.CandidateEventCreate | models.CandidateEventRevision,
    *,
    identifier: UUID | None,
    version: int | None,
) -> models.DividendEventRecord:
    payload = body.model_dump(mode="json")
    if version is not None:
        payload = {"event_version": version, **payload}

    def action(con: Connection, auth: Auth) -> models.DividendEventRecord:
        if identifier is None:
            event_id, next_version = uuid4(), 1
        else:
            assert version is not None and isinstance(body, models.CandidateEventRevision)
            _observed(con, auth.owner.id, identifier, version, body.revision_id)
            event_id, next_version = identifier, version + 1
        try:
            candidate = candidates.require_current(
                con, auth.owner.id, body.candidate_id, version=body.candidate_version
            )
        except candidates.CandidateError as error:
            raise events.EventError(str(error)) from None
        if candidate.record.decision.id != body.candidate_revision_id:
            raise events.EventError("CANDIDATE_VERSION_CONFLICT")
        intent = writer.EventIntent(
            event_id=event_id,
            revision_id=uuid4(),
            revision_no=next_version,
            kind="CANDIDATE",
            candidate_id=body.candidate_id,
            candidate_version=body.candidate_version,
            reason=body.reason,
        )
        saved = writer.save(
            con, auth.owner.id, intent, at=now(), request_id=UUID(request.state.request_id)
        )
        result = _current(saved)
        _audit_intent(con, auth, request, headers, payload, saved.record)
        return result

    def replay(con: Connection, auth: Auth, cached: models.DividendEventRecord) -> None:
        expected = 1 if version is None else version + 1
        if cached.version != expected or (identifier is not None and cached.id != identifier):
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        historical = events.get(con, auth.owner.id, cached.id, version=expected)
        if historical is None or _current(historical) != cached:
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        record = historical.record
        if (
            record.decision_kind != "CANDIDATE"
            or record.candidate_revision_id != body.candidate_revision_id
            or record.reason != body.reason
        ):
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        try:
            candidate = candidates.get(
                con, auth.owner.id, body.candidate_id, version=body.candidate_version
            )
        except candidates.CandidateError:
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE") from None
        if candidate is None or candidate.record.decision.id != body.candidate_revision_id:
            raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        if version is not None:
            assert isinstance(body, models.CandidateEventRevision)
            previous = events.get(con, auth.owner.id, cached.id, version=version)
            if previous is None or previous.record.revision_id != body.revision_id:
                raise events.EventError("DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, record)

    try:
        return mutate(
            request,
            headers,
            payload,
            models.DividendEventRecord,
            action,
            status=201 if identifier is None else 200,
            reauth=True,
            validate_replay=replay,
        )
    except events.EventError as error:
        raise _write_failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.post(
    "",
    status_code=201,
    operation_id="create_manual_dividend_event",
    responses={409: {"model": Error}, 412: {"model": Error}},
)
def create_manual_event(
    body: models.ManualEventCreate, request: Request, response: Response, headers: WriteDep
) -> models.DividendEventRecord:
    result = _write_manual(request, headers, body, identifier=None, version=None)
    response.headers["Location"] = f"/api/v1/dividends/events/{result.id}"
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.post(
    "/from-candidate",
    status_code=201,
    operation_id="create_dividend_event_from_candidate",
    responses={409: {"model": Error}, 412: {"model": Error}},
)
def create_from_candidate(
    body: models.CandidateEventCreate, request: Request, response: Response, headers: WriteDep
) -> models.DividendEventRecord:
    result = _write_candidate(request, headers, body, identifier=None, version=None)
    response.headers["Location"] = f"/api/v1/dividends/events/{result.id}"
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.post(
    "/{event_id}/from-candidate",
    operation_id="revise_dividend_event_from_candidate",
    responses={409: {"model": Error}, 412: {"model": Error}},
)
def revise_from_candidate(
    event_id: UUID,
    body: models.CandidateEventRevision,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.DividendEventRecord:
    result = _write_candidate(request, headers, body, identifier=event_id, version=version)
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch(
    "/{event_id}",
    operation_id="revise_dividend_event",
    responses={409: {"model": Error}, 412: {"model": Error}},
)
def revise_manual_event(
    event_id: UUID,
    body: models.ManualEventRevision,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.DividendEventRecord:
    result = _write_manual(request, headers, body, identifier=event_id, version=version)
    response.headers["ETag"] = f'"{result.version}"'
    return result


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
