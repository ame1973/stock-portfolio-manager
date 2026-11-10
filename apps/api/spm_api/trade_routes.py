"""Private trade endpoints; all financial mutations share one short guarded UoW."""

import re
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text

from spm_api import trade_audit, trade_history_review, trade_unverified_correction
from spm_api import trade_service as service
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.errors import ApiError, Error, Success
from spm_api.finance import FinanceError
from spm_api.master_service import owned_account, owned_portfolio, security_row
from spm_api.mutations import check_version, mutate
from spm_api.pagination import Page, PageDep, paginate, validate_date_page
from spm_api.security import Auth, AuthDep, VersionDep, WriteDep, matching_version
from spm_api.trade_models import (
    TradeAuditOriginRecord,
    TradeAuditRecovery,
    TradeCreate,
    TradeHistoryReviewRecord,
    TradeHistoryReviewStatus,
    TradeHistoryReviewWrite,
    TradeRecord,
    TradeRevision,
    TradeUnverifiedCorrectionRecord,
    TradeUnverifiedRevisionWrite,
    TradeUnverifiedVoidWrite,
    VoidTrade,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Trades"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _physical_trade_version(
    value: Annotated[str, Header(alias="If-Match", pattern=r'^"[1-9][0-9]{0,18}"$')],
) -> int:
    version = matching_version(value)
    if version > INT64_MAX:
        raise ApiError(422, "INVALID_TRADE_VERSION", "The trade version is invalid")
    return version


PhysicalTradeVersion = Annotated[int, Depends(_physical_trade_version)]


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


def _review_unavailable() -> ApiError:
    return ApiError(503, "TRADE_REVIEW_UNAVAILABLE", "The trade review cannot be read safely")


def _review_record(record: trade_history_review.Review) -> TradeHistoryReviewRecord:
    return TradeHistoryReviewRecord(
        id=record.id,
        trade_id=record.trade_id,
        revision=str(record.revision),
        decision=record.decision,
        gap=record.gap,
        observed_trade_status=record.observed_status,
        observed_trade_version=record.observed_trade_version,
        created_at=record.created_at,
        reason=record.reason,
    )


def _owned_reviews(
    con: Connection, owner: UUID, identifier: UUID
) -> tuple[RowMapping, tuple[trade_history_review.Review, ...]]:
    row = service.owned_trade(con, str(owner), str(identifier))
    try:
        reviews = trade_history_review.history(con, owner, identifier)
        if reviews is None:
            raise ValueError("Trade review disappeared")
        return row, reviews
    except (ValueError, TypeError, KeyError, OverflowError):
        raise _review_unavailable() from None


def _current_review_gap(
    con: Connection, owner: UUID, trade: RowMapping
) -> trade_history_review.Gap | None:
    try:
        return trade_history_review._gap(con, owner, trade)
    except trade_history_review.TradeReviewError as error:
        if str(error) == "TRADE_REVIEW_NOT_REQUIRED":
            return None
        raise


def _review_failure(error: trade_history_review.TradeReviewError) -> ApiError:
    code = str(error)
    if code == "TRADE_REVIEW_NOT_FOUND":
        return ApiError(404, "NOT_FOUND", "Trade not found")
    if code == "TRADE_REVIEW_VERSION_CONFLICT":
        return ApiError(412, "PRECONDITION_FAILED", "This review changed; reload before editing")
    if code == "INVALID_TRADE_REVIEW_SCOPE":
        return ApiError(422, code, "The trade review request is invalid")
    if code in {
        "TRADE_REVIEW_CONFLICT",
        "TRADE_REVIEW_CHANGED",
        "TRADE_REVIEW_DECISION_CONFLICT",
        "TRADE_REVIEW_NOT_REQUIRED",
        "TRADE_REVIEW_REQUIRED",
        "TRADE_REVIEW_SCOPE_CHANGED",
        "TRADE_REVIEW_TIME_CONFLICT",
    }:
        return ApiError(409, code, "The trade review decision could not be accepted")
    return _review_unavailable()


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
    version: PhysicalTradeVersion,
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


@router.get("/trades/{id}/history-review", operation_id="get_trade_history_review")
def trade_history_review_status(
    id: UUID, request: Request, auth: AuthDep
) -> TradeHistoryReviewStatus:
    with unit_of_work(request.app.state.engine) as con:
        row, reviews = _owned_reviews(con, auth.owner.id, id)
        try:
            gap = _current_review_gap(con, auth.owner.id, row)
            latest = reviews[-1] if reviews else None
            state: Literal[
                "NOT_REQUIRED",
                "NOT_REVIEWED",
                "ACKNOWLEDGED_UNVERIFIED",
                "WITHDRAWN",
                "EVIDENCE_CHANGED",
            ]
            if latest is None:
                state = "NOT_REQUIRED" if gap is None else "NOT_REVIEWED"
            elif latest.decision == "WITHDRAW":
                state = "WITHDRAWN"
            else:
                state = "ACKNOWLEDGED_UNVERIFIED" if gap == latest.gap else "EVIDENCE_CHANGED"
            result = TradeHistoryReviewStatus(
                trade_id=id,
                trade_version=row["version"],
                trade_status=row["status"],
                review_revision=str(len(reviews)),
                current_gap=gap,
                state=state,
                latest=_review_record(latest) if latest else None,
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise _review_unavailable() from None
    return result


@router.get("/trades/{id}/history-review/revisions", operation_id="list_trade_history_reviews")
def trade_history_review_revisions(
    id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[TradeHistoryReviewRecord]:
    value = page.cursor_value
    if value is not None and (
        re.fullmatch(r"[1-9][0-9]{0,18}", value) is None or int(value) > INT64_MAX
    ):
        raise ApiError(422, "INVALID_CURSOR", "Invalid trade review pagination cursor")
    with unit_of_work(request.app.state.engine) as con:
        _, reviews = _owned_reviews(con, auth.owner.id, id)
        params = {
            **page.params(),
            "cursor_value": int(value) if value is not None else None,
            "trade": str(id),
            "owner": str(auth.owner.id),
        }
        rows = (
            con.execute(
                text(r"""
            SELECT id,revision FROM trade_history_reviews
            WHERE trade_id=:trade AND owner_id=:owner AND id LIKE :search ESCAPE '\'
              AND (:cursor_value IS NULL OR (revision,id)<(:cursor_value,:cursor_id))
            ORDER BY revision DESC,id DESC LIMIT :limit
        """),
                params,
            )
            .mappings()
            .all()
        )
        records = {str(record.id): record for record in reviews}
        try:
            return paginate(rows, page, "revision", lambda row: _review_record(records[row["id"]]))
        except (ValueError, TypeError, KeyError, OverflowError):
            raise _review_unavailable() from None


@router.post("/trades/{id}/history-review", operation_id="save_trade_history_review")
def save_trade_history_review(
    id: UUID,
    body: TradeHistoryReviewWrite,
    request: Request,
    headers: WriteDep,
    version: PhysicalTradeVersion,
) -> TradeHistoryReviewRecord:
    expected = int(body.expected_revision)

    def save(con: Connection, auth: Auth) -> TradeHistoryReviewRecord:
        row, reviews = _owned_reviews(con, auth.owner.id, id)
        check_version(row["version"], version)
        check_version(len(reviews), expected)
        if body.decision == "ACKNOWLEDGE_UNVERIFIED":
            current_gap = _current_review_gap(con, auth.owner.id, row)
            if current_gap is None:
                raise trade_history_review.TradeReviewError("TRADE_REVIEW_NOT_REQUIRED")
            if current_gap != body.expected_gap:
                raise trade_history_review.TradeReviewError("TRADE_REVIEW_CHANGED")
        elif reviews and reviews[-1].gap != body.expected_gap:
            raise trade_history_review.TradeReviewError("TRADE_REVIEW_CHANGED")
        return _review_record(
            trade_history_review.save(
                con,
                auth.owner.id,
                id,
                review_id=uuid4(),
                request_id=UUID(request.state.request_id),
                expected_trade_version=version,
                expected_revision=expected,
                decision=body.decision,
                reason=body.reason,
            )
        )

    def validate_replay(con: Connection, auth: Auth, cached: TradeHistoryReviewRecord) -> None:
        if (
            cached.trade_id != id
            or cached.revision != str(expected + 1)
            or cached.observed_trade_version != version
        ):
            raise _review_unavailable()
        _, reviews = _owned_reviews(con, auth.owner.id, id)
        if len(reviews) <= expected:
            raise _review_unavailable()
        original = reviews[expected]
        if (
            _review_record(original) != cached
            or original.decision != body.decision
            or original.gap != body.expected_gap
            or original.reason != body.reason
            or original.observed_trade_version != version
        ):
            raise _review_unavailable()

    try:
        result = mutate(
            request,
            headers,
            {"version": version, **body.model_dump(mode="json")},
            TradeHistoryReviewRecord,
            save,
            reauth=True,
            validate_replay=validate_replay,
        )
    except trade_history_review.TradeReviewError as error:
        raise _review_failure(error) from None
    except (ValueError, PydanticSerializationError):
        raise _review_unavailable() from None
    return result


def _correction_unavailable() -> ApiError:
    return ApiError(
        503, "TRADE_CORRECTION_UNAVAILABLE", "The trade correction cannot be read safely"
    )


def _correction_record(
    result: trade_unverified_correction.CorrectionResult,
) -> TradeUnverifiedCorrectionRecord:
    return TradeUnverifiedCorrectionRecord(
        authorization_id=result.authorization.id,
        trade=result.trade,
        scope="UNVERIFIED_TRADE_CORRECTION_ONLY",
        history_completeness="UNVERIFIED",
    )


def _unverified_correction(
    id: UUID,
    body: TradeUnverifiedRevisionWrite | TradeUnverifiedVoidWrite,
    request: Request,
    headers: WriteDep,
    version: int,
) -> TradeUnverifiedCorrectionRecord:
    pins = tuple(
        trade_history_review.CorrectionReviewPin(
            item.trade_id, item.trade_version, int(item.review_revision)
        )
        for item in body.reviews
    )
    operation: Literal["REVISE", "VOID"] = (
        "REVISE" if isinstance(body, TradeUnverifiedRevisionWrite) else "VOID"
    )
    change = body.revision if isinstance(body, TradeUnverifiedRevisionWrite) else body.reason

    def write(con: Connection, auth: Auth) -> TradeUnverifiedCorrectionRecord:
        if isinstance(change, TradeRevision):
            result = trade_unverified_correction.revise(
                con,
                auth,
                id,
                version=version,
                body=change,
                pins=pins,
                request_id=UUID(request.state.request_id),
            )
        else:
            result = trade_unverified_correction.void(
                con,
                auth,
                id,
                version=version,
                reason=change,
                pins=pins,
                request_id=UUID(request.state.request_id),
            )
        return _correction_record(result)

    def validate_replay(
        con: Connection, auth: Auth, cached: TradeUnverifiedCorrectionRecord
    ) -> None:
        try:
            result = trade_unverified_correction.replay(
                con,
                auth,
                cached.authorization_id,
                id,
                version=version,
                operation=operation,
                change=change,
                pins=pins,
            )
            if _correction_record(result) != cached:
                raise _correction_unavailable()
        except (ValueError, ApiError, PydanticSerializationError):
            raise _correction_unavailable() from None

    payload = body.model_dump(mode="json")
    payload["reviews"].sort(key=lambda item: item["trade_id"])
    try:
        return mutate(
            request,
            headers,
            {"version": version, **payload},
            TradeUnverifiedCorrectionRecord,
            write,
            reauth=True,
            validate_replay=validate_replay,
        )
    except FinanceError:
        raise
    except trade_history_review.TradeReviewError as error:
        raise _review_failure(error) from None
    except trade_unverified_correction.UnverifiedCorrectionError as error:
        code = str(error)
        if code == "INVALID_TRADE_CORRECTION_SCOPE":
            raise ApiError(422, code, "The trade correction request is invalid") from None
        if code == "TRADE_CORRECTION_CONFLICT":
            raise ApiError(
                409, code, "The trade correction conflicts with an earlier request"
            ) from None
        raise _correction_unavailable() from None
    except (ValueError, PydanticSerializationError):
        raise _correction_unavailable() from None


@router.post("/trades/{id}/unverified-corrections/revise", operation_id="revise_unverified_trade")
def revise_unverified_trade(
    id: UUID,
    body: TradeUnverifiedRevisionWrite,
    request: Request,
    headers: WriteDep,
    version: PhysicalTradeVersion,
) -> TradeUnverifiedCorrectionRecord:
    return _unverified_correction(id, body, request, headers, version)


@router.post("/trades/{id}/unverified-corrections/void", operation_id="void_unverified_trade")
def void_unverified_trade(
    id: UUID,
    body: TradeUnverifiedVoidWrite,
    request: Request,
    headers: WriteDep,
    version: PhysicalTradeVersion,
) -> TradeUnverifiedCorrectionRecord:
    return _unverified_correction(id, body, request, headers, version)


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
