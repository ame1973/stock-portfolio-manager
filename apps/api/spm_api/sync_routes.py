"""Authenticated source-only enqueue/read/cancel APIs, with no provider I/O in request handlers."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

import spm_api.job_service as jobs
import spm_api.sync_service as service
from spm_api.db import unit_of_work
from spm_api.errors import Error
from spm_api.job_models import ItemStatus, JobStatus
from spm_api.master_models import ProviderId
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate, validate_time_page
from spm_api.security import AuthDep, WriteDep
from spm_api.sync_models import (
    CancelJobRequest,
    JobCreated,
    SourceJob,
    SourceJobItem,
    SyncDividendRequest,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Source jobs"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 422, 503)},
)


@router.post("/dividends/sync", status_code=202, operation_id="start_dividend_sync")
def sync_dividends(
    body: SyncDividendRequest,
    request: Request,
    response: Response,
    headers: WriteDep,
) -> JobCreated:
    """Queue source work only; reconciliation and financial adoption do not run."""
    selected = service.selections(request.app.state.settings, body.provider_ids)
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        JobCreated,
        lambda con, auth: service.enqueue(con, auth, body, selected, request.state.request_id),
        status=202,
    )
    response.headers["Location"] = f"/api/v1/jobs/{result.job_id}"
    return result


@router.get("/jobs", operation_id="list_jobs")
def jobs_list(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    status: JobStatus | None = None,
) -> Page[SourceJob]:
    """List supported managed source jobs, never unvalidated retained job payloads."""
    validate_time_page(page)
    with unit_of_work(request.app.state.engine) as con:
        rows = (
            con.execute(
                text(r"""
            SELECT j.* FROM jobs j WHERE j.owner_id=:owner AND j.runtime_version=1 AND j.kind=:kind
              AND (:status IS NULL OR j.status=:status)
              AND (j.kind LIKE :search ESCAPE '\' OR j.id LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (j.created_at,j.id)<(:cursor_value,:cursor_id))
            ORDER BY j.created_at DESC,j.id DESC LIMIT :limit
        """),
                {**page.params(), "owner": str(auth.owner.id), "kind": jobs.KIND, "status": status},
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "created_at", lambda row: service.as_job(con, row))


@router.get("/jobs/{id}", operation_id="get_job")
def job(id: UUID, request: Request, response: Response, auth: AuthDep) -> SourceJob:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_job(con, service.owned_job(con, auth.owner.id, id))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/jobs/{id}/items", operation_id="list_job_items")
def job_items(
    id: UUID,
    request: Request,
    auth: AuthDep,
    page: PageDep,
    status: ItemStatus | None = None,
    provider_id: ProviderId | None = None,
    security_id: UUID | None = None,
) -> Page[SourceJobItem]:
    service.validate_item_cursor(page)
    with unit_of_work(request.app.state.engine) as con:
        service.owned_job(con, auth.owner.id, id)
        rows = (
            con.execute(
                text(r"""
            SELECT i.*,CAST(i.ordinal AS TEXT) AS cursor_ordinal FROM job_items i
            JOIN securities s ON s.id=i.security_id
            WHERE i.job_id=:job AND (:status IS NULL OR i.status=:status)
              AND (:provider IS NULL OR i.provider_id=:provider)
              AND (:security IS NULL OR i.security_id=:security)
              AND (i.provider_id LIKE :search ESCAPE '\' OR i.operation LIKE :search ESCAPE '\'
                OR s.display_ticker LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR
                (i.ordinal,i.id)>(CAST(:cursor_value AS INTEGER),:cursor_id))
            ORDER BY i.ordinal,i.id LIMIT :limit
        """),
                {
                    **page.params(),
                    "job": str(id),
                    "status": status,
                    "provider": provider_id,
                    "security": str(security_id) if security_id is not None else None,
                },
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "cursor_ordinal", service.as_item)


@router.post("/jobs/{id}/cancel", operation_id="cancel_job")
def cancel_job(
    id: UUID,
    request: Request,
    response: Response,
    headers: WriteDep,
    body: CancelJobRequest | None = None,
) -> SourceJob:
    """Accept cancellation intent, not a claim that an in-flight response has already stopped."""
    result = mutate(
        request,
        headers,
        {},
        SourceJob,
        lambda con, auth: service.cancel(con, auth, id, request.state.request_id),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result
