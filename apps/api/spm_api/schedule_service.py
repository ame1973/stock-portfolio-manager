"""Owned source schedules and atomic due receipts; all private I/O stays with callers."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import Connection, text

import spm_api.job_service as jobs
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.job_models import ProviderSelection, SourceJobPlan
from spm_api.mutations import check_version
from spm_api.schedule_models import (
    DueSchedule,
    SourceScheduleCreate,
    SourceSchedulePolicy,
    SourceScheduleUpdate,
)
from spm_api.schedule_time import _utc, latest_due, next_due, occurrence_window

_ACTIVE = {"QUEUED", "LEASED", "RUNNING", "RETRYING", "CANCEL_REQUESTED"}


class ScheduleError(ValueError):
    """Stable internal errors never include stored policies, profiles or exception payloads."""


def _writer(con: Connection) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise ScheduleError("WRITE_UNIT_OF_WORK_REQUIRED")


def _clock(at: datetime) -> datetime:
    try:
        return _utc(at)
    except ValueError as error:
        raise ScheduleError(str(error)) from None


def _active(con: Connection, owner: UUID) -> None:
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:owner AND is_active=1"), {"owner": str(owner)}
        ).first()
        is None
    ):
        raise ScheduleError("SCHEDULE_OWNER_UNAVAILABLE")


def policy_from_row(row: dict[str, Any]) -> SourceSchedulePolicy:
    encoded = row["policy_json"]
    if not isinstance(encoded, str) or not 2 <= len(encoded) <= 16384:
        raise ScheduleError("SCHEDULE_POLICY_UNAVAILABLE")
    try:
        value = SourceSchedulePolicy.model_validate_json(encoded)
    except ValidationError:
        raise ScheduleError("SCHEDULE_POLICY_UNAVAILABLE") from None
    if (
        canonical(value.model_dump(mode="json")) != encoded
        or digest(encoded) != row["policy_sha256"]
    ):
        raise ScheduleError("SCHEDULE_POLICY_UNAVAILABLE")
    return value


def _scope(con: Connection, policy: SourceSchedulePolicy) -> None:
    for identifier in policy.security_ids:
        actual = con.execute(
            text("""
            SELECT e.market,e.timezone FROM securities s JOIN exchanges e ON e.code=s.exchange_code
            WHERE s.id=:id
        """),
            {"id": str(identifier)},
        ).first()
        if actual is None or tuple(actual) != (policy.market, policy.exchange_timezone):
            raise ScheduleError("SCHEDULE_SECURITY_SCOPE")


def owned_schedule(con: Connection, owner_id: UUID, schedule_id: UUID) -> dict[str, Any]:
    row = (
        con.execute(
            text("SELECT * FROM source_schedules WHERE id=:id AND owner_id=:owner"),
            {"id": str(schedule_id), "owner": str(owner_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ScheduleError("SCHEDULE_NOT_FOUND")
    result = dict(row)
    policy_from_row(result)
    return result


def create(
    con: Connection,
    owner_id: UUID,
    body: SourceScheduleCreate,
    *,
    at: datetime,
    request_id: str | None = None,
) -> dict[str, Any]:
    _writer(con)
    clock = stamp(_clock(at))
    body = SourceScheduleCreate.model_validate(body.model_dump(mode="json"))
    _active(con, owner_id)
    _scope(con, body.policy)
    identifier, encoded = str(uuid4()), canonical(body.policy.model_dump(mode="json"))
    con.execute(
        text("""
        INSERT INTO source_schedules(id,owner_id,name,policy_json,policy_sha256,
            created_at,updated_at)
        VALUES (:id,:owner,:name,:policy,:hash,:at,:at)
    """),
        {
            "id": identifier,
            "owner": str(owner_id),
            "name": body.name,
            "policy": encoded,
            "hash": digest(encoded),
            "at": clock,
        },
    )
    audit(
        con,
        str(owner_id),
        "source_schedule",
        identifier,
        "SOURCE_SCHEDULE_CREATED",
        after={"name": body.name, "version": 1, "enabled": False, "policy_sha256": digest(encoded)},
        reason="Owner created an explicit disabled source schedule",
        request_id=request_id,
    )
    return owned_schedule(con, owner_id, UUID(identifier))


def revise(
    con: Connection,
    owner_id: UUID,
    schedule_id: UUID,
    body: SourceScheduleUpdate,
    *,
    version: int,
    at: datetime,
    request_id: str | None = None,
) -> dict[str, Any]:
    _writer(con)
    clock = _clock(at)
    body = SourceScheduleUpdate.model_validate(body.model_dump(mode="json"))
    _active(con, owner_id)
    row = owned_schedule(con, owner_id, schedule_id)
    if type(version) is not int or not 1 <= version <= 2147483647:
        raise ScheduleError("INVALID_SCHEDULE_VERSION")
    check_version(row["version"], version)
    if stamp(clock) < row["updated_at"]:
        raise ScheduleError("SCHEDULE_CLOCK_REGRESSION")
    policy = policy_from_row(row)
    if (
        row["initial_job_id"] is not None
        and body.policy.backfill_from_date != policy.backfill_from_date
    ):
        raise ScheduleError("INITIAL_BACKFILL_ALREADY_QUEUED")
    encoded = canonical(body.policy.model_dump(mode="json"))
    if body.enabled or encoded != row["policy_json"]:
        _scope(con, body.policy)
    inclusive = row["last_due_at"] is None or stamp(clock) > row["last_due_at"]
    following = next_due(body.policy, clock, inclusive=inclusive) if body.enabled else None
    if body.enabled and following is None:
        raise ScheduleError("SCHEDULE_TIME_EXHAUSTED")
    con.execute(
        text("""
        UPDATE source_schedules SET name=:name,policy_json=:policy,policy_sha256=:hash,
            enabled=:enabled,next_run_at=:next,updated_at=:at,version=version+1
        WHERE id=:id AND owner_id=:owner AND version=:version
    """),
        {
            "name": body.name,
            "policy": encoded,
            "hash": digest(encoded),
            "enabled": int(body.enabled),
            "next": stamp(following) if following else None,
            "at": stamp(clock),
            "id": str(schedule_id),
            "owner": str(owner_id),
            "version": version,
        },
    )
    audit(
        con,
        str(owner_id),
        "source_schedule",
        str(schedule_id),
        "SOURCE_SCHEDULE_CHANGED",
        before={
            "name": row["name"],
            "version": version,
            "enabled": bool(row["enabled"]),
            "policy_sha256": row["policy_sha256"],
        },
        after={
            "name": body.name,
            "version": version + 1,
            "enabled": body.enabled,
            "policy_sha256": digest(encoded),
        },
        reason=body.reason,
        request_id=request_id,
    )
    return owned_schedule(con, owner_id, schedule_id)


def due_ids(con: Connection, *, at: datetime, limit: int = 25) -> tuple[tuple[UUID, UUID], ...]:
    """Return bounded owner/id candidates so one corrupt definition need not block its peers."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ScheduleError("INVALID_SCHEDULE_LIMIT")
    return tuple(
        (UUID(row[0]), UUID(row[1]))
        for row in con.execute(
            text("""
        SELECT s.owner_id,s.id FROM source_schedules s JOIN owners o ON o.id=s.owner_id
        WHERE s.enabled=1 AND s.next_run_at<=:at AND o.is_active=1
        ORDER BY s.next_run_at,s.id LIMIT :limit
    """),
            {"at": stamp(_clock(at)), "limit": limit},
        )
    )


def _stored_slot(policy: SourceSchedulePolicy, encoded: str) -> datetime:
    try:
        value = _clock(datetime.fromisoformat(encoded))
        if stamp(value) == encoded and next_due(policy, value, inclusive=True) == value:
            return value
    except ValueError:
        pass
    raise ScheduleError("SCHEDULE_SLOT_UNAVAILABLE")


def due_schedule(
    con: Connection,
    owner_id: UUID,
    schedule_id: UUID,
    *,
    at: datetime,
) -> DueSchedule | None:
    clock = _clock(at)
    row = owned_schedule(con, owner_id, schedule_id)
    _active(con, owner_id)
    if not row["enabled"] or row["next_run_at"] is None:
        return None
    policy = policy_from_row(row)
    expected = _stored_slot(policy, row["next_run_at"])
    if expected > clock:
        return None
    _scope(con, policy)
    latest = latest_due(policy, clock)
    if latest is None or latest < expected:
        raise ScheduleError("SCHEDULE_SLOT_UNAVAILABLE")
    return DueSchedule(
        schedule_id=schedule_id,
        owner_id=owner_id,
        version=row["version"],
        expected_run_at=expected,
        due_at=latest,
        policy=policy,
        initial=row["initial_job_id"] is None and policy.backfill_from_date is not None,
    )


def _same_tick(row: dict[str, Any], tick: DueSchedule) -> bool:
    return bool(
        row["policy_json"] == canonical(tick.policy.model_dump(mode="json"))
        and row["expected_run_at"] == stamp(_clock(tick.expected_run_at))
        and row["due_at"] == stamp(_clock(tick.due_at))
        and (row["kind"] == "INITIAL_BACKFILL") == tick.initial
    )


def materialize(
    con: Connection,
    tick: DueSchedule,
    selected: tuple[ProviderSelection, ...],
    *,
    at: datetime,
    request_id: str | None = None,
) -> dict[str, Any]:
    _writer(con)
    clock = _clock(at)
    tick = DueSchedule.model_validate_json(tick.model_dump_json())
    retained = (
        con.execute(
            text("""
        SELECT * FROM source_schedule_occurrences
        WHERE schedule_id=:id AND owner_id=:owner AND schedule_version=:version
    """),
            {"id": str(tick.schedule_id), "owner": str(tick.owner_id), "version": tick.version},
        )
        .mappings()
        .first()
    )
    if retained is not None:
        original = dict(retained)
        policy_from_row(original)
        if not _same_tick(original, tick):
            raise ScheduleError("SCHEDULE_TICK_CONFLICT")
        return original
    row = owned_schedule(con, tick.owner_id, tick.schedule_id)
    _active(con, tick.owner_id)
    if (
        row["version"] != tick.version
        or not row["enabled"]
        or row["next_run_at"] != stamp(_clock(tick.expected_run_at))
        or row["policy_json"] != canonical(tick.policy.model_dump(mode="json"))
        or tick.initial
        != (row["initial_job_id"] is None and tick.policy.backfill_from_date is not None)
    ):
        raise ScheduleError("SCHEDULE_TICK_STALE")
    if stamp(clock) < row["updated_at"]:
        raise ScheduleError("SCHEDULE_CLOCK_REGRESSION")
    _stored_slot(tick.policy, row["next_run_at"])
    if latest_due(tick.policy, clock) != _clock(tick.due_at):
        raise ScheduleError("SCHEDULE_TICK_STALE")
    _scope(con, tick.policy)
    try:
        window = occurrence_window(tick.policy, tick.due_at, initial=tick.initial)
    except ValueError as error:
        raise ScheduleError(str(error)) from None
    following = next_due(tick.policy, clock)
    previous = con.execute(
        text("SELECT id,status FROM jobs WHERE id=:id AND owner_id=:owner"),
        {"id": row["last_job_id"], "owner": str(tick.owner_id)},
    ).first()
    blocking = previous[0] if previous is not None and previous[1] in _ACTIVE else None
    identifier = uuid5(tick.schedule_id, f"SOURCE_ONLY:{tick.version}")
    job_id = None
    if blocking is None:
        normalized = tuple(
            ProviderSelection.model_validate(value.model_dump(mode="json")) for value in selected
        )
        if (
            len(normalized) != len(tick.policy.provider_ids)
            or tuple(sorted(value.provider_id for value in normalized)) != tick.policy.provider_ids
        ):
            raise ScheduleError("INVALID_SCHEDULE_SELECTION")
        plan = SourceJobPlan(
            security_ids=tick.policy.security_ids,
            providers=normalized,
            window=window,
            operations=tick.policy.operations,
            force=False,
        )
        job_id = jobs.enqueue(
            con, tick.owner_id, plan, intent_id=identifier, at=clock, request_id=request_id
        )["id"]
    parameters = {
        "id": str(identifier),
        "schedule": str(tick.schedule_id),
        "owner": str(tick.owner_id),
        "sequence": row["last_sequence"] + 1,
        "version": tick.version,
        "expected": stamp(_clock(tick.expected_run_at)),
        "due": stamp(_clock(tick.due_at)),
        "at": stamp(clock),
        "next": stamp(following) if following else None,
        "policy": row["policy_json"],
        "hash": row["policy_sha256"],
        "start": window.start,
        "end": window.end,
        "kind": "INITIAL_BACKFILL" if tick.initial else "REGULAR",
        "outcome": "SKIPPED_IN_FLIGHT" if blocking else "QUEUED",
        "job": job_id,
        "blocking": blocking,
    }
    con.execute(
        text("""
        INSERT INTO source_schedule_occurrences(id,schedule_id,owner_id,sequence,schedule_version,
            expected_run_at,due_at,materialized_at,next_run_at,policy_json,policy_sha256,
            window_start,window_end,kind,outcome,job_id,blocking_job_id)
        VALUES (:id,:schedule,:owner,:sequence,:version,:expected,:due,:at,:next,:policy,:hash,
            :start,:end,:kind,:outcome,:job,:blocking)
    """),
        parameters,
    )
    audit(
        con,
        str(tick.owner_id),
        "source_schedule",
        str(tick.schedule_id),
        "SOURCE_SCHEDULE_OCCURRED",
        after={
            "occurrence_id": str(identifier),
            "sequence": parameters["sequence"],
            "outcome": parameters["outcome"],
            "job_id": job_id,
            "blocking_job_id": blocking,
        },
        reason="Source-only scheduled occurrence; no canonical adoption",
        request_id=request_id,
    )
    return dict(
        con.execute(
            text("SELECT * FROM source_schedule_occurrences WHERE id=:id"), {"id": str(identifier)}
        )
        .mappings()
        .one()
    )
