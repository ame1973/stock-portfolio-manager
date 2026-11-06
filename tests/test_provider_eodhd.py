"""Real EODHD adapter contracts over mocked HTTP, with no credentials or ledger adoption."""

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.providers.adapters import FetchPage, ProviderAdapter
from spm_api.providers.base import DividendObservation, FetchState, SplitObservation
from spm_api.providers.eodhd import EodhdAdapter
from spm_api.providers.http import ProviderAccess, ProviderHTTP
from spm_api.providers.symbols import AliasPeriod, AliasSegment, FetchWindow, resolve_symbol
from spm_api.providers.values import dps_string, exact_number, split_ratio
from tests.helpers import create, database_snapshot, initialize

pytestmark = pytest.mark.anyio
SID = UUID("00000000-0000-4000-8000-000000000001")
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")
BODY = (
    b'[{"date":"2026-01-08","declarationDate":null,"recordDate":"2026-01-09",'
    b'"paymentDate":"2026-01-22","period":"Interim","value":0.6,'
    b'"unadjustedValue":0.9,"currency":"USD"}]'
)


def segment(**changes: Any) -> AliasSegment:
    period = AliasPeriod.model_validate(
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "security_id": str(SID),
            "provider_id": "eodhd",
            "provider_symbol": "0005.HK",
            "effective_from": "2020-01-01",
            "verified": True,
            **changes,
        }
    )
    return AliasSegment(alias=period, window=WINDOW)


def access() -> ProviderAccess:
    return ProviderAccess(
        enabled=True, terms_acknowledged=True, credential=SecretStr("synthetic-eodhd-test-key")
    )


def response(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body)
    )


def row(**changes: Any) -> dict[str, Any]:
    return {
        "date": "2026-01-08",
        "unadjustedValue": "0.9",
        "value": "0.6",
        "currency": "USD",
        "period": "Quarterly",
        **changes,
    }


async def test_eodhd_fetches_one_documented_window_and_preserves_unproven_cash_basis() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(BODY)

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as transport:
        adapter: ProviderAdapter = EodhdAdapter(transport)
        result = await adapter.fetch_dividends(segment())
    assert len(calls) == 1
    assert calls[0].url.path == "/api/div/0005.HK"
    assert calls[0].url.params["from"] == WINDOW.start and calls[0].url.params["to"] == WINDOW.end
    assert calls[0].url.params["fmt"] == "json"
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    assert result.raw_sha256 == hashlib.sha256(BODY).hexdigest()
    assert result.source_url == "https://eodhd.com/api/div/0005.HK"
    assert result.next_cursor is None and result.issue_count == 0
    observed = result.rows[0]
    assert observed.security_id == SID and observed.provider_symbol == "0005.HK"
    assert observed.origin_group == "EODHD" and observed.dps == "0.90000000"
    assert observed.currency == "USD" and observed.announce_date is None
    assert observed.record_date == "2026-01-09" and observed.pay_date == "2026-01-22"
    assert observed.event_type == "INTERIM" and observed.adjustment_basis == "UNKNOWN"
    assert observed.provider_event_id is None
    assert "UNRESOLVED_ADJUSTMENT" in result.warnings
    assert "HISTORICAL_PER_SHARE_BASIS_UNRESOLVED" in result.warnings
    assert (
        "receipt_state" not in observed.model_dump()
        and "review_status" not in observed.model_dump()
    )
    serialized = json.loads(result.model_dump_json())
    assert serialized["rows"][0]["dps"] == "0.90000000"
    assert "synthetic-eodhd-test-key" not in result.model_dump_json()


async def test_healthcheck_and_capabilities_are_local_metadata_not_a_paid_probe() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(b"[]")

    async with ProviderHTTP(
        "eodhd", ProviderAccess(), transport=httpx.MockTransport(handle)
    ) as transport:
        adapter = EodhdAdapter(transport)
        caps = adapter.get_capabilities()
        health = await adapter.healthcheck()
        unavailable = await adapter.fetch_dividends(segment())
    assert caps.operations == ("DIVIDENDS", "SPLITS") and caps.has_unadjusted_dividend is True
    assert caps.coverage_start is None and caps.live_validation == "NOT_TESTED"
    assert caps.data_rights == "UNVERIFIED" and caps.raw_payload_storage is False
    assert health.state == FetchState.UNCONFIGURED and health.reason == "NO_API_KEY"
    assert health.scope == "CONFIGURATION_ONLY" and health.quota_remaining is None
    assert health.last_success_at is None and unavailable.error_code == "NO_API_KEY"
    assert unavailable.state == FetchState.UNCONFIGURED and unavailable.rows == ()
    assert calls == []
    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as transport:
        health = await EodhdAdapter(transport).healthcheck()
    assert health.configured and health.state == FetchState.STALE and health.reason == "NOT_PROBED"
    assert calls == []


@pytest.mark.parametrize(
    "changes,cursor,state,reason",
    [
        ({"provider_id": "yahoo"}, None, FetchState.UNSUPPORTED, "ALIAS_PROVIDER_MISMATCH"),
        (
            {"provider_symbol": "0005"},
            None,
            FetchState.PARSE_ERROR,
            "EXPLICIT_EXCHANGE_ALIAS_REQUIRED",
        ),
        ({}, "unexpected-page", FetchState.PARSE_ERROR, "UNEXPECTED_PROVIDER_CURSOR"),
    ],
)
async def test_alias_and_cursor_guards_fail_before_network(
    changes: dict[str, Any],
    cursor: str | None,
    state: FetchState,
    reason: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid alias/cursor reached EODHD HTTP")

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment(**changes), cursor)
    assert result.state == state and result.error_code == reason and result.rows == ()
    assert not result.complete


async def test_wrong_transport_is_not_reused_for_an_eodhd_identity() -> None:
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(b"[]"))
    ) as transport:
        with pytest.raises(ValueError, match="fixed-origin"):
            EodhdAdapter(transport)


@pytest.mark.parametrize(
    "status,state",
    [
        (401, FetchState.AUTH_ERROR),
        (403, FetchState.AUTH_ERROR),
        (429, FetchState.RATE_LIMITED),
        (500, FetchState.TRANSIENT_ERROR),
    ],
)
async def test_transport_failures_remain_explicit_and_never_return_dividend_rows(
    status: int,
    state: FetchState,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(b"[]", status)

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.state == state and result.http_status == status and result.rows == ()
    assert not result.complete and len(calls) == 1


async def test_valid_empty_array_is_no_data_not_confirmed_zero_dividends() -> None:
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(b"[]"))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.NO_DATA and result.complete and result.rows == ()


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"data": [], "next_url": "https://eodhd.com/next"}, "INVALID_RESPONSE_SHAPE"),
        (None, "INVALID_RESPONSE_SHAPE"),
        ({"error": "invalid API key"}, "INVALID_RESPONSE_SHAPE"),
        ([None] * 10001, "PROVIDER_ROW_LIMIT"),
    ],
)
async def test_undocumented_envelopes_or_hidden_pagination_are_not_silently_accepted(
    payload: Any,
    reason: str,
) -> None:
    body = json.dumps(payload).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.PARSE_ERROR and result.error_code == reason
    assert not result.complete and result.rows == ()


@pytest.mark.parametrize(
    "changes,dps,basis",
    [
        ({"unadjustedValue": "0", "value": "9"}, "0.00000000", "UNKNOWN"),
        ({"unadjustedValue": None, "value": "0.6"}, "0.60000000", "SPLIT_ADJUSTED"),
        ({"unadjustedValue": None, "value": None}, None, "UNKNOWN"),
        ({"unadjustedValue": "0.9000000000"}, "0.90000000", "UNKNOWN"),
    ],
)
async def test_unadjusted_zero_is_not_replaced_and_fallback_basis_stays_explicit(
    changes: dict[str, Any],
    dps: str | None,
    basis: str,
) -> None:
    body = json.dumps([row(**changes)]).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.rows[0].dps == dps
    assert result.rows[0].adjustment_basis == basis
    assert result.rows[0].adjustment_basis != "AS_DECLARED"


@pytest.mark.parametrize(
    "currency,expected", [(None, None), ("EUR", None), ("HKD", "HKD"), (" usd ", "USD")]
)
async def test_dividend_currency_is_not_filled_from_security_or_market(
    currency: str | None,
    expected: str | None,
) -> None:
    body = json.dumps([row(currency=currency, paymentDate=None)]).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.rows[0].currency == expected and result.rows[0].pay_date is None
    assert result.rows[0].dps == "0.90000000"
    if expected is None:
        assert "MISSING_CURRENCY" in result.warnings
    if currency == "EUR":
        assert "UNSUPPORTED_EVENT_CURRENCY" in result.warnings


@pytest.mark.parametrize(
    "period,expected",
    [
        ("Quarterly", "UNKNOWN"),
        ("annual", "UNKNOWN"),
        ("Other", "UNKNOWN"),
        ("Special", "SPECIAL"),
        ("Final", "FINAL"),
        ("Interim", "INTERIM"),
        ("Supplemental", "SUPPLEMENTAL"),
        (None, "UNKNOWN"),
    ],
)
async def test_frequency_is_not_guessed_to_be_a_regular_event_type(
    period: Any, expected: str
) -> None:
    body = json.dumps([row(period=period)]).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.rows[0].event_type == expected


async def test_unsupported_currency_and_period_corrections_keep_distinct_evidence() -> None:
    body = json.dumps(
        [
            row(currency="EUR", period="Quarterly"),
            row(currency="GBP", period="Quarterly"),
            row(currency="EUR", period="Annual"),
        ]
    ).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 3
    assert all(item.currency is None for item in result.rows)
    assert [item.source_currency for item in result.rows] == ["EUR", "GBP", "EUR"]
    assert [item.source_period for item in result.rows] == ["QUARTERLY", "QUARTERLY", "ANNUAL"]
    assert len({item.normalized_sha256 for item in result.rows}) == 3
    assert all(item.event_type == "UNKNOWN" for item in result.rows)


async def test_same_date_special_and_other_source_assertions_are_not_merged() -> None:
    body = json.dumps([row(period="Special"), row(period="Quarterly")]).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert len(result.rows) == 2 and result.complete
    assert result.rows[0].normalized_sha256 != result.rows[1].normalized_sha256


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"date": "2026-02-30"},
        row(unadjustedValue=True),
        row(unadjustedValue="0.000000001"),
        row(paymentDate="2026-02-30"),
        row(date="2025-12-31"),
        row(date="2026-02-01"),
    ],
)
async def test_bad_or_outside_mapping_rows_leave_good_evidence_partial_not_destroyed(
    invalid: Any,
) -> None:
    body = json.dumps([row(), invalid]).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and not result.complete
    assert len(result.rows) == 1 and result.rows[0].ex_date == "2026-01-08"
    assert result.issue_count == 1 and result.error_code == "PARTIAL_PROVIDER_PAGE"
    assert result.raw_sha256 == hashlib.sha256(body).hexdigest()


async def test_bad_rows_fail_and_duplicate_evidence_does_not_count_twice() -> None:
    bodies = [b"[null,{}]", json.dumps([row(), row()]).encode()]
    replies = iter(bodies)
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(next(replies)))
    ) as transport:
        adapter = EodhdAdapter(transport)
        invalid = await adapter.fetch_dividends(segment())
        duplicate = await adapter.fetch_dividends(segment(verified=False))
    assert (
        invalid.state == FetchState.PARSE_ERROR and invalid.rows == () and invalid.issue_count == 2
    )
    assert len(duplicate.rows) == 1 and not duplicate.complete and duplicate.issue_count == 1
    assert "DUPLICATE_SOURCE_OBSERVATION" in duplicate.warnings
    assert "UNVERIFIED_PROVIDER_ALIAS" in duplicate.warnings


@pytest.mark.parametrize(
    "ratio,numerator,denominator",
    [
        ("3.000000/2.000000", 3, 2),
        ("1.000000/4.000000", 1, 4),
        ("1.5/1", 3, 2),
        ("10000000000000000000/10000000000000000000", 1, 1),
        ("9223372036854775807/9223372036854775806", 9223372036854775807, 9223372036854775806),
    ],
)
async def test_split_factors_are_exact_reduced_provider_adjustments_not_adopted_actions(
    ratio: str,
    numerator: int,
    denominator: int,
) -> None:
    body = json.dumps([{"date": "2026-01-08", "split": ratio}]).encode()
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(body)

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await EodhdAdapter(transport).fetch_splits(segment())
    assert calls[0].url.path == "/api/splits/0005.HK" and len(calls) == 1
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    factor = result.rows[0]
    assert (factor.ratio_numerator, factor.ratio_denominator) == (numerator, denominator)
    assert factor.effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    assert "PROVIDER_ADJUSTMENT_ONLY" in result.warnings
    serialized = json.loads(result.model_dump_json())
    assert serialized["rows"][0]["ratio_numerator"] == str(numerator)
    assert serialized["rows"][0]["ratio_denominator"] == str(denominator)
    assert len(factor.normalized_sha256) == 64


@pytest.mark.parametrize(
    "ratio", [None, True, "3", "3/0", "0/3", "-1/3", "3/2/1", "1e3/1", "9223372036854775808/1"]
)
async def test_invalid_split_terms_are_not_rounded_or_faked(ratio: Any) -> None:
    body = json.dumps([{"date": "2026-01-08", "split": ratio}]).encode()
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as transport:
        result = await EodhdAdapter(transport).fetch_splits(segment())
    assert result.state == FetchState.PARSE_ERROR and result.rows == () and not result.complete


@pytest.mark.parametrize(
    "value,expected",
    [
        ("000.3000000000", "0.30000000"),
        (Decimal("0.3"), "0.30000000"),
        (Decimal("1e-8"), "0.00000001"),
        (0, "0.00000000"),
        (None, None),
        ("92233720368.54775807", "92233720368.54775807"),
    ],
)
def test_numeric_tokens_normalize_only_exact_equivalent_values(
    value: Any, expected: str | None
) -> None:
    assert dps_string(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        0.3,
        "1e3",
        "0.000000001",
        "-1",
        "NaN",
        "Infinity",
        "０.３",
        Decimal("Infinity"),
        Decimal("1e1000000"),
        Decimal("1e-1000000"),
        10**65,
        "1" * 65,
        Decimal("1." + "1" * 64),
    ],
)
def test_untrusted_number_boundary_rejects_float_nonfinite_precision_and_resource_abuse(
    value: Any,
) -> None:
    with pytest.raises(ValueError):
        dps_string(value)


def test_split_decimal_algebra_never_rounds_to_int64_or_mutates_input() -> None:
    assert split_ratio("6/4") == (3, 2)
    assert exact_number(Decimal("-0")) == Decimal(0)
    with pytest.raises(ValueError):
        split_ratio("x" * 130)
    with pytest.raises(ValueError):
        exact_number(Decimal("-1"))


async def test_fetching_source_dividends_and_split_factors_never_adopts_or_rebooks_the_ledger(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic adapter isolation"})
    broker = await create(client, "brokers", {"name": "Synthetic adapter broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic adapter cash",
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
            "display_ticker": "SYN",
            "name": "Synthetic adapter security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "eodhd",
            "provider_symbol": "SYN.US",
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
        resolved = resolve_symbol(con, UUID(security["id"]), "eodhd", WINDOW)
    before = database_snapshot(engine)
    replies = iter([BODY, b'[{"date":"2026-01-08","split":"3.000000/2.000000"}]'])
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(next(replies)))
    ) as transport:
        adapter = EodhdAdapter(transport)
        dividend = await adapter.fetch_dividends(resolved.segments[0])
        split = await adapter.fetch_splits(resolved.segments[0])
    assert len(dividend.rows) == len(split.rows) == 1
    assert dividend.rows[0].adjustment_basis == "UNKNOWN"
    assert split.rows[0].effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    assert database_snapshot(engine) == before


def test_adapter_page_contracts_do_not_allow_data_free_success_or_complete_partial_failures() -> (
    None
):
    observed = DividendObservation(provider_id="eodhd", security_id=SID, provider_symbol="0005.HK")
    invalid_pages: list[dict[str, Any]] = [
        {"state": FetchState.OK_WITH_DATA},
        {"state": FetchState.NO_DATA, "rows": (observed,)},
        {"state": FetchState.PARSE_ERROR, "complete": True},
        {"state": FetchState.OK_WITH_DATA, "rows": (observed,), "complete": True, "issue_count": 1},
    ]
    for values in invalid_pages:
        with pytest.raises(ValidationError):
            FetchPage[DividendObservation].model_validate({"window": WINDOW, **values})
    split = SplitObservation(
        provider_id="eodhd",
        security_id=SID,
        provider_symbol="0005.HK",
        effective_date="2026-01-08",
        ratio_numerator=3,
        ratio_denominator=2,
    )
    assert split.model_dump(mode="json")["ratio_numerator"] == "3"


@pytest.mark.parametrize("first,last", [("2025-12-31", None), ("2026-01-01", "2026-02-01")])
def test_direct_fetch_segments_cannot_bypass_explicit_identity_periods(
    first: str, last: str | None
) -> None:
    period = segment(effective_from="2026-01-01", effective_to="2026-01-31").alias
    window = FetchWindow(start=first, end=last or "2026-01-31")
    with pytest.raises(ValidationError):
        AliasSegment(alias=period, window=window)
