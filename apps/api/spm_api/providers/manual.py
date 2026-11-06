"""Human-supplied evidence; no HTTP, scraping, verification or financial writes."""

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from spm_api.decimal_types import IsoDate
from spm_api.errors import Model
from spm_api.providers.adapters import AdapterHealth, FetchPage
from spm_api.providers.base import (
    DividendObservation,
    EvidenceCitation,
    FetchState,
    ProviderDefinition,
    SourceObservation,
    SplitObservation,
    registry,
)
from spm_api.providers.http import JsonValue, ProviderAccess, TransportError, TransportResult
from spm_api.providers.parsing import parsed_page
from spm_api.providers.symbols import AliasPeriod, AliasSegment

EvidenceProvider = Literal["hkex_evidence", "sec_evidence", "manual"]
_OFFICIAL_HOSTS = {
    "hkex_evidence": frozenset({"hkexnews.hk", "www.hkexnews.hk", "www1.hkexnews.hk"}),
    "sec_evidence": frozenset({"sec.gov", "www.sec.gov"}),
}


def _attribution(provider_id: EvidenceProvider, evidence: EvidenceCitation) -> None:
    if provider_id == "manual":
        return
    url = urlsplit(evidence.url or "")
    if (
        url.scheme != "https"
        or url.hostname not in _OFFICIAL_HOSTS[provider_id]
        or url.port not in {None, 443}
    ):
        raise ValueError("Official attribution requires its explicit HTTPS document host")


class DividendEvidence(DividendObservation):
    provider_id: EvidenceProvider
    ex_date: IsoDate
    evidence: EvidenceCitation = Field(repr=False)

    @model_validator(mode="after")
    def cited_source(self) -> Self:
        _attribution(self.provider_id, self.evidence)
        return self


class SplitEvidence(SplitObservation):
    provider_id: EvidenceProvider
    evidence: EvidenceCitation = Field(repr=False)

    @model_validator(mode="after")
    def cited_source(self) -> Self:
        _attribution(self.provider_id, self.evidence)
        return self


class EvidenceBatch(Model):
    """Bounded immutable supplied assertions, not a filesystem/network document loader."""

    model_config = ConfigDict(frozen=True)

    dividends: Annotated[tuple[DividendEvidence, ...], Field(max_length=10000)] = ()
    splits: Annotated[tuple[SplitEvidence, ...], Field(max_length=10000)] = ()


class ManualEvidenceAdapter:
    def __init__(
        self,
        provider_id: EvidenceProvider,
        access: ProviderAccess | None = None,
        *,
        batch: EvidenceBatch | None = None,
    ) -> None:
        if provider_id not in {"hkex_evidence", "sec_evidence", "manual"}:
            raise ValueError("Only explicit manual evidence profiles are supported")
        self._provider_id = provider_id
        self._access = (
            ProviderAccess.model_validate(access) if access is not None else ProviderAccess()
        )
        if self._access.credential is not None:
            raise ValueError("Manual evidence does not accept provider credentials")
        self._batch = EvidenceBatch.model_validate(batch) if batch is not None else EvidenceBatch()

    def get_capabilities(self) -> ProviderDefinition:
        return registry()[self._provider_id]

    def _configuration_error(self) -> TransportError | None:
        if not self._access.enabled:
            return TransportError.PROVIDER_DISABLED
        if not self._access.terms_acknowledged:
            return TransportError.RIGHTS_NOT_ACKNOWLEDGED
        return None

    async def healthcheck(self) -> AdapterHealth:
        error = self._configuration_error()
        return AdapterHealth(
            provider_id=self._provider_id,
            configured=error is None,
            state=FetchState.UNCONFIGURED if error is not None else FetchState.STALE,
            reason=error.value if error is not None else "MANUAL_INPUT_ONLY",
        )

    async def fetch_dividends(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[DividendObservation]:
        return self._fetch(
            segment,
            cursor,
            self._batch.dividends,
            DividendEvidence,
            FetchPage[DividendObservation],
            "ex_date",
        )

    async def fetch_splits(
        self, segment: AliasSegment, cursor: str | None = None
    ) -> FetchPage[SplitObservation]:
        return self._fetch(
            segment,
            cursor,
            self._batch.splits,
            SplitEvidence,
            FetchPage[SplitObservation],
            "effective_date",
        )

    def _fetch[T: SourceObservation](
        self,
        segment: AliasSegment,
        cursor: str | None,
        assertions: tuple[T, ...],
        model: type[T],
        page: type[FetchPage[T]],
        date_key: str,
    ) -> FetchPage[T]:
        if segment.alias.provider_id != self._provider_id:
            return page(
                state=FetchState.UNSUPPORTED,
                window=segment.window,
                error_code="ALIAS_PROVIDER_MISMATCH",
            )
        if segment.market not in self.get_capabilities().markets:
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
        error = self._configuration_error()
        if error is not None:
            return page(
                state=FetchState.UNCONFIGURED, window=segment.window, error_code=error.value
            )
        records: list[JsonValue] = [
            item.model_dump(mode="json")
            for item in assertions
            if item.provider_id == self._provider_id
            and item.security_id == segment.alias.security_id
        ]

        def parse(row: dict[str, JsonValue], alias: AliasPeriod) -> T:
            item = model.model_validate(row)
            if item.provider_symbol != alias.provider_symbol:
                raise ValueError("Evidence must match the explicit dated provider identity")
            return item

        return parsed_page(
            segment,
            TransportResult(),
            records,
            parse,
            page,
            date_key=date_key,
            allow_outside_window=True,
            initial_warnings=(
                "USER_SUPPLIED_ASSERTION",
                "DOCUMENT_NOT_FETCHED",
                "NOT_AN_INDEPENDENT_VENDOR_VOTE",
            ),
        )
