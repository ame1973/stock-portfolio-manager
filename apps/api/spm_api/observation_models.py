"""Exact source-only observation views exclude private contexts and unresolved legacy payloads."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from spm_api.action_models import EvidenceUrl, JsonRatio
from spm_api.decimal_types import Dps, IsoDate
from spm_api.errors import Model
from spm_api.master_models import Currency, ProviderCode, ProviderId
from spm_api.pagination import Page
from spm_api.providers.adapters import Code
from spm_api.providers.base import (
    AdjustmentBasis,
    EventType,
    EvidenceCitation,
    OriginGroup,
    SourcePeriod,
)


class PublicCitation(EvidenceCitation):
    """Citation identity/terms remain; query-bearing document links are withheld, not rewritten."""

    url_withheld: bool = False


class _ObservationRecord(Model):
    id: UUID
    provider_run_id: UUID
    provider_id: ProviderId
    origin_group: OriginGroup
    security_id: UUID
    provider_symbol: ProviderCode
    provider_event_id: str | None
    evidence: PublicCitation | None
    normalized_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_url: EvidenceUrl | None
    retrieved_at: AwareDatetime
    sightings_count: int = Field(ge=1)
    last_seen_at: AwareDatetime
    provenance_version: Literal[1] = 1
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    canonical_adoption: Literal["NOT_ADOPTED"] = "NOT_ADOPTED"


class DividendObservationRecord(_ObservationRecord):
    ex_date: IsoDate | None
    announce_date: IsoDate | None
    record_date: IsoDate | None
    pay_date: IsoDate | None
    event_type: EventType
    dps: Dps | None
    currency: Currency | None
    source_currency: str | None
    source_period: SourcePeriod | None
    source_frequency: int | None
    adjustment_basis: AdjustmentBasis
    incomplete_reasons: tuple[Code, ...]


class SplitObservationRecord(_ObservationRecord):
    effective_date: IsoDate
    ratio_numerator: JsonRatio
    ratio_denominator: JsonRatio
    source_adjustment_type: Literal["FORWARD_SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND"] | None
    effect_scope: Literal["PROVIDER_ADJUSTMENT_ONLY"]


class ObservationPage[T: Model](Page[T]):
    legacy_records_omitted: int = Field(ge=0)
    scope: Literal["IMMUTABLE_SOURCE_V1"] = "IMMUTABLE_SOURCE_V1"
