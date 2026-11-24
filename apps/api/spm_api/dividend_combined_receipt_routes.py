"""Owner-scoped combined receipt history without changing legacy write authority."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_combined_receipt_history as history
import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as legacy
from spm_api.db import unit_of_work
from spm_api.dividend_combined_receipt_models import CombinedRecordedReceipt
from spm_api.errors import ApiError, Error
from spm_api.pagination import Page, PageDep, paginate, validate_date_page
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}",
    tags=["Combined recorded dividend receipt history"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("Invalid retained receipt amount")
    return format(value, "f")


def _public(
    current: events.EventRevision,
    account_id: UUID,
    record: history.RecordedReceipt,
    revisions: dict[UUID, int],
) -> CombinedRecordedReceipt:
    if isinstance(record, direct.RecordedDirectReceipt):
        revision_id = record.revision_id
        if revisions[revision_id] != record.revision_no:
            raise ValueError("Inconsistent direct receipt revision")
        basis, entitlement = "DIRECT_EVENT_ACCOUNT", None
    elif isinstance(record, legacy.AuditedReceipt):
        revision_id = record.original_revision_id
        basis, entitlement = "LEGACY_ENTITLEMENT", record.entitlement_id
    else:
        raise ValueError("Invalid retained receipt record")
    receipt = record.receipt
    return CombinedRecordedReceipt.model_validate(
        {
            "id": receipt.id,
            "event_id": current.event_id,
            "account_id": account_id,
            "receipt_basis": basis,
            "entitlement_id": entitlement,
            "revision_id": revision_id,
            "revision_no": revisions[revision_id],
            "current_revision_id": current.revision_id,
            "current_revision_no": current.revision_no,
            "received_date": receipt.received_date.isoformat(),
            "recorded_amount": _amount(receipt.received),
            "currency": receipt.currency,
            "recorded_tax": _amount(receipt.tax),
            "recorded_fee": _amount(receipt.fee),
            "fx_rate_hkd": _amount(receipt.fx_rate_hkd),
            "received_hkd": _amount(receipt.received_hkd),
            "source": receipt.source,
            "status": receipt.status,
            "version": str(receipt.version),
            "created_at": receipt.created_at,
            "voided_at": receipt.voided_at,
            "cash_scope": receipt.cash_scope,
            "audit_scope": record.audit_scope,
            "scope": "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY",
            "history_completeness": record.history_completeness,
            "current_use_scope": record.current_use_scope,
            "eligibility_scope": record.eligibility_scope,
            "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
        }
    )


@router.get("/recorded-receipts", operation_id="list_combined_recorded_dividend_receipts")
def receipt_history(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[CombinedRecordedReceipt]:
    validate_date_page(page, None, None)
    try:
        with unit_of_work(request.app.state.engine) as con:
            current = events.get(con, auth.owner.id, event_id)
            if current is None:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            if (current.record.owner_id, current.record.event_id) != (auth.owner.id, event_id):
                raise ValueError("Inconsistent retained event scope")
            retained = history.history(con, auth.owner.id, event_id, account_id)
            if retained is None:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            if any(
                not isinstance(item, direct.RecordedDirectReceipt | legacy.AuditedReceipt)
                for item in retained
            ):
                raise ValueError("Invalid retained receipt record")
            records = {str(item.receipt.id): item for item in retained}
            if len(records) != len(retained):
                raise ValueError("Duplicate retained receipt identity")
            revisions = {
                events._uuid(row.id): row.revision_no
                for row in con.execute(
                    text("SELECT id,revision_no FROM dividend_event_revisions WHERE event_id=:id"),
                    {"id": str(event_id)},
                )
            }
            rows = (
                con.execute(
                    text(r"""
                    SELECT receipt.id,receipt.received_date FROM (
                        SELECT old.id,old.received_date FROM dividend_receipts old
                        JOIN dividend_entitlements ent ON ent.id=old.entitlement_id
                        JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                        WHERE rev.event_id=:event AND ent.account_id=:account
                          AND old.account_id=:account
                        UNION ALL
                        SELECT id,received_date FROM dividend_direct_receipts
                        WHERE event_id=:event AND account_id=:account
                    ) receipt
                    WHERE receipt.id LIKE :search ESCAPE '\'
                      AND (:cursor_value IS NULL OR
                           (receipt.received_date,receipt.id)<(:cursor_value,:cursor_id))
                    ORDER BY receipt.received_date DESC,receipt.id DESC LIMIT :limit
                """),
                    {**page.params(), "event": str(event_id), "account": str(account_id)},
                )
                .mappings()
                .all()
            )

            def public(row: RowMapping) -> CombinedRecordedReceipt:
                record = records[row["id"]]
                if row["received_date"] != record.receipt.received_date.isoformat():
                    raise ValueError("Inconsistent recorded receipt date")
                return _public(current.record, account_id, record, revisions)

            return paginate(rows, page, "received_date", public)
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
            "COMBINED_RECEIPT_HISTORY_UNAVAILABLE",
            "The combined receipt history cannot be read safely",
        ) from None
