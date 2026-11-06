"""Alpha Vantage dividend evidence with explicit HTTP-200 business failures and unknown basis."""

from spm_api.providers.adapters import AdapterHealth, FetchPage
from spm_api.providers.base import (
    DividendObservation,
    FetchState,
    ProviderDefinition,
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
from spm_api.providers.values import dps_string


def _dividend(row: dict[str, JsonValue], alias: AliasPeriod) -> DividendObservation:
    amount, compatible_amount = row.get("amount"), row.get("dividend_per_share")
    if (
        amount is not None
        and compatible_amount is not None
        and dps_string(amount) != dps_string(compatible_amount)
    ):
        raise ValueError("Conflicting provider amount fields")
    currency, original = source_currency(row.get("currency"))
    return DividendObservation(
        provider_id="alphavantage",
        security_id=alias.security_id,
        provider_symbol=alias.provider_symbol,
        ex_date=source_date(row.get("ex_dividend_date"), required=True),
        announce_date=source_date(row.get("declaration_date")),
        record_date=source_date(row.get("record_date")),
        pay_date=source_date(row.get("payment_date")),
        dps=dps_string(amount if amount is not None else compatible_amount),
        currency=currency,
        source_currency=original,
        adjustment_basis="UNKNOWN",
    )


def _business_error(payload: dict[str, JsonValue]) -> tuple[FetchState, str] | None:
    messages = [payload[key] for key in ("Error Message", "Note", "Information") if key in payload]
    if not messages:
        return None
    text = " ".join(value[:4096].casefold() for value in messages if isinstance(value, str))
    if any(
        word in text
        for word in ("rate limit", "request limit", "call frequency", "requests per", "throttl")
    ):
        return FetchState.RATE_LIMITED, "PROVIDER_RATE_LIMITED"
    if any(
        word in text
        for word in ("api key", "apikey", "premium", "subscription", "permission", "entitlement")
    ):
        return FetchState.AUTH_ERROR, "PROVIDER_PERMISSION_ERROR"
    if "symbol" in text and any(word in text for word in ("not found", "invalid", "unsupported")):
        return FetchState.UNSUPPORTED, "PROVIDER_SYMBOL_UNSUPPORTED"
    return FetchState.PARSE_ERROR, "PROVIDER_ERROR_RESPONSE"


class AlphaVantageAdapter:
    def __init__(self, transport: ProviderHTTP) -> None:
        if transport.provider_id != "alphavantage":
            raise ValueError("Alpha Vantage adapter requires its own fixed-origin transport")
        self._http = transport

    def get_capabilities(self) -> ProviderDefinition:
        return registry()["alphavantage"]

    async def healthcheck(self) -> AdapterHealth:
        return configuration_health(self._http)

    async def fetch_dividends(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[DividendObservation]:
        page = FetchPage[DividendObservation]
        if segment.alias.provider_id != "alphavantage":
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
        if cursor is not None:
            return page(
                state=FetchState.PARSE_ERROR,
                window=segment.window,
                error_code="UNEXPECTED_PROVIDER_CURSOR",
            )
        result = await self._http.get_json(
            "/query",
            params={
                "function": "DIVIDENDS",
                "symbol": segment.alias.provider_symbol,
                "datatype": "json",
            },
        )
        if result.error is not None:
            return parsed_page(segment, result, None, _dividend, page, date_key="ex_dividend_date")
        common = page_metadata(segment, result)
        payload = result.payload
        if not isinstance(payload, dict):
            return page(state=FetchState.PARSE_ERROR, error_code="INVALID_RESPONSE_SHAPE", **common)
        failure = _business_error(payload)
        if failure is not None:
            return page(state=failure[0], error_code=failure[1], **common)
        symbol = payload.get("symbol")
        if (
            not isinstance(symbol, str)
            or symbol.casefold() != segment.alias.provider_symbol.casefold()
        ):
            return page(
                state=FetchState.PARSE_ERROR, error_code="RESPONSE_SYMBOL_MISMATCH", **common
            )
        if payload.get("next_url") is not None or payload.get("next_cursor") is not None:
            return page(
                state=FetchState.PARSE_ERROR, error_code="UNDOCUMENTED_PAGINATION", **common
            )
        return parsed_page(
            segment,
            result,
            payload.get("data"),
            _dividend,
            page,
            date_key="ex_dividend_date",
            allow_outside_window=True,
            initial_warnings=("ALPHA_ADJUSTMENT_BASIS_UNDOCUMENTED",),
        )

    async def fetch_splits(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[SplitObservation]:
        # DIVIDENDS is this adapter's documented compatibility profile; no unproven split fields.
        return FetchPage[SplitObservation](
            state=FetchState.UNSUPPORTED,
            window=segment.window,
            error_code="ALPHA_SPLIT_CONTRACT_UNVERIFIED",
        )
