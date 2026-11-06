"""Alpha/Massive mock-HTTP contracts: explicit errors, scoped pages and unverified evidence."""

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.providers.adapters import ProviderAdapter
from spm_api.providers.alphavantage import AlphaVantageAdapter
from spm_api.providers.base import FetchState
from spm_api.providers.http import ProviderAccess, ProviderHTTP
from spm_api.providers.massive import MassiveAdapter
from spm_api.providers.symbols import AliasPeriod, AliasSegment, FetchWindow, resolve_symbol
from tests.helpers import create, database_snapshot, initialize

pytestmark = pytest.mark.anyio
KEY = "synthetic-vendor-test-key"
SID = UUID("00000000-0000-4000-8000-000000000001")
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")


def segment(provider: Any, market: Any = "US", **changes: Any) -> AliasSegment:
    alias = AliasPeriod.model_validate(
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "security_id": SID,
            "provider_id": provider,
            "provider_symbol": "BRK.B",
            "effective_from": "2020-01-01",
            "verified": True,
            **changes,
        }
    )
    return AliasSegment(alias=alias, window=WINDOW, market=market)


def access() -> ProviderAccess:
    return ProviderAccess(enabled=True, terms_acknowledged=True, credential=SecretStr(KEY))


def adapter(transport: ProviderHTTP) -> ProviderAdapter:
    return (
        AlphaVantageAdapter(transport)
        if transport.provider_id == "alphavantage"
        else MassiveAdapter(transport)
    )


def response(payload: Any, status: int = 200) -> httpx.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.Response(
        status, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body)
    )


def alpha_row(**changes: Any) -> dict[str, Any]:
    return {"ex_dividend_date": "2026-01-08", "amount": "0.9", "payment_date": None, **changes}


def massive_row(**changes: Any) -> dict[str, Any]:
    return {
        "ticker": "BRK.B",
        "id": "opaque-event-1",
        "ex_dividend_date": "2026-01-08",
        "cash_amount": "0.9",
        "split_adjusted_cash_amount": "0.6",
        "currency": "USD",
        "distribution_type": "recurring",
        "frequency": 4,
        "pay_date": None,
        "historical_adjustment_factor": "0.000001",
        **changes,
    }


@pytest.mark.parametrize("provider", ["alphavantage", "massive"])
async def test_paid_adapters_remain_unconfigured_and_health_never_spends_quota(
    provider: Any,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response([])

    async with ProviderHTTP(
        provider, ProviderAccess(), transport=httpx.MockTransport(handle)
    ) as client:
        source = adapter(client)
        health = await source.healthcheck()
        missing = await source.fetch_dividends(segment(provider))
        caps = source.get_capabilities()
    assert health.state == missing.state == FetchState.UNCONFIGURED
    assert health.reason == missing.error_code == "NO_API_KEY" and not health.configured
    assert health.scope == "CONFIGURATION_ONLY" and health.quota_remaining is None
    assert health.last_success_at is None and calls == []
    assert caps.coverage_start is None and caps.live_validation == "NOT_TESTED"
    assert caps.raw_payload_storage is False and "DIVIDENDS" in caps.operations
    async with ProviderHTTP(provider, access(), transport=httpx.MockTransport(handle)) as client:
        health = await adapter(client).healthcheck()
    assert health.configured and health.state == FetchState.STALE and health.reason == "NOT_PROBED"
    assert calls == []


@pytest.mark.parametrize("provider", ["alphavantage", "massive"])
@pytest.mark.parametrize(
    "market,reason", [(None, "MARKET_CONTEXT_REQUIRED"), ("HK", "UNSUPPORTED_MARKET")]
)
async def test_us_adapters_do_not_treat_hk_usd_counters_or_unknown_markets_as_us(
    provider: Any,
    market: Any,
    reason: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unsupported market reached HTTP")

    async with ProviderHTTP(provider, access(), transport=httpx.MockTransport(handle)) as client:
        result = await adapter(client).fetch_dividends(segment(provider, market))
    assert result.state == FetchState.UNSUPPORTED and result.error_code == reason


@pytest.mark.parametrize("provider", ["alphavantage", "massive"])
async def test_other_provider_aliases_never_reach_a_vendor(provider: Any) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Wrong alias reached HTTP")

    async with ProviderHTTP(provider, access(), transport=httpx.MockTransport(handle)) as client:
        result = await adapter(client).fetch_dividends(segment("yahoo"))
    assert result.state == FetchState.UNSUPPORTED and result.error_code == "ALIAS_PROVIDER_MISMATCH"


@pytest.mark.parametrize("provider", ["alphavantage", "massive"])
@pytest.mark.parametrize(
    "status,state",
    [
        (401, FetchState.AUTH_ERROR),
        (429, FetchState.RATE_LIMITED),
        (503, FetchState.TRANSIENT_ERROR),
    ],
)
async def test_transport_failures_are_not_reinterpreted_as_no_dividends(
    provider: Any, status: int, state: FetchState
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response({}, status)

    async with ProviderHTTP(provider, access(), transport=httpx.MockTransport(handle)) as client:
        result = await adapter(client).fetch_dividends(segment(provider))
    assert result.state == state and result.rows == () and len(calls) == 1
    assert result.http_status == status and not result.complete


async def test_alpha_filters_history_without_guessing_currency() -> None:
    payload = {
        "symbol": "BRK.B",
        "data": [
            alpha_row(ex_dividend_date="2025-12-31"),
            alpha_row(),
            alpha_row(ex_dividend_date="2026-02-01"),
        ],
    }
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(payload)

    async with ProviderHTTP(
        "alphavantage", access(), transport=httpx.MockTransport(handle)
    ) as client:
        result = await AlphaVantageAdapter(client).fetch_dividends(segment("alphavantage"))
    assert len(calls) == 1 and calls[0].url.path == "/query"
    assert (
        calls[0].url.params["function"] == "DIVIDENDS" and calls[0].url.params["datatype"] == "json"
    )
    assert calls[0].url.params["symbol"] == "BRK.B" and calls[0].url.params["apikey"] == KEY
    assert result.state == FetchState.OK_WITH_DATA and result.complete and len(result.rows) == 1
    assert result.filtered_count == 2 and result.issue_count == 0
    event = result.rows[0]
    assert event.dps == "0.90000000" and event.currency is None and event.source_currency is None
    assert event.adjustment_basis == "UNKNOWN" and event.pay_date is None
    assert "ALPHA_ADJUSTMENT_BASIS_UNDOCUMENTED" in result.warnings
    assert (
        "MISSING_CURRENCY" in result.warnings
        and "OUTSIDE_MAPPING_WINDOW_IGNORED" in result.warnings
    )
    assert (
        result.source_url == "https://www.alphavantage.co/query"
        and KEY not in result.model_dump_json()
    )


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"amount": None, "dividend_per_share": "0.68"}, "0.68000000"),
        ({"amount": "0", "dividend_per_share": "0.00000000"}, "0.00000000"),
        ({"amount": "0.9", "dividend_per_share": "0.9000000000"}, "0.90000000"),
        ({"amount": None, "dividend_per_share": None}, None),
    ],
)
async def test_alpha_known_compatibility_amounts_are_exact_not_silently_conflicting(
    changes: dict[str, Any], expected: str | None
) -> None:
    async with ProviderHTTP(
        "alphavantage",
        access(),
        transport=httpx.MockTransport(
            lambda _: response({"symbol": "BRK.B", "data": [alpha_row(**changes)]})
        ),
    ) as client:
        result = await AlphaVantageAdapter(client).fetch_dividends(segment("alphavantage"))
    assert result.state == FetchState.OK_WITH_DATA and result.rows[0].dps == expected


async def test_alpha_conflicting_amount_fields_remain_partial_not_a_chosen_income() -> None:
    async with ProviderHTTP(
        "alphavantage",
        access(),
        transport=httpx.MockTransport(
            lambda _: response(
                {"symbol": "BRK.B", "data": [alpha_row(), alpha_row(dividend_per_share="0.8")]}
            )
        ),
    ) as client:
        result = await AlphaVantageAdapter(client).fetch_dividends(segment("alphavantage"))
    assert len(result.rows) == 1 and result.issue_count == 1 and not result.complete
    assert result.rows[0].dps == "0.90000000"


@pytest.mark.parametrize(
    "payload,state,code",
    [
        (
            {"Note": f"Your API key {KEY} exceeds request limit of 25 requests per day"},
            FetchState.RATE_LIMITED,
            "PROVIDER_RATE_LIMITED",
        ),
        (
            {"Information": f"Your API key {KEY} requires premium entitlement"},
            FetchState.AUTH_ERROR,
            "PROVIDER_PERMISSION_ERROR",
        ),
        (
            {"Error Message": "Unsupported symbol not found"},
            FetchState.UNSUPPORTED,
            "PROVIDER_SYMBOL_UNSUPPORTED",
        ),
        ({"Error Message": "Invalid API call"}, FetchState.PARSE_ERROR, "PROVIDER_ERROR_RESPONSE"),
        ({"Note": None}, FetchState.PARSE_ERROR, "PROVIDER_ERROR_RESPONSE"),
    ],
)
async def test_alpha_http_200_business_errors_are_sanitized_never_empty_success(
    payload: Any, state: FetchState, code: str
) -> None:
    async with ProviderHTTP(
        "alphavantage", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as client:
        result = await AlphaVantageAdapter(client).fetch_dividends(segment("alphavantage"))
    assert result.state == state and result.error_code == code and result.rows == ()
    assert result.http_status == 200 and not result.complete
    assert KEY not in result.model_dump_json() and result.raw_sha256 is not None


@pytest.mark.parametrize(
    "payload,reason",
    [
        ([], "INVALID_RESPONSE_SHAPE"),
        ({"symbol": "BRK-B", "data": []}, "RESPONSE_SYMBOL_MISMATCH"),
        ({"data": []}, "RESPONSE_SYMBOL_MISMATCH"),
        (
            {"symbol": "BRK.B", "data": [], "next_url": "https://evil.invalid/"},
            "UNDOCUMENTED_PAGINATION",
        ),
        ({"symbol": "BRK.B", "data": None}, "INVALID_RESPONSE_SHAPE"),
    ],
)
async def test_alpha_does_not_guess_changed_identities_or_pagination(
    payload: Any, reason: str
) -> None:
    async with ProviderHTTP(
        "alphavantage", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as client:
        result = await AlphaVantageAdapter(client).fetch_dividends(segment("alphavantage"))
    assert result.state == FetchState.PARSE_ERROR and result.error_code == reason


async def test_alpha_empty_history_is_not_zero_and_unproven_splits_are_not_invented() -> None:
    bodies = iter(
        [
            {"symbol": "BRK.B", "data": []},
            {"symbol": "BRK.B", "data": [alpha_row(ex_dividend_date="2020-01-01")]},
        ]
    )
    async with ProviderHTTP(
        "alphavantage", access(), transport=httpx.MockTransport(lambda _: response(next(bodies)))
    ) as client:
        source = AlphaVantageAdapter(client)
        empty = await source.fetch_dividends(segment("alphavantage"))
        outside = await source.fetch_dividends(segment("alphavantage"))
        split = await source.fetch_splits(segment("alphavantage"))
        cursor = await source.fetch_dividends(segment("alphavantage"), "not-documented")
    assert empty.state == outside.state == FetchState.NO_DATA and outside.filtered_count == 1
    assert split.state == FetchState.UNSUPPORTED and cursor.state == FetchState.PARSE_ERROR
    assert "SPLITS" not in source.get_capabilities().operations


async def test_massive_v1_cash_fields_and_query_filters_ignore_the_historical_price_factor() -> (
    None
):
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response({"status": "OK", "request_id": 123, "results": [massive_row()]})

    async with ProviderHTTP("massive", access(), transport=httpx.MockTransport(handle)) as client:
        result = await MassiveAdapter(client).fetch_dividends(segment("massive"))
    sent = calls[0]
    assert len(calls) == 1 and sent.url.path == "/stocks/v1/dividends"
    assert sent.headers["authorization"] == f"Bearer {KEY}" and KEY not in str(sent.url)
    assert sent.url.params["ticker"] == "BRK.B" and sent.url.params["limit"] == "1000"
    assert (
        sent.url.params["ex_dividend_date.gte"] == WINDOW.start
        and sent.url.params["ex_dividend_date.lte"] == WINDOW.end
    )
    assert sent.url.params["sort"] == "ex_dividend_date.asc"
    event = result.rows[0]
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    assert (
        event.dps == "0.90000000"
        and event.adjustment_basis == "UNKNOWN"
        and event.currency == "USD"
    )
    assert event.provider_event_id == "opaque-event-1" and event.event_type == "REGULAR"
    assert (
        event.source_frequency == 4
        and event.source_period == "QUARTERLY"
        and event.pay_date is None
    )
    assert "ORIGINAL_CASH_BASIS_UNRESOLVED" in result.warnings


async def test_massive_one_page_per_call_follows_only_sanitized_continuations() -> None:
    bodies = iter(
        [
            {
                "status": "OK",
                "results": [massive_row()],
                "next_url": "https://api.massive.com/stocks/v1/dividends?cursor=next%3Dpage&apiKey=upstream-private-key",
            },
            {
                "status": "OK",
                "results": [massive_row(id="opaque-event-2", ex_dividend_date="2026-01-09")],
            },
        ]
    )
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(next(bodies))

    async with ProviderHTTP("massive", access(), transport=httpx.MockTransport(handle)) as client:
        source = MassiveAdapter(client)
        first = await source.fetch_dividends(segment("massive"))
        assert len(calls) == 1
        assert first.next_cursor == "/stocks/v1/dividends?cursor=next%3Dpage"
        assert "upstream-private-key" not in first.model_dump_json()
        second = await source.fetch_dividends(segment("massive"), first.next_cursor)
    assert len(calls) == 2 and calls[1].url.params["cursor"] == "next=page"
    assert "apikey" not in {key.casefold() for key in calls[1].url.params}
    assert second.next_cursor is None and first.complete and second.complete
    assert first.rows[0].normalized_sha256 != second.rows[0].normalized_sha256


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.invalid/stocks/v1/dividends?cursor=x",
        "https://api.massive.com/stocks/v1/splits?cursor=x",
        "http://api.massive.com/stocks/v1/dividends?cursor=x",
        "https://user:secret@api.massive.com/stocks/v1/dividends?cursor=x",
        "/stocks/v1/dividends?ticker=OTHER&cursor=x",
        "/stocks/v1/dividends?ticker.any_of=BRK.B,OTHER&cursor=x",
        "/stocks/v1/dividends?ex_dividend_date.gte=2020-01-01&cursor=x",
        "/stocks/v1/dividends?cursor=" + KEY,
        "",
        123,
        {},
    ],
)
async def test_bad_massive_continuations_keep_current_evidence_explicitly_partial(
    next_url: Any,
) -> None:
    payload = {"status": "OK", "results": [massive_row()], "next_url": next_url}
    async with ProviderHTTP(
        "massive", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as client:
        result = await MassiveAdapter(client).fetch_dividends(segment("massive"))
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1
    assert not result.complete and result.issue_count == 1 and result.next_cursor is None
    assert "PAGINATION_REJECTED" in result.warnings and KEY not in result.model_dump_json()


async def test_massive_bad_or_repeated_cursors_never_loop_or_change_operation() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(
            {
                "status": "OK",
                "results": [massive_row()],
                "next_url": "https://api.massive.com/stocks/v1/dividends?cursor=one",
            }
        )

    async with ProviderHTTP("massive", access(), transport=httpx.MockTransport(handle)) as client:
        source = MassiveAdapter(client)
        repeat = await source.fetch_dividends(segment("massive"), "/stocks/v1/dividends?cursor=one")
        invalid = await source.fetch_dividends(segment("massive"), "/stocks/v1/splits?cursor=other")
    assert len(calls) == 1 and repeat.next_cursor is None and not repeat.complete
    assert invalid.state == FetchState.PARSE_ERROR and invalid.error_code == "PAGINATION_REJECTED"


@pytest.mark.parametrize(
    "status,state",
    [
        ("NOT_AUTHORIZED", FetchState.AUTH_ERROR),
        ("RATE_LIMITED", FetchState.RATE_LIMITED),
        ("NOT_FOUND", FetchState.UNSUPPORTED),
        ("ERROR", FetchState.PARSE_ERROR),
        (None, FetchState.PARSE_ERROR),
        ([], FetchState.PARSE_ERROR),
        ({}, FetchState.PARSE_ERROR),
    ],
)
async def test_massive_body_status_is_not_unconditionally_http_200_success(
    status: Any, state: FetchState
) -> None:
    async with ProviderHTTP(
        "massive",
        access(),
        transport=httpx.MockTransport(
            lambda _: response({"status": status, "results": [massive_row()]})
        ),
    ) as client:
        result = await MassiveAdapter(client).fetch_dividends(segment("massive"))
    assert result.state == state and result.rows == () and not result.complete


@pytest.mark.parametrize(
    "changes,expected,basis",
    [
        ({"cash_amount": "0", "split_adjusted_cash_amount": "9"}, "0.00000000", "UNKNOWN"),
        ({"cash_amount": None}, "0.60000000", "SPLIT_ADJUSTED"),
        ({"cash_amount": None, "split_adjusted_cash_amount": None}, None, "UNKNOWN"),
    ],
)
async def test_massive_known_zero_and_missing_cash_never_become_adjusted_or_guessed(
    changes: Any, expected: str | None, basis: str
) -> None:
    async with ProviderHTTP(
        "massive",
        access(),
        transport=httpx.MockTransport(
            lambda _: response({"status": "OK", "results": [massive_row(**changes)]})
        ),
    ) as client:
        result = await MassiveAdapter(client).fetch_dividends(segment("massive"))
    assert result.rows[0].dps == expected and result.rows[0].adjustment_basis == basis


@pytest.mark.parametrize(
    "changes",
    [
        {"ticker": "OTHER"},
        {"id": 123},
        {"frequency": True},
        {"frequency": 366},
        {"cash_amount": "0.000000001"},
        {"pay_date": "2026-02-30"},
        {"ex_dividend_date": "2025-12-31"},
    ],
)
async def test_massive_bad_identity_amount_or_date_is_not_mixed_into_good_rows(
    changes: Any,
) -> None:
    async with ProviderHTTP(
        "massive",
        access(),
        transport=httpx.MockTransport(
            lambda _: response(
                {
                    "status": "OK",
                    "results": [massive_row(), massive_row(**{"id": "bad", **changes})],
                }
            )
        ),
    ) as client:
        result = await MassiveAdapter(client).fetch_dividends(segment("massive"))
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1
    assert not result.complete and result.issue_count == 1


async def test_massive_unsupported_currency_and_frequency_corrections_remain_distinct() -> None:
    payload = {
        "status": "OK",
        "results": [
            massive_row(currency="CAD", frequency=2),
            massive_row(currency="EUR", frequency=24),
        ],
    }
    async with ProviderHTTP(
        "massive", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as client:
        result = await MassiveAdapter(client).fetch_dividends(segment("massive"))
    assert len(result.rows) == 2 and all(item.currency is None for item in result.rows)
    assert [item.source_currency for item in result.rows] == ["CAD", "EUR"]
    assert [item.source_frequency for item in result.rows] == [2, 24]
    assert result.rows[0].normalized_sha256 != result.rows[1].normalized_sha256


@pytest.mark.parametrize(
    "kind,new,old,expected",
    [
        ("forward_split", "3", "2", (3, 2)),
        ("reverse_split", "1", "4", (1, 4)),
        ("stock_dividend", "1.5", "1", (3, 2)),
        ("unknown", "3", "2", (3, 2)),
    ],
)
async def test_massive_uses_individual_new_old_terms_not_cumulative_historical_factor(
    kind: str, new: str, old: str, expected: tuple[int, int]
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(
            {
                "status": "OK",
                "results": [
                    {
                        "id": "split-1",
                        "ticker": "BRK.B",
                        "execution_date": "2026-01-08",
                        "split_to": new,
                        "split_from": old,
                        "adjustment_type": kind,
                        "historical_adjustment_factor": "999",
                    }
                ],
            }
        )

    async with ProviderHTTP("massive", access(), transport=httpx.MockTransport(handle)) as client:
        result = await MassiveAdapter(client).fetch_splits(segment("massive"))
    assert calls[0].url.path == "/stocks/v1/splits"
    assert calls[0].url.params["execution_date.gte"] == WINDOW.start
    factor = result.rows[0]
    assert (factor.ratio_numerator, factor.ratio_denominator) == expected
    assert factor.effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    if kind == "stock_dividend":
        assert factor.source_adjustment_type == "STOCK_DIVIDEND"
        assert "STOCK_DIVIDEND_REQUIRES_REVIEW" in result.warnings


async def test_massive_invalid_shape_empty_and_factor_direction_conflict_remain_explicit() -> None:
    payloads = iter(
        [
            [],
            {"status": "OK", "results": None},
            {"status": "OK", "results": []},
            {
                "status": "OK",
                "results": [
                    {
                        "id": "factor",
                        "ticker": "BRK.B",
                        "execution_date": "2026-01-08",
                        "split_to": "1",
                        "split_from": "4",
                        "adjustment_type": "forward_split",
                    }
                ],
            },
        ]
    )
    async with ProviderHTTP(
        "massive", access(), transport=httpx.MockTransport(lambda _: response(next(payloads)))
    ) as client:
        source = MassiveAdapter(client)
        bad = await source.fetch_dividends(segment("massive"))
        invalid_rows = await source.fetch_dividends(segment("massive"))
        empty = await source.fetch_dividends(segment("massive"))
        factor = await source.fetch_splits(segment("massive"))
    assert bad.state == invalid_rows.state == FetchState.PARSE_ERROR
    assert empty.state == FetchState.NO_DATA and empty.complete
    assert "PROVIDER_FACTOR_DIRECTION_CONFLICT" in factor.warnings
    assert factor.rows[0].source_adjustment_type == "FORWARD_SPLIT"


@pytest.mark.parametrize("provider", ["alphavantage", "massive"])
async def test_vendor_fetch_success_and_failure_leave_the_real_ledger_unchanged(
    client: httpx.AsyncClient,
    engine: Engine,
    provider: Any,
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic vendor isolation"})
    broker = await create(client, "brokers", {"name": "Synthetic vendor broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic vendor cash",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-01",
            "opening_fx_rate_hkd": "7.8",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NYSE",
            "display_ticker": "BRK.B",
            "name": "Synthetic vendor security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": provider,
            "provider_symbol": "BRK.B",
            "effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        "trades",
        {
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "security_id": security["id"],
            "side": "BUY",
            "trade_date": "2026-01-02",
            "quantity": "10",
            "price": "10",
            "currency": "USD",
            "fx_rate_hkd": "7.8",
        },
    )
    with unit_of_work(engine) as con:
        resolved = resolve_symbol(con, UUID(security["id"]), provider, WINDOW)
    assert resolved.segments[0].market == "US"
    before = database_snapshot(engine)
    payload = (
        {"symbol": "BRK.B", "data": [alpha_row()]}
        if provider == "alphavantage"
        else {"status": "OK", "results": [massive_row()]}
    )
    replies = iter([(payload, 200), ({}, 429)])
    async with ProviderHTTP(
        provider, access(), transport=httpx.MockTransport(lambda _: response(*next(replies)))
    ) as transport:
        source = adapter(transport)
        success = await source.fetch_dividends(resolved.segments[0])
        failure = await source.fetch_dividends(resolved.segments[0])
    assert success.state == FetchState.OK_WITH_DATA and failure.state == FetchState.RATE_LIMITED
    assert success.rows[0].adjustment_basis == "UNKNOWN"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "first,second",
    [(None, "1"), ("0", "1"), ("1", "0"), (True, "1"), ("-1", "1"), ("9223372036854775808", "1")],
)
async def test_massive_invalid_individual_factors_are_not_replaced_with_price_adjustments(
    first: Any,
    second: Any,
) -> None:
    payload = {
        "status": "OK",
        "results": [
            {
                "id": "bad-factor",
                "ticker": "BRK.B",
                "execution_date": "2026-01-08",
                "split_to": first,
                "split_from": second,
                "historical_adjustment_factor": "0.5",
            }
        ],
    }
    async with ProviderHTTP(
        "massive", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await MassiveAdapter(transport).fetch_splits(segment("massive"))
    assert result.state == FetchState.PARSE_ERROR and result.rows == () and not result.complete


async def test_continuation_validation_is_pure_and_never_returns_a_secret_query() -> None:
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response([]))
    ) as transport:
        safe = transport.pagination_reference(
            "https://eodhd.com/api/div/0005.HK?cursor=next&api_token=other",
            expected_path="/api/div/0005.HK",
        )
        assert safe == "/api/div/0005.HK?cursor=next&fmt=json" and KEY not in safe
    async with ProviderHTTP(
        "manual", ProviderAccess(), transport=httpx.MockTransport(lambda _: response([]))
    ) as transport:
        with pytest.raises(ValueError, match="pagination"):
            transport.pagination_reference("/private", expected_path="/private")


async def test_query_order_cannot_disguise_an_immediate_pagination_loop() -> None:
    payload = {
        "status": "OK",
        "results": [massive_row()],
        "next_url": "https://api.massive.com/stocks/v1/dividends?limit=1000&cursor=one",
    }
    async with ProviderHTTP(
        "massive", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await MassiveAdapter(transport).fetch_dividends(
            segment("massive"), "/stocks/v1/dividends?cursor=one&limit=1000"
        )
    assert result.next_cursor is None and not result.complete
    assert "PAGINATION_REJECTED" in result.warnings


@pytest.mark.parametrize("factory", [AlphaVantageAdapter, MassiveAdapter])
async def test_adapters_never_reuse_another_vendors_transport(factory: Any) -> None:
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response([]))
    ) as client:
        with pytest.raises(ValueError, match="fixed-origin"):
            factory(client)
