"""Retain explicit, revision-scoped dividend tax-setting estimates."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0021_dividend_tax_settings"
down_revision = "0020_dividend_projection_history"
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
    day = f"substr({value},1,10)"
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=27
        AND {_day(day)} AND substr({value},11,1)='T'
        AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _reason(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0
        AND length({value}) BETWEEN 10 AND 1000
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' '))"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_tax_settings (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT
                CHECK({_uuid("revision_id")}),
            revision_no INTEGER NOT NULL
                CHECK(typeof(revision_no)='integer' AND revision_no BETWEEN 1 AND 2147483647),
            portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT
                CHECK({_uuid("portfolio_id")}),
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT
                CHECK({_uuid("account_id")}),
            broker_id TEXT NOT NULL REFERENCES brokers(id) ON DELETE RESTRICT
                CHECK({_uuid("broker_id")}),
            account_version INTEGER NOT NULL
                CHECK(typeof(account_version)='integer' AND account_version>0),
            broker_version INTEGER NOT NULL
                CHECK(typeof(broker_version)='integer' AND broker_version>0),
            setting_no INTEGER NOT NULL CHECK(typeof(setting_no)='integer' AND setting_no>0),
            previous_id TEXT REFERENCES dividend_tax_settings(id) ON DELETE RESTRICT
                CHECK(previous_id IS NULL OR {_uuid("previous_id")}),
            effective_from TEXT NOT NULL CHECK({_day("effective_from")}),
            tax_status TEXT NOT NULL CHECK(tax_status IN ('ESTIMATED','UNKNOWN')),
            rate_ppm INTEGER,
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(event_id,revision_id,account_id,setting_no),
            UNIQUE(event_id,revision_id,account_id,request_id),
            CHECK((tax_status='UNKNOWN' AND rate_ppm IS NULL) OR
                  (tax_status='ESTIMATED' AND typeof(rate_ppm)='integer'
                   AND rate_ppm BETWEEN 0 AND 1000000))
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_tax_settings_scope
        ON dividend_tax_settings(owner_id,event_id,revision_id,account_id,setting_no)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_tax_settings_insert
        BEFORE INSERT ON dividend_tax_settings
        WHEN EXISTS(SELECT 1 FROM dividend_tax_settings old
                    WHERE old.id=NEW.id OR old.audit_id=NEW.audit_id
                       OR (old.event_id=NEW.event_id AND old.revision_id=NEW.revision_id
                           AND old.account_id=NEW.account_id AND
                           (old.setting_no=NEW.setting_no OR old.request_id=NEW.request_id)))
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
              JOIN accounts account ON account.id=NEW.account_id
                   AND account.portfolio_id=NEW.portfolio_id AND account.broker_id=NEW.broker_id
                   AND account.version=NEW.account_version
              JOIN portfolios portfolio ON portfolio.id=NEW.portfolio_id
                   AND portfolio.owner_id=ctl.owner_id
              JOIN brokers broker ON broker.id=NEW.broker_id AND broker.owner_id=ctl.owner_id
                   AND broker.version=NEW.broker_version
              JOIN audit_events setting_audit ON setting_audit.id=NEW.audit_id
              WHERE event.id=NEW.event_id AND event.provenance_version=1
                AND event.current_revision_id=rev.id AND event.version=rev.revision_no
                AND event.workflow_status=rev.review_status AND ctl.owner_id=NEW.owner_id
                AND {_hash("source_audit.after_sha256")} AND {_utc("source_audit.created_at")}
                AND source_audit.reason=rev.reason
                AND NEW.created_at>=rev.created_at AND NEW.created_at>=source_audit.created_at
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=rev.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
                AND setting_audit.owner_id=NEW.owner_id
                AND setting_audit.entity_type='dividend_tax_setting'
                AND setting_audit.entity_id=NEW.id
                AND setting_audit.action='DIVIDEND_TAX_SETTING_RECORDED'
                AND setting_audit.request_id=NEW.request_id
                AND {_utc("setting_audit.created_at")} AND setting_audit.created_at>=NEW.created_at
                AND {_hash("setting_audit.after_sha256")} AND {_reason("setting_audit.reason")}
                AND ((NEW.setting_no=1 AND setting_audit.before_sha256 IS NULL) OR
                     (NEW.setting_no>1 AND {_hash("setting_audit.before_sha256")}))
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_tax_setting' AND a.entity_id=NEW.id
                       AND a.action='DIVIDEND_TAX_SETTING_RECORDED')=1)
          OR NOT ((NEW.setting_no=1 AND NEW.previous_id IS NULL AND NOT EXISTS(
              SELECT 1 FROM dividend_tax_settings old
              WHERE old.event_id=NEW.event_id AND old.revision_id=NEW.revision_id
                AND old.account_id=NEW.account_id)) OR EXISTS(
              SELECT 1 FROM dividend_tax_settings previous
              WHERE previous.id=NEW.previous_id AND previous.owner_id=NEW.owner_id
                AND previous.event_id=NEW.event_id AND previous.revision_id=NEW.revision_id
                AND previous.account_id=NEW.account_id AND previous.setting_no=NEW.setting_no-1
                AND previous.created_at<=NEW.created_at))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_TAX_SETTING'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_tax_settings_{operation.lower()}
            BEFORE {operation} ON dividend_tax_settings
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_TAX_SETTING'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_TAX_SETTINGS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; preserve tax provenance without inferring a rate",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting tax settings")
