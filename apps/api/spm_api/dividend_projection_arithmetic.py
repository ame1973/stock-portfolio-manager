"""Recheck supplied dividend projection arithmetic without granting financial authority."""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, DecimalException
from uuid import UUID

from spm_api.decimal_types import from_scaled
from spm_api.dividend_event_service import EventRevision
from spm_api.dividend_math import Accrual, accrue
from spm_api.dividend_projection import ProjectionError

_AMOUNT_FIELDS = (
    "eligible_shares_e8",
    "gross_micro",
    "tax_micro",
    "fee_micro",
    "expected_net_micro",
    "rounding_delta_micro",
    "fx_rate_hkd_e12",
    "fx_source",
    "expected_hkd_micro",
    "tax_rate_snapshot_ppm",
    "fee_original_micro",
    "fee_original_currency",
    "fee_rate_event_e12",
    "fee_rate_source",
    "quality",
)


def _decimal(value: object, scale: int) -> Decimal:
    if type(value) is not int:
        raise ValueError
    return Decimal(from_scaled(value, scale))


def _source(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 100
        and "\x00" not in value
        and value == value.strip()
    )


def checked_accrual(row: Mapping[str, object], event: EventRevision) -> Accrual | None:
    """Recompute supplied amounts, without verifying history, eligibility or ownership."""
    try:
        if not isinstance(row, Mapping) or not isinstance(event, EventRevision):
            raise ValueError
        event = EventRevision.model_validate(event.model_dump(warnings="error"))
        account = row["account_id"]
        if not isinstance(account, str) or str(UUID(account)) != account:
            raise ValueError
        if (
            row["owner_id"] != str(event.owner_id)
            or row["event_id"] != str(event.event_id)
            or row["revision_id"] != str(event.revision_id)
            or row["currency"] != event.terms.currency
            or row["currency"] not in ("HKD", "USD")
            or row["eligibility_rule"] not in ("ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW")
        ):
            raise ValueError
        state = row["state"]
        if state not in ("EXPECTED", "NO_POSITION", "NEEDS_REVIEW", "CANCELLED"):
            raise ValueError
        if state != "EXPECTED":
            if any(row[name] is not None for name in _AMOUNT_FIELDS):
                raise ValueError
            return None
        if (
            row["eligibility_rule"] != "ORDINARY_PRE_EX_DATE"
            or event.review_status not in ("PROVISIONAL", "CORROBORATED", "VERIFIED")
            or event.terms.adjustment_basis != "AS_DECLARED"
            or event.terms.dps is None
        ):
            raise ValueError
        currency = row["currency"]
        quantity = _decimal(row["eligible_shares_e8"], 8)
        tax_rate = _decimal(row["tax_rate_snapshot_ppm"], 6)
        original_fee = _decimal(row["fee_original_micro"], 6)
        fee = _decimal(row["fee_micro"], 6)
        fee_currency = row["fee_original_currency"]
        if quantity <= 0 or original_fee < 0 or fee < 0 or fee_currency not in ("HKD", "USD"):
            raise ValueError
        fee_rate_raw, fee_source = row["fee_rate_event_e12"], row["fee_rate_source"]
        if fee_currency == currency:
            if type(fee_rate_raw) is not int or fee_rate_raw != 1_000_000_000_000:
                raise ValueError
            if fee_source != "IDENTITY":
                raise ValueError
        elif fee_rate_raw is None:
            if fee_source is not None or original_fee != 0 or fee != 0:
                raise ValueError
        elif not _source(fee_source) or fee_source == "IDENTITY":
            raise ValueError
        fee_rate = _decimal(fee_rate_raw, 12) if fee_rate_raw is not None else None
        fx_raw, fx_source = row["fx_rate_hkd_e12"], row["fx_source"]
        if currency == "HKD":
            if type(fx_raw) is not int or fx_raw != 1_000_000_000_000 or fx_source != "IDENTITY":
                raise ValueError
        elif fx_raw is None:
            if fx_source is not None:
                raise ValueError
        elif not _source(fx_source) or fx_source == "IDENTITY":
            raise ValueError
        fx = _decimal(fx_raw, 12) if fx_raw is not None else None
        result = accrue(
            str(event.event_id),
            account,
            currency,
            quantity,
            Decimal(event.terms.dps),
            tax_rate,
            original_fee,
            fx,
            fee_currency=fee_currency,
            fee_rate_event=fee_rate,
        )
        fields = {
            "gross_micro": result.gross_local,
            "tax_micro": result.tax_local,
            "fee_micro": result.fee_local,
            "expected_net_micro": result.net_local,
            "rounding_delta_micro": result.rounding_delta_local,
        }
        if any(_decimal(row[name], 6) != value for name, value in fields.items()):
            raise ValueError
        stored_hkd = row["expected_hkd_micro"]
        if (None if stored_hkd is None else _decimal(stored_hkd, 6)) != result.booked_hkd:
            raise ValueError
        quality = row["quality"]
        if quality not in ("OK", "MISSING_QUOTE", "MISSING_FX", "NOT_COMPUTABLE", "NEEDS_REVIEW"):
            raise ValueError
        if (
            (result.quality == "NEEDS_REVIEW" and quality != "NEEDS_REVIEW")
            or (result.quality == "MISSING_FX" and quality not in ("MISSING_FX", "NEEDS_REVIEW"))
            or (result.booked_hkd is not None and quality == "MISSING_FX")
        ):
            raise ValueError
        return replace(result, quality=quality)
    except (KeyError, TypeError, ValueError, OverflowError, DecimalException):
        raise ProjectionError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None
