"""Preserve immutable provider contexts, completed fetches, observations and repeated sightings."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op
from sqlalchemy import Connection

revision = "0008_provider_evidence"
down_revision = "0007_actions_opening"
branch_labels = None
depends_on = None

_PROVIDERS = (
    "'eodhd','alphavantage','massive','yahoo','yfinance','hkex_evidence','sec_evidence','manual'"
)
_MANUAL = "'hkex_evidence','sec_evidence','manual'"
_ORIGIN = """CASE {value}
    WHEN 'eodhd' THEN 'EODHD' WHEN 'alphavantage' THEN 'ALPHAVANTAGE'
    WHEN 'massive' THEN 'MASSIVE' WHEN 'yahoo' THEN 'YAHOO' WHEN 'yfinance' THEN 'YAHOO'
    WHEN 'hkex_evidence' THEN 'HKEX_OFFICIAL' WHEN 'sec_evidence' THEN 'SEC_OFFICIAL'
    WHEN 'manual' THEN 'USER_VERIFIED' END"""
_BASE_FIELDS = ("provider_id", "security_id", "provider_symbol", "provider_event_id", "evidence")
_DIV_FIELDS = _BASE_FIELDS + (
    "ex_date",
    "announce_date",
    "record_date",
    "pay_date",
    "event_type",
    "dps",
    "currency",
    "source_currency",
    "source_period",
    "source_frequency",
    "adjustment_basis",
)
_SPLIT_FIELDS = _BASE_FIELDS + (
    "effective_date",
    "ratio_numerator",
    "ratio_denominator",
    "source_adjustment_type",
    "effect_scope",
)


def _immutable(con: Connection, table: str) -> None:
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_provider_{table}_{operation.lower()} BEFORE {operation} ON {table}
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_PROVIDER_EVIDENCE'); END
        """)


def _hash(value: str) -> str:
    return f"(length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"


def _alias_current(prefix: str) -> str:
    return f"""
        SELECT 1 FROM provider_symbols a JOIN securities s ON s.id=a.security_id
        JOIN exchanges e ON e.code=s.exchange_code
        WHERE a.id={prefix}.alias_id AND a.version={prefix}.alias_version
          AND a.provider_id={prefix}.provider_id AND a.security_id={prefix}.security_id
          AND a.provider_symbol=json_extract({prefix}.alias_json,'$.alias.provider_symbol')
          AND a.verified=json_extract({prefix}.alias_json,'$.alias.verified')
          AND a.effective_from=json_extract({prefix}.alias_json,'$.alias.effective_from')
          AND a.effective_to IS json_extract({prefix}.alias_json,'$.alias.effective_to')
          AND {prefix}.window_start>=a.effective_from
          AND (a.effective_to IS NULL OR {prefix}.window_end<=a.effective_to)
          AND e.market=json_extract({prefix}.alias_json,'$.market')
          AND e.timezone=json_extract({prefix}.alias_json,'$.exchange_timezone')
          AND (a.provider_id NOT IN ('alphavantage','massive','sec_evidence') OR e.market='US')
          AND (a.provider_id!='hkex_evidence' OR e.market='HK')
          AND NOT EXISTS(SELECT 1 FROM provider_symbols b
                WHERE b.id!=a.id AND b.provider_id=a.provider_id
                  AND (b.security_id=a.security_id OR b.provider_symbol=a.provider_symbol)
                  AND b.effective_from<={prefix}.window_end
                  AND (b.effective_to IS NULL OR b.effective_to>={prefix}.window_start))
    """


def upgrade() -> None:
    con = op.get_bind()
    if con.exec_driver_sql("""
        SELECT 1 FROM dividend_observations
        WHERE dps_e8 IS NOT NULL AND (typeof(dps_e8)!='integer' OR dps_e8<0) LIMIT 1
    """).first():
        raise RuntimeError("Invalid retained provider amounts require explicit review")
    con.exec_driver_sql(f"""
        CREATE TABLE provider_fetch_contexts (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
            provider_id TEXT NOT NULL CHECK(provider_id IN ({_PROVIDERS})),
            origin_group TEXT NOT NULL,
            alias_id TEXT NOT NULL REFERENCES provider_symbols(id) ON DELETE RESTRICT,
            alias_version INTEGER NOT NULL
                CHECK(typeof(alias_version)='integer' AND alias_version>=1),
            operation TEXT NOT NULL CHECK(operation IN ('DIVIDENDS','SPLITS')),
            window_start TEXT NOT NULL CHECK(length(window_start)=10
                AND substr(window_start,1,4)!='0000'
                AND date(window_start,'+0 days') IS window_start),
            window_end TEXT NOT NULL CHECK(length(window_end)=10
                AND substr(window_end,1,4)!='0000'
                AND date(window_end,'+0 days') IS window_end AND window_end>=window_start),
            alias_json TEXT NOT NULL
                CHECK(json_valid(alias_json) AND json_type(alias_json)='object'),
            profile_json TEXT NOT NULL
                CHECK(json_valid(profile_json) AND json_type(profile_json)='object'),
            profile_sha256 TEXT NOT NULL CHECK({_hash("profile_sha256")}),
            context_sha256 TEXT NOT NULL CHECK({_hash("context_sha256")}),
            parser_version TEXT NOT NULL CHECK(length(parser_version) BETWEEN 1 AND 100),
            normalization_version INTEGER NOT NULL
                CHECK(typeof(normalization_version)='integer' AND normalization_version=1),
            created_at TEXT NOT NULL
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_provider_context_owner
        ON provider_fetch_contexts(owner_id,security_id,created_at)
    """)
    for name, definition in (
        ("provenance_version", "INTEGER NOT NULL DEFAULT 0 CHECK(provenance_version IN (0,1))"),
        ("owner_id", "TEXT REFERENCES owners(id) ON DELETE RESTRICT"),
        ("context_id", "TEXT REFERENCES provider_fetch_contexts(id) ON DELETE RESTRICT"),
        ("operation", "TEXT CHECK(operation IN ('DIVIDENDS','SPLITS'))"),
        ("requested_start", "TEXT"),
        ("requested_end", "TEXT"),
        ("response_sha256", "TEXT"),
        ("raw_sha256", "TEXT"),
        ("source_url", "TEXT"),
        ("parse_complete", "INTEGER NOT NULL DEFAULT 0 CHECK(parse_complete IN (0,1))"),
        ("issue_count", "INTEGER NOT NULL DEFAULT 0 CHECK(issue_count>=0)"),
        ("filtered_count", "INTEGER NOT NULL DEFAULT 0 CHECK(filtered_count>=0)"),
        (
            "warnings_json",
            "TEXT NOT NULL DEFAULT '[]' "
            "CHECK(json_valid(warnings_json) AND json_type(warnings_json)='array')",
        ),
        ("next_cursor", "TEXT"),
        ("retry_at", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("attempted", "INTEGER NOT NULL DEFAULT 0 CHECK(attempted IN (0,1))"),
    ):
        con.exec_driver_sql(f"ALTER TABLE provider_runs ADD COLUMN {name} {definition}")
    con.exec_driver_sql("""
        ALTER TABLE dividend_observations ADD COLUMN provenance_version
        INTEGER NOT NULL DEFAULT 0 CHECK(provenance_version IN (0,1))
    """)
    con.exec_driver_sql("""
        ALTER TABLE dividend_observations ADD COLUMN normalized_json TEXT
        CHECK(normalized_json IS NULL OR (
            json_valid(normalized_json) AND json_type(normalized_json)='object'))
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE split_observations (
            id TEXT PRIMARY KEY,
            provider_run_id TEXT NOT NULL REFERENCES provider_runs(id) ON DELETE RESTRICT,
            provider_id TEXT NOT NULL CHECK(provider_id IN ({_PROVIDERS})),
            origin_group TEXT NOT NULL,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
            provider_symbol TEXT NOT NULL,
            provider_event_id TEXT,
            normalized_sha256 TEXT NOT NULL CHECK({_hash("normalized_sha256")}),
            raw_sha256 TEXT CHECK(raw_sha256 IS NULL OR {_hash("raw_sha256")}),
            source_url TEXT,
            effective_date TEXT NOT NULL CHECK(length(effective_date)=10
                AND substr(effective_date,1,4)!='0000'
                AND date(effective_date,'+0 days') IS effective_date),
            ratio_num INTEGER NOT NULL CHECK(typeof(ratio_num)='integer' AND ratio_num>0),
            ratio_den INTEGER NOT NULL CHECK(typeof(ratio_den)='integer' AND ratio_den>0),
            effect_scope TEXT NOT NULL CHECK(effect_scope='PROVIDER_ADJUSTMENT_ONLY'),
            normalized_json TEXT NOT NULL
                CHECK(json_valid(normalized_json) AND json_type(normalized_json)='object'),
            retrieved_at TEXT NOT NULL,
            UNIQUE(provider_id,security_id,normalized_sha256)
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_split_observations_security
        ON split_observations(security_id,effective_date,provider_id)
    """)
    con.exec_driver_sql("""
        CREATE TABLE provider_observation_sightings (
            run_id TEXT NOT NULL REFERENCES provider_runs(id) ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL
                CHECK(typeof(ordinal)='integer' AND ordinal BETWEEN 0 AND 9999),
            dividend_observation_id TEXT REFERENCES dividend_observations(id) ON DELETE RESTRICT,
            split_observation_id TEXT REFERENCES split_observations(id) ON DELETE RESTRICT,
            CHECK((dividend_observation_id IS NULL)!=(split_observation_id IS NULL)),
            PRIMARY KEY(run_id,ordinal),
            UNIQUE(run_id,dividend_observation_id),
            UNIQUE(run_id,split_observation_id)
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_provider_context_insert BEFORE INSERT ON provider_fetch_contexts
        WHEN NEW.origin_group IS NOT ({_ORIGIN.format(value="NEW.provider_id")})
          OR json_extract(NEW.alias_json,'$.alias.id') IS NOT NEW.alias_id
          OR json_extract(NEW.alias_json,'$.alias.version') IS NOT NEW.alias_version
          OR json_extract(NEW.alias_json,'$.alias.provider_id') IS NOT NEW.provider_id
          OR json_extract(NEW.alias_json,'$.alias.security_id') IS NOT NEW.security_id
          OR json_extract(NEW.alias_json,'$.window.start') IS NOT NEW.window_start
          OR json_extract(NEW.alias_json,'$.window.end') IS NOT NEW.window_end
          OR json_extract(NEW.profile_json,'$.provider_id') IS NOT NEW.provider_id
          OR json_type(NEW.profile_json,'$.version') IS NOT 'integer'
          OR json_extract(NEW.profile_json,'$.version')<1
          OR json_type(NEW.profile_json,'$.enabled') IS NOT 'true'
          OR json_type(NEW.profile_json,'$.terms_acknowledged') IS NOT 'true'
          OR json_type(NEW.profile_json,'$.raw_payload_storage') IS NOT 'false'
          OR length(trim(coalesce(json_extract(NEW.profile_json,'$.terms_reference'),'')))=0
          OR length(trim(coalesce(json_extract(NEW.profile_json,'$.terms_reason'),'')))<10
          OR EXISTS(SELECT 1 FROM json_each(NEW.profile_json) WHERE key NOT IN (
                'provider_id','version','enabled','terms_acknowledged','terms_reference',
                'terms_reason','requests_per_minute','requests_per_day','credential_source',
                'credential_version','raw_payload_storage'))
          OR (NEW.provider_id NOT IN ({_MANUAL}) AND (
                json_type(NEW.profile_json,'$.requests_per_minute') IS NOT 'integer'
                OR json_type(NEW.profile_json,'$.requests_per_day') IS NOT 'integer'
                OR json_extract(NEW.profile_json,'$.requests_per_minute')<1
                OR json_extract(NEW.profile_json,'$.requests_per_day')<1))
          OR (NEW.provider_id IN ({_MANUAL}) AND (
                json_extract(NEW.profile_json,'$.requests_per_minute') IS NOT NULL
                OR json_extract(NEW.profile_json,'$.requests_per_day') IS NOT NULL))
          OR NOT EXISTS({_alias_current("NEW")})
        BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_CONTEXT'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_provider_run_insert BEFORE INSERT ON provider_runs
        WHEN NEW.provenance_version!=1 OR NEW.owner_id IS NULL
          OR NEW.provider_id NOT IN ({_PROVIDERS})
          OR NEW.origin_group IS NOT ({_ORIGIN.format(value="NEW.provider_id")})
          OR NEW.operation IS NULL OR NEW.finished_at IS NULL OR NEW.duration_ms IS NULL
          OR typeof(NEW.duration_ms)!='integer' OR NEW.duration_ms<0
          OR typeof(NEW.observations_count)!='integer' OR typeof(NEW.issue_count)!='integer'
          OR typeof(NEW.filtered_count)!='integer' OR typeof(NEW.parse_complete)!='integer'
          OR typeof(NEW.attempted)!='integer'
          OR NEW.requested_start IS NULL OR length(NEW.requested_start)!=10
          OR substr(NEW.requested_start,1,4)='0000'
          OR date(NEW.requested_start,'+0 days') IS NOT NEW.requested_start
          OR NEW.requested_end IS NULL OR length(NEW.requested_end)!=10
          OR substr(NEW.requested_end,1,4)='0000'
          OR date(NEW.requested_end,'+0 days') IS NOT NEW.requested_end
          OR NEW.requested_end<NEW.requested_start
          OR NEW.coverage_start IS NOT NULL OR NEW.coverage_end IS NOT NULL
          OR NEW.response_sha256 IS NULL OR length(NEW.response_sha256)!=64
          OR NEW.response_sha256 GLOB '*[^0-9a-f]*'
          OR (NEW.raw_sha256 IS NOT NULL AND (
                length(NEW.raw_sha256)!=64 OR NEW.raw_sha256 GLOB '*[^0-9a-f]*'))
          OR NEW.status='EMPTY_CONFIRMED'
          OR (NEW.parse_complete=1 AND (
                NEW.issue_count!=0 OR NEW.status NOT IN ('OK_WITH_DATA','NO_DATA')))
          OR ((NEW.observations_count>0)!=(NEW.status='OK_WITH_DATA'))
          OR (NEW.http_status IS NOT NULL AND (
                typeof(NEW.http_status)!='integer' OR NEW.http_status NOT BETWEEN 100 AND 599))
          OR (NEW.sanitized_error IS NOT NULL AND (
                length(NEW.sanitized_error)>100 OR NEW.sanitized_error GLOB '*[^A-Z0-9_]*'))
          OR (NEW.source_url IS NOT NULL AND (
                instr(NEW.source_url,'?') OR instr(NEW.source_url,'#')
                OR instr(NEW.source_url,'@') OR length(NEW.source_url)>2048))
          OR (NEW.next_cursor IS NOT NULL AND (
                NEW.provider_id!='massive' OR length(NEW.next_cursor)>4096
                OR substr(NEW.next_cursor,1,1)!='/'))
          OR (NEW.attempted=0 AND (
                NEW.http_status IS NOT NULL OR NEW.source_url IS NOT NULL
                OR NEW.raw_sha256 IS NOT NULL))
          OR (NEW.provider_id IN ({_MANUAL}) AND NEW.attempted!=0)
          OR (NEW.provider_id NOT IN ({_MANUAL})
                AND NEW.status IN ('OK_WITH_DATA','NO_DATA') AND (
                    NEW.attempted!=1 OR NEW.source_url IS NULL OR NEW.raw_sha256 IS NULL
                    OR NEW.http_status IS NULL OR NEW.http_status NOT BETWEEN 200 AND 299))
          OR (NEW.job_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM jobs j WHERE j.id=NEW.job_id AND j.owner_id=NEW.owner_id))
          OR (NEW.context_id IS NULL AND (
                NEW.attempted!=0 OR NEW.observations_count!=0
                OR NEW.status NOT IN ('UNSUPPORTED','UNCONFIGURED','PARSE_ERROR')))
          OR (NEW.context_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM provider_fetch_contexts c
                WHERE c.id=NEW.context_id AND c.owner_id=NEW.owner_id
                  AND c.security_id=NEW.security_id AND c.provider_id=NEW.provider_id
                  AND c.origin_group=NEW.origin_group AND c.operation=NEW.operation
                  AND c.window_start=NEW.requested_start AND c.window_end=NEW.requested_end
                  AND (NEW.source_url IS NULL OR NEW.source_url IS CASE NEW.provider_id
                        WHEN 'eodhd' THEN 'https://eodhd.com/api/' ||
                            CASE NEW.operation WHEN 'DIVIDENDS' THEN 'div/' ELSE 'splits/' END
                            || json_extract(c.alias_json,'$.alias.provider_symbol')
                        WHEN 'alphavantage' THEN 'https://www.alphavantage.co/query'
                        WHEN 'massive' THEN 'https://api.massive.com/stocks/v1/'
                            || lower(NEW.operation)
                        WHEN 'yahoo' THEN 'https://query1.finance.yahoo.com/v8/finance/chart/'
                            || json_extract(c.alias_json,'$.alias.provider_symbol')
                        WHEN 'yfinance' THEN 'https://query1.finance.yahoo.com/v8/finance/chart/'
                            || json_extract(c.alias_json,'$.alias.provider_symbol') END)
                  AND EXISTS({_alias_current("c")})))
        BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_RUN'); END
    """)
    for table, operation, fields in (
        ("dividend_observations", "DIVIDENDS", _DIV_FIELDS),
        ("split_observations", "SPLITS", _SPLIT_FIELDS),
    ):
        additional = (
            """
          OR NEW.provenance_version!=1 OR NEW.raw_permitted!=0
          OR NEW.event_type NOT IN ('REGULAR','SPECIAL','INTERIM','FINAL','SUPPLEMENTAL',
                'ETF_DISTRIBUTION','REIT_DISTRIBUTION','UNKNOWN')
          OR (NEW.dps_e8 IS NOT NULL AND (typeof(NEW.dps_e8)!='integer' OR NEW.dps_e8<0))
          OR (NEW.dps_e8 IS NULL AND json_type(NEW.normalized_json,'$.dps') IS NOT 'null')
          OR (NEW.dps_e8 IS NOT NULL AND (
                json_type(NEW.normalized_json,'$.dps') IS NOT 'text'
                OR json_extract(NEW.normalized_json,'$.dps') IS NOT
                    printf('%d.%08d',NEW.dps_e8/100000000,NEW.dps_e8%100000000)))
          OR json_extract(NEW.normalized_json,'$.ex_date') IS NOT NEW.ex_date
          OR json_extract(NEW.normalized_json,'$.announce_date') IS NOT NEW.announce_date
          OR json_extract(NEW.normalized_json,'$.record_date') IS NOT NEW.record_date
          OR json_extract(NEW.normalized_json,'$.pay_date') IS NOT NEW.pay_date
          OR json_extract(NEW.normalized_json,'$.event_type') IS NOT NEW.event_type
          OR json_extract(NEW.normalized_json,'$.currency') IS NOT NEW.currency
          OR json_extract(NEW.normalized_json,'$.adjustment_basis') IS NOT NEW.adjustment_basis
        """
            if operation == "DIVIDENDS"
            else """
          OR json_type(NEW.normalized_json,'$.ratio_numerator') IS NOT 'text'
          OR json_type(NEW.normalized_json,'$.ratio_denominator') IS NOT 'text'
          OR json_extract(NEW.normalized_json,'$.ratio_numerator') IS NOT
                CAST(NEW.ratio_num AS TEXT)
          OR json_extract(NEW.normalized_json,'$.ratio_denominator') IS NOT
                CAST(NEW.ratio_den AS TEXT)
          OR json_extract(NEW.normalized_json,'$.effective_date') IS NOT NEW.effective_date
          OR json_extract(NEW.normalized_json,'$.effect_scope') IS NOT NEW.effect_scope
        """
        )
        allowed = ",".join(f"'{name}'" for name in fields)
        date_column = "ex_date" if operation == "DIVIDENDS" else "effective_date"
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_provider_{table}_insert BEFORE INSERT ON {table}
            WHEN NEW.normalized_json IS NULL OR NOT json_valid(NEW.normalized_json)
              OR length(NEW.normalized_json)>16384
              OR NEW.origin_group IS NOT ({_ORIGIN.format(value="NEW.provider_id")})
              OR NOT {_hash("NEW.normalized_sha256")}
              OR json_extract(NEW.normalized_json,'$.provider_id') IS NOT NEW.provider_id
              OR json_extract(NEW.normalized_json,'$.security_id') IS NOT NEW.security_id
              OR json_extract(NEW.normalized_json,'$.provider_symbol') IS NOT NEW.provider_symbol
              OR json_extract(NEW.normalized_json,'$.provider_event_id')
                    IS NOT NEW.provider_event_id
              OR EXISTS(SELECT 1 FROM json_each(NEW.normalized_json) WHERE key NOT IN ({allowed}))
              OR (NEW.provider_id IN ({_MANUAL})
                    AND json_type(NEW.normalized_json,'$.evidence') IS NOT 'object')
              OR NOT EXISTS(
                    SELECT 1 FROM provider_runs r
                    JOIN provider_fetch_contexts c ON c.id=r.context_id
                    WHERE r.id=NEW.provider_run_id AND r.provenance_version=1
                      AND r.status='OK_WITH_DATA' AND r.operation='{operation}'
                      AND r.security_id=NEW.security_id AND r.provider_id=NEW.provider_id
                      AND r.origin_group=NEW.origin_group
                      AND NEW.provider_symbol=json_extract(c.alias_json,'$.alias.provider_symbol')
                      AND NEW.raw_sha256 IS r.raw_sha256 AND NEW.source_url IS r.source_url
                      AND NEW.retrieved_at=r.finished_at
                      AND (NEW.{date_column} IS NULL
                           OR NEW.{date_column} BETWEEN r.requested_start AND r.requested_end))
              {additional}
            BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_OBSERVATION'); END
        """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_provider_sighting_insert BEFORE INSERT ON provider_observation_sightings
        WHEN NOT EXISTS(
            SELECT 1 FROM provider_runs r JOIN provider_fetch_contexts c ON c.id=r.context_id
            WHERE r.id=NEW.run_id AND r.status='OK_WITH_DATA'
              AND NEW.ordinal<r.observations_count AND (
                (NEW.dividend_observation_id IS NOT NULL AND r.operation='DIVIDENDS' AND EXISTS(
                    SELECT 1 FROM dividend_observations o WHERE o.id=NEW.dividend_observation_id
                    AND o.provenance_version=1 AND o.security_id=r.security_id
                    AND o.provider_id=r.provider_id AND o.origin_group=r.origin_group
                    AND o.provider_symbol=json_extract(c.alias_json,'$.alias.provider_symbol')
                    AND (o.ex_date IS NULL
                         OR o.ex_date BETWEEN r.requested_start AND r.requested_end)))
                OR (NEW.split_observation_id IS NOT NULL AND r.operation='SPLITS' AND EXISTS(
                    SELECT 1 FROM split_observations o WHERE o.id=NEW.split_observation_id
                    AND o.security_id=r.security_id AND o.provider_id=r.provider_id
                    AND o.origin_group=r.origin_group
                    AND o.provider_symbol=json_extract(c.alias_json,'$.alias.provider_symbol')
                    AND o.effective_date BETWEEN r.requested_start AND r.requested_end))))
        BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_SIGHTING'); END
    """)
    for table in (
        "provider_fetch_contexts",
        "provider_runs",
        "split_observations",
        "provider_observation_sightings",
    ):
        _immutable(con, table)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_PROVIDER_EVIDENCE',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; legacy evidence preserved without invented provenance",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting provider evidence history"
    )
