"""Retain immutable original trade audit inputs without modifying trade facts."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0017_trade_audit_origins"
down_revision = "0016_dividend_event_controls"
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
        CREATE TABLE trade_audit_origins (
            trade_id TEXT NOT NULL PRIMARY KEY REFERENCES trades(id) ON DELETE RESTRICT
                CHECK({_uuid("trade_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            creation_audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("creation_audit_id")}),
            original_status TEXT NOT NULL
                CHECK(original_status IN ('ACTIVE','DRAFT_SCHEDULED','PENDING_FX')),
            original_updated_at TEXT NOT NULL CHECK({_utc("original_updated_at")}),
            capture_mode TEXT NOT NULL
                CHECK(capture_mode IN ('AT_CREATION','BEFORE_RETIRE','RETAINED_RESPONSE')),
            capture_request_id TEXT NOT NULL CHECK({_uuid("capture_request_id")}),
            capture_audit_id TEXT UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK(capture_audit_id IS NULL OR {_uuid("capture_audit_id")}),
            captured_at TEXT NOT NULL CHECK({_utc("captured_at")})
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_trade_audit_origin_insert BEFORE INSERT ON trade_audit_origins
        WHEN EXISTS(SELECT 1 FROM trade_audit_origins old
                    WHERE old.trade_id=NEW.trade_id OR old.creation_audit_id=NEW.creation_audit_id
                      OR (NEW.capture_audit_id IS NOT NULL
                          AND old.capture_audit_id=NEW.capture_audit_id))
          OR NOT EXISTS(
            SELECT 1 FROM trades t JOIN accounts ac ON ac.id=t.account_id
            JOIN portfolios p ON p.id=t.portfolio_id AND p.id=ac.portfolio_id
            JOIN brokers b ON b.id=ac.broker_id AND b.owner_id=p.owner_id
            JOIN owners o ON o.id=p.owner_id AND o.is_active=1
            JOIN ledger_sequences q ON q.sequence=t.created_sequence
              AND q.kind='TRADE' AND q.fact_id=t.root_id
            JOIN audit_events a ON a.id=NEW.creation_audit_id
            WHERE t.id=NEW.trade_id AND p.owner_id=NEW.owner_id
              AND typeof(t.revision)='integer' AND t.revision BETWEEN 1 AND 9223372036854775807
              AND a.owner_id=NEW.owner_id AND a.entity_type='trade' AND a.entity_id=t.id
              AND a.action=CASE WHEN t.revision=1 THEN 'CREATE' ELSE 'REVISE' END
              AND (SELECT count(*) FROM audit_events x WHERE x.entity_type='trade'
                   AND x.entity_id=t.id AND x.action=a.action)=1
              AND {_uuid("a.request_id")} AND {_hash("a.after_sha256")}
              AND ((a.action='CREATE' AND a.before_sha256 IS NULL)
                   OR (a.action='REVISE' AND {_hash("a.before_sha256")}))
              AND {_utc("a.created_at")}
              AND ((NEW.capture_mode IN ('AT_CREATION','BEFORE_RETIRE')
                    AND t.status=NEW.original_status AND t.version=1
                    AND t.updated_at=NEW.original_updated_at)
                   OR (NEW.capture_mode='RETAINED_RESPONSE' AND t.status IN ('SUPERSEDED','VOID')))
              AND ((NEW.capture_mode='AT_CREATION' AND NEW.capture_audit_id IS NULL
                    AND NEW.capture_request_id=a.request_id AND NEW.captured_at=a.created_at)
                   OR (NEW.capture_mode IN ('BEFORE_RETIRE','RETAINED_RESPONSE')
                       AND EXISTS(SELECT 1 FROM audit_events c WHERE c.id=NEW.capture_audit_id
                           AND c.owner_id=NEW.owner_id AND c.entity_type='trade_audit_origin'
                           AND c.entity_id=NEW.trade_id AND c.request_id=NEW.capture_request_id
                           AND c.action=CASE WHEN NEW.capture_mode='BEFORE_RETIRE'
                                      THEN 'TRADE_AUDIT_ORIGIN_CAPTURED'
                                      ELSE 'TRADE_AUDIT_ORIGIN_RECOVERED' END
                           AND (SELECT count(*) FROM audit_events x
                                WHERE x.entity_type='trade_audit_origin' AND x.entity_id=t.id
                                  AND x.action=c.action)=1
                           AND c.before_sha256 IS NULL AND {_hash("c.after_sha256")}
                           AND {_reason("c.reason")} AND c.created_at=NEW.captured_at)))
          )
        BEGIN SELECT RAISE(ABORT,'INVALID_OR_IMMUTABLE_TRADE_AUDIT_ORIGIN'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_trade_audit_origin_{operation.lower()}
            BEFORE {operation} ON trade_audit_origins
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_TRADE_AUDIT_ORIGIN'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) VALUES (?,?,'schema',?,"
            "'MIGRATE_TRADE_AUDIT_ORIGINS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; preserve trade facts without inventing audit evidence",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting audit history")
