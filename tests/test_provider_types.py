"""Provider contracts use synthetic source cases, never live credentials or income guesses."""

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from spm_api.providers.base import (
    Availability,
    DividendObservation,
    FetchState,
    availability,
    independent_vendor_origins,
    registry,
)

CASES: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "packages/finance-fixtures/dividend_provider_cases.json"
    ).read_text()
)["cases"]
SECURITY = UUID("00000000-0000-4000-8000-000000000001")


def case(identifier: str) -> dict[str, Any]:
    return next(item for item in CASES if item["id"] == identifier)


def observation(**changes: Any) -> DividendObservation:
    return DividendObservation.model_validate(
        {
            "provider_id": "eodhd",
            "security_id": str(SECURITY),
            "provider_symbol": "0005.HK",
            "ex_date": "2026-01-02",
            "dps": "0.3",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            **changes,
        }
    )


def test_capability_catalogue_is_frozen_metadata_not_live_availability() -> None:
    providers = registry()
    assert isinstance(providers, MappingProxyType)
    assert len(providers) == 8
    assert providers["yahoo"].origin_group == providers["yfinance"].origin_group == "YAHOO"
    assert providers["yfinance"].access_mode == "OPTIONAL_SDK"
    for definition in providers.values():
        assert definition.live_validation == "NOT_TESTED"
        assert definition.raw_payload_storage is False
        assert "configured" not in definition.model_dump()
    with pytest.raises(ValidationError):
        providers["yahoo"].origin_group = "EODHD"
    assert registry() is providers


@pytest.mark.parametrize("identifier", ["DIV-001", "DIV-002"])
def test_source_cases_count_independent_vendor_origins_only(identifier: str) -> None:
    data = case(identifier)
    origins = independent_vendor_origins(row["provider"] for row in data["observations"])
    assert len(origins) == data["expected"]["independent_groups"]
    assert "PROVISIONAL" not in origins and "CORROBORATED" not in origins


def test_manual_and_official_references_are_not_extra_vendor_votes() -> None:
    assert independent_vendor_origins(["manual", "hkex_evidence", "sec_evidence"]) == frozenset()
    assert independent_vendor_origins(["yahoo", "yfinance", "manual", "hkex_evidence"]) == {"YAHOO"}


@pytest.mark.parametrize("identifier", ["DIV-007", "DIV-008", "DIV-015"])
def test_source_availability_cases_never_infer_zero_dividends(identifier: str) -> None:
    data = case(identifier)
    states = [FetchState(row["state"]) for row in data["fetches"]]
    assert availability(states).value == data["expected"]["global_state"]


@pytest.mark.parametrize(
    "state",
    [
        FetchState.UNCONFIGURED,
        FetchState.RATE_LIMITED,
        FetchState.AUTH_ERROR,
        FetchState.TRANSIENT_ERROR,
        FetchState.PARSE_ERROR,
        FetchState.STALE,
    ],
)
def test_failures_do_not_mean_confirmed_empty_income(state: FetchState) -> None:
    assert availability([state]) == Availability.DATA_UNAVAILABLE
    assert availability([state, FetchState.NO_DATA]) == Availability.DATA_NOT_CONFIRMED
    assert availability([state, FetchState.OK_WITH_DATA]) == Availability.OBSERVATIONS_AVAILABLE


def test_empty_evidence_requires_its_window_before_any_global_empty_claim() -> None:
    assert availability([]) == Availability.DATA_UNAVAILABLE
    assert availability([FetchState.EMPTY_CONFIRMED]) == Availability.DATA_NOT_CONFIRMED


def test_rate_limit_source_case_is_not_reclassified_as_no_data() -> None:
    data = case("DIV-009")
    state = FetchState(data["fetches"][0]["state"])
    assert state == FetchState.RATE_LIMITED
    assert availability([state]) == Availability.DATA_UNAVAILABLE
    assert data["expected"]["preserve_existing_events"] is True


def test_dps_is_an_exact_canonical_string_and_missing_is_not_zero() -> None:
    assert observation(dps="0000.30000000").dps == "0.30000000"
    assert observation(dps="92233720368.54775807").dps == "92233720368.54775807"
    assert observation(dps="0").dps == "0.00000000"
    missing = observation(dps=None)
    assert missing.dps is None and "MISSING_DPS" in missing.incomplete_reasons
    assert missing.normalized_sha256 != observation(dps="0").normalized_sha256


@pytest.mark.parametrize(
    "value",
    [
        0.3,
        1,
        True,
        "NaN",
        "Infinity",
        "1e3",
        "-1",
        "0.000000001",
        "92233720368.54775808",
    ],
)
def test_provider_evidence_rejects_float_bool_notation_precision_and_overflow(
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        observation(dps=value)


def test_div_012_event_currency_is_independent_of_quote_currency() -> None:
    data = case("DIV-012")
    event = observation(dps=data["event"]["dps"], currency=data["event"]["currency"])
    assert data["quote_currency"] == "HKD"
    assert event.currency == data["expected"]["dividend_currency"] == "USD"
    assert observation(currency=None).currency is None
    with pytest.raises(ValidationError):
        observation(currency="EUR")
    with pytest.raises(ValidationError):
        observation(quote_currency="HKD")


def test_unknown_adjustment_cannot_become_as_declared_without_evidence() -> None:
    data = case("DIV-006")["observations"][0]
    item = observation(
        provider_id=data["provider"], dps=data["dps"], adjustment_basis=data["basis"]
    )
    assert item.dps == "0.03000000"
    assert "UNRESOLVED_ADJUSTMENT" in item.incomplete_reasons
    assert "UNRESOLVED_ADJUSTMENT" in observation(adjustment_basis="UNKNOWN").incomplete_reasons
    assert observation().incomplete_reasons == frozenset()


def test_same_ex_date_regular_and_special_observations_remain_distinct() -> None:
    data = case("DIV-004")
    items = [
        observation(dps=row["dps"], event_type=row["event_type"]) for row in data["observations"]
    ]
    assert len({item.normalized_sha256 for item in items}) == data["expected"]["event_count"]
    assert (
        observation(event_type="REGULAR").normalized_sha256
        != observation(event_type="SPECIAL").normalized_sha256
    )


def test_div_017_corrected_provider_event_keeps_two_normalized_versions() -> None:
    rows = case("DIV-017")["observations"]
    old = observation(provider_event_id=rows[0]["provider_event_id"], dps=rows[0]["dps"])
    new = observation(provider_event_id=rows[1]["provider_event_id"], dps=rows[1]["dps"])
    assert old.provider_event_id == new.provider_event_id == "EVT1"
    assert old.normalized_sha256 != new.normalized_sha256
    assert (
        observation(dps="000.4", provider_event_id="EVT1").normalized_sha256
        == old.normalized_sha256
    )
    for field, value in {"dps": "0.45000000", "currency": "HKD", "pay_date": "2026-01-03"}.items():
        with pytest.raises(ValidationError):
            setattr(old, field, value)
    assert old.dps == "0.40000000"


def test_div_018_missing_pay_date_does_not_fill_today_or_claim_received() -> None:
    data = case("DIV-018")["observations"][0]
    item = observation(
        provider_id=data["provider"], ex_date=data["ex_date"], pay_date=data["pay_date"]
    )
    assert item.pay_date is None
    assert "receipt_state" not in item.model_dump()
    assert "review_status" not in item.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"ex_date": "2026-02-30"},
        {"pay_date": "2026-01-02T00:00:00Z"},
        {"provider_id": "unknown"},
        {"provider_symbol": "0005.HK?api_token=private"},
        {"provider_event_id": "bad\nidentity"},
        {"origin_group": "FAKE_INDEPENDENT_ORIGIN"},
        {"raw_payload": "not permitted"},
    ],
)
def test_observation_boundary_rejects_invalid_identity_and_private_or_unbounded_fields(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        observation(**changes)
