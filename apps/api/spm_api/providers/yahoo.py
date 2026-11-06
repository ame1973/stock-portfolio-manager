"""Opt-in Yahoo chart compatibility: exchange-local evidence, never authoritative income."""

from calendar import timegm
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from spm_api.providers.adapters import AdapterHealth, FetchPage
from spm_api.providers.base import (
    DividendObservation,
    FetchState,
    ProviderDefinition,
    SourceObservation,
    SplitObservation,
    registry,
)
from spm_api.providers.http import JsonValue, ProviderHTTP
from spm_api.providers.parsing import (
    configuration_health,
    page_metadata,
    parsed_page,
    source_currency,
    source_date,
)
from spm_api.providers.symbols import AliasPeriod, AliasSegment
from spm_api.providers.values import dps_string, ratio_terms, split_ratio

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_BUSINESS_STATES = {
    "not found": FetchState.UNSUPPORTED,
    "unauthorized": FetchState.AUTH_ERROR,
    "forbidden": FetchState.AUTH_ERROR,
    "too many requests": FetchState.RATE_LIMITED,
    "service unavailable": FetchState.TRANSIENT_ERROR,
    "internal server error": FetchState.TRANSIENT_ERROR,
}


def _dated_events(events: dict[str, JsonValue], zone: ZoneInfo) -> list[JsonValue]:
    records: list[JsonValue] = []
    for key, item in events.items():
        if not isinstance(item, dict):
            records.append(None)
            continue
        instant = item.get("date")
        if type(instant) is not int or key != str(instant):
            records.append(None)
            continue
        try:
            local_date = (_EPOCH + timedelta(seconds=instant)).astimezone(zone).date().isoformat()
        except (OverflowError, ValueError):
            records.append(None)
            continue
        records.append({**item, "_event_key": key, "_event_date": local_date})
    return records


def _dividend(row: dict[str, JsonValue], alias: AliasPeriod) -> DividendObservation:
    identifier = row["_event_key"]
    assert isinstance(identifier, str)
    currency, original = source_currency(row.get("currency"))
    return DividendObservation(
        provider_id=alias.provider_id,
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        provider_event_id=identifier,
        ex_date=source_date(row["_event_date"], required=True),
        dps=dps_string(row.get("amount")),
        currency=currency,
        source_currency=original,
        adjustment_basis="UNKNOWN",
    )


def _split(row: dict[str, JsonValue], alias: AliasPeriod) -> SplitObservation:
    identifier = row["_event_key"]
    assert isinstance(identifier, str)
    terms = ratio_terms(row.get("numerator"), row.get("denominator"))
    ratio = row.get("splitRatio")
    if ratio is not None and (
        not isinstance(ratio, str) or split_ratio(ratio.replace(":", "/")) != terms
    ):
        raise ValueError("Conflicting provider split terms")
    effective_date = source_date(row["_event_date"], required=True)
    assert effective_date is not None
    return SplitObservation(
        provider_id=alias.provider_id,
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        provider_event_id=identifier,
        effective_date=effective_date,
        ratio_numerator=terms[0],
        ratio_denominator=terms[1],
    )


class YahooAdapter:
    """One chart request per operation; no cookies/crumb negotiation or hidden fallback."""

    _provider_id: Literal["yahoo", "yfinance"] = "yahoo"

    def __init__(self, transport: ProviderHTTP) -> None:
        if transport.provider_id != "yahoo":
            raise ValueError("Yahoo adapter requires its own fixed-origin transport")
        self._http = transport

    def get_capabilities(self) -> ProviderDefinition:
        return registry()[self._provider_id]

    async def healthcheck(self) -> AdapterHealth:
        return configuration_health(self._http)

    async def fetch_dividends(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[DividendObservation]:
        return await self._fetch(
            segment, cursor, "dividends", _dividend, FetchPage[DividendObservation]
        )

    async def fetch_splits(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[SplitObservation]:
        return await self._fetch(segment, cursor, "splits", _split, FetchPage[SplitObservation])

    def _readiness_error(self) -> str | None:
        return None

    def _event_records(
        self, records: dict[str, JsonValue], zone: ZoneInfo, resource: str
    ) -> list[JsonValue]:
        return _dated_events(records, zone)

    async def _fetch[T: SourceObservation](
        self,
        segment: AliasSegment,
        cursor: str | None,
        resource: str,
        parse: Callable[[dict[str, JsonValue], AliasPeriod], T],
        page: type[FetchPage[T]],
    ) -> FetchPage[T]:
        if segment.alias.provider_id != self._provider_id:
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="ALIAS_PROVIDER_MISMATCH",
            )
        if segment.market is None:
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="MARKET_CONTEXT_REQUIRED",
            )
        if cursor is not None:
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="UNEXPECTED_PROVIDER_CURSOR",
            )
        if segment.exchange_timezone is None:
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="EXCHANGE_TIMEZONE_REQUIRED",
            )
        try:
            zone = ZoneInfo(segment.exchange_timezone)
        except (ValueError, ZoneInfoNotFoundError):
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="INVALID_EXCHANGE_TIMEZONE",
            )
        try:
            first = datetime.combine(date.fromisoformat(segment.window.start), time.min, zone)
            after = datetime.combine(
                date.fromisoformat(segment.window.end) + timedelta(days=1), time.min, zone
            )
            params = {
                "period1": str(timegm(first.utctimetuple())),
                "period2": str(timegm(after.utctimetuple())),
                "interval": "1d",
                "events": "div" if resource == "dividends" else "splits",
            }
        except (OverflowError, ValueError):
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="UNREPRESENTABLE_FETCH_WINDOW",
            )
        readiness = self._readiness_error()
        if readiness is not None:
            return page(state=FetchState.UNCONFIGURED, window=segment.window, error_code=readiness)
        result = await self._http.get_json(
            f"/v8/finance/chart/{segment.alias.provider_symbol}", params=params
        )
        if result.error is not None:
            return parsed_page(segment, result, None, parse, page, date_key="_event_date")
        common = page_metadata(segment, result)
        root = result.payload
        chart = root.get("chart") if isinstance(root, dict) else None
        if not isinstance(chart, dict):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        error = chart.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            state = (
                _BUSINESS_STATES.get(code[:256].casefold(), FetchState.PARSE_ERROR)
                if isinstance(code, str)
                else FetchState.PARSE_ERROR
            )
            return page(state=state, error_code="PROVIDER_ERROR_RESPONSE", **common)
        results = chart.get("result")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        data = results[0]
        assert isinstance(root, dict)
        if any(
            container.get(name) is not None
            for container in (root, chart, data)
            for name in ("next_url", "next_cursor")
        ):
            return page(
                state=FetchState.PARSE_ERROR, error_code="UNDOCUMENTED_PAGINATION", **common
            )
        meta = data.get("meta")
        symbol = meta.get("symbol") if isinstance(meta, dict) else None
        if (
            not isinstance(symbol, str)
            or symbol.casefold() != segment.alias.provider_symbol.casefold()
        ):
            return page(
                state=FetchState.PARSE_ERROR, error_code="RESPONSE_SYMBOL_MISMATCH", **common
            )
        assert isinstance(meta, dict)
        reported_zone = meta.get("exchangeTimezoneName")
        if reported_zone is not None and reported_zone != segment.exchange_timezone:
            return page(
                state=FetchState.PARSE_ERROR, error_code="RESPONSE_TIMEZONE_MISMATCH", **common
            )
        events = data.get("events", {})
        if not isinstance(events, dict):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        records = events.get(resource, {})
        if not isinstance(records, dict):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        if len(records) > 10000:
            return page(state=FetchState.PARSE_ERROR, error_code="PROVIDER_ROW_LIMIT", **common)
        warnings = ["YAHOO_COMPATIBILITY_ONLY"]
        if reported_zone is None:
            warnings.append("SOURCE_TIMEZONE_ABSENT")
        if "events" not in data or resource not in events:
            warnings.append("YAHOO_EVENTS_ABSENT")
        if resource == "dividends":
            warnings.append("YAHOO_ADJUSTMENT_BASIS_UNDOCUMENTED")
            if events.get("capitalGains"):
                warnings.append("CAPITAL_GAINS_NOT_DIVIDEND_EVIDENCE")
        if self._provider_id == "yfinance":
            warnings.append("YFINANCE_PARSER_ONLY")
        try:
            normalized = self._event_records(records, zone, resource)
        except ValueError:
            return page(
                state=FetchState.PARSE_ERROR, error_code="PROVIDER_NORMALIZATION_ERROR", **common
            )
        return parsed_page(
            segment,
            result,
            normalized,
            parse,
            page,
            date_key="_event_date",
            initial_warnings=warnings,
        )
