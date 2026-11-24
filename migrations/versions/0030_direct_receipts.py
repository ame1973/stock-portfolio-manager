"""Retain direct event/account receipt cash without manufacturing entitlements."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0030_direct_receipts"
down_revision = "0029_receipt_fx_guard"
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
        CREATE TABLE dividend_direct_receipts (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT
                CHECK({_uuid("revision_id")}),
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT
                CHECK({_uuid("account_id")}),
            received_date TEXT NOT NULL CHECK({_day("received_date")}),
            received_micro INTEGER NOT NULL
                CHECK(typeof(received_micro)='integer' AND received_micro!=-9223372036854775808),
            currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
            tax_micro INTEGER
                CHECK(tax_micro IS NULL OR (typeof(tax_micro)='integer' AND tax_micro>=0)),
            fee_micro INTEGER
                CHECK(fee_micro IS NULL OR (typeof(fee_micro)='integer' AND fee_micro>=0)),
            fx_rate_hkd_e12 INTEGER,
            received_hkd_micro INTEGER CHECK(received_hkd_micro IS NULL OR
                (typeof(received_hkd_micro)='integer'
                 AND received_hkd_micro!=-9223372036854775808)),
            note TEXT NOT NULL
                CHECK(typeof(note)='text' AND instr(note,char(0))=0 AND length(note)<=1000),
            status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','VOID')),
            version INTEGER NOT NULL DEFAULT 1
                CHECK(typeof(version)='integer' AND version IN (1,2)),
            voided_at TEXT CHECK(voided_at IS NULL OR {_utc("voided_at")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            CHECK(COALESCE((
                (currency='HKD' AND fx_rate_hkd_e12=1000000000000
                 AND received_hkd_micro=received_micro)
                OR (currency='USD' AND (
                    (fx_rate_hkd_e12 IS NULL AND received_hkd_micro IS NULL)
                    OR (typeof(fx_rate_hkd_e12)='integer' AND fx_rate_hkd_e12>0
                        AND typeof(received_hkd_micro)='integer'
                        AND ((received_micro>0 AND received_hkd_micro>=0)
                          OR (received_micro<0 AND received_hkd_micro<=0)
                          OR (received_micro=0 AND received_hkd_micro=0)))))
            ),0))
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_direct_receipts_scope
        ON dividend_direct_receipts(event_id,account_id,received_date,id)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_direct_receipt_insert
        BEFORE INSERT ON dividend_direct_receipts
        WHEN EXISTS(SELECT 1 FROM dividend_direct_receipts WHERE id=NEW.id)
          OR EXISTS(SELECT 1 FROM dividend_receipts WHERE id=NEW.id)
          OR NEW.status IS NOT 'ACTIVE' OR NEW.version IS NOT 1 OR NEW.voided_at IS NOT NULL
          OR NOT EXISTS(
              SELECT 1 FROM dividend_events event
              JOIN dividend_event_controls ctl ON ctl.event_id=event.id
              JOIN owners owner ON owner.id=ctl.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions rev ON rev.id=NEW.revision_id AND rev.event_id=event.id
              JOIN accounts account ON account.id=NEW.account_id AND account.status='ACTIVE'
              JOIN portfolios portfolio ON portfolio.id=account.portfolio_id
                   AND portfolio.owner_id=ctl.owner_id
              JOIN brokers broker ON broker.id=account.broker_id AND broker.owner_id=ctl.owner_id
              WHERE event.id=NEW.event_id AND event.provenance_version=1
                AND event.current_revision_id=rev.id AND event.version=rev.revision_no
                AND event.workflow_status=rev.review_status
                AND {_utc("rev.created_at")} AND rev.created_at<=NEW.created_at)
        BEGIN SELECT RAISE(ABORT,'INVALID_DIRECT_DIVIDEND_RECEIPT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_direct_receipt_legacy_id
        BEFORE INSERT ON dividend_receipts
        WHEN EXISTS(SELECT 1 FROM dividend_direct_receipts WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'DUPLICATE_DIVIDEND_RECEIPT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_direct_dividend_cash_source
        BEFORE INSERT ON cash_ledger_entries
        WHEN NEW.entry_role='PRIMARY' AND NEW.source_type='DIVIDEND_DIRECT_RECEIPT'
          AND NOT EXISTS(
              SELECT 1 FROM dividend_direct_receipts receipt
              WHERE receipt.id=NEW.source_id AND receipt.status='ACTIVE'
                AND receipt.account_id=NEW.account_id AND receipt.currency=NEW.currency
                AND receipt.received_date=NEW.effective_date AND NEW.kind='DIVIDEND_RECEIPT'
                AND NEW.external_flow=0 AND NEW.reverses_entry_id IS NULL
                AND NEW.amount_micro=receipt.received_micro
                AND NEW.booked_hkd_micro IS receipt.received_hkd_micro
                AND NEW.fx_rate_hkd_e12 IS receipt.fx_rate_hkd_e12
                AND NEW.note=receipt.note)
        BEGIN SELECT RAISE(ABORT,'CASH_SOURCE_MISMATCH'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_direct_receipt_origins (
            receipt_id TEXT NOT NULL PRIMARY KEY REFERENCES dividend_direct_receipts(id)
                ON DELETE RESTRICT CHECK({_uuid("receipt_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT
                CHECK({_uuid("revision_id")}),
            revision_no INTEGER NOT NULL
                CHECK(typeof(revision_no)='integer' AND revision_no BETWEEN 1 AND 2147483647),
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT
                CHECK({_uuid("account_id")}),
            portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT
                CHECK({_uuid("portfolio_id")}),
            broker_id TEXT NOT NULL REFERENCES brokers(id) ON DELETE RESTRICT
                CHECK({_uuid("broker_id")}),
            account_version INTEGER NOT NULL
                CHECK(typeof(account_version)='integer' AND account_version>0),
            portfolio_version INTEGER NOT NULL
                CHECK(typeof(portfolio_version)='integer' AND portfolio_version>0),
            broker_version INTEGER NOT NULL
                CHECK(typeof(broker_version)='integer' AND broker_version>0),
            portfolio_data_revision INTEGER NOT NULL
                CHECK(typeof(portfolio_data_revision)='integer' AND portfolio_data_revision>=0),
            cash_entry_id TEXT NOT NULL UNIQUE REFERENCES cash_ledger_entries(id) ON DELETE RESTRICT
                CHECK({_uuid("cash_entry_id")}),
            fx_source TEXT CHECK(fx_source IS NULL OR {_text("fx_source", 1, 100)}),
            actual_hkd_reason TEXT
                CHECK(actual_hkd_reason IS NULL OR {_text("actual_hkd_reason", 10, 1000)}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(owner_id,request_id)
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_direct_receipt_origins_scope
        ON dividend_direct_receipt_origins(event_id,account_id,receipt_id)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_direct_origin_insert
        BEFORE INSERT ON dividend_direct_receipt_origins
        WHEN EXISTS(SELECT 1 FROM dividend_direct_receipt_origins old
                    WHERE old.receipt_id=NEW.receipt_id OR old.cash_entry_id=NEW.cash_entry_id
                       OR old.audit_id=NEW.audit_id
                       OR (old.owner_id=NEW.owner_id AND old.request_id=NEW.request_id))
          OR EXISTS(SELECT 1 FROM dividend_receipt_origins old
                    WHERE old.owner_id=NEW.owner_id AND old.request_id=NEW.request_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_direct_receipts receipt
              JOIN dividend_events event ON event.id=receipt.event_id AND event.id=NEW.event_id
                   AND event.provenance_version=1
              JOIN dividend_event_controls ctl ON ctl.event_id=event.id
                   AND ctl.owner_id=NEW.owner_id
              JOIN owners owner ON owner.id=ctl.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions rev ON rev.id=receipt.revision_id
                   AND rev.id=NEW.revision_id AND rev.revision_no=NEW.revision_no
                   AND rev.event_id=event.id AND event.current_revision_id=rev.id
                   AND event.version=rev.revision_no AND event.workflow_status=rev.review_status
              JOIN dividend_revision_decisions decision ON decision.revision_id=rev.id
                   AND decision.event_id=event.id AND decision.owner_id=NEW.owner_id
              JOIN audit_events source_audit ON source_audit.entity_type='dividend_event_revision'
                   AND source_audit.entity_id=rev.id
                   AND source_audit.action='DIVIDEND_EVENT_REVISED'
                   AND source_audit.owner_id=NEW.owner_id
                   AND source_audit.request_id=decision.request_id
              JOIN accounts account ON account.id=receipt.account_id AND account.id=NEW.account_id
                   AND account.status='ACTIVE' AND account.portfolio_id=NEW.portfolio_id
                   AND account.broker_id=NEW.broker_id AND account.version=NEW.account_version
              JOIN portfolios portfolio ON portfolio.id=NEW.portfolio_id
                   AND portfolio.owner_id=NEW.owner_id AND portfolio.version=NEW.portfolio_version
                   AND portfolio.data_revision=NEW.portfolio_data_revision
              JOIN brokers broker ON broker.id=NEW.broker_id AND broker.owner_id=NEW.owner_id
                   AND broker.version=NEW.broker_version
              JOIN cash_ledger_entries cash ON cash.id=NEW.cash_entry_id
                   AND cash.account_id=receipt.account_id AND cash.source_id=receipt.id
                   AND cash.source_type='DIVIDEND_DIRECT_RECEIPT' AND cash.entry_role='PRIMARY'
                   AND cash.kind='DIVIDEND_RECEIPT' AND cash.external_flow=0
                   AND cash.reverses_entry_id IS NULL AND cash.version=1
                   AND cash.effective_date=receipt.received_date AND cash.currency=receipt.currency
                   AND cash.amount_micro=receipt.received_micro AND cash.note=receipt.note
                   AND cash.fx_rate_hkd_e12 IS receipt.fx_rate_hkd_e12
                   AND cash.booked_hkd_micro IS receipt.received_hkd_micro
              JOIN audit_events receipt_audit ON receipt_audit.id=NEW.audit_id
                   AND receipt_audit.owner_id=NEW.owner_id
                   AND receipt_audit.entity_type='dividend_direct_receipt'
                   AND receipt_audit.entity_id=receipt.id
                   AND receipt_audit.action='DIVIDEND_DIRECT_RECEIPT_RECORDED'
                   AND receipt_audit.request_id=NEW.request_id
              WHERE receipt.id=NEW.receipt_id AND receipt.status='ACTIVE' AND receipt.version=1
                AND receipt.voided_at IS NULL AND {_utc("cash.created_at")}
                AND {_utc("source_audit.created_at")} AND {_utc("receipt_audit.created_at")}
                AND source_audit.reason=rev.reason AND {_hash("source_audit.after_sha256")}
                AND rev.created_at<=source_audit.created_at
                AND source_audit.created_at<=receipt.created_at
                AND receipt.created_at<=cash.created_at AND cash.created_at<=NEW.created_at
                AND receipt_audit.created_at=NEW.created_at AND receipt_audit.before_sha256 IS NULL
                AND {_hash("receipt_audit.after_sha256")}
                AND {_text("receipt_audit.reason", 10, 1000)}
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=rev.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_direct_receipt' AND a.entity_id=receipt.id
                       AND a.action='DIVIDEND_DIRECT_RECEIPT_RECORDED')=1
                AND NOT EXISTS(SELECT 1 FROM cash_ledger_entries reversal
                               WHERE reversal.reverses_entry_id=cash.id)
                AND COALESCE((
                    (receipt.currency='HKD' AND NEW.fx_source='IDENTITY')
                    OR (receipt.currency='USD' AND (
                        (receipt.fx_rate_hkd_e12 IS NULL AND NEW.fx_source IS NULL
                         AND NEW.actual_hkd_reason IS NULL)
                        OR (receipt.fx_rate_hkd_e12 IS NOT NULL
                            AND {_text("NEW.fx_source", 1, 100)} AND NEW.fx_source!='IDENTITY')))
                ),0))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIRECT_DIVIDEND_RECEIPT_ORIGIN'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_legacy_receipt_origin_direct_request
        BEFORE INSERT ON dividend_receipt_origins
        WHEN EXISTS(SELECT 1 FROM dividend_direct_receipt_origins old
                    WHERE old.owner_id=NEW.owner_id AND old.request_id=NEW.request_id)
        BEGIN SELECT RAISE(ABORT,'DUPLICATE_DIVIDEND_RECEIPT_REQUEST'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_direct_origin_{operation.lower()}
            BEFORE {operation} ON dividend_direct_receipt_origins
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIRECT_DIVIDEND_ORIGIN'); END
        """)
    columns = (
        "id",
        "event_id",
        "revision_id",
        "account_id",
        "received_date",
        "received_micro",
        "currency",
        "tax_micro",
        "fee_micro",
        "fx_rate_hkd_e12",
        "received_hkd_micro",
        "note",
        "created_at",
    )
    changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in columns)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_direct_receipt_update
        BEFORE UPDATE ON dividend_direct_receipts
        WHEN {changed} OR OLD.status IS NOT 'ACTIVE' OR OLD.version IS NOT 1
          OR NEW.status IS NOT 'VOID' OR NEW.version IS NOT 2 OR NOT {_utc("NEW.voided_at")}
          OR NEW.voided_at<OLD.created_at OR NOT EXISTS(
              SELECT 1 FROM dividend_direct_receipt_origins origin
              JOIN cash_ledger_entries original ON original.id=origin.cash_entry_id
              JOIN cash_ledger_entries reversal ON reversal.reverses_entry_id=original.id
              JOIN audit_events a ON a.entity_type='dividend_direct_receipt' AND a.entity_id=OLD.id
                   AND a.action='DIVIDEND_DIRECT_RECEIPT_VOIDED' AND a.owner_id=origin.owner_id
              WHERE origin.receipt_id=OLD.id AND original.source_type='DIVIDEND_DIRECT_RECEIPT'
                AND original.source_id=OLD.id AND original.entry_role='PRIMARY'
                AND original.version=2 AND original.account_id=OLD.account_id
                AND original.currency=OLD.currency
                AND original.effective_date=OLD.received_date AND original.kind='DIVIDEND_RECEIPT'
                AND original.external_flow=0 AND original.amount_micro=OLD.received_micro
                AND original.fx_rate_hkd_e12 IS OLD.fx_rate_hkd_e12
                AND original.booked_hkd_micro IS OLD.received_hkd_micro
                AND reversal.entry_role='REVERSAL' AND reversal.kind='REVERSAL'
                AND reversal.version=1 AND {_utc("reversal.created_at")}
                AND reversal.created_at>=origin.created_at
                AND reversal.source_type=original.source_type
                AND reversal.source_id=original.source_id
                AND reversal.account_id=original.account_id AND reversal.currency=original.currency
                AND reversal.effective_date=original.effective_date
                AND reversal.external_flow=original.external_flow
                AND reversal.amount_micro=-original.amount_micro
                AND reversal.fx_rate_hkd_e12 IS original.fx_rate_hkd_e12
                AND reversal.booked_hkd_micro IS -original.booked_hkd_micro
                AND a.reason=reversal.note AND {_text("a.reason", 10, 1000)}
                AND {_uuid("a.request_id")} AND {_hash("a.before_sha256")}
                AND {_hash("a.after_sha256")}
                AND {_utc("a.created_at")} AND a.created_at>=reversal.created_at
                AND NEW.voided_at>=a.created_at
                AND (SELECT count(*) FROM audit_events other
                     WHERE other.entity_type='dividend_direct_receipt' AND other.entity_id=OLD.id
                       AND other.action='DIVIDEND_DIRECT_RECEIPT_VOIDED')=1)
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIRECT_DIVIDEND_RECEIPT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_direct_receipt_delete
        BEFORE DELETE ON dividend_direct_receipts
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIRECT_DIVIDEND_RECEIPT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_direct_receipt_blocks_revision
        BEFORE UPDATE OF current_revision_id ON dividend_events
        WHEN NEW.current_revision_id IS NOT OLD.current_revision_id
          AND (EXISTS(SELECT 1 FROM dividend_direct_receipts WHERE event_id=OLD.id)
            OR EXISTS(SELECT 1 FROM dividend_direct_receipt_origins WHERE event_id=OLD.id))
        BEGIN SELECT RAISE(ABORT,'DIRECT_RECEIPT_RECONCILIATION_REQUIRED'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_direct_receipt_blocks_rejection
        BEFORE INSERT ON dividend_event_rejections
        WHEN EXISTS(SELECT 1 FROM dividend_direct_receipts WHERE event_id=NEW.event_id)
          OR EXISTS(SELECT 1 FROM dividend_direct_receipt_origins WHERE event_id=NEW.event_id)
        BEGIN SELECT RAISE(ABORT,'DIRECT_RECEIPT_RECONCILIATION_REQUIRED'); END
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIRECT_DIVIDEND_RECEIPTS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; do not infer receipt payment or eligibility",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting receipt history")
