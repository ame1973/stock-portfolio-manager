"""Read direct receipt input pins in one owned snapshot without authorizing cash."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_combined_receipt_history as receipts
import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_service as rejections
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account, positive_version
from spm_api.dividend_direct_receipt_input_models import DirectReceiptInputs
from spm_api.errors import ApiError, Error
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts",
    tags=["Direct recorded dividend receipt inputs"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


@router.get("/inputs", operation_id="get_direct_recorded_dividend_receipt_inputs")
def receipt_inputs(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep
) -> DirectReceiptInputs:
    try:
        with unit_of_work(request.app.state.engine) as con:
            owner = auth.owner.id
            reviewed = events.get(con, owner, event_id)
            current = account(con, owner, account_id)
            if reviewed is None or current is None:
                raise ApiError(404, "NOT_FOUND", "Dividend receipt scope not found")
            event = reviewed.record
            if (event.owner_id, event.event_id, events._uuid(current["id"])) != (
                owner,
                event_id,
                account_id,
            ):
                raise ValueError("Inconsistent input scope")
            scope = (
                con.execute(
                    text("""
                        SELECT a.status,a.version AS account_version,
                               p.version AS portfolio_version,p.data_revision,
                               b.version AS broker_version
                        FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
                        JOIN brokers b ON b.id=a.broker_id
                        WHERE a.id=:account AND p.id=:portfolio AND b.id=:broker
                          AND p.owner_id=:owner AND b.owner_id=:owner
                    """),
                    {
                        "account": str(account_id),
                        "portfolio": current["portfolio_id"],
                        "broker": current["broker_id"],
                        "owner": str(owner),
                    },
                )
                .mappings()
                .one_or_none()
            )
            if scope is None or (scope["account_version"], scope["broker_version"]) != (
                current["account_version"],
                current["broker_version"],
            ):
                raise ValueError("Inconsistent account scope")
            portfolio_version = positive_version(scope["portfolio_version"])
            revision = scope["data_revision"]
            if type(revision) is not int or not 0 <= revision <= INT64_MAX:
                raise ValueError("Invalid portfolio revision")
            rejected = rejections.history(con, owner, event_id)
            retained = receipts.history(con, owner, event_id, account_id)
            if rejected is None or retained is None:
                raise ValueError("The recorded input history is unavailable")
            if any(
                not isinstance(item, rejections.RejectionRecord)
                or (item.owner_id, item.event_id) != (owner, event_id)
                or item.revision_no > event.revision_no
                or (item.revision_id == event.revision_id)
                != (item.revision_no == event.revision_no)
                for item in rejected
            ):
                raise ValueError("Inconsistent rejection scope")
            matches = [item for item in rejected if item.revision_id == event.revision_id]
            if len(matches) > 1:
                raise ValueError("Duplicate current rejection")
            return DirectReceiptInputs(
                event_id=event_id,
                revision_id=event.revision_id,
                revision_no=event.revision_no,
                workflow_status=event.review_status,
                account_id=account_id,
                portfolio_id=current["portfolio_id"],
                broker_id=current["broker_id"],
                account_status=scope["status"],
                account_version=str(positive_version(scope["account_version"])),
                portfolio_version=str(portfolio_version),
                broker_version=str(positive_version(scope["broker_version"])),
                portfolio_data_revision=str(revision),
                rejection_state="REJECTED" if matches else "NOT_REJECTED",
                rejection_id=matches[0].id if matches else None,
                receipt_basis="DIRECT_EVENT_ACCOUNT",
                scope="DIRECT_RECORDED_RECEIPT_INPUTS_ONLY",
                history_completeness="UNVERIFIED",
                current_use_scope="REVALIDATION_REQUIRED",
                eligibility_scope="NOT_ESTABLISHED",
                payment_scope="OWNER_RECORDED_NOT_VERIFIED",
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
            "DIRECT_RECEIPT_INPUTS_UNAVAILABLE",
            "The direct receipt inputs cannot be read safely",
        ) from None
