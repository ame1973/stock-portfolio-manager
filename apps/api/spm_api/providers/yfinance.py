"""Optional pinned yfinance action parser over the bounded Yahoo transport, never its HTTP stack."""

import warnings
from calendar import timegm
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal
from zoneinfo import ZoneInfo

from spm_api.providers.adapters import AdapterHealth
from spm_api.providers.base import FetchState
from spm_api.providers.http import JsonValue, ProviderHTTP
from spm_api.providers.values import dps_string, ratio_terms
from spm_api.providers.yahoo import YahooAdapter

SDK_VERSION = "1.8.0"


class YfinanceAdapter(YahooAdapter):
    """SDK parsing is optional; fixed-origin I/O and exact source assertions remain mandatory."""

    _provider_id: Literal["yfinance"] = "yfinance"

    def __init__(self, transport: ProviderHTTP) -> None:
        super().__init__(transport)
        self._parser: Callable[[dict[str, object]], Any] | None = None

    def _readiness_error(self) -> str | None:
        error = self._http.configuration_error()
        if error is not None:
            return error.value
        try:
            installed = version("yfinance")
        except PackageNotFoundError:
            return "SDK_NOT_INSTALLED"
        except (OSError, ValueError):
            return "SDK_METADATA_ERROR"
        if installed != SDK_VERSION:
            return "SDK_VERSION_UNSUPPORTED"
        if self._parser is None:
            try:
                # Importing the SDK changes warning filters; do not leak that process-wide state.
                with warnings.catch_warnings():
                    module = import_module("yfinance.utils")
                parser = getattr(module, "parse_actions", None)
                if not callable(parser):
                    return "SDK_PARSER_UNAVAILABLE"
                self._parser = parser
            except Exception:
                return "SDK_IMPORT_ERROR"
        return None

    async def healthcheck(self) -> AdapterHealth:
        error = self._readiness_error()
        return AdapterHealth(
            provider_id="yfinance",
            configured=error is None,
            state=FetchState.UNCONFIGURED if error is not None else FetchState.STALE,
            reason=error if error is not None else "SDK_PARSER_READY_NOT_PROBED",
        )

    def _event_records(
        self, records: dict[str, JsonValue], zone: ZoneInfo, resource: str
    ) -> list[JsonValue]:
        original = super()._event_records(records, zone, resource)
        source: dict[str, dict[str, JsonValue]] = {}
        sdk_events: dict[str, dict[str, object]] = {}
        normalized: list[JsonValue] = []
        for item in original:
            if not isinstance(item, dict):
                normalized.append(None)
                continue
            key = item["_event_key"]
            assert isinstance(key, str)
            try:
                if resource == "dividends":
                    amount = dps_string(item.get("amount"))
                    value: dict[str, object] = {
                        "date": item["date"],
                        "amount": Decimal(amount) if amount is not None else None,
                    }
                    if isinstance(item.get("currency"), str):
                        value["currency"] = item["currency"]
                else:
                    first, second = ratio_terms(item.get("numerator"), item.get("denominator"))
                    # Fraction objects prevent pandas/SDK division from producing a binary float.
                    value = {
                        "date": item["date"],
                        "numerator": Fraction(first),
                        "denominator": Fraction(second),
                    }
            except ValueError:
                normalized.append(None)
                continue
            source[key] = item
            sdk_events[key] = value
        if not source:
            return normalized
        if self._parser is None:
            raise ValueError("SDK parser was not initialized")
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                frames = self._parser({"events": {resource: sdk_events}})
                if not isinstance(frames, tuple) or len(frames) != 3:
                    raise ValueError("Unexpected SDK action tuple")
                frame = frames[0 if resource == "dividends" else 1]
                if len(frame) > 10000:
                    raise ValueError("SDK output exceeded the bounded source page")
                # iterrows upcasts all-null/mixed rows; tuples preserve each column's exact objects.
                columns = tuple(frame.columns)
                rows = [
                    (values[0], dict(zip(columns, values[1:], strict=True)))
                    for values in frame.itertuples(index=True, name=None)
                ]
            if captured:
                raise ValueError("SDK warnings require review")
        except Exception:
            raise ValueError("SDK action normalization failed") from None
        seen = set()
        for index, row in rows:
            try:
                if getattr(index, "nanosecond", 0):
                    raise ValueError("SDK action date lost integer-second precision")
                instant = index.to_pydatetime()
                if not isinstance(instant, datetime) or instant.microsecond:
                    raise ValueError("SDK action date lost integer-second precision")
                instant = instant.replace(tzinfo=UTC) if instant.tzinfo is None else instant
                key = str(timegm(instant.utctimetuple()))
                if key not in source or key in seen:
                    raise ValueError("SDK action changed or duplicated the source identity")
                seen.add(key)
                item = source[key]
                if resource == "dividends":
                    amount = dps_string(row.get("Dividends"))
                    if amount != dps_string(item.get("amount")):
                        raise ValueError("SDK dividend changed the exact source amount")
                    currency = item.get("currency")
                    if isinstance(currency, str) and currency and row.get("currency") != currency:
                        raise ValueError("SDK dividend changed the explicit source currency")
                    normalized.append({**item, "amount": amount})
                else:
                    factor = row.get("Stock Splits")
                    expected = ratio_terms(item.get("numerator"), item.get("denominator"))
                    if not isinstance(factor, Fraction) or factor != Fraction(*expected):
                        raise ValueError("SDK split lost the exact rational factor")
                    normalized.append(
                        {
                            **item,
                            "numerator": factor.numerator,
                            "denominator": factor.denominator,
                        }
                    )
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                normalized.append(None)
        normalized.extend(None for key in source if key not in seen)
        return normalized
