"""Massive's documented v1 cash distributions and provider-only split factors, one page per call."""

from collections.abc import Callable
from urllib.parse import parse_qsl, urlsplit

from spm_api.providers.adapters import AdapterHealth, FetchPage
from spm_api.providers.base import (
    DividendObservation,
    EventType,
    FetchState,
    ProviderDefinition,
    SourceObservation,
    SourcePeriod,
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
from spm_api.providers.values import dps_string, ratio_terms

_DISTRIBUTIONS: dict[str, EventType] = {
    "recurring": "REGULAR",
    "special": "SPECIAL",
    "supplemental": "SUPPLEMENTAL",
}
_PERIODS: dict[int, SourcePeriod] = {1: "ANNUAL", 4: "QUARTERLY", 12: "MONTHLY", 52: "WEEKLY"}


def _identity(row: dict[str, JsonValue], alias: AliasPeriod) -> str | None:
    ticker = row.get("ticker")
    if not isinstance(ticker, str) or ticker.casefold() != alias.provider_symbol.casefold():
        raise ValueError("Provider row is for a different explicit identity")
    identifier = row.get("id")
    if identifier is not None and not isinstance(identifier, str):
        raise ValueError("Provider event ID must be opaque text")
    return identifier or None


def _dividend(row: dict[str, JsonValue], alias: AliasPeriod) -> DividendObservation:
    identifier = _identity(row, alias)
    historical, adjusted = row.get("cash_amount"), row.get("split_adjusted_cash_amount")
    currency, original = source_currency(row.get("currency"))
    distribution = row.get("distribution_type")
    frequency = row.get("frequency")
    if frequency is not None and (type(frequency) is not int or not 0 <= frequency <= 365):
        raise ValueError("Provider frequency must be a bounded integer")
    assert frequency is None or isinstance(frequency, int)
    return DividendObservation(
        provider_id="massive",
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        provider_event_id=identifier,
        ex_date=source_date(row.get("ex_dividend_date"), required=True),
        announce_date=source_date(row.get("declaration_date")),
        record_date=source_date(row.get("record_date")),
        pay_date=source_date(row.get("pay_date")),
        dps=dps_string(historical if historical is not None else adjusted),
        currency=currency,
        source_currency=original,
        source_frequency=frequency,
        source_period=_PERIODS.get(frequency) if frequency is not None else None,
        event_type=_DISTRIBUTIONS.get(distribution, "UNKNOWN")
        if isinstance(distribution, str)
        else "UNKNOWN",
        adjustment_basis="UNKNOWN"
        if historical is not None or adjusted is None
        else "SPLIT_ADJUSTED",
    )


def _split(row: dict[str, JsonValue], alias: AliasPeriod) -> SplitObservation:
    identifier = _identity(row, alias)
    first, second = ratio_terms(row.get("split_to"), row.get("split_from"))
    effective_date = source_date(row.get("execution_date"), required=True)
    assert effective_date is not None
    raw_type = row.get("adjustment_type")
    return SplitObservation(
        provider_id="massive",
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        provider_event_id=identifier,
        effective_date=effective_date,
        ratio_numerator=first,
        ratio_denominator=second,
        source_adjustment_type="FORWARD_SPLIT"
        if raw_type == "forward_split"
        else "REVERSE_SPLIT"
        if raw_type == "reverse_split"
        else "STOCK_DIVIDEND"
        if raw_type == "stock_dividend"
        else None,
    )


def _warnings(row: dict[str, JsonValue], observation: SourceObservation) -> tuple[str, ...]:
    if isinstance(observation, DividendObservation):
        return ("ORIGINAL_CASH_BASIS_UNRESOLVED",) if row.get("cash_amount") is not None else ()
    if isinstance(observation, SplitObservation):
        if observation.source_adjustment_type == "STOCK_DIVIDEND":
            return ("STOCK_DIVIDEND_REQUIRES_REVIEW",)
        if (
            observation.source_adjustment_type == "FORWARD_SPLIT"
            and observation.ratio_numerator <= observation.ratio_denominator
        ) or (
            observation.source_adjustment_type == "REVERSE_SPLIT"
            and observation.ratio_numerator >= observation.ratio_denominator
        ):
            return ("PROVIDER_FACTOR_DIRECTION_CONFLICT",)
    return ()


class MassiveAdapter:
    def __init__(self, transport: ProviderHTTP) -> None:
        if transport.provider_id != "massive":
            raise ValueError("Massive adapter requires its own fixed-origin transport")
        self._http = transport

    def get_capabilities(self) -> ProviderDefinition:
        return registry()["massive"]

    async def healthcheck(self) -> AdapterHealth:
        return configuration_health(self._http)

    async def fetch_dividends(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[DividendObservation]:
        return await self._fetch(
            segment,
            cursor,
            "dividends",
            "ex_dividend_date",
            _dividend,
            FetchPage[DividendObservation],
        )

    async def fetch_splits(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[SplitObservation]:
        return await self._fetch(
            segment, cursor, "splits", "execution_date", _split, FetchPage[SplitObservation]
        )

    def _cursor(self, reference: str, path: str, segment: AliasSegment, date_key: str) -> str:
        safe = self._http.pagination_reference(reference, expected_path=path)
        for key, value in parse_qsl(urlsplit(safe).query):
            if key == "ticker" and value.casefold() != segment.alias.provider_symbol.casefold():
                raise ValueError("Pagination changes the explicit ticker")
            if key.startswith("ticker."):
                raise ValueError("Pagination changes ticker scope")
            if key.startswith(date_key) and (key, value) not in {
                (f"{date_key}.gte", segment.window.start),
                (f"{date_key}.lte", segment.window.end),
            }:
                raise ValueError("Pagination changes the requested date scope")
        return safe

    async def _fetch[T: SourceObservation](
        self,
        segment: AliasSegment,
        cursor: str | None,
        resource: str,
        date_key: str,
        parse: Callable[[dict[str, JsonValue], AliasPeriod], T],
        page: type[FetchPage[T]],
    ) -> FetchPage[T]:
        if segment.alias.provider_id != "massive":
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="ALIAS_PROVIDER_MISMATCH",
            )
        if segment.market != "US":
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="MARKET_CONTEXT_REQUIRED"
                if segment.market is None
                else "UNSUPPORTED_MARKET",
            )
        path = f"/stocks/v1/{resource}"
        try:
            current = self._cursor(cursor, path, segment, date_key) if cursor is not None else path
        except ValueError:
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="PAGINATION_REJECTED",
            )
        params = (
            {}
            if cursor is not None
            else {
                "ticker": segment.alias.provider_symbol,
                f"{date_key}.gte": segment.window.start,
                f"{date_key}.lte": segment.window.end,
                "limit": "1000",
                "sort": f"{date_key}.asc",
            }
        )
        result = await self._http.get_json(current, params=params)
        if result.error is not None:
            return parsed_page(segment, result, None, parse, page, date_key=date_key)
        common = page_metadata(segment, result)
        payload = result.payload
        if not isinstance(payload, dict):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        status = payload.get("status")
        if not isinstance(status, str):
            return page(
                state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_STATUS", **common
            )
        if status != "OK":
            state = (
                FetchState.AUTH_ERROR
                if status in {"NOT_AUTHORIZED", "UNAUTHORIZED"}
                else (
                    FetchState.RATE_LIMITED
                    if status == "RATE_LIMITED"
                    else FetchState.UNSUPPORTED
                    if status in {"NOT_FOUND", "UNSUPPORTED"}
                    else FetchState.PARSE_ERROR
                )
            )
            return page(state=state, error_code="PROVIDER_ERROR_RESPONSE", **common)
        next_url = payload.get("next_url")
        next_cursor = None
        issues = 0
        warnings: tuple[str, ...] = ()
        if next_url is not None:
            try:
                if not isinstance(next_url, str) or not next_url:
                    raise ValueError("Invalid continuation reference")
                next_cursor = self._cursor(next_url, path, segment, date_key)
                if next_cursor == current:
                    raise ValueError("Repeated continuation reference")
            except ValueError:
                issues = 1
                next_cursor = None
                warnings = ("PAGINATION_REJECTED",)
        return parsed_page(
            segment,
            result,
            payload.get("results"),
            parse,
            page,
            date_key=date_key,
            next_cursor=next_cursor,
            issues=issues,
            initial_warnings=warnings,
            row_warnings=_warnings,
        )
