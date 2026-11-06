"""Persist conservative shared upstream spending, immutable attempts and resumable work rounds."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op
from sqlalchemy import Connection

revision = "0010_upstream_budgets"
down_revision = "0009_source_jobs"
branch_labels = None
depends_on = None

_ORIGINS = "'EODHD','ALPHAVANTAGE','MASSIVE','YAHOO'"
_ORIGIN = """CASE {value} WHEN 'eodhd' THEN 'EODHD' WHEN 'alphavantage' THEN 'ALPHAVANTAGE'
    WHEN 'massive' THEN 'MASSIVE' WHEN 'yahoo' THEN 'YAHOO' WHEN 'yfinance' THEN 'YAHOO' END"""
_TERMINAL = "'SUCCEEDED','PARTIAL','FAILED','SKIPPED','CANCELLED'"


def _utc(value: str) -> str:
    return f"""(typeof({value})='text' AND length({value})=27
        AND substr({value},11,1)='T' AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*' AND substr({value},1,4)!='0000'
        AND date(substr({value},1,10),'+0 days') IS substr({value},1,10)
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _hash(value: str) -> str:
    return f"(typeof({value})='text' AND length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"


def _immutable(con: Connection, table: str) -> None:
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_{table}_no_{operation.lower()} BEFORE {operation} ON {table}
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_UPSTREAM_ATTEMPT'); END
        """)


def upgrade() -> None:
    con = op.get_bind()
    if con.exec_driver_sql("""
        SELECT 1 FROM job_items WHERE status='RUNNING' AND attempt_count<1 LIMIT 1
    """).first():
        raise RuntimeError("Invalid retained running work requires explicit review")
    con.exec_driver_sql("DROP TRIGGER trg_job_item_update")
    con.exec_driver_sql("""
        ALTER TABLE job_items ADD COLUMN round_open INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(round_open)='integer' AND round_open IN (0,1))
    """)
    con.exec_driver_sql("UPDATE job_items SET round_open=1 WHERE status='RUNNING'")
    con.exec_driver_sql("""
        CREATE TRIGGER trg_job_round_insert BEFORE INSERT ON job_items
        WHEN NEW.round_open!=0
        BEGIN SELECT RAISE(ABORT,'INVALID_JOB_ROUND'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_job_item_update BEFORE UPDATE ON job_items
        WHEN OLD.status IN ({_TERMINAL}) OR NEW.id IS NOT OLD.id OR NEW.job_id IS NOT OLD.job_id
          OR NEW.ordinal IS NOT OLD.ordinal OR NEW.security_id IS NOT OLD.security_id
          OR NEW.provider_id IS NOT OLD.provider_id OR NEW.operation IS NOT OLD.operation
          OR NEW.created_at IS NOT OLD.created_at OR NEW.version!=OLD.version+1
          OR NEW.updated_at<OLD.updated_at
          OR (NEW.status='RUNNING' AND (OLD.status NOT IN ('PENDING','RETRYING')
                OR NEW.round_open!=1
                OR NEW.attempt_count!=OLD.attempt_count
                    +CASE OLD.round_open WHEN 1 THEN 0 ELSE 1 END))
          OR (NEW.status!='RUNNING' AND NEW.attempt_count!=OLD.attempt_count)
          OR (NEW.status IN ({_TERMINAL}) AND NEW.round_open!=0)
          OR (NEW.round_open=1 AND (NEW.attempt_count<1
                OR NEW.status NOT IN ('RUNNING','RETRYING')))
          OR (NEW.status='RETRYING' AND NEW.round_open=1 AND (
                NEW.last_error_code NOT IN ('QUOTA_WAIT','CIRCUIT_OPEN') OR OLD.round_open!=1))
          OR (NEW.status IN ('RUNNING',{_TERMINAL}) AND NEW.next_run_at IS NOT NULL)
          OR NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=NEW.job_id AND j.runtime_version=1
                AND j.status IN ('QUEUED','LEASED','RUNNING','RETRYING','CANCEL_REQUESTED')
                AND j.lease_generation=NEW.lease_generation
                AND (NEW.status='CANCELLED' OR j.status IN ('LEASED','RUNNING'))
                AND (NEW.status!='RUNNING' OR (
                    j.status='RUNNING' AND j.lease_expires_at>NEW.updated_at)))
          OR (NEW.status='SUCCEEDED' AND (OLD.status!='RUNNING' OR NOT EXISTS(
                SELECT 1 FROM job_checkpoints p JOIN provider_runs r ON r.id=p.provider_run_id
                JOIN jobs j ON j.id=NEW.job_id WHERE p.item_id=NEW.id
                  AND p.sequence=(SELECT max(sequence) FROM job_checkpoints WHERE item_id=NEW.id)
                  AND r.next_cursor IS NULL
                  AND r.requested_end=json_extract(j.request_json,'$.window.end'))))
        BEGIN SELECT RAISE(ABORT,'INVALID_JOB_ITEM_TRANSITION'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE provider_budgets (
            origin_group TEXT PRIMARY KEY CHECK(origin_group IN ({_ORIGINS})),
            minute_limit INTEGER NOT NULL CHECK(typeof(minute_limit)='integer'
                AND minute_limit BETWEEN 1 AND 2147483647),
            day_limit INTEGER NOT NULL CHECK(typeof(day_limit)='integer'
                AND day_limit BETWEEN 1 AND 2147483647),
            tokens_num INTEGER NOT NULL CHECK(typeof(tokens_num)='integer'
                AND tokens_num BETWEEN 0 AND minute_limit*60000000),
            refilled_at TEXT NOT NULL CHECK({_utc("refilled_at")}),
            blocked_until TEXT CHECK(blocked_until IS NULL OR {_utc("blocked_until")}),
            circuit_state TEXT NOT NULL CHECK(circuit_state IN ('CLOSED','OPEN','HALF_OPEN')),
            circuit_until TEXT CHECK(circuit_until IS NULL OR {_utc("circuit_until")}),
            consecutive_failures INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(consecutive_failures)='integer'
                    AND consecutive_failures BETWEEN 0 AND 5),
            probe_attempt_id TEXT REFERENCES upstream_attempts(id) ON DELETE RESTRICT,
            version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
            updated_at TEXT NOT NULL CHECK({_utc("updated_at")} AND updated_at>=refilled_at),
            CHECK((circuit_state='CLOSED' AND circuit_until IS NULL AND probe_attempt_id IS NULL)
               OR (circuit_state='OPEN' AND circuit_until IS NOT NULL AND probe_attempt_id IS NULL)
               OR (circuit_state='HALF_OPEN' AND circuit_until IS NOT NULL
                    AND probe_attempt_id IS NOT NULL))
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE upstream_attempts (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
            item_id TEXT NOT NULL REFERENCES job_items(id) ON DELETE RESTRICT,
            context_id TEXT NOT NULL REFERENCES provider_fetch_contexts(id) ON DELETE RESTRICT,
            provider_id TEXT NOT NULL,
            origin_group TEXT NOT NULL REFERENCES provider_budgets(origin_group) ON DELETE RESTRICT,
            lease_generation INTEGER NOT NULL
                CHECK(typeof(lease_generation)='integer' AND lease_generation>=1),
            work_attempt INTEGER NOT NULL CHECK(typeof(work_attempt)='integer'
                AND work_attempt BETWEEN 1 AND 4),
            cursor_in TEXT CHECK(cursor_in IS NULL OR length(cursor_in) BETWEEN 1 AND 4096),
            minute_limit INTEGER NOT NULL CHECK(typeof(minute_limit)='integer'
                AND minute_limit BETWEEN 1 AND 2147483647),
            day_limit INTEGER NOT NULL CHECK(typeof(day_limit)='integer'
                AND day_limit BETWEEN 1 AND 2147483647),
            policy_json TEXT NOT NULL CHECK(json_valid(policy_json)
                AND json_type(policy_json)='object' AND length(policy_json)<=16384),
            policy_sha256 TEXT NOT NULL CHECK({_hash("policy_sha256")}),
            request_sha256 TEXT NOT NULL CHECK({_hash("request_sha256")}),
            reserved_at TEXT NOT NULL CHECK({_utc("reserved_at")}),
            expires_at TEXT NOT NULL CHECK({_utc("expires_at")} AND expires_at>reserved_at)
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_upstream_origin_window ON upstream_attempts(origin_group,reserved_at,id)
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_upstream_item ON upstream_attempts(item_id,reserved_at,id)
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE upstream_outcomes (
            attempt_id TEXT PRIMARY KEY REFERENCES upstream_attempts(id) ON DELETE RESTRICT,
            disposition TEXT NOT NULL CHECK(disposition IN ('COMPLETED','ABANDONED','CANCELLED')),
            provider_run_id TEXT UNIQUE REFERENCES provider_runs(id) ON DELETE RESTRICT,
            reason TEXT CHECK(reason IS NULL OR reason IN
                ('LEASE_EXPIRED','JOB_CANCELLED','CONFIG_CHANGED','WORKER_ERROR')),
            finished_at TEXT NOT NULL CHECK({_utc("finished_at")}),
            retry_at TEXT CHECK(retry_at IS NULL OR {_utc("retry_at")}),
            CHECK((disposition='COMPLETED' AND provider_run_id IS NOT NULL
                    AND provider_run_id=attempt_id AND reason IS NULL)
               OR (disposition!='COMPLETED' AND provider_run_id IS NULL AND reason IS NOT NULL))
        )
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_budget_update BEFORE UPDATE ON provider_budgets
        WHEN NEW.origin_group IS NOT OLD.origin_group OR NEW.version!=OLD.version+1
          OR NEW.updated_at<OLD.updated_at OR NEW.refilled_at<OLD.refilled_at
          OR (NEW.probe_attempt_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM upstream_attempts a WHERE a.id=NEW.probe_attempt_id
                  AND a.origin_group=NEW.origin_group AND NOT EXISTS(
                    SELECT 1 FROM upstream_outcomes o WHERE o.attempt_id=a.id)))
        BEGIN SELECT RAISE(ABORT,'INVALID_UPSTREAM_BUDGET'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_budget_no_delete BEFORE DELETE ON provider_budgets
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_UPSTREAM_BUDGET_HISTORY'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_upstream_attempt_insert BEFORE INSERT ON upstream_attempts
        WHEN NEW.origin_group IS NOT ({_ORIGIN.format(value="NEW.provider_id")})
          OR (NEW.cursor_in IS NOT NULL AND (
                NEW.provider_id!='massive' OR instr(NEW.cursor_in,'#') OR instr(NEW.cursor_in,'@')
                OR instr(lower(NEW.cursor_in),'api_key') OR instr(lower(NEW.cursor_in),'api_token')
                OR instr(lower(NEW.cursor_in),'authorization') OR NOT EXISTS(
                    SELECT 1 FROM provider_fetch_contexts c WHERE c.id=NEW.context_id AND (
                        NEW.cursor_in='/stocks/v1/'||lower(c.operation)
                        OR instr(NEW.cursor_in,'/stocks/v1/'||lower(c.operation)||'?')=1))))
          OR json_type(NEW.policy_json,'$.schema_version') IS NOT 'integer'
          OR json_extract(NEW.policy_json,'$.schema_version') IS NOT 1
          OR json_extract(NEW.policy_json,'$.origin_group') IS NOT NEW.origin_group
          OR json_type(NEW.policy_json,'$.requests_per_minute') IS NOT 'integer'
          OR json_type(NEW.policy_json,'$.requests_per_day') IS NOT 'integer'
          OR json_extract(NEW.policy_json,'$.requests_per_minute') IS NOT NEW.minute_limit
          OR json_extract(NEW.policy_json,'$.requests_per_day') IS NOT NEW.day_limit
          OR json_type(NEW.policy_json,'$.members') IS NOT 'array'
          OR json_array_length(NEW.policy_json,'$.members') NOT BETWEEN 1 AND 2
          OR EXISTS(SELECT 1 FROM json_each(NEW.policy_json) WHERE key NOT IN
                ('schema_version','origin_group','requests_per_minute','requests_per_day','members'))
          OR EXISTS(SELECT 1 FROM json_each(NEW.policy_json,'$.members') m
                WHERE m.type!='object'
                  OR ({_ORIGIN.format(value="json_extract(m.value,'$.provider_id')")})
                        IS NOT NEW.origin_group
                  OR NOT {_hash("json_extract(m.value,'$.profile_sha256')")}
                  OR json_type(m.value,'$.requests_per_minute') IS NOT 'integer'
                  OR json_type(m.value,'$.requests_per_day') IS NOT 'integer'
                  OR json_extract(m.value,'$.requests_per_minute') NOT BETWEEN 1 AND 2147483647
                  OR json_extract(m.value,'$.requests_per_day') NOT BETWEEN 1 AND 2147483647
                  OR EXISTS(SELECT 1 FROM json_each(m.value) WHERE key NOT IN
                        ('provider_id','profile_sha256','requests_per_minute','requests_per_day')))
          OR (SELECT min(json_extract(value,'$.requests_per_minute'))
                FROM json_each(NEW.policy_json,'$.members')) IS NOT NEW.minute_limit
          OR (SELECT min(json_extract(value,'$.requests_per_day'))
                FROM json_each(NEW.policy_json,'$.members')) IS NOT NEW.day_limit
          OR (SELECT count(DISTINCT json_extract(value,'$.provider_id'))
                FROM json_each(NEW.policy_json,'$.members'))
                    !=json_array_length(NEW.policy_json,'$.members')
          OR EXISTS(SELECT 1 FROM upstream_attempts a WHERE a.item_id=NEW.item_id
                AND NOT EXISTS(SELECT 1 FROM upstream_outcomes o WHERE o.attempt_id=a.id))
          OR NOT EXISTS(SELECT 1 FROM jobs j JOIN job_items i ON i.job_id=j.id
                JOIN provider_fetch_contexts c ON c.id=NEW.context_id
                WHERE j.id=NEW.job_id AND j.owner_id=NEW.owner_id AND j.runtime_version=1
                  AND j.status='RUNNING' AND j.lease_generation=NEW.lease_generation
                  AND j.lease_expires_at=NEW.expires_at AND NEW.reserved_at>=j.updated_at
                  AND i.id=NEW.item_id AND i.status='RUNNING' AND i.round_open=1
                  AND i.lease_generation=NEW.lease_generation AND i.attempt_count=NEW.work_attempt
                  AND i.provider_id=NEW.provider_id AND c.provider_id=NEW.provider_id
                  AND c.security_id=i.security_id AND c.operation=i.operation
                  AND c.owner_id=j.owner_id AND c.origin_group=NEW.origin_group
                  AND EXISTS(SELECT 1 FROM json_each(j.request_json,'$.providers')
                        WHERE json_extract(value,'$.provider_id')=NEW.provider_id
                          AND json_extract(value,'$.profile_sha256')=c.profile_sha256)
                  AND EXISTS(SELECT 1 FROM json_each(NEW.policy_json,'$.members')
                        WHERE json_extract(value,'$.provider_id')=NEW.provider_id
                          AND json_extract(value,'$.profile_sha256')=c.profile_sha256))
        BEGIN SELECT RAISE(ABORT,'INVALID_UPSTREAM_ATTEMPT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_upstream_outcome_insert BEFORE INSERT ON upstream_outcomes
        WHEN NOT EXISTS(SELECT 1 FROM upstream_attempts a JOIN jobs j ON j.id=a.job_id
            JOIN job_items i ON i.id=a.item_id WHERE a.id=NEW.attempt_id
              AND NEW.finished_at>=a.reserved_at AND (
                (NEW.disposition='COMPLETED' AND j.status='RUNNING' AND i.status='RUNNING'
                  AND j.lease_generation=a.lease_generation AND j.lease_expires_at>NEW.finished_at
                  AND a.expires_at>NEW.finished_at AND EXISTS(SELECT 1 FROM provider_runs r
                    WHERE r.id=NEW.provider_run_id AND r.id=a.id AND r.job_id=a.job_id
                      AND r.owner_id=a.owner_id AND r.context_id=a.context_id
                      AND r.started_at>=a.reserved_at AND r.finished_at<=NEW.finished_at
                      AND r.requested_cursor IS a.cursor_in))
                OR (NEW.disposition!='COMPLETED' AND (
                    NEW.reason IN ('CONFIG_CHANGED','WORKER_ERROR')
                    OR j.status IN ('CANCEL_REQUESTED','CANCELLED')
                    OR j.lease_generation!=a.lease_generation OR j.lease_expires_at IS NULL
                    OR j.lease_expires_at<=NEW.finished_at OR a.expires_at<=NEW.finished_at
                    OR i.status!='RUNNING' OR i.attempt_count!=a.work_attempt))))
        BEGIN SELECT RAISE(ABORT,'INVALID_UPSTREAM_OUTCOME'); END
    """)
    _immutable(con, "upstream_attempts")
    _immutable(con, "upstream_outcomes")
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_UPSTREAM_BUDGETS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; original facts/attempts retained without quota reset",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting upstream history"
    )
