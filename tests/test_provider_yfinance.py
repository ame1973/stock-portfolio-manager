"""Actual optional SDK parsing preserves exact tokens and cannot bypass Yahoo I/O or identity."""

import importlib
import tomllib
import warnings
from decimal import Decimal
from fractions import Fraction
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.providers.yfinance as sdk_bridge
from spm_api.db import unit_of_work
from spm_api.providers.base import FetchState, independent_vendor_origins
from spm_api.providers.http import ProviderAccess, ProviderHTTP
from spm_api.providers.symbols import AliasSegment, FetchWindow, resolve_symbol
from spm_api.providers.yfinance import SDK_VERSION, YfinanceAdapter
from tests.helpers import create, database_snapshot, initialize
from tests.test_provider_yahoo import chart, event, response
from tests.test_provider_yahoo import segment as yahoo_segment

pytestmark = pytest.mark.anyio


def segment(**changes: Any) -> AliasSegment:
    return yahoo_segment(provider_id="yfinance", **changes)


def access() -> ProviderAccess:
    return ProviderAccess(enabled=True, terms_acknowledged=True)


@pytest.fixture
def sdk() -> Any:
    with warnings.catch_warnings():
        return importlib.import_module("yfinance.utils")


async def test_optional_parser_is_disabled_by_default_and_terms_precede_package_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_metadata(*args: Any) -> Any:
        pytest.fail("Disabled/unacknowledged SDK must not load an optional package")

    def no_http(request: httpx.Request) -> httpx.Response:
        pytest.fail("Disabled/unacknowledged SDK reached HTTP")

    monkeypatch.setattr(sdk_bridge, "version", no_metadata)
    for configuration, reason in [
        (ProviderAccess(), "PROVIDER_DISABLED"),
        (ProviderAccess(enabled=True), "RIGHTS_NOT_ACKNOWLEDGED"),
    ]:
        async with ProviderHTTP(
            "yahoo", configuration, transport=httpx.MockTransport(no_http)
        ) as transport:
            source = YfinanceAdapter(transport)
            health = await source.healthcheck()
            result = await source.fetch_dividends(segment())
            factor = await source.fetch_splits(segment())
            caps = source.get_capabilities()
        assert health.state == result.state == factor.state == FetchState.UNCONFIGURED
        assert result.error_code == factor.error_code == health.reason == reason
        assert not health.configured and health.provider_id == "yfinance"
        assert caps.access_mode == "OPTIONAL_SDK" and caps.operations == ("DIVIDENDS", "SPLITS")
        assert caps.live_validation == "NOT_TESTED" and caps.has_unadjusted_dividend is None
        assert independent_vendor_origins(["yahoo", "yfinance"]) == {"YAHOO"}


@pytest.mark.parametrize(
    "failure,reason",
    [
        (PackageNotFoundError("yfinance"), "SDK_NOT_INSTALLED"),
        (OSError("private-package-location"), "SDK_METADATA_ERROR"),
        (ValueError("private-package-location"), "SDK_METADATA_ERROR"),
        ("0.2.0", "SDK_VERSION_UNSUPPORTED"),
        ("99.0.0", "SDK_VERSION_UNSUPPORTED"),
    ],
)
async def test_absent_broken_or_unverified_sdk_versions_fail_before_http(
    failure: Any, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def metadata(name: str) -> str:
        if isinstance(failure, Exception):
            raise failure
        assert isinstance(failure, str)
        return failure

    def no_http(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unavailable SDK spent a provider request")

    monkeypatch.setattr(sdk_bridge, "version", metadata)
    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(no_http)) as transport:
        source = YfinanceAdapter(transport)
        health = await source.healthcheck()
        result = await source.fetch_dividends(segment())
    assert health.state == result.state == FetchState.UNCONFIGURED
    assert result.error_code == health.reason == reason and not result.complete
    assert "private-package-location" not in result.model_dump_json()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_sdk_import_and_missing_parser_errors_are_sanitized_local_configuration(
    unavailable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_import(name: str) -> Any:
        if unavailable:
            raise RuntimeError("private-import-error")
        return object()

    monkeypatch.setattr(sdk_bridge, "import_module", broken_import)
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP"))
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.UNCONFIGURED and result.rows == ()
    assert result.error_code == ("SDK_IMPORT_ERROR" if unavailable else "SDK_PARSER_UNAVAILABLE")
    assert "private-import-error" not in result.model_dump_json()


async def test_sdk_import_warning_filter_changes_are_scoped_and_health_is_not_a_live_probe(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_filters = list(warnings.filters)

    def import_with_filter_change(name: str) -> Any:
        warnings.filterwarnings("ignore")
        return sdk

    monkeypatch.setattr(sdk_bridge, "import_module", import_with_filter_change)
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Health spent quota")),
    ) as transport:
        health = await YfinanceAdapter(transport).healthcheck()
    assert list(warnings.filters) == original_filters
    assert health.configured and health.state == FetchState.STALE
    assert health.reason == "SDK_PARSER_READY_NOT_PROBED" and health.scope == "CONFIGURATION_ONLY"
    assert health.last_success_at is health.quota_remaining is None


@pytest.mark.parametrize("amount", ["0.90000000", "0", "1.00000001", "92233720368.54775807", None])
async def test_real_sdk_keeps_decimal_null_zero_and_int64_dps_without_sdk_network(
    amount: Any, sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_parser = sdk.parse_actions
    calls = []
    parser_inputs = []

    def parser(data: dict[str, Any]) -> Any:
        values = list(data["events"]["dividends"].values())
        assert len(values) == 1
        assert values[0]["amount"] is None or isinstance(values[0]["amount"], Decimal)
        assert set(values[0]) == {"date", "amount", "currency"}
        parser_inputs.append(data)
        return actual_parser(data)

    def no_sdk_network(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("SDK Ticker/history/download/network paths are forbidden")

    sdk_root = importlib.import_module("yfinance")
    sdk_data = importlib.import_module("yfinance.data")
    monkeypatch.setattr(sdk_root, "Ticker", no_sdk_network)
    monkeypatch.setattr(sdk_root, "download", no_sdk_network)
    monkeypatch.setattr(sdk_data.YfData, "get", no_sdk_network)
    monkeypatch.setattr(sdk_data.YfData, "cache_get", no_sdk_network)
    monkeypatch.setattr(sdk, "parse_actions", parser)
    payload = chart(
        {"dividends": event("2026-01-07T16:30:00+00:00", amount=amount, currency="USD")}
    )

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(payload)

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    (observed,) = result.rows
    expected = None if amount is None else "0.00000000" if amount == "0" else amount
    assert observed.dps == expected and observed.ex_date == "2026-01-08"
    assert observed.provider_id == "yfinance" and observed.origin_group == "YAHOO"
    assert observed.provider_symbol == "0005.HK" and observed.currency == "USD"
    assert observed.adjustment_basis == "UNKNOWN" and observed.pay_date is None
    assert "YFINANCE_PARSER_ONLY" in result.warnings and len(parser_inputs) == len(calls) == 1
    assert (
        calls[0].url.host == "query1.finance.yahoo.com" and calls[0].url.params["events"] == "div"
    )
    assert "cookie" not in calls[0].headers and "authorization" not in calls[0].headers


@pytest.mark.parametrize("currency", [None, "", "CAD", "USD"])
async def test_sdk_cannot_fill_currency_from_quote_metadata_or_invent_payment_dates(
    currency: Any,
) -> None:
    payload = chart({"dividends": event(currency=currency)}, currency="HKD")
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    observed = result.rows[0]
    assert observed.currency == ("USD" if currency == "USD" else None)
    assert observed.pay_date is observed.record_date is observed.announce_date is None
    if currency == "CAD":
        assert observed.source_currency == "CAD" and "UNSUPPORTED_EVENT_CURRENCY" in result.warnings


@pytest.mark.parametrize(
    "new,old,expected",
    [
        (3, 2, (3, 2)),
        (1, 3, (1, 3)),
        (1, 4, (1, 4)),
        ("0.3", "0.2", (3, 2)),
        ("9223372036854775807", "9223372036854775806", (9223372036854775807, 9223372036854775806)),
    ],
)
async def test_real_sdk_split_division_uses_fraction_and_preserves_rational_terms(
    new: Any, old: Any, expected: tuple[int, int], sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = sdk.parse_actions

    def parser(data: dict[str, Any]) -> Any:
        first = next(iter(data["events"]["splits"].values()))
        assert isinstance(first["numerator"], Fraction) and isinstance(
            first["denominator"], Fraction
        )
        return actual(data)

    monkeypatch.setattr(sdk, "parse_actions", parser)
    payload = chart({"splits": event(numerator=new, denominator=old)})
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_splits(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    factor = result.rows[0]
    assert (factor.ratio_numerator, factor.ratio_denominator) == expected
    assert factor.provider_id == "yfinance" and factor.effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    assert factor.model_dump(mode="json")["ratio_numerator"] == str(expected[0])


@pytest.mark.parametrize("loss", ["float", "amount", "currency", "missing", "date", "subsecond"])
async def test_changed_or_lossy_sdk_outputs_are_not_accepted_as_exact_observations(
    loss: str, sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = sdk.parse_actions
    pandas = importlib.import_module("pandas")

    def parser(data: dict[str, Any]) -> Any:
        frames = actual(data)
        frame = frames[0]
        if loss == "float":
            frame.iloc[0, frame.columns.get_loc("Dividends")] = 0.9
        elif loss == "amount":
            frame.iloc[0, frame.columns.get_loc("Dividends")] = Decimal("0.8")
        elif loss == "currency":
            frame.iloc[0, frame.columns.get_loc("currency")] = "HKD"
        elif loss == "missing":
            frames = (frame.iloc[0:0], frames[1], frames[2])
        elif loss == "date":
            frame.index = frame.index + pandas.Timedelta(days=1)
        else:
            frame.index = frame.index.as_unit("ns") + pandas.Timedelta(nanoseconds=1)
        return frames

    monkeypatch.setattr(sdk, "parse_actions", parser)
    payload = chart({"dividends": event(currency="USD")})
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(payload))
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.PARSE_ERROR and result.rows == () and not result.complete
    assert result.issue_count >= 1


@pytest.mark.parametrize("factor", [1.5, Decimal("1.5"), Fraction(2, 3)])
async def test_sdk_approximate_or_changed_split_factors_are_never_adopted(
    factor: Any, sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = sdk.parse_actions

    def parser(data: dict[str, Any]) -> Any:
        frames = actual(data)
        frames[1].iloc[0, 0] = factor
        return frames

    monkeypatch.setattr(sdk, "parse_actions", parser)
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(
            lambda _: response(chart({"splits": event(numerator=3, denominator=2)}))
        ),
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_splits(segment())
    assert result.state == FetchState.PARSE_ERROR and result.rows == () and result.issue_count == 1


async def test_one_sdk_row_failure_keeps_other_valid_rows_as_partial(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = sdk.parse_actions

    def parser(data: dict[str, Any]) -> Any:
        frames = actual(data)
        frames[0].iloc[0, 0] = 0.9
        return frames

    monkeypatch.setattr(sdk, "parse_actions", parser)
    rows = {**event(), **event("2026-01-09T01:30:00+00:00", amount="0.4")}
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: response(chart({"dividends": rows}))),
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1
    assert result.rows[0].dps == "0.40000000" and result.issue_count == 1 and not result.complete


@pytest.mark.parametrize("failure", ["exception", "warning", "tuple", "large"])
async def test_unexpected_sdk_failures_never_echo_private_details_or_become_empty(
    failure: str, sdk: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    actual = sdk.parse_actions

    def parser(data: dict[str, Any]) -> Any:
        if failure == "exception":
            raise RuntimeError("private-sdk-details")
        if failure == "warning":
            warnings.warn("private-sdk-details", UserWarning, stacklevel=1)
            return actual(data)
        if failure == "tuple":
            return ()
        frames = actual(data)
        frame = frames[0].reindex(frames[0].index.repeat(10001))
        return frame, frames[1], frames[2]

    monkeypatch.setattr(sdk, "parse_actions", parser)
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: response(chart({"dividends": event()}))),
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert (
        result.state == FetchState.PARSE_ERROR
        and result.error_code == "PROVIDER_NORMALIZATION_ERROR"
    )
    assert result.rows == () and not result.complete and result.http_status == 200
    assert "private-sdk-details" not in result.model_dump_json()
    captured = capsys.readouterr()
    assert "private-sdk-details" not in captured.out + captured.err


@pytest.mark.parametrize(
    "status,state",
    [
        (401, FetchState.AUTH_ERROR),
        (429, FetchState.RATE_LIMITED),
        (503, FetchState.TRANSIENT_ERROR),
    ],
)
async def test_sdk_is_not_called_for_http_failures_and_never_retries(
    status: int, state: FetchState, sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_parser(data: Any) -> Any:
        pytest.fail("HTTP failure reached the SDK")

    monkeypatch.setattr(sdk, "parse_actions", no_parser)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response({}, status)

    async with ProviderHTTP("yahoo", access(), transport=httpx.MockTransport(handle)) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == state and result.rows == () and len(calls) == 1


@pytest.mark.parametrize("rows", [{}, {"bad-key": {"date": 0}}, event(amount=True)])
async def test_empty_or_all_bad_input_never_invokes_sdk_or_fills_missing_data(
    rows: Any, sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_parser(data: Any) -> Any:
        pytest.fail("Empty or invalid bounded actions reached SDK parsing")

    monkeypatch.setattr(sdk, "parse_actions", no_parser)
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: response(chart({"dividends": rows}))),
    ) as transport:
        result = await YfinanceAdapter(transport).fetch_dividends(segment())
    assert result.state == (FetchState.NO_DATA if not rows else FetchState.PARSE_ERROR)
    assert result.rows == () and result.complete == (not rows)


async def test_sdk_alias_context_is_preserved_and_cannot_relabel_a_yahoo_mapping() -> None:
    async with ProviderHTTP(
        "yahoo",
        access(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Wrong context reached HTTP")),
    ) as transport:
        source = YfinanceAdapter(transport)
        wrong = await source.fetch_dividends(yahoo_segment())
        cursor = await source.fetch_dividends(segment(), "unexpected")
        unknown = await source.fetch_dividends(segment(zone=None))
    assert wrong.error_code == "ALIAS_PROVIDER_MISMATCH"
    assert cursor.error_code == "UNEXPECTED_PROVIDER_CURSOR"
    assert unknown.error_code == "EXCHANGE_TIMEZONE_REQUIRED"
    async with ProviderHTTP("yfinance", access()) as unsupported:
        with pytest.raises(ValueError, match="fixed-origin"):
            YfinanceAdapter(unsupported)


def test_optional_and_dev_dependencies_pin_the_actual_parser_profile() -> None:
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    pinned = f"yfinance=={SDK_VERSION}"
    assert pinned in project["project"]["optional-dependencies"]["yahoo-sdk"]
    assert pinned in project["dependency-groups"]["dev"]


async def test_actual_http_sqlite_ledger_stays_unchanged_after_sdk_success_and_failure(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic SDK isolation"})
    broker = await create(client, "brokers", {"name": "Synthetic SDK broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic SDK cash",
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
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Synthetic SDK security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "yfinance",
            "provider_symbol": "0005.HK",
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
        resolved = resolve_symbol(
            con, UUID(security["id"]), "yfinance", FetchWindow(start="2026-01-01", end="2026-01-31")
        )
    before = database_snapshot(engine)
    payload = chart(
        {"dividends": event(currency="USD"), "splits": event(numerator=1, denominator=3)}
    )
    replies = iter([(payload, 200), (payload, 200), ({}, 429)])
    async with ProviderHTTP(
        "yahoo", access(), transport=httpx.MockTransport(lambda _: response(*next(replies)))
    ) as transport:
        source = YfinanceAdapter(transport)
        cash = await source.fetch_dividends(resolved.segments[0])
        factor = await source.fetch_splits(resolved.segments[0])
        failure = await source.fetch_dividends(resolved.segments[0])
    assert cash.state == factor.state == FetchState.OK_WITH_DATA
    assert failure.state == FetchState.RATE_LIMITED and failure.rows == ()
    assert cash.rows[0].provider_id == "yfinance" and cash.rows[0].adjustment_basis == "UNKNOWN"
    assert (factor.rows[0].ratio_numerator, factor.rows[0].ratio_denominator) == (1, 3)
    assert database_snapshot(engine) == before
