"""Retain inert split-basis declarations with immutable source and current-action fences."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0014_dividend_basis_assertions"
down_revision = "0013_managed_source_contexts"
branch_labels = None
depends_on = None

_FIELDS = (
    "id",
    "url",
    "citation",
    "reason",
    "observation_id",
    "source_sha256",
    "security_id",
    "ex_date",
    "share_basis_date",
    "source_basis",
    "target_basis",
    "complete",
    "split_only",
    "splits",
)
_REFS = (
    "id",
    "root_id",
    "security_id",
    "version",
    "action_type",
    "effective_date",
    "ratio_numerator",
    "ratio_denominator",
    "status",
    "lifecycle_status",
)


def _names(values: tuple[str, ...]) -> str:
    return ",".join(f"'{name}'" for name in values)


def _uuid(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=36
        AND substr({value},9,1)='-' AND substr({value},14,1)='-'
        AND substr({value},19,1)='-' AND substr({value},24,1)='-'
        AND length(replace({value},'-',''))=32
        AND replace({value},'-','') NOT GLOB '*[^0-9a-f]*')"""


def _hash(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0
        AND length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"""


def _day(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=10
        AND substr({value},1,4)!='0000' AND date({value},'+0 days') IS {value})"""


def _utc(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=27
        AND {_day(f"substr({value},1,10)")}
        AND substr({value},11,1)='T' AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _text(value: str, minimum: int, maximum: int) -> str:
    return f"""(typeof({value})='text' AND length({value}) BETWEEN {minimum} AND {maximum}
        AND instr({value},char(0))=0
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' '))"""


def _window(day: str) -> str:
    return f"""((NEW.share_basis_date>=NEW.ex_date
            AND {day} BETWEEN NEW.ex_date AND NEW.share_basis_date)
        OR (NEW.share_basis_date<NEW.ex_date
            AND {day}>NEW.share_basis_date AND {day}<NEW.ex_date))"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_basis_assertions (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
            observation_id TEXT NOT NULL REFERENCES dividend_observations(id) ON DELETE RESTRICT
                CHECK({_uuid("observation_id")}),
            source_sha256 TEXT NOT NULL CHECK({_hash("source_sha256")}),
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT
                CHECK({_uuid("security_id")}),
            ex_date TEXT NOT NULL CHECK({_day("ex_date")}),
            share_basis_date TEXT NOT NULL CHECK({_day("share_basis_date")}),
            assertion_json TEXT NOT NULL CHECK(typeof(assertion_json)='text'
                AND length(CAST(assertion_json AS BLOB))<=131072 AND json_valid(assertion_json)),
            assertion_sha256 TEXT NOT NULL CHECK({_hash("assertion_sha256")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    mirrors = " OR ".join(
        f"json_type(NEW.assertion_json,'$.{name}') IS NOT 'text' "
        f"OR json_extract(NEW.assertion_json,'$.{name}') IS NOT NEW.{name}"
        for name in (
            "id",
            "observation_id",
            "source_sha256",
            "security_id",
            "ex_date",
            "share_basis_date",
        )
    )
    reference_mirrors = " OR ".join(
        f"json_extract(j.value,'$.{name}') IS NOT c.{column}"
        for name, column in (
            ("id", "id"),
            ("root_id", "root_id"),
            ("security_id", "security_id"),
            ("version", "version"),
            ("action_type", "action_type"),
            ("effective_date", "effective_date"),
            ("ratio_numerator", "ratio_num"),
            ("ratio_denominator", "ratio_den"),
        )
    )
    reference_types = " OR ".join(
        f"json_type(j.value,'$.{name}') IS NOT '{kind}'"
        for name, kind in (
            ("id", "text"),
            ("root_id", "text"),
            ("security_id", "text"),
            ("version", "integer"),
            ("action_type", "text"),
            ("effective_date", "text"),
            ("ratio_numerator", "integer"),
            ("ratio_denominator", "integer"),
        )
    )
    citation = "json_extract(NEW.assertion_json,'$.citation')"
    # Each CASE guards JSON functions that would otherwise raise on malformed scalar atoms.
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_basis_insert BEFORE INSERT ON dividend_basis_assertions
        WHEN EXISTS(SELECT 1 FROM dividend_basis_assertions WHERE id=NEW.id)
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
          OR NOT EXISTS(
                SELECT 1 FROM dividend_observations o JOIN provider_runs r ON r.id=o.provider_run_id
                WHERE o.id=NEW.observation_id AND o.provenance_version=1
                  AND o.normalized_sha256=NEW.source_sha256 AND o.security_id=NEW.security_id
                  AND o.ex_date=NEW.ex_date AND o.adjustment_basis='SPLIT_ADJUSTED'
                  AND r.provenance_version=1 AND r.status='OK_WITH_DATA'
                  AND r.operation='DIVIDENDS' AND r.security_id=o.security_id
                  AND r.provider_id=o.provider_id AND r.origin_group=o.origin_group
                  AND EXISTS(SELECT 1 FROM provider_observation_sightings s
                        WHERE s.run_id=r.id AND s.dividend_observation_id=o.id))
          OR CASE WHEN typeof(NEW.assertion_json) IS NOT 'text'
                    OR length(CAST(NEW.assertion_json AS BLOB))>131072
                    OR json_valid(NEW.assertion_json) IS NOT 1 THEN 1
              WHEN json_type(NEW.assertion_json) IS NOT 'object' THEN 1
              ELSE
                (SELECT count(*) FROM json_each(NEW.assertion_json))!={len(_FIELDS)}
                OR (SELECT count(DISTINCT key) FROM json_each(NEW.assertion_json))!={len(_FIELDS)}
                OR EXISTS(SELECT 1 FROM json_each(NEW.assertion_json)
                    WHERE key NOT IN ({_names(_FIELDS)}))
                OR {mirrors}
                OR json_extract(NEW.assertion_json,'$.source_basis')
                    IS NOT 'AFTER_EFFECTIVE_DATE_ACTIONS'
                OR json_extract(NEW.assertion_json,'$.target_basis') IS NOT 'PRE_EX_DATE_EVENTS'
                OR json_type(NEW.assertion_json,'$.complete') IS NOT 'true'
                OR json_type(NEW.assertion_json,'$.split_only') IS NOT 'true'
                OR NOT {_text(citation, 1, 300)}
                OR {citation} GLOB ('*['||char(1)||'-'||char(31)||char(127)||']*')
                OR NOT {_text("json_extract(NEW.assertion_json,'$.reason')", 10, 1000)}
                OR (json_type(NEW.assertion_json,'$.url') IS NOT 'null' AND (
                    json_type(NEW.assertion_json,'$.url') IS NOT 'text'
                    OR length(json_extract(NEW.assertion_json,'$.url')) NOT BETWEEN 1 AND 2048))
                OR EXISTS(SELECT 1 FROM json_tree(NEW.assertion_json)
                    WHERE type='text' AND instr(atom,char(0))>0)
                OR CASE WHEN json_type(NEW.assertion_json,'$.splits') IS NOT 'array' THEN 1
                    WHEN json_array_length(NEW.assertion_json,'$.splits')>100 THEN 1
                    WHEN EXISTS(SELECT 1 FROM json_each(NEW.assertion_json,'$.splits')
                        WHERE type IS NOT 'object') THEN 1
                    ELSE
                        (SELECT count(DISTINCT json_extract(value,'$.id'))
                            FROM json_each(NEW.assertion_json,'$.splits'))
                            !=json_array_length(NEW.assertion_json,'$.splits')
                        OR (SELECT count(DISTINCT json_extract(value,'$.root_id'))
                            FROM json_each(NEW.assertion_json,'$.splits'))
                            !=json_array_length(NEW.assertion_json,'$.splits')
                        OR EXISTS(
                            SELECT 1 FROM json_each(NEW.assertion_json,'$.splits') j
                            LEFT JOIN corporate_actions c ON c.id=json_extract(j.value,'$.id')
                            WHERE (SELECT count(*) FROM json_each(j.value))!={len(_REFS)}
                              OR (SELECT count(DISTINCT key) FROM json_each(j.value))!={len(_REFS)}
                              OR EXISTS(SELECT 1 FROM json_each(j.value)
                                    WHERE key NOT IN ({_names(_REFS)}))
                              OR {reference_types} OR {reference_mirrors}
                              OR NOT {_uuid("json_extract(j.value,'$.id')")}
                              OR NOT {_uuid("json_extract(j.value,'$.root_id')")}
                              OR json_extract(j.value,'$.status') IS NOT 'CONFIRMED'
                              OR json_extract(j.value,'$.lifecycle_status') IS NOT 'CURRENT'
                              OR typeof(json_extract(j.value,'$.version')) IS NOT 'integer'
                              OR typeof(json_extract(j.value,'$.ratio_numerator')) IS NOT 'integer'
                              OR typeof(json_extract(j.value,'$.ratio_denominator'))
                                    IS NOT 'integer'
                              OR c.id IS NULL OR c.security_id IS NOT NEW.security_id
                              OR c.is_current IS NOT 1 OR c.status IS NOT 'CONFIRMED'
                              OR c.version<1 OR c.ratio_num<1 OR c.ratio_den<1
                              OR NOT ((c.action_type='SPLIT' AND c.ratio_num>c.ratio_den)
                                    OR (c.action_type='REVERSE_SPLIT' AND c.ratio_num<c.ratio_den))
                              OR NOT {_window("c.effective_date")})
                        OR EXISTS(
                            SELECT 1 FROM corporate_actions c WHERE c.security_id=NEW.security_id
                              AND c.is_current=1 AND c.status!='VOID'
                              AND c.action_type!='SYMBOL_CHANGE' AND {_window("c.effective_date")}
                              AND (c.status!='CONFIRMED'
                                OR c.action_type NOT IN ('SPLIT','REVERSE_SPLIT')
                                OR NOT EXISTS(SELECT 1
                                    FROM json_each(NEW.assertion_json,'$.splits') j
                                    WHERE json_extract(j.value,'$.id')=c.id)))
                    END
              END
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_BASIS_ASSERTION'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_basis_no_{operation.lower()}
            BEFORE {operation} ON dividend_basis_assertions
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_BASIS_ASSERTION'); END
        """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_basis_source
            ON dividend_basis_assertions(owner_id,observation_id,created_at,id)
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_basis_security
            ON dividend_basis_assertions(security_id,created_at,id)
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_BASIS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; empty basis store without source or financial adoption",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting basis declarations"
    )
