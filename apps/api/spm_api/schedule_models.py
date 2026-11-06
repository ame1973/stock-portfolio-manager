"""Explicit source schedule policies, owner intents and internal frozen timing fences."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from spm_api.decimal_types import IsoDate
from spm_api.errors import Model
from spm_api.job_models import SourceOperation
from spm_api.master_models import ChangeReason, Name, ProviderId
from spm_api.providers.base import Market, registry
from spm_api.providers.symbols import FetchWindow


class SourceSchedulePolicy(Model):
    """A bounded local week pattern, not a verified exchange-session calendar."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(default=1, strict=True, ge=1, le=1)
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    calendar_basis: Literal["LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR"] = (
        "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR"
    )
    security_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]
    provider_ids: Annotated[tuple[ProviderId, ...], Field(min_length=1, max_length=8)]
    operations: Annotated[tuple[SourceOperation, ...], Field(min_length=1, max_length=2)] = (
        "DIVIDENDS",
    )
    market: Market
    first_date: IsoDate
    local_run_time: Annotated[str, Field(strict=True, pattern=r"^(1[6-9]|2[0-3]):[0-5][0-9]$")]
    cadence: Literal["DAILY", "WEEKDAYS", "WEEKLY"]
    weekday: Annotated[int, Field(strict=True, ge=0, le=6)] | None = None
    lookback_days: Annotated[int, Field(strict=True, ge=1, le=3660)] | None = None
    lookback_months: Annotated[int, Field(strict=True, ge=1, le=120)] | None = None
    backfill_from_date: IsoDate | None = None

    @field_validator("security_ids", "provider_ids", "operations")
    @classmethod
    def canonical_sets[T: str | UUID](cls, values: tuple[T, ...]) -> tuple[T, ...]:
        return tuple(sorted(set(values), key=str))

    @model_validator(mode="after")
    def explicit_policy(self) -> "SourceSchedulePolicy":
        if (self.cadence == "WEEKLY") != (self.weekday is not None):
            raise ValueError("Only weekly schedules require an explicit weekday")
        if (self.lookback_days is None) == (self.lookback_months is None):
            raise ValueError("Exactly one recurring day or calendar-month window is required")
        if self.backfill_from_date is not None and self.backfill_from_date > self.first_date:
            raise ValueError(
                "An explicit initial backfill cannot start after the first schedule date"
            )
        if any(
            registry()[provider].access_mode == "MANUAL_EVIDENCE" for provider in self.provider_ids
        ):
            raise ValueError("Automated schedules cannot invent cited manual or official input")
        return self

    @property
    def exchange_timezone(self) -> str:
        return "Asia/Hong_Kong" if self.market == "HK" else "America/New_York"


class SourceScheduleCreate(Model):
    name: Name
    policy: SourceSchedulePolicy


class SourceScheduleUpdate(SourceScheduleCreate):
    enabled: bool = Field(strict=True)
    reason: ChangeReason


class DueSchedule(Model):
    """Internal frozen evaluation fence, not a caller-supplied scheduling API body."""

    model_config = ConfigDict(frozen=True)

    schedule_id: UUID
    owner_id: UUID
    version: int = Field(strict=True, ge=1, le=2147483647)
    expected_run_at: Annotated[AwareDatetime, Field(strict=True)]
    due_at: Annotated[AwareDatetime, Field(strict=True)]
    policy: SourceSchedulePolicy
    initial: bool = Field(strict=True)


class SourceScheduleRecord(Model):
    """Owned schedule intent and committed progress, never private profile/lease state."""

    id: UUID
    name: Name
    policy: SourceSchedulePolicy
    enabled: bool
    version: int = Field(strict=True, ge=1, le=2147483647)
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    canonical_reconciliation: Literal["NOT_RUN"] = "NOT_RUN"
    automatic_materialization_installed: bool
    exchange_timezone: Literal["Asia/Hong_Kong", "America/New_York"]
    next_run_at: AwareDatetime | None
    occurrence_count: int = Field(strict=True, ge=0, le=2147483647)
    initial_backfill_queued: bool
    last_job_id: UUID | None
    last_due_at: AwareDatetime | None
    last_materialized_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SourceScheduleOccurrence(Model):
    """Immutable scheduling receipt, not provider success or financial adoption."""

    id: UUID
    schedule_id: UUID
    sequence: int = Field(strict=True, ge=1, le=2147483647)
    schedule_version: int = Field(strict=True, ge=1, le=2147483647)
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    canonical_reconciliation: Literal["NOT_RUN"] = "NOT_RUN"
    expected_run_at: AwareDatetime
    due_at: AwareDatetime
    materialized_at: AwareDatetime
    next_run_at: AwareDatetime | None
    policy: SourceSchedulePolicy
    window: FetchWindow
    kind: Literal["REGULAR", "INITIAL_BACKFILL"]
    outcome: Literal["QUEUED", "SKIPPED_IN_FLIGHT"]
    job_id: UUID | None
    blocking_job_id: UUID | None
