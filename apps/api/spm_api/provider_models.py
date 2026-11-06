"""Public provider diagnostics never turn local readiness or parsed pages into coverage proof."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from spm_api.action_models import EvidenceUrl
from spm_api.errors import Model
from spm_api.job_models import SourceOperation
from spm_api.master_models import ProviderCode, ProviderId
from spm_api.providers.adapters import AdapterHealth, Code
from spm_api.providers.base import Availability, FetchState, OriginGroup, ProviderDefinition
from spm_api.providers.symbols import FetchWindow


class ProviderInfo(ProviderDefinition):
    configured: bool
    capabilities: tuple[SourceOperation, ...]
    configuration: AdapterHealth
    scope: Literal["CANDIDATE_METADATA_AND_LOCAL_CONFIGURATION"] = (
        "CANDIDATE_METADATA_AND_LOCAL_CONFIGURATION"
    )


class RecordedSourcePage(Model):
    """One completed immutable page, not an active probe or a complete-history assertion."""

    id: UUID
    security_id: UUID
    operation: SourceOperation
    state: FetchState
    provider_symbol: ProviderCode | None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: int = Field(ge=0)
    attempted: bool
    http_status: int | None = Field(ge=100, le=599)
    error_code: Code | None
    window: FetchWindow
    parse_complete: bool
    has_more: bool
    observations_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    filtered_count: int = Field(ge=0)
    source_url: EvidenceUrl | None
    scope: Literal["RECORDED_SOURCE_PAGE"] = "RECORDED_SOURCE_PAGE"


class ProviderHealth(Model):
    provider_id: ProviderId
    configured: bool
    state: FetchState
    reason: Code
    last_result: RecordedSourcePage | None
    last_success_at: AwareDatetime | None
    last_error_code: Code | None
    legacy_runs_omitted: int = Field(ge=0)
    quota_remaining: None = None
    live_validation: Literal["NOT_TESTED"] = "NOT_TESTED"
    scope: Literal["LOCAL_CONFIGURATION_AND_RECORDED_SOURCE"] = (
        "LOCAL_CONFIGURATION_AND_RECORDED_SOURCE"
    )


class ProviderCoverage(Model):
    security_id: UUID
    provider_id: ProviderId
    origin_group: OriginGroup
    provider_symbol: ProviderCode | None
    operation: SourceOperation
    status: FetchState | Literal["NOT_TESTED"]
    configuration: AdapterHealth
    supported_market: bool
    supported_operation: bool
    availability: Availability
    observations_count: int = Field(ge=0)
    last_result: RecordedSourcePage | None
    last_success_at: AwareDatetime | None
    legacy_runs_omitted: int = Field(ge=0)
    legacy_status_ignored: bool
    coverage_start: None = None
    coverage_end: None = None
    coverage_basis: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    live_validation: Literal["NOT_TESTED"] = "NOT_TESTED"
    scope: Literal["RECORDED_SOURCE_ONLY"] = "RECORDED_SOURCE_ONLY"
