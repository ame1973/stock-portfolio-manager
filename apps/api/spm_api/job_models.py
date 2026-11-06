"""Sealed source-work plans and fenced lease tokens; no arbitrary job payloads or credentials."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from spm_api.errors import Model
from spm_api.master_models import ProviderId
from spm_api.providers.symbols import FetchWindow

SourceOperation = Literal["DIVIDENDS", "SPLITS"]
ProfileHash = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class ProviderSelection(Model):
    """Server-derived non-secret profile identity; null cannot become enabled after enqueue."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    profile_sha256: ProfileHash | None


class SourceJobPlan(Model):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    security_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]
    providers: Annotated[tuple[ProviderSelection, ...], Field(min_length=1, max_length=8)]
    window: FetchWindow
    operations: Annotated[tuple[SourceOperation, ...], Field(min_length=1, max_length=2)] = (
        "DIVIDENDS",
        "SPLITS",
    )
    force: bool = Field(default=False, strict=True)

    @field_validator("security_ids")
    @classmethod
    def securities_as_set(cls, values: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return tuple(sorted(set(values), key=str))

    @field_validator("providers")
    @classmethod
    def providers_as_set(
        cls, values: tuple[ProviderSelection, ...]
    ) -> tuple[ProviderSelection, ...]:
        if len({item.provider_id for item in values}) != len(values):
            raise ValueError("A provider cannot have competing configuration snapshots")
        return tuple(sorted(values, key=lambda item: item.provider_id))

    @field_validator("operations")
    @classmethod
    def operations_as_set(cls, values: tuple[SourceOperation, ...]) -> tuple[SourceOperation, ...]:
        return tuple(sorted(set(values)))


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    PARTIAL = "PARTIAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class ItemStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class WorkError(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    UNSUPPORTED = "UNSUPPORTED"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    SYMBOL_AMBIGUOUS = "SYMBOL_AMBIGUOUS"
    ALIAS_CHANGED = "ALIAS_CHANGED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PARSE_ERROR = "PARSE_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    QUOTA_WAIT = "QUOTA_WAIT"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
    WORKER_ERROR = "WORKER_ERROR"
    PAGE_LIMIT = "PAGE_LIMIT"
    CURSOR_CYCLE = "CURSOR_CYCLE"


@dataclass(frozen=True)
class JobLease:
    job_id: UUID
    owner_id: UUID
    worker_id: UUID
    generation: int
    expires_at: datetime
