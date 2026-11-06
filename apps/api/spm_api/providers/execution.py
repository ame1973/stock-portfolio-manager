"""Fenced upstream reservations and outcomes; never perform HTTP or refund uncertain spending."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

import spm_api.job_service as jobs
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.job_models import JobLease, SourceJobPlan, WorkError
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import DividendObservation, FetchState, SplitObservation, registry
from spm_api.providers.budgets import (
    TOKEN_UNITS,
    OriginLimits,
    aware,
    later,
    microseconds,
    retry_deadline,
)
from spm_api.providers.persistence import (
    CaptureReceipt,
    PreparedFetch,
    _cursor,
    _guard_credential,
    persist_context,
)

COOLDOWN = timedelta(seconds=60)
FAILURE_THRESHOLD = 5


@dataclass(frozen=True)
class ReservedAttempt:
    id: UUID
    context_id: UUID
    expires_at: datetime
    created: bool


@dataclass(frozen=True)
class BudgetDecision:
    attempt: ReservedAttempt | None
    retry_at: datetime | None
    reason: str


@dataclass(frozen=True)
class CompletedAttempt:
    receipt: CaptureReceipt
    retry_at: datetime | None
    replayed: bool


def _select(con: Connection, query: str, **values: Any) -> dict[str, Any] | None:
    row = con.execute(text(query), values).mappings().first()
    return dict(row) if row is not None else None


def _state(con: Connection, origin: str) -> dict[str, Any] | None:
    return _select(con, "SELECT * FROM provider_budgets WHERE origin_group=:origin", origin=origin)


def _save(con: Connection, state: dict[str, Any], at: str, **values: Any) -> dict[str, Any]:
    changes = ",".join(f"{key}=:{key}" for key in values)
    con.execute(
        text(f"""
        UPDATE provider_budgets SET {changes},updated_at=:at,version=version+1
        WHERE origin_group=:origin
    """),
        {**values, "at": at, "origin": state["origin_group"]},
    )
    return {**state, **values, "updated_at": at, "version": state["version"] + 1}


def _audit(con: Connection, reservation: dict[str, Any], action: str, **values: Any) -> None:
    audit(
        con,
        reservation["owner_id"],
        "upstream_attempt",
        reservation["id"],
        action,
        after={"origin_group": reservation["origin_group"], **values},
    )


def _waiting(
    con: Connection, lease: JobLease, item_id: UUID, at: datetime, deadline: datetime, reason: str
) -> BudgetDecision:
    jobs.defer_item(
        con,
        lease,
        item_id,
        at=at,
        until=deadline,
        error=WorkError.CIRCUIT_OPEN if reason.startswith("CIRCUIT") else WorkError.QUOTA_WAIT,
    )
    return BudgetDecision(None, deadline, reason)


def _reap(con: Connection, state: dict[str, Any], at: datetime) -> dict[str, Any]:
    clock = stamp(at)
    pending = (
        con.execute(
            text("""
        SELECT a.*,j.status AS job_status,j.lease_generation AS current_generation,
            j.lease_expires_at AS current_expiry,i.status AS item_status,
            i.attempt_count AS current_attempt
        FROM upstream_attempts a JOIN jobs j ON j.id=a.job_id JOIN job_items i ON i.id=a.item_id
        WHERE a.origin_group=:origin AND NOT EXISTS(
            SELECT 1 FROM upstream_outcomes o WHERE o.attempt_id=a.id)
        ORDER BY a.reserved_at,a.id
    """),
            {"origin": state["origin_group"]},
        )
        .mappings()
        .all()
    )
    for value in pending:
        reservation = dict(value)
        cancelled = reservation["job_status"] in {"CANCEL_REQUESTED", "CANCELLED"}
        stale = (
            cancelled
            or reservation["lease_generation"] != reservation["current_generation"]
            or reservation["expires_at"] <= clock
            or reservation["current_expiry"] is None
            or reservation["current_expiry"] <= clock
            or reservation["item_status"] != "RUNNING"
            or reservation["work_attempt"] != reservation["current_attempt"]
        )
        if not stale:
            continue
        reason = "JOB_CANCELLED" if cancelled else "LEASE_EXPIRED"
        con.execute(
            text("""
            INSERT INTO upstream_outcomes(attempt_id,disposition,reason,finished_at)
            VALUES (:id,:disposition,:reason,:at)
        """),
            {
                "id": reservation["id"],
                "disposition": "CANCELLED" if cancelled else "ABANDONED",
                "reason": reason,
                "at": clock,
            },
        )
        _audit(con, reservation, "UPSTREAM_ATTEMPT_ABANDONED", reason=reason)
        if state["probe_attempt_id"] == reservation["id"]:
            state = _save(
                con,
                state,
                clock,
                circuit_state="OPEN",
                probe_attempt_id=None,
                circuit_until=stamp(later(at, COOLDOWN)),
                consecutive_failures=FAILURE_THRESHOLD,
            )
    return state


def _refilled(state: dict[str, Any], limits: OriginLimits, at: datetime) -> int:
    elapsed = microseconds(at - datetime.fromisoformat(state["refilled_at"]))
    if elapsed < 0:
        raise jobs.JobError("UPSTREAM_CLOCK_REGRESSION")
    previous: int = min(
        state["minute_limit"] * TOKEN_UNITS, state["tokens_num"] + elapsed * state["minute_limit"]
    )
    return min(previous, limits.requests_per_minute * TOKEN_UNITS)


def reserve_attempt(
    con: Connection,
    lease: JobLease,
    item_id: UUID,
    prepared: PreparedFetch,
    limits: OriginLimits,
    *,
    attempt_id: UUID,
    at: datetime,
    cursor_in: str | None = None,
) -> BudgetDecision:
    """A created grant permits one bounded HTTP call; a replay never permits reissuing it."""
    jobs._writer(con)
    current, clock = aware(at), jobs._clock(at)
    metadata = prepared.metadata()
    policy = canonical(limits.model_dump(mode="json"))
    _cursor(prepared, cursor_in)
    _guard_credential(prepared, *metadata.values(), policy)
    fingerprint = digest(
        canonical(
            {
                "owner_id": str(lease.owner_id),
                "job_id": str(lease.job_id),
                "item_id": str(item_id),
                "generation": lease.generation,
                "context_id": str(prepared.id),
                "context_sha256": digest(canonical(metadata)),
                "cursor_in": cursor_in,
            }
        )
    )
    existing = _select(con, "SELECT * FROM upstream_attempts WHERE id=:id", id=str(attempt_id))
    if existing is not None:
        if existing["request_sha256"] != fingerprint:
            raise jobs.JobError("UPSTREAM_ATTEMPT_CONFLICT")
        return BudgetDecision(
            ReservedAttempt(
                attempt_id,
                UUID(existing["context_id"]),
                datetime.fromisoformat(existing["expires_at"]),
                False,
            ),
            None,
            "ALREADY_RESERVED",
        )
    job, item, clock = jobs.validate_source_request(
        con, lease, item_id, prepared, at=current, cursor_in=cursor_in
    )
    provider = prepared.configuration.provider_id
    if prepared.operation not in registry()[provider].operations:
        raise jobs.JobError("UNSUPPORTED_UPSTREAM_OPERATION")
    member = next((value for value in limits.members if value.provider_id == provider), None)
    profile = prepared.configuration.profile
    if (
        limits.origin_group != registry()[provider].origin_group
        or member is None
        or profile is None
        or member.profile_sha256 != metadata["profile_sha256"]
        or member.requests_per_minute != profile.requests_per_minute
        or member.requests_per_day != profile.requests_per_day
    ):
        raise jobs.JobError("UPSTREAM_BUDGET_SCOPE_MISMATCH")
    if item["status"] not in {"PENDING", "RETRYING", "RUNNING"}:
        raise jobs.JobError("JOB_ITEM_NOT_READY")
    if item["next_run_at"] is not None and item["next_run_at"] > clock:
        return BudgetDecision(None, datetime.fromisoformat(item["next_run_at"]), "ITEM_WAIT")
    state = _state(con, limits.origin_group)
    if state is None:
        con.execute(
            text("""
            INSERT INTO provider_budgets(origin_group,minute_limit,day_limit,tokens_num,
                refilled_at,circuit_state,updated_at)
            VALUES (:origin,:minute,:day,:tokens,:at,'CLOSED',:at)
        """),
            {
                "origin": limits.origin_group,
                "minute": limits.requests_per_minute,
                "day": limits.requests_per_day,
                "tokens": limits.requests_per_minute * TOKEN_UNITS,
                "at": clock,
            },
        )
        state = _state(con, limits.origin_group)
        assert state is not None
    if clock < state["updated_at"]:
        return _waiting(
            con,
            lease,
            item_id,
            current,
            datetime.fromisoformat(state["updated_at"]),
            "CLOCK_REGRESSION",
        )
    state = _reap(con, state, current)
    if (
        _select(
            con,
            """
        SELECT a.id FROM upstream_attempts a WHERE a.item_id=:item
          AND NOT EXISTS(SELECT 1 FROM upstream_outcomes o WHERE o.attempt_id=a.id)
    """,
            item=str(item_id),
        )
        is not None
    ):
        raise jobs.JobError("UPSTREAM_ATTEMPT_IN_FLIGHT")
    tokens = _refilled(state, limits, current)
    waits: list[tuple[datetime, str]] = []
    if state["blocked_until"] is not None and state["blocked_until"] > clock:
        waits.append((datetime.fromisoformat(state["blocked_until"]), "PROVIDER_RETRY_AFTER"))
    if state["circuit_state"] == "OPEN" and state["circuit_until"] > clock:
        waits.append((datetime.fromisoformat(state["circuit_until"]), "CIRCUIT_OPEN"))
    if state["circuit_state"] == "HALF_OPEN":
        probe = _select(
            con,
            "SELECT expires_at FROM upstream_attempts WHERE id=:id",
            id=state["probe_attempt_id"],
        )
        if probe is None:
            raise jobs.JobError("UPSTREAM_CIRCUIT_INCONSISTENT")
        waits.append((datetime.fromisoformat(probe["expires_at"]), "CIRCUIT_PROBE"))
    if tokens < TOKEN_UNITS:
        delay = (
            TOKEN_UNITS - tokens + limits.requests_per_minute - 1
        ) // limits.requests_per_minute
        waits.append((later(current, timedelta(microseconds=delay)), "MINUTE_LIMIT"))
    cutoff = stamp(later(current, -timedelta(days=1)))
    day_count = con.execute(
        text("""
        SELECT count(*) FROM upstream_attempts WHERE origin_group=:origin AND reserved_at>=:cutoff
    """),
        {"origin": limits.origin_group, "cutoff": cutoff},
    ).scalar_one()
    if day_count >= limits.requests_per_day:
        earliest = con.execute(
            text("""
            SELECT reserved_at FROM upstream_attempts WHERE origin_group=:origin
                AND reserved_at>=:cutoff ORDER BY reserved_at,id LIMIT 1 OFFSET :excess
        """),
            {
                "origin": limits.origin_group,
                "cutoff": cutoff,
                "excess": day_count - limits.requests_per_day,
            },
        ).scalar_one()
        waits.append(
            (
                later(datetime.fromisoformat(earliest), timedelta(days=1, microseconds=1)),
                "DAY_LIMIT",
            )
        )
    if waits:
        deadline, reason = max(waits)
        _save(
            con,
            state,
            clock,
            minute_limit=limits.requests_per_minute,
            day_limit=limits.requests_per_day,
            tokens_num=tokens,
            refilled_at=clock,
        )
        return _waiting(con, lease, item_id, current, deadline, reason)
    if item["status"] != "RUNNING":
        item = jobs.begin_item(con, lease, item_id, at=current)
    job, _ = jobs._fence(con, lease, current)
    persist_context(con, prepared)
    reservation = {
        "id": str(attempt_id),
        "owner_id": str(lease.owner_id),
        "job_id": str(lease.job_id),
        "item_id": str(item_id),
        "context_id": str(prepared.id),
        "provider_id": provider,
        "origin_group": limits.origin_group,
        "lease_generation": lease.generation,
        "work_attempt": item["attempt_count"],
        "cursor_in": cursor_in,
        "minute_limit": limits.requests_per_minute,
        "day_limit": limits.requests_per_day,
        "policy_json": policy,
        "policy_sha256": limits.sha256,
        "request_sha256": fingerprint,
        "reserved_at": clock,
        "expires_at": job["lease_expires_at"],
    }
    con.execute(
        text(f"""
        INSERT INTO upstream_attempts({",".join(reservation)})
        VALUES ({",".join(":" + key for key in reservation)})
    """),
        reservation,
    )
    changes: dict[str, Any] = {}
    if state["circuit_state"] == "OPEN":
        changes.update(circuit_state="HALF_OPEN", probe_attempt_id=str(attempt_id))
    _save(
        con,
        state,
        clock,
        minute_limit=limits.requests_per_minute,
        day_limit=limits.requests_per_day,
        tokens_num=tokens - TOKEN_UNITS,
        refilled_at=clock,
        **changes,
    )
    _audit(
        con,
        reservation,
        "UPSTREAM_ATTEMPT_RESERVED",
        work_attempt=item["attempt_count"],
        policy_sha256=limits.sha256,
    )
    jobs._update(con, "jobs", job["id"], clock, result_json=jobs._summary(con, job)[1])
    return BudgetDecision(
        ReservedAttempt(
            attempt_id, prepared.id, datetime.fromisoformat(job["lease_expires_at"]), True
        ),
        None,
        "RESERVED",
    )


def _outcome_state(
    con: Connection, reservation: dict[str, Any], page: Any, at: datetime, deadline: datetime | None
) -> None:
    state = _state(con, reservation["origin_group"])
    assert state is not None
    clock = stamp(at)
    if clock < state["updated_at"]:
        raise jobs.JobError("UPSTREAM_CLOCK_REGRESSION")
    changes: dict[str, Any] = {}
    if deadline is not None and (
        page.state == FetchState.RATE_LIMITED or page.retry_at is not None
    ):
        changes["blocked_until"] = max(state["blocked_until"] or clock, stamp(deadline))
    healthy = page.complete and page.state in {FetchState.OK_WITH_DATA, FetchState.NO_DATA}
    if state["probe_attempt_id"] == reservation["id"]:
        changes.update(
            probe_attempt_id=None,
            circuit_state="CLOSED" if healthy else "OPEN",
            consecutive_failures=0 if healthy else FAILURE_THRESHOLD,
            circuit_until=None if healthy else stamp(later(at, COOLDOWN)),
        )
    elif state["circuit_state"] == "CLOSED":
        if healthy:
            changes["consecutive_failures"] = 0
        elif page.state == FetchState.TRANSIENT_ERROR:
            failures = min(FAILURE_THRESHOLD, state["consecutive_failures"] + 1)
            changes["consecutive_failures"] = failures
            if failures == FAILURE_THRESHOLD:
                changes.update(circuit_state="OPEN", circuit_until=stamp(later(at, COOLDOWN)))
        elif page.state == FetchState.AUTH_ERROR:
            changes.update(circuit_state="OPEN", circuit_until=stamp(later(at, COOLDOWN)))
    _save(con, state, clock, **{"consecutive_failures": state["consecutive_failures"], **changes})


def complete_attempt(
    con: Connection,
    lease: JobLease,
    attempt_id: UUID,
    prepared: PreparedFetch,
    page: FetchPage[DividendObservation] | FetchPage[SplitObservation],
    *,
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    at: datetime,
    jitter_ms: int | None = None,
) -> CompletedAttempt:
    """Commit source/checkpoint/outcome/circuit/work transitions together, never inside HTTP."""
    jobs._writer(con)
    current, clock = aware(at), jobs._clock(at)
    reservation = _select(con, "SELECT * FROM upstream_attempts WHERE id=:id", id=str(attempt_id))
    if reservation is None:
        raise jobs.JobError("UPSTREAM_ATTEMPT_NOT_FOUND")
    if (
        reservation["job_id"] != str(lease.job_id)
        or reservation["owner_id"] != str(lease.owner_id)
        or reservation["lease_generation"] != lease.generation
        or reservation["context_id"] != str(prepared.id)
    ):
        raise jobs.JobError("UPSTREAM_ATTEMPT_CONFLICT")
    outcome = _select(
        con, "SELECT * FROM upstream_outcomes WHERE attempt_id=:id", id=str(attempt_id)
    )
    if outcome is not None and outcome["disposition"] != "COMPLETED":
        raise jobs.JobError("UPSTREAM_ATTEMPT_CLOSED")
    if outcome is None:
        job, _ = jobs._fence(con, lease, current)
        if jobs._clock(started_at) < reservation["reserved_at"] or jobs._clock(finished_at) > clock:
            raise jobs.JobError("INVALID_UPSTREAM_TIMING")
        item = jobs._item(con, job, UUID(reservation["item_id"]))
        if (
            reservation["expires_at"] <= clock
            or item["status"] != "RUNNING"
            or item["attempt_count"] != reservation["work_attempt"]
        ):
            raise jobs.JobError("UPSTREAM_ATTEMPT_EXPIRED")
    receipt = jobs.record_page(
        con,
        lease,
        UUID(reservation["item_id"]),
        prepared,
        page,
        run_id=attempt_id,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        at=current,
        cursor_in=reservation["cursor_in"],
    )
    if outcome is not None:
        return CompletedAttempt(
            receipt,
            datetime.fromisoformat(outcome["retry_at"])
            if outcome["retry_at"] is not None
            else None,
            True,
        )
    retryable = page.state in {FetchState.RATE_LIMITED, FetchState.TRANSIENT_ERROR}
    deadline = (
        retry_deadline(current, reservation["work_attempt"], page.retry_at, jitter_ms=jitter_ms)
        if retryable
        else None
    )
    con.execute(
        text("""
        INSERT INTO upstream_outcomes(attempt_id,disposition,provider_run_id,finished_at,retry_at)
        VALUES (:id,'COMPLETED',:id,:at,:retry)
    """),
        {"id": str(attempt_id), "at": clock, "retry": stamp(deadline) if deadline else None},
    )
    _outcome_state(con, reservation, page, current, deadline)
    item_id = UUID(reservation["item_id"])
    prefix = con.execute(
        text("SELECT count(*) FROM job_checkpoints WHERE item_id=:id"),
        {"id": reservation["item_id"]},
    ).scalar_one()
    partial = bool(page.rows) or prefix > 0
    if page.complete and page.state in {FetchState.OK_WITH_DATA, FetchState.NO_DATA}:
        cycle = (
            page.next_cursor is not None
            and con.execute(
                text("""
            SELECT 1 FROM job_checkpoints p JOIN provider_runs r ON r.id=p.provider_run_id
            WHERE p.item_id=:item AND p.cursor_in=:cursor
              AND r.requested_start=:start AND r.requested_end=:end LIMIT 1
        """),
                {
                    "item": reservation["item_id"],
                    "cursor": page.next_cursor,
                    "start": prepared.segment.window.start,
                    "end": prepared.segment.window.end,
                },
            ).first()
        )
        if cycle:
            jobs.finish_item(
                con, lease, item_id, at=current, status="PARTIAL", error=WorkError.CURSOR_CYCLE
            )
        elif page.next_cursor is None:
            plan = SourceJobPlan.model_validate_json(job["request_json"])
            if prepared.segment.window.end == plan.window.end:
                jobs.finish_item(con, lease, item_id, at=current, status="SUCCEEDED")
    elif retryable:
        assert deadline is not None
        if reservation["work_attempt"] >= 4 and partial:
            jobs.finish_item(
                con, lease, item_id, at=current, status="PARTIAL", error=WorkError.RETRIES_EXHAUSTED
            )
        else:
            jobs.retry_item(
                con,
                lease,
                item_id,
                at=current,
                until=deadline,
                error=WorkError.RATE_LIMITED
                if page.state == FetchState.RATE_LIMITED
                else WorkError.TRANSIENT_ERROR,
            )
    elif page.state in {FetchState.UNSUPPORTED, FetchState.UNCONFIGURED}:
        jobs.finish_item(
            con,
            lease,
            item_id,
            at=current,
            status="SKIPPED",
            error=WorkError.UNSUPPORTED
            if page.state == FetchState.UNSUPPORTED
            else WorkError.UNCONFIGURED,
        )
    else:
        jobs.finish_item(
            con,
            lease,
            item_id,
            at=current,
            status="PARTIAL" if partial else "FAILED",
            error=WorkError.PARSE_ERROR
            if page.state in {FetchState.PARSE_ERROR, FetchState.OK_WITH_DATA, FetchState.NO_DATA}
            else WorkError.PROVIDER_FAILED,
        )
    _audit(
        con,
        reservation,
        "UPSTREAM_ATTEMPT_COMPLETED",
        state=page.state.value,
        run_id=str(attempt_id),
    )
    items = jobs._items(con, str(lease.job_id))
    if all(value["status"] in jobs._ITEM_TERMINAL for value in items):
        jobs.finish(con, lease, at=current)
    else:
        job = jobs.owned_job(con, lease.owner_id, lease.job_id)
        jobs._update(con, "jobs", job["id"], clock, result_json=jobs._summary(con, job)[1])
    return CompletedAttempt(receipt, deadline, False)


def abandon_attempt(
    con: Connection,
    lease: JobLease,
    attempt_id: UUID,
    *,
    at: datetime,
    reason: Literal["CONFIG_CHANGED", "WORKER_ERROR"] = "WORKER_ERROR",
) -> None:
    jobs._writer(con)
    clock = jobs._clock(at)
    reservation = _select(con, "SELECT * FROM upstream_attempts WHERE id=:id", id=str(attempt_id))
    if (
        reservation is None
        or reservation["job_id"] != str(lease.job_id)
        or reservation["owner_id"] != str(lease.owner_id)
        or reservation["lease_generation"] != lease.generation
    ):
        raise jobs.JobError("UPSTREAM_ATTEMPT_CONFLICT")
    existing = _select(
        con, "SELECT * FROM upstream_outcomes WHERE attempt_id=:id", id=str(attempt_id)
    )
    if existing is not None:
        if existing["disposition"] == "COMPLETED" or existing["reason"] not in {
            reason,
            "JOB_CANCELLED",
        }:
            raise jobs.JobError("UPSTREAM_ATTEMPT_CLOSED")
        return
    job, clock = jobs._fence(con, lease, at, states=("RUNNING", "CANCEL_REQUESTED"))
    cancelled = job["status"] == "CANCEL_REQUESTED"
    con.execute(
        text("""
        INSERT INTO upstream_outcomes(attempt_id,disposition,reason,finished_at)
        VALUES (:id,:disposition,:reason,:at)
    """),
        {
            "id": str(attempt_id),
            "disposition": "CANCELLED" if cancelled else "ABANDONED",
            "reason": "JOB_CANCELLED" if cancelled else reason,
            "at": clock,
        },
    )
    state = _state(con, reservation["origin_group"])
    assert state is not None
    if state["probe_attempt_id"] == str(attempt_id):
        _save(
            con,
            state,
            clock,
            circuit_state="OPEN",
            probe_attempt_id=None,
            circuit_until=stamp(later(aware(at), COOLDOWN)),
            consecutive_failures=FAILURE_THRESHOLD,
        )
    else:
        _save(con, state, clock, consecutive_failures=state["consecutive_failures"])
    _audit(
        con,
        reservation,
        "UPSTREAM_ATTEMPT_ABANDONED",
        reason="JOB_CANCELLED" if cancelled else reason,
    )
    if cancelled:
        jobs.acknowledge_cancel(con, lease, at=at)
    else:
        item_id = UUID(reservation["item_id"])
        jobs.finish_item(
            con,
            lease,
            item_id,
            at=at,
            status="FAILED",
            error=WorkError.CONFIG_CHANGED
            if reason == "CONFIG_CHANGED"
            else WorkError.WORKER_ERROR,
        )
        if all(item["status"] in jobs._ITEM_TERMINAL for item in jobs._items(con, job["id"])):
            jobs.finish(con, lease, at=at)
