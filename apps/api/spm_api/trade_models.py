"""String-only trade inputs and immutable revision responses."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney, PositiveQuantity, Price
from spm_api.errors import Model
from spm_api.master_models import Currency


def _execution(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Use a timezone-aware RFC3339 execution timestamp") from exc


ExecutionTime = Annotated[
    str,
    Field(
        strict=True,
        max_length=40,
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$",
        json_schema_extra={"format": "date-time"},
    ),
    AfterValidator(_execution),
]
ChangeReason = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=10, max_length=1000)
]
Note = Annotated[str, Field(strict=True, max_length=1000)]


class TradeCreate(Model):
    portfolio_id: UUID
    account_id: UUID
    security_id: UUID
    side: Literal["BUY", "SELL"]
    trade_date: IsoDate
    executed_at_utc: ExecutionTime | None = None
    quantity: PositiveQuantity
    price: Price
    fee: NonNegativeMoney = "0.000000"
    tax: NonNegativeMoney = "0.000000"
    currency: Currency
    fx_rate_hkd: FxRate | None = None
    fx_source: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
    ] = "MANUAL"
    actual_settlement: Money | None = None
    actual_hkd: Money | None = None
    override_reason: Note = ""
    note: Note = ""

    @model_validator(mode="after")
    def actual_evidence(self) -> "TradeCreate":
        if (self.actual_settlement is not None or self.actual_hkd is not None) and len(
            self.override_reason.strip()
        ) < 10:
            raise ValueError("Actual settlement overrides require an evidence explanation")
        return self


class TradeRevision(TradeCreate):
    reason: ChangeReason


class VoidTrade(Model):
    reason: ChangeReason


class TradeRecord(Model):
    id: UUID
    root_id: UUID
    portfolio_id: UUID
    account_id: UUID
    security_id: UUID
    side: Literal["BUY", "SELL"]
    trade_date: IsoDate
    executed_at_utc: ExecutionTime | None
    quantity: PositiveQuantity
    price: Price
    fee: NonNegativeMoney
    tax: NonNegativeMoney
    currency: Currency
    fx_rate_hkd: FxRate | None
    fx_source: str | None
    gross: Money
    settlement: Money
    booked_hkd: Money | None
    rounding_delta: Money
    settlement_override: bool
    hkd_override: bool
    override_reason: str
    status: Literal["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID"]
    revision: int
    version: int
    supersedes_id: UUID | None
    created_sequence: int
    note: str
    created_at: str
    updated_at: str
