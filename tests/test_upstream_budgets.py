"""Shared upstream policy never invents a plan, key grant, reset or shorter retry deadline."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from spm_api.config import Settings
from spm_api.providers.budgets import (
    BudgetMember,
    OriginLimits,
    aware,
    later,
    load_budget,
    microseconds,
    retry_deadline,
)
from spm_api.providers.configuration import ProviderConfigurationStore
from tests.test_provider_configuration import KEY, policy, provision
from tests.test_provider_configuration import local as local

AT = datetime(2026, 11, 6, tzinfo=UTC)


def test_shared_yahoo_wrappers_take_minimum_limits_without_loading_sdk(local: Settings) -> None:
    provision(local, "yahoo", body=policy("yahoo", requests_per_minute=10, requests_per_day=100))
    provision(
        local, "yfinance", body=policy("yfinance", requests_per_minute=2, requests_per_day=50)
    )
    snapshot = load_budget(ProviderConfigurationStore(local), "yahoo")
    assert snapshot.limits.origin_group == "YAHOO"
    assert snapshot.limits.requests_per_minute == 2 and snapshot.limits.requests_per_day == 50
    assert [member.provider_id for member in snapshot.limits.members] == ["yahoo", "yfinance"]
    assert snapshot.primary.provider_id == "yahoo"
    assert snapshot.unchanged(ProviderConfigurationStore(local))
    assert load_budget(ProviderConfigurationStore(local), "yfinance").limits == snapshot.limits


def test_peer_appearance_and_same_length_key_rewrite_invalidate_memory_snapshot(
    local: Settings,
) -> None:
    provision(local, "yahoo")
    store = ProviderConfigurationStore(local)
    observed = load_budget(store, "yahoo")
    provision(local, "yfinance", body=policy("yfinance", requests_per_minute=1, requests_per_day=5))
    assert not observed.unchanged(store)
    _, key = provision(local)
    observed = load_budget(store, "eodhd")
    key.write_bytes(b"changed-file-test-key!!")
    assert not observed.unchanged(store)
    assert KEY.decode() not in repr(observed)
    assert KEY.decode() not in observed.limits.model_dump_json()


def test_disabled_or_invalid_peer_cannot_grant_traffic_or_defaults(local: Settings) -> None:
    provision(local, "yahoo")
    provision(local, "yfinance", body=policy("yfinance", enabled=False))
    store = ProviderConfigurationStore(local)
    snapshot = load_budget(store, "yahoo")
    assert len(snapshot.limits.members) == 1 and snapshot.unchanged(store)
    with pytest.raises(ValueError, match="PROVIDER_UNCONFIGURED"):
        load_budget(store, "yfinance")
    provision(local, "yfinance", body={"api_key": "private"})
    assert not snapshot.unchanged(store)
    assert len(load_budget(store, "yahoo").limits.members) == 1


def test_manual_sources_and_missing_files_cannot_invent_network_budgets(local: Settings) -> None:
    store = ProviderConfigurationStore(local)
    with pytest.raises(ValueError, match="PROVIDER_UNCONFIGURED"):
        load_budget(store, "eodhd")
    with pytest.raises(ValueError, match="MANUAL_EVIDENCE_HAS_NO_NETWORK_BUDGET"):
        load_budget(store, "manual")
    assert not local.data_dir.exists() and not local.provider_secret_dir.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"requests_per_minute": 0},
        {"requests_per_day": True},
        {"requests_per_minute": 1.0},
        {"requests_per_day": 2147483648},
        {"origin_group": "HKEX_OFFICIAL"},
        {"origin_group": "YAHOO"},
        {"api_key": "private"},
        {"requests_per_minute": 3},
    ],
)
def test_limits_are_strict_origin_scoped_and_not_looser_than_members(changes: Any) -> None:
    data = {
        "origin_group": "EODHD",
        "requests_per_minute": 2,
        "requests_per_day": 5,
        "members": [
            {
                "provider_id": "eodhd",
                "profile_sha256": "a" * 64,
                "requests_per_minute": 2,
                "requests_per_day": 5,
            }
        ],
    }
    with pytest.raises(ValidationError):
        OriginLimits.model_validate({**data, **changes})


def test_duplicate_profiles_are_not_independent_limits() -> None:
    member = BudgetMember(
        provider_id="yahoo", profile_sha256="a" * 64, requests_per_minute=5, requests_per_day=10
    )
    with pytest.raises(ValidationError):
        OriginLimits(
            origin_group="YAHOO",
            requests_per_minute=5,
            requests_per_day=10,
            members=(member, member),
        )


@pytest.mark.parametrize("attempt", [1, 2, 3, 4])
def test_retry_exponential_floor_and_bounded_integer_jitter(attempt: int) -> None:
    base = 1000 * 2 ** (attempt - 1)
    assert retry_deadline(AT, attempt, None, jitter_ms=0) == AT + timedelta(milliseconds=base)
    assert retry_deadline(AT, attempt, None, jitter_ms=base) == AT + timedelta(
        milliseconds=base * 2
    )
    assert retry_deadline(AT, attempt, None) >= AT + timedelta(milliseconds=base)


def test_absolute_retry_after_is_not_clamped_and_offsets_stay_exact() -> None:
    deadline = "9999-12-31T23:59:59.999999Z"
    assert retry_deadline(AT, 1, deadline, jitter_ms=0) == datetime.max.replace(tzinfo=UTC)
    assert retry_deadline(AT, 1, "2026-11-06T08:00:10+08:00", jitter_ms=0) == AT + timedelta(
        seconds=10
    )
    assert retry_deadline(
        datetime.max.replace(tzinfo=UTC), 4, None, jitter_ms=8000
    ) == datetime.max.replace(tzinfo=UTC)
    assert later(datetime.min.replace(tzinfo=UTC), -timedelta(days=1)) == datetime.min.replace(
        tzinfo=UTC
    )
    assert aware(AT) == AT and microseconds(timedelta(days=1, microseconds=1)) == 86400000001


@pytest.mark.parametrize(
    "attempt,jitter", [(0, 0), (5, 0), (True, 0), (1, True), (1, -1), (1, 1001)]
)
def test_retry_bounds_and_naive_times_are_not_coerced(attempt: Any, jitter: Any) -> None:
    with pytest.raises(ValueError):
        retry_deadline(AT, attempt, None, jitter_ms=jitter)
    with pytest.raises(ValueError):
        retry_deadline(AT.replace(tzinfo=None), 1, None)
    with pytest.raises(ValueError):
        retry_deadline(AT, 1, "2026-11-06T00:00:10")
