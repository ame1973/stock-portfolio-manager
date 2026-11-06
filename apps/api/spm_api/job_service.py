"""Durable source jobs: short transactions, fenced leases, independent work and page checkpoints."""

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from spm_api.audit import audit, canonical, digest, stamp
from spm_api.job_models import (
    JobLease,
    SourceJobPlan,
    WorkError,
)
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import DividendObservation, FetchState, SplitObservation
from spm_api.providers.persistence import CaptureReceipt, PreparedFetch, capture_page

KIND = "DIVIDEND_SOURCE_SYNC"
_TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
_ITEM_TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED", "CANCELLED"}


class JobError(ValueError):
    """Stable internal control error; never attach an HTTP response body or private payload."""


def _writer(con: Connection) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise JobError("WRITE_UNIT_OF_WORK_REQUIRED")


def _clock(at: datetime) -> str:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise JobError("AWARE_JOB_TIME_REQUIRED")
    return stamp(at.astimezone(UTC))


def _expiry(at: datetime, seconds: int) -> str:
    if type(seconds) is not int or not 30 <= seconds <= 300:
        raise JobError("INVALID_LEASE_DURATION")
    current = datetime.fromisoformat(_clock(at))
    try:
        return stamp(current + timedelta(seconds=seconds))
    except OverflowError:
        raise JobError("LEASE_TIME_OVERFLOW") from None


def _row(con: Connection, table: str, identifier: str) -> dict[str, Any]:
    value = (
        con.execute(text(f"SELECT * FROM {table} WHERE id=:id"), {"id": identifier})
        .mappings()
        .first()
    )
    if value is None:
        raise JobError("JOB_NOT_FOUND" if table == "jobs" else "JOB_ITEM_NOT_FOUND")
    return dict(value)


def _update(con: Connection, table: str, identifier: str, at: str, **values: Any) -> None:
    assignments = ",".join(f"{key}=:{key}" for key in values)
    con.execute(
        text(f"UPDATE {table} SET {assignments},updated_at=:at,version=version+1 WHERE id=:id"),
        {**values, "at": at, "id": identifier},
    )


def _event(con: Connection, job: dict[str, Any], action: str, **values: Any) -> None:
    audit(con, job["owner_id"], "job", job["id"], action, after=values)


def owned_job(con: Connection, owner_id: UUID, job_id: UUID) -> dict[str, Any]:
    value = (
        con.execute(
            text("SELECT * FROM jobs WHERE id=:id AND owner_id=:owner"),
            {"id": str(job_id), "owner": str(owner_id)},
        )
        .mappings()
        .first()
    )
    if value is None:
        raise JobError("JOB_NOT_FOUND")
    return dict(value)


def enqueue(
    con: Connection, owner_id: UUID, plan: SourceJobPlan, *, intent_id: UUID, at: datetime
) -> dict[str, Any]:
    _writer(con)
    clock = _clock(at)
    plan = SourceJobPlan.model_validate(plan.model_dump(mode="json"))
    active = con.execute(
        text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner_id)}
    ).first()
    if active is None:
        raise JobError("JOB_OWNER_UNAVAILABLE")
    request = canonical(plan.model_dump(mode="json"))
    fingerprint = digest(request)
    key = digest(canonical({"owner": str(owner_id), "kind": KIND, "intent": str(intent_id)}))
    existing = (
        con.execute(text("SELECT * FROM jobs WHERE dedupe_key=:key"), {"key": key})
        .mappings()
        .first()
    )
    if existing is not None:
        if existing["request_sha256"] != fingerprint or existing["owner_id"] != str(owner_id):
            raise JobError("JOB_INTENT_CONFLICT")
        return dict(existing)
    for security in plan.security_ids:
        if (
            con.execute(
                text("SELECT 1 FROM securities WHERE id=:id"), {"id": str(security)}
            ).first()
            is None
        ):
            raise JobError("JOB_SECURITY_NOT_FOUND")
    identifier = str(uuid4())
    con.execute(
        text("""
            INSERT INTO jobs(id,owner_id,kind,dedupe_key,status,progress_total,max_attempts,
                request_json,created_at,runtime_version,request_sha256,updated_at)
            VALUES (:id,:owner,:kind,:key,'QUEUED',:total,4,:request,:at,1,:hash,:at)
        """),
        {
            "id": identifier,
            "owner": str(owner_id),
            "kind": KIND,
            "key": key,
            "total": len(plan.security_ids),
            "request": request,
            "at": clock,
            "hash": fingerprint,
        },
    )
    ordinal = 0
    for security in plan.security_ids:
        for provider in plan.providers:
            for operation in plan.operations:
                con.execute(
                    text("""
                        INSERT INTO job_items(id,job_id,ordinal,security_id,provider_id,operation,
                            status,created_at,updated_at)
                        VALUES (:id,:job,:ordinal,:security,:provider,:operation,'PENDING',:at,:at)
                    """),
                    {
                        "id": str(uuid4()),
                        "job": identifier,
                        "ordinal": ordinal,
                        "security": str(security),
                        "provider": provider.provider_id,
                        "operation": operation,
                        "at": clock,
                    },
                )
                ordinal += 1
    job = _row(con, "jobs", identifier)
    _event(
        con, job, "JOB_CREATED", scope="SOURCE_ONLY", symbols=len(plan.security_ids), items=ordinal
    )
    return job


def _fence(
    con: Connection, lease: JobLease, at: datetime, *, states: tuple[str, ...] = ("RUNNING",)
) -> tuple[dict[str, Any], str]:
    _writer(con)
    clock = _clock(at)
    job = _row(con, "jobs", str(lease.job_id))
    if (
        job["runtime_version"] != 1
        or job["owner_id"] != str(lease.owner_id)
        or job["lease_owner"] != str(lease.worker_id)
        or type(lease.generation) is not int
        or job["lease_generation"] != lease.generation
        or job["status"] not in states
        or job["lease_expires_at"] is None
        or job["lease_expires_at"] <= clock
    ):
        raise JobError("JOB_LEASE_LOST")
    if clock < job["updated_at"]:
        raise JobError("JOB_CLOCK_REGRESSION")
    return job, clock


def _item(con: Connection, job: dict[str, Any], item_id: UUID) -> dict[str, Any]:
    item = _row(con, "job_items", str(item_id))
    if item["job_id"] != job["id"]:
        raise JobError("JOB_ITEM_NOT_FOUND")
    return item


def _items(con: Connection, job_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in con.execute(
            text("SELECT * FROM job_items WHERE job_id=:id ORDER BY ordinal"), {"id": job_id}
        ).mappings()
    ]


def claim(
    con: Connection, worker_id: UUID, *, at: datetime, lease_seconds: int = 90
) -> JobLease | None:
    _writer(con)
    clock, expiry = _clock(at), _expiry(at, lease_seconds)
    cancelled = (
        con.execute(
            text("""
        SELECT * FROM jobs WHERE runtime_version=1 AND status='CANCEL_REQUESTED'
          AND (lease_expires_at IS NULL OR lease_expires_at<=:at) AND updated_at<=:at
        ORDER BY created_at,id LIMIT 1
    """),
            {"at": clock},
        )
        .mappings()
        .first()
    )
    if cancelled is not None:
        _cancel(con, dict(cancelled), clock)
    value = (
        con.execute(
            text("""
        SELECT j.* FROM jobs j JOIN owners o ON o.id=j.owner_id
        WHERE j.kind=:kind AND j.runtime_version=1 AND o.is_active=1 AND j.updated_at<=:at
          AND ((j.status IN ('QUEUED','RETRYING') AND (j.next_run_at IS NULL OR j.next_run_at<=:at))
            OR (j.status IN ('LEASED','RUNNING') AND j.lease_expires_at<=:at))
        ORDER BY j.created_at,j.id LIMIT 1
    """),
            {"kind": KIND, "at": clock},
        )
        .mappings()
        .first()
    )
    if value is None:
        return None
    job = dict(value)
    recovering = job["status"] in {"LEASED", "RUNNING"}
    if recovering:
        for item in _items(con, job["id"]):
            if item["status"] == "RUNNING":
                exhausted = item["attempt_count"] >= job["max_attempts"]
                _update(
                    con,
                    "job_items",
                    item["id"],
                    clock,
                    lease_generation=job["lease_generation"],
                    status="FAILED" if exhausted else "RETRYING",
                    next_run_at=None if exhausted else clock,
                    last_error_code="RETRIES_EXHAUSTED" if exhausted else "LEASE_EXPIRED",
                )
    generation = job["lease_generation"] + 1
    _update(
        con,
        "jobs",
        job["id"],
        clock,
        status="LEASED",
        lease_owner=str(worker_id),
        lease_generation=generation,
        lease_expires_at=expiry,
        next_run_at=None,
    )
    _event(con, job, "JOB_RECOVERED" if recovering else "JOB_LEASED", generation=generation)
    return JobLease(
        UUID(job["id"]),
        UUID(job["owner_id"]),
        worker_id,
        generation,
        datetime.fromisoformat(expiry),
    )


def start(con: Connection, lease: JobLease, *, at: datetime) -> None:
    job, clock = _fence(con, lease, at, states=("LEASED", "RUNNING"))
    if job["status"] == "RUNNING":
        return
    _update(con, "jobs", job["id"], clock, status="RUNNING", started_at=job["started_at"] or clock)
    _event(con, job, "JOB_STARTED", generation=lease.generation)


def renew(con: Connection, lease: JobLease, *, at: datetime, lease_seconds: int = 90) -> JobLease:
    job, clock = _fence(con, lease, at, states=("LEASED", "RUNNING"))
    expiry = max(job["lease_expires_at"], _expiry(at, lease_seconds))
    if expiry != job["lease_expires_at"]:
        _update(con, "jobs", job["id"], clock, lease_expires_at=expiry)
    return JobLease(
        lease.job_id,
        lease.owner_id,
        lease.worker_id,
        lease.generation,
        datetime.fromisoformat(expiry),
    )


def begin_item(con: Connection, lease: JobLease, item_id: UUID, *, at: datetime) -> dict[str, Any]:
    job, clock = _fence(con, lease, at)
    item = _item(con, job, item_id)
    if (
        item["status"] not in {"PENDING", "RETRYING"}
        or (item["next_run_at"] is not None and item["next_run_at"] > clock)
        or item["attempt_count"] >= job["max_attempts"]
    ):
        raise JobError("JOB_ITEM_NOT_READY")
    attempt = item["attempt_count"] + 1
    _update(
        con,
        "job_items",
        item["id"],
        clock,
        lease_generation=job["lease_generation"],
        status="RUNNING",
        attempt_count=attempt,
        next_run_at=None,
    )
    _update(con, "jobs", job["id"], clock, attempt_count=max(job["attempt_count"], attempt))
    return _row(con, "job_items", item["id"])


def _checkpoint(
    con: Connection,
    job: dict[str, Any],
    item: dict[str, Any],
    lease: JobLease,
    run_id: UUID,
    cursor_in: str | None,
    clock: str,
) -> None:
    run = (
        con.execute(
            text("""
        SELECT r.*,c.alias_json,c.profile_sha256,c.parser_version,c.normalization_version
        FROM provider_runs r JOIN provider_fetch_contexts c ON c.id=r.context_id WHERE r.id=:id
    """),
            {"id": str(run_id)},
        )
        .mappings()
        .one()
    )
    if (
        run["job_id"] != job["id"]
        or run["owner_id"] != job["owner_id"]
        or run["security_id"] != item["security_id"]
        or run["provider_id"] != item["provider_id"]
        or run["operation"] != item["operation"]
    ):
        raise JobError("JOB_PAGE_SCOPE_MISMATCH")
    plan = SourceJobPlan.model_validate_json(job["request_json"])
    previous = (
        con.execute(
            text("""
        SELECT p.sequence,r.*,c.alias_json,c.profile_sha256,c.parser_version,c.normalization_version
        FROM job_checkpoints p JOIN provider_runs r ON r.id=p.provider_run_id
        JOIN provider_fetch_contexts c ON c.id=r.context_id
        WHERE p.item_id=:id ORDER BY p.sequence DESC LIMIT 1
    """),
            {"id": item["id"]},
        )
        .mappings()
        .first()
    )
    if previous is None:
        valid = cursor_in is None and run["requested_start"] == plan.window.start
        sequence = 1
    else:
        sequence = previous["sequence"] + 1
        if previous["next_cursor"] is not None:
            valid = cursor_in == previous["next_cursor"] and all(
                run[key] == previous[key]
                for key in (
                    "requested_start",
                    "requested_end",
                    "alias_json",
                    "profile_sha256",
                    "parser_version",
                    "normalization_version",
                )
            )
        else:
            valid = (
                cursor_in is None
                and previous["requested_end"] < plan.window.end
                and run["requested_start"]
                == (date.fromisoformat(previous["requested_end"]) + timedelta(days=1)).isoformat()
            )
    if not valid or run["requested_end"] > plan.window.end:
        raise JobError("JOB_CHECKPOINT_GAP")
    if (
        cursor_in is not None
        and con.execute(
            text("""
        SELECT 1 FROM job_checkpoints p JOIN provider_runs r ON r.id=p.provider_run_id
        WHERE p.item_id=:id AND p.cursor_in=:cursor
          AND r.requested_start=:start AND r.requested_end=:end
    """),
            {
                "id": item["id"],
                "cursor": cursor_in,
                "start": run["requested_start"],
                "end": run["requested_end"],
            },
        ).first()
    ):
        raise JobError("JOB_CURSOR_CYCLE")
    con.execute(
        text("""
        INSERT INTO job_checkpoints(item_id,sequence,provider_run_id,cursor_in,
            lease_generation,created_at)
        VALUES (:item,:sequence,:run,:cursor,:generation,:at)
    """),
        {
            "item": item["id"],
            "sequence": sequence,
            "run": str(run_id),
            "cursor": cursor_in,
            "generation": lease.generation,
            "at": clock,
        },
    )
    _update(con, "jobs", job["id"], clock, result_json=_summary(con, job)[1])
    _event(con, job, "JOB_CHECKPOINT", item_id=item["id"], sequence=sequence, run_id=str(run_id))


def record_page(
    con: Connection,
    lease: JobLease,
    item_id: UUID,
    prepared: PreparedFetch,
    page: FetchPage[DividendObservation] | FetchPage[SplitObservation],
    *,
    run_id: UUID,
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    at: datetime,
    cursor_in: str | None = None,
) -> CaptureReceipt:
    """Fence source capture and checkpoint in one caller UoW, after all external I/O."""
    _writer(con)
    job = owned_job(con, lease.owner_id, lease.job_id)
    item = _item(con, job, item_id)
    if (
        prepared.owner_id != lease.owner_id
        or str(prepared.segment.alias.security_id) != item["security_id"]
        or prepared.segment.alias.provider_id != item["provider_id"]
        or prepared.operation != item["operation"]
    ):
        raise JobError("JOB_PAGE_SCOPE_MISMATCH")
    existing = (
        con.execute(text("SELECT job_id FROM provider_runs WHERE id=:id"), {"id": str(run_id)})
        .mappings()
        .first()
    )
    if existing is None:
        job, clock = _fence(con, lease, at)
        if item["status"] != "RUNNING":
            raise JobError("JOB_ITEM_NOT_RUNNING")
        plan = SourceJobPlan.model_validate_json(job["request_json"])
        selection = next(
            provider for provider in plan.providers if provider.provider_id == item["provider_id"]
        )
        if (
            selection.profile_sha256 is None
            or selection.profile_sha256 != prepared.metadata()["profile_sha256"]
        ):
            raise JobError("JOB_CONFIGURATION_CHANGED")
    elif existing["job_id"] != job["id"]:
        raise JobError("JOB_PAGE_SCOPE_MISMATCH")
    receipt = capture_page(
        con,
        prepared,
        page,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        job_id=lease.job_id,
        requested_cursor=cursor_in,
    )
    if (
        existing is None
        and page.complete
        and page.state in {FetchState.OK_WITH_DATA, FetchState.NO_DATA}
    ):
        _checkpoint(con, job, item, lease, run_id, cursor_in, clock)
    return receipt


def _summary(con: Connection, job: dict[str, Any]) -> tuple[int, str]:
    items = _items(con, job["id"])
    by_security: dict[str, list[str]] = {}
    for item in items:
        by_security.setdefault(item["security_id"], []).append(item["status"])
    progress = sum(
        all(status in _ITEM_TERMINAL - {"CANCELLED"} for status in states)
        for states in by_security.values()
    )
    observations = con.execute(
        text("""
        SELECT (SELECT count(*) FROM dividend_observations o JOIN provider_runs r
                    ON r.id=o.provider_run_id WHERE r.job_id=:job)
             + (SELECT count(*) FROM split_observations o JOIN provider_runs r
                    ON r.id=o.provider_run_id WHERE r.job_id=:job)
    """),
        {"job": job["id"]},
    ).scalar_one()
    return progress, canonical(
        {
            "scope": "SOURCE_ONLY",
            "scanned_symbols": progress,
            "work_units": len(items),
            "work_units_done": sum(
                item["status"] in _ITEM_TERMINAL - {"CANCELLED"} for item in items
            ),
            "observations_new": observations,
            "pages_committed": con.execute(
                text("""
            SELECT count(*) FROM job_checkpoints p JOIN job_items i ON i.id=p.item_id
            WHERE i.job_id=:id
        """),
                {"id": job["id"]},
            ).scalar_one(),
            "failures": sum(item["status"] == "FAILED" for item in items),
            "skipped_unsupported": sum(item["last_error_code"] == "UNSUPPORTED" for item in items),
        }
    )


def finish_item(
    con: Connection,
    lease: JobLease,
    item_id: UUID,
    *,
    at: datetime,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED"],
    error: WorkError | None = None,
) -> None:
    job, clock = _fence(con, lease, at)
    item = _item(con, job, item_id)
    if item["status"] in _ITEM_TERMINAL:
        if item["status"] == status and item["last_error_code"] == error:
            return
        raise JobError("JOB_ITEM_ALREADY_FINISHED")
    if status not in {"SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED"}:
        raise JobError("INVALID_JOB_OUTCOME")
    if status == "SUCCEEDED":
        last = (
            con.execute(
                text("""
            SELECT r.next_cursor,r.requested_end FROM job_checkpoints p
            JOIN provider_runs r ON r.id=p.provider_run_id WHERE p.item_id=:id
            ORDER BY p.sequence DESC LIMIT 1
        """),
                {"id": item["id"]},
            )
            .mappings()
            .first()
        )
        plan = SourceJobPlan.model_validate_json(job["request_json"])
        if (
            item["status"] != "RUNNING"
            or error is not None
            or last is None
            or last["next_cursor"] is not None
            or last["requested_end"] != plan.window.end
        ):
            raise JobError("JOB_WINDOW_NOT_PROCESSED")
    elif error is None or not isinstance(error, WorkError):
        raise JobError("JOB_ERROR_CODE_REQUIRED")
    if status == "PARTIAL" and (
        item["status"] != "RUNNING"
        or con.execute(
            text("""
            SELECT 1 FROM provider_runs r WHERE job_id=:job AND security_id=:security
              AND provider_id=:provider AND operation=:operation AND (observations_count>0
                OR EXISTS(SELECT 1 FROM job_checkpoints p
                    WHERE p.provider_run_id=r.id AND p.item_id=:item)) LIMIT 1
        """),
            {
                "job": job["id"],
                "security": item["security_id"],
                "item": item["id"],
                "provider": item["provider_id"],
                "operation": item["operation"],
            },
        ).first()
        is None
    ):
        raise JobError("JOB_PARTIAL_EVIDENCE_REQUIRED")
    _update(
        con,
        "job_items",
        item["id"],
        clock,
        lease_generation=job["lease_generation"],
        status=status,
        next_run_at=None,
        last_error_code=error.value if error is not None else None,
    )
    progress, result = _summary(con, job)
    _update(
        con,
        "jobs",
        job["id"],
        clock,
        progress_done=progress,
        result_json=result,
        last_error_code=error.value if error is not None else job["last_error_code"],
    )
    _event(
        con,
        job,
        "JOB_ITEM_FINISHED",
        item_id=item["id"],
        status=status,
        error=error.value if error is not None else None,
    )


def retry_item(
    con: Connection,
    lease: JobLease,
    item_id: UUID,
    *,
    at: datetime,
    until: datetime,
    error: WorkError,
) -> None:
    job, clock = _fence(con, lease, at)
    deadline = _clock(until)
    item = _item(con, job, item_id)
    if item["status"] != "RUNNING" or deadline <= clock or not isinstance(error, WorkError):
        raise JobError("INVALID_JOB_RETRY")
    if item["attempt_count"] >= job["max_attempts"]:
        finish_item(con, lease, item_id, at=at, status="FAILED", error=WorkError.RETRIES_EXHAUSTED)
        return
    _update(
        con,
        "job_items",
        item["id"],
        clock,
        lease_generation=job["lease_generation"],
        status="RETRYING",
        next_run_at=deadline,
        last_error_code=error.value,
    )
    _update(con, "jobs", job["id"], clock, last_error_code=error.value)


def defer_item(
    con: Connection,
    lease: JobLease,
    item_id: UUID,
    *,
    at: datetime,
    until: datetime,
    error: WorkError,
) -> None:
    job, clock = _fence(con, lease, at)
    item, deadline = _item(con, job, item_id), _clock(until)
    if (
        item["status"] not in {"PENDING", "RETRYING"}
        or deadline <= clock
        or error not in {WorkError.QUOTA_WAIT, WorkError.CIRCUIT_OPEN}
    ):
        raise JobError("INVALID_JOB_DEFERRAL")
    _update(
        con,
        "job_items",
        item["id"],
        clock,
        lease_generation=job["lease_generation"],
        status="RETRYING",
        next_run_at=deadline,
        last_error_code=error.value,
    )
    _update(con, "jobs", job["id"], clock, last_error_code=error.value)


def park(con: Connection, lease: JobLease, *, at: datetime) -> None:
    job, clock = _fence(con, lease, at)
    items = _items(con, job["id"])
    remaining = [item for item in items if item["status"] not in _ITEM_TERMINAL]
    if not remaining or any(
        item["status"] == "RUNNING" or item["next_run_at"] is None or item["next_run_at"] <= clock
        for item in remaining
    ):
        raise JobError("JOB_NOT_WAITING")
    deadline = min(item["next_run_at"] for item in remaining)
    progress, result = _summary(con, job)
    _update(
        con,
        "jobs",
        job["id"],
        clock,
        status="RETRYING",
        next_run_at=deadline,
        lease_owner=None,
        lease_expires_at=None,
        progress_done=progress,
        result_json=result,
    )
    _event(con, job, "JOB_WAITING", next_run_at=deadline)


def finish(con: Connection, lease: JobLease, *, at: datetime) -> None:
    job, clock = _fence(con, lease, at)
    items = _items(con, job["id"])
    if not items or any(item["status"] not in _ITEM_TERMINAL for item in items):
        raise JobError("JOB_NOT_FINISHED")
    if all(item["status"] == "SUCCEEDED" for item in items):
        status = "SUCCEEDED"
    elif any(item["status"] in {"SUCCEEDED", "PARTIAL"} for item in items):
        status = "PARTIAL"
    else:
        status = "FAILED"
    progress, result = _summary(con, job)
    _update(
        con,
        "jobs",
        job["id"],
        clock,
        status=status,
        finished_at=clock,
        lease_owner=None,
        lease_expires_at=None,
        progress_done=progress,
        result_json=result,
        last_error_code=(
            None
            if status == "SUCCEEDED"
            else next(
                (item["last_error_code"] for item in items if item["last_error_code"] is not None),
                None,
            )
        ),
    )
    _event(con, job, "JOB_COMPLETED", status=status, progress_done=progress)


def _cancel(con: Connection, job: dict[str, Any], clock: str) -> None:
    for item in _items(con, job["id"]):
        if item["status"] not in _ITEM_TERMINAL:
            _update(
                con,
                "job_items",
                item["id"],
                clock,
                lease_generation=job["lease_generation"],
                status="CANCELLED",
                next_run_at=None,
            )
    progress, result = _summary(con, job)
    _update(
        con,
        "jobs",
        job["id"],
        clock,
        status="CANCELLED",
        finished_at=clock,
        lease_owner=None,
        lease_expires_at=None,
        next_run_at=None,
        progress_done=progress,
        result_json=result,
    )
    _event(con, job, "JOB_CANCELLED", progress_done=progress)


def request_cancel(
    con: Connection, owner_id: UUID, job_id: UUID, *, at: datetime
) -> dict[str, Any]:
    _writer(con)
    job, clock = owned_job(con, owner_id, job_id), _clock(at)
    if job["status"] in _TERMINAL or job["status"] == "CANCEL_REQUESTED":
        return job
    if job["runtime_version"] != 1:
        raise JobError("JOB_UNMANAGED")
    if clock < job["updated_at"]:
        raise JobError("JOB_CLOCK_REGRESSION")
    if job["lease_expires_at"] is not None and job["lease_expires_at"] > clock:
        _update(con, "jobs", job["id"], clock, status="CANCEL_REQUESTED")
        _event(con, job, "JOB_CANCEL_REQUESTED")
    else:
        _cancel(con, job, clock)
    return _row(con, "jobs", job["id"])


def acknowledge_cancel(con: Connection, lease: JobLease, *, at: datetime) -> None:
    job, clock = _fence(con, lease, at, states=("CANCEL_REQUESTED",))
    _cancel(con, job, clock)
