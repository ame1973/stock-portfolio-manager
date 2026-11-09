"""Decode immutable trade rows without changing their stored financial values."""

from sqlalchemy import RowMapping

from spm_api.decimal_types import from_scaled
from spm_api.trade_models import TradeRecord


def as_trade(row: RowMapping) -> TradeRecord:
    values = {key: row[key] for key in TradeRecord.model_fields if key in row}
    for field, column, scale in (
        ("quantity", "quantity_e8", 8),
        ("price", "price_e8", 8),
        ("fee", "fee_micro", 6),
        ("tax", "tax_micro", 6),
        ("fx_rate_hkd", "fx_rate_hkd_e12", 12),
        ("gross", "gross_micro", 6),
        ("settlement", "settlement_micro", 6),
        ("booked_hkd", "booked_hkd_micro", 6),
        ("rounding_delta", "rounding_delta_micro", 6),
    ):
        values[field] = from_scaled(row[column], scale) if row[column] is not None else None
    values["settlement_override"] = bool(row["settlement_override"])
    values["hkd_override"] = bool(row["hkd_override"])
    return TradeRecord.model_validate(values)
