"""Single-leader supervision and fenced, quota-enforced source dispatch."""

import asyncio
import fcntl
import json
import logging
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import FrameType
from uuid import UUID, uuid4

from sqlalchemy import Engine

from spm_api.config import Settings
from spm_api.db import check_database, make_engine


@contextmanager
def leader_lock(directory: Path) -> Iterator[bool]:
    """OS releases the local-filesystem lock even when the worker crashes."""
    with (directory / ".worker.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def heartbeat(directory: Path, leader: str) -> None:
    temp = directory / f".worker-heartbeat-{leader}.tmp"
    temp.touch(mode=0o600, exist_ok=True)
    temp.write_text(json.dumps({"leader": leader, "timestamp": time.time()}))
    os.replace(temp, directory / ".worker-heartbeat.json")


def healthy(directory: Path, *, max_age: int = 30) -> bool:
    try:
        value = json.loads((directory / ".worker-heartbeat.json").read_text())
        return bool(0 <= time.time() - value["timestamp"] < max_age)
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def run_leader(settings: Settings, engine: Engine, leader: UUID, stop: Event) -> None:
    from spm_worker.dispatcher import dispatch_once

    async def pulse() -> None:
        while not stop.is_set():
            check_database(engine, settings)
            heartbeat(settings.data_dir, str(leader))
            await asyncio.sleep(5)

    async def dispatch() -> None:
        while not stop.is_set():
            try:
                worked = await dispatch_once(engine, settings, leader, stopping=stop.is_set)
            except Exception:
                logging.getLogger(__name__).error("SOURCE_DISPATCH_FAILED")
                worked = False
            if not worked:
                await asyncio.sleep(1)

    async with asyncio.TaskGroup() as group:
        group.create_task(pulse())
        group.create_task(dispatch())


def main() -> None:
    settings = Settings()
    engine = make_engine(settings.database_path)
    stop = Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    leader = uuid4()
    try:
        check_database(engine, settings)
        while not stop.is_set():
            with leader_lock(settings.data_dir) as acquired:
                if acquired:
                    asyncio.run(run_leader(settings, engine, leader, stop))
                else:
                    stop.wait(5)
    except Exception:
        raise SystemExit("Worker stopped after an internal failure.") from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
