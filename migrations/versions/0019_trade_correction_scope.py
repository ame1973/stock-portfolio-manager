"""Retain immutable, inert scope records for explicitly unverified trade corrections."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0019_trade_correction_scope"
down_revision = "0018_trade_history_reviews"
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
        CREATE TABLE trade_correction_authorizations (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            root_id TEXT NOT NULL REFERENCES trades(id) ON DELETE RESTRICT
                CHECK({_uuid("root_id")}),
            trade_id TEXT NOT NULL UNIQUE REFERENCES trades(id) ON DELETE RESTRICT
                CHECK({_uuid("trade_id")}),
            trade_version INTEGER NOT NULL
                CHECK(typeof(trade_version)='integer' AND trade_version=1),
            operation TEXT NOT NULL CHECK(operation IN ('REVISE','VOID')),
            result_trade_id TEXT NOT NULL REFERENCES trades(id) ON DELETE RESTRICT
                CHECK({_uuid("result_trade_id")}),
            scope_json TEXT NOT NULL CHECK(CASE WHEN typeof(scope_json)='text'
                AND instr(scope_json,char(0))=0 AND json_valid(scope_json)
                THEN json_type(scope_json)='array' AND json_array_length(scope_json)>0 ELSE 0 END),
            intent_sha256 TEXT NOT NULL CHECK({_hash("intent_sha256")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(owner_id,request_id)
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_trade_correction_authorizations_root
        ON trade_correction_authorizations(owner_id,root_id)
    """)
    scope = """(CASE WHEN json_valid(NEW.scope_json) THEN
        CASE WHEN json_type(NEW.scope_json)='array' THEN NEW.scope_json ELSE '[]' END
        ELSE '[]' END)"""
    item = "(CASE WHEN j.type='object' THEN j.value ELSE '{}' END)"
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_trade_correction_authorization_insert
        BEFORE INSERT ON trade_correction_authorizations
        WHEN EXISTS(SELECT 1 FROM trade_correction_authorizations old
                    WHERE old.id=NEW.id OR old.trade_id=NEW.trade_id OR old.audit_id=NEW.audit_id
                       OR (old.owner_id=NEW.owner_id AND old.request_id=NEW.request_id))
          OR NOT EXISTS(
            SELECT 1 FROM trades target
            JOIN portfolios p ON p.id=target.portfolio_id AND p.owner_id=NEW.owner_id
            JOIN accounts ac ON ac.id=target.account_id AND ac.portfolio_id=p.id
            JOIN brokers b ON b.id=ac.broker_id AND b.owner_id=p.owner_id
            JOIN owners o ON o.id=p.owner_id AND o.is_active=1
            JOIN trades result ON result.id=NEW.result_trade_id AND result.root_id=target.root_id
            JOIN portfolios rp ON rp.id=result.portfolio_id AND rp.owner_id=NEW.owner_id
            JOIN accounts ra ON ra.id=result.account_id AND ra.portfolio_id=rp.id
            JOIN brokers rb ON rb.id=ra.broker_id AND rb.owner_id=rp.owner_id
            JOIN audit_events a ON a.id=NEW.audit_id
            JOIN audit_events ta ON ta.entity_type='trade' AND ta.entity_id=NEW.result_trade_id
                 AND ta.action=NEW.operation AND ta.owner_id=NEW.owner_id
                 AND ta.request_id=NEW.request_id
            WHERE target.id=NEW.trade_id AND target.root_id=NEW.root_id AND target.version=2
              AND ((NEW.operation='REVISE' AND target.status='SUPERSEDED'
                    AND result.supersedes_id=target.id AND result.revision=target.revision+1
                    AND result.created_sequence=target.created_sequence AND result.version=1
                    AND result.status IN ('ACTIVE','DRAFT_SCHEDULED','PENDING_FX'))
                   OR (NEW.operation='VOID' AND target.status='VOID' AND result.id=target.id))
              AND a.owner_id=NEW.owner_id AND a.entity_type='trade_correction_authorization'
              AND a.entity_id=NEW.id AND a.action='TRADE_CORRECTION_UNVERIFIED'
              AND a.request_id=NEW.request_id AND a.created_at=NEW.created_at
              AND a.before_sha256 IS NULL AND {_hash("a.after_sha256")}
              AND {_reason("a.reason")} AND ta.reason=a.reason
              AND {_hash("ta.before_sha256")} AND {_hash("ta.after_sha256")}
              AND (SELECT count(*) FROM audit_events x WHERE x.entity_type='trade'
                   AND x.entity_id=NEW.result_trade_id AND x.action=NEW.operation)=1
              AND (SELECT count(*) FROM audit_events x
                   WHERE x.entity_type='trade_correction_authorization'
                     AND x.entity_id=NEW.id AND x.action='TRADE_CORRECTION_UNVERIFIED')=1
          )
          OR EXISTS(
            SELECT 1 FROM json_each({scope}) j WHERE j.type!='object'
              OR (SELECT count(*) FROM json_each({item}))!=5
              OR json_type({item},'$.trade_id')!='text'
              OR json_type({item},'$.trade_version')!='integer'
              OR json_type({item},'$.review_id')!='text'
              OR json_type({item},'$.review_revision')!='text'
              OR json_extract({item},'$.gap') IS NOT 'MISSING_CREATION_AUDIT'
              OR NOT EXISTS(
                SELECT 1 FROM trade_history_reviews r JOIN trades t ON t.id=r.trade_id
                WHERE t.id=json_extract({item},'$.trade_id') AND t.root_id=NEW.root_id
                  AND r.id=json_extract({item},'$.review_id') AND r.owner_id=NEW.owner_id
                  AND r.decision='ACKNOWLEDGE_UNVERIFIED' AND r.gap='MISSING_CREATION_AUDIT'
                  AND printf('%lld',r.revision)=json_extract({item},'$.review_revision')
                  AND r.revision=(SELECT max(x.revision) FROM trade_history_reviews x
                                  WHERE x.trade_id=t.id)
                  AND r.created_at<=NEW.created_at
                  AND ((t.id=NEW.trade_id AND json_extract({item},'$.trade_version')=1)
                       OR (t.id!=NEW.trade_id AND t.status='SUPERSEDED'
                           AND json_extract({item},'$.trade_version')=t.version))
                  AND NOT EXISTS(SELECT 1 FROM audit_events c WHERE c.entity_type='trade'
                      AND c.entity_id=t.id AND c.action=CASE WHEN t.revision=1
                          THEN 'CREATE' ELSE 'REVISE' END)
              )
          )
          OR json_array_length({scope})!=(
            SELECT count(DISTINCT json_extract({item},'$.trade_id')) FROM json_each({scope}) j
          )
          OR json_array_length({scope})!=(
            SELECT count(*) FROM trades t WHERE t.root_id=NEW.root_id AND NOT EXISTS(
              SELECT 1 FROM audit_events c WHERE c.entity_type='trade' AND c.entity_id=t.id
              AND c.action=CASE WHEN t.revision=1 THEN 'CREATE' ELSE 'REVISE' END)
          )
          OR EXISTS(
            SELECT 1 FROM trades t JOIN trade_history_reviews r ON r.trade_id=t.id
            WHERE t.root_id=NEW.root_id AND r.gap='MISSING_CREATION_AUDIT' AND EXISTS(
              SELECT 1 FROM audit_events c WHERE c.entity_type='trade' AND c.entity_id=t.id
              AND c.action=CASE WHEN t.revision=1 THEN 'CREATE' ELSE 'REVISE' END)
          )
        BEGIN SELECT RAISE(ABORT,'INVALID_OR_IMMUTABLE_TRADE_CORRECTION_AUTHORIZATION'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_trade_correction_authorization_{operation.lower()}
            BEFORE {operation} ON trade_correction_authorizations
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_TRADE_CORRECTION_AUTHORIZATION'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) VALUES (?,?,'schema',?,"
            "'MIGRATE_TRADE_CORRECTION_SCOPE',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; retain unverified correction scopes without granting use",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting correction history"
    )
