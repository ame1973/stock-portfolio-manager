"""Seed explicit old schedule intent, then let the actual deployed daemon evaluate its own clock."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

import spm_api.schedule_service as schedules
from spm_api.config import Settings
from spm_api.db import make_engine, unit_of_work
from spm_api.job_models import ProviderSelection
from spm_api.schedule_models import SourceScheduleCreate, SourceSchedulePolicy, SourceScheduleUpdate


def snapshot(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    with unit_of_work(engine) as con:
        names = con.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'").scalars()
        return {
            name: [
                tuple(row) for row in con.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY rowid')
            ]
            for name in names
        }


def history(engine: Engine) -> str:
    """Retain a real 25+2 receipt boundary, then pause before the deployed worker resumes."""
    current = datetime.now(UTC)
    at = current - timedelta(days=40)
    policy = SourceSchedulePolicy(
        security_ids=(UUID(sys.argv[1]),),
        provider_ids=("eodhd",),
        market="US",
        first_date=at.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        local_run_time="17:15",
        cadence="DAILY",
        lookback_days=30,
    )
    with unit_of_work(engine, write=True) as con:
        owner = UUID(con.exec_driver_sql("SELECT id FROM owners WHERE is_active=1").scalar_one())
        row = schedules.create(
            con,
            owner,
            SourceScheduleCreate(name="Synthetic paused 27-receipt schedule", policy=policy),
            at=at,
        )
        identifier = UUID(row["id"])
        row = schedules.revise(
            con,
            owner,
            identifier,
            SourceScheduleUpdate(
                name=row["name"],
                policy=policy,
                enabled=True,
                reason="Explicit synthetic receipt-history fixture, never live upstream work.",
            ),
            version=1,
            at=at,
        )
    for _ in range(27):
        with unit_of_work(engine, write=True) as con:
            row = schedules.owned_schedule(con, owner, identifier)
            due_at = datetime.fromisoformat(row["next_run_at"]) + timedelta(seconds=1)
            tick = schedules.due_schedule(con, owner, identifier, at=due_at)
            assert tick is not None
            schedules.materialize(
                con, tick, (ProviderSelection(provider_id="eodhd", profile_sha256=None),), at=due_at
            )
    with unit_of_work(engine, write=True) as con:
        row = schedules.owned_schedule(con, owner, identifier)
        schedules.revise(
            con,
            owner,
            identifier,
            SourceScheduleUpdate(
                name=row["name"],
                policy=policy,
                enabled=False,
                reason="Pause the explicit fixture after 27 immutable evaluations.",
            ),
            version=row["version"],
            at=current,
        )
    return str(identifier)


def main() -> None:
    assert os.geteuid() == 10001 and os.environ.get("SPM_SYNTHETIC_E2E") == "schedule-worker"
    settings = Settings()
    engine = make_engine(settings.database_path)
    try:
        before = snapshot(engine)
        if len(sys.argv) > 2:
            assert sys.argv[2] == "--history"
            identifier = history(engine)
            after = snapshot(engine)
            allowed = {
                "source_schedules",
                "source_schedule_occurrences",
                "jobs",
                "job_items",
                "audit_events",
            }
            assert {key: value for key, value in before.items() if key not in allowed} == {
                key: value for key, value in after.items() if key not in allowed
            }
            print(
                json.dumps(
                    {
                        "schedule_id": identifier,
                        "scope": "SYNTHETIC_PAUSED_HISTORY",
                        "provider_io": "NOT_RUN",
                    }
                )
            )
            return
        current = datetime.now(UTC)
        at = current - timedelta(days=3)
        local = current.astimezone(ZoneInfo("America/New_York"))
        # Keep the next real slot hours away, not a flaky imminent minute boundary.
        run_time = "23:59" if local.hour <= 16 else "16:00"
        policy = SourceSchedulePolicy(
            security_ids=(UUID(sys.argv[1]),),
            provider_ids=("eodhd",),
            market="US",
            first_date=at.date().isoformat(),
            local_run_time=run_time,
            cadence="DAILY",
            lookback_days=30,
            backfill_from_date=(at.date() - timedelta(days=30)).isoformat(),
        )
        with unit_of_work(engine, write=True) as con:
            owner = UUID(
                con.exec_driver_sql("SELECT id FROM owners WHERE is_active=1").scalar_one()
            )
            row = schedules.create(
                con,
                owner,
                SourceScheduleCreate(name="Synthetic explicit real-daemon schedule", policy=policy),
                at=at,
            )
            schedules.revise(
                con,
                owner,
                UUID(row["id"]),
                SourceScheduleUpdate(
                    name=row["name"],
                    policy=policy,
                    enabled=True,
                    reason="Explicit synthetic due intent for the real unconfigured worker.",
                ),
                version=1,
                at=at,
            )
        after = snapshot(engine)
        allowed = {"source_schedules", "audit_events"}
        assert {key: value for key, value in before.items() if key not in allowed} == {
            key: value for key, value in after.items() if key not in allowed
        }
        print(
            json.dumps(
                {
                    "schedule_id": row["id"],
                    "scope": "SYNTHETIC_EXPLICIT_DUE_INTENT",
                    "provider_io": "NOT_RUN",
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
