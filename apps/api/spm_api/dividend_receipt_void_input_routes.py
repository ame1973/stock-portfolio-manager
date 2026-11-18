"""Private receipt correction inputs; no authorization to reverse recorded cash."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as history
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account, positive_version
from spm_api.dividend_receipt_models import ReceiptVoidInputs
from spm_api.errors import ApiError, Error
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts",
    tags=["Recorded dividend receipt history"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


@router.get("/{receipt_id}/void-inputs", operation_id="get_recorded_dividend_receipt_void_inputs")
def receipt_void_inputs(
    event_id: UUID, account_id: UUID, receipt_id: UUID, request: Request, auth: AuthDep
) -> ReceiptVoidInputs:
    try:
        with unit_of_work(request.app.state.engine) as con:
            owner = auth.owner.id
            event = events.get(con, owner, event_id)
            current = account(con, owner, account_id)
            if event is None or current is None:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            records = history.history(con, owner, event_id, account_id)
            if records is None:
                raise ValueError("Inconsistent retained receipt scope")
            matches = [record for record in records if record.receipt.id == receipt_id]
            if not matches:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            if len(matches) != 1:
                raise ValueError("Duplicate retained receipt")
            scope = (
                con.execute(
                    text("""
                        SELECT a.status,p.version,p.data_revision FROM accounts a
                        JOIN portfolios p ON p.id=a.portfolio_id
                        WHERE a.id=:account AND p.id=:portfolio AND p.owner_id=:owner
                    """),
                    {
                        "account": str(account_id),
                        "portfolio": current["portfolio_id"],
                        "owner": str(owner),
                    },
                )
                .mappings()
                .one_or_none()
            )
            if scope is None:
                raise ValueError("Inconsistent current receipt scope")
            portfolio_version = positive_version(scope["version"])
            revision = scope["data_revision"]
            if type(revision) is not int or not 0 <= revision <= INT64_MAX:
                raise ValueError("Invalid portfolio revision")
            record = matches[0]
            receipt, origin = record.receipt, record.origin
            return ReceiptVoidInputs.model_validate(
                {
                    "receipt_id": receipt.id,
                    "event_id": event_id,
                    "observed_revision_id": event.record.revision_id,
                    "observed_revision_no": event.record.revision_no,
                    "account_id": account_id,
                    "portfolio_id": current["portfolio_id"],
                    "broker_id": current["broker_id"],
                    "account_status": scope["status"],
                    "account_version": str(current["account_version"]),
                    "portfolio_version": str(portfolio_version),
                    "broker_version": str(current["broker_version"]),
                    "portfolio_data_revision": str(revision),
                    "entitlement_id": record.entitlement_id,
                    "original_revision_id": record.original_revision_id,
                    "cash_entry_id": origin.cash_entry_id if origin else None,
                    "origin_portfolio_id": origin.portfolio_id if origin else None,
                    "received_date": receipt.received_date.isoformat(),
                    "received_amount": _decimal(receipt.received),
                    "currency": receipt.currency,
                    "tax": _decimal(receipt.tax),
                    "fee": _decimal(receipt.fee),
                    "fx_rate_hkd": _decimal(receipt.fx_rate_hkd),
                    "received_hkd": _decimal(receipt.received_hkd),
                    "status": receipt.status,
                    "receipt_version": str(receipt.version),
                    "created_at": receipt.created_at,
                    "voided_at": receipt.voided_at,
                    "audit_scope": record.audit_scope,
                    "scope": "RECORDED_RECEIPT_VOID_INPUTS_ONLY",
                    "history_completeness": record.history_completeness,
                    "current_use_scope": record.current_use_scope,
                    "eligibility_scope": record.eligibility_scope,
                    "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
                }
            )
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise ApiError(
            503,
            "DIVIDEND_RECEIPT_VOID_INPUTS_UNAVAILABLE",
            "The receipt correction inputs cannot be read safely",
        ) from None
