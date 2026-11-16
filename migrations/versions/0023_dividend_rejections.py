"""Retain explicit dividend revision rejections without changing financial facts."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0023_dividend_rejections"
down_revision = "0022_dividend_projection_tax"
branch_labels = None
depends_on = None


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
        AND {_day(f"substr({value},1,10)")} AND substr({value},11,1)='T'
        AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _text(value: str, minimum: int, maximum: int) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0
        AND length({value}) BETWEEN {minimum} AND {maximum}
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' '))"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_event_rejections (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            revision_id TEXT NOT NULL UNIQUE REFERENCES dividend_event_revisions(id)
                ON DELETE RESTRICT CHECK({_uuid("revision_id")}),
            revision_no INTEGER NOT NULL
                CHECK(typeof(revision_no)='integer' AND revision_no BETWEEN 1 AND 2147483647),
            citation TEXT NOT NULL CHECK({_text("citation", 1, 300)}),
            source_url TEXT CHECK(source_url IS NULL OR {_text("source_url", 1, 2048)}),
            reason TEXT NOT NULL CHECK({_text("reason", 10, 1000)}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_event_rejections_scope
        ON dividend_event_rejections(owner_id,event_id,revision_no)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_rejections_insert
        BEFORE INSERT ON dividend_event_rejections
        WHEN EXISTS(SELECT 1 FROM dividend_event_rejections old
                    WHERE old.id=NEW.id OR old.revision_id=NEW.revision_id
                       OR old.audit_id=NEW.audit_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_events event
              JOIN dividend_event_controls ctl ON ctl.event_id=event.id
              JOIN owners owner ON owner.id=ctl.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions rev ON rev.id=NEW.revision_id
                   AND rev.event_id=event.id AND rev.revision_no=NEW.revision_no
              JOIN dividend_revision_decisions decision ON decision.revision_id=rev.id
                   AND decision.event_id=event.id AND decision.owner_id=ctl.owner_id
              JOIN audit_events source_audit ON source_audit.entity_type='dividend_event_revision'
                   AND source_audit.entity_id=rev.id
                   AND source_audit.action='DIVIDEND_EVENT_REVISED'
                   AND source_audit.owner_id=ctl.owner_id
                   AND source_audit.request_id=decision.request_id
              JOIN audit_events rejection_audit ON rejection_audit.id=NEW.audit_id
              WHERE event.id=NEW.event_id AND event.provenance_version=1
                AND event.current_revision_id=rev.id AND event.version=rev.revision_no
                AND event.workflow_status=rev.review_status AND ctl.owner_id=NEW.owner_id
                AND source_audit.reason=rev.reason AND {_hash("source_audit.after_sha256")}
                AND {_utc("source_audit.created_at")} AND NEW.created_at>=rev.created_at
                AND NEW.created_at>=source_audit.created_at
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=rev.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
                AND rejection_audit.owner_id=NEW.owner_id
                AND rejection_audit.entity_type='dividend_event_rejection'
                AND rejection_audit.entity_id=NEW.id
                AND rejection_audit.action='DIVIDEND_EVENT_REVISION_REJECTED'
                AND rejection_audit.request_id=NEW.request_id
                AND rejection_audit.reason=NEW.reason
                AND {_hash("rejection_audit.before_sha256")}
                AND {_hash("rejection_audit.after_sha256")}
                AND {_utc("rejection_audit.created_at")}
                AND rejection_audit.created_at>=NEW.created_at
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_rejection' AND a.entity_id=NEW.id
                       AND a.action='DIVIDEND_EVENT_REVISION_REJECTED')=1)
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_EVENT_REJECTION'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_rejections_{operation.lower()}
            BEFORE {operation} ON dividend_event_rejections
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_EVENT_REJECTION'); END
        """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_rejection_event_publication
        BEFORE UPDATE OF current_revision_id ON dividend_events
        WHEN NEW.current_revision_id IS NOT OLD.current_revision_id AND EXISTS(
            SELECT 1 FROM dividend_event_rejections rejected
            WHERE rejected.event_id=OLD.id AND rejected.revision_id=OLD.current_revision_id)
        BEGIN SELECT RAISE(ABORT,'DIVIDEND_EVENT_REJECTION_PENDING'); END
    """)
    for table in ("dividend_entitlements", "dividend_projection_versions", "dividend_tax_settings"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_rejection_{table}_insert
            BEFORE INSERT ON {table}
            WHEN EXISTS(SELECT 1 FROM dividend_event_rejections rejected
                        WHERE rejected.revision_id=NEW.revision_id)
            BEGIN SELECT RAISE(ABORT,'DIVIDEND_EVENT_REJECTION_PENDING'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_REJECTIONS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; retain explicit revision rejections "
                "without financial changes",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting rejections")
