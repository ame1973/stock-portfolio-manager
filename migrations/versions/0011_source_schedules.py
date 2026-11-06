"""Add disabled owned source schedules and atomic, immutable occurrence receipts."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0011_source_schedules"
down_revision = "0010_upstream_budgets"
branch_labels = None
depends_on = None

_ACTIVE = "'QUEUED','LEASED','RUNNING','RETRYING','CANCEL_REQUESTED'"
_POLICY_KEYS = (
    "'schema_version','scope','calendar_basis','security_ids','provider_ids','operations',"
    "'market','first_date','local_run_time','cadence','weekday','lookback_days',"
    "'lookback_months','backfill_from_date'"
)


def _date(value: str) -> str:
    return f"""(typeof({value})='text' AND length({value})=10
        AND substr({value},1,4)!='0000' AND date({value},'+0 days') IS {value})"""


def _utc(value: str) -> str:
    return f"""(typeof({value})='text' AND length({value})=27
        AND {_date(f"substr({value},1,10)")}
        AND substr({value},11,1)='T' AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _uuid(value: str) -> str:
    return f"""(typeof({value})='text' AND length({value})=36
        AND substr({value},9,1)='-' AND substr({value},14,1)='-'
        AND substr({value},19,1)='-' AND substr({value},24,1)='-'
        AND length(replace({value},'-',''))=32
        AND replace({value},'-','') NOT GLOB '*[^0-9a-f]*')"""


def _hash(value: str) -> str:
    return f"(typeof({value})='text' AND length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"


def _policy_invalid(value: str) -> str:
    fields = {
        key: f"json_extract({value},'$.{key}')"
        for key in (
            "first_date",
            "backfill_from_date",
            "local_run_time",
            "weekday",
            "lookback_days",
            "lookback_months",
        )
    }
    run, first, backfill = (
        fields[key]
        for key in (
            "local_run_time",
            "first_date",
            "backfill_from_date",
        )
    )
    sets = []
    for key, maximum, members in (
        ("security_ids", 100, None),
        ("provider_ids", 5, "'eodhd','alphavantage','massive','yahoo','yfinance'"),
        ("operations", 2, "'DIVIDENDS','SPLITS'"),
    ):
        invalid_member = f"value NOT IN ({members})" if members else f"NOT {_uuid('value')}"
        sets.append(f"""
            OR json_type({value},'$.{key}') IS NOT 'array'
            OR json_array_length({value},'$.{key}') NOT BETWEEN 1 AND {maximum}
            OR EXISTS(SELECT 1 FROM json_each({value},'$.{key}')
                WHERE type!='text' OR {invalid_member})
            OR (SELECT count(DISTINCT value) FROM json_each({value},'$.{key}'))
                !=json_array_length({value},'$.{key}')
        """)
    return f"""
        json_type({value}) IS NOT 'object'
        OR (SELECT count(*) FROM json_each({value}))!=14
        OR (SELECT count(DISTINCT key) FROM json_each({value}))!=14
        OR EXISTS(SELECT 1 FROM json_each({value}) WHERE key NOT IN ({_POLICY_KEYS}))
        OR json_type({value},'$.schema_version') IS NOT 'integer'
        OR json_extract({value},'$.schema_version') IS NOT 1
        OR json_extract({value},'$.scope') IS NOT 'SOURCE_ONLY'
        OR json_extract({value},'$.calendar_basis') IS NOT 'LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR'
        OR json_extract({value},'$.market') NOT IN ('HK','US')
        OR json_type({value},'$.market') IS NOT 'text'
        OR NOT {_date(first)}
        OR NOT (json_type({value},'$.backfill_from_date') IS 'null'
            OR ({_date(backfill)} AND {backfill}<={first}))
        OR json_type({value},'$.local_run_time') IS NOT 'text'
        OR length({run})!=5 OR substr({run},3,1)!=':'
        OR substr({run},1,2) NOT BETWEEN '16' AND '23'
        OR substr({run},4,2) NOT BETWEEN '00' AND '59'
        OR replace({run},':','') GLOB '*[^0-9]*'
        OR json_type({value},'$.cadence') IS NOT 'text'
        OR json_extract({value},'$.cadence') NOT IN ('DAILY','WEEKDAYS','WEEKLY')
        OR NOT ((json_extract({value},'$.cadence')='WEEKLY'
                AND json_type({value},'$.weekday') IS 'integer'
                AND {fields["weekday"]} BETWEEN 0 AND 6)
            OR (json_extract({value},'$.cadence')!='WEEKLY'
                AND json_type({value},'$.weekday') IS 'null'))
        OR NOT ((json_type({value},'$.lookback_days') IS 'integer'
                AND {fields["lookback_days"]} BETWEEN 1 AND 3660
                AND json_type({value},'$.lookback_months') IS 'null')
            OR (json_type({value},'$.lookback_months') IS 'integer'
                AND {fields["lookback_months"]} BETWEEN 1 AND 120
                AND json_type({value},'$.lookback_days') IS 'null'))
        {"".join(sets)}
    """


def _catalogue_invalid(value: str) -> str:
    return f"""EXISTS(SELECT 1 FROM json_each({value},'$.security_ids') p
        WHERE NOT EXISTS(SELECT 1 FROM securities s JOIN exchanges e ON e.code=s.exchange_code
            WHERE s.id=p.value AND e.market=json_extract({value},'$.market')
              AND e.timezone=CASE e.market WHEN 'HK' THEN 'Asia/Hong_Kong'
                WHEN 'US' THEN 'America/New_York' END))"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE source_schedules (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
            name TEXT NOT NULL CHECK(typeof(name)='text' AND length(name) BETWEEN 1 AND 100
                AND name=trim(name) AND instr(name,char(0))=0
                AND name NOT GLOB ('*['||char(1)||'-'||char(31)||char(127)||']*')),
            policy_json TEXT NOT NULL CHECK(typeof(policy_json)='text'
                AND length(policy_json) BETWEEN 2 AND 16384 AND json_valid(policy_json)),
            policy_sha256 TEXT NOT NULL CHECK({_hash("policy_sha256")}),
            enabled INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(enabled)='integer' AND enabled IN (0,1)),
            version INTEGER NOT NULL DEFAULT 1
                CHECK(typeof(version)='integer' AND version BETWEEN 1 AND 2147483647),
            next_run_at TEXT CHECK(next_run_at IS NULL OR {_utc("next_run_at")}),
            last_sequence INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(last_sequence)='integer' AND last_sequence BETWEEN 0 AND 2147483647),
            last_due_at TEXT CHECK(last_due_at IS NULL OR {_utc("last_due_at")}),
            last_materialized_at TEXT CHECK(last_materialized_at IS NULL
                OR {_utc("last_materialized_at")}),
            last_job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
            initial_job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            updated_at TEXT NOT NULL CHECK({_utc("updated_at")} AND updated_at>=created_at),
            UNIQUE(id,owner_id), UNIQUE(owner_id,name),
            CHECK(enabled=1 OR next_run_at IS NULL),
            CHECK((last_sequence=0 AND last_due_at IS NULL AND last_materialized_at IS NULL
                    AND last_job_id IS NULL AND initial_job_id IS NULL)
                OR (last_sequence>0 AND last_due_at IS NOT NULL AND last_materialized_at IS NOT NULL
                    AND last_job_id IS NOT NULL AND last_due_at<=last_materialized_at
                    AND last_materialized_at<=updated_at))
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE source_schedule_occurrences (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            schedule_id TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL
                CHECK(typeof(sequence)='integer' AND sequence BETWEEN 1 AND 2147483647),
            schedule_version INTEGER NOT NULL CHECK(typeof(schedule_version)='integer'
                AND schedule_version BETWEEN 1 AND 2147483647),
            expected_run_at TEXT NOT NULL CHECK({_utc("expected_run_at")}),
            due_at TEXT NOT NULL CHECK({_utc("due_at")} AND due_at>=expected_run_at),
            materialized_at TEXT NOT NULL
                CHECK({_utc("materialized_at")} AND materialized_at>=due_at),
            next_run_at TEXT CHECK(next_run_at IS NULL
                OR ({_utc("next_run_at")} AND next_run_at>materialized_at)),
            policy_json TEXT NOT NULL CHECK(typeof(policy_json)='text'
                AND length(policy_json) BETWEEN 2 AND 16384 AND json_valid(policy_json)),
            policy_sha256 TEXT NOT NULL CHECK({_hash("policy_sha256")}),
            window_start TEXT NOT NULL CHECK({_date("window_start")}),
            window_end TEXT NOT NULL CHECK({_date("window_end")} AND window_start<=window_end),
            kind TEXT NOT NULL CHECK(kind IN ('REGULAR','INITIAL_BACKFILL')),
            outcome TEXT NOT NULL CHECK(outcome IN ('QUEUED','SKIPPED_IN_FLIGHT')),
            job_id TEXT UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
            blocking_job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
            FOREIGN KEY(schedule_id,owner_id)
                REFERENCES source_schedules(id,owner_id) ON DELETE RESTRICT,
            UNIQUE(schedule_id,sequence), UNIQUE(schedule_id,schedule_version),
            UNIQUE(schedule_id,due_at),
            CHECK((outcome='QUEUED' AND job_id IS NOT NULL AND blocking_job_id IS NULL)
                OR (outcome='SKIPPED_IN_FLIGHT' AND job_id IS NULL AND blocking_job_id IS NOT NULL))
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_source_schedule_due ON source_schedules(enabled,next_run_at,id)
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_source_schedule_owner ON source_schedules(owner_id,name,id)
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_source_occurrence_owner ON source_schedule_occurrences(owner_id,due_at,id)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_source_schedule_insert BEFORE INSERT ON source_schedules
        WHEN {_policy_invalid("NEW.policy_json")} OR {_catalogue_invalid("NEW.policy_json")}
          OR NEW.enabled!=0 OR NEW.version!=1 OR NEW.last_sequence!=0
          OR NEW.next_run_at IS NOT NULL OR NEW.last_due_at IS NOT NULL
          OR NEW.last_materialized_at IS NOT NULL OR NEW.last_job_id IS NOT NULL
          OR NEW.initial_job_id IS NOT NULL OR NEW.updated_at IS NOT NEW.created_at
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
        BEGIN SELECT RAISE(ABORT,'INVALID_SOURCE_SCHEDULE'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_source_schedule_update BEFORE UPDATE ON source_schedules
        WHEN NEW.id IS NOT OLD.id OR NEW.owner_id IS NOT OLD.owner_id
          OR NEW.created_at IS NOT OLD.created_at OR NEW.updated_at<OLD.updated_at
          OR NEW.version!=OLD.version+1 OR {_policy_invalid("NEW.policy_json")}
          OR (NEW.policy_json IS OLD.policy_json AND NEW.policy_sha256 IS NOT OLD.policy_sha256)
          OR ((NEW.enabled=1 OR NEW.policy_json IS NOT OLD.policy_json)
                AND {_catalogue_invalid("NEW.policy_json")})
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
          OR (OLD.initial_job_id IS NOT NULL AND
                json_extract(NEW.policy_json,'$.backfill_from_date')
                    IS NOT json_extract(OLD.policy_json,'$.backfill_from_date'))
          OR NOT ((NEW.last_sequence=OLD.last_sequence
                AND NEW.last_due_at IS OLD.last_due_at
                AND NEW.last_materialized_at IS OLD.last_materialized_at
                AND NEW.last_job_id IS OLD.last_job_id AND NEW.initial_job_id IS OLD.initial_job_id
                AND (NEW.enabled=0 OR (NEW.next_run_at IS NOT NULL
                    AND NEW.next_run_at>=NEW.updated_at
                    AND (NEW.last_due_at IS NULL OR NEW.next_run_at>NEW.last_due_at))))
            OR (NEW.last_sequence=OLD.last_sequence+1 AND NEW.name IS OLD.name
                AND NEW.enabled IS OLD.enabled AND NEW.policy_json IS OLD.policy_json
                AND NEW.policy_sha256 IS OLD.policy_sha256 AND EXISTS(
                    SELECT 1 FROM source_schedule_occurrences o WHERE o.schedule_id=OLD.id
                      AND o.owner_id=OLD.owner_id AND o.sequence=NEW.last_sequence
                      AND o.schedule_version=OLD.version AND o.expected_run_at IS OLD.next_run_at
                      AND NEW.last_due_at=o.due_at AND NEW.last_materialized_at=o.materialized_at
                      AND NEW.updated_at=o.materialized_at AND NEW.next_run_at IS o.next_run_at
                      AND NEW.last_job_id IS CASE WHEN o.outcome='QUEUED' THEN o.job_id
                            ELSE OLD.last_job_id END
                      AND NEW.initial_job_id IS CASE WHEN o.outcome='QUEUED'
                            AND o.kind='INITIAL_BACKFILL' THEN o.job_id
                            ELSE OLD.initial_job_id END)))
        BEGIN SELECT RAISE(ABORT,'INVALID_SOURCE_SCHEDULE_UPDATE'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_source_schedule_no_delete BEFORE DELETE ON source_schedules
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_SOURCE_SCHEDULE_IDENTITY'); END
    """)
    # The service checks exact zoneinfo slots; SQLite checks civil dates and immutable job scope.
    month = (
        "date(NEW.window_end,'start of month','-'||"
        "json_extract(NEW.policy_json,'$.lookback_months')||' months')"
    )
    regular_start = f"""CASE WHEN json_type(NEW.policy_json,'$.lookback_days')='integer'
        THEN date(NEW.window_end,'-'||(json_extract(NEW.policy_json,'$.lookback_days')-1)||' days')
        ELSE date({month},'+'||(min(CAST(strftime('%d',NEW.window_end) AS INTEGER),
            CAST(strftime('%d',date({month},'+1 month','-1 day')) AS INTEGER))-1)||' days') END"""
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_source_occurrence_insert BEFORE INSERT ON source_schedule_occurrences
        WHEN {_policy_invalid("NEW.policy_json")} OR {_catalogue_invalid("NEW.policy_json")}
          OR NOT EXISTS(SELECT 1 FROM source_schedules s WHERE s.id=NEW.schedule_id
                AND s.owner_id=NEW.owner_id AND s.enabled=1 AND s.version=NEW.schedule_version
                AND s.last_sequence+1=NEW.sequence AND s.next_run_at=NEW.expected_run_at
                AND s.updated_at<=NEW.materialized_at AND NEW.policy_json=s.policy_json
                AND NEW.policy_sha256=s.policy_sha256
                AND (s.last_due_at IS NULL OR NEW.due_at>s.last_due_at)
                AND ((NEW.kind='INITIAL_BACKFILL')=(s.initial_job_id IS NULL
                    AND json_extract(NEW.policy_json,'$.backfill_from_date') IS NOT NULL)))
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
          OR NEW.window_end<json_extract(NEW.policy_json,'$.first_date')
          OR NEW.window_end NOT BETWEEN date(substr(NEW.due_at,1,10),'-1 day')
                AND substr(NEW.due_at,1,10)
          OR (json_extract(NEW.policy_json,'$.market')='HK'
                AND NEW.window_end IS NOT substr(NEW.due_at,1,10))
          OR (json_extract(NEW.policy_json,'$.cadence')='WEEKDAYS'
                AND strftime('%w',NEW.window_end) IN ('0','6'))
          OR (json_extract(NEW.policy_json,'$.cadence')='WEEKLY'
                AND CAST(strftime('%w',NEW.window_end) AS INTEGER)
                    !=(json_extract(NEW.policy_json,'$.weekday')+1)%7)
          OR NEW.window_start IS NOT CASE WHEN NEW.kind='INITIAL_BACKFILL'
                THEN json_extract(NEW.policy_json,'$.backfill_from_date') ELSE {regular_start} END
          OR (NEW.outcome='SKIPPED_IN_FLIGHT' AND NOT EXISTS(
                SELECT 1 FROM source_schedules s JOIN jobs j ON j.id=s.last_job_id
                WHERE s.id=NEW.schedule_id AND j.id=NEW.blocking_job_id
                  AND j.owner_id=NEW.owner_id AND j.runtime_version=1 AND j.status IN ({_ACTIVE})))
          OR (NEW.outcome='QUEUED' AND (EXISTS(
                SELECT 1 FROM source_schedule_occurrences o JOIN jobs j ON j.id=o.job_id
                WHERE o.schedule_id=NEW.schedule_id AND j.status IN ({_ACTIVE}))
            OR NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=NEW.job_id
                AND j.owner_id=NEW.owner_id AND j.runtime_version=1
                AND j.kind='DIVIDEND_SOURCE_SYNC'
                AND j.status='QUEUED' AND j.version=1 AND j.created_at=NEW.materialized_at
                AND j.updated_at=NEW.materialized_at
                AND json_extract(j.request_json,'$.scope') IS 'SOURCE_ONLY'
                AND json_type(j.request_json,'$.force') IS 'false'
                AND json_extract(j.request_json,'$.window.start') IS NEW.window_start
                AND json_extract(j.request_json,'$.window.end') IS NEW.window_end
                AND json_extract(j.request_json,'$.security_ids')
                    IS json_extract(NEW.policy_json,'$.security_ids')
                AND json_extract(j.request_json,'$.operations')
                    IS json_extract(NEW.policy_json,'$.operations')
                AND json_array_length(j.request_json,'$.providers')
                    =json_array_length(NEW.policy_json,'$.provider_ids')
                AND (SELECT count(*) FROM job_items WHERE job_id=j.id)
                    =j.progress_total*json_array_length(j.request_json,'$.providers')
                        *json_array_length(j.request_json,'$.operations')
                AND NOT EXISTS(SELECT 1 FROM json_each(j.request_json,'$.providers') p
                    WHERE json_extract(p.value,'$.provider_id') NOT IN
                        (SELECT value FROM json_each(NEW.policy_json,'$.provider_ids'))))))
        BEGIN SELECT RAISE(ABORT,'INVALID_SOURCE_SCHEDULE_OCCURRENCE'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_source_occurrence_advance AFTER INSERT ON source_schedule_occurrences
        BEGIN
            UPDATE source_schedules SET version=version+1,last_sequence=NEW.sequence,
                last_due_at=NEW.due_at,last_materialized_at=NEW.materialized_at,
                updated_at=NEW.materialized_at,next_run_at=NEW.next_run_at,
                last_job_id=CASE WHEN NEW.outcome='QUEUED' THEN NEW.job_id ELSE last_job_id END,
                initial_job_id=CASE WHEN NEW.outcome='QUEUED' AND NEW.kind='INITIAL_BACKFILL'
                    THEN NEW.job_id ELSE initial_job_id END
            WHERE id=NEW.schedule_id AND owner_id=NEW.owner_id;
        END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_source_occurrence_no_{operation.lower()}
            BEFORE {operation} ON source_schedule_occurrences
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_SOURCE_SCHEDULE_OCCURRENCE'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_SOURCE_SCHEDULES',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; disabled schedule tables without legacy adoption or jobs",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting schedule history"
    )
