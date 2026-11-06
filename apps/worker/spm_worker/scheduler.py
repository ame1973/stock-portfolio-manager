"""Bounded automatic source intent evaluation; only the OS-locked leader calls this loop."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine

import spm_api.schedule_service as schedules
import spm_api.sync_service as source_api
from spm_api.audit import now
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.job_models import ProviderSelection

_RACES = {
    "SCHEDULE_TICK_STALE",
    "SCHEDULE_NOT_FOUND",
    "SCHEDULE_OWNER_UNAVAILABLE",
    "INVALID_SCHEDULE_SELECTION",
}


async def schedule_once(
    engine: Engine,
    settings: Settings,
    *,
    clock: Callable[[], datetime] = now,
    stopping: Callable[[], bool] | None = None,
    after: tuple[str, UUID] | None = None,
    limit: int = 25,
) -> tuple[str, UUID] | None:
    """Return an in-memory scan fence, never a work counter or a durable scheduling intent."""
    halted = stopping or (lambda: False)
    if halted():
        return None
    with unit_of_work(engine) as con:
        candidates = schedules.due_ids(con, at=clock(), limit=limit, after=after)
        following = (
            (
                con.exec_driver_sql(
                    "SELECT next_run_at FROM source_schedules WHERE id=?",
                    (str(candidates[-1][1]),),
                ).scalar_one(),
                candidates[-1][1],
            )
            if len(candidates) == limit
            else None
        )
    for owner, identifier in candidates:
        await asyncio.sleep(0)
        if halted():
            return None
        try:
            with unit_of_work(engine) as con:
                tick = schedules.due_schedule(con, owner, identifier, at=clock())
                blocked = schedules.blocking_job(con, owner, identifier) is not None
            if tick is None:
                continue
            if halted():
                return None
            selected: tuple[ProviderSelection, ...] = ()
            if not blocked:
                # Bounded private-file I/O holds neither a DB transaction nor the event loop.
                selected = await asyncio.to_thread(
                    source_api.selections, settings, tick.policy.provider_ids
                )
            if halted():
                return None
            with unit_of_work(engine, write=True) as con:
                if halted():
                    return None
                schedules.materialize(con, tick, selected, at=clock())
        except schedules.ScheduleError as error:
            if str(error) not in _RACES:
                logging.getLogger(__name__).error("SOURCE_SCHEDULE_EVALUATION_FAILED")
        except Exception:
            logging.getLogger(__name__).error("SOURCE_SCHEDULE_EVALUATION_FAILED")
    return following
