"""Private, audited dividend projection-history reads without current-use authority."""

import re
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_projection_history as history
import spm_api.dividend_projection_models as models
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.errors import ApiError, Error
from spm_api.pagination import Page, PageDep, paginate
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections",
    tags=["Dividend projection history"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503,
        "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE",
        "The projection history cannot be read safely",
    )


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _public(
    con: Connection, owner: UUID, record: history.RetainedProjection
) -> models.ProjectionRecord:
    version = record.version
    event = events.get(con, owner, version.event_id, version=version.revision_no)
    if event is None or event.record.revision_id != version.revision_id:
        raise ValueError("The retained event revision is unavailable")
    ex_date, currency = event.record.terms.ex_date, event.record.terms.currency
    if ex_date is None or currency is None:
        raise ValueError("The retained event terms are incomplete")
    accrual = version.accrual
    return models.ProjectionRecord.model_validate(
        {
            "id": version.id,
            "event_id": version.event_id,
            "revision_id": version.revision_id,
            "revision_no": version.revision_no,
            "account_id": version.account_id,
            "portfolio_id": record.portfolio_id,
            "broker_id": record.broker_id,
            "account_version": str(record.account_version),
            "broker_version": str(record.broker_version),
            "portfolio_data_revision": str(record.portfolio_data_revision),
            "projection_no": str(version.projection_no),
            "previous_id": version.previous_id,
            "state": version.state,
            "as_of": record.as_of.isoformat(),
            "ex_date": ex_date,
            "timing_scope": (
                "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY"
                if record.as_of.isoformat() < ex_date
                else "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY"
            ),
            "currency": currency,
            "eligibility_rule": record.eligibility_rule,
            "retired_trade_scope": record.retired_trade_scope,
            "creation_audit_scope": record.creation_audit_scope,
            "recorded_shares": _decimal(record.eligible_shares),
            "estimated_amounts": None
            if accrual is None
            else {
                "currency": accrual.currency,
                "gross_local": _decimal(accrual.gross_local),
                "tax_local": _decimal(accrual.tax_local),
                "fee_local": _decimal(accrual.fee_local),
                "net_local": _decimal(accrual.net_local),
                "rounding_delta_local": _decimal(accrual.rounding_delta_local),
                "estimated_hkd": _decimal(accrual.booked_hkd),
                "quality": accrual.quality,
            },
            "tax_rate": _decimal(record.tax_rate),
            "fee_original": _decimal(record.fee_original),
            "fee_currency": record.fee_currency,
            "fee_rate_event": _decimal(record.fee_rate_event),
            "fee_rate_source": record.fee_rate_source,
            "fx_rate_hkd": _decimal(record.fx_rate_hkd),
            "fx_source": record.fx_source,
            "tax_provenance": _provenance(record.tax_provenance),
            "fee_provenance": _provenance(record.fee_provenance),
            "created_at": record.created_at,
            "reason": record.reason,
            "history_completeness": record.history_completeness,
            "scope": record.scope,
            "current_use_scope": "REVALIDATION_REQUIRED",
            "eligibility_scope": "NOT_ESTABLISHED",
        }
    )


def _provenance(
    value: history.RetainedTaxProvenance | history.RetainedFeeProvenance | None,
) -> models.ProjectionProvenance | None:
    if value is None:
        return None
    return models.ProjectionProvenance(
        effective_on=value.effective_on.isoformat(),
        selected_setting_id=value.selected_setting_id,
        observed_head_no=str(value.observed_head_no),
        observed_head_id=value.observed_head_id,
        status=value.status,
        scope=value.scope,
    )


@router.get("", operation_id="list_dividend_projection_history")
def projection_history(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep, page: PageDep
) -> Page[models.ProjectionRecord]:
    cursor = page.cursor_value
    if cursor is not None and (
        re.fullmatch(r"[1-9][0-9]{0,18}", cursor) is None or int(cursor) > INT64_MAX
    ):
        raise ApiError(422, "INVALID_CURSOR", "The projection history cursor is invalid")
    try:
        with unit_of_work(request.app.state.engine) as con:
            retained = history.history(con, auth.owner.id, event_id, account_id)
            if retained is None:
                raise ApiError(404, "NOT_FOUND", "Dividend projection scope not found")
            records = {str(record.version.id): record for record in retained}
            rows = (
                con.execute(
                    text(r"""
                    SELECT id,projection_no FROM dividend_projection_versions
                    WHERE owner_id=:owner AND event_id=:event AND account_id=:account
                      AND (id LIKE :search ESCAPE '\'
                        OR CAST(projection_no AS TEXT) LIKE :search ESCAPE '\')
                      AND (:cursor_value IS NULL OR (projection_no,id)<(:cursor_value,:cursor_id))
                    ORDER BY projection_no DESC,id DESC LIMIT :limit
                """),
                    {
                        **page.params(),
                        "owner": str(auth.owner.id),
                        "event": str(event_id),
                        "account": str(account_id),
                        "cursor_value": int(cursor) if cursor is not None else None,
                    },
                )
                .mappings()
                .all()
            )
            return paginate(
                rows,
                page,
                "projection_no",
                lambda row: _public(con, auth.owner.id, records[row["id"]]),
            )
    except (history.ProjectionHistoryError, events.EventError):
        raise _unavailable() from None
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None
