"""Bind validated local profiles to real adapters; no probe, quota reservation or hidden fetch."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import cast

import httpx

from spm_api.config import Settings
from spm_api.master_models import ProviderId
from spm_api.providers.adapters import AdapterHealth, ProviderAdapter
from spm_api.providers.alphavantage import AlphaVantageAdapter
from spm_api.providers.base import registry
from spm_api.providers.configuration import LoadedConfiguration, ProviderConfigurationStore
from spm_api.providers.eodhd import EodhdAdapter
from spm_api.providers.http import ProviderHTTP
from spm_api.providers.manual import EvidenceBatch, EvidenceProvider, ManualEvidenceAdapter
from spm_api.providers.massive import MassiveAdapter
from spm_api.providers.yahoo import YahooAdapter
from spm_api.providers.yfinance import YfinanceAdapter

_HTTP: dict[ProviderId, Callable[[ProviderHTTP], ProviderAdapter]] = {
    "eodhd": EodhdAdapter,
    "alphavantage": AlphaVantageAdapter,
    "massive": MassiveAdapter,
    "yahoo": YahooAdapter,
    "yfinance": YfinanceAdapter,
}


@asynccontextmanager
async def configured_adapter(
    configuration: LoadedConfiguration,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    evidence: EvidenceBatch | None = None,
) -> AsyncIterator[ProviderAdapter]:
    """The later job driver must reserve quota and recheck config/alias versions around I/O."""
    if configuration.error_code is not None or configuration.profile is None:
        raise ValueError("PROVIDER_UNCONFIGURED")
    provider_id = configuration.provider_id
    if registry()[provider_id].access_mode == "MANUAL_EVIDENCE":
        yield ManualEvidenceAdapter(
            cast(EvidenceProvider, provider_id), configuration.access, batch=evidence
        )
    else:
        # Both Yahoo profiles spend the same actual upstream request, never a second vendor vote.
        origin = "yahoo" if provider_id == "yfinance" else provider_id
        async with ProviderHTTP(origin, configuration.access, transport=transport) as client:
            yield _HTTP[provider_id](client)


async def check_configuration(settings: Settings, provider_id: ProviderId) -> AdapterHealth:
    """Read one local profile/dependency state without probing or reserving upstream traffic."""
    loaded = ProviderConfigurationStore(settings).load(provider_id)
    if loaded.error_code is not None:
        return loaded.health
    async with configured_adapter(loaded) as adapter:
        return await adapter.healthcheck()


async def check_configurations(settings: Settings) -> tuple[AdapterHealth, ...]:
    """Inspect files/local dependencies only; provider errors never become application unready."""
    return tuple([await check_configuration(settings, provider_id) for provider_id in registry()])
