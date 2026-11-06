"""EODHD's documented JSON routes produce source evidence, never adopted financial facts."""

import re
from collections.abc import Callable

from pydantic import TypeAdapter

from spm_api.decimal_types import IsoDate
from spm_api.providers.adapters import AdapterHealth, FetchPage, PageMetadata
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
from spm_api.providers.http import JsonValue, ProviderHTTP, TransportResult
from spm_api.providers.symbols import AliasPeriod, AliasSegment
from spm_api.providers.values import dps_string, split_ratio

_DATE = TypeAdapter(IsoDate)
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


def _date(value: JsonValue, *, required: bool = False) -> str | None:
    if value is None or value in ("", "0000-00-00"):
        if required:
            raise ValueError("Provider event needs its mapping-local calendar date")
        return None
    return _DATE.validate_python(value)


def _dividend(row: dict[str, JsonValue], alias: AliasPeriod) -> DividendObservation:
    historical = row.get("unadjustedValue")
    adjusted = row.get("value")
    # Documented historical cash is not proof of declaration/ex-date terms or an owner receipt.
    amount = historical if historical is not None else adjusted
    currency = row.get("currency")
    normalized_currency = currency.strip().upper() if isinstance(currency, str) else None
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
        ex_date=_date(row.get("date"), required=True),
        announce_date=_date(row.get("declarationDate")),
        record_date=_date(row.get("recordDate")),
        pay_date=_date(row.get("paymentDate")),
        dps=dps_string(amount),
        currency="USD"
        if normalized_currency == "USD"
        else "HKD"
        if normalized_currency == "HKD"
        else None,
        event_type=event_type,
        source_currency=normalized_currency
        if normalized_currency is not None and re.fullmatch(r"[A-Z]{3}", normalized_currency)
        else None,
        source_period=_SOURCE_PERIODS.get(period.strip().casefold())
        if isinstance(period, str)
        else None,
        adjustment_basis="UNKNOWN"
        if historical is not None or adjusted is None
        else "SPLIT_ADJUSTED",
    )


def _split(row: dict[str, JsonValue], alias: AliasPeriod) -> SplitObservation:
    first, second = split_ratio(row.get("split"))
    effective_date = _date(row.get("date"), required=True)
    assert effective_date is not None
    return SplitObservation(
        provider_id="eodhd",
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        effective_date=effective_date,
        ratio_numerator=first,
        ratio_denominator=second,
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
        error = self._http.configuration_error()
        state = (
            TransportResult(error=error).failure_state if error is not None else FetchState.STALE
        )
        assert state is not None
        return AdapterHealth(
            provider_id="eodhd",
            configured=error is None,
            state=state,
            reason=error.value if error is not None else "NOT_PROBED",
        )

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
        common: PageMetadata = {
            "window": segment.window,
            "raw_sha256": result.raw_sha256,
            "source_url": result.source_url,
            "http_status": result.status_code,
            "retry_at": result.retry_at,
        }
        if result.error is not None:
            state = result.failure_state
            assert state is not None
            return page(state=state, error_code=result.error.value, **common)
        if not isinstance(result.payload, list):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        if len(result.payload) > 10000:
            return page(state=FetchState.PARSE_ERROR, error_code="PROVIDER_ROW_LIMIT", **common)
        rows: list[T] = []
        seen = set()
        warnings = set() if segment.alias.verified else {"UNVERIFIED_PROVIDER_ALIAS"}
        issues = 0
        for item in result.payload:
            if not isinstance(item, dict):
                issues += 1
                warnings.add("MALFORMED_PROVIDER_ROW")
                continue
            try:
                event_date = _date(item.get("date"), required=True)
                assert event_date is not None
                if not segment.window.start <= event_date <= segment.window.end:
                    issues += 1
                    warnings.add("OUTSIDE_MAPPING_WINDOW")
                    continue
                observation = parse(item, segment.alias)
            except ValueError:
                issues += 1
                warnings.add("MALFORMED_PROVIDER_ROW")
                continue
            fingerprint = observation.normalized_sha256
            if fingerprint in seen:
                issues += 1
                warnings.add("DUPLICATE_SOURCE_OBSERVATION")
                continue
            seen.add(fingerprint)
            rows.append(observation)
            if isinstance(observation, DividendObservation):
                warnings.update(observation.incomplete_reasons)
                if item.get("currency") is not None and observation.currency is None:
                    warnings.add("UNSUPPORTED_EVENT_CURRENCY")
                if item.get("unadjustedValue") is not None:
                    warnings.add("HISTORICAL_PER_SHARE_BASIS_UNRESOLVED")
            else:
                warnings.add("PROVIDER_ADJUSTMENT_ONLY")
        return page(
            state=FetchState.OK_WITH_DATA
            if rows
            else FetchState.PARSE_ERROR
            if issues
            else FetchState.NO_DATA,
            rows=tuple(rows),
            complete=issues == 0,
            issue_count=issues,
            warnings=tuple(sorted(warnings)),
            error_code="PARTIAL_PROVIDER_PAGE" if issues else None,
            **common,
        )
