"""Shared deterministic adapter normalization; no network, retries or ledger writes."""

import re
from collections.abc import Callable, Iterable

from pydantic import TypeAdapter

from spm_api.decimal_types import IsoDate
from spm_api.master_models import Currency
from spm_api.providers.adapters import AdapterHealth, FetchPage, PageMetadata
from spm_api.providers.base import DividendObservation, FetchState, SourceObservation
from spm_api.providers.http import JsonValue, ProviderHTTP, TransportResult
from spm_api.providers.symbols import AliasPeriod, AliasSegment

_DATE = TypeAdapter(IsoDate)


def source_date(value: JsonValue, *, required: bool = False) -> str | None:
    if value is None or value in ("", "0000-00-00"):
        if required:
            raise ValueError("Provider event needs its mapping-local calendar date")
        return None
    return _DATE.validate_python(value)


def source_currency(value: JsonValue) -> tuple[Currency | None, str | None]:
    code = value.strip().upper() if isinstance(value, str) else None
    supported: Currency | None = "HKD" if code == "HKD" else "USD" if code == "USD" else None
    return supported, code if code is not None and re.fullmatch(r"[A-Z]{3}", code) else None


def page_metadata(segment: AliasSegment, result: TransportResult) -> PageMetadata:
    return {
        "window": segment.window,
        "raw_sha256": result.raw_sha256,
        "source_url": result.source_url,
        "http_status": result.status_code,
        "retry_at": result.retry_at,
    }


def configuration_health(transport: ProviderHTTP) -> AdapterHealth:
    error = transport.configuration_error()
    state = TransportResult(error=error).failure_state if error is not None else FetchState.STALE
    assert state is not None
    return AdapterHealth(
        provider_id=transport.provider_id,
        configured=error is None,
        state=state,
        reason=error.value if error is not None else "NOT_PROBED",
    )


def parsed_page[T: SourceObservation](
    segment: AliasSegment,
    result: TransportResult,
    records: JsonValue,
    parse: Callable[[dict[str, JsonValue], AliasPeriod], T],
    page: type[FetchPage[T]],
    *,
    date_key: str,
    next_cursor: str | None = None,
    issues: int = 0,
    allow_outside_window: bool = False,
    initial_warnings: Iterable[str] = (),
    row_warnings: Callable[[dict[str, JsonValue], T], Iterable[str]] | None = None,
) -> FetchPage[T]:
    common = page_metadata(segment, result)
    if result.error is not None:
        state = result.failure_state
        assert state is not None
        return page(state=state, error_code=result.error.value, **common)
    if not isinstance(records, list):
        return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
    if len(records) > 10000:
        return page(state=FetchState.PARSE_ERROR, error_code="PROVIDER_ROW_LIMIT", **common)
    rows: list[T] = []
    filtered = 0
    seen = set()
    warnings = set(initial_warnings)
    if not segment.alias.verified:
        warnings.add("UNVERIFIED_PROVIDER_ALIAS")
    for item in records:
        if not isinstance(item, dict):
            issues += 1
            warnings.add("MALFORMED_PROVIDER_ROW")
            continue
        try:
            event_date = source_date(item.get(date_key), required=True)
            assert event_date is not None
            if not segment.window.start <= event_date <= segment.window.end:
                if allow_outside_window:
                    filtered += 1
                    warnings.add("OUTSIDE_MAPPING_WINDOW_IGNORED")
                else:
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
        else:
            warnings.add("PROVIDER_ADJUSTMENT_ONLY")
        if row_warnings is not None:
            warnings.update(row_warnings(item, observation))
    return page(
        state=FetchState.OK_WITH_DATA
        if rows
        else FetchState.PARSE_ERROR
        if issues
        else FetchState.NO_DATA,
        rows=tuple(rows),
        complete=issues == 0,
        issue_count=issues,
        filtered_count=filtered,
        warnings=tuple(sorted(warnings)),
        error_code="PARTIAL_PROVIDER_PAGE" if issues else None,
        next_cursor=next_cursor,
        **common,
    )
