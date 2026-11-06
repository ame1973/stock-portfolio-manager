"""Public source-sync contracts exclude credentials and private execution plans."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from spm_api.decimal_types import IsoDate
from spm_api.errors import Model
from spm_api.job_models import ItemStatus, JobStatus, SourceOperation, WorkError
from spm_api.master_models import ProviderId
from spm_api.providers.symbols import FetchWindow


class SyncDividendRequest(Model):
    security_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]
    from_date: IsoDate
    to_date: IsoDate
    provider_ids: Annotated[tuple[ProviderId, ...], Field(min_length=1, max_length=8)]
    operations: Annotated[tuple[SourceOperation, ...], Field(min_length=1, max_length=2)] = (
        "DIVIDENDS",
    )
    force: bool = Field(default=False, strict=True)

    @field_validator("security_ids", "provider_ids", "operations")
    @classmethod
    def canonical_set[T: str | UUID](cls, values: tuple[T, ...]) -> tuple[T, ...]:
        return tuple(sorted(set(values), key=str))

    @model_validator(mode="after")
    def ordered(self) -> "SyncDividendRequest":
        FetchWindow(start=self.from_date, end=self.to_date)
        return self


class JobCreated(Model):
    job_id: UUID
    status: Literal["QUEUED"] = "QUEUED"
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    canonical_reconciliation: Literal["NOT_RUN"] = "NOT_RUN"


class SourceJobResult(Model):
    scope: Literal["SOURCE_ONLY"]
    provider_attempts: int = Field(ge=0)
    provider_attempts_scope: Literal["RESERVED_OR_RECORDED_HTTP"]
    provider_http_recorded: int = Field(ge=0)
    provider_attempts_unresolved: int = Field(ge=0)
    scanned_symbols: int = Field(ge=0, le=100)
    work_units: int = Field(ge=1, le=1600)
    work_units_done: int = Field(ge=0, le=1600)
    observations_new: int = Field(ge=0)
    pages_committed: int = Field(ge=0)
    failures: int = Field(ge=0, le=1600)
    skipped_unsupported: int = Field(ge=0, le=1600)


class SourceJob(Model):
    id: UUID
    kind: Literal["DIVIDEND_SOURCE_SYNC"]
    status: JobStatus
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    canonical_reconciliation: Literal["NOT_RUN"] = "NOT_RUN"
    progress_done: int = Field(ge=0, le=100)
    progress_total: int = Field(ge=1, le=100)
    attempt_count: int = Field(ge=0, le=4)
    version: int = Field(ge=1)
    last_error_code: WorkError | None
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    next_run_at: AwareDatetime | None
    result: SourceJobResult


class SourceJobItem(Model):
    id: UUID
    security_id: UUID
    provider_id: ProviderId
    operation: SourceOperation
    ordinal: int = Field(ge=0, le=1599)
    status: ItemStatus
    attempt_count: int = Field(ge=0, le=4)
    next_run_at: AwareDatetime | None
    last_error_code: WorkError | None


class CancelJobRequest(Model):
    """An optional empty body rejects accidental credential or financial inputs."""
