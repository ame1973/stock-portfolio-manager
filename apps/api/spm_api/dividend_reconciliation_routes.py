"""Owner-scoped recorded reconciliation, never an economic or payment grant."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_reconciliation_history as history
from spm_api.db import unit_of_work
from spm_api.dividend_reconciliation_models import RecordedReconciliation
from spm_api.errors import ApiError, Error
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}",
    tags=["Recorded dividend reconciliation"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


def _amount(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("Invalid recorded money")
    return format(value, "f")


def _ordinal(value: object) -> str:
    if type(value) is not int:
        raise ValueError("Invalid recorded ordinal")
    return str(value)


@router.get("/reconciliation", operation_id="get_recorded_dividend_reconciliation")
def reconciliation(
    event_id: UUID, account_id: UUID, request: Request, auth: AuthDep
) -> RecordedReconciliation:
    try:
        with unit_of_work(request.app.state.engine) as con:
            record = history.read(con, auth.owner.id, event_id, account_id)
            if record is None:
                raise ApiError(404, "NOT_FOUND", "Dividend reconciliation scope not found")
            if not isinstance(record, history.RecordedReconciliation) or (
                record.event_id,
                record.account_id,
            ) != (event_id, account_id):
                raise ValueError("Inconsistent recorded reconciliation scope")
            data = asdict(record)
            for name in ("recorded_expected", "recorded_received", "recorded_outstanding"):
                data[name] = _amount(data[name])
            for name in (
                "active_receipts",
                "void_receipts",
                "unverified_origins",
                "unavailable_cash_links",
            ):
                data[name] = _ordinal(data[name])
            if record.projection_no is not None:
                data["projection_no"] = _ordinal(record.projection_no)
            if record.projection_as_of is not None:
                if type(record.projection_as_of) is not date:
                    raise ValueError("Invalid projection date")
                data["projection_as_of"] = record.projection_as_of.isoformat()
            return RecordedReconciliation.model_validate(data)
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
            "DIVIDEND_RECONCILIATION_UNAVAILABLE",
            "The recorded reconciliation cannot be read safely",
        ) from None
