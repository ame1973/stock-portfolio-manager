"""Add fenced managed source jobs without adopting or rewriting retained legacy queue rows."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op
from sqlalchemy import Connection

revision = "0009_source_jobs"
down_revision = "0008_provider_evidence"
branch_labels = None
depends_on = None

_PROVIDERS = (
    "'eodhd','alphavantage','massive','yahoo','yfinance','hkex_evidence','sec_evidence','manual'"
)
_TERMINAL = "'SUCCEEDED','PARTIAL','FAILED','CANCELLED'"
_ITEM_TERMINAL = "'SUCCEEDED','PARTIAL','FAILED','SKIPPED','CANCELLED'"
_ERRORS = (
    "'UNCONFIGURED','UNSUPPORTED','SYMBOL_NOT_FOUND','SYMBOL_AMBIGUOUS','ALIAS_CHANGED',"
    "'CONFIG_CHANGED','PROVIDER_FAILED','PARSE_ERROR','RATE_LIMITED','TRANSIENT_ERROR',"
    "'QUOTA_WAIT','CIRCUIT_OPEN','LEASE_EXPIRED','RETRIES_EXHAUSTED','WORKER_ERROR',"
    "'PAGE_LIMIT','CURSOR_CYCLE'"
)


def _utc(value: str) -> str:
    return f"""(typeof({value})='text' AND length({value})=27
        AND substr({value},11,1)='T' AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND substr({value},1,4)!='0000'
        AND date(substr({value},1,10),'+0 days') IS substr({value},1,10)
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _hash(value: str) -> str:
    return f"(typeof({value})='text' AND length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"


def _controls(prefix: str) -> str:
    return f"""
        {prefix}.runtime_version!=1 OR {prefix}.kind!='DIVIDEND_SOURCE_SYNC'
        OR typeof({prefix}.version)!='integer' OR {prefix}.version<1
        OR typeof({prefix}.lease_generation)!='integer' OR {prefix}.lease_generation<0
        OR typeof({prefix}.progress_done)!='integer' OR typeof({prefix}.progress_total)!='integer'
        OR {prefix}.progress_total NOT BETWEEN 1 AND 100
        OR {prefix}.progress_done>{prefix}.progress_total
        OR typeof({prefix}.attempt_count)!='integer' OR {prefix}.attempt_count NOT BETWEEN 0 AND 4
        OR {prefix}.max_attempts!=4 OR {prefix}.request_sha256 IS NULL
        OR NOT {_hash(prefix + ".request_sha256")}
        OR NOT {_utc(prefix + ".created_at")} OR NOT {_utc(prefix + ".updated_at")}
        OR {prefix}.updated_at<{prefix}.created_at
        OR ({prefix}.started_at IS NOT NULL AND (NOT {_utc(prefix + ".started_at")}
                OR {prefix}.started_at<{prefix}.created_at))
        OR ({prefix}.status='RUNNING' AND {prefix}.started_at IS NULL)
        OR ({prefix}.finished_at IS NOT NULL AND (NOT {_utc(prefix + ".finished_at")}
                OR {prefix}.finished_at<{prefix}.created_at
                OR {prefix}.finished_at<{prefix}.started_at))
        OR ({prefix}.next_run_at IS NOT NULL AND NOT {_utc(prefix + ".next_run_at")})
        OR ({prefix}.lease_expires_at IS NOT NULL AND NOT {_utc(prefix + ".lease_expires_at")})
        OR (({prefix}.status IN ('LEASED','RUNNING','CANCEL_REQUESTED')) !=
                ({prefix}.lease_owner IS NOT NULL AND {prefix}.lease_expires_at IS NOT NULL))
        OR (({prefix}.lease_owner IS NULL)!=({prefix}.lease_expires_at IS NULL))
        OR ({prefix}.status IN ('LEASED','RUNNING','CANCEL_REQUESTED') AND (
                {prefix}.lease_generation<1 OR {prefix}.lease_expires_at<={prefix}.updated_at))
        OR (({prefix}.status IN ({_TERMINAL}))!=({prefix}.finished_at IS NOT NULL))
        OR ({prefix}.status!='RETRYING' AND {prefix}.next_run_at IS NOT NULL)
        OR ({prefix}.status='RETRYING' AND {prefix}.next_run_at IS NULL)
        OR ({prefix}.last_error_code IS NOT NULL AND {prefix}.last_error_code NOT IN ({_ERRORS}))
    """


def _immutable(con: Connection, table: str) -> None:
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_{table}_no_{operation.lower()} BEFORE {operation} ON {table}
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_JOB_CHECKPOINT'); END
        """)


def upgrade() -> None:
    con = op.get_bind()
    for name, declaration in (
        ("runtime_version", "INTEGER NOT NULL DEFAULT 0 CHECK(runtime_version IN (0,1))"),
        ("version", "INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1)"),
        (
            "lease_generation",
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(typeof(lease_generation)='integer' AND lease_generation>=0)",
        ),
        ("request_sha256", "TEXT"),
        ("updated_at", "TEXT"),
    ):
        con.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
    con.exec_driver_sql("ALTER TABLE provider_runs ADD COLUMN requested_cursor TEXT")
    con.exec_driver_sql("""
        CREATE TRIGGER trg_provider_requested_cursor BEFORE INSERT ON provider_runs
        WHEN NEW.requested_cursor IS NOT NULL AND (
            NEW.provider_id!='massive' OR length(NEW.requested_cursor) NOT BETWEEN 1 AND 4096
            OR substr(NEW.requested_cursor,1,1)!='/'
            OR instr(NEW.requested_cursor,'#') OR instr(NEW.requested_cursor,'@')
            OR instr(lower(NEW.requested_cursor),'api_key')
            OR instr(lower(NEW.requested_cursor),'api_token')
            OR instr(lower(NEW.requested_cursor),'authorization'))
        BEGIN SELECT RAISE(ABORT,'INVALID_REQUESTED_CURSOR'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE job_items (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL
                CHECK(typeof(ordinal)='integer' AND ordinal BETWEEN 0 AND 1599),
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
            provider_id TEXT NOT NULL CHECK(provider_id IN ({_PROVIDERS})),
            operation TEXT NOT NULL CHECK(operation IN ('DIVIDENDS','SPLITS')),
            status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','RETRYING',{_ITEM_TERMINAL})),
            attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 4),
            lease_generation INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(lease_generation)='integer' AND lease_generation>=0),
            next_run_at TEXT CHECK(next_run_at IS NULL OR {_utc("next_run_at")}),
            last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN ({_ERRORS})),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            updated_at TEXT NOT NULL CHECK({_utc("updated_at")} AND updated_at>=created_at),
            version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
            UNIQUE(job_id,ordinal),
            UNIQUE(job_id,security_id,provider_id,operation)
        )
    """)
    con.exec_driver_sql(
        "CREATE INDEX idx_job_items_ready ON job_items(job_id,status,next_run_at,ordinal)"
    )
    con.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_job_item_running ON job_items(job_id) WHERE status='RUNNING'"
    )
    con.exec_driver_sql(f"""
        CREATE TABLE job_checkpoints (
            item_id TEXT NOT NULL REFERENCES job_items(id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL
                CHECK(typeof(sequence)='integer' AND sequence BETWEEN 1 AND 10000),
            provider_run_id TEXT NOT NULL UNIQUE REFERENCES provider_runs(id) ON DELETE RESTRICT,
            cursor_in TEXT CHECK(cursor_in IS NULL OR length(cursor_in) BETWEEN 1 AND 4096),
            lease_generation INTEGER NOT NULL
                CHECK(typeof(lease_generation)='integer' AND lease_generation>=1),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            PRIMARY KEY(item_id,sequence)
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_job_insert BEFORE INSERT ON jobs
        WHEN {_controls("NEW")}
          OR NEW.status!='QUEUED' OR NEW.version!=1 OR NEW.lease_generation!=0
          OR NEW.progress_done!=0 OR NEW.attempt_count!=0 OR NEW.started_at IS NOT NULL
          OR NEW.result_json IS NOT NULL OR NEW.last_error_code IS NOT NULL
          OR length(NEW.request_json)>65536 OR json_type(NEW.request_json)!='object'
          OR json_type(NEW.request_json,'$.schema_version') IS NOT 'integer'
          OR json_extract(NEW.request_json,'$.schema_version') IS NOT 1
          OR json_extract(NEW.request_json,'$.scope') IS NOT 'SOURCE_ONLY'
          OR json_type(NEW.request_json,'$.security_ids') IS NOT 'array'
          OR json_array_length(NEW.request_json,'$.security_ids') IS NOT NEW.progress_total
          OR EXISTS(SELECT 1 FROM json_each(NEW.request_json,'$.security_ids') WHERE type!='text')
          OR (SELECT count(DISTINCT value) FROM json_each(NEW.request_json,'$.security_ids'))
                !=NEW.progress_total
          OR json_type(NEW.request_json,'$.providers') IS NOT 'array'
          OR json_array_length(NEW.request_json,'$.providers') NOT BETWEEN 1 AND 8
          OR EXISTS(SELECT 1 FROM json_each(NEW.request_json,'$.providers') p
                WHERE p.type!='object'
                OR json_extract(p.value,'$.provider_id') NOT IN ({_PROVIDERS})
                OR json_type(p.value,'$.provider_id') IS NOT 'text'
                OR (json_type(p.value,'$.profile_sha256') IS NOT 'text'
                    AND json_type(p.value,'$.profile_sha256') IS NOT 'null')
                OR (json_extract(p.value,'$.profile_sha256') IS NOT NULL
                    AND NOT {_hash("json_extract(p.value,'$.profile_sha256')")})
                OR EXISTS(SELECT 1 FROM json_each(p.value)
                    WHERE key NOT IN ('provider_id','profile_sha256')))
          OR (SELECT count(DISTINCT json_extract(value,'$.provider_id'))
                FROM json_each(NEW.request_json,'$.providers'))
                    !=json_array_length(NEW.request_json,'$.providers')
          OR json_type(NEW.request_json,'$.operations') IS NOT 'array'
          OR json_array_length(NEW.request_json,'$.operations') NOT BETWEEN 1 AND 2
          OR EXISTS(SELECT 1 FROM json_each(NEW.request_json,'$.operations')
                    WHERE type!='text' OR value NOT IN ('DIVIDENDS','SPLITS'))
          OR (SELECT count(DISTINCT value) FROM json_each(NEW.request_json,'$.operations'))
                !=json_array_length(NEW.request_json,'$.operations')
          OR (json_type(NEW.request_json,'$.force') IS NOT 'true'
                AND json_type(NEW.request_json,'$.force') IS NOT 'false')
          OR json_type(NEW.request_json,'$.window') IS NOT 'object'
          OR json_type(NEW.request_json,'$.window.start') IS NOT 'text'
          OR json_type(NEW.request_json,'$.window.end') IS NOT 'text'
          OR length(json_extract(NEW.request_json,'$.window.start'))!=10
          OR length(json_extract(NEW.request_json,'$.window.end'))!=10
          OR substr(json_extract(NEW.request_json,'$.window.start'),1,4)='0000'
          OR substr(json_extract(NEW.request_json,'$.window.end'),1,4)='0000'
          OR date(json_extract(NEW.request_json,'$.window.start'),'+0 days')
                IS NOT json_extract(NEW.request_json,'$.window.start')
          OR date(json_extract(NEW.request_json,'$.window.end'),'+0 days')
                IS NOT json_extract(NEW.request_json,'$.window.end')
          OR json_extract(NEW.request_json,'$.window.end')
                <json_extract(NEW.request_json,'$.window.start')
          OR EXISTS(SELECT 1 FROM json_each(NEW.request_json,'$.window')
                WHERE key NOT IN ('start','end'))
          OR EXISTS(SELECT 1 FROM json_each(NEW.request_json) WHERE key NOT IN
                ('schema_version','scope','security_ids','providers','window','operations','force'))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_JOB'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_job_update BEFORE UPDATE ON jobs
        WHEN OLD.runtime_version!=1 OR {_controls("NEW")}
          OR OLD.status IN ({_TERMINAL})
          OR NEW.id IS NOT OLD.id OR NEW.owner_id IS NOT OLD.owner_id OR NEW.kind IS NOT OLD.kind
          OR NEW.dedupe_key IS NOT OLD.dedupe_key OR NEW.request_json IS NOT OLD.request_json
          OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.created_at IS NOT OLD.created_at
          OR NEW.progress_total IS NOT OLD.progress_total
          OR NEW.max_attempts IS NOT OLD.max_attempts
          OR NEW.version!=OLD.version+1 OR NEW.updated_at<OLD.updated_at
          OR NEW.progress_done<OLD.progress_done OR NEW.attempt_count<OLD.attempt_count
          OR NEW.lease_generation NOT BETWEEN OLD.lease_generation AND OLD.lease_generation+1
          OR (NEW.lease_generation>OLD.lease_generation AND (
                NEW.status!='LEASED' OR (OLD.status IN ('LEASED','RUNNING')
                    AND OLD.lease_expires_at>NEW.updated_at)))
          OR (NEW.lease_owner IS NOT NULL AND NEW.lease_owner IS NOT OLD.lease_owner
                AND NEW.lease_generation!=OLD.lease_generation+1)
          OR (OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at)
          OR (NEW.status='SUCCEEDED' AND (
                NOT EXISTS(SELECT 1 FROM job_items WHERE job_id=NEW.id)
                OR EXISTS(SELECT 1 FROM job_items WHERE job_id=NEW.id AND status!='SUCCEEDED')))
          OR NOT ((OLD.status='QUEUED' AND NEW.status IN ('LEASED','CANCELLED'))
              OR (OLD.status='LEASED'
                    AND NEW.status IN ('LEASED','RUNNING','CANCEL_REQUESTED','CANCELLED'))
              OR (OLD.status='RUNNING' AND NEW.status IN ('RUNNING','LEASED','RETRYING',
                    'SUCCEEDED','PARTIAL','FAILED','CANCEL_REQUESTED','CANCELLED'))
              OR (OLD.status='RETRYING' AND NEW.status IN ('LEASED','CANCELLED'))
              OR (OLD.status='CANCEL_REQUESTED' AND NEW.status='CANCELLED'))
        BEGIN SELECT RAISE(ABORT,'INVALID_JOB_TRANSITION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_job_no_delete BEFORE DELETE ON jobs
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_JOB_HISTORY'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_job_item_insert BEFORE INSERT ON job_items
        WHEN NEW.status!='PENDING' OR NEW.attempt_count!=0 OR NEW.version!=1
          OR NEW.lease_generation!=0
          OR NEW.next_run_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
          OR NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=NEW.job_id
                AND j.runtime_version=1 AND j.status='QUEUED'
                AND EXISTS(SELECT 1 FROM json_each(j.request_json,'$.security_ids')
                    WHERE value=NEW.security_id)
                AND EXISTS(SELECT 1 FROM json_each(j.request_json,'$.providers')
                    WHERE json_extract(value,'$.provider_id')=NEW.provider_id)
                AND EXISTS(SELECT 1 FROM json_each(j.request_json,'$.operations')
                    WHERE value=NEW.operation))
        BEGIN SELECT RAISE(ABORT,'INVALID_JOB_ITEM'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_job_item_update BEFORE UPDATE ON job_items
        WHEN OLD.status IN ({_ITEM_TERMINAL}) OR NEW.id IS NOT OLD.id
          OR NEW.job_id IS NOT OLD.job_id
          OR NEW.ordinal IS NOT OLD.ordinal OR NEW.security_id IS NOT OLD.security_id
          OR NEW.provider_id IS NOT OLD.provider_id OR NEW.operation IS NOT OLD.operation
          OR NEW.created_at IS NOT OLD.created_at OR NEW.version!=OLD.version+1
          OR NEW.updated_at<OLD.updated_at
          OR (NEW.status='RUNNING' AND (OLD.status NOT IN ('PENDING','RETRYING')
                OR NEW.attempt_count!=OLD.attempt_count+1))
          OR (NEW.status!='RUNNING' AND NEW.attempt_count!=OLD.attempt_count)
          OR (NEW.status IN ('RUNNING',{_ITEM_TERMINAL}) AND NEW.next_run_at IS NOT NULL)
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
    con.exec_driver_sql("""
        CREATE TRIGGER trg_job_item_no_delete BEFORE DELETE ON job_items
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_JOB_HISTORY'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_job_checkpoint_insert BEFORE INSERT ON job_checkpoints
        WHEN NEW.sequence!=(SELECT coalesce(max(sequence),0)+1 FROM job_checkpoints
                            WHERE item_id=NEW.item_id)
          OR NOT EXISTS(SELECT 1 FROM job_items i JOIN jobs j ON j.id=i.job_id
                JOIN provider_runs r ON r.id=NEW.provider_run_id
                WHERE i.id=NEW.item_id AND i.status='RUNNING' AND j.status='RUNNING'
                  AND j.runtime_version=1 AND j.lease_generation=NEW.lease_generation
                  AND i.lease_generation=NEW.lease_generation AND j.lease_expires_at>NEW.created_at
                  AND NEW.created_at>=j.updated_at AND r.job_id=j.id AND r.owner_id=j.owner_id
                  AND r.security_id=i.security_id AND r.provider_id=i.provider_id
                  AND r.operation=i.operation AND r.provenance_version=1 AND r.parse_complete=1
                  AND r.requested_cursor IS NEW.cursor_in
                  AND r.status IN ('OK_WITH_DATA','NO_DATA'))
        BEGIN SELECT RAISE(ABORT,'INVALID_JOB_CHECKPOINT'); END
    """)
    _immutable(con, "job_checkpoints")
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) VALUES (?,?,'schema',?,'MIGRATE_SOURCE_JOBS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; legacy jobs retained without invented execution state",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting job history")
