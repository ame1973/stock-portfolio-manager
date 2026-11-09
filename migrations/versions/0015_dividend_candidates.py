"""Retain inert, append-only dividend candidate revisions and explicit source references."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0015_dividend_candidates"
down_revision = "0014_dividend_basis_assertions"
branch_labels = None
depends_on = None

_FIELDS = "'observation_id','source_sha256','basis_assertion_id','reason'"
_TYPES = (
    "'REGULAR','SPECIAL','INTERIM','FINAL','SUPPLEMENTAL',"
    "'ETF_DISTRIBUTION','REIT_DISTRIBUTION','UNKNOWN'"
)


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


def _reason(value: str) -> str:
    return f"""(typeof({value})='text' AND length({value}) BETWEEN 10 AND 1000
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' ')
        AND instr({value},char(0))=0)"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_candidate_revisions (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            candidate_id TEXT NOT NULL CHECK({_uuid("candidate_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK(typeof(owner_id)='text' AND instr(owner_id,char(0))=0),
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT
                CHECK({_uuid("security_id")}),
            revision_no INTEGER NOT NULL CHECK(typeof(revision_no)='integer'
                AND revision_no BETWEEN 1 AND 2147483647),
            state TEXT NOT NULL CHECK(state IN ('OPEN','DISMISSED')),
            event_type TEXT NOT NULL CHECK(event_type IN ({_TYPES})),
            ex_date TEXT CHECK(ex_date IS NULL OR {_day("ex_date")}),
            announce_date TEXT CHECK(announce_date IS NULL OR {_day("announce_date")}),
            record_date TEXT CHECK(record_date IS NULL OR {_day("record_date")}),
            pay_date TEXT CHECK(pay_date IS NULL OR {_day("pay_date")}),
            dps_e8 INTEGER CHECK(dps_e8 IS NULL OR (typeof(dps_e8)='integer' AND dps_e8>=0)),
            currency TEXT CHECK(currency IS NULL OR currency IN ('HKD','USD')),
            adjustment_basis TEXT NOT NULL
                CHECK(adjustment_basis IN ('AS_DECLARED','SPLIT_ADJUSTED','UNKNOWN')),
            matches_json TEXT NOT NULL CHECK(typeof(matches_json)='text'
                AND length(CAST(matches_json AS BLOB))<=131072 AND json_valid(matches_json)),
            matches_sha256 TEXT NOT NULL CHECK({_hash("matches_sha256")}),
            change_reason TEXT NOT NULL CHECK({_reason("change_reason")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(candidate_id,revision_no)
        )
    """)
    observation = "json_extract(j.value,'$.observation_id')"
    fingerprint = "json_extract(j.value,'$.source_sha256')"
    basis = "json_extract(j.value,'$.basis_assertion_id')"
    reason = "json_extract(j.value,'$.reason')"
    # CASE prevents JSON functions from evaluating malformed arrays or scalar elements.
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_candidate_insert BEFORE INSERT ON dividend_candidate_revisions
        WHEN EXISTS(SELECT 1 FROM dividend_candidate_revisions WHERE id=NEW.id)
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
          OR NEW.revision_no!=COALESCE((SELECT max(revision_no) FROM dividend_candidate_revisions
                WHERE candidate_id=NEW.candidate_id),0)+1
          OR (SELECT count(*) FROM dividend_candidate_revisions
                WHERE candidate_id=NEW.candidate_id)!=NEW.revision_no-1
          OR EXISTS(SELECT 1 FROM dividend_candidate_revisions
                WHERE candidate_id=NEW.candidate_id AND (owner_id IS NOT NEW.owner_id
                    OR security_id IS NOT NEW.security_id OR created_at>NEW.created_at))
          OR CASE WHEN typeof(NEW.matches_json) IS NOT 'text'
                    OR length(CAST(NEW.matches_json AS BLOB))>131072
                    OR json_valid(NEW.matches_json) IS NOT 1 THEN 1
              WHEN json_type(NEW.matches_json) IS NOT 'array' THEN 1
              WHEN json_array_length(NEW.matches_json)>100 THEN 1
              WHEN EXISTS(SELECT 1 FROM json_each(NEW.matches_json)
                    WHERE type IS NOT 'object') THEN 1
              ELSE
                (SELECT count(DISTINCT json_extract(value,'$.observation_id'))
                    FROM json_each(NEW.matches_json))!=json_array_length(NEW.matches_json)
                OR EXISTS(
                    SELECT 1 FROM json_each(NEW.matches_json) j
                    LEFT JOIN dividend_observations o ON o.id={observation}
                    LEFT JOIN provider_runs r ON r.id=o.provider_run_id
                    WHERE (SELECT count(*) FROM json_each(j.value))!=4
                      OR (SELECT count(DISTINCT key) FROM json_each(j.value))!=4
                      OR EXISTS(SELECT 1 FROM json_each(j.value) WHERE key NOT IN ({_FIELDS}))
                      OR json_type(j.value,'$.observation_id') IS NOT 'text'
                      OR json_type(j.value,'$.source_sha256') IS NOT 'text'
                      OR (json_type(j.value,'$.basis_assertion_id') IS NOT 'null'
                          AND json_type(j.value,'$.basis_assertion_id') IS NOT 'text')
                      OR json_type(j.value,'$.reason') IS NOT 'text'
                      OR NOT {_uuid(observation)} OR NOT {_hash(fingerprint)}
                      OR NOT {_reason(reason)}
                      OR o.id IS NULL OR o.provenance_version IS NOT 1
                      OR o.security_id IS NOT NEW.security_id
                      OR o.normalized_sha256 IS NOT {fingerprint}
                      OR r.provenance_version IS NOT 1 OR r.status IS NOT 'OK_WITH_DATA'
                      OR r.operation IS NOT 'DIVIDENDS' OR r.security_id IS NOT o.security_id
                      OR r.provider_id IS NOT o.provider_id OR r.origin_group IS NOT o.origin_group
                      OR NOT EXISTS(SELECT 1 FROM provider_observation_sightings s
                            WHERE s.run_id=r.id AND s.dividend_observation_id=o.id)
                      OR (json_type(j.value,'$.basis_assertion_id')='text' AND (
                            NOT {_uuid(basis)} OR NOT EXISTS(
                                SELECT 1 FROM dividend_basis_assertions b WHERE b.id={basis}
                                  AND b.owner_id=NEW.owner_id AND b.security_id=NEW.security_id
                                  AND b.observation_id=o.id
                                  AND b.source_sha256=o.normalized_sha256)))
                )
              END
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_CANDIDATE_REVISION'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_candidate_no_{operation.lower()}
            BEFORE {operation} ON dividend_candidate_revisions
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_CANDIDATE_REVISION'); END
        """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_candidate_owner
            ON dividend_candidate_revisions(owner_id,security_id,candidate_id,revision_no DESC)
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_candidate_created
            ON dividend_candidate_revisions(owner_id,created_at,id)
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_CANDIDATES',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; empty candidate history without financial adoption",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting candidate history"
    )
