"""Private trade endpoints; all financial mutations share one short guarded UoW."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

from spm_api import trade_audit
from spm_api import trade_service as service
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.errors import ApiError, Error, Success
from spm_api.master_service import owned_account, owned_portfolio, security_row
from spm_api.mutations import check_version, mutate
from spm_api.pagination import Page, PageDep, paginate, validate_date_page
from spm_api.security import Auth, AuthDep, VersionDep, WriteDep, matching_version
from spm_api.trade_models import (
    TradeAuditOriginRecord,
    TradeAuditRecovery,
    TradeCreate,
    TradeRecord,
    TradeRevision,
    VoidTrade,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Trades"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _recovery_version(
    value: Annotated[str, Header(alias="If-Match", pattern=r'^"[1-9][0-9]{0,18}"$')],
) -> int:
    version = matching_version(value)
    if version > INT64_MAX:
        raise ApiError(422, "INVALID_TRADE_VERSION", "The trade version is invalid")
    return version


RecoveryVersion = Annotated[int, Depends(_recovery_version)]


def _origin_unavailable() -> ApiError:
    return ApiError(503, "TRADE_AUDIT_UNAVAILABLE", "The trade audit history is unavailable")


def _origin_record(
    row: RowMapping, origin: trade_audit.TradeAuditOrigin | None
) -> TradeAuditOriginRecord:
    return TradeAuditOriginRecord(
        trade_id=row["id"],
        trade_version=row["version"],
        trade_status=row["status"],
        origin=origin.capture_mode if origin is not None else "MISSING",
        captured_at=origin.captured_at if origin is not None else None,
    )


def _owned_origin(
    con: Connection, owner: UUID, identifier: UUID
) -> tuple[RowMapping, trade_audit.TradeAuditOrigin | None, TradeAuditOriginRecord]:
    row = service.owned_trade(con, str(owner), str(identifier))
    try:
        origin = trade_audit.get_origin(con, owner, identifier)
        return row, origin, _origin_record(row, origin)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise _origin_unavailable() from None


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
    validate_date_page(page, from_date, to_date)
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


@router.get("/trades/{id}/audit-origin", operation_id="get_trade_audit_origin")
def trade_audit_origin(id: UUID, request: Request, auth: AuthDep) -> TradeAuditOriginRecord:
    with unit_of_work(request.app.state.engine) as con:
        return _owned_origin(con, auth.owner.id, id)[2]


@router.post("/trades/{id}/audit-origin/recover", operation_id="recover_trade_audit_origin")
def recover_trade_audit_origin(
    id: UUID,
    body: TradeAuditRecovery,
    request: Request,
    headers: WriteDep,
    version: RecoveryVersion,
) -> TradeAuditOriginRecord:
    """Reauthenticate an owned recovery; matched hashes are not ownership verification."""

    def recover(con: Connection, auth: Auth) -> TradeAuditOriginRecord:
        row, existing, _ = _owned_origin(con, auth.owner.id, id)
        check_version(row["version"], version)
        if existing is not None:
            raise ApiError(
                409, "TRADE_ORIGIN_ALREADY_RETAINED", "The trade origin is already retained"
            )
        try:
            origin = trade_audit.recover_from_response(
                con,
                auth.owner.id,
                id,
                body.original_response,
                UUID(request.state.request_id),
                body.reason,
            )
        except trade_audit.TradeAuditError as error:
            if str(error) == "INVALID_TRADE_AUDIT_SCOPE":
                raise ApiError(
                    422, "INVALID_TRADE_ORIGIN", "The recovery request is invalid"
                ) from None
            if str(error) == "TRADE_AUDIT_NOT_FOUND":
                raise _origin_unavailable() from None
            if str(error) == "TRADE_AUDIT_ORIGIN_EXISTS":
                raise ApiError(
                    409, "TRADE_ORIGIN_ALREADY_RETAINED", "The trade origin is already retained"
                ) from None
            raise ApiError(
                409,
                "TRADE_ORIGIN_REVIEW_REQUIRED",
                "The original response or audit history cannot be matched",
            ) from None
        return _origin_record(row, origin)

    def validate_replay(con: Connection, auth: Auth, cached: TradeAuditOriginRecord) -> None:
        row, origin, actual = _owned_origin(con, auth.owner.id, id)
        if (
            origin is None
            or origin.capture_mode != "RETAINED_RESPONSE"
            or cached != actual
            or row["version"] != version
        ):
            raise _origin_unavailable()
        try:
            historical = trade_audit.recover_from_response(
                con,
                auth.owner.id,
                id,
                body.original_response,
                origin.capture_request_id,
                body.reason,
            )
        except trade_audit.TradeAuditError:
            raise _origin_unavailable() from None
        if historical != origin:
            raise _origin_unavailable()

    try:
        return mutate(
            request,
            headers,
            {"version": version, **body.model_dump(mode="json")},
            TradeAuditOriginRecord,
            recover,
            reauth=True,
            validate_replay=validate_replay,
        )
    except (ValueError, PydanticSerializationError):
        raise _origin_unavailable() from None


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
