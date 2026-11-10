"""String-only trade inputs and immutable revision responses."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints, field_validator, model_validator

from spm_api.decimal_types import (
    INT64_MAX,
    FxRate,
    IsoDate,
    Money,
    NonNegativeMoney,
    PositiveQuantity,
    Price,
)
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Currency, Note


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


class TradeAuditRecovery(Model):
    original_response: TradeRecord = Field(repr=False)
    reason: ChangeReason

    @field_validator("reason")
    @classmethod
    def no_nul_reason(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("The recovery reason cannot contain NUL")
        return value


class TradeAuditOriginRecord(Model):
    trade_id: UUID
    trade_version: int
    trade_status: Literal["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID"]
    origin: Literal["MISSING", "AT_CREATION", "BEFORE_RETIRE", "RETAINED_RESPONSE"]
    captured_at: str | None
    scope: Literal["DATABASE_AUDIT_ORIGIN_ONLY"] = "DATABASE_AUDIT_ORIGIN_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"


def _review_ordinal(value: str) -> str:
    number = int(value)
    if str(number) != value or number > INT64_MAX:
        raise ValueError("The review revision is invalid")
    return value


ReviewOrdinal = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^(0|[1-9][0-9]{0,18})$"),
    AfterValidator(_review_ordinal),
]
TradeReviewGap = Literal["MISSING_CREATION_AUDIT", "MISSING_RETIRED_ORIGIN"]
TradeReviewDecision = Literal["ACKNOWLEDGE_UNVERIFIED", "WITHDRAW"]
TradeReviewState = Literal[
    "NOT_REQUIRED", "NOT_REVIEWED", "ACKNOWLEDGED_UNVERIFIED", "WITHDRAWN", "EVIDENCE_CHANGED"
]
TradePhysicalStatus = Literal["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID"]


class TradeHistoryReviewWrite(Model):
    decision: TradeReviewDecision
    expected_revision: ReviewOrdinal
    expected_gap: TradeReviewGap
    reason: ChangeReason

    @field_validator("expected_revision")
    @classmethod
    def revision_can_advance(cls, value: str) -> str:
        if int(value) == INT64_MAX:
            raise ValueError("The review revision cannot advance")
        return value

    @field_validator("reason")
    @classmethod
    def no_nul_reason(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("The review reason cannot contain NUL")
        return value


class TradeHistoryReviewRecord(Model):
    id: UUID
    trade_id: UUID
    revision: ReviewOrdinal
    decision: TradeReviewDecision
    gap: TradeReviewGap
    observed_trade_status: TradePhysicalStatus
    observed_trade_version: int
    created_at: str
    reason: str
    scope: Literal["UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"] = "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"


class TradeHistoryReviewStatus(Model):
    trade_id: UUID
    trade_version: int
    trade_status: TradePhysicalStatus
    review_revision: ReviewOrdinal
    current_gap: TradeReviewGap | None
    state: TradeReviewState
    latest: TradeHistoryReviewRecord | None
    scope: Literal["UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"] = "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"


CorrectionReviewOrdinal = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,18}$"),
    AfterValidator(_review_ordinal),
]


class TradeCorrectionReviewPin(Model):
    trade_id: UUID
    trade_version: Annotated[int, Field(strict=True, ge=1, le=2)]
    review_revision: CorrectionReviewOrdinal
    gap: Literal["MISSING_CREATION_AUDIT"]


MAX_TRADE_CORRECTION_REVIEWS = 100


class TradeUnverifiedCorrectionScope(Model):
    confirmation: Literal["UNVERIFIED_TRADE_CORRECTION_ONLY"]
    reviews: Annotated[
        tuple[TradeCorrectionReviewPin, ...],
        Field(min_length=1, max_length=MAX_TRADE_CORRECTION_REVIEWS),
    ]

    @field_validator("reviews")
    @classmethod
    def distinct_trades(
        cls, value: tuple[TradeCorrectionReviewPin, ...]
    ) -> tuple[TradeCorrectionReviewPin, ...]:
        if len({item.trade_id for item in value}) != len(value):
            raise ValueError("Correction review trades must be distinct")
        return value


class TradeUnverifiedRevisionWrite(TradeUnverifiedCorrectionScope):
    revision: TradeRevision = Field(repr=False)

    @field_validator("revision")
    @classmethod
    def no_nul_reason(cls, value: TradeRevision) -> TradeRevision:
        if "\0" in value.reason:
            raise ValueError("The correction reason cannot contain NUL")
        return value


class TradeUnverifiedVoidWrite(TradeUnverifiedCorrectionScope):
    reason: ChangeReason = Field(repr=False)

    @field_validator("reason")
    @classmethod
    def no_nul_reason(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("The correction reason cannot contain NUL")
        return value


class TradeUnverifiedCorrectionRecord(Model):
    authorization_id: UUID
    trade: TradeRecord = Field(repr=False)
    scope: Literal["UNVERIFIED_TRADE_CORRECTION_ONLY"]
    history_completeness: Literal["UNVERIFIED"]


class TradeCorrectionScopeGap(Model):
    trade_id: UUID
    trade_version: Annotated[int, Field(strict=True, ge=1, le=2)]
    review_revision: ReviewOrdinal
    current_gap: TradeReviewGap | None
    state: Literal["NOT_REVIEWED", "ACKNOWLEDGED_UNVERIFIED", "WITHDRAWN", "EVIDENCE_CHANGED"]


class TradeCorrectionScopeStatus(Model):
    trade_id: UUID
    root_id: UUID
    trade_version: Annotated[int, Field(strict=True, ge=1, le=2)]
    trade_status: TradePhysicalStatus
    gaps: Annotated[
        tuple[TradeCorrectionScopeGap, ...], Field(max_length=MAX_TRADE_CORRECTION_REVIEWS)
    ]
    scope: Literal["UNVERIFIED_TRADE_CORRECTION_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
