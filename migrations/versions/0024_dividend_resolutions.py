"""Bind rejection resolution to an audited immediate successor event revision."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0024_dividend_resolutions"
down_revision = "0023_dividend_rejections"
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


def _utc(value: str) -> str:
    day = f"substr({value},1,10)"
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=27
        AND substr({day},1,4)!='0000' AND date({day},'+0 days') IS {day}
        AND substr({value},11,1)='T' AND substr({value},20,1)='.'
        AND substr({value},27,1)='Z' AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _reason(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0
        AND length({value}) BETWEEN 10 AND 1000
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' '))"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_rejection_resolutions (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            rejection_id TEXT NOT NULL UNIQUE REFERENCES dividend_event_rejections(id)
                ON DELETE RESTRICT CHECK({_uuid("rejection_id")}),
            rejected_revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id)
                ON DELETE RESTRICT CHECK({_uuid("rejected_revision_id")}),
            successor_revision_id TEXT NOT NULL UNIQUE REFERENCES dividend_event_revisions(id)
                ON DELETE RESTRICT CHECK({_uuid("successor_revision_id")}),
            successor_revision_no INTEGER NOT NULL CHECK(typeof(successor_revision_no)='integer'
                AND successor_revision_no BETWEEN 2 AND 2147483647),
            reason TEXT NOT NULL CHECK({_reason("reason")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_rejection_resolutions_event
        ON dividend_rejection_resolutions(owner_id,event_id,successor_revision_no)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_resolutions_insert
        BEFORE INSERT ON dividend_rejection_resolutions
        WHEN EXISTS(SELECT 1 FROM dividend_rejection_resolutions old
                    WHERE old.id=NEW.id OR old.rejection_id=NEW.rejection_id
                       OR old.successor_revision_id=NEW.successor_revision_id
                       OR old.audit_id=NEW.audit_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_event_rejections rejected
              JOIN dividend_events event ON event.id=rejected.event_id
              JOIN dividend_event_controls ctl ON ctl.event_id=event.id
              JOIN owners owner ON owner.id=ctl.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions previous ON previous.id=rejected.revision_id
                   AND previous.event_id=event.id AND previous.revision_no=event.version
              JOIN dividend_event_revisions successor ON successor.id=NEW.successor_revision_id
                   AND successor.event_id=event.id AND successor.revision_no=event.version+1
              JOIN dividend_revision_decisions decision ON decision.revision_id=successor.id
                   AND decision.event_id=event.id AND decision.owner_id=ctl.owner_id
                   AND decision.request_id=NEW.request_id
                   AND decision.created_at=successor.created_at
              JOIN audit_events rejection_audit ON rejection_audit.id=rejected.audit_id
              JOIN audit_events successor_audit
                   ON successor_audit.entity_type='dividend_event_revision'
                   AND successor_audit.entity_id=successor.id
                   AND successor_audit.action='DIVIDEND_EVENT_REVISED'
                   AND successor_audit.owner_id=ctl.owner_id
                   AND successor_audit.request_id=NEW.request_id
              JOIN audit_events resolution_audit ON resolution_audit.id=NEW.audit_id
              WHERE rejected.id=NEW.rejection_id AND rejected.owner_id=NEW.owner_id
                AND event.id=NEW.event_id AND event.provenance_version=1
                AND ctl.owner_id=NEW.owner_id AND event.current_revision_id=previous.id
                AND rejected.revision_id=NEW.rejected_revision_id
                AND rejected.revision_no=previous.revision_no
                AND NEW.successor_revision_no=successor.revision_no
                AND successor.reason=NEW.reason AND successor_audit.reason=NEW.reason
                AND successor.created_at=NEW.created_at AND NEW.created_at>=rejected.created_at
                AND {_utc("rejection_audit.created_at")}
                AND NEW.created_at>=rejection_audit.created_at
                AND {_utc("successor_audit.created_at")}
                AND {_hash("successor_audit.before_sha256")}
                AND {_hash("successor_audit.after_sha256")}
                AND (SELECT count(*) FROM dividend_event_revisions r
                     WHERE r.event_id=event.id)=event.version+1
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=successor.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
                AND rejection_audit.owner_id=NEW.owner_id
                AND rejection_audit.entity_type='dividend_event_rejection'
                AND rejection_audit.entity_id=rejected.id
                AND rejection_audit.action='DIVIDEND_EVENT_REVISION_REJECTED'
                AND rejection_audit.request_id=rejected.request_id
                AND rejection_audit.reason=rejected.reason
                AND {_hash("rejection_audit.before_sha256")}
                AND {_hash("rejection_audit.after_sha256")}
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_rejection' AND a.entity_id=rejected.id
                       AND a.action='DIVIDEND_EVENT_REVISION_REJECTED')=1
                AND resolution_audit.owner_id=NEW.owner_id
                AND resolution_audit.entity_type='dividend_event_revision'
                AND resolution_audit.entity_id=successor.id
                AND resolution_audit.action='DIVIDEND_EVENT_REJECTION_RESOLUTION'
                AND resolution_audit.request_id=NEW.request_id
                AND resolution_audit.reason=NEW.reason
                AND {_hash("resolution_audit.before_sha256")}
                AND {_hash("resolution_audit.after_sha256")}
                AND {_utc("resolution_audit.created_at")}
                AND resolution_audit.created_at>=NEW.created_at
                AND resolution_audit.created_at>=successor_audit.created_at
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=successor.id
                       AND a.action='DIVIDEND_EVENT_REJECTION_RESOLUTION')=1)
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_REJECTION_RESOLUTION'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_resolutions_{operation.lower()}
            BEFORE {operation} ON dividend_rejection_resolutions
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_REJECTION_RESOLUTION'); END
        """)
    # The replacement remains conservative until atomic financial rebuilding exists.
    con.exec_driver_sql("DROP TRIGGER trg_dividend_rejection_event_publication")
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_rejection_event_publication
        BEFORE UPDATE OF current_revision_id ON dividend_events
        WHEN NEW.current_revision_id IS NOT OLD.current_revision_id AND EXISTS(
            SELECT 1 FROM dividend_event_rejections rejected
            WHERE rejected.event_id=OLD.id AND rejected.revision_id=OLD.current_revision_id
              AND (NOT EXISTS(
                    SELECT 1 FROM dividend_rejection_resolutions resolution
                    JOIN dividend_event_controls ctl ON ctl.event_id=OLD.id
                    WHERE resolution.rejection_id=rejected.id AND resolution.event_id=OLD.id
                      AND resolution.owner_id=ctl.owner_id
                      AND resolution.rejected_revision_id=OLD.current_revision_id
                      AND resolution.successor_revision_id=NEW.current_revision_id
                      AND resolution.successor_revision_no=NEW.version)
                OR EXISTS(
                    SELECT 1 FROM dividend_entitlements entitlement
                    JOIN dividend_event_revisions revision ON revision.id=entitlement.revision_id
                    WHERE revision.event_id=OLD.id)
                OR EXISTS(SELECT 1 FROM dividend_projection_versions projection
                          WHERE projection.event_id=OLD.id)))
        BEGIN SELECT RAISE(ABORT,'DIVIDEND_EVENT_REJECTION_PENDING'); END
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_RESOLUTIONS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; retain explicit rejection resolutions",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting resolutions")
