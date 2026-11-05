"""Private trade endpoints; all financial mutations share one short guarded UoW."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import text

from spm_api import trade_service as service
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.errors import ApiError, Error, Success
from spm_api.master_service import owned_account, owned_portfolio, security_row
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate
from spm_api.security import AuthDep, VersionDep, WriteDep
from spm_api.trade_models import TradeCreate, TradeRecord, TradeRevision, VoidTrade

router = APIRouter(
    prefix="/api/v1",
    tags=["Trades"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


@router.get("/trades", operation_id="list_trades")
def trades(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    portfolio_id: UUID | None = None,
    account_id: UUID | None = None,
    security_id: UUID | None = None,
    from_date: Annotated[IsoDate | None, Query()] = None,
    to_date: Annotated[IsoDate | None, Query()] = None,
    root_id: UUID | None = None,
    include_history: bool = False,
) -> Page[TradeRecord]:
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ApiError(422, "VALIDATION_ERROR", "Date range end precedes start")
    if page.cursor_value is not None:
        try:
            if date.fromisoformat(page.cursor_value).isoformat() != page.cursor_value:
                raise ValueError("Invalid date cursor")
        except ValueError as exc:
            raise ApiError(422, "INVALID_CURSOR", "Invalid trade date cursor") from exc
    with unit_of_work(request.app.state.engine) as con:
        owner = str(auth.owner.id)
        if portfolio_id is not None:
            owned_portfolio(con, owner, str(portfolio_id))
        if account_id is not None:
            owned_account(con, owner, str(account_id))
        if security_id is not None:
            security_row(con, str(security_id))
        if root_id is not None:
            service.owned_trade(con, owner, str(root_id))
        rows = (
            con.execute(
                text(r"""
            SELECT t.* FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
            JOIN securities s ON s.id=t.security_id
            WHERE p.owner_id=:owner AND (:portfolio IS NULL OR t.portfolio_id=:portfolio)
            AND (:account IS NULL OR t.account_id=:account)
            AND (:security IS NULL OR t.security_id=:security)
            AND (:from_date IS NULL OR t.trade_date>=:from_date)
            AND (:to_date IS NULL OR t.trade_date<=:to_date)
            AND (:root IS NULL OR t.root_id=:root) AND (:history OR t.status!='SUPERSEDED')
            AND (t.note LIKE :search ESCAPE '\' OR s.display_ticker LIKE :search ESCAPE '\')
            AND (:cursor_value IS NULL OR (t.trade_date,t.id)<(:cursor_value,:cursor_id))
            ORDER BY t.trade_date DESC,t.id DESC LIMIT :limit
        """),
                {
                    **page.params(),
                    "owner": owner,
                    "portfolio": str(portfolio_id) if portfolio_id is not None else None,
                    "account": str(account_id) if account_id is not None else None,
                    "security": str(security_id) if security_id is not None else None,
                    "from_date": from_date,
                    "to_date": to_date,
                    "root": str(root_id) if root_id is not None else None,
                    "history": include_history,
                },
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "trade_date", service.as_trade)


@router.post("/trades", status_code=201, operation_id="create_trade")
def create_trade(
    body: TradeCreate, request: Request, response: Response, headers: WriteDep
) -> TradeRecord:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        TradeRecord,
        lambda con, auth: service.create_trade(con, auth, body, request.state.request_id),
        status=201,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/trades/{id}", operation_id="get_trade")
def trade(id: UUID, request: Request, response: Response, auth: AuthDep) -> TradeRecord:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_trade(service.owned_trade(con, str(auth.owner.id), str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/trades/{id}", operation_id="revise_trade")
def revise_trade(
    id: UUID,
    body: TradeRevision,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> TradeRecord:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        TradeRecord,
        lambda con, auth: service.revise_trade(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/trades/{id}", operation_id="void_trade")
def void_trade(
    id: UUID, body: VoidTrade, request: Request, headers: WriteDep, version: VersionDep
) -> Success:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        Success,
        lambda con, auth: service.void_trade(
            con, auth, str(id), version, body.reason, request.state.request_id
        ),
    )
