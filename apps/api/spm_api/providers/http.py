"""Single-attempt provider HTTP: fixed egress, bounded exact JSON and secret-free outcomes."""

import asyncio
import hashlib
import json
import logging
import math
import re
import zlib
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import TracebackType
from typing import Self, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ConfigDict, Field, SecretStr

from spm_api.audit import now, stamp
from spm_api.errors import Model
from spm_api.master_models import ProviderId
from spm_api.providers.base import FetchState

type JsonValue = None | bool | str | int | Decimal | list[JsonValue] | dict[str, JsonValue]
_ORIGINS = {
    "eodhd": "eodhd.com",
    "alphavantage": "www.alphavantage.co",
    "massive": "api.massive.com",
    "yahoo": "query1.finance.yahoo.com",
}
_PATHS = {
    "eodhd": re.compile(r"/api/(?:div|splits)/[A-Za-z0-9^][A-Za-z0-9.^=_-]{0,63}"),
    "alphavantage": re.compile(r"/query"),
    "massive": re.compile(r"/stocks/v1/(?:dividends|splits)"),
    "yahoo": re.compile(r"/v8/finance/chart/[A-Za-z0-9^][A-Za-z0-9.^=_-]{0,63}"),
}
_CREDENTIAL_NAMES = frozenset(
    {
        "api_token",
        "apikey",
        "api_key",
        "token",
        "access_token",
        "authorization",
        "cookie",
        "session",
        "password",
        "secret",
    }
)
_PROVIDER_LOG_SCOPE: ContextVar[bool] = ContextVar("provider_http_log_scope", default=False)


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs complete request URLs; httpcore debug may include upstream headers/errors.
        return not _PROVIDER_LOG_SCOPE.get()


_LOG_FILTER = _TransportLogFilter()
_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


class ProviderAccess(Model):
    """Trusted server-only configuration, never an API request or response DTO."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=False, strict=True)
    terms_acknowledged: bool = Field(default=False, strict=True)
    credential: SecretStr | None = Field(default=None, repr=False)


class TransportError(StrEnum):
    NO_API_KEY = "NO_API_KEY"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    RIGHTS_NOT_ACKNOWLEDGED = "RIGHTS_NOT_ACKNOWLEDGED"
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL"
    UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"
    URL_REJECTED = "URL_REJECTED"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    HTTP_TIMEOUT = "HTTP_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INVALID_ENCODING = "INVALID_ENCODING"
    INVALID_CONTENT_TYPE = "INVALID_CONTENT_TYPE"
    INVALID_JSON = "INVALID_JSON"


@dataclass(frozen=True)
class TransportResult:
    """Receiving JSON is not a dividend, complete coverage or successful synchronization."""

    error: TransportError | None = None
    status_code: int | None = None
    payload: JsonValue = field(default=None, repr=False)
    raw_sha256: str | None = None
    source_url: str | None = None
    retry_at: str | None = None

    @property
    def failure_state(self) -> FetchState | None:
        if self.error is None:
            return None
        if self.error in {
            TransportError.NO_API_KEY,
            TransportError.PROVIDER_DISABLED,
            TransportError.RIGHTS_NOT_ACKNOWLEDGED,
            TransportError.INVALID_CREDENTIAL,
        }:
            return FetchState.UNCONFIGURED
        if self.error == TransportError.UNSUPPORTED_PROVIDER:
            return FetchState.UNSUPPORTED
        if self.error == TransportError.AUTH_ERROR:
            return FetchState.AUTH_ERROR
        if self.error == TransportError.RATE_LIMITED:
            return FetchState.RATE_LIMITED
        if self.error in {TransportError.HTTP_TIMEOUT, TransportError.NETWORK_ERROR} or (
            self.error == TransportError.UPSTREAM_ERROR and (self.status_code or 0) >= 500
        ):
            return FetchState.TRANSIENT_ERROR
        return FetchState.PARSE_ERROR


class _ResponseRejected(Exception):
    def __init__(self, code: TransportError) -> None:
        self.code = code


def retry_at(value: str | None, instant: datetime) -> str | None:
    """Keep an upstream wait as an absolute UTC deadline; never shorten a huge valid delay."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > 256:
        return (
            stamp(datetime.max.replace(tzinfo=UTC))
            if value.isascii() and value.isdecimal()
            else None
        )
    if re.fullmatch(r"[0-9]+", value):
        try:
            target = instant + timedelta(seconds=int(value))
        except OverflowError:
            target = datetime.max.replace(tzinfo=UTC)
    else:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
        except (ValueError, TypeError, OverflowError):
            return None
    try:
        target = target.astimezone(UTC)
    except OverflowError:
        target = datetime.max.replace(tzinfo=UTC) if target.year > instant.year else instant
    except ValueError:
        return None
    return stamp(max(instant, target))


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Ambiguous duplicate JSON key")
        result[name] = value
    return result


def _integer(value: str) -> int:
    if len(value.lstrip("-")) > 64:
        raise ValueError("Unbounded JSON integer")
    return int(value)


def _nonfinite(value: str) -> JsonValue:
    raise ValueError("Nonfinite JSON numeric token")


def exact_json(body: bytes) -> JsonValue:
    """Never turn a provider's JSON numeric token into a financial Python float."""
    return cast(
        JsonValue,
        json.loads(
            body,
            parse_float=Decimal,
            parse_int=_integer,
            parse_constant=_nonfinite,
            object_pairs_hook=_unique_object,
        ),
    )


async def _body(response: httpx.Response, maximum: int) -> bytes:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise _ResponseRejected(TransportError.INVALID_ENCODING)
    decoder = (
        None if encoding == "identity" else zlib.decompressobj(31 if encoding == "gzip" else 15)
    )
    output = bytearray()
    wire_bytes = 0
    try:
        async for part in response.aiter_raw(chunk_size=65536):
            wire_bytes += len(part)
            if wire_bytes > maximum:
                raise _ResponseRejected(TransportError.PAYLOAD_TOO_LARGE)
            output.extend(decoder.decompress(part, maximum - len(output) + 1) if decoder else part)
            if len(output) > maximum:
                raise _ResponseRejected(TransportError.PAYLOAD_TOO_LARGE)
        if decoder and (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail):
            raise _ResponseRejected(TransportError.INVALID_ENCODING)
    except zlib.error:
        raise _ResponseRejected(TransportError.INVALID_ENCODING) from None
    return bytes(output)


class ProviderHTTP:
    """One bounded attempt. The durable worker, not this client, owns quota/retry policy."""

    def __init__(
        self,
        provider_id: ProviderId,
        access: ProviderAccess,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        maximum_bytes: int = 1024 * 1024,
        total_timeout: float = 20,
        clock: Callable[[], datetime] = now,
    ) -> None:
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 8 * 1024 * 1024:
            raise ValueError("Provider body limit must be between 1 byte and 8 MiB")
        if (
            type(total_timeout) not in {int, float}
            or not math.isfinite(total_timeout)
            or not 0 < total_timeout <= 20
        ):
            raise ValueError("Provider total timeout must be positive and at most 20 seconds")
        self.provider_id = provider_id
        self._access = access
        self._maximum = maximum_bytes
        self._deadline = total_timeout
        self._clock = clock
        for name in _LOGGERS:
            logging.getLogger(name).addFilter(_LOG_FILTER)
        self._client = httpx.AsyncClient(
            verify=True,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(20, connect=5, pool=5),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={
                "User-Agent": "StockPortfolioManager/0.1 (+self-hosted evidence client)",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate, identity",
            },
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        token = _PROVIDER_LOG_SCOPE.set(True)
        try:
            await self._client.__aexit__(exc_type, exc, traceback)
        finally:
            _PROVIDER_LOG_SCOPE.reset(token)

    def configuration_error(self) -> TransportError | None:
        """Check local permission/credential shape, never upstream availability or live coverage."""
        if self.provider_id not in _ORIGINS:
            return TransportError.UNSUPPORTED_PROVIDER
        credential = self._access.credential
        if self.provider_id != "yahoo" and credential is None:
            return TransportError.NO_API_KEY
        if not self._access.enabled:
            return TransportError.PROVIDER_DISABLED
        if not self._access.terms_acknowledged:
            return TransportError.RIGHTS_NOT_ACKNOWLEDGED
        if (
            credential is not None
            and re.fullmatch(r"[\x21-\x7e]{1,256}", credential.get_secret_value()) is None
        ):
            return TransportError.INVALID_CREDENTIAL
        return None

    def _url(self, reference: str, params: Mapping[str, str]) -> httpx.URL:
        if (
            not isinstance(reference, str)
            or not isinstance(params, Mapping)
            or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in params.items()
            )
        ):
            raise ValueError("Provider URL and query values must be strings")
        if len(reference) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in reference):
            raise ValueError("Invalid provider URL")
        host = _ORIGINS[self.provider_id]
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme != "https"
                or parsed.hostname != host
                or parsed.port not in {None, 443}
            ):
                raise ValueError("Provider origin rejected")
        elif not reference.startswith("/") or reference.startswith("//"):
            raise ValueError("Provider reference must use an absolute path")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("Provider authority rejected")
        if _PATHS[self.provider_id].fullmatch(parsed.path) is None:
            raise ValueError("Provider path rejected")
        query: dict[str, str] = {}
        for name, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=32,
            encoding="utf-8",
            errors="strict",
        ):
            if name.casefold() in _CREDENTIAL_NAMES:
                continue
            if name in query:
                raise ValueError("Ambiguous provider query")
            query[name] = value
        if any(name.casefold() in _CREDENTIAL_NAMES for name in params):
            raise ValueError("Credentials must come from server configuration")
        query.update(params)
        if len(query) > 32 or any(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) is None
            or len(value) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for name, value in query.items()
        ):
            raise ValueError("Unbounded provider query")
        credential = self._access.credential
        if self.provider_id in {"eodhd", "alphavantage"} and credential is not None:
            query["api_token" if self.provider_id == "eodhd" else "apikey"] = (
                credential.get_secret_value()
            )
        if self.provider_id == "eodhd":
            query["fmt"] = "json"
        url = httpx.URL(urlunsplit(("https", host, parsed.path, "", "")), params=query)
        if len(str(url)) > 4096:
            raise ValueError("Provider URL exceeds the request bound")
        return url

    def pagination_reference(self, reference: str, *, expected_path: str) -> str:
        """Retain only a validated query-safe relative link, never an upstream credential URL."""
        if self.provider_id not in _ORIGINS:
            raise ValueError("Provider does not support HTTP pagination")
        url = self._url(reference, {})
        if url.path != expected_path:
            raise ValueError("Pagination cannot change the requested operation")
        pairs = [
            (key, value)
            for key, value in url.params.multi_items()
            if key.casefold() not in _CREDENTIAL_NAMES
        ]
        credential = self._access.credential
        if credential is not None and any(
            credential.get_secret_value() in value or credential.get_secret_value() in key
            for key, value in pairs
        ):
            raise ValueError("Pagination reference contains a credential")
        return urlunsplit(("", "", url.path, urlencode(sorted(pairs)), ""))

    async def get_json(
        self, reference: str, *, params: Mapping[str, str] | None = None
    ) -> TransportResult:
        error = self.configuration_error()
        if error:
            return TransportResult(error=error)
        try:
            url = self._url(reference, params if params is not None else {})
        except (ValueError, UnicodeError):
            return TransportResult(error=TransportError.URL_REJECTED)
        source = str(url.copy_with(query=None))
        headers = {}
        if self.provider_id == "massive" and self._access.credential is not None:
            headers["Authorization"] = f"Bearer {self._access.credential.get_secret_value()}"
        status = None
        raw_hash = None
        token = _PROVIDER_LOG_SCOPE.set(True)
        try:
            async with asyncio.timeout(self._deadline):
                async with self._client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        error = TransportError.REDIRECT_REJECTED
                    elif status in {401, 403}:
                        error = TransportError.AUTH_ERROR
                    elif status == 429:
                        error = TransportError.RATE_LIMITED
                    elif status >= 400:
                        error = TransportError.UPSTREAM_ERROR
                    else:
                        content_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if content_type != "application/json" and not content_type.endswith(
                            "+json"
                        ):
                            raise _ResponseRejected(TransportError.INVALID_CONTENT_TYPE)
                        body = await _body(response, self._maximum)
                        raw_hash = hashlib.sha256(body).hexdigest()
                        payload = exact_json(body)
                        return TransportResult(
                            status_code=status,
                            payload=payload,
                            raw_sha256=raw_hash,
                            source_url=source,
                        )
                    return TransportResult(
                        error=error,
                        status_code=status,
                        source_url=source,
                        retry_at=retry_at(response.headers.get("retry-after"), self._clock())
                        if status == 429 or status >= 500
                        else None,
                    )
        except (TimeoutError, httpx.TimeoutException):
            error = TransportError.HTTP_TIMEOUT
        except httpx.RequestError:
            error = TransportError.NETWORK_ERROR
        except _ResponseRejected as rejected:
            error = rejected.code
        except (ValueError, UnicodeError, DecimalException, RecursionError, httpx.StreamError):
            error = TransportError.INVALID_JSON
        finally:
            _PROVIDER_LOG_SCOPE.reset(token)
        return TransportResult(
            error=error, status_code=status, raw_sha256=raw_hash, source_url=source
        )
