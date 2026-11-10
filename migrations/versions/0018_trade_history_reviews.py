"""Retain immutable decisions about unverified trade-history gaps."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0018_trade_history_reviews"
down_revision = "0017_trade_audit_origins"
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
        CREATE TABLE trade_history_reviews (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            trade_id TEXT NOT NULL REFERENCES trades(id) ON DELETE RESTRICT
                CHECK({_uuid("trade_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            revision INTEGER NOT NULL
                CHECK(typeof(revision)='integer' AND revision BETWEEN 1 AND 9223372036854775807),
            previous_review_id TEXT REFERENCES trade_history_reviews(id) ON DELETE RESTRICT
                CHECK(previous_review_id IS NULL OR {_uuid("previous_review_id")}),
            decision TEXT NOT NULL CHECK(decision IN ('ACKNOWLEDGE_UNVERIFIED','WITHDRAW')),
            gap TEXT NOT NULL CHECK(gap IN ('MISSING_CREATION_AUDIT','MISSING_RETIRED_ORIGIN')),
            observed_status TEXT NOT NULL CHECK(observed_status IN
                ('ACTIVE','DRAFT_SCHEDULED','PENDING_FX','SUPERSEDED','VOID')),
            observed_trade_version INTEGER NOT NULL
                CHECK(typeof(observed_trade_version)='integer' AND observed_trade_version IN (1,2)),
            observed_updated_at TEXT NOT NULL CHECK({_utc("observed_updated_at")}),
            snapshot_sha256 TEXT NOT NULL CHECK({_hash("snapshot_sha256")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(trade_id,revision), UNIQUE(trade_id,request_id)
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_trade_history_reviews_owner
        ON trade_history_reviews(owner_id,trade_id,revision)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_trade_history_review_insert BEFORE INSERT ON trade_history_reviews
        WHEN EXISTS(SELECT 1 FROM trade_history_reviews old
                    WHERE old.id=NEW.id OR old.audit_id=NEW.audit_id
                       OR (old.trade_id=NEW.trade_id AND
                           (old.revision=NEW.revision OR old.request_id=NEW.request_id)))
          OR NEW.revision IS NOT COALESCE(
               (SELECT max(old.revision) FROM trade_history_reviews old
                WHERE old.trade_id=NEW.trade_id),0)+1
          OR NOT (
            (NEW.revision=1 AND NEW.previous_review_id IS NULL
             AND NEW.decision='ACKNOWLEDGE_UNVERIFIED')
            OR (NEW.revision>1 AND EXISTS(
                SELECT 1 FROM trade_history_reviews prev
                WHERE prev.id=NEW.previous_review_id AND prev.trade_id=NEW.trade_id
                  AND prev.owner_id=NEW.owner_id AND prev.revision=NEW.revision-1
                  AND prev.decision!=NEW.decision AND NEW.created_at>=prev.created_at
                  AND (NEW.decision='ACKNOWLEDGE_UNVERIFIED' OR NEW.gap=prev.gap)))
          )
          OR NOT EXISTS(
            SELECT 1 FROM trades t JOIN accounts ac ON ac.id=t.account_id
            JOIN portfolios p ON p.id=t.portfolio_id AND p.id=ac.portfolio_id
            JOIN brokers b ON b.id=ac.broker_id AND b.owner_id=p.owner_id
            JOIN owners o ON o.id=p.owner_id AND o.is_active=1
            JOIN ledger_sequences q ON q.sequence=t.created_sequence
              AND q.kind='TRADE' AND q.fact_id=t.root_id
            JOIN audit_events a ON a.id=NEW.audit_id
            WHERE t.id=NEW.trade_id AND p.owner_id=NEW.owner_id
              AND typeof(t.revision)='integer' AND t.revision BETWEEN 1 AND 9223372036854775807
              AND t.status=NEW.observed_status AND t.version=NEW.observed_trade_version
              AND t.updated_at=NEW.observed_updated_at
              AND ((t.version=1 AND t.status IN ('ACTIVE','DRAFT_SCHEDULED','PENDING_FX'))
                   OR (t.version=2 AND t.status IN ('SUPERSEDED','VOID')))
              AND a.owner_id=NEW.owner_id AND a.entity_type='trade_history_review'
              AND a.entity_id=NEW.id AND a.request_id=NEW.request_id
              AND a.action=CASE WHEN NEW.decision='ACKNOWLEDGE_UNVERIFIED'
                           THEN 'TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED'
                           ELSE 'TRADE_HISTORY_UNVERIFIED_WITHDRAWN' END
              AND (SELECT count(*) FROM audit_events x WHERE x.entity_type='trade_history_review'
                   AND x.entity_id=NEW.id AND x.action=a.action)=1
              AND {_uuid("a.request_id")} AND {_hash("a.after_sha256")}
              AND ((NEW.revision=1 AND a.before_sha256 IS NULL)
                   OR (NEW.revision>1 AND {_hash("a.before_sha256")}))
              AND {_reason("a.reason")} AND a.created_at=NEW.created_at
              AND (NEW.decision='WITHDRAW' OR (
                   NOT EXISTS(SELECT 1 FROM trade_audit_origins origin WHERE origin.trade_id=t.id)
                   AND ((NEW.gap='MISSING_CREATION_AUDIT' AND NOT EXISTS(
                          SELECT 1 FROM audit_events c
                          WHERE c.entity_type='trade' AND c.entity_id=t.id
                            AND c.action=CASE WHEN t.revision=1 THEN 'CREATE' ELSE 'REVISE' END))
                        OR (NEW.gap='MISSING_RETIRED_ORIGIN'
                            AND t.status IN ('SUPERSEDED','VOID')
                            AND (SELECT count(*) FROM audit_events c WHERE c.entity_type='trade'
                                 AND c.entity_id=t.id AND c.action=CASE WHEN t.revision=1
                                     THEN 'CREATE' ELSE 'REVISE' END)=1
                            AND EXISTS(SELECT 1 FROM audit_events c
                                WHERE c.entity_type='trade' AND c.entity_id=t.id
                                  AND c.action=CASE WHEN t.revision=1
                                      THEN 'CREATE' ELSE 'REVISE' END
                                  AND c.owner_id=NEW.owner_id AND {_uuid("c.id")}
                                  AND {_uuid("c.request_id")} AND {_utc("c.created_at")}
                                  AND {_hash("c.after_sha256")} AND typeof(c.reason)='text'
                                  AND ((c.action='CREATE' AND c.before_sha256 IS NULL
                                        AND c.reason=CASE WHEN t.override_reason!=''
                                            THEN t.override_reason ELSE t.note END)
                                       OR (c.action='REVISE' AND {_hash("c.before_sha256")})))))))
          )
        BEGIN SELECT RAISE(ABORT,'INVALID_OR_IMMUTABLE_TRADE_HISTORY_REVIEW'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_trade_history_review_{operation.lower()}
            BEFORE {operation} ON trade_history_reviews
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_TRADE_HISTORY_REVIEW'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) VALUES (?,?,'schema',?,"
            "'MIGRATE_TRADE_HISTORY_REVIEWS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; retain review decisions without inventing trade evidence",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting review history")
