"""Protect explicit recorded receipt creation without granting eligibility or payment proof."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as history
import spm_api.dividend_receipt_write_models as models
import spm_api.dividend_receipt_writer as writer
from spm_api.audit import audit, canonical, digest
from spm_api.dividend_receipt_terms import ReceiptComponents
from spm_api.errors import ApiError, Error
from spm_api.mutations import mutate
from spm_api.security import Auth, DividendEventVersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/receipts",
    tags=["Recorded dividend receipt writes"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE", "The receipt cannot be read safely"
    )


def _failure(error: writer.ReceiptWriteError) -> ApiError:
    code = str(error)
    if code == "DIVIDEND_RECEIPT_NOT_FOUND":
        return ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
    if code == "DIVIDEND_RECEIPT_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code in {
        "INVALID_DIVIDEND_RECEIPT_SCOPE",
        "INVALID_DIVIDEND_RECEIPT_TERMS",
        "DIVIDEND_RECEIPT_DATE_INVALID",
    }:
        return ApiError(422, code, "The receipt inputs are invalid")
    if code in {
        "DIVIDEND_RECEIPT_SCOPE_CHANGED",
        "DIVIDEND_RECEIPT_ACCOUNT_ARCHIVED",
        "DIVIDEND_RECEIPT_CONFLICT",
    }:
        return ApiError(409, code, "The receipt no longer matches its observed scope")
    return _unavailable()


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _intent(body: models.ReceiptCreate, version: int) -> writer.ReceiptIntent:
    components = {name: getattr(body, name) for name in ReceiptComponents.model_fields}
    return writer.ReceiptIntent.model_validate(
        {
            "id": body.receipt_id,
            "event_id": body.event_id,
            "revision_id": body.revision_id,
            "revision_no": version,
            "entitlement_id": body.entitlement_id,
            "account_id": body.account_id,
            "portfolio_id": body.portfolio_id,
            "broker_id": body.broker_id,
            "account_version": int(body.account_version),
            "portfolio_version": int(body.portfolio_version),
            "broker_version": int(body.broker_version),
            "expected_data_revision": int(body.expected_data_revision),
            "received_date": body.received_date,
            "components": components,
            "note": body.note,
            "reason": body.reason,
            "confirmation": body.confirmation,
        }
    )


def _public(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID, record: history.AuditedReceipt
) -> models.ReceiptCreated:
    origin = record.origin
    if origin is None or record.audit_scope != "RECORDED_AUDIT_MATCHED_ONLY":
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    revision_no = con.execute(
        text("SELECT revision_no FROM dividend_event_revisions WHERE id=:id AND event_id=:event"),
        {"id": str(record.original_revision_id), "event": str(event_id)},
    ).scalar_one_or_none()
    if type(revision_no) is not int:
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    revision = events.get(con, owner, event_id, version=revision_no)
    if revision is None or revision.record.revision_id != record.original_revision_id:
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    receipt = record.receipt
    return models.ReceiptCreated.model_validate(
        {
            "id": receipt.id,
            "event_id": event_id,
            "account_id": account_id,
            "entitlement_id": record.entitlement_id,
            "original_revision_id": record.original_revision_id,
            "original_revision_no": revision_no,
            "observed_revision_id": origin.observed_revision_id,
            "observed_revision_no": origin.observed_revision_no,
            "portfolio_id": origin.portfolio_id,
            "broker_id": origin.broker_id,
            "account_version": str(origin.account_version),
            "portfolio_version": str(origin.portfolio_version),
            "broker_version": str(origin.broker_version),
            "portfolio_data_revision": str(origin.portfolio_data_revision),
            "cash_entry_id": origin.cash_entry_id,
            "received_date": receipt.received_date.isoformat(),
            "received_amount": _decimal(receipt.received),
            "currency": receipt.currency,
            "tax": _decimal(receipt.tax),
            "fee": _decimal(receipt.fee),
            "fx_rate_hkd": _decimal(receipt.fx_rate_hkd),
            "fx_source": origin.fx_source,
            "received_hkd": _decimal(receipt.received_hkd),
            "actual_hkd_override": origin.actual_hkd_reason is not None,
            "source": receipt.source,
            "status": "ACTIVE",
            "version": "1",
            "created_at": receipt.created_at,
            "audit_scope": record.audit_scope,
            "scope": "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY",
            "history_completeness": record.history_completeness,
            "current_use_scope": record.current_use_scope,
            "eligibility_scope": record.eligibility_scope,
            "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
        }
    )


def _binding(
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: history.AuditedReceipt,
) -> dict[str, object]:
    origin = record.origin
    if origin is None:
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    return {
        "receipt_id": str(record.receipt.id),
        "entitlement_id": str(record.entitlement_id),
        "original_revision_id": str(record.original_revision_id),
        "observed_revision_id": str(origin.observed_revision_id),
        "observed_revision_no": origin.observed_revision_no,
        "cash_entry_id": str(origin.cash_entry_id),
        "route": f"{request.method} {request.url.path}",
        "key_sha256": digest(headers.key),
        "intent_sha256": digest(canonical(payload)),
        "scope": "RECORDED_RECEIPT_HTTP_INTENT_ONLY",
    }


def _check_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: history.AuditedReceipt,
) -> None:
    origin = record.origin
    if origin is None:
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    rows = con.execute(
        text("""
            SELECT id,owner_id,action,request_id,reason,before_sha256,after_sha256,created_at
            FROM audit_events WHERE entity_type='dividend_receipt_http_intent' AND entity_id=:id
        """),
        {"id": str(record.receipt.id)},
    ).all()
    expected = (
        str(auth.owner.id),
        "DIVIDEND_RECEIPT_HTTP_INTENT",
        str(origin.request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, record))),
    )
    if len(rows) != 1:
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    events._uuid(rows[0].id)
    if tuple(rows[0][1:7]) != expected or events._time(rows[0].created_at) < origin.created_at:
        raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")


@router.post(
    "",
    status_code=201,
    operation_id="create_dividend_receipt",
)
def create_receipt(
    body: models.ReceiptCreate,
    request: Request,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.ReceiptCreated:
    payload = {"event_version": version, **body.model_dump(mode="json")}

    def action(con: Connection, auth: Auth) -> models.ReceiptCreated:
        current = events.get(con, auth.owner.id, body.event_id)
        if current is not None and current.record.revision_no != version:
            raise ApiError(412, "PRECONDITION_FAILED", "The observed event changed; reload it")
        intent = _intent(body, version)
        record = writer.save(con, auth.owner.id, intent, request_id=UUID(request.state.request_id))
        if record.origin is None or record.receipt.status != "ACTIVE":
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        audit(
            con,
            str(auth.owner.id),
            "dividend_receipt_http_intent",
            str(record.receipt.id),
            "DIVIDEND_RECEIPT_HTTP_INTENT",
            request_id=str(record.origin.request_id),
            after=_binding(request, headers, payload, record),
        )
        _check_intent(con, auth, request, headers, payload, record)
        return _public(con, auth.owner.id, body.event_id, body.account_id, record)

    def replay(con: Connection, auth: Auth, cached: models.ReceiptCreated) -> None:
        if cached.id != body.receipt_id:
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        retained = history.history(con, auth.owner.id, body.event_id, body.account_id)
        if retained is None:
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        matching = [item for item in retained if item.receipt.id == body.receipt_id]
        if len(matching) != 1 or matching[0].origin is None:
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        try:
            verified = writer.save(
                con, auth.owner.id, _intent(body, version), request_id=matching[0].origin.request_id
            )
        except writer.ReceiptWriteError:
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE") from None
        if _public(con, auth.owner.id, body.event_id, body.account_id, verified) != cached:
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, verified)

    def validate_commit(con: Connection, auth: Auth, result: models.ReceiptCreated) -> None:
        intent = _intent(body, version)
        record = writer.save(con, auth.owner.id, intent, request_id=UUID(request.state.request_id))
        if (
            record.receipt.status != "ACTIVE"
            or _public(con, auth.owner.id, body.event_id, body.account_id, record) != result
        ):
            raise writer.ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, record)
        writer._current_scope(con, auth.owner.id, intent, invalidated=True)

    try:
        return mutate(
            request,
            headers,
            payload,
            models.ReceiptCreated,
            action,
            status=201,
            reauth=True,
            validate_replay=replay,
            validate_commit=validate_commit,
        )
    except writer.ReceiptWriteError as error:
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
