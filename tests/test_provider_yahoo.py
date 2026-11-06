"""Yahoo mock-HTTP compatibility: exact event tokens and exchange-local identity boundaries."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.providers.adapters import ProviderAdapter
from spm_api.providers.base import FetchState, independent_vendor_origins
from spm_api.providers.http import ProviderAccess, ProviderHTTP
from spm_api.providers.symbols import AliasPeriod, AliasSegment, FetchWindow, resolve_symbol
from spm_api.providers.yahoo import YahooAdapter
from tests.helpers import create, database_snapshot, initialize

pytestmark = pytest.mark.anyio
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")
SID = UUID("00000000-0000-4000-8000-000000000001")


def epoch(iso: str) -> int:
    delta = datetime.fromisoformat(iso) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86400 + delta.seconds


def segment(
    *,
    market: Any = "HK",
    zone: Any = "Asia/Hong_Kong",
    window: FetchWindow = WINDOW,
    **changes: Any,
) -> AliasSegment:
    return AliasSegment(
        alias=AliasPeriod.model_validate(
            {
                "id": "00000000-0000-4000-8000-000000000002",
                "security_id": SID,
                "provider_id": "yahoo",
                "provider_symbol": "0005.HK",
                "effective_from": "2020-01-01",
                "verified": True,
                **changes,
            }
        ),
        window=window,
        market=market,
        exchange_timezone=zone,
    )


def access() -> ProviderAccess:
    return ProviderAccess(enabled=True, terms_acknowledged=True)


def event(when: str = "2026-01-08T01:30:00+00:00", **changes: Any) -> dict[str, Any]:
    instant = epoch(when)
    return {str(instant): {"date": instant, "amount": "0.9", **changes}}


def chart(events: Any, **meta: Any) -> dict[str, Any]:
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": "0005.HK",
                        "exchangeTimezoneName": "Asia/Hong_Kong",
                        "currency": "HKD",
                        "gmtoffset": 0,
                        **meta,
                    },
                    "events": events,
                }
            ],
        }
    }


def response(payload: Any, status: int = 200) -> httpx.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.Response(
        status, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body)
    )


async def test_yahoo_is_opt_in_without_a_key_and_local_health_never_probes() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(chart({}))

    async with ProviderHTTP(
        "yahoo", ProviderAccess(), transport=httpx.MockTransport(handle)
    ) as transport:
        source: ProviderAdapter = YahooAdapter(transport)
        health = await source.healthcheck()
        disabled = await source.fetch_dividends(segment())
        disabled_split = await source.fetch_splits(segment())
        caps = source.get_capabilities()
    assert health.state == disabled.state == disabled_split.state == FetchState.UNCONFIGURED
    assert health.reason == disabled.error_code == "PROVIDER_DISABLED" and not health.configured
    assert health.last_success_at is None and health.quota_remaining is None and calls == []
    assert caps.operations == ("DIVIDENDS", "SPLITS") and caps.markets == ("HK", "US")
    assert caps.live_validation == "NOT_TESTED" and caps.data_rights == "UNVERIFIED"
    assert caps.has_unadjusted_dividend is None and caps.raw_payload_storage is False
    assert independent_vendor_origins(["yahoo", "yfinance", "manual"]) == {"YAHOO"}
    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        health = await YahooAdapter(transport).healthcheck()
    assert health.configured and health.reason == "NOT_PROBED" and health.state == FetchState.STALE
    assert health.scope == "CONFIGURATION_ONLY" and calls == []


async def test_yahoo_terms_acknowledgment_is_required_even_without_a_paid_key() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unacknowledged terms reached HTTP")

    async with ProviderHTTP(
        "yahoo", ProviderAccess(enabled=True), transport=httpx.MockTransport(handle)
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert (
        result.state == FetchState.UNCONFIGURED and result.error_code == "RIGHTS_NOT_ACKNOWLEDGED"
    )


async def test_yahoo_preserves_exact_event_currency_and_uses_exchange_local_request_bounds() -> (
    None
):
    instant = epoch("2026-01-07T16:30:00+00:00")
    body = (
        b'{"chart":{"error":null,"result":[{"meta":{"symbol":"0005.HK",'
        b'"exchangeTimezoneName":"Asia/Hong_Kong","currency":"HKD","gmtoffset":0},'
        b'"events":{"dividends":{"'
        + str(instant).encode()
        + b'":{"date":'
        + str(instant).encode()
        + b',"amount":0.9000000000,"currency":"USD"}}}}]}}'
    )
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(body)

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.complete and result.issue_count == 0
    row = result.rows[0]
    assert row.ex_date == "2026-01-08" and row.provider_event_id == str(instant)
    assert row.provider_symbol == "0005.HK" and row.currency == row.source_currency == "USD"
    assert row.dps == "0.90000000" and row.adjustment_basis == "UNKNOWN"
    assert row.pay_date is row.announce_date is row.record_date is None
    assert row.event_type == "UNKNOWN" and "UNRESOLVED_ADJUSTMENT" in result.warnings
    assert "YAHOO_COMPATIBILITY_ONLY" in result.warnings and row.origin_group == "YAHOO"
    assert result.raw_sha256 is not None and len(result.raw_sha256) == 64
    assert result.source_url == "https://query1.finance.yahoo.com/v8/finance/chart/0005.HK"
    assert len(calls) == 1 and calls[0].url.path == "/v8/finance/chart/0005.HK"
    assert dict(calls[0].url.params) == {
        "period1": str(epoch("2025-12-31T16:00:00+00:00")),
        "period2": str(epoch("2026-01-31T16:00:00+00:00")),
        "interval": "1d",
        "events": "div",
    }
    assert "cookie" not in calls[0].headers and "authorization" not in calls[0].headers


@pytest.mark.parametrize(
    "window,first,after,hours",
    [
        (
            FetchWindow(start="2026-03-07", end="2026-03-09"),
            "2026-03-07T05:00:00+00:00",
            "2026-03-10T04:00:00+00:00",
            71,
        ),
        (
            FetchWindow(start="2026-10-31", end="2026-11-02"),
            "2026-10-31T04:00:00+00:00",
            "2026-11-03T05:00:00+00:00",
            73,
        ),
    ],
)
async def test_us_window_end_is_exclusive_and_historical_dst_is_not_a_fixed_offset(
    window: FetchWindow,
    first: str,
    after: str,
    hours: int,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(chart({}, symbol="BRK-B", exchangeTimezoneName="America/New_York"))

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await YahooAdapter(transport).fetch_dividends(
            segment(provider_symbol="BRK-B", market="US", zone="America/New_York", window=window)
        )
    params = calls[0].url.params
    assert params["period1"] == str(epoch(first)) and params["period2"] == str(epoch(after))
    assert int(params["period2"]) - int(params["period1"]) == hours * 3600
    assert result.state == FetchState.NO_DATA and result.complete
    assert calls[0].url.path.endswith("/BRK-B")


@pytest.mark.parametrize(
    "zone,market,when,inside",
    [
        ("Asia/Hong_Kong", "HK", "2026-01-07T15:59:59+00:00", False),
        ("Asia/Hong_Kong", "HK", "2026-01-07T16:00:00+00:00", True),
        ("Asia/Hong_Kong", "HK", "2026-01-08T15:59:59+00:00", True),
        ("Asia/Hong_Kong", "HK", "2026-01-08T16:00:00+00:00", False),
        ("America/New_York", "US", "2026-01-08T04:59:59+00:00", False),
        ("America/New_York", "US", "2026-01-08T05:00:00+00:00", True),
        ("America/New_York", "US", "2026-01-09T04:59:59+00:00", True),
        ("America/New_York", "US", "2026-01-09T05:00:00+00:00", False),
    ],
)
async def test_events_stay_inside_inclusive_exchange_local_alias_dates(
    zone: str, market: Any, when: str, inside: bool
) -> None:
    payload = chart({"dividends": event(when)}, exchangeTimezoneName=zone)
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(
            segment(
                market=market,
                zone=zone,
                window=FetchWindow(start="2026-01-08", end="2026-01-08"),
                effective_to="2026-01-08",
            )
        )
    assert result.state == (FetchState.OK_WITH_DATA if inside else FetchState.PARSE_ERROR)
    if inside:
        assert result.rows[0].ex_date == "2026-01-08" and result.complete
    else:
        assert result.rows == () and not result.complete and result.issue_count == 1
        assert "OUTSIDE_MAPPING_WINDOW" in result.warnings


@pytest.mark.parametrize(
    "changes,cursor,reason",
    [
        ({"provider_id": "yfinance"}, None, "ALIAS_PROVIDER_MISMATCH"),
        ({"market": None}, None, "MARKET_CONTEXT_REQUIRED"),
        ({"zone": None}, None, "EXCHANGE_TIMEZONE_REQUIRED"),
        ({"zone": "No/Such_Zone"}, None, "INVALID_EXCHANGE_TIMEZONE"),
        ({"zone": "../escape"}, None, "INVALID_EXCHANGE_TIMEZONE"),
        ({}, "not-a-documented-cursor", "UNEXPECTED_PROVIDER_CURSOR"),
        (
            {"window": FetchWindow(start="9999-12-31", end="9999-12-31")},
            None,
            "UNREPRESENTABLE_FETCH_WINDOW",
        ),
        (
            {
                "window": FetchWindow(start="0001-01-01", end="0001-01-01"),
                "effective_from": "0001-01-01",
            },
            None,
            "UNREPRESENTABLE_FETCH_WINDOW",
        ),
    ],
)
async def test_context_and_window_fail_before_http_without_guessed_zone_or_cursor(
    changes: dict[str, Any], cursor: str | None, reason: str
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid alias/context/window reached HTTP")

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment(**changes), cursor)
    assert result.error_code == reason and not result.complete and result.rows == ()


@pytest.mark.parametrize("operation", ["dividends", "splits"])
@pytest.mark.parametrize(
    "status,state",
    [
        (302, FetchState.PARSE_ERROR),
        (401, FetchState.AUTH_ERROR),
        (429, FetchState.RATE_LIMITED),
        (503, FetchState.TRANSIENT_ERROR),
    ],
)
async def test_http_failures_do_not_negotiate_cookies_retry_or_fabricate_empty_data(
    operation: str, status: int, state: FetchState
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response({"private": "never-return-this-upstream-error"}, status)

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        source = YahooAdapter(transport)
        result = (
            await source.fetch_dividends(segment())
            if operation == "dividends"
            else await source.fetch_splits(segment())
        )
    assert result.state == state and result.http_status == status and len(calls) == 1
    assert result.rows == () and not result.complete and result.next_cursor is None
    assert "never-return-this" not in result.model_dump_json()


@pytest.mark.parametrize(
    "error,state",
    [
        ({"code": "Not Found"}, FetchState.UNSUPPORTED),
        ({"code": "Unauthorized"}, FetchState.AUTH_ERROR),
        ({"code": "Forbidden"}, FetchState.AUTH_ERROR),
        ({"code": "Too Many Requests"}, FetchState.RATE_LIMITED),
        ({"code": "Service Unavailable"}, FetchState.TRANSIENT_ERROR),
        ({"code": "Internal Server Error"}, FetchState.TRANSIENT_ERROR),
        (
            {"code": "new-private-error", "description": "sensitive-raw-text"},
            FetchState.PARSE_ERROR,
        ),
        ({"code": []}, FetchState.PARSE_ERROR),
        (False, FetchState.PARSE_ERROR),
    ],
)
async def test_http_200_chart_errors_never_become_no_data_or_echo_raw_descriptions(
    error: Any, state: FetchState
) -> None:
    payload = chart({"dividends": event()})
    payload["chart"]["error"] = error
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == state and result.error_code == "PROVIDER_ERROR_RESPONSE"
    assert result.rows == () and not result.complete and result.http_status == 200
    assert "sensitive-raw-text" not in result.model_dump_json()
    assert "new-private-error" not in result.model_dump_json()


@pytest.mark.parametrize(
    "metadata", [{"currency": "HKD"}, {"currency": "USD"}, {"currency": "EUR"}]
)
async def test_quote_currency_never_fills_an_absent_event_currency(
    metadata: dict[str, Any],
) -> None:
    payload = chart({"dividends": event()}, **metadata)
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.rows[0].currency is result.rows[0].source_currency is None
    assert "MISSING_CURRENCY" in result.warnings and result.rows[0].dps == "0.90000000"


@pytest.mark.parametrize(
    "amount,expected",
    [(None, None), ("0", "0.00000000"), (0, "0.00000000"), ("1.00000001", "1.00000001")],
)
async def test_missing_zero_and_exact_dividends_remain_distinct(amount: Any, expected: Any) -> None:
    payload = chart({"dividends": event(amount=amount)})
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.rows[0].dps == expected
    assert ("MISSING_DPS" in result.warnings) == (expected is None)


@pytest.mark.parametrize(
    "bad",
    [
        {"date": True},
        {"date": "1767835800"},
        {"date": None},
        {"date": 1.5},
        {"date": 0},
        {"amount": True},
        {"amount": "-0.9"},
        {"amount": "0.000000001"},
        {"amount": "92233720368.54775808"},
    ],
)
async def test_malformed_event_rows_preserve_other_valid_evidence_as_partial(
    bad: dict[str, Any],
) -> None:
    rows = {**event(), **event("2026-01-09T01:30:00+00:00", **bad)}
    payload = chart({"dividends": rows})
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1
    assert (
        not result.complete
        and result.issue_count == 1
        and result.error_code == "PARTIAL_PROVIDER_PAGE"
    )
    assert "MALFORMED_PROVIDER_ROW" in result.warnings


@pytest.mark.parametrize(
    "key,value",
    [
        ("not-an-epoch", {"date": 1767835800}),
        ("1767835800", []),
        ("-62135596801", {"date": -62135596801}),
        ("253402300799", {"date": 253402300799}),
        ("999999999999", {"date": 999999999999}),
        ("9999999999999", {"date": 9999999999999}),
    ],
)
async def test_invalid_epoch_keys_and_calendar_overflow_are_errors_not_empty(
    key: str, value: Any
) -> None:
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: response(chart({"dividends": {key: value}}))),
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert (
        result.state == FetchState.PARSE_ERROR and not result.complete and result.issue_count == 1
    )


@pytest.mark.parametrize(
    "source_events", [{}, {"dividends": {}}, {"splits": {}}, {"capitalGains": event()}]
)
async def test_absent_or_empty_events_remain_unconfirmed_not_zero_income(
    source_events: Any,
) -> None:
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: response(chart(source_events))),
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.NO_DATA and result.complete and result.rows == ()
    if source_events.get("capitalGains"):
        assert "CAPITAL_GAINS_NOT_DIVIDEND_EVIDENCE" in result.warnings


async def test_absent_events_and_source_timezone_use_only_explicit_catalogue_context() -> None:
    payload = chart({})
    data = payload["chart"]["result"][0]
    del data["events"]
    del data["meta"]["exchangeTimezoneName"]
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.NO_DATA and result.complete
    assert {"SOURCE_TIMEZONE_ABSENT", "YAHOO_EVENTS_ABSENT"} <= set(result.warnings)


@pytest.mark.parametrize(
    "payload,reason",
    [
        ([], "INVALID_RESPONSE_SHAPE"),
        ({}, "INVALID_RESPONSE_SHAPE"),
        ({"chart": None}, "INVALID_RESPONSE_SHAPE"),
        ({"chart": {"result": None, "error": None}}, "INVALID_RESPONSE_SHAPE"),
        ({"chart": {"result": [], "error": None}}, "INVALID_RESPONSE_SHAPE"),
        ({"chart": {"result": [{}, {}], "error": None}}, "INVALID_RESPONSE_SHAPE"),
        ({"chart": {"result": [None], "error": None}}, "INVALID_RESPONSE_SHAPE"),
        (chart(None), "INVALID_RESPONSE_SHAPE"),
        (chart([]), "INVALID_RESPONSE_SHAPE"),
        (chart({"dividends": []}), "INVALID_RESPONSE_SHAPE"),
        (chart({"dividends": None}), "INVALID_RESPONSE_SHAPE"),
        (chart({}, symbol="5.HK"), "RESPONSE_SYMBOL_MISMATCH"),
        (chart({}, symbol=None), "RESPONSE_SYMBOL_MISMATCH"),
        (chart({}, exchangeTimezoneName="UTC"), "RESPONSE_TIMEZONE_MISMATCH"),
        (chart({}, exchangeTimezoneName=[]), "RESPONSE_TIMEZONE_MISMATCH"),
    ],
)
async def test_invalid_response_shapes_or_changed_identity_never_claim_coverage(
    payload: Any, reason: str
) -> None:
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.PARSE_ERROR and result.error_code == reason
    assert not result.complete and result.rows == ()


@pytest.mark.parametrize("location", ["root", "chart", "result"])
async def test_undocumented_pagination_is_not_silently_ignored(location: str) -> None:
    payload = chart({"dividends": event()})
    target = (
        payload
        if location == "root"
        else payload["chart"]
        if location == "chart"
        else payload["chart"]["result"][0]
    )
    target["next_url"] = "https://foreign.invalid/?token=untrusted"
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.PARSE_ERROR and result.error_code == "UNDOCUMENTED_PAGINATION"
    assert result.next_cursor is None and "untrusted" not in result.model_dump_json()


@pytest.mark.parametrize(
    "new,old,ratio,expected",
    [
        (3, 2, "3:2", (3, 2)),
        ("0.3", "0.2", "3/2", (3, 2)),
        (1, 4, "1:4", (1, 4)),
        (4, 2, None, (2, 1)),
        (
            "9223372036854775807",
            "9223372036854775806",
            None,
            (9223372036854775807, 9223372036854775806),
        ),
    ],
)
async def test_split_numeric_terms_are_exact_provider_only_not_current_share_actions(
    new: Any, old: Any, ratio: Any, expected: tuple[int, int]
) -> None:
    calls = []
    payload = chart({"splits": event(numerator=new, denominator=old, splitRatio=ratio)})

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(payload)

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await YahooAdapter(transport).fetch_splits(segment())
    row = result.rows[0]
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    assert (row.ratio_numerator, row.ratio_denominator) == expected
    assert row.effect_scope == "PROVIDER_ADJUSTMENT_ONLY" and row.source_adjustment_type is None
    assert "PROVIDER_ADJUSTMENT_ONLY" in result.warnings and row.effective_date == "2026-01-08"
    assert row.model_dump(mode="json")["ratio_numerator"] == str(expected[0])
    assert calls[0].url.params["events"] == "splits" and len(calls) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"numerator": None, "denominator": 2},
        {"numerator": 0, "denominator": 2},
        {"numerator": 1, "denominator": 0},
        {"numerator": True, "denominator": 1},
        {"numerator": "9223372036854775808", "denominator": 1},
        {"numerator": 3, "denominator": 2, "splitRatio": "2:3"},
        {"numerator": 3, "denominator": 2, "splitRatio": []},
        {"numerator": 3, "denominator": 2, "splitRatio": "3::2"},
        {"splitRatio": "3:2"},
    ],
)
async def test_bad_or_conflicting_split_fields_never_become_inferred_share_terms(
    fields: dict[str, Any],
) -> None:
    payload = chart({"splits": event(**fields)})
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YahooAdapter(transport).fetch_splits(segment())
    assert result.state == FetchState.PARSE_ERROR and result.rows == () and not result.complete


async def test_response_row_bound_duplicate_json_and_currency_correction_stay_distinct() -> None:
    payloads = iter(
        [
            chart({"dividends": {str(number): None for number in range(10001)}}),
            b'{"chart":{},"chart":{}}',
            chart({"dividends": event(currency="CAD")}),
            chart({"dividends": event(currency="EUR")}),
        ]
    )
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(next(payloads)))
    ) as transport:
        source = YahooAdapter(transport)
        too_many = await source.fetch_dividends(segment())
        duplicate = await source.fetch_dividends(segment())
        cad = await source.fetch_dividends(segment())
        eur = await source.fetch_dividends(segment())
    assert too_many.state == FetchState.PARSE_ERROR and too_many.error_code == "PROVIDER_ROW_LIMIT"
    assert duplicate.state == FetchState.PARSE_ERROR and duplicate.error_code == "INVALID_JSON"
    assert cad.rows[0].currency is eur.rows[0].currency is None
    assert cad.rows[0].source_currency == "CAD" and eur.rows[0].source_currency == "EUR"
    assert cad.rows[0].normalized_sha256 != eur.rows[0].normalized_sha256
    assert "UNSUPPORTED_EVENT_CURRENCY" in cad.warnings


async def test_noncanonical_epoch_key_cannot_duplicate_the_same_source_event() -> None:
    rows = event()
    key, value = next(iter(rows.items()))
    rows["0" + key] = value
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: response(chart({"dividends": rows}))),
    ) as transport:
        result = await YahooAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1
    assert result.issue_count == 1 and not result.complete
    assert "MALFORMED_PROVIDER_ROW" in result.warnings


async def test_direct_wrong_transport_is_rejected_without_provider_fallback() -> None:
    async with ProviderHTTP(
        "eodhd", ProviderAccess(), transport=httpx.MockTransport(lambda _: response([]))
    ) as transport:
        with pytest.raises(ValueError, match="own fixed-origin"):
            YahooAdapter(transport)


@pytest.mark.parametrize("exchange,zone", [("HKG", "Asia/Hong_Kong"), ("NYSE", "America/New_York")])
async def test_actual_resolver_and_both_fetches_leave_every_ledger_table_unchanged(
    client: httpx.AsyncClient, engine: Engine, exchange: str, zone: str
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic Yahoo isolation"})
    broker = await create(client, "brokers", {"name": "Synthetic Yahoo broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic Yahoo cash",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-01",
            "opening_fx_rate_hkd": "7.8",
        },
    )
    ticker, provider_symbol = ("0005", "0005.HK") if exchange == "HKG" else ("BRK.B", "BRK-B")
    security = await create(
        client,
        "securities",
        {
            "exchange_code": exchange,
            "display_ticker": ticker,
            "name": "Synthetic Yahoo security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "yahoo",
            "provider_symbol": provider_symbol,
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
        resolved = resolve_symbol(con, UUID(security["id"]), "yahoo", WINDOW)
    current = resolved.segments[0]
    assert current.exchange_timezone == zone
    assert current.market == ("HK" if exchange == "HKG" else "US")
    before = database_snapshot(engine)
    actions = {"dividends": event(currency="USD"), "splits": event(numerator=3, denominator=2)}
    replies = iter(
        [(chart(actions, symbol=provider_symbol, exchangeTimezoneName=zone), 200)] * 2 + [({}, 429)]
    )
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(*next(replies)))
    ) as transport:
        source = YahooAdapter(transport)
        dividend = await source.fetch_dividends(current)
        split = await source.fetch_splits(current)
        failure = await source.fetch_dividends(current)
    assert dividend.state == split.state == FetchState.OK_WITH_DATA
    assert failure.state == FetchState.RATE_LIMITED and failure.rows == ()
    assert dividend.rows[0].currency == "USD" and dividend.rows[0].adjustment_basis == "UNKNOWN"
    assert split.rows[0].effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    assert database_snapshot(engine) == before
