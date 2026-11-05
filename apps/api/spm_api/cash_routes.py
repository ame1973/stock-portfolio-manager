"""Private journal/expense routes; financial writes use the shared atomic mutation UoW."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import text

from spm_api import cash_service as service
from spm_api.cash_models import (
    AccountCashBalances,
    CashDate,
    CashEntry,
    CashEntryCreate,
    CashEntryRevision,
    Expense,
    ExpenseCreate,
    ExpenseRevision,
    VoidFinancial,
)
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.errors import Error, Success
from spm_api.master_models import Currency
from spm_api.master_service import owned_account, owned_portfolio
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate, validate_date_page
from spm_api.security import AuthDep, VersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1",
    tags=["Cash"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


@router.get("/cash-entries", operation_id="list_cash_entries")
def cash_entries(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    account_id: UUID | None = None,
    portfolio_id: UUID | None = None,
    currency: Currency | None = None,
    from_date: Annotated[IsoDate | None, Query()] = None,
    to_date: Annotated[IsoDate | None, Query()] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    source_type: Annotated[str | None, Query(max_length=50)] = None,
    entry_role: Literal["PRIMARY", "REVERSAL"] | None = None,
) -> Page[CashEntry]:
    validate_date_page(page, from_date, to_date)
    with unit_of_work(request.app.state.engine) as con:
        owner = str(auth.owner.id)
        if account_id is not None:
            owned_account(con, owner, str(account_id))
        if portfolio_id is not None:
            owned_portfolio(con, owner, str(portfolio_id))
        rows = (
            con.execute(
                text(
                    service.CASH_SQL
                    + r"""
            AND (:account IS NULL OR c.account_id=:account)
            AND (:portfolio IS NULL OR a.portfolio_id=:portfolio)
            AND (:currency IS NULL OR c.currency=:currency) AND (:kind IS NULL OR c.kind=:kind)
            AND (:source IS NULL OR c.source_type=:source) AND (:role IS NULL OR c.entry_role=:role)
            AND (:start IS NULL OR c.effective_date>=:start)
            AND (:end IS NULL OR c.effective_date<=:end)
            AND c.note LIKE :search ESCAPE '\'
            AND (:cursor_value IS NULL OR (c.effective_date,c.id)<(:cursor_value,:cursor_id))
            ORDER BY c.effective_date DESC,c.id DESC LIMIT :limit
        """
                ),
                {
                    **page.params(),
                    "owner": owner,
                    "account": str(account_id) if account_id else None,
                    "portfolio": str(portfolio_id) if portfolio_id else None,
                    "currency": currency,
                    "kind": kind,
                    "source": source_type,
                    "role": entry_role,
                    "start": from_date,
                    "end": to_date,
                },
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "effective_date", service.as_cash)


@router.post("/cash-entries", status_code=201, operation_id="create_cash_entry")
def create_cash_entry(
    body: CashEntryCreate,
    request: Request,
    response: Response,
    headers: WriteDep,
) -> CashEntry:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        CashEntry,
        lambda con, auth: service.create_cash(con, auth, body, request.state.request_id),
        status=201,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/cash-entries/{id}", operation_id="get_cash_entry")
def cash_entry(id: UUID, request: Request, response: Response, auth: AuthDep) -> CashEntry:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_cash(service.owned_cash(con, str(auth.owner.id), str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/cash-entries/{id}", operation_id="revise_cash_entry")
def revise_cash_entry(
    id: UUID,
    body: CashEntryRevision,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> CashEntry:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        CashEntry,
        lambda con, auth: service.revise_cash(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/cash-entries/{id}", operation_id="void_cash_entry")
def void_cash_entry(
    id: UUID,
    body: VoidFinancial,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
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


@router.get("/accounts/{id}/cash-balances", operation_id="get_cash_balances")
def cash_balances(
    id: UUID,
    request: Request,
    auth: AuthDep,
    as_of: Annotated[CashDate | None, Query()] = None,
) -> AccountCashBalances:
    with unit_of_work(request.app.state.engine) as con:
        return service.cash_balances(
            con, str(auth.owner.id), str(id), as_of or date.today().isoformat()
        )


@router.get("/expenses", operation_id="list_expenses")
def expenses(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    account_id: UUID | None = None,
    portfolio_id: UUID | None = None,
    currency: Currency | None = None,
    from_date: Annotated[IsoDate | None, Query()] = None,
    to_date: Annotated[IsoDate | None, Query()] = None,
    include_history: bool = False,
) -> Page[Expense]:
    validate_date_page(page, from_date, to_date)
    with unit_of_work(request.app.state.engine) as con:
        owner = str(auth.owner.id)
        if account_id is not None:
            owned_account(con, owner, str(account_id))
        if portfolio_id is not None:
            owned_portfolio(con, owner, str(portfolio_id))
        rows = (
            con.execute(
                text(
                    service.EXPENSE_SQL
                    + r"""
            AND (:account IS NULL OR e.account_id=:account)
            AND (:portfolio IS NULL OR a.portfolio_id=:portfolio)
            AND (:currency IS NULL OR e.currency=:currency)
            AND (:start IS NULL OR e.date>=:start) AND (:end IS NULL OR e.date<=:end)
            AND (:history OR e.status!='SUPERSEDED')
            AND (e.note LIKE :search ESCAPE '\' OR e.category LIKE :search ESCAPE '\')
            AND (:cursor_value IS NULL OR (e.date,e.id)<(:cursor_value,:cursor_id))
            ORDER BY e.date DESC,e.id DESC LIMIT :limit
        """
                ),
                {
                    **page.params(),
                    "owner": owner,
                    "account": str(account_id) if account_id else None,
                    "portfolio": str(portfolio_id) if portfolio_id else None,
                    "currency": currency,
                    "start": from_date,
                    "end": to_date,
                    "history": include_history,
                },
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "date", service.as_expense)


@router.post("/expenses", status_code=201, operation_id="create_expense")
def create_expense(
    body: ExpenseCreate,
    request: Request,
    response: Response,
    headers: WriteDep,
) -> Expense:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Expense,
        lambda con, auth: service.create_expense(con, auth, body, request.state.request_id),
        status=201,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/expenses/{id}", operation_id="get_expense")
def expense(id: UUID, request: Request, response: Response, auth: AuthDep) -> Expense:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_expense(service.owned_expense(con, str(auth.owner.id), str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/expenses/{id}", operation_id="revise_expense")
def revise_expense(
    id: UUID,
    body: ExpenseRevision,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> Expense:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Expense,
        lambda con, auth: service.revise_expense(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/expenses/{id}", operation_id="void_expense")
def void_expense(
    id: UUID,
    body: VoidFinancial,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
) -> Success:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        Success,
        lambda con, auth: service.void_expense(
            con, auth, str(id), version, body.reason, request.state.request_id
        ),
    )
