"""Owner-session-scoped retained receipt reads, never a financial grant."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_history as history
import spm_api.dividend_receipt_models as models
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account, positive_version
from spm_api.errors import ApiError, Error
from spm_api.pagination import Page, PageDep, paginate, validate_date_page, validate_version_page
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


@router.get("/inputs", operation_id="get_recorded_dividend_receipt_inputs")
def receipt_inputs(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> models.ReceiptInputs:
    validate_version_page(page)
    try:
        with unit_of_work(request.app.state.engine) as con:
            owner = auth.owner.id
            event = events.get(con, owner, event_id)
            current = account(con, owner, account_id)
            if event is None or current is None:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            retained = history.history(con, owner, event_id, account_id)
            if retained is None or (
                retained.event_id,
                retained.account_id,
                retained.current_revision_id,
            ) != (event_id, account_id, event.record.revision_id):
                raise ValueError("Inconsistent retained receipt scope")
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
            records = {str(item.id): item for item in retained.entitlements}
            if len(records) != len(retained.entitlements):
                raise ValueError("Duplicate retained entitlement")
            rows = (
                con.execute(
                    text(r"""
                        SELECT ent.id,rev.id AS revision_id,rev.revision_no
                        FROM dividend_entitlements ent
                        JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                        WHERE rev.event_id=:event AND ent.account_id=:account
                          AND ent.id LIKE :search ESCAPE '\'
                          AND (:cursor_value IS NULL OR
                               (rev.revision_no,ent.id)<(:cursor_value,:cursor_id))
                        ORDER BY rev.revision_no DESC,ent.id DESC LIMIT :limit
                    """),
                    {
                        **page.params(),
                        "event": str(event_id),
                        "account": str(account_id),
                        "cursor_value": (
                            int(page.cursor_value) if page.cursor_value is not None else None
                        ),
                    },
                )
                .mappings()
                .all()
            )

            def basis(row: RowMapping) -> models.ExistingReceiptBasis:
                item = records[row["id"]]
                if (
                    row["revision_id"] != str(item.revision_id)
                    or type(row["revision_no"]) is not int
                    or row["revision_no"] != item.revision_no
                ):
                    raise ValueError("Inconsistent retained entitlement revision")
                return models.ExistingReceiptBasis.model_validate(
                    {
                        "id": item.id,
                        "revision_id": item.revision_id,
                        "revision_no": item.revision_no,
                        "currency": item.currency,
                        "recorded_shares": _decimal(item.eligible_shares),
                        "recorded_expected_net": _decimal(item.expected_net),
                        "recorded_quality": item.recorded_quality,
                        "created_at": item.created_at,
                        "scope": item.scope,
                    }
                )

            return models.ReceiptInputs(
                event_id=event_id,
                revision_id=event.record.revision_id,
                revision_no=event.record.revision_no,
                account_id=account_id,
                portfolio_id=current["portfolio_id"],
                broker_id=current["broker_id"],
                account_status=scope["status"],
                account_version=str(current["account_version"]),
                portfolio_version=str(portfolio_version),
                broker_version=str(current["broker_version"]),
                portfolio_data_revision=str(revision),
                entitlements=paginate(rows, page, "revision_no", basis),
                scope="RECORDED_RECEIPT_INPUTS_ONLY",
                history_completeness=retained.history_completeness,
                current_use_scope=retained.current_use_scope,
                eligibility_scope=retained.eligibility_scope,
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
            "DIVIDEND_RECEIPT_INPUTS_UNAVAILABLE",
            "The receipt inputs cannot be read safely",
        ) from None


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
