"""Private recorded projections without current-use or receipt authority."""

import re
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_projection_current as current
import spm_api.dividend_projection_history as history
import spm_api.dividend_projection_models as models
import spm_api.dividend_projection_write_models as write_models
import spm_api.dividend_projection_writer as writer
from spm_api.audit import audit, canonical, digest
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.errors import ApiError, Error
from spm_api.mutations import mutate
from spm_api.pagination import Page, PageDep, paginate
from spm_api.security import Auth, AuthDep, DividendEventVersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections",
    tags=["Dividend projection history"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503,
        "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE",
        "The projection history cannot be read safely",
    )


def _failure(error: writer.ProjectionWriteError) -> ApiError:
    code = str(error)
    if code in {"DIVIDEND_PROJECTION_NOT_FOUND", "DIVIDEND_EVENT_NOT_FOUND"}:
        return ApiError(404, "NOT_FOUND", "Dividend projection scope not found")
    if code == "DIVIDEND_PROJECTION_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code in {"INVALID_DIVIDEND_PROJECTION_SCOPE", "INVALID_DIVIDEND_PROJECTION_FX"}:
        return ApiError(422, code, "The projection inputs are invalid")
    if code in {"DIVIDEND_PROJECTION_VERSION_CONFLICT", "DIVIDEND_EVENT_VERSION_CONFLICT"}:
        return ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
    if code in {
        "DIVIDEND_PROJECTION_CONFLICT",
        "DIVIDEND_PROJECTION_SCOPE_CHANGED",
        "DIVIDEND_PROJECTION_INPUT_CHANGED",
        "DIVIDEND_PROJECTION_TIME_CONFLICT",
        "DIVIDEND_REVISION_REJECTED",
        "DIVIDEND_REJECTION_VERSION_CONFLICT",
        "DIVIDEND_EVENT_SOURCE_CHANGED",
        "DIVIDEND_TAX_SETTING_CHANGED",
        "DIVIDEND_TAX_SCOPE_CHANGED",
        "DIVIDEND_FEE_SETTING_CHANGED",
        "DIVIDEND_FEE_SCOPE_CHANGED",
        "CANDIDATE_NOT_FOUND",
        "CANDIDATE_VERSION_CONFLICT",
        "CANDIDATE_DISMISSED",
        "CANDIDATE_SOURCE_UNAVAILABLE",
        "CANDIDATE_SOURCE_CHANGED",
        "CANDIDATE_BASIS_UNAVAILABLE",
        "CANDIDATE_BASIS_CHANGED",
    }:
        return ApiError(409, code, "The projection no longer matches its observed inputs")
    return _unavailable()


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _public(
    con: Connection, owner: UUID, record: history.RetainedProjection
) -> models.ProjectionRecord:
    version = record.version
    event = events.get(con, owner, version.event_id, version=version.revision_no)
    if event is None or event.record.revision_id != version.revision_id:
        raise ValueError("The retained event revision is unavailable")
    ex_date, currency = event.record.terms.ex_date, event.record.terms.currency
    if ex_date is None or currency is None:
        raise ValueError("The retained event terms are incomplete")
    accrual = version.accrual
    return models.ProjectionRecord.model_validate(
        {
            "id": version.id,
            "event_id": version.event_id,
            "revision_id": version.revision_id,
            "revision_no": version.revision_no,
            "account_id": version.account_id,
            "portfolio_id": record.portfolio_id,
            "broker_id": record.broker_id,
            "account_version": str(record.account_version),
            "broker_version": str(record.broker_version),
            "portfolio_data_revision": str(record.portfolio_data_revision),
            "projection_no": str(version.projection_no),
            "previous_id": version.previous_id,
            "state": version.state,
            "as_of": record.as_of.isoformat(),
            "ex_date": ex_date,
            "timing_scope": (
                "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY"
                if record.as_of.isoformat() < ex_date
                else "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY"
            ),
            "currency": currency,
            "eligibility_rule": record.eligibility_rule,
            "retired_trade_scope": record.retired_trade_scope,
            "creation_audit_scope": record.creation_audit_scope,
            "recorded_shares": _decimal(record.eligible_shares),
            "estimated_amounts": None
            if accrual is None
            else {
                "currency": accrual.currency,
                "gross_local": _decimal(accrual.gross_local),
                "tax_local": _decimal(accrual.tax_local),
                "fee_local": _decimal(accrual.fee_local),
                "net_local": _decimal(accrual.net_local),
                "rounding_delta_local": _decimal(accrual.rounding_delta_local),
                "estimated_hkd": _decimal(accrual.booked_hkd),
                "quality": accrual.quality,
            },
            "tax_rate": _decimal(record.tax_rate),
            "fee_original": _decimal(record.fee_original),
            "fee_currency": record.fee_currency,
            "fee_rate_event": _decimal(record.fee_rate_event),
            "fee_rate_source": record.fee_rate_source,
            "fx_rate_hkd": _decimal(record.fx_rate_hkd),
            "fx_source": record.fx_source,
            "tax_provenance": _provenance(record.tax_provenance),
            "fee_provenance": _provenance(record.fee_provenance),
            "created_at": record.created_at,
            "reason": record.reason,
            "history_completeness": record.history_completeness,
            "scope": record.scope,
            "current_use_scope": "REVALIDATION_REQUIRED",
            "eligibility_scope": "NOT_ESTABLISHED",
        }
    )


def _provenance(
    value: history.RetainedTaxProvenance | history.RetainedFeeProvenance | None,
) -> models.ProjectionProvenance | None:
    if value is None:
        return None
    return models.ProjectionProvenance(
        effective_on=value.effective_on.isoformat(),
        selected_setting_id=value.selected_setting_id,
        observed_head_no=str(value.observed_head_no),
        observed_head_id=value.observed_head_id,
        status=value.status,
        scope=value.scope,
    )


def _intent(
    event_id: UUID,
    account_id: UUID,
    revision_no: int,
    identifier: UUID,
    body: write_models.ProjectionWrite,
) -> writer.ProjectionIntent:
    values = body.model_dump(exclude={"confirmation", "confirm_revision_fence"})
    for name in (
        "account_version",
        "broker_version",
        "expected_data_revision",
        "expected_projection_no",
        "expected_tax_head_no",
        "expected_fee_head_no",
    ):
        values[name] = int(values[name])
    return writer.ProjectionIntent.model_validate(
        {
            **values,
            "id": identifier,
            "event_id": event_id,
            "account_id": account_id,
            "revision_no": revision_no,
        }
    )


def _binding(
    request: Request, headers: WriteDep, payload: dict[str, Any], record: history.RetainedProjection
) -> dict[str, object]:
    version = record.version
    return {
        "event_id": str(version.event_id),
        "revision_id": str(version.revision_id),
        "revision_no": version.revision_no,
        "account_id": str(version.account_id),
        "projection_id": str(version.id),
        "projection_no": version.projection_no,
        "route": f"{request.method} {request.url.path}",
        "key_sha256": digest(headers.key),
        "intent_sha256": digest(canonical(payload)),
        "scope": "RECORDED_PROJECTION_HTTP_INTENT_ONLY",
    }


def _check_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: history.RetainedProjection,
) -> None:
    rows = con.execute(
        text("""
        SELECT owner_id,action,request_id,reason,before_sha256,after_sha256,created_at
        FROM audit_events WHERE entity_type='dividend_projection_http_intent' AND entity_id=:id
    """),
        {"id": str(record.version.id)},
    ).all()
    expected = (
        str(auth.owner.id),
        "DIVIDEND_PROJECTION_HTTP_INTENT",
        str(record.request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, record))),
    )
    if (
        len(rows) != 1
        or tuple(rows[0][:6]) != expected
        or events._time(rows[0].created_at) < record.created_at
    ):
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")


@router.get("", operation_id="list_dividend_projection_history")
def projection_history(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[models.ProjectionRecord]:
    cursor = page.cursor_value
    if cursor is not None and (
        re.fullmatch(r"[1-9][0-9]{0,18}", cursor) is None or int(cursor) > INT64_MAX
    ):
        raise ApiError(422, "INVALID_CURSOR", "The projection history cursor is invalid")
    try:
        with unit_of_work(request.app.state.engine) as con:
            retained = history.history(con, auth.owner.id, event_id, account_id)
            if retained is None:
                raise ApiError(404, "NOT_FOUND", "Dividend projection scope not found")
            records = {str(record.version.id): record for record in retained}
            rows = (
                con.execute(
                    text(r"""
                    SELECT id,projection_no FROM dividend_projection_versions
                    WHERE owner_id=:owner AND event_id=:event AND account_id=:account
                      AND (id LIKE :search ESCAPE '\'
                        OR CAST(projection_no AS TEXT) LIKE :search ESCAPE '\')
                      AND (:cursor_value IS NULL OR (projection_no,id)<(:cursor_value,:cursor_id))
                    ORDER BY projection_no DESC,id DESC LIMIT :limit
                """),
                    {
                        **page.params(),
                        "owner": str(auth.owner.id),
                        "event": str(event_id),
                        "account": str(account_id),
                        "cursor_value": int(cursor) if cursor is not None else None,
                    },
                )
                .mappings()
                .all()
            )
            return paginate(
                rows,
                page,
                "projection_no",
                lambda row: _public(con, auth.owner.id, records[row["id"]]),
            )
    except (history.ProjectionHistoryError, events.EventError):
        raise _unavailable() from None
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None


@router.post(
    "",
    status_code=201,
    operation_id="create_recorded_dividend_projection",
    responses={409: {"model": Error}, 412: {"model": Error}},
)
def create_projection(
    event_id: UUID,
    account_id: UUID,
    body: write_models.ProjectionWrite,
    request: Request,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.ProjectionRecord:
    payload = {"event_version": version, **body.model_dump(mode="json")}
    expected = int(body.expected_projection_no)

    def action(con: Connection, auth: Auth) -> models.ProjectionRecord:
        intent = _intent(event_id, account_id, version, uuid4(), body)
        record = writer.save(con, auth.owner.id, intent, request_id=UUID(request.state.request_id))
        audit(
            con,
            str(auth.owner.id),
            "dividend_projection_http_intent",
            str(record.version.id),
            "DIVIDEND_PROJECTION_HTTP_INTENT",
            request_id=str(record.request_id),
            after=_binding(request, headers, payload, record),
        )
        _check_intent(con, auth, request, headers, payload, record)
        return _public(con, auth.owner.id, record)

    def replay(con: Connection, auth: Auth, cached: models.ProjectionRecord) -> None:
        retained = history.history(con, auth.owner.id, event_id, account_id)
        if retained is None or len(retained) <= expected:
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        original = retained[expected]
        if original.version.id != cached.id:
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        intent = _intent(event_id, account_id, version, original.version.id, body)
        try:
            verified = writer.save(con, auth.owner.id, intent, request_id=original.request_id)
        except writer.ProjectionWriteError:
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None
        if _public(con, auth.owner.id, verified) != cached:
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, verified)

    def commit(con: Connection, auth: Auth, result: models.ProjectionRecord) -> None:
        record = current.require_current(
            con,
            auth.owner.id,
            event_id,
            account_id,
            projection_id=result.id,
            projection_no=int(result.projection_no),
            as_of=date.fromisoformat(body.as_of),
        )
        request_id = UUID(request.state.request_id)
        if record.request_id != request_id:
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        intent = _intent(event_id, account_id, version, record.version.id, body)
        writer._verify_intent(con, auth.owner.id, intent, request_id, record)
        _check_intent(con, auth, request, headers, payload, record)
        if _public(con, auth.owner.id, record) != result:
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")

    try:
        return mutate(
            request,
            headers,
            payload,
            models.ProjectionRecord,
            action,
            status=201,
            reauth=True,
            validate_replay=replay,
            validate_commit=commit,
        )
    except writer.ProjectionWriteError as error:
        raise _failure(error) from None
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None
