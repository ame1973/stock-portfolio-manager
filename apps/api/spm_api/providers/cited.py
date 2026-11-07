"""Internal actor-bound cited assertions; callers still owe current HTTP session/CSRF checks."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic_ns
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text

from spm_api.audit import audit, now
from spm_api.cited_models import (
    CitedDividendCreate,
    CitedSourceCreated,
    CitedSourceInput,
    CitedSplitCreate,
)
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import DividendObservation, FetchState, SplitObservation
from spm_api.providers.manual import EvidenceBatch
from spm_api.providers.persistence import PreparedFetch, capture_page, prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.selection import OwnerConfigurationStore
from spm_api.providers.symbols import FetchWindow, ResolutionState, resolve_symbol


class CitedEvidenceError(ValueError):
    """Fixed codes only; never interpolate private citations, URLs or stored rows."""


def _request(value: CitedSourceInput) -> CitedSourceInput:
    try:
        if type(value) is CitedDividendCreate:
            return CitedDividendCreate.model_validate(value.model_dump(mode="json"))
        if type(value) is CitedSplitCreate:
            return CitedSplitCreate.model_validate(value.model_dump(mode="json"))
    except (ValueError, TypeError):
        pass
    raise CitedEvidenceError("INVALID_CITED_ASSERTION")


@dataclass(frozen=True)
class PreparedCitedEvidence:
    body: CitedSourceInput = field(repr=False)
    fetch: PreparedFetch = field(repr=False)
    page: FetchPage[DividendObservation] | FetchPage[SplitObservation] = field(repr=False)
    citation_id: UUID
    run_id: UUID
    started_at: datetime
    finished_at: datetime
    duration_ms: int


async def prepare(
    settings: Settings, engine: Engine, actor: UUID, body: CitedSourceInput
) -> PreparedCitedEvidence:
    """Load explicit owned access outside all writers; parse one quotation without HTTP."""
    if not isinstance(actor, UUID):
        raise CitedEvidenceError("INVALID_CITED_ACTOR")
    body = _request(body)
    citation_id = uuid4()
    try:
        source = body.source(citation_id)
    except (ValueError, TypeError):
        raise CitedEvidenceError("INVALID_CITED_ASSERTION") from None
    started, clock = now(), monotonic_ns()
    store = OwnerConfigurationStore(settings, engine, actor)
    configuration = await asyncio.to_thread(store.load, body.provider_id)
    if configuration.error_code is not None:
        raise CitedEvidenceError("CITED_SOURCE_UNCONFIGURED")
    day = body.ex_date if isinstance(body, CitedDividendCreate) else body.effective_date
    window = FetchWindow(start=day, end=day)
    with unit_of_work(engine) as con:
        resolved = resolve_symbol(con, body.security_id, body.provider_id, window)
    if (
        resolved.state != ResolutionState.RESOLVED
        or len(resolved.segments) != 1
        or resolved.segments[0].alias.provider_symbol != body.provider_symbol
    ):
        raise CitedEvidenceError("CITED_SOURCE_IDENTITY_UNRESOLVED")
    fetch = prepare_fetch(actor, configuration, resolved.segments[0], body.operation)
    batch = (
        EvidenceBatch(dividends=(source,))
        if isinstance(source, DividendObservation)
        else EvidenceBatch(splits=(source,))
    )
    async with configured_adapter(configuration, evidence=batch) as adapter:
        page = (
            await adapter.fetch_dividends(fetch.segment)
            if body.operation == "DIVIDENDS"
            else await adapter.fetch_splits(fetch.segment)
        )
    if not await asyncio.to_thread(store.unchanged, configuration):
        raise CitedEvidenceError("PROVIDER_CONFIG_CHANGED")
    result = PreparedCitedEvidence(
        body, fetch, page, citation_id, uuid4(), started, now(), (monotonic_ns() - clock) // 1000000
    )
    _validate(result, actor)
    return result


def _validate(prepared: PreparedCitedEvidence, actor: UUID) -> None:
    body = _request(prepared.body)
    fetch, page = prepared.fetch, prepared.page
    if fetch.owner_id != actor or fetch.configuration.selection_owner_id != actor:
        raise CitedEvidenceError("CITED_ACTOR_CHANGED")
    try:
        source = body.source(prepared.citation_id)
    except (ValueError, TypeError):
        raise CitedEvidenceError("INVALID_CITED_ASSERTION") from None
    day = body.ex_date if isinstance(body, CitedDividendCreate) else body.effective_date
    if (
        not isinstance(prepared.run_id, UUID)
        or fetch.operation != body.operation
        or fetch.segment.window != FetchWindow(start=day, end=day)
        or page.state != FetchState.OK_WITH_DATA
        or not page.complete
        or page.rows != (source,)
        or page.issue_count
        or page.filtered_count
        or page.next_cursor is not None
        or not {"USER_SUPPLIED_ASSERTION", "DOCUMENT_NOT_FETCHED", "NOT_AN_INDEPENDENT_VENDOR_VOTE"}
        <= set(page.warnings)
    ):
        raise CitedEvidenceError("CITED_ASSERTION_CHANGED")


def save(
    con: Connection,
    actor: UUID,
    prepared: PreparedCitedEvidence,
    *,
    request_id: UUID,
) -> CitedSourceCreated:
    """Append capture/authorship audit atomically; no private file/HTTP in the writer."""
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise CitedEvidenceError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(actor, UUID) or not isinstance(request_id, UUID):
        raise CitedEvidenceError("INVALID_CITED_ACTOR")
    _validate(prepared, actor)
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:owner AND is_active=1"), {"owner": str(actor)}
        ).first()
        is None
    ):
        raise CitedEvidenceError("CITED_ACTOR_CHANGED")
    existing = con.execute(
        text("SELECT 1 FROM provider_runs WHERE id=:id"), {"id": str(prepared.run_id)}
    ).first()
    receipt = capture_page(
        con,
        prepared.fetch,
        prepared.page,
        run_id=prepared.run_id,
        started_at=prepared.started_at,
        finished_at=prepared.finished_at,
        duration_ms=prepared.duration_ms,
    )
    result = CitedSourceCreated(
        provider_id=prepared.body.provider_id,
        security_id=prepared.body.security_id,
        operation=prepared.body.operation,
        observation_id=UUID(receipt.observation_ids[0]),
        provider_run_id=receipt.run_id,
        citation_id=prepared.citation_id,
    )
    if existing is None:
        audit(
            con,
            str(actor),
            "source_citation",
            str(prepared.citation_id),
            "USER_CITED_SOURCE_ASSERTED",
            request_id=str(request_id),
            after=result.model_dump(mode="json"),
            reason=prepared.body.citation.reason,
        )
    return result
