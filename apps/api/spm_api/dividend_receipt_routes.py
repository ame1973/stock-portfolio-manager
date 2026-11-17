"""Owner-session-scoped retained receipt reads, never a financial grant."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_receipt_history as history
import spm_api.dividend_receipt_models as models
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error
from spm_api.pagination import Page, PageDep, paginate, validate_date_page
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts",
    tags=["Recorded dividend receipt history"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _public(
    retained: history.ReceiptHistory,
    entitlement: history.RetainedEntitlement,
    receipt: history.RetainedReceipt,
) -> models.RecordedReceipt:
    return models.RecordedReceipt.model_validate(
        {
            "id": receipt.id,
            "event_id": retained.event_id,
            "account_id": retained.account_id,
            "entitlement_id": entitlement.id,
            "revision_id": entitlement.revision_id,
            "revision_no": entitlement.revision_no,
            "current_revision_id": retained.current_revision_id,
            "received_date": receipt.received_date.isoformat(),
            "recorded_amount": _decimal(receipt.received),
            "currency": receipt.currency,
            "recorded_tax": _decimal(receipt.tax),
            "recorded_fee": _decimal(receipt.fee),
            "fx_rate_hkd": _decimal(receipt.fx_rate_hkd),
            "received_hkd": _decimal(receipt.received_hkd),
            "source": receipt.source,
            "status": receipt.status,
            "version": str(receipt.version),
            "created_at": receipt.created_at,
            "voided_at": receipt.voided_at,
            "cash_scope": receipt.cash_scope,
            "audit_scope": receipt.audit_scope,
            "history_completeness": retained.history_completeness,
            "scope": retained.scope,
            "current_use_scope": retained.current_use_scope,
            "eligibility_scope": retained.eligibility_scope,
        }
    )


@router.get("", operation_id="list_recorded_dividend_receipts")
def receipt_history(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[models.RecordedReceipt]:
    validate_date_page(page, None, None)
    try:
        with unit_of_work(request.app.state.engine) as con:
            retained = history.history(con, auth.owner.id, event_id, account_id)
            if retained is None:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            records = {
                str(receipt.id): (entitlement, receipt)
                for entitlement in retained.entitlements
                for receipt in entitlement.receipts
            }
            if len(records) != sum(
                len(entitlement.receipts) for entitlement in retained.entitlements
            ):
                raise ValueError("Duplicate retained receipt")
            rows = (
                con.execute(
                    text(r"""
                        SELECT receipt.id,receipt.received_date FROM dividend_receipts receipt
                        JOIN dividend_entitlements ent ON ent.id=receipt.entitlement_id
                        JOIN dividend_event_revisions revision ON revision.id=ent.revision_id
                        WHERE revision.event_id=:event AND ent.account_id=:account
                          AND receipt.account_id=:account AND receipt.id LIKE :search ESCAPE '\'
                          AND (:cursor_value IS NULL OR
                               (receipt.received_date,receipt.id)<(:cursor_value,:cursor_id))
                        ORDER BY receipt.received_date DESC,receipt.id DESC LIMIT :limit
                    """),
                    {**page.params(), "event": str(event_id), "account": str(account_id)},
                )
                .mappings()
                .all()
            )
            return paginate(
                rows,
                page,
                "received_date",
                lambda row: _public(retained, *records[row["id"]]),
            )
    except (
        history.ReceiptHistoryError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise ApiError(
            503,
            "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE",
            "The receipt history cannot be read safely",
        ) from None
