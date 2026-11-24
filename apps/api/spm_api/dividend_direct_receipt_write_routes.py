"""Protect direct recorded cash with atomic HTTP intent, replay and final scope checks."""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_history as history
import spm_api.dividend_direct_receipt_write_models as models
import spm_api.dividend_direct_receipt_writer as writer
import spm_api.dividend_event_service as events
from spm_api.audit import audit, canonical, digest
from spm_api.dividend_receipt_terms import ReceiptComponents
from spm_api.errors import ApiError, Error
from spm_api.mutations import mutate
from spm_api.security import Auth, DividendEventVersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/direct-receipts",
    tags=["Direct recorded dividend receipt writes"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503,
        "DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE",
        "The direct receipt cannot be read safely",
    )


def _failure(error: writer.DirectReceiptWriteError) -> ApiError:
    code = str(error)
    if code == "DIVIDEND_DIRECT_RECEIPT_NOT_FOUND":
        return ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
    if code == "DIRECT_RECEIPT_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code in {
        "INVALID_DIRECT_RECEIPT_SCOPE",
        "INVALID_DIVIDEND_RECEIPT_TERMS",
        "DIVIDEND_DIRECT_RECEIPT_DATE_INVALID",
    }:
        return ApiError(422, code, "The direct receipt inputs are invalid")
    if code in {
        "DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED",
        "DIVIDEND_DIRECT_RECEIPT_ACCOUNT_ARCHIVED",
        "DIVIDEND_DIRECT_RECEIPT_EVENT_WITHDRAWN",
        "DIVIDEND_DIRECT_RECEIPT_CONFLICT",
        "DIVIDEND_REVISION_REJECTED",
    }:
        return ApiError(409, code, "The direct receipt no longer matches its observed scope")
    return _unavailable()


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("Invalid retained cash component")
    return format(value, "f")


def _intent(body: models.DirectReceiptCreate, version: int) -> writer.DirectReceiptIntent:
    return writer.DirectReceiptIntent.model_validate(
        {
            "id": body.receipt_id,
            "event_id": body.event_id,
            "revision_id": body.revision_id,
            "revision_no": version,
            "account_id": body.account_id,
            "portfolio_id": body.portfolio_id,
            "broker_id": body.broker_id,
            "account_version": int(body.account_version),
            "portfolio_version": int(body.portfolio_version),
            "broker_version": int(body.broker_version),
            "expected_data_revision": int(body.expected_data_revision),
            "received_date": body.received_date,
            "components": {name: getattr(body, name) for name in ReceiptComponents.model_fields},
            "note": body.note,
            "reason": body.reason,
            "confirmation": body.confirmation,
            "confirm_revision_fence": body.confirm_revision_fence,
        }
    )


def _public(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    account_id: UUID,
    record: history.RecordedDirectReceipt,
) -> models.DirectReceiptCreated:
    if not isinstance(record, history.RecordedDirectReceipt):
        raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
    retained = combined.history(con, owner, event_id, account_id)
    if retained is None or [item for item in retained if item.receipt.id == record.receipt.id] != [
        record
    ]:
        raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
    origin = record.origin
    revision = events.get(con, owner, event_id, version=record.revision_no)
    if (
        revision is None
        or (revision.record.owner_id, revision.record.event_id, revision.record.revision_id)
        != (owner, event_id, record.revision_id)
        or (origin.observed_revision_id, origin.observed_revision_no)
        != (record.revision_id, record.revision_no)
    ):
        raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
    receipt = record.receipt
    return models.DirectReceiptCreated.model_validate(
        {
            "id": receipt.id,
            "event_id": event_id,
            "account_id": account_id,
            "revision_id": record.revision_id,
            "revision_no": record.revision_no,
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
            "actual_hkd": (
                _decimal(receipt.received_hkd) if origin.actual_hkd_reason is not None else None
            ),
            "actual_hkd_reason": origin.actual_hkd_reason,
            "receipt_basis": "DIRECT_EVENT_ACCOUNT",
            "source": receipt.source,
            "status": "ACTIVE",
            "version": "1",
            "created_at": receipt.created_at,
            "audit_scope": record.audit_scope,
            "scope": "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY",
            "history_completeness": record.history_completeness,
            "current_use_scope": record.current_use_scope,
            "eligibility_scope": record.eligibility_scope,
            "payment_scope": record.payment_scope,
        }
    )


def _binding(
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: history.RecordedDirectReceipt,
) -> dict[str, object]:
    return {
        "receipt_id": str(record.receipt.id),
        "event_id": payload["event_id"],
        "account_id": payload["account_id"],
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "cash_entry_id": str(record.origin.cash_entry_id),
        "route": f"{request.method} {request.url.path}",
        "key_sha256": digest(headers.key),
        "intent_sha256": digest(canonical(payload)),
        "scope": "DIRECT_RECORDED_RECEIPT_HTTP_INTENT_ONLY",
    }


def _check_intent(
    con: Connection,
    auth: Auth,
    request: Request,
    headers: WriteDep,
    payload: dict[str, Any],
    record: history.RecordedDirectReceipt,
) -> None:
    rows = con.execute(
        text("""
            SELECT id,owner_id,action,request_id,reason,before_sha256,after_sha256,created_at
            FROM audit_events WHERE entity_type='dividend_direct_receipt_http_intent'
              AND entity_id=:id
        """),
        {"id": str(record.receipt.id)},
    ).all()
    expected = (
        str(auth.owner.id),
        "DIVIDEND_DIRECT_RECEIPT_HTTP_INTENT",
        str(record.origin.request_id),
        "",
        None,
        digest(canonical(_binding(request, headers, payload, record))),
    )
    if len(rows) != 1:
        raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
    events._uuid(rows[0].id)
    if (
        tuple(rows[0][1:7]) != expected
        or events._time(rows[0].created_at) < record.origin.created_at
    ):
        raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")


@router.post("", status_code=201, operation_id="create_direct_dividend_receipt")
def create_receipt(
    body: models.DirectReceiptCreate,
    request: Request,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.DirectReceiptCreated:
    payload = {"event_version": version, **body.model_dump(mode="json")}

    def action(con: Connection, auth: Auth) -> models.DirectReceiptCreated:
        current = events.get(con, auth.owner.id, body.event_id)
        if current is None:
            raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
        if (current.record.owner_id, current.record.event_id) != (auth.owner.id, body.event_id):
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        if current.record.revision_no != version:
            raise ApiError(412, "PRECONDITION_FAILED", "The observed event changed; reload it")
        retained = combined.history(con, auth.owner.id, body.event_id, body.account_id)
        if retained is None:
            raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
        # Only mutate's validated cache replay may recover an existing recorded operation.
        if any(item.receipt.id == body.receipt_id for item in retained):
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_CONFLICT")
        record = writer.save(
            con, auth.owner.id, _intent(body, version), request_id=UUID(request.state.request_id)
        )
        if (
            not isinstance(record, history.RecordedDirectReceipt)
            or record.receipt.status != "ACTIVE"
        ):
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        audit(
            con,
            str(auth.owner.id),
            "dividend_direct_receipt_http_intent",
            str(record.receipt.id),
            "DIVIDEND_DIRECT_RECEIPT_HTTP_INTENT",
            request_id=str(record.origin.request_id),
            after=_binding(request, headers, payload, record),
        )
        _check_intent(con, auth, request, headers, payload, record)
        return _public(con, auth.owner.id, body.event_id, body.account_id, record)

    def replay(con: Connection, auth: Auth, cached: models.DirectReceiptCreated) -> None:
        if cached.id != body.receipt_id:
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        retained = combined.history(con, auth.owner.id, body.event_id, body.account_id)
        if retained is None:
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        matching = [item for item in retained if item.receipt.id == body.receipt_id]
        if len(matching) != 1 or not isinstance(matching[0], history.RecordedDirectReceipt):
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        try:
            verified = writer.save(
                con, auth.owner.id, _intent(body, version), request_id=matching[0].origin.request_id
            )
        except writer.DirectReceiptWriteError:
            raise writer.DirectReceiptWriteError(
                "DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE"
            ) from None
        if _public(con, auth.owner.id, body.event_id, body.account_id, verified) != cached:
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, verified)

    def validate_commit(con: Connection, auth: Auth, result: models.DirectReceiptCreated) -> None:
        intent = _intent(body, version)
        record = writer.save(con, auth.owner.id, intent, request_id=UUID(request.state.request_id))
        if not isinstance(record, history.RecordedDirectReceipt) or (
            record.receipt.status != "ACTIVE"
            or _public(con, auth.owner.id, body.event_id, body.account_id, record) != result
        ):
            raise writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE")
        _check_intent(con, auth, request, headers, payload, record)
        writer._current_scope(con, auth.owner.id, intent, invalidated=True)

    try:
        return mutate(
            request,
            headers,
            payload,
            models.DirectReceiptCreated,
            action,
            status=201,
            reauth=True,
            validate_replay=replay,
            validate_commit=validate_commit,
        )
    except writer.DirectReceiptWriteError as error:
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
