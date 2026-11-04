"""Authenticated, paginated master-data endpoints backed by atomic services."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from spm_api import master_service as service
from spm_api.db import unit_of_work
from spm_api.errors import Error, Success
from spm_api.master_models import (
    Account,
    AccountCreate,
    AccountUpdate,
    Broker,
    BrokerCreate,
    BrokerUpdate,
    Exchange,
    Identifier,
    IdentifierCreate,
    MappingPeriodUpdate,
    Portfolio,
    PortfolioCreate,
    PortfolioUpdate,
    ProviderSymbol,
    ProviderSymbolCreate,
    Security,
    SecurityCreate,
    SecurityUpdate,
)
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate
from spm_api.security import AuthDep, VersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1",
    tags=["Master data"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


@router.get("/portfolios", operation_id="list_portfolios")
def portfolios(request: Request, auth: AuthDep, page: PageDep) -> Page[Portfolio]:
    with unit_of_work(request.app.state.engine) as con:
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM portfolios WHERE owner_id=:owner AND name LIKE :search ESCAPE '\'
            AND (:cursor_value IS NULL OR (name COLLATE NOCASE,id)
                > (:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY name COLLATE NOCASE,id LIMIT :limit
        """),
                {**page.params(), "owner": str(auth.owner.id)},
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "name", service.as_portfolio)


@router.post("/portfolios", status_code=201, operation_id="create_portfolio")
def create_portfolio(body: PortfolioCreate, request: Request, headers: WriteDep) -> Portfolio:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Portfolio,
        lambda con, auth: service.create_portfolio(con, auth, body, request.state.request_id),
        status=201,
    )


@router.get("/portfolios/{id}", operation_id="get_portfolio")
def portfolio(id: UUID, request: Request, response: Response, auth: AuthDep) -> Portfolio:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_portfolio(service.owned_portfolio(con, str(auth.owner.id), str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/portfolios/{id}", operation_id="update_portfolio")
def update_portfolio(
    id: UUID,
    body: PortfolioUpdate,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
    response: Response,
) -> Portfolio:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Portfolio,
        lambda con, auth: service.update_portfolio(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/brokers", operation_id="list_brokers")
def brokers(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    status: Literal["ACTIVE", "ARCHIVED"] | None = None,
) -> Page[Broker]:
    with unit_of_work(request.app.state.engine) as con:
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM brokers WHERE owner_id=:owner AND name LIKE :search ESCAPE '\'
            AND (:status IS NULL OR status=:status)
            AND (:cursor_value IS NULL OR (name COLLATE NOCASE,id)
                > (:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY name COLLATE NOCASE,id LIMIT :limit
        """),
                {**page.params(), "owner": str(auth.owner.id), "status": status},
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "name", service.as_broker)


@router.post("/brokers", status_code=201, operation_id="create_broker")
def create_broker(body: BrokerCreate, request: Request, headers: WriteDep) -> Broker:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Broker,
        lambda con, auth: service.create_broker(con, auth, body, request.state.request_id),
        status=201,
    )


@router.patch("/brokers/{id}", operation_id="update_broker")
def update_broker(
    id: UUID,
    body: BrokerUpdate,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
    response: Response,
) -> Broker:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Broker,
        lambda con, auth: service.update_broker(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/brokers/{id}", operation_id="archive_broker")
def archive_broker(id: UUID, request: Request, headers: WriteDep, version: VersionDep) -> Success:
    return mutate(
        request,
        headers,
        {},
        Success,
        lambda con, auth: service.archive_broker(
            con, auth, str(id), version, request.state.request_id
        ),
    )


@router.get("/accounts", operation_id="list_accounts")
def accounts(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    portfolio_id: UUID | None = None,
    status: Literal["ACTIVE", "ARCHIVED"] | None = None,
) -> Page[Account]:
    with unit_of_work(request.app.state.engine) as con:
        if portfolio_id:
            service.owned_portfolio(con, str(auth.owner.id), str(portfolio_id))
        rows = (
            con.execute(
                text(r"""
            SELECT a.* FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
            WHERE p.owner_id=:owner AND a.name LIKE :search ESCAPE '\'
            AND (:portfolio IS NULL OR a.portfolio_id=:portfolio)
            AND (:status IS NULL OR a.status=:status)
            AND (:cursor_value IS NULL OR (a.name COLLATE NOCASE,a.id)
                > (:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY a.name COLLATE NOCASE,a.id LIMIT :limit
        """),
                {
                    **page.params(),
                    "owner": str(auth.owner.id),
                    "portfolio": str(portfolio_id) if portfolio_id else None,
                    "status": status,
                },
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "name", service.as_account)


@router.post("/accounts", status_code=201, operation_id="create_account")
def create_account(body: AccountCreate, request: Request, headers: WriteDep) -> Account:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Account,
        lambda con, auth: service.create_account(con, auth, body, request.state.request_id),
        status=201,
    )


@router.get("/accounts/{id}", operation_id="get_account")
def account(id: UUID, request: Request, response: Response, auth: AuthDep) -> Account:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_account(service.owned_account(con, str(auth.owner.id), str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/accounts/{id}", operation_id="update_account")
def update_account(
    id: UUID,
    body: AccountUpdate,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
    response: Response,
) -> Account:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Account,
        lambda con, auth: service.update_account(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/accounts/{id}", operation_id="archive_account")
def archive_account(id: UUID, request: Request, headers: WriteDep, version: VersionDep) -> Success:
    return mutate(
        request,
        headers,
        {},
        Success,
        lambda con, auth: service.archive_account(
            con, auth, str(id), version, request.state.request_id
        ),
    )


@router.get("/exchanges", operation_id="list_exchanges")
def exchanges(request: Request, auth: AuthDep) -> Page[Exchange]:
    with unit_of_work(request.app.state.engine) as con:
        rows = con.execute(text("SELECT * FROM exchanges ORDER BY code")).mappings().all()
    return Page(items=[Exchange.model_validate(dict(row)) for row in rows])


@router.get("/securities", operation_id="list_securities")
def securities(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    exchange_code: str | None = None,
    market: Literal["HK", "US"] | None = None,
) -> Page[Security]:
    with unit_of_work(request.app.state.engine) as con:
        rows = (
            con.execute(
                text(r"""
            SELECT s.* FROM securities s JOIN exchanges e ON e.code=s.exchange_code
            WHERE (s.name LIKE :search ESCAPE '\' OR s.display_ticker LIKE :search ESCAPE '\')
            AND (:exchange IS NULL OR s.exchange_code=:exchange)
            AND (:market IS NULL OR e.market=:market)
            AND (:cursor_value IS NULL OR (s.display_ticker COLLATE NOCASE,s.id)
                > (:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY s.display_ticker COLLATE NOCASE,s.id LIMIT :limit
        """),
                {**page.params(), "exchange": exchange_code, "market": market},
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "display_ticker", service.as_security)


@router.post("/securities", status_code=201, operation_id="create_security")
def create_security(body: SecurityCreate, request: Request, headers: WriteDep) -> Security:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Security,
        lambda con, auth: service.create_security(con, auth, body, request.state.request_id),
        status=201,
    )


@router.get("/securities/{id}", operation_id="get_security")
def security(id: UUID, request: Request, response: Response, auth: AuthDep) -> Security:
    with unit_of_work(request.app.state.engine) as con:
        result = service.as_security(service.security_row(con, str(id)))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/securities/{id}", operation_id="update_security")
def update_security(
    id: UUID,
    body: SecurityUpdate,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
    response: Response,
) -> Security:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Security,
        lambda con, auth: service.update_security(
            con, auth, str(id), version, body, request.state.request_id
        ),
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/securities/{id}/identifiers", operation_id="list_identifiers")
def identifiers(id: UUID, request: Request, auth: AuthDep, page: PageDep) -> Page[Identifier]:
    with unit_of_work(request.app.state.engine) as con:
        service.security_row(con, str(id))
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM security_identifiers WHERE security_id=:security
            AND identifier_value LIKE :search ESCAPE '\'
            AND (:cursor_value IS NULL OR (identifier_value COLLATE NOCASE,id)
                > (:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY identifier_value COLLATE NOCASE,id LIMIT :limit
        """),
                {**page.params(), "security": str(id)},
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "identifier_value", service.as_identifier)


@router.post("/securities/{id}/identifiers", status_code=201, operation_id="add_identifier")
def add_identifier(
    id: UUID, body: IdentifierCreate, request: Request, headers: WriteDep
) -> Identifier:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Identifier,
        lambda con, auth: service.add_identifier(
            con, auth, str(id), body, request.state.request_id
        ),
        status=201,
    )


@router.patch("/securities/{id}/identifiers/{mapping_id}", operation_id="correct_identifier_period")
def correct_identifier(
    id: UUID,
    mapping_id: UUID,
    body: MappingPeriodUpdate,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
) -> Identifier:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        Identifier,
        lambda con, auth: Identifier.model_validate(
            service.update_mapping(
                con,
                auth,
                str(id),
                str(mapping_id),
                version,
                body,
                request.state.request_id,
                provider=False,
            )
        ),
    )


@router.get("/securities/{id}/provider-symbols", operation_id="list_provider_symbols")
def provider_symbols(
    id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[ProviderSymbol]:
    with unit_of_work(request.app.state.engine) as con:
        service.security_row(con, str(id))
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM provider_symbols WHERE security_id=:security
            AND provider_symbol LIKE :search ESCAPE '\'
            AND (:cursor_value IS NULL OR (provider_symbol COLLATE NOCASE,id)
                > (:cursor_value COLLATE NOCASE,:cursor_id))
            ORDER BY provider_symbol COLLATE NOCASE,id LIMIT :limit
        """),
                {**page.params(), "security": str(id)},
            )
            .mappings()
            .all()
        )
    return paginate(rows, page, "provider_symbol", service.as_provider_symbol)


@router.post(
    "/securities/{id}/provider-symbols", status_code=201, operation_id="add_provider_symbol"
)
def add_provider_symbol(
    id: UUID, body: ProviderSymbolCreate, request: Request, headers: WriteDep
) -> ProviderSymbol:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        ProviderSymbol,
        lambda con, auth: service.add_provider_symbol(
            con, auth, str(id), body, request.state.request_id
        ),
        status=201,
    )


@router.patch(
    "/securities/{id}/provider-symbols/{mapping_id}", operation_id="correct_provider_symbol_period"
)
def correct_provider_symbol(
    id: UUID,
    mapping_id: UUID,
    body: MappingPeriodUpdate,
    request: Request,
    headers: WriteDep,
    version: VersionDep,
) -> ProviderSymbol:
    return mutate(
        request,
        headers,
        body.model_dump(mode="json", exclude_unset=True),
        ProviderSymbol,
        lambda con, auth: ProviderSymbol.model_validate(
            service.update_mapping(
                con,
                auth,
                str(id),
                str(mapping_id),
                version,
                body,
                request.state.request_id,
                provider=True,
            )
        ),
    )
