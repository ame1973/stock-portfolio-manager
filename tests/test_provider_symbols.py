"""Explicit provider aliases cover the whole requested window or no fetch is authorized."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.providers.symbols import (
    AliasPeriod,
    AliasSegment,
    FetchWindow,
    ResolutionState,
    SymbolResolution,
    resolve_periods,
    resolve_symbol,
)
from tests.helpers import create, database_snapshot, initialize

SECURITY = UUID("00000000-0000-4000-8000-000000000001")
OTHER = UUID("00000000-0000-4000-8000-000000000002")
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")


def alias(**changes: Any) -> AliasPeriod:
    return AliasPeriod.model_validate(
        {
            "id": "00000000-0000-4000-8000-000000000003",
            "security_id": str(SECURITY),
            "provider_id": "yahoo",
            "provider_symbol": "BRK-B",
            "effective_from": "2020-01-01",
            **changes,
        }
    )


def test_complete_alias_is_clipped_without_claiming_upstream_coverage() -> None:
    original = alias(effective_to="2026-02-01")
    result = resolve_periods(SECURITY, "yahoo", "US", WINDOW, [original])
    assert result.state == ResolutionState.RESOLVED
    assert len(result.segments) == 1
    assert result.segments[0].window == WINDOW
    assert result.segments[0].alias == original
    assert result.warnings == {"UNVERIFIED_PROVIDER_ALIAS"}
    assert original.effective_from == "2020-01-01"
    verified = resolve_periods(SECURITY, "yahoo", "US", WINDOW, [alias(verified=True)])
    assert verified.warnings == frozenset()


def test_alias_change_splits_windows_and_preserves_the_original_identity_versions() -> None:
    old = alias(effective_to="2026-01-15", version=3)
    new = alias(
        id="00000000-0000-4000-8000-000000000004",
        provider_symbol="BRK.NEW",
        effective_from="2026-01-16",
        version=2,
    )
    result = resolve_periods(SECURITY, "yahoo", "US", WINDOW, [new, old])
    assert result.state == ResolutionState.RESOLVED
    assert [
        (part.alias.provider_symbol, part.window.start, part.window.end, part.alias.version)
        for part in result.segments
    ] == [
        ("BRK-B", "2026-01-01", "2026-01-15", 3),
        ("BRK.NEW", "2026-01-16", "2026-01-31", 2),
    ]


@pytest.mark.parametrize(
    "periods, reason",
    [
        ([], "NO_PROVIDER_ALIAS"),
        ([alias(effective_from="2026-02-01")], "NO_PROVIDER_ALIAS"),
        ([alias(effective_from="2026-01-02")], "ALIAS_WINDOW_GAP"),
        ([alias(effective_to="2026-01-30")], "ALIAS_WINDOW_GAP"),
        (
            [
                alias(effective_to="2026-01-15"),
                alias(id="00000000-0000-4000-8000-000000000004", effective_from="2026-01-17"),
            ],
            "ALIAS_WINDOW_GAP",
        ),
    ],
)
def test_missing_head_tail_or_interior_mapping_never_returns_partial_fetch_segments(
    periods: list[AliasPeriod],
    reason: str,
) -> None:
    result = resolve_periods(SECURITY, "yahoo", "US", WINDOW, periods)
    assert result.state == ResolutionState.NOT_FOUND and result.reason == reason
    assert result.segments == ()


@pytest.mark.parametrize(
    "first_end, second_start",
    [
        (None, "2026-01-15"),
        ("2026-01-15", "2026-01-15"),
        (None, "2020-01-01"),
    ],
)
def test_own_overlaps_are_ambiguous_even_when_the_first_alias_covers_the_entire_window(
    first_end: str | None,
    second_start: str,
) -> None:
    result = resolve_periods(
        SECURITY,
        "yahoo",
        "US",
        WINDOW,
        [
            alias(effective_to=first_end),
            alias(id="00000000-0000-4000-8000-000000000004", effective_from=second_start),
        ],
    )
    assert result.state == ResolutionState.AMBIGUOUS
    assert result.reason == "OVERLAPPING_ALIASES" and result.segments == ()


def test_cross_security_symbol_collision_is_not_a_guess_or_a_partial_success() -> None:
    result = resolve_periods(
        SECURITY,
        "yahoo",
        "US",
        WINDOW,
        [
            alias(),
            alias(id="00000000-0000-4000-8000-000000000004", security_id=OTHER),
        ],
    )
    assert result.state == ResolutionState.AMBIGUOUS
    assert result.reason == "ALIAS_IDENTITY_COLLISION" and result.segments == ()


def test_other_providers_symbols_and_nonoverlapping_reuse_do_not_poison_a_resolution() -> None:
    result = resolve_periods(
        SECURITY,
        "yahoo",
        "US",
        WINDOW,
        [
            alias(),
            alias(
                id="00000000-0000-4000-8000-000000000004", security_id=OTHER, provider_id="eodhd"
            ),
            alias(
                id="00000000-0000-4000-8000-000000000005",
                security_id=OTHER,
                effective_to="2025-12-31",
            ),
            alias(
                id="00000000-0000-4000-8000-000000000006",
                security_id=OTHER,
                provider_symbol="OTHER",
            ),
        ],
    )
    assert result.state == ResolutionState.RESOLVED


def test_market_support_is_not_inferred_from_an_hk_securitys_dividend_or_quote_currency() -> None:
    result = resolve_periods(SECURITY, "alphavantage", "HK", WINDOW, [])
    assert result.state == ResolutionState.UNSUPPORTED and result.reason == "UNSUPPORTED_MARKET"
    assert result.segments == ()


@pytest.mark.parametrize(
    "start, end",
    [
        ("2024-02-29", "2024-03-01"),
        ("9999-12-31", "9999-12-31"),
    ],
)
def test_calendar_boundaries_do_not_invent_a_day_or_overflow_the_last_date(
    start: str,
    end: str,
) -> None:
    window = FetchWindow(start=start, end=end)
    result = resolve_periods(SECURITY, "yahoo", "US", window, [alias()])
    assert result.state == ResolutionState.RESOLVED and result.segments[0].window == window


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-02-30", "2026-03-01"),
        ("2026-02-01", "2026-01-31"),
        ("0000-01-01", "2026-01-01"),
        ("2026-1-1", "2026-01-31"),
    ],
)
def test_invalid_calendar_windows_are_rejected(start: str, end: str) -> None:
    with pytest.raises(ValidationError):
        FetchWindow(start=start, end=end)


def test_resolution_model_cannot_expose_unresolved_partial_segments_or_an_empty_success() -> None:
    with pytest.raises(ValidationError):
        SymbolResolution(state=ResolutionState.RESOLVED)
    with pytest.raises(ValidationError):
        SymbolResolution(
            state=ResolutionState.NOT_FOUND, segments=(AliasSegment(alias=alias(), window=WINDOW),)
        )
    with pytest.raises(ValidationError):
        alias(effective_from="2026-01-02", effective_to="2026-01-01")


@pytest.mark.anyio
@pytest.mark.parametrize("identifier", ["DIV-013", "DIV-014"])
async def test_source_dot_and_leading_zero_cases_use_real_explicit_mapping_without_writes(
    client: httpx.AsyncClient,
    engine: Engine,
    identifier: str,
) -> None:
    cases = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "packages/finance-fixtures/dividend_provider_cases.json"
        ).read_text()
    )["cases"]
    data = next(item for item in cases if item["id"] == identifier)
    await initialize(client, engine)
    ticker, exchange = data["security"].split("@")
    security = await create(
        client,
        "securities",
        {
            "exchange_code": exchange,
            "display_ticker": ticker,
            "name": "Synthetic provider identity",
            "quote_currency": "HKD" if exchange == "HKG" else "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    for provider, symbol in data["provider_symbols"].items():
        await create(
            client,
            f"securities/{security['id']}/provider-symbols",
            {
                "provider_id": provider,
                "provider_symbol": symbol,
                "effective_from": "2020-01-01",
            },
        )
    before = database_snapshot(engine)
    for provider, expected in data["provider_symbols"].items():
        with unit_of_work(engine) as con:
            result = resolve_symbol(con, UUID(security["id"]), provider, WINDOW)
        assert result.state == ResolutionState.RESOLVED
        assert result.segments[0].alias.provider_symbol == expected
        assert str(result.segments[0].alias.security_id) == security["id"]
        assert result.segments[0].exchange_timezone == (
            "Asia/Hong_Kong" if exchange == "HKG" else "America/New_York"
        )
    assert database_snapshot(engine) == before
    assert (await client.get(f"/api/v1/securities/{security['id']}")).json()[
        "display_ticker"
    ] == ticker


@pytest.mark.anyio
async def test_repository_keeps_historical_alias_windows_and_never_guesses_another_provider(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NYSE",
            "display_ticker": "SYN",
            "name": "Synthetic historical aliases",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    for symbol, first, last in [
        ("OLD", "2020-01-01", "2025-12-31"),
        ("NEW", "2026-01-01", None),
    ]:
        await create(
            client,
            f"securities/{security['id']}/provider-symbols",
            {
                "provider_id": "yahoo",
                "provider_symbol": symbol,
                "effective_from": first,
                "effective_to": last,
            },
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = resolve_symbol(
            con, UUID(security["id"]), "yahoo", FetchWindow(start="2025-12-30", end="2026-01-02")
        )
        missing = resolve_symbol(con, UUID(security["id"]), "eodhd", WINDOW)
        absent = resolve_symbol(con, OTHER, "yahoo", WINDOW)
    assert result.state == ResolutionState.RESOLVED
    assert [
        (part.alias.provider_symbol, part.window.start, part.window.end) for part in result.segments
    ] == [
        ("OLD", "2025-12-30", "2025-12-31"),
        ("NEW", "2026-01-01", "2026-01-02"),
    ]
    assert missing.state == ResolutionState.NOT_FOUND and missing.reason == "NO_PROVIDER_ALIAS"
    assert absent.state == ResolutionState.NOT_FOUND and absent.reason == "SECURITY_NOT_FOUND"
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_repository_rejects_unsupported_market_and_a_real_mapping_gap_without_writes(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Synthetic HK USD counter",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "yahoo",
            "provider_symbol": "0005.HK",
            "effective_from": "2026-01-02",
        },
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        unsupported = resolve_symbol(con, UUID(security["id"]), "alphavantage", WINDOW)
        gap = resolve_symbol(con, UUID(security["id"]), "yahoo", WINDOW)
    assert unsupported.state == ResolutionState.UNSUPPORTED
    assert gap.state == ResolutionState.NOT_FOUND and gap.reason == "ALIAS_WINDOW_GAP"
    assert database_snapshot(engine) == before
