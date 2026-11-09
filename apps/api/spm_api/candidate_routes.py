"""Protected owner candidate review; never verify or adopt canonical dividend facts."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_candidate_service as service
from spm_api.audit import now
from spm_api.candidate_models import (
    CandidateAssessment,
    CandidateEvidenceCheck,
    CandidateRecord,
    CandidateWrite,
)
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error
from spm_api.master_service import security_row
from spm_api.mutations import check_version, mutate
from spm_api.pagination import Page, PageDep, paginate, validate_time_page, validate_version_page
from spm_api.security import Auth, AuthDep, WriteDep, matching_version

router = APIRouter(
    prefix="/api/v1/dividends/candidates",
    tags=["Dividend candidate review"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _version(
    value: Annotated[str, Header(alias="If-Match", pattern=r'^"[1-9][0-9]{0,9}"$')],
) -> int:
    result = matching_version(value)
    if result > 2_147_483_647:
        raise ApiError(422, "INVALID_CANDIDATE_VERSION", "The candidate version is invalid")
    return result


CandidateVersion = Annotated[int, Depends(_version)]


def _public(review: service.ReviewedCandidate) -> CandidateRecord:
    record, assessment = review.record, review.assessment
    decision = record.decision
    return CandidateRecord(
        id=decision.candidate_id,
        revision_id=decision.id,
        version=decision.revision_no,
        state=decision.state,
        terms=decision.terms,
        matches=decision.matches,
        change_reason=decision.change_reason,
        created_at=record.created_at,
        assessment=CandidateAssessment(
            status=assessment.status,
            independent_origins=tuple(sorted(assessment.independent_origins)),
            eligible_observation_ids=assessment.eligible_observation_ids,
            conflicting_fields=tuple(sorted(assessment.conflicting_fields)),
            incomplete_reasons=tuple(sorted(assessment.incomplete_reasons)),
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
                for check in assessment.checks
            ),
        ),
    )


def _owned(
    con: Connection, owner: UUID, identifier: UUID, *, version: int | None = None
) -> service.ReviewedCandidate:
    result = service.get(con, owner, identifier, version=version)
    if result is None:
        raise service.CandidateError("CANDIDATE_NOT_FOUND")
    return result


def _decision(body: CandidateWrite, identifier: UUID, version: int) -> service.CandidateDecision:
    return service.CandidateDecision(
        id=uuid4(),
        candidate_id=identifier,
        revision_no=version,
        state=body.state,
        terms=body.terms,
        matches=body.matches,
        change_reason=body.change_reason,
    )


def _validate_replay(
    con: Connection,
    auth: Auth,
    cached: CandidateRecord,
    body: CandidateWrite,
    *,
    identifier: UUID | None,
    version: int,
) -> None:
    if cached.version != version or (identifier is not None and cached.id != identifier):
        raise service.CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")
    historical = service.get(con, auth.owner.id, cached.id, version=version)
    if historical is None or _public(historical) != cached:
        raise service.CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")
    decision = historical.record.decision
    original = CandidateWrite(
        state=decision.state,
        terms=decision.terms,
        matches=decision.matches,
        change_reason=decision.change_reason,
    )
    if original != body:
        raise service.CandidateError("CANDIDATE_HISTORY_UNAVAILABLE")


def _write(
    request: Request,
    headers: WriteDep,
    body: CandidateWrite,
    *,
    identifier: UUID | None,
    version: int | None,
) -> CandidateRecord:
    def record(con: Connection, auth: Auth) -> CandidateRecord:
        if identifier is None:
            candidate_id, next_version = uuid4(), 1
        else:
            assert version is not None
            previous = _owned(con, auth.owner.id, identifier)
            check_version(previous.record.decision.revision_no, version)
            if version == 2_147_483_647:
                raise service.CandidateError("CANDIDATE_VERSION_EXHAUSTED")
            candidate_id, next_version = identifier, version + 1
        return _public(
            service.save(
                con,
                auth.owner.id,
                _decision(body, candidate_id, next_version),
                at=now(),
                request_id=UUID(request.state.request_id),
            )
        )

    def replay(con: Connection, auth: Auth, cached: CandidateRecord) -> None:
        _validate_replay(
            con,
            auth,
            cached,
            body,
            identifier=identifier,
            version=1 if version is None else version + 1,
        )

    payload = body.model_dump(mode="json")
    if version is not None:
        payload = {"version": version, **payload}
    try:
        return mutate(
            request,
            headers,
            payload,
            CandidateRecord,
            record,
            status=201 if identifier is None else 200,
            validate_replay=replay,
        )
    except service.CandidateError:
        raise
    except (ValueError, PydanticSerializationError):
        raise ApiError(
            503, "CANDIDATE_HISTORY_UNAVAILABLE", "The candidate cannot be read safely"
        ) from None


def _from_row(con: Connection, owner: UUID, row: RowMapping) -> CandidateRecord:
    try:
        identifier = UUID(row["candidate_id"])
        if (
            str(identifier) != row["candidate_id"]
            or row["joined_security_id"] != row["security_id"]
            or type(row["revision_no"]) is not int
        ):
            raise ValueError
        result = _owned(con, owner, identifier, version=row["revision_no"])
        if str(result.record.decision.id) != row["id"]:
            raise ValueError
        return _public(result)
    except service.CandidateError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise service.CandidateError("CANDIDATE_HISTORY_UNAVAILABLE") from None


@router.get("", operation_id="list_dividend_candidates")
def candidates(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    security_id: UUID | None = None,
    state: Literal["OPEN", "DISMISSED"] | None = None,
) -> Page[CandidateRecord]:
    validate_time_page(page)
    with unit_of_work(request.app.state.engine) as con:
        if security_id is not None:
            security_row(con, str(security_id))
        rows = (
            con.execute(
                text(r"""
            SELECT c.*,s.id AS joined_security_id FROM dividend_candidate_revisions c
            LEFT JOIN securities s ON s.id=c.security_id
            WHERE c.owner_id=:owner AND (:security IS NULL OR c.security_id=:security)
              AND (:state IS NULL OR c.state=:state)
              AND NOT EXISTS(SELECT 1 FROM dividend_candidate_revisions newer
                    WHERE newer.candidate_id=c.candidate_id AND newer.owner_id=c.owner_id
                      AND newer.revision_no>c.revision_no)
              AND (c.candidate_id LIKE :search ESCAPE '\'
                    OR s.display_ticker LIKE :search ESCAPE '\' OR s.name LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (c.created_at,c.id)<(:cursor_value,:cursor_id))
            ORDER BY c.created_at DESC,c.id DESC LIMIT :limit
        """),
                {
                    **page.params(),
                    "owner": str(auth.owner.id),
                    "security": str(security_id) if security_id is not None else None,
                    "state": state,
                },
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "created_at", lambda row: _from_row(con, auth.owner.id, row))


@router.post("", status_code=201, operation_id="create_dividend_candidate")
def create_candidate(
    body: CandidateWrite, request: Request, response: Response, headers: WriteDep
) -> CandidateRecord:
    result = _write(request, headers, body, identifier=None, version=None)
    response.headers["Location"] = f"/api/v1/dividends/candidates/{result.id}"
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/{id}", operation_id="get_dividend_candidate")
def candidate(id: UUID, request: Request, response: Response, auth: AuthDep) -> CandidateRecord:
    with unit_of_work(request.app.state.engine) as con:
        result = _public(_owned(con, auth.owner.id, id))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/{id}", operation_id="revise_dividend_candidate")
def revise_candidate(
    id: UUID,
    body: CandidateWrite,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: CandidateVersion,
) -> CandidateRecord:
    result = _write(request, headers, body, identifier=id, version=version)
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/{id}/revisions", operation_id="list_dividend_candidate_revisions")
def revisions(id: UUID, request: Request, auth: AuthDep, page: PageDep) -> Page[CandidateRecord]:
    validate_version_page(page)
    with unit_of_work(request.app.state.engine) as con:
        _owned(con, auth.owner.id, id)
        rows = (
            con.execute(
                text(r"""
            SELECT c.*,s.id AS joined_security_id FROM dividend_candidate_revisions c
            LEFT JOIN securities s ON s.id=c.security_id
            WHERE c.owner_id=:owner AND c.candidate_id=:candidate
              AND (c.id LIKE :search ESCAPE '\' OR c.change_reason LIKE :search ESCAPE '\'
                    OR CAST(c.revision_no AS TEXT) LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (c.revision_no,c.id)<(:cursor_value,:cursor_id))
            ORDER BY c.revision_no DESC,c.id DESC LIMIT :limit
        """),
                {
                    **page.params(),
                    "owner": str(auth.owner.id),
                    "candidate": str(id),
                    "cursor_value": int(page.cursor_value)
                    if page.cursor_value is not None
                    else None,
                },
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "revision_no", lambda row: _from_row(con, auth.owner.id, row))
