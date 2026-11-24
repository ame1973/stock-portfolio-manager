"""Protect direct receipt reversals with bound HTTP intent and zero-write replay."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_history as history
import spm_api.dividend_direct_receipt_void as voids
import spm_api.dividend_direct_receipt_void_write_models as models
import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as legacy
from spm_api.audit import audit, canonical, digest
from spm_api.dividend_receipt_void_write_routes import ReceiptVersion
from spm_api.errors import ApiError, Error
from spm_api.mutations import mutate
from spm_api.security import Auth, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/direct-receipts",
    tags=["Direct recorded dividend receipt writes"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIRECT_RECEIPT_VOID_UNAVAILABLE", "The direct receipt cannot be voided safely"
    )


def _failure(error: voids.DirectReceiptVoidError) -> ApiError:
    code = str(error)
    if code == "DIRECT_RECEIPT_NOT_FOUND":
        return ApiError(404, "NOT_FOUND", "Direct dividend receipt scope not found")
    if code == "DIRECT_RECEIPT_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code == "INVALID_DIRECT_RECEIPT_VOID_SCOPE":
        return ApiError(422, code, "The direct receipt correction inputs are invalid")
    if code == "DIRECT_RECEIPT_VOID_VERSION_CHANGED":
        return ApiError(412, "PRECONDITION_FAILED", "The receipt changed; reload it")
    if code in {"DIRECT_RECEIPT_VOID_SCOPE_CHANGED", "DIRECT_RECEIPT_VOID_CONFLICT"}:
        return ApiError(409, code, "The direct receipt cannot be voided using this observed scope")
    return _unavailable()


def _intent(
    identifier: UUID, body: models.DirectReceiptVoidWrite, version: int
) -> voids.DirectReceiptVoidIntent:
    return voids.DirectReceiptVoidIntent.model_validate(
        {
            **body.model_dump(),
            "receipt_id": identifier,
            "expected_receipt_version": version,
            **{
                name: int(getattr(body, name))
                for name in (
                    "account_version",
                    "portfolio_version",
                    "broker_version",
                    "expected_data_revision",
                )
            },
        }
    )


def _record(
    con: Connection, owner: UUID, intent: voids.DirectReceiptVoidIntent
) -> history.RecordedDirectReceipt:
    retained = combined.history(con, owner, intent.event_id, intent.account_id)
    if retained is None:
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_NOT_FOUND")
    if any(
        not isinstance(item, history.RecordedDirectReceipt | legacy.AuditedReceipt)
        for item in retained
    ):
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    matches = [item for item in retained if item.receipt.id == intent.receipt_id]
    if len(matches) > 1:
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    if not matches or not isinstance(matches[0], history.RecordedDirectReceipt):
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_NOT_FOUND")
    return matches[0]


def _public(
    con: Connection,
    owner: UUID,
    intent: voids.DirectReceiptVoidIntent,
    record: history.RecordedDirectReceipt,
) -> models.DirectReceiptVoided:
    # Independently read retained cash instead of trusting a consistently substituted return.
    if (
        not isinstance(record, history.RecordedDirectReceipt)
        or _record(con, owner, intent) != record
    ):
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    receipt, origin = record.receipt, record.origin
    if (
        receipt.status != "VOID"
        or receipt.version != 2
        or receipt.voided_at is None
        or record.revision_id != intent.original_revision_id
        or record.revision_no != intent.original_revision_no
        or origin.cash_entry_id != intent.cash_entry_id
    ):
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    rows = con.execute(
        text("SELECT id FROM cash_ledger_entries WHERE reverses_entry_id=:id"),
        {"id": str(origin.cash_entry_id)},
    ).all()
    if len(rows) != 1:
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    return models.DirectReceiptVoided.model_validate(
        {
            "id": receipt.id,
            "event_id": intent.event_id,
            "observed_revision_id": intent.observed_revision_id,
            "observed_revision_no": intent.observed_revision_no,
            "original_revision_id": record.revision_id,
            "original_revision_no": record.revision_no,
            "account_id": intent.account_id,
            "portfolio_id": intent.portfolio_id,
            "origin_portfolio_id": origin.portfolio_id,
            "broker_id": intent.broker_id,
            "account_version": str(intent.account_version),
            "portfolio_version": str(intent.portfolio_version),
            "broker_version": str(intent.broker_version),
            "portfolio_data_revision": str(intent.expected_data_revision),
            "cash_entry_id": origin.cash_entry_id,
            "reversal_entry_id": events._uuid(rows[0].id),
            "receipt_basis": "DIRECT_EVENT_ACCOUNT",
            "status": "VOID",
            "version": "2",
            "created_at": receipt.created_at,
            "voided_at": receipt.voided_at,
            "audit_scope": record.audit_scope,
            "scope": "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY",
            "history_completeness": record.history_completeness,
            "current_use_scope": record.current_use_scope,
            "eligibility_scope": record.eligibility_scope,
            "payment_scope": record.payment_scope,
        }
    )


def _binding(
    request: Request, headers: WriteDep, payload: dict[str, Any], result: models.DirectReceiptVoided
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
        "scope": "DIRECT_RECORDED_RECEIPT_VOID_HTTP_INTENT_ONLY",
    }


def _check_http_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    result: models.DirectReceiptVoided,
    request_id: UUID,
) -> None:
    rows = con.execute(
        text("""
            SELECT id,owner_id,action,request_id,reason,before_sha256,after_sha256,created_at
            FROM audit_events WHERE entity_type='dividend_direct_receipt_void_http_intent'
              AND entity_id=:id
        """),
        {"id": str(result.id)},
    ).all()
    expected = (
        str(auth.owner.id),
        "DIVIDEND_DIRECT_RECEIPT_VOID_HTTP_INTENT",
        str(request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, result))),
    )
    if len(rows) != 1:
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    events._uuid(rows[0].id)
    if tuple(rows[0][1:7]) != expected or events._time(rows[0].created_at) < result.voided_at:
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")


def _request_id(con: Connection, identifier: UUID) -> UUID:
    rows = con.execute(
        text("""
            SELECT request_id FROM audit_events WHERE entity_type='dividend_direct_receipt'
              AND entity_id=:id AND action='DIVIDEND_DIRECT_RECEIPT_VOIDED'
        """),
        {"id": str(identifier)},
    ).all()
    if len(rows) != 1:
        raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
    return events._uuid(rows[0].request_id)


@router.post("/{id}/void", operation_id="void_direct_dividend_receipt")
def void_receipt(
    id: UUID,
    body: models.DirectReceiptVoidWrite,
    request: Request,
    headers: WriteDep,
    version: ReceiptVersion,
) -> models.DirectReceiptVoided:
    payload = {"receipt_id": str(id), "receipt_version": version, **body.model_dump(mode="json")}
    frozen_scope: tuple[tuple[str, int, int], ...] | None = None

    def action(con: Connection, auth: Auth) -> models.DirectReceiptVoided:
        nonlocal frozen_scope
        intent = _intent(id, body, version)
        retained = _record(con, auth.owner.id, intent)
        # Only a validated idempotency replay may recover a previously recorded void.
        if retained.receipt.status == "VOID":
            raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_CONFLICT")
        frozen_scope = tuple(
            (portfolio, current_version, revision + 1)
            for portfolio, current_version, revision in voids._scope(
                con, auth.owner.id, intent, retained.origin
            )
        )
        request_id = UUID(request.state.request_id)
        record = voids.void(con, auth.owner.id, intent, request_id=request_id)
        result = _public(con, auth.owner.id, intent, record)
        audit(
            con,
            str(auth.owner.id),
            "dividend_direct_receipt_void_http_intent",
            str(id),
            "DIVIDEND_DIRECT_RECEIPT_VOID_HTTP_INTENT",
            request_id=str(request_id),
            after=_binding(request, headers, payload, result),
        )
        _check_http_intent(con, auth, request, headers, payload, result, request_id)
        return result

    def replay(con: Connection, auth: Auth, cached: models.DirectReceiptVoided) -> None:
        try:
            intent = _intent(id, body, version)
            record = _record(con, auth.owner.id, intent)
            request_id = _request_id(con, id)
            voids._verify_void(con, auth.owner.id, intent, request_id, record)
            if _public(con, auth.owner.id, intent, record) != cached:
                raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
            _check_http_intent(con, auth, request, headers, payload, cached, request_id)
        except voids.DirectReceiptVoidError:
            raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE") from None

    def validate_commit(con: Connection, auth: Auth, result: models.DirectReceiptVoided) -> None:
        intent = _intent(id, body, version)
        record = _record(con, auth.owner.id, intent)
        request_id = UUID(request.state.request_id)
        voids._verify_void(con, auth.owner.id, intent, request_id, record)
        if _public(con, auth.owner.id, intent, record) != result:
            raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_UNAVAILABLE")
        _check_http_intent(con, auth, request, headers, payload, result, request_id)
        if (
            frozen_scope is None
            or voids._scope(con, auth.owner.id, intent, record.origin, invalidated=True)
            != frozen_scope
        ):
            raise voids.DirectReceiptVoidError("DIRECT_RECEIPT_VOID_SCOPE_CHANGED")

    try:
        return mutate(
            request,
            headers,
            payload,
            models.DirectReceiptVoided,
            action,
            reauth=True,
            validate_replay=replay,
            validate_commit=validate_commit,
        )
    except voids.DirectReceiptVoidError as error:
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
