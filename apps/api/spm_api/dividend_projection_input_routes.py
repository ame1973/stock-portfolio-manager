"""Read one private projection-input snapshot without granting current financial use."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_fee_service as fees
import spm_api.dividend_projection_history as projections
import spm_api.dividend_projection_input_models as models
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_tax_service as tax
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.dividend_account_scope import account
from spm_api.dividend_eligibility import EligibilityRule
from spm_api.errors import ApiError, Error
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/projection-inputs",
    tags=["Dividend projection inputs"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _selection(
    selected: tax.TaxSelection | fees.FeeSelection, current: RowMapping
) -> models.ProjectionInputSelection:
    item = selected.selected
    matched = item is not None and (
        str(item.portfolio_id),
        str(item.broker_id),
        item.account_version,
        item.broker_version,
    ) == (
        current["portfolio_id"],
        current["broker_id"],
        current["account_version"],
        current["broker_version"],
    )
    return models.ProjectionInputSelection(
        effective_on=selected.effective_on.isoformat(),
        head_no=str(selected.head_no),
        head_id=selected.head_id,
        selected_id=item.id if item is not None else None,
        status=selected.status,
        selected_scope=(
            "NO_SELECTED_SETTING"
            if item is None
            else "ACCOUNT_SCOPE_MATCH"
            if matched
            else "ACCOUNT_SCOPE_CHANGED"
        ),
        scope="OWNER_ESTIMATE_ONLY",
    )


@router.get("", operation_id="get_dividend_projection_inputs")
def projection_inputs(
    event_id: UUID,
    account_id: UUID,
    request: Request,
    auth: AuthDep,
    as_of: IsoDate,
    eligibility_rule: EligibilityRule,
    tax_effective_on: IsoDate,
    fee_effective_on: IsoDate,
) -> models.ProjectionInputs:
    try:
        with unit_of_work(request.app.state.engine) as con:
            owner = auth.owner.id
            reviewed = events.get(con, owner, event_id)
            if reviewed is None:
                raise ApiError(404, "NOT_FOUND", "Dividend projection scope not found")
            current = account(con, owner, account_id)
            if current is None:
                raise ApiError(404, "NOT_FOUND", "Dividend projection scope not found")
            event = reviewed.record
            if event.terms.ex_date is None or event.terms.currency is None:
                raise ValueError("The event terms are incomplete")
            revision = con.execute(
                text("SELECT data_revision FROM portfolios WHERE id=:id AND owner_id=:owner"),
                {"id": current["portfolio_id"], "owner": str(owner)},
            ).scalar_one_or_none()
            if type(revision) is not int or not 0 <= revision <= INT64_MAX:
                raise ValueError("The portfolio revision is unavailable")
            rejected = rejections.history(con, owner, event_id)
            tax_settings = tax.history(
                con, owner, event_id, event.revision_id, event.revision_no, account_id
            )
            fee_settings = fees.history(
                con, owner, event_id, event.revision_id, event.revision_no, account_id
            )
            retained = projections.history(con, owner, event_id, account_id)
            if rejected is None or tax_settings is None or fee_settings is None or retained is None:
                raise ValueError("The recorded input history is unavailable")
            selected_tax = tax.select(tax_settings, date.fromisoformat(tax_effective_on))
            selected_fee = fees.select(fee_settings, date.fromisoformat(fee_effective_on))
            head = retained[-1].version if retained else None
            return models.ProjectionInputs(
                event_id=event_id,
                revision_id=event.revision_id,
                revision_no=event.revision_no,
                ex_date=event.terms.ex_date,
                currency=event.terms.currency,
                account_id=account_id,
                portfolio_id=current["portfolio_id"],
                broker_id=current["broker_id"],
                account_version=str(current["account_version"]),
                broker_version=str(current["broker_version"]),
                portfolio_data_revision=str(revision),
                projection_head_no=str(head.projection_no if head else 0),
                projection_head_id=head.id if head else None,
                as_of=as_of,
                eligibility_rule=eligibility_rule,
                tax=_selection(selected_tax, current),
                fee=_selection(selected_fee, current),
                rejection_state=(
                    "REJECTED"
                    if any(item.revision_id == event.revision_id for item in rejected)
                    else "NOT_REJECTED"
                ),
                scope="RECORDED_PROJECTION_INPUTS_ONLY",
                current_use_scope="REVALIDATION_REQUIRED",
                source_scope="CURRENT_SOURCE_NOT_REVALIDATED",
                eligibility_scope="NOT_ESTABLISHED",
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
            "DIVIDEND_PROJECTION_INPUTS_UNAVAILABLE",
            "The projection inputs cannot be read safely",
        ) from None
