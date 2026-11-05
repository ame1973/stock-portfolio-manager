"""Authenticated installation-wide corporate-action terms and immutable history endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import text

from spm_api import action_service as service
from spm_api.action_models import ActionType, CorporateAction, CorporateActionCreate
from spm_api.cash_models import VoidFinancial
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.errors import Error, Success
from spm_api.master_service import security_row
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate, validate_date_page
from spm_api.security import AuthDep, VersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1",
    tags=["CorporateActions"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


@router.get("/corporate-actions", operation_id="list_corporate_actions")
def actions(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    security_id: UUID | None = None,
    root_id: UUID | None = None,
    action_type: ActionType | None = None,
    status: Literal["CONFIRMED", "NEEDS_REVIEW", "VOID"] | None = None,
    from_date: Annotated[IsoDate | None, Query()] = None,
    to_date: Annotated[IsoDate | None, Query()] = None,
    include_history: bool = False,
) -> Page[CorporateAction]:
    validate_date_page(page, from_date, to_date)
    with unit_of_work(request.app.state.engine) as con:
        if security_id is not None:
            security_row(con, str(security_id))
        if root_id is not None:
            service.action_row(con, str(root_id))
        rows = (
            con.execute(
                text(
                    service.ACTION_SQL
                    + r"""
            AND (:security IS NULL OR c.security_id=:security)
            AND (:root IS NULL OR c.root_id=:root)
            AND (:type IS NULL OR c.action_type=:type) AND (:status IS NULL OR c.status=:status)
            AND (:history OR c.is_current=1)
            AND (:start IS NULL OR c.effective_date>=:start)
            AND (:end IS NULL OR c.effective_date<=:end)
            AND (c.note LIKE :search ESCAPE '\' OR c.reason LIKE :search ESCAPE '\'
                 OR c.new_symbol LIKE :search ESCAPE '\')
            AND (:cursor_value IS NULL OR (c.effective_date,c.id)<(:cursor_value,:cursor_id))
            ORDER BY c.effective_date DESC,c.id DESC LIMIT :limit
        """
                ),
                {
                    **page.params(),
                    "security": str(security_id) if security_id else None,
                    "root": str(root_id) if root_id else None,
                    "type": action_type,
                    "status": status,
                    "history": include_history,
                    "start": from_date,
                    "end": to_date,
                },
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "effective_date", service.as_action)


@router.post("/corporate-actions", status_code=201, operation_id="create_corporate_action")
def create_action(
    body: CorporateActionCreate, request: Request, response: Response, headers: WriteDep
) -> CorporateAction:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        CorporateAction,
        lambda con, auth: service.create_action(con, auth, body, request.state.request_id),
        status=201,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/corporate-actions/{id}", operation_id="get_corporate_action")
def action(id: UUID, request: Request, response: Response, auth: AuthDep) -> CorporateAction:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_action(service.action_row(con, str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/corporate-actions/{id}", operation_id="revise_corporate_action")
def revise_action(
    id: UUID,
    body: CorporateActionCreate,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> CorporateAction:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        CorporateAction,
        lambda con, auth: service.revise_action(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/corporate-actions/{id}", operation_id="void_corporate_action")
def void_action(
    id: UUID, body: VoidFinancial, request: Request, headers: WriteDep, version: VersionDep
) -> Success:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        Success,
        lambda con, auth: service.void_action(
            con, auth, str(id), version, body.reason, request.state.request_id
        ),
    )
