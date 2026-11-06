"""Server-derived shared-origin limits and exact retry policy, without HTTP or implicit defaults."""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from spm_api.audit import canonical, digest
from spm_api.errors import Model
from spm_api.job_models import ProfileHash
from spm_api.master_models import ProviderId
from spm_api.providers.base import registry
from spm_api.providers.configuration import LoadedConfiguration, ProviderConfigurationStore

NetworkOrigin = Literal["EODHD", "ALPHAVANTAGE", "MASSIVE", "YAHOO"]
TOKEN_UNITS = 60_000_000
MAX_TIME = datetime.max.replace(tzinfo=UTC)


class BudgetMember(Model):
    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    profile_sha256: ProfileHash
    requests_per_minute: int = Field(strict=True, ge=1, le=2147483647)
    requests_per_day: int = Field(strict=True, ge=1, le=2147483647)


class OriginLimits(Model):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    origin_group: NetworkOrigin
    requests_per_minute: int = Field(strict=True, ge=1, le=2147483647)
    requests_per_day: int = Field(strict=True, ge=1, le=2147483647)
    members: Annotated[tuple[BudgetMember, ...], Field(min_length=1, max_length=2)]

    @field_validator("members")
    @classmethod
    def ordered(cls, values: tuple[BudgetMember, ...]) -> tuple[BudgetMember, ...]:
        if len({member.provider_id for member in values}) != len(values):
            raise ValueError("A budget cannot contain duplicate profiles")
        return tuple(sorted(values, key=lambda member: member.provider_id))

    @model_validator(mode="after")
    def one_upstream(self) -> Self:
        if any(
            registry()[member.provider_id].origin_group != self.origin_group
            for member in self.members
        ):
            raise ValueError("Budget profiles must share the same actual upstream")
        if self.requests_per_minute != min(
            value.requests_per_minute for value in self.members
        ) or self.requests_per_day != min(value.requests_per_day for value in self.members):
            raise ValueError("Shared-origin limits must use the most conservative enabled profiles")
        return self

    @property
    def sha256(self) -> str:
        return digest(canonical(self.model_dump(mode="json")))


@dataclass(frozen=True)
class BudgetSnapshot:
    """Keep loaded keys in memory only; public persisted policy is limits, not this snapshot."""

    provider_id: ProviderId
    limits: OriginLimits
    configurations: tuple[LoadedConfiguration, ...] = field(repr=False)

    @property
    def primary(self) -> LoadedConfiguration:
        return next(value for value in self.configurations if value.provider_id == self.provider_id)

    def unchanged(self, store: ProviderConfigurationStore) -> bool:
        for observed in self.configurations:
            if observed.error_code is None:
                if not store.unchanged(observed):
                    return False
            else:
                current = store.load(observed.provider_id)
                if current.error_code != observed.error_code or current.profile != observed.profile:
                    return False
        return True


def load_budget(store: ProviderConfigurationStore, provider_id: ProviderId) -> BudgetSnapshot:
    definition = registry()[provider_id]
    if definition.access_mode == "MANUAL_EVIDENCE":
        raise ValueError("MANUAL_EVIDENCE_HAS_NO_NETWORK_BUDGET")
    configurations = tuple(
        store.load(key)
        for key, value in registry().items()
        if value.origin_group == definition.origin_group
    )
    primary = next(value for value in configurations if value.provider_id == provider_id)
    if primary.error_code is not None:
        raise ValueError("PROVIDER_UNCONFIGURED")
    members = []
    minutes = []
    days = []
    for value in configurations:
        if value.error_code is not None:
            continue
        profile, revision = value.profile, value.revision
        if (
            profile is None
            or revision is None
            or not profile.enabled
            or not profile.terms_acknowledged
            or profile.requests_per_minute is None
            or profile.requests_per_day is None
        ):
            raise ValueError("NETWORK_BUDGET_REQUIRED")
        members.append(
            BudgetMember(
                provider_id=value.provider_id,
                profile_sha256=revision.profile_sha256,
                requests_per_minute=profile.requests_per_minute,
                requests_per_day=profile.requests_per_day,
            )
        )
        minutes.append(profile.requests_per_minute)
        days.append(profile.requests_per_day)
    limits = OriginLimits.model_validate(
        {
            "origin_group": definition.origin_group,
            "requests_per_minute": min(minutes),
            "requests_per_day": min(days),
            "members": members,
        }
    )
    return BudgetSnapshot(provider_id, limits, configurations)


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_UPSTREAM_TIME_REQUIRED")
    return value.astimezone(UTC)


def later(value: datetime, delta: timedelta) -> datetime:
    try:
        return aware(value) + delta
    except OverflowError:
        return MAX_TIME if delta >= timedelta(0) else datetime.min.replace(tzinfo=UTC)


def microseconds(delta: timedelta) -> int:
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def retry_deadline(
    at: datetime, logical_attempt: int, retry_at: str | None, *, jitter_ms: int | None = None
) -> datetime:
    """Exponential floor plus bounded jitter; an upstream absolute deadline is never truncated."""
    current = aware(at)
    if type(logical_attempt) is not int or not 1 <= logical_attempt <= 4:
        raise ValueError("INVALID_UPSTREAM_ATTEMPT")
    base = 1000 * (2 ** (logical_attempt - 1))
    jitter = secrets.randbelow(base + 1) if jitter_ms is None else jitter_ms
    if type(jitter) is not int or not 0 <= jitter <= base:
        raise ValueError("INVALID_RETRY_JITTER")
    result = later(current, timedelta(milliseconds=base + jitter))
    if retry_at is not None:
        result = max(result, aware(datetime.fromisoformat(retry_at)))
    return result
