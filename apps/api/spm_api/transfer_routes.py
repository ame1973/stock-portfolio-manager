"""Private paired cash and securities-transfer endpoints with bounded immutable history."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import RowMapping, text

from spm_api import transfer_service as service
from spm_api.cash_models import VoidFinancial
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.errors import Error, Success
from spm_api.master_models import Currency
from spm_api.master_service import owned_account, owned_portfolio, security_row
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, PageQuery, paginate, validate_date_page
from spm_api.security import Auth, AuthDep, VersionDep, WriteDep
from spm_api.transfer_models import (
    CashTransfer,
    CashTransferCreate,
    SecurityTransfer,
    SecurityTransferCreate,
    SecurityTransferRevision,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Transfers"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _rows(
    request: Request,
    auth: Auth,
    page: PageQuery,
    kind: Literal["cash", "security"],
    portfolio: UUID | None,
    account: UUID | None,
    root: UUID | None,
    start: str | None,
    end: str | None,
    history: bool,
    *,
    security: UUID | None = None,
    currency: Currency | None = None,
) -> list[RowMapping]:
    validate_date_page(page, start, end)
    with unit_of_work(request.app.state.engine) as con:
        owner = str(auth.owner.id)
        if portfolio is not None:
            owned_portfolio(con, owner, str(portfolio))
        if account is not None:
            owned_account(con, owner, str(account))
        if root is not None:
            service.owned_transfer(con, owner, str(root), kind)
        if security is not None:
            security_row(con, str(security))
        sql = service.CASH_SQL if kind == "cash" else service.SECURITY_SQL
        current = "t.status!='SUPERSEDED'" if kind == "cash" else "t.is_current=1"
        extra = (
            "AND (:currency IS NULL OR t.source_currency=:currency OR t.target_currency=:currency)"
            if kind == "cash"
            else "AND (:security IS NULL OR t.security_id=:security)"
        )
        return list(
            con.execute(
                text(
                    sql
                    + f"""
            AND (:portfolio IS NULL OR t.portfolio_id=:portfolio)
            AND (:account IS NULL OR t.source_account_id=:account OR t.target_account_id=:account)
            AND (:root IS NULL OR t.root_id=:root) AND (:history OR {current})
            AND (:start IS NULL OR t.transfer_date>=:start)
            AND (:end IS NULL OR t.transfer_date<=:end)
            {extra} AND t.note LIKE :search ESCAPE '\\'
            AND (:cursor_value IS NULL OR (t.transfer_date,t.id)<(:cursor_value,:cursor_id))
            ORDER BY t.transfer_date DESC,t.id DESC LIMIT :limit
        """
                ),
                {
                    **page.params(),
                    "owner": owner,
                    "portfolio": str(portfolio) if portfolio else None,
                    "account": str(account) if account else None,
                    "root": str(root) if root else None,
                    "security": str(security) if security else None,
                    "currency": currency,
                    "start": start,
                    "end": end,
                    "history": history,
                },
            ).mappings()
        )


@router.get("/cash-transfers", operation_id="list_cash_transfers")
def cash_transfers(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    portfolio_id: UUID | None = None,
    account_id: UUID | None = None,
    root_id: UUID | None = None,
    from_date: Annotated[IsoDate | None, Query()] = None,
    to_date: Annotated[IsoDate | None, Query()] = None,
    currency: Currency | None = None,
    include_history: bool = False,
) -> Page[CashTransfer]:
    rows = _rows(
        request,
        auth,
        page,
        "cash",
        portfolio_id,
        account_id,
        root_id,
        from_date,
        to_date,
        include_history,
        currency=currency,
    )
    return paginate(rows, page, "transfer_date", service.as_cash)


@router.post("/cash-transfers", status_code=201, operation_id="create_cash_transfer")
def create_cash_transfer(
    body: CashTransferCreate, request: Request, response: Response, headers: WriteDep
) -> CashTransfer:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        CashTransfer,
        lambda con, auth: service.create_cash(con, auth, body, request.state.request_id),
        status=201,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/cash-transfers/{id}", operation_id="get_cash_transfer")
def cash_transfer(id: UUID, request: Request, response: Response, auth: AuthDep) -> CashTransfer:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_cash(service.owned_transfer(con, str(auth.owner.id), str(id), "cash"))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/cash-transfers/{id}", operation_id="revise_cash_transfer")
def revise_cash_transfer(
    id: UUID,
    body: CashTransferCreate,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> CashTransfer:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        CashTransfer,
        lambda con, auth: service.revise_cash(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/cash-transfers/{id}", operation_id="void_cash_transfer")
def void_cash_transfer(
    id: UUID, body: VoidFinancial, request: Request, headers: WriteDep, version: VersionDep
) -> Success:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        Success,
        lambda con, auth: service.void_cash(
            con, auth, str(id), version, body.reason, request.state.request_id
        ),
    )


@router.get("/security-transfers", operation_id="list_security_transfers")
def security_transfers(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    portfolio_id: UUID | None = None,
    account_id: UUID | None = None,
    root_id: UUID | None = None,
    security_id: UUID | None = None,
    from_date: Annotated[IsoDate | None, Query()] = None,
    to_date: Annotated[IsoDate | None, Query()] = None,
    include_history: bool = False,
) -> Page[SecurityTransfer]:
    rows = _rows(
        request,
        auth,
        page,
        "security",
        portfolio_id,
        account_id,
        root_id,
        from_date,
        to_date,
        include_history,
        security=security_id,
    )
    return paginate(rows, page, "transfer_date", service.as_security)


@router.post("/security-transfers", status_code=201, operation_id="create_security_transfer")
def create_security_transfer(
    body: SecurityTransferCreate, request: Request, response: Response, headers: WriteDep
) -> SecurityTransfer:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        SecurityTransfer,
        lambda con, auth: service.create_security(con, auth, body, request.state.request_id),
        status=201,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/security-transfers/{id}", operation_id="get_security_transfer")
def security_transfer(
    id: UUID, request: Request, response: Response, auth: AuthDep
) -> SecurityTransfer:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_security(
            service.owned_transfer(con, str(auth.owner.id), str(id), "security")
        )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/security-transfers/{id}", operation_id="revise_security_transfer")
def revise_security_transfer(
    id: UUID,
    body: SecurityTransferRevision,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> SecurityTransfer:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        SecurityTransfer,
        lambda con, auth: service.revise_security(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/security-transfers/{id}", operation_id="void_security_transfer")
def void_security_transfer(
    id: UUID, body: VoidFinancial, request: Request, headers: WriteDep, version: VersionDep
) -> Success:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        Success,
        lambda con, auth: service.void_security(
            con, auth, str(id), version, body.reason, request.state.request_id
        ),
    )
