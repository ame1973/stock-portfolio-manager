"""Typed adapter pages distinguish parsed source evidence from financial verification."""

from typing import Annotated, Literal, Protocol, Self, TypedDict

from pydantic import ConfigDict, Field, model_validator

from spm_api.errors import Model
from spm_api.master_models import ProviderId
from spm_api.providers.base import (
    DividendObservation,
    FetchState,
    ProviderDefinition,
    SourceObservation,
    SplitObservation,
)
from spm_api.providers.symbols import AliasSegment, FetchWindow

Code = Annotated[str, Field(strict=True, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")]


class PageMetadata(TypedDict):
    window: FetchWindow
    raw_sha256: str | None
    source_url: str | None
    http_status: int | None
    retry_at: str | None


class FetchPage[T: SourceObservation](Model):
    """Complete means this page was processed, not income/coverage or pagination completion."""

    model_config = ConfigDict(frozen=True)

    state: FetchState
    window: FetchWindow
    rows: tuple[T, ...] = ()
    complete: bool = Field(default=False, strict=True)
    issue_count: int = Field(default=0, strict=True, ge=0)
    filtered_count: int = Field(default=0, strict=True, ge=0)
    warnings: Annotated[tuple[Code, ...], Field(max_length=32)] = ()
    next_cursor: str | None = Field(default=None, strict=True, max_length=4096, repr=False)
    raw_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    source_url: Annotated[str, Field(strict=True, max_length=2048)] | None = None
    http_status: int | None = Field(default=None, strict=True, ge=100, le=599)
    error_code: Code | None = None
    retry_at: Annotated[str, Field(strict=True, max_length=40)] | None = None

    @model_validator(mode="after")
    def truthful_result(self) -> Self:
        if bool(self.rows) != (self.state == FetchState.OK_WITH_DATA):
            raise ValueError("Only a data-bearing source result may contain observations")
        if self.complete and (
            self.issue_count or self.state not in {FetchState.OK_WITH_DATA, FetchState.NO_DATA}
        ):
            raise ValueError("A failed or partially malformed page cannot be complete")
        return self


class AdapterHealth(Model):
    """Local configuration diagnostics do not spend quota or pretend to probe an upstream."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    configured: bool
    state: FetchState
    reason: Code
    scope: Literal["CONFIGURATION_ONLY"] = "CONFIGURATION_ONLY"
    live_validation: Literal["NOT_TESTED"] = "NOT_TESTED"
    last_success_at: None = None
    quota_remaining: None = None


class ProviderAdapter(Protocol):
    def get_capabilities(self) -> ProviderDefinition: ...

    async def healthcheck(self) -> AdapterHealth: ...

    async def fetch_dividends(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[DividendObservation]: ...

    async def fetch_splits(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[SplitObservation]: ...
