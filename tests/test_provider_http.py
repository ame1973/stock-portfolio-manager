"""Mock HTTP exercises real provider transport policies without keys or external requests."""

import asyncio
import gzip
import hashlib
import logging
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from spm_api.providers.base import FetchState
from spm_api.providers.http import (
    ProviderAccess,
    ProviderHTTP,
    TransportError,
    retry_at,
)

pytestmark = pytest.mark.anyio
KEY = "synthetic-server-provider-key"
NOW = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)


def access(**changes: Any) -> ProviderAccess:
    return ProviderAccess.model_validate(
        {
            "enabled": True,
            "terms_acknowledged": True,
            "credential": SecretStr(KEY),
            **changes,
        }
    )


def response(body: bytes = b"[]", status: int = 200, /, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **headers},
        stream=httpx.ByteStream(body),
    )


@pytest.mark.parametrize(
    "provider,path,expected",
    [
        ("eodhd", "/api/div/0005.HK", TransportError.NO_API_KEY),
        ("alphavantage", "/query", TransportError.NO_API_KEY),
        ("massive", "/stocks/v1/dividends", TransportError.NO_API_KEY),
        ("yahoo", "/v8/finance/chart/0005.HK", TransportError.PROVIDER_DISABLED),
        ("yfinance", "/v8/finance/chart/0005.HK", TransportError.UNSUPPORTED_PROVIDER),
        ("hkex_evidence", "/", TransportError.UNSUPPORTED_PROVIDER),
        ("sec_evidence", "/", TransportError.UNSUPPORTED_PROVIDER),
        ("manual", "/", TransportError.UNSUPPORTED_PROVIDER),
    ],
)
async def test_default_configuration_never_calls_a_provider_or_a_manual_scraper(
    provider: Any,
    path: str,
    expected: TransportError,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    async with ProviderHTTP(
        provider, ProviderAccess(), transport=httpx.MockTransport(handle)
    ) as client:
        result = await client.get_json(path)
    assert result.error == expected and result.status_code is None
    assert calls == [] and KEY not in repr(result)
    assert result.failure_state == (
        FetchState.UNSUPPORTED
        if expected == TransportError.UNSUPPORTED_PROVIDER
        else FetchState.UNCONFIGURED
    )


@pytest.mark.parametrize(
    "configuration,error",
    [
        (access(enabled=False), TransportError.PROVIDER_DISABLED),
        (access(terms_acknowledged=False), TransportError.RIGHTS_NOT_ACKNOWLEDGED),
        (access(credential=""), TransportError.INVALID_CREDENTIAL),
        (access(credential="bad\nkey"), TransportError.INVALID_CREDENTIAL),
        (access(credential="bad key"), TransportError.INVALID_CREDENTIAL),
        (access(credential="x" * 257), TransportError.INVALID_CREDENTIAL),
    ],
)
async def test_keys_do_not_grant_permissions_and_invalid_secrets_are_not_sent(
    configuration: ProviderAccess,
    error: TransportError,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    async with ProviderHTTP(
        "eodhd", configuration, transport=httpx.MockTransport(handle)
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error == error and calls == []
    assert KEY not in str(configuration) and KEY not in repr(result)


async def test_runtime_client_uses_tls_no_environment_proxy_no_redirects_and_phase_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = httpx.AsyncClient
    options = {}

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        options.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response())
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error is None
    assert options["verify"] is True and options["trust_env"] is False
    assert options["follow_redirects"] is False
    timeout = options["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5 and timeout.read == timeout.write == 20 and timeout.pool == 5


async def test_eodhd_injects_the_server_key_json_format_and_redacts_the_evidence_url() -> None:
    requests = []
    body = b'{"dps":0.10000000000000001,"large":9007199254740993,"missing":null}'

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(body)

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as client:
        result = await client.get_json(
            "/api/div/0005.HK?api_token=untrusted&fmt=csv",
            params={"from": "2026-01-01", "to": "2026-01-31"},
        )
    assert len(requests) == 1
    sent = requests[0]
    assert sent.url.host == "eodhd.com" and sent.url.params["api_token"] == KEY
    assert sent.url.params["fmt"] == "json"
    assert "StockPortfolioManager" in sent.headers["user-agent"]
    assert isinstance(result.payload, dict)
    assert result.payload["dps"] == Decimal("0.10000000000000001")
    assert isinstance(result.payload["dps"], Decimal)
    assert result.payload["large"] == 9007199254740993
    assert result.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert result.source_url == "https://eodhd.com/api/div/0005.HK"
    assert result.failure_state is None and KEY not in repr(result)


async def test_alpha_query_auth_and_massive_bearer_pagination_never_adopt_an_upstream_key() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response()

    mock = httpx.MockTransport(handle)
    async with ProviderHTTP("alphavantage", access(), transport=mock) as client:
        result = await client.get_json(
            "/query", params={"function": "DIVIDENDS", "symbol": "BRK.B"}
        )
        assert result.error is None
    async with ProviderHTTP("massive", access(), transport=mock) as client:
        result = await client.get_json(
            "https://api.massive.com/stocks/v1/dividends?cursor=next%3Dpage&apiKey=untrusted"
        )
        assert result.error is None
    assert requests[0].url.params["apikey"] == KEY
    assert requests[0].url.params["symbol"] == "BRK.B"
    assert requests[1].url.params["cursor"] == "next=page"
    assert "apikey" not in {name.casefold() for name in requests[1].url.params}
    assert requests[1].headers["authorization"] == f"Bearer {KEY}"
    assert result.source_url == "https://api.massive.com/stocks/v1/dividends"


async def test_explicit_free_provider_opt_in_receives_json_not_a_zero_dividend_claim() -> None:
    async with ProviderHTTP(
        "yahoo", access(credential=None), transport=httpx.MockTransport(lambda _: response())
    ) as client:
        result = await client.get_json("/v8/finance/chart/0005.HK")
    assert result.error is None and result.payload == []
    assert result.failure_state is None


@pytest.mark.parametrize(
    "reference",
    [
        "http://eodhd.com/api/div/0005.HK",
        "https://evil.invalid/api/div/0005.HK",
        "https://eodhd.com.evil.invalid/api/div/0005.HK",
        "https://127.0.0.1/api/div/0005.HK",
        "https://eodhd.com:444/api/div/0005.HK",
        "https://user:password@eodhd.com/api/div/0005.HK",
        "//eodhd.com/api/div/0005.HK",
        "/api/div/0005.HK#fragment",
        "/api/div/../private",
        "/api/div/%2e%2e",
        "/api/div/%2Fprivate",
        "/other/path",
        "relative/path",
        "/api/div/0005.HK\n",
        "/api/div/0005.HK?from=2026&from=2025",
        "/api/div/0005.HK?bad=%ff",
        "/api/div/0005.HK?bad=%0a",
        "/api/div/" + "x" * 4096,
    ],
)
async def test_fixed_origin_and_path_policy_rejects_ssrf_redirect_and_ambiguous_queries_before_io(
    reference: str,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as client:
        result = await client.get_json(reference)
    assert result.error == TransportError.URL_REJECTED
    assert result.failure_state == FetchState.PARSE_ERROR and result.source_url is None
    assert calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"api_token": "caller-key"},
        {"apikey": "caller-key"},
        {"Authorization": "Bearer caller-key"},
        {"bad\nname": "x"},
        {"cursor": "bad\nvalue"},
        {"cursor": "x" * 2049},
        {f"field{number}": "x" for number in range(33)},
        {f"field{number}": "x" * 2048 for number in range(3)},
    ],
)
async def test_unbounded_or_caller_supplied_credential_params_are_not_sent(
    params: dict[str, str],
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response()

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as client:
        result = await client.get_json("/api/div/0005.HK", params=params)
    assert result.error == TransportError.URL_REJECTED and calls == []


@pytest.mark.parametrize(
    "status,error,state",
    [
        (301, TransportError.REDIRECT_REJECTED, FetchState.PARSE_ERROR),
        (302, TransportError.REDIRECT_REJECTED, FetchState.PARSE_ERROR),
        (401, TransportError.AUTH_ERROR, FetchState.AUTH_ERROR),
        (403, TransportError.AUTH_ERROR, FetchState.AUTH_ERROR),
        (404, TransportError.UPSTREAM_ERROR, FetchState.PARSE_ERROR),
        (429, TransportError.RATE_LIMITED, FetchState.RATE_LIMITED),
        (500, TransportError.UPSTREAM_ERROR, FetchState.TRANSIENT_ERROR),
        (503, TransportError.UPSTREAM_ERROR, FetchState.TRANSIENT_ERROR),
    ],
)
async def test_http_failures_do_not_follow_redirects_retry_inline_or_expose_error_bodies(
    status: int,
    error: TransportError,
    state: FetchState,
) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(
            KEY.encode(), status, location="https://evil.invalid/", **{"retry-after": "60"}
        )

    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(handle), clock=lambda: NOW
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert len(calls) == 1 and result.error == error and result.status_code == status
    assert result.failure_state == state
    assert result.payload is None and KEY not in repr(result)
    assert result.retry_at == (
        "2026-11-06T00:01:00.000000Z" if status == 429 or status >= 500 else None
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("60", "2026-11-06T00:01:00.000000Z"),
        ("Fri, 06 Nov 2026 00:02:00 GMT", "2026-11-06T00:02:00.000000Z"),
        ("Thu, 05 Nov 2026 00:00:00 GMT", "2026-11-06T00:00:00.000000Z"),
        ("9" * 80, "9999-12-31T23:59:59.999999Z"),
        (None, None),
        ("nonsense", None),
        ("-1", None),
        ("0.5", None),
        ("9" * 257, "9999-12-31T23:59:59.999999Z"),
        ("x" * 257, None),
        ("Fri, 31 Dec 9999 23:59:59 -0100", "9999-12-31T23:59:59.999999Z"),
        ("Fri, 06 Nov 2026 00:02:00 +2500", None),
    ],
)
def test_retry_after_is_an_absolute_utc_deadline_not_a_truncated_wait(
    value: str | None,
    expected: str | None,
) -> None:
    assert retry_at(value, NOW) == expected


@pytest.mark.parametrize(
    "body",
    [
        b'{"dps":NaN}',
        b'{"dps":Infinity}',
        b'{"dps":-Infinity}',
        b'{"dps":1,"dps":2}',
        b'{"outer":{"dps":1,"dps":2}}',
        b'{"n":' + b"9" * 65 + b"}",
        b"not json",
        b"\xff",
        b"[" * 2000,
        b'{"dps":1e999999999999999999999999}',
    ],
)
async def test_invalid_duplicate_nonfinite_and_unbounded_json_never_becomes_provider_data(
    body: bytes,
) -> None:
    async with ProviderHTTP(
        "eodhd", access(), transport=httpx.MockTransport(lambda _: response(body))
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error == TransportError.INVALID_JSON and result.payload is None
    assert result.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert result.failure_state == FetchState.PARSE_ERROR


@pytest.mark.parametrize("content_type", ["text/html", "text/csv", "", "application/octet-stream"])
async def test_unexpected_content_type_is_not_parsed_or_recorded(content_type: str) -> None:
    async with ProviderHTTP(
        "eodhd",
        access(),
        transport=httpx.MockTransport(lambda _: response(b"[]", **{"content-type": content_type})),
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error == TransportError.INVALID_CONTENT_TYPE
    assert result.raw_sha256 is None and result.payload is None


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity"])
async def test_encoded_json_is_bounded_and_hashed_as_the_exact_decoded_payload(
    encoding: str,
) -> None:
    body = b'[{"dps":0.30000000}]'
    encoded = (
        gzip.compress(body)
        if encoding == "gzip"
        else zlib.compress(body)
        if encoding == "deflate"
        else body
    )
    async with ProviderHTTP(
        "eodhd",
        access(),
        transport=httpx.MockTransport(
            lambda _: response(encoded, **{"content-encoding": encoding})
        ),
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error is None
    assert result.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert isinstance(result.payload, list) and isinstance(result.payload[0], dict)
    assert result.payload[0]["dps"] == Decimal("0.30000000")


@pytest.mark.parametrize(
    "body,headers,error",
    [
        (b'"' + b"x" * 200 + b'"', {}, TransportError.PAYLOAD_TOO_LARGE),
        (
            gzip.compress(b'"' + b"x" * 10000 + b'"'),
            {"content-encoding": "gzip"},
            TransportError.PAYLOAD_TOO_LARGE,
        ),
        (b"bad gzip", {"content-encoding": "gzip"}, TransportError.INVALID_ENCODING),
        (gzip.compress(b"[]")[:-2], {"content-encoding": "gzip"}, TransportError.INVALID_ENCODING),
        (
            gzip.compress(b"[]") + b"extra",
            {"content-encoding": "gzip"},
            TransportError.INVALID_ENCODING,
        ),
        (b"[]", {"content-encoding": "br"}, TransportError.INVALID_ENCODING),
    ],
)
async def test_wire_size_compressed_bombs_and_invalid_encodings_are_closed_failures(
    body: bytes,
    headers: dict[str, str],
    error: TransportError,
) -> None:
    async with ProviderHTTP(
        "eodhd",
        access(),
        maximum_bytes=128,
        transport=httpx.MockTransport(lambda _: response(body, **headers)),
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error == error and result.payload is None
    assert result.failure_state == FetchState.PARSE_ERROR


@pytest.mark.parametrize(
    "exception,error",
    [
        (httpx.ConnectTimeout, TransportError.HTTP_TIMEOUT),
        (httpx.ReadTimeout, TransportError.HTTP_TIMEOUT),
        (httpx.ConnectError, TransportError.NETWORK_ERROR),
    ],
)
async def test_network_failures_return_only_sanitized_codes(
    exception: Any, error: TransportError
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise exception(f"Untrusted exception contains {request.url}", request=request)

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error == error and result.failure_state == FetchState.TRANSIENT_ERROR
    assert KEY not in repr(result) and "api_token" not in repr(result)


class WaitingBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await asyncio.Event().wait()
        yield b"[]"

    async def aclose(self) -> None:
        self.closed = True


async def test_total_deadline_cancels_a_stalled_stream_and_closes_it_without_sleep() -> None:
    body = WaitingBody()
    async with ProviderHTTP(
        "eodhd",
        access(),
        total_timeout=0.01,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=body)
        ),
    ) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert body.started.is_set() and body.closed
    assert result.error == TransportError.HTTP_TIMEOUT and result.status_code == 200


async def test_cancellation_propagates_and_releases_the_provider_response() -> None:
    body = WaitingBody()
    async with ProviderHTTP(
        "eodhd",
        access(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=body)
        ),
    ) as client:
        pending = asyncio.create_task(client.get_json("/api/div/0005.HK"))
        await body.started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert body.closed


async def test_httpx_and_httpcore_debug_logs_do_not_publish_credentials_or_raw_upstream_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handle(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpx").info("Unsafe full URL: %s", request.url)
        logging.getLogger("httpcore.http11").debug("Unsafe upstream headers: %s", KEY)
        return response(json_body)

    json_body = ('{"echo":"' + KEY + '"}').encode()
    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as client:
        result = await client.get_json("/api/div/0005.HK")
    assert result.error is None and KEY not in repr(result)
    logging.getLogger("httpx").info("Unrelated HTTP logging remains available")
    assert "Unrelated HTTP logging remains available" in caplog.text
    assert KEY not in caplog.text and "api_token" not in caplog.text


@pytest.mark.parametrize(
    "maximum,deadline",
    [
        (0, 20),
        (-1, 20),
        (True, 20),
        (9 * 1024 * 1024, 20),
        (1024, 0),
        (1024, -1),
        (1024, 21),
        (1024, float("inf")),
        (1024, float("nan")),
        (1024, True),
    ],
)
def test_transport_resource_limits_cannot_be_disabled(maximum: Any, deadline: Any) -> None:
    with pytest.raises(ValueError):
        ProviderHTTP("eodhd", access(), maximum_bytes=maximum, total_timeout=deadline)


@pytest.mark.parametrize(
    "reference,params",
    [
        (None, {}),
        (123, {}),
        (True, {}),
        ("/api/div/0005.HK", []),
        ("/api/div/0005.HK", {"symbol": 123}),
        ("/api/div/0005.HK", {"symbol": None}),
        ("/api/div/0005.HK", {1: "value"}),
    ],
)
async def test_malformed_upstream_references_and_nonstring_params_fail_closed(
    reference: Any,
    params: Any,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input reached the provider transport")

    async with ProviderHTTP("eodhd", access(), transport=httpx.MockTransport(handle)) as client:
        result = await client.get_json(reference, params=params)
    assert result.error == TransportError.URL_REJECTED and result.source_url is None


def test_server_configuration_is_strict_and_secret_repr_is_redacted() -> None:
    assert KEY not in repr(access()) and KEY not in access().model_dump_json()
    with pytest.raises(ValidationError):
        access(enabled="true")
