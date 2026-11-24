"""Protect audited receipt reversals without granting payment or eligibility proof."""

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as history
import spm_api.dividend_receipt_void as voids
import spm_api.dividend_receipt_void_write_models as models
from spm_api.audit import audit, canonical, digest
from spm_api.decimal_types import INT64_MAX
from spm_api.errors import ApiError, Error
from spm_api.mutations import mutate
from spm_api.security import Auth, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/receipts",
    tags=["Recorded dividend receipt writes"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _version(
    value: Annotated[str, Header(alias="If-Match", max_length=21, pattern=r'^"[1-9][0-9]{0,18}"$')],
) -> int:
    if re.fullmatch(r'"[1-9][0-9]{0,18}"', value) is None:
        raise ApiError(422, "INVALID_RECEIPT_VERSION", "The receipt version is invalid")
    number = int(value[1:-1])
    if number > INT64_MAX:
        raise ApiError(422, "INVALID_RECEIPT_VERSION", "The receipt version is invalid")
    return number


ReceiptVersion = Annotated[int, Depends(_version)]


def _unavailable() -> ApiError:
    return ApiError(503, "DIVIDEND_RECEIPT_VOID_UNAVAILABLE", "The receipt cannot be voided safely")


def _failure(error: voids.ReceiptVoidError) -> ApiError:
    code = str(error)
    if code == "DIVIDEND_RECEIPT_NOT_FOUND":
        return ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
    if code == "DIVIDEND_RECEIPT_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code == "INVALID_DIVIDEND_RECEIPT_VOID_SCOPE":
        return ApiError(422, code, "The receipt correction inputs are invalid")
    if code == "DIVIDEND_RECEIPT_VOID_VERSION_CHANGED":
        return ApiError(412, "PRECONDITION_FAILED", "The receipt changed; reload it")
    if code in {
        "DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED",
        "DIVIDEND_RECEIPT_VOID_CONFLICT",
        "DIVIDEND_RECEIPT_UNVERIFIED",
    }:
        return ApiError(409, code, "The receipt cannot be voided using this observed scope")
    return _unavailable()


def _intent(
    identifier: UUID, body: models.ReceiptVoidWrite, version: int
) -> voids.ReceiptVoidIntent:
    return voids.ReceiptVoidIntent.model_validate(
        {
            "receipt_id": identifier,
            "event_id": body.event_id,
            "observed_revision_id": body.observed_revision_id,
            "observed_revision_no": body.observed_revision_no,
            "entitlement_id": body.entitlement_id,
            "original_revision_id": body.original_revision_id,
            "account_id": body.account_id,
            "portfolio_id": body.portfolio_id,
            "broker_id": body.broker_id,
            "account_version": int(body.account_version),
            "portfolio_version": int(body.portfolio_version),
            "broker_version": int(body.broker_version),
            "expected_data_revision": int(body.expected_data_revision),
            "cash_entry_id": body.cash_entry_id,
            "expected_receipt_version": version,
            "reason": body.reason,
            "confirmation": body.confirmation,
        }
    )


def _reversal(con: Connection, record: history.AuditedReceipt) -> UUID:
    if record.origin is None:
        raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    rows = con.execute(
        text("SELECT id FROM cash_ledger_entries WHERE reverses_entry_id=:id"),
        {"id": str(record.origin.cash_entry_id)},
    ).all()
    if len(rows) != 1:
        raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    return events._uuid(rows[0].id)


def _public(
    con: Connection, intent: voids.ReceiptVoidIntent, record: history.AuditedReceipt
) -> models.ReceiptVoided:
    receipt, origin = record.receipt, record.origin
    if (
        origin is None
        or record.audit_scope != "RECORDED_AUDIT_MATCHED_ONLY"
        or receipt.id != intent.receipt_id
        or receipt.status != "VOID"
        or receipt.version != 2
        or receipt.voided_at is None
        or record.entitlement_id != intent.entitlement_id
        or record.original_revision_id != intent.original_revision_id
        or origin.cash_entry_id != intent.cash_entry_id
    ):
        raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    return models.ReceiptVoided.model_validate(
        {
            "id": receipt.id,
            "event_id": intent.event_id,
            "observed_revision_id": intent.observed_revision_id,
            "observed_revision_no": intent.observed_revision_no,
            "entitlement_id": record.entitlement_id,
            "original_revision_id": record.original_revision_id,
            "account_id": intent.account_id,
            "portfolio_id": intent.portfolio_id,
            "origin_portfolio_id": origin.portfolio_id,
            "broker_id": intent.broker_id,
            "account_version": str(intent.account_version),
            "portfolio_version": str(intent.portfolio_version),
            "broker_version": str(intent.broker_version),
            "portfolio_data_revision": str(intent.expected_data_revision),
            "cash_entry_id": origin.cash_entry_id,
            "reversal_entry_id": _reversal(con, record),
            "status": "VOID",
            "version": "2",
            "created_at": receipt.created_at,
            "voided_at": receipt.voided_at,
            "audit_scope": record.audit_scope,
            "scope": "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY",
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
    result: models.ReceiptVoided,
) -> dict[str, object]:
    return {
        "receipt_id": str(result.id),
        "event_id": str(result.event_id),
        "account_id": str(result.account_id),
        "cash_entry_id": str(result.cash_entry_id),
        "reversal_entry_id": str(result.reversal_entry_id),
        "route": f"{request.method} {request.url.path}",
        "key_sha256": digest(headers.key),
        "intent_sha256": digest(canonical(payload)),
        "scope": "RECORDED_RECEIPT_VOID_HTTP_INTENT_ONLY",
    }


def _check_http_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    result: models.ReceiptVoided,
    request_id: UUID,
) -> None:
    rows = con.execute(
        text("""
            SELECT id,owner_id,action,request_id,reason,before_sha256,after_sha256,created_at
            FROM audit_events WHERE entity_type='dividend_receipt_void_http_intent'
              AND entity_id=:id
        """),
        {"id": str(result.id)},
    ).all()
    expected = (
        str(auth.owner.id),
        "DIVIDEND_RECEIPT_VOID_HTTP_INTENT",
        str(request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, result))),
    )
    if len(rows) != 1:
        raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    events._uuid(rows[0].id)
    if tuple(rows[0][1:7]) != expected or events._time(rows[0].created_at) < result.voided_at:
        raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")


def _request_id(con: Connection, identifier: UUID) -> UUID:
    rows = con.execute(
        text("""
            SELECT request_id FROM audit_events WHERE entity_type='dividend_receipt'
            AND entity_id=:id AND action='DIVIDEND_RECEIPT_VOIDED'
        """),
        {"id": str(identifier)},
    ).all()
    if len(rows) != 1:
        raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    return events._uuid(rows[0].request_id)


@router.post("/{id}/void", operation_id="void_dividend_receipt")
def void_receipt(
    id: UUID,
    body: models.ReceiptVoidWrite,
    request: Request,
    headers: WriteDep,
    version: ReceiptVersion,
) -> models.ReceiptVoided:
    payload = {"receipt_id": str(id), "receipt_version": version, **body.model_dump(mode="json")}
    frozen_scope: tuple[tuple[str, int, int], ...] | None = None

    def action(con: Connection, auth: Auth) -> models.ReceiptVoided:
        nonlocal frozen_scope
        intent = _intent(id, body, version)
        retained = history.history(con, auth.owner.id, body.event_id, body.account_id)
        if retained is None:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_NOT_FOUND")
        matching = [item for item in retained if item.receipt.id == id]
        if not matching:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_NOT_FOUND")
        if len(matching) != 1:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        if matching[0].origin is None or matching[0].audit_scope != "RECORDED_AUDIT_MATCHED_ONLY":
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_UNVERIFIED")
        if matching[0].receipt.status == "VOID":
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_CONFLICT")
        request_id = UUID(request.state.request_id)
        record = voids.void(con, auth.owner.id, intent, request_id=request_id)
        if record.origin is None:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        result = _public(con, intent, record)
        frozen_scope = voids._scope(con, auth.owner.id, intent, record.origin, invalidated=True)
        audit(
            con,
            str(auth.owner.id),
            "dividend_receipt_void_http_intent",
            str(id),
            "DIVIDEND_RECEIPT_VOID_HTTP_INTENT",
            request_id=str(request_id),
            after=_binding(request, headers, payload, result),
        )
        _check_http_intent(con, auth, request, headers, payload, result, request_id)
        return result

    def replay(con: Connection, auth: Auth, cached: models.ReceiptVoided) -> None:
        if cached.id != id:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        retained = history.history(con, auth.owner.id, body.event_id, body.account_id)
        if retained is None:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        matching = [item for item in retained if item.receipt.id == id]
        if len(matching) != 1 or matching[0].origin is None:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        request_id = _request_id(con, id)
        intent = _intent(id, body, version)
        try:
            verified = voids.void(con, auth.owner.id, intent, request_id=request_id)
        except voids.ReceiptVoidError:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE") from None
        if _public(con, intent, verified) != cached:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        _check_http_intent(con, auth, request, headers, payload, cached, request_id)

    def validate_commit(con: Connection, auth: Auth, result: models.ReceiptVoided) -> None:
        intent = _intent(id, body, version)
        request_id = UUID(request.state.request_id)
        record = voids.void(con, auth.owner.id, intent, request_id=request_id)
        if record.origin is None or _public(con, intent, record) != result:
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        _check_http_intent(con, auth, request, headers, payload, result, request_id)
        if (
            frozen_scope is None
            or voids._scope(con, auth.owner.id, intent, record.origin, invalidated=True)
            != frozen_scope
        ):
            raise voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED")

    try:
        return mutate(
            request,
            headers,
            payload,
            models.ReceiptVoided,
            action,
            reauth=True,
            validate_replay=replay,
            validate_commit=validate_commit,
        )
    except voids.ReceiptVoidError as error:
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
