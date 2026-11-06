"""Typed provider evidence, immutable capability catalogue and truthful availability."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints

from spm_api.action_models import JsonRatio
from spm_api.decimal_types import Dps, IsoDate
from spm_api.errors import Model
from spm_api.master_models import Currency, ProviderCode, ProviderId

Market = Literal["HK", "US"]
OriginGroup = Literal[
    "EODHD", "ALPHAVANTAGE", "MASSIVE", "YAHOO", "HKEX_OFFICIAL", "SEC_OFFICIAL", "USER_VERIFIED"
]
AdjustmentBasis = Literal["AS_DECLARED", "SPLIT_ADJUSTED", "UNKNOWN"]
EventType = Literal[
    "REGULAR",
    "SPECIAL",
    "INTERIM",
    "FINAL",
    "SUPPLEMENTAL",
    "ETF_DISTRIBUTION",
    "REIT_DISTRIBUTION",
    "UNKNOWN",
]


SourcePeriod = Literal[
    "ANNUAL",
    "QUARTERLY",
    "MONTHLY",
    "WEEKLY",
    "INTERIM",
    "FINAL",
    "SPECIAL",
    "SUPPLEMENTAL",
    "OTHER",
]


class FetchState(StrEnum):
    OK_WITH_DATA = "OK_WITH_DATA"
    NO_DATA = "NO_DATA"
    EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
    UNSUPPORTED = "UNSUPPORTED"
    UNCONFIGURED = "UNCONFIGURED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    STALE = "STALE"


class Availability(StrEnum):
    OBSERVATIONS_AVAILABLE = "OBSERVATIONS_AVAILABLE"
    DATA_NOT_CONFIRMED = "DATA_NOT_CONFIRMED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"


def availability(states: Iterable[FetchState]) -> Availability:
    """Classify retrieval only, never verify income or infer zero dividends."""
    observed = frozenset(states)
    if FetchState.OK_WITH_DATA in observed:
        return Availability.OBSERVATIONS_AVAILABLE
    if observed & {FetchState.NO_DATA, FetchState.EMPTY_CONFIRMED}:
        return Availability.DATA_NOT_CONFIRMED
    if observed == {FetchState.UNSUPPORTED}:
        return Availability.PROVIDER_UNSUPPORTED
    return Availability.DATA_UNAVAILABLE


class ProviderDefinition(Model):
    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    origin_group: OriginGroup
    markets: Annotated[tuple[Market, ...], Field(min_length=1, max_length=2)]
    access_mode: Literal["API_KEY", "PUBLIC_HTTP", "OPTIONAL_SDK", "MANUAL_EVIDENCE"]
    documentation_url: str | None = None
    live_validation: Literal["NOT_TESTED"] = "NOT_TESTED"
    data_rights: Literal["UNVERIFIED", "MANUAL_EVIDENCE_ONLY"]
    raw_payload_storage: Literal[False] = False
    operations: tuple[Literal["DIVIDENDS", "SPLITS"], ...] = ()
    coverage_start: IsoDate | None = None
    has_unadjusted_dividend: bool | None = Field(default=None, strict=True)


class _Catalogue(Model):
    schema_version: Literal[1]
    providers: tuple[ProviderDefinition, ...]


@cache
def registry() -> Mapping[ProviderId, ProviderDefinition]:
    """Read static metadata, not credentials or fabricated runtime health."""
    source = files("spm_api.providers").joinpath("adapter_capability.json").read_text("utf-8")
    catalogue = _Catalogue.model_validate_json(source)
    result = {item.provider_id: item for item in catalogue.providers}
    if len(result) != len(catalogue.providers) or set(result) != set(get_args(ProviderId)):
        raise ValueError("Provider catalogue must contain each supported provider exactly once")
    return MappingProxyType(result)


def independent_vendor_origins(providers: Iterable[ProviderId]) -> frozenset[OriginGroup]:
    """Official/manual evidence is not an extra vendor corroboration vote."""
    return frozenset(
        registry()[provider].origin_group
        for provider in providers
        if registry()[provider].access_mode != "MANUAL_EVIDENCE"
    )


class SourceObservation(Model):
    """Bounded immutable source identity, never a canonical financial fact."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    security_id: UUID
    provider_symbol: ProviderCode
    provider_event_id: (
        Annotated[
            str,
            StringConstraints(
                strict=True, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$"
            ),
        ]
        | None
    ) = None

    @property
    def origin_group(self) -> OriginGroup:
        return registry()[self.provider_id].origin_group

    @property
    def normalized_sha256(self) -> str:
        value = {**self.model_dump(mode="json"), "origin_group": self.origin_group}
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DividendObservation(SourceObservation):
    """One source assertion, not a canonical event or receipt."""

    ex_date: IsoDate | None = None
    announce_date: IsoDate | None = None
    record_date: IsoDate | None = None
    pay_date: IsoDate | None = None
    event_type: EventType = "UNKNOWN"
    dps: Dps | None = None
    currency: Currency | None = None
    source_currency: Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")] | None = None
    source_period: SourcePeriod | None = None
    source_frequency: int | None = Field(default=None, strict=True, ge=0, le=365)
    adjustment_basis: AdjustmentBasis = "UNKNOWN"

    @property
    def incomplete_reasons(self) -> frozenset[str]:
        reasons = set()
        for name, value in (
            ("MISSING_EX_DATE", self.ex_date),
            ("MISSING_DPS", self.dps),
            ("MISSING_CURRENCY", self.currency),
        ):
            if value is None:
                reasons.add(name)
        if self.adjustment_basis != "AS_DECLARED":
            reasons.add("UNRESOLVED_ADJUSTMENT")
        return frozenset(reasons)


class SplitObservation(SourceObservation):
    """A provider factor may include other adjustments; it never adopts ledger share terms."""

    effective_date: IsoDate
    ratio_numerator: JsonRatio
    ratio_denominator: JsonRatio
    source_adjustment_type: Literal["FORWARD_SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND"] | None = (
        None
    )
    effect_scope: Literal["PROVIDER_ADJUSTMENT_ONLY"] = "PROVIDER_ADJUSTMENT_ONLY"
