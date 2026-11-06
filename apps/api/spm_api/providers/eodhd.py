"""EODHD's documented JSON routes produce source evidence, never adopted financial facts."""

import re
from collections.abc import Callable

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
    parsed_page,
    source_currency,
    source_date,
)
from spm_api.providers.symbols import AliasPeriod, AliasSegment
from spm_api.providers.values import dps_string, split_ratio

_PERIOD_TYPES: dict[str, EventType] = {
    "interim": "INTERIM",
    "final": "FINAL",
    "special": "SPECIAL",
    "supplemental": "SUPPLEMENTAL",
}
_SOURCE_PERIODS: dict[str, SourcePeriod] = {
    "annual": "ANNUAL",
    "quarterly": "QUARTERLY",
    "monthly": "MONTHLY",
    "weekly": "WEEKLY",
    "interim": "INTERIM",
    "final": "FINAL",
    "special": "SPECIAL",
    "supplemental": "SUPPLEMENTAL",
    "other": "OTHER",
    "other payments": "OTHER",
}


def _dividend(row: dict[str, JsonValue], alias: AliasPeriod) -> DividendObservation:
    historical = row.get("unadjustedValue")
    adjusted = row.get("value")
    # Documented historical cash is not proof of declaration/ex-date terms or an owner receipt.
    amount = historical if historical is not None else adjusted
    currency, original_currency = source_currency(row.get("currency"))
    period = row.get("period")
    event_type = (
        _PERIOD_TYPES.get(period.strip().casefold(), "UNKNOWN")
        if isinstance(period, str)
        else "UNKNOWN"
    )
    return DividendObservation(
        provider_id="eodhd",
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        ex_date=source_date(row.get("date"), required=True),
        announce_date=source_date(row.get("declarationDate")),
        record_date=source_date(row.get("recordDate")),
        pay_date=source_date(row.get("paymentDate")),
        dps=dps_string(amount),
        currency=currency,
        event_type=event_type,
        source_currency=original_currency,
        source_period=_SOURCE_PERIODS.get(period.strip().casefold())
        if isinstance(period, str)
        else None,
        adjustment_basis="UNKNOWN"
        if historical is not None or adjusted is None
        else "SPLIT_ADJUSTED",
    )


def _split(row: dict[str, JsonValue], alias: AliasPeriod) -> SplitObservation:
    first, second = split_ratio(row.get("split"))
    effective_date = source_date(row.get("date"), required=True)
    assert effective_date is not None
    return SplitObservation(
        provider_id="eodhd",
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        effective_date=effective_date,
        ratio_numerator=first,
        ratio_denominator=second,
    )


def _warnings(row: dict[str, JsonValue], observation: SourceObservation) -> tuple[str, ...]:
    return (
        ("HISTORICAL_PER_SHARE_BASIS_UNRESOLVED",)
        if (isinstance(observation, DividendObservation) and row.get("unadjustedValue") is not None)
        else ()
    )


class EodhdAdapter:
    """Each operation costs one explicit transport call; the caller owns leases/quota/retry."""

    def __init__(self, transport: ProviderHTTP) -> None:
        if transport.provider_id != "eodhd":
            raise ValueError("EODHD adapter requires its own fixed-origin transport")
        self._http = transport

    def get_capabilities(self) -> ProviderDefinition:
        return registry()["eodhd"]

    async def healthcheck(self) -> AdapterHealth:
        return configuration_health(self._http)

    async def fetch_dividends(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[DividendObservation]:
        return await self._fetch(segment, cursor, "div", _dividend, FetchPage[DividendObservation])

    async def fetch_splits(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[SplitObservation]:
        return await self._fetch(segment, cursor, "splits", _split, FetchPage[SplitObservation])

    async def _fetch[T: SourceObservation](
        self,
        segment: AliasSegment,
        cursor: str | None,
        resource: str,
        parse: Callable[[dict[str, JsonValue], AliasPeriod], T],
        page: type[FetchPage[T]],
    ) -> FetchPage[T]:
        if segment.alias.provider_id != "eodhd":
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="ALIAS_PROVIDER_MISMATCH",
            )
        if re.fullmatch(r".+\.[A-Za-z0-9_-]{1,8}", segment.alias.provider_symbol) is None:
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="EXPLICIT_EXCHANGE_ALIAS_REQUIRED",
            )
        # The documented EODHD routes return a single array, not an invented pagination API.
        if cursor is not None:
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="UNEXPECTED_PROVIDER_CURSOR",
            )
        result = await self._http.get_json(
            f"/api/{resource}/{segment.alias.provider_symbol}",
            params={"from": segment.window.start, "to": segment.window.end},
        )
        return parsed_page(
            segment, result, result.payload, parse, page, date_key="date", row_warnings=_warnings
        )
