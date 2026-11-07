"""Dispatch sealed source jobs through real adapters, never holding a transaction across HTTP."""

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from time import monotonic_ns
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from sqlalchemy import Engine

import spm_api.job_service as jobs
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.job_models import JobLease, SourceJobPlan, SourceOperation, WorkError
from spm_api.master_models import ProviderId
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import DividendObservation, SplitObservation, registry
from spm_api.providers.budgets import BudgetSnapshot, load_budget
from spm_api.providers.execution import (
    abandon_attempt,
    complete_attempt,
    reconcile_attempts,
    reserve_attempt,
)
from spm_api.providers.persistence import PreparedFetch, prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.selection import OwnerConfigurationStore
from spm_api.providers.symbols import AliasSegment, ResolutionState, resolve_symbol


def now() -> datetime:
    return datetime.now(UTC)


def _work_error(error: Exception) -> WorkError:
    if isinstance(error, ValueError):
        if str(error) == "PROVIDER_ALIAS_CHANGED":
            return WorkError.ALIAS_CHANGED
        if str(error) in {"PROVIDER_CONFIG_CHANGED", "PROVIDER_OWNER_MISMATCH"}:
            return WorkError.CONFIG_CHANGED
    return WorkError.WORKER_ERROR


class _Unavailable(Exception):
    def __init__(self, error: WorkError, *, skipped: bool = False) -> None:
        self.error, self.skipped = error, skipped


def _finish_item(
    engine: Engine,
    lease: JobLease,
    item_id: UUID,
    at: datetime,
    error: WorkError,
    *,
    skipped: bool = False,
) -> None:
    with unit_of_work(engine, write=True) as con:
        job = jobs.owned_job(con, lease.owner_id, lease.job_id)
        if job["status"] == "CANCEL_REQUESTED":
            reconcile_attempts(con, at=at)
            jobs.acknowledge_cancel(con, lease, at=at)
            return
        status: Literal["PARTIAL", "SKIPPED", "FAILED"] = (
            "PARTIAL"
            if jobs.last_checkpoint(con, item_id) is not None
            else ("SKIPPED" if skipped else "FAILED")
        )
        jobs.finish_item(con, lease, item_id, at=at, status=status, error=error)


def _prepare(
    engine: Engine,
    lease: JobLease,
    item: dict[str, Any],
    plan: SourceJobPlan,
    snapshot: BudgetSnapshot,
) -> tuple[PreparedFetch, str | None]:
    provider: ProviderId = item["provider_id"]
    with unit_of_work(engine) as con:
        resolved = resolve_symbol(con, UUID(item["security_id"]), provider, plan.window)
        last = jobs.last_checkpoint(con, UUID(item["id"]))
    if resolved.state != ResolutionState.RESOLVED:
        raise _Unavailable(
            WorkError.SYMBOL_AMBIGUOUS
            if resolved.state == ResolutionState.AMBIGUOUS
            else WorkError.UNSUPPORTED
            if resolved.state == ResolutionState.UNSUPPORTED
            else WorkError.SYMBOL_NOT_FOUND,
            skipped=resolved.state == ResolutionState.UNSUPPORTED,
        )
    cursor, context_id = None, None
    segment: AliasSegment | None
    if last is not None and last["next_cursor"] is not None:
        segment = AliasSegment.model_validate_json(last["alias_json"])
        if segment not in resolved.segments:
            raise _Unavailable(WorkError.ALIAS_CHANGED)
        cursor, context_id = last["next_cursor"], UUID(last["context_id"])
    else:
        first = (
            plan.window.start
            if last is None
            else (date.fromisoformat(last["requested_end"]) + timedelta(days=1)).isoformat()
        )
        segment = next((part for part in resolved.segments if part.window.start == first), None)
        if segment is None:
            raise _Unavailable(WorkError.ALIAS_CHANGED)
    return prepare_fetch(
        lease.owner_id, snapshot.primary, segment, item["operation"], context_id=context_id
    ), cursor


async def _fetch_item(
    engine: Engine,
    settings: Settings,
    lease: JobLease,
    item: dict[str, Any],
    plan: SourceJobPlan,
    clock: Callable[[], datetime],
    transport: httpx.AsyncBaseTransport | None,
    stopping: Callable[[], bool] | None,
) -> JobLease:
    provider: ProviderId = item["provider_id"]
    operation: SourceOperation = item["operation"]
    selection = next(value for value in plan.providers if value.provider_id == provider)
    definition = registry()[provider]
    if selection.profile_sha256 is None:
        raise _Unavailable(WorkError.UNCONFIGURED, skipped=True)
    if operation not in definition.operations or definition.access_mode == "MANUAL_EVIDENCE":
        raise _Unavailable(WorkError.UNSUPPORTED, skipped=True)
    store = OwnerConfigurationStore(settings, engine, lease.owner_id)
    try:
        snapshot = await asyncio.to_thread(load_budget, store, provider)
    except ValueError:
        raise _Unavailable(WorkError.UNCONFIGURED, skipped=True) from None
    if (
        snapshot.primary.revision is None
        or snapshot.primary.revision.profile_sha256 != selection.profile_sha256
    ):
        raise _Unavailable(WorkError.CONFIG_CHANGED)
    prepared, cursor = _prepare(engine, lease, item, plan, snapshot)
    identifier = None
    try:
        async with configured_adapter(snapshot.primary, transport=transport) as adapter:
            if not (await adapter.healthcheck()).configured:
                raise _Unavailable(WorkError.UNCONFIGURED, skipped=True)
            if not await asyncio.to_thread(snapshot.unchanged, store):
                raise _Unavailable(WorkError.CONFIG_CHANGED)
            if stopping is not None and stopping():
                return lease
            with unit_of_work(engine, write=True) as con:
                at = clock()
                if lease.expires_at - at <= timedelta(seconds=30):
                    lease = jobs.renew(con, lease, at=at)
                decision = reserve_attempt(
                    con,
                    lease,
                    UUID(item["id"]),
                    prepared,
                    snapshot.limits,
                    attempt_id=uuid4(),
                    at=at,
                    cursor_in=cursor,
                    budget_snapshot=snapshot,
                )
            if decision.attempt is None:
                return lease
            if not decision.attempt.created:
                raise jobs.JobError("UPSTREAM_GRANT_ALREADY_USED")
            identifier = decision.attempt.id
            if not await asyncio.to_thread(snapshot.unchanged, store):
                raise _Unavailable(WorkError.CONFIG_CHANGED)
            if stopping is not None and stopping():
                return lease
            started, tick = clock(), monotonic_ns()
            page: FetchPage[DividendObservation] | FetchPage[SplitObservation]
            page = (
                await adapter.fetch_dividends(prepared.segment, cursor)
                if operation == "DIVIDENDS"
                else (await adapter.fetch_splits(prepared.segment, cursor))
            )
            finished, duration = clock(), (monotonic_ns() - tick) // 1_000_000
        if not await asyncio.to_thread(snapshot.unchanged, store):
            raise _Unavailable(WorkError.CONFIG_CHANGED)
        with unit_of_work(engine, write=True) as con:
            complete_attempt(
                con,
                lease,
                identifier,
                prepared,
                page,
                started_at=started,
                finished_at=finished,
                duration_ms=duration,
                at=clock(),
                budget_snapshot=snapshot,
            )
    except _Unavailable:
        if identifier is None:
            raise
        with unit_of_work(engine, write=True) as con:
            abandon_attempt(con, lease, identifier, at=clock(), reason="CONFIG_CHANGED")
    except jobs.JobError:
        raise
    except Exception as error:
        if identifier is None:
            raise
        # Never retain exception text, provider bodies, credential paths or private URLs.
        code = _work_error(error)
        with unit_of_work(engine, write=True) as con:
            abandon_attempt(
                con,
                lease,
                identifier,
                at=clock(),
                reason="CONFIG_CHANGED" if code == WorkError.CONFIG_CHANGED else "WORKER_ERROR",
                error=WorkError.WORKER_ERROR if code == WorkError.CONFIG_CHANGED else code,
            )
    return lease


async def dispatch_once(
    engine: Engine,
    settings: Settings,
    worker_id: UUID,
    *,
    clock: Callable[[], datetime] = now,
    transport: httpx.AsyncBaseTransport | None = None,
    stopping: Callable[[], bool] | None = None,
) -> bool:
    """Process one job's ready pages; durable waits release its lease without sleeping."""
    if stopping is not None and stopping():
        return False
    with unit_of_work(engine, write=True) as con:
        at = clock()
        reconcile_attempts(con, at=at)
        lease = jobs.claim(con, worker_id, at=at)
        if lease is None:
            return False
        jobs.start(con, lease, at=at)
    try:
        while stopping is None or not stopping():
            await asyncio.sleep(0)
            if stopping is not None and stopping():
                return True
            with unit_of_work(engine, write=True) as con:
                at = clock()
                job = jobs.owned_job(con, lease.owner_id, lease.job_id)
                if job["status"] in jobs._TERMINAL:
                    return True
                if job["status"] == "CANCEL_REQUESTED":
                    jobs._fence(con, lease, at, states=("CANCEL_REQUESTED",))
                    reconcile_attempts(con, at=at)
                    jobs.acknowledge_cancel(con, lease, at=at)
                    return True
                job, timestamp = jobs._fence(con, lease, at)
                plan = SourceJobPlan.model_validate_json(job["request_json"])
                remaining = [
                    value
                    for value in jobs._items(con, job["id"])
                    if value["status"] not in jobs._ITEM_TERMINAL
                ]
                if not remaining:
                    jobs.finish(con, lease, at=at)
                    return True
                item = next((value for value in remaining if value["status"] == "RUNNING"), None)
                if item is None:
                    item = next(
                        (
                            value
                            for value in remaining
                            if value["next_run_at"] is None or value["next_run_at"] <= timestamp
                        ),
                        None,
                    )
                if item is None:
                    jobs.park(con, lease, at=at)
                    return True
                last = jobs.last_checkpoint(con, UUID(item["id"]))
                if (
                    last is not None
                    and last["next_cursor"] is None
                    and last["requested_end"] == plan.window.end
                ):
                    if item["status"] != "RUNNING":
                        jobs.begin_item(con, lease, UUID(item["id"]), at=at)
                    jobs.finish_item(con, lease, UUID(item["id"]), at=at, status="SUCCEEDED")
                    continue
            try:
                lease = await _fetch_item(
                    engine, settings, lease, item, plan, clock, transport, stopping
                )
            except _Unavailable as error:
                _finish_item(
                    engine, lease, UUID(item["id"]), clock(), error.error, skipped=error.skipped
                )
            except jobs.JobError as error:
                if str(error) == "JOB_LEASE_LOST":
                    continue
                if str(error) in {"JOB_CLOCK_REGRESSION", "UPSTREAM_CLOCK_REGRESSION"}:
                    raise
                code = {
                    "JOB_PAGE_LIMIT": WorkError.PAGE_LIMIT,
                    "JOB_CURSOR_CYCLE": WorkError.CURSOR_CYCLE,
                    "JOB_CHECKPOINT_GAP": WorkError.CONFIG_CHANGED,
                    "JOB_CONFIGURATION_CHANGED": WorkError.CONFIG_CHANGED,
                }.get(str(error), WorkError.WORKER_ERROR)
                _finish_item(engine, lease, UUID(item["id"]), clock(), code)
            except Exception as error:
                _finish_item(engine, lease, UUID(item["id"]), clock(), _work_error(error))
        return True
    except jobs.JobError as error:
        if str(error) not in {
            "JOB_LEASE_LOST",
            "JOB_CLOCK_REGRESSION",
            "UPSTREAM_CLOCK_REGRESSION",
        }:
            raise
        return True
