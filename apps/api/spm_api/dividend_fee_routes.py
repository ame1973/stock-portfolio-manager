"""Owner-scoped fee estimates; no confirmed charge or economic projection."""

import re
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text

import spm_api.dividend_event_service as events
import spm_api.dividend_fee_models as models
import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as writer
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import audit, canonical, digest
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.errors import ApiError, Error
from spm_api.master_service import owned_account, owned_broker
from spm_api.mutations import check_version, mutate
from spm_api.pagination import Page, PageDep, paginate
from spm_api.security import Auth, AuthDep, DividendEventVersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings",
    tags=["Dividend fee estimates"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)

HistoryVersion = Annotated[int, Query(ge=1, le=2_147_483_647)]


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIVIDEND_FEE_HISTORY_UNAVAILABLE", "The fee history cannot be read safely"
    )


def _failure(
    error: events.EventError | fees.FeeSettingError | rejections.RejectionError,
) -> ApiError:
    code = str(error)
    if code in {"DIVIDEND_FEE_NOT_FOUND", "DIVIDEND_EVENT_NOT_FOUND"}:
        return ApiError(404, "NOT_FOUND", "Dividend fee scope not found")
    if code == "DIVIDEND_FEE_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code == "INVALID_DIVIDEND_FEE_SCOPE":
        return ApiError(422, code, "The fee setting is invalid")
    if code in {"DIVIDEND_FEE_VERSION_CONFLICT", "DIVIDEND_EVENT_VERSION_CONFLICT"}:
        return ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
    if code in {
        "DIVIDEND_FEE_CONFLICT",
        "DIVIDEND_FEE_SCOPE_CHANGED",
        "DIVIDEND_FEE_SETTING_CHANGED",
        "DIVIDEND_FEE_TIME_CONFLICT",
        "DIVIDEND_REVISION_REJECTED",
        "DIVIDEND_REJECTION_VERSION_CONFLICT",
        "DIVIDEND_EVENT_SOURCE_CHANGED",
        "CANDIDATE_NOT_FOUND",
        "CANDIDATE_VERSION_CONFLICT",
        "CANDIDATE_DISMISSED",
        "CANDIDATE_SOURCE_UNAVAILABLE",
        "CANDIDATE_SOURCE_CHANGED",
        "CANDIDATE_BASIS_UNAVAILABLE",
        "CANDIDATE_BASIS_CHANGED",
    }:
        return ApiError(409, code, "The fee setting no longer matches its observed scope")
    return _unavailable()


def _event(con: Connection, owner: UUID, event_id: UUID) -> events.ReviewedEvent:
    result = events.get(con, owner, event_id)
    if result is None:
        raise fees.FeeSettingError("DIVIDEND_FEE_NOT_FOUND")
    return result


def _history(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    revision_id: UUID,
    revision_no: int,
    account_id: UUID,
) -> tuple[fees.FeeSetting, ...]:
    result = fees.history(con, owner, event_id, revision_id, revision_no, account_id)
    if result is None:
        raise fees.FeeSettingError("DIVIDEND_FEE_NOT_FOUND")
    return result


def _public(record: fees.FeeSetting) -> models.FeeSettingRecord:
    return models.FeeSettingRecord(
        id=record.id,
        event_id=record.event_id,
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        account_id=record.account_id,
        portfolio_id=record.portfolio_id,
        broker_id=record.broker_id,
        account_version=str(record.account_version),
        broker_version=str(record.broker_version),
        setting_no=str(record.setting_no),
        previous_id=record.previous_id,
        effective_from=record.effective_from,
        fee_status=record.fee_status,
        fee=record.fee,
        fee_currency=record.fee_currency,
        reason=record.reason,
        created_at=record.created_at,
        scope="OWNER_ESTIMATE_ONLY",
        current_use_scope="REVALIDATION_REQUIRED",
    )


def _binding(
    request: Request, headers: WriteDep, payload: dict[str, Any], record: fees.FeeSetting
) -> dict[str, object]:
    return {
        "event_id": str(record.event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "account_id": str(record.account_id),
        "setting_id": str(record.id),
        "setting_no": record.setting_no,
        "route": f"{request.method} {request.url.path}",
        "key_sha256": digest(headers.key),
        "intent_sha256": digest(canonical(payload)),
    }


def _check_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: fees.FeeSetting,
) -> None:
    rows = con.execute(
        text("""
        SELECT owner_id,action,request_id,reason,before_sha256,after_sha256,created_at
        FROM audit_events WHERE entity_type='dividend_fee_http_intent' AND entity_id=:id
    """),
        {"id": str(record.id)},
    ).all()
    fee_audit_time = con.execute(
        text("SELECT created_at FROM audit_events WHERE id=:id"), {"id": str(record.audit_id)}
    ).scalar_one()
    expected = (
        str(auth.owner.id),
        "DIVIDEND_FEE_HTTP_INTENT",
        str(record.request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, record))),
    )
    if (
        len(rows) != 1
        or tuple(rows[0][:6]) != expected
        or events._time(rows[0].created_at) < record.created_at
        or events._time(rows[0].created_at) < events._time(fee_audit_time)
    ):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")


@router.get("", operation_id="get_dividend_fee_estimate")
def current_estimate(
    event_id: UUID,
    account_id: UUID,
    request: Request,
    auth: AuthDep,
    effective_on: IsoDate,
) -> models.FeeSettingSelection:
    try:
        with unit_of_work(request.app.state.engine) as con:
            event = _event(con, auth.owner.id, event_id).record
            settings = _history(
                con, auth.owner.id, event_id, event.revision_id, event.revision_no, account_id
            )
            selected = fees.select(settings, date.fromisoformat(effective_on))
            rejected = rejections.history(con, auth.owner.id, event_id)
            if rejected is None:
                raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
            account = owned_account(con, str(auth.owner.id), str(account_id))
            broker = owned_broker(con, str(auth.owner.id), account["broker_id"])
            matched = selected.selected is not None and (
                str(selected.selected.portfolio_id) == account["portfolio_id"]
                and str(selected.selected.broker_id) == account["broker_id"]
                and selected.selected.account_version == account["version"]
                and selected.selected.broker_version == broker["version"]
            )
            scope: Literal["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]
            if selected.selected is None:
                scope = "NO_SELECTED_SETTING"
            else:
                scope = "ACCOUNT_SCOPE_MATCH" if matched else "ACCOUNT_SCOPE_CHANGED"
            return models.FeeSettingSelection(
                event_id=event_id,
                revision_id=event.revision_id,
                revision_no=event.revision_no,
                account_id=account_id,
                portfolio_id=account["portfolio_id"],
                broker_id=account["broker_id"],
                account_version=str(account["version"]),
                broker_version=str(broker["version"]),
                effective_on=effective_on,
                head_no=str(selected.head_no),
                head_id=selected.head_id,
                fee_status=selected.status,
                selected=_public(selected.selected) if selected.selected is not None else None,
                selected_scope=scope,
                revision_rejection=(
                    "REJECTED"
                    if any(item.revision_id == event.revision_id for item in rejected)
                    else "NOT_REJECTED"
                ),
                scope="OWNER_ESTIMATE_ONLY",
                current_use_scope="REVALIDATION_REQUIRED",
            )
    except (events.EventError, fees.FeeSettingError, rejections.RejectionError) as error:
        raise _failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.get("/history", operation_id="list_dividend_fee_estimate_history")
def estimate_history(
    event_id: UUID,
    account_id: UUID,
    request: Request,
    auth: AuthDep,
    page: PageDep,
    revision_id: UUID,
    revision_no: HistoryVersion,
) -> Page[models.FeeSettingRecord]:
    cursor = page.cursor_value
    if cursor is not None and (
        re.fullmatch(r"[1-9][0-9]{0,18}", cursor) is None or int(cursor) > INT64_MAX
    ):
        raise ApiError(422, "INVALID_CURSOR", "The fee history cursor is invalid")
    try:
        with unit_of_work(request.app.state.engine) as con:
            settings = _history(con, auth.owner.id, event_id, revision_id, revision_no, account_id)
            records = {str(record.id): record for record in settings}
            rows = (
                con.execute(
                    text(r"""
                    SELECT id,setting_no FROM dividend_fee_settings
                    WHERE owner_id=:owner AND event_id=:event AND revision_id=:revision
                      AND account_id=:account
                      AND (id LIKE :search ESCAPE '\'
                        OR CAST(setting_no AS TEXT) LIKE :search ESCAPE '\')
                      AND (:cursor_value IS NULL OR (setting_no,id)<(:cursor_value,:cursor_id))
                    ORDER BY setting_no DESC,id DESC LIMIT :limit
                """),
                    {
                        **page.params(),
                        "owner": str(auth.owner.id),
                        "event": str(event_id),
                        "revision": str(revision_id),
                        "account": str(account_id),
                        "cursor_value": int(cursor) if cursor is not None else None,
                    },
                )
                .mappings()
                .all()
            )
            return paginate(rows, page, "setting_no", lambda row: _public(records[row["id"]]))
    except (events.EventError, fees.FeeSettingError, rejections.RejectionError) as error:
        raise _failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.post("", status_code=201, operation_id="save_dividend_fee_estimate")
def save_estimate(
    event_id: UUID,
    account_id: UUID,
    body: models.FeeSettingWrite,
    request: Request,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.FeeSettingRecord:
    expected = int(body.expected_head_no)
    payload = {"event_version": version, **body.model_dump(mode="json")}
    pending: (
        tuple[
            writer.FeeIntent,
            fees.FeeSetting,
            tuple[int, int],
            tuple[str | None, ...],
            str,
        ]
        | None
    ) = None

    def action(con: Connection, auth: Auth) -> models.FeeSettingRecord:
        nonlocal pending
        event = _event(con, auth.owner.id, event_id).record
        check_version(event.revision_no, version)
        if event.revision_id != body.revision_id:
            raise ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
        settings = _history(con, auth.owner.id, event_id, body.revision_id, version, account_id)
        check_version(len(settings), expected)
        if (settings[-1].id if settings else None) != body.expected_head_id:
            raise ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
        intent = writer.FeeIntent(
            id=uuid4(),
            event_id=event_id,
            revision_id=body.revision_id,
            revision_no=version,
            account_id=account_id,
            portfolio_id=body.portfolio_id,
            broker_id=body.broker_id,
            account_version=int(body.account_version),
            broker_version=int(body.broker_version),
            expected_setting_no=expected,
            expected_head_id=body.expected_head_id,
            effective_from=body.effective_from,
            fee_status=body.fee_status,
            fee=body.fee,
            fee_currency=body.fee_currency,
            reason=body.reason,
        )
        record = writer.save(con, auth.owner.id, intent, request_id=UUID(request.state.request_id))
        if event.terms.ex_date is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        from_date = min(intent.effective_from, event.terms.ex_date)
        portfolio = writer._portfolio_scope(con, auth.owner.id, intent.portfolio_id)
        marker = writer._invalidation(con, intent, from_date)
        audit(
            con,
            str(auth.owner.id),
            "dividend_fee_http_intent",
            str(record.id),
            "DIVIDEND_FEE_HTTP_INTENT",
            request_id=str(record.request_id),
            after=_binding(request, headers, payload, record),
        )
        _check_intent(con, auth, request, headers, payload, record)
        pending = (intent, record, portfolio, marker, from_date)
        return _public(record)

    def commit(con: Connection, auth: Auth, result: models.FeeSettingRecord) -> None:
        if pending is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        intent, saved, portfolio, marker, from_date = pending
        checked = writer._require_saved(
            con, auth.owner.id, intent, UUID(request.state.request_id), saved
        )
        _check_intent(con, auth, request, headers, payload, checked)
        if _public(checked) != result:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        if writer._portfolio_scope(con, auth.owner.id, intent.portfolio_id) != portfolio:
            raise fees.FeeSettingError("DIVIDEND_FEE_SCOPE_CHANGED")
        if writer._invalidation(con, intent, from_date) != marker:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")

    def replay(con: Connection, auth: Auth, cached: models.FeeSettingRecord) -> None:
        settings = fees.history(con, auth.owner.id, event_id, body.revision_id, version, account_id)
        if settings is None or len(settings) <= expected:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        original = settings[expected]
        if (
            _public(original) != cached
            or original.event_id != event_id
            or original.account_id != account_id
            or original.revision_id != body.revision_id
            or original.revision_no != version
            or original.setting_no != expected + 1
            or original.previous_id != body.expected_head_id
            or original.portfolio_id != body.portfolio_id
            or original.broker_id != body.broker_id
            or original.account_version != int(body.account_version)
            or original.broker_version != int(body.broker_version)
            or original.effective_from != body.effective_from
            or original.fee_status != body.fee_status
            or original.fee != body.fee
            or original.fee_currency != body.fee_currency
            or original.reason != body.reason
        ):
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, original)

    try:
        return mutate(
            request,
            headers,
            payload,
            models.FeeSettingRecord,
            action,
            status=201,
            reauth=True,
            validate_replay=replay,
            validate_commit=commit,
        )
    except (events.EventError, fees.FeeSettingError, rejections.RejectionError) as error:
        raise _failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None
