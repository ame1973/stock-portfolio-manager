"""Protected owned schedule metadata/receipts without automatic dispatch or private I/O."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

import spm_api.schedule_service as service
from spm_api.audit import now
from spm_api.db import unit_of_work
from spm_api.errors import Error
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate, validate_name_page, validate_time_page
from spm_api.schedule_models import (
    SourceScheduleCreate,
    SourceScheduleOccurrence,
    SourceScheduleRecord,
    SourceScheduleUpdate,
)
from spm_api.security import AuthDep, VersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/source-schedules",
    tags=["Source schedules"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


@router.get("", operation_id="list_source_schedules")
def schedules(
    request: Request, auth: AuthDep, page: PageDep, enabled: bool | None = None
) -> Page[SourceScheduleRecord]:
    validate_name_page(page)
    with unit_of_work(request.app.state.engine) as con:
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM source_schedules WHERE owner_id=:owner AND name LIKE :search ESCAPE '\'
              AND (:enabled IS NULL OR enabled=:enabled)
              AND (:cursor_value IS NULL OR (name COLLATE NOCASE,id)
                    >(:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY name COLLATE NOCASE,id LIMIT :limit
        """),
                {**page.params(), "owner": str(auth.owner.id), "enabled": enabled},
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "name", lambda row: service.as_schedule(con, row))


@router.post("", status_code=201, operation_id="create_source_schedule")
def create_schedule(
    body: SourceScheduleCreate, request: Request, response: Response, headers: WriteDep
) -> SourceScheduleRecord:
    """Persist a disabled owner definition, not a claim of installed automatic scheduling."""
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        SourceScheduleRecord,
        lambda con, auth: service.as_schedule(
            con,
            service.create(con, auth.owner.id, body, at=now(), request_id=request.state.request_id),
        ),
        status=201,
    )
    response.headers["Location"] = f"/api/v1/source-schedules/{result.id}"
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/{id}", operation_id="get_source_schedule")
def schedule(id: UUID, request: Request, response: Response, auth: AuthDep) -> SourceScheduleRecord:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_schedule(con, service.owned_schedule(con, auth.owner.id, id))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/{id}", operation_id="update_source_schedule")
def update_schedule(
    id: UUID,
    body: SourceScheduleUpdate,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> SourceScheduleRecord:
    result = mutate(
        request,
        headers,
        {"version": version, **body.model_dump(mode="json")},
        SourceScheduleRecord,
        lambda con, auth: service.as_schedule(
            con,
            service.revise(
                con,
                auth.owner.id,
                id,
                body,
                version=version,
                at=now(),
                request_id=request.state.request_id,
            ),
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/{id}/occurrences", operation_id="list_source_schedule_occurrences")
def occurrences(
    id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[SourceScheduleOccurrence]:
    validate_time_page(page)
    with unit_of_work(request.app.state.engine) as con:
        service.owned_schedule(con, auth.owner.id, id)
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM source_schedule_occurrences
            WHERE schedule_id=:schedule AND owner_id=:owner
              AND (kind LIKE :search ESCAPE '\' OR outcome LIKE :search ESCAPE '\'
                    OR id LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (due_at,id)<(:cursor_value,:cursor_id))
            ORDER BY due_at DESC,id DESC LIMIT :limit
        """),
                {**page.params(), "schedule": str(id), "owner": str(auth.owner.id)},
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "due_at", lambda row: service.as_occurrence(con, row))
