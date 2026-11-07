"""Add inert, owned, append-only managed provider policy and credential selections."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0012_provider_profiles"
down_revision = "0011_source_schedules"
branch_labels = None
depends_on = None

_PROVIDERS = (
    "'eodhd','alphavantage','massive','yahoo','yfinance','hkex_evidence','sec_evidence','manual'"
)
_KEYS = "'eodhd','alphavantage','massive'"
_MANUAL = "'hkex_evidence','sec_evidence','manual'"


def _uuid(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=36
        AND substr({value},9,1)='-' AND substr({value},14,1)='-'
        AND substr({value},19,1)='-' AND substr({value},24,1)='-'
        AND length(replace({value},'-',''))=32
        AND replace({value},'-','') NOT GLOB '*[^0-9a-f]*')"""


def _utc(value: str) -> str:
    day = f"substr({value},1,10)"
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=27
        AND substr({value},1,4)!='0000' AND date({day},'+0 days') IS {day}
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
        CREATE TABLE provider_profile_revisions (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
            provider_id TEXT NOT NULL CHECK(provider_id IN ({_PROVIDERS})),
            version INTEGER NOT NULL CHECK(typeof(version)='integer'
                AND version BETWEEN 1 AND 2147483647),
            enabled INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(enabled)='integer' AND enabled IN (0,1)),
            terms_acknowledged INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(terms_acknowledged)='integer' AND terms_acknowledged IN (0,1)),
            terms_reference TEXT NOT NULL DEFAULT '' CHECK(typeof(terms_reference)='text'
                AND length(terms_reference)<=1000 AND instr(terms_reference,char(0))=0),
            terms_reason TEXT CHECK(terms_reason IS NULL OR {_reason("terms_reason")}),
            requests_per_minute INTEGER CHECK(requests_per_minute IS NULL
                OR (typeof(requests_per_minute)='integer'
                    AND requests_per_minute BETWEEN 1 AND 2147483647)),
            requests_per_day INTEGER CHECK(requests_per_day IS NULL
                OR (typeof(requests_per_day)='integer'
                    AND requests_per_day BETWEEN 1 AND 2147483647)),
            credential_artifact_id TEXT CHECK(credential_artifact_id IS NULL
                OR {_uuid("credential_artifact_id")}),
            raw_payload_storage INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(raw_payload_storage)='integer' AND raw_payload_storage=0),
            change_reason TEXT NOT NULL CHECK({_reason("change_reason")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(owner_id,provider_id,version),
            CHECK(terms_acknowledged=0 OR (length(trim(terms_reference,
                char(9)||char(10)||char(11)||char(12)||char(13)||' '))>0
                AND terms_reason IS NOT NULL)),
            CHECK(credential_artifact_id IS NULL OR provider_id IN ({_KEYS})),
            CHECK(provider_id NOT IN ({_MANUAL})
                OR (requests_per_minute IS NULL AND requests_per_day IS NULL)),
            CHECK(enabled=0 OR (terms_acknowledged=1
                AND (provider_id NOT IN ({_KEYS}) OR credential_artifact_id IS NOT NULL)
                AND (provider_id IN ({_MANUAL})
                    OR (requests_per_minute IS NOT NULL AND requests_per_day IS NOT NULL))))
        )
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_provider_profile_insert BEFORE INSERT ON provider_profile_revisions
        WHEN NEW.version!=COALESCE((SELECT max(version) FROM provider_profile_revisions
                WHERE owner_id=NEW.owner_id AND provider_id=NEW.provider_id),0)+1
          OR (NEW.version=1 AND NEW.enabled!=0)
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
          OR EXISTS(SELECT 1 FROM provider_profile_revisions WHERE id=NEW.id)
          OR EXISTS(SELECT 1 FROM provider_profile_revisions
                WHERE owner_id=NEW.owner_id AND provider_id=NEW.provider_id
                  AND created_at>NEW.created_at)
          OR (NEW.credential_artifact_id IS NOT NULL AND EXISTS(
                SELECT 1 FROM provider_profile_revisions
                WHERE credential_artifact_id=NEW.credential_artifact_id
                  AND (owner_id!=NEW.owner_id OR provider_id!=NEW.provider_id)))
        BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_PROFILE_REVISION'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_provider_profile_no_{operation.lower()}
            BEFORE {operation} ON provider_profile_revisions
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_PROVIDER_PROFILE_REVISION'); END
        """)
    con.exec_driver_sql("""
        CREATE INDEX idx_provider_profile_credential
            ON provider_profile_revisions(credential_artifact_id)
            WHERE credential_artifact_id IS NOT NULL
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_PROVIDER_PROFILES',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; empty managed provider history without file adoption",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting provider history"
    )
