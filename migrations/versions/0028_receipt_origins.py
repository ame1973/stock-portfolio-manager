"""Retain explicit receipt cash and FX provenance without adopting historical rows."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0028_receipt_origins"
down_revision = "0027_receipt_unknowns"
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
        CREATE TABLE dividend_receipt_origins (
            receipt_id TEXT NOT NULL PRIMARY KEY REFERENCES dividend_receipts(id) ON DELETE RESTRICT
                CHECK({_uuid("receipt_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            observed_revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id)
                ON DELETE RESTRICT CHECK({_uuid("observed_revision_id")}),
            observed_revision_no INTEGER NOT NULL
                CHECK(typeof(observed_revision_no)='integer'
                      AND observed_revision_no BETWEEN 1 AND 2147483647),
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
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_receipt_origins_scope
        ON dividend_receipt_origins(owner_id,event_id,account_id,receipt_id)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_receipt_origin_insert
        BEFORE INSERT ON dividend_receipt_origins
        WHEN EXISTS(SELECT 1 FROM dividend_receipt_origins old
                    WHERE old.receipt_id=NEW.receipt_id OR old.cash_entry_id=NEW.cash_entry_id
                       OR old.audit_id=NEW.audit_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_receipts receipt
              JOIN dividend_entitlements entitlement ON entitlement.id=receipt.entitlement_id
                   AND entitlement.account_id=receipt.account_id
              JOIN dividend_event_revisions original ON original.id=entitlement.revision_id
                   AND original.event_id=NEW.event_id
              JOIN dividend_events event ON event.id=NEW.event_id AND event.provenance_version=1
              JOIN dividend_event_controls ctl ON ctl.event_id=event.id
                   AND ctl.owner_id=NEW.owner_id
              JOIN owners owner ON owner.id=NEW.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions observed ON observed.id=NEW.observed_revision_id
                   AND observed.event_id=event.id AND observed.revision_no=NEW.observed_revision_no
              JOIN dividend_revision_decisions decision ON decision.revision_id=observed.id
                   AND decision.event_id=event.id AND decision.owner_id=NEW.owner_id
              JOIN audit_events source_audit ON source_audit.entity_type='dividend_event_revision'
                   AND source_audit.entity_id=observed.id
                   AND source_audit.action='DIVIDEND_EVENT_REVISED'
                   AND source_audit.owner_id=NEW.owner_id
                   AND source_audit.request_id=decision.request_id
              JOIN accounts account ON account.id=NEW.account_id AND account.id=receipt.account_id
                   AND account.portfolio_id=NEW.portfolio_id AND account.broker_id=NEW.broker_id
                   AND account.version=NEW.account_version
              JOIN portfolios portfolio ON portfolio.id=NEW.portfolio_id
                   AND portfolio.owner_id=NEW.owner_id AND portfolio.version=NEW.portfolio_version
                   AND portfolio.data_revision=NEW.portfolio_data_revision
              JOIN brokers broker ON broker.id=NEW.broker_id AND broker.owner_id=NEW.owner_id
                   AND broker.version=NEW.broker_version
              JOIN cash_ledger_entries cash ON cash.id=NEW.cash_entry_id
                   AND cash.account_id=receipt.account_id AND cash.source_id=receipt.id
                   AND cash.source_type='DIVIDEND_RECEIPT' AND cash.entry_role='PRIMARY'
                   AND cash.kind='DIVIDEND_RECEIPT' AND cash.external_flow=0
                   AND cash.reverses_entry_id IS NULL AND cash.version=1
                   AND cash.effective_date=receipt.received_date AND cash.currency=receipt.currency
                   AND cash.amount_micro=receipt.received_micro
                   AND cash.fx_rate_hkd_e12 IS receipt.fx_rate_hkd_e12
                   AND cash.booked_hkd_micro IS receipt.received_hkd_micro
              JOIN audit_events receipt_audit ON receipt_audit.id=NEW.audit_id
                   AND receipt_audit.owner_id=NEW.owner_id
                   AND receipt_audit.entity_type='dividend_receipt'
                   AND receipt_audit.entity_id=receipt.id
                   AND receipt_audit.action='DIVIDEND_RECEIPT_RECORDED'
                   AND receipt_audit.request_id=NEW.request_id
              WHERE receipt.id=NEW.receipt_id AND receipt.status='ACTIVE' AND receipt.version=1
                AND receipt.voided_at IS NULL AND event.current_revision_id=observed.id
                AND event.version=observed.revision_no
                AND event.workflow_status=observed.review_status
                AND {_utc("receipt.created_at")} AND {_utc("cash.created_at")}
                AND {_utc("original.created_at")} AND {_utc("entitlement.created_at")}
                AND {_utc("observed.created_at")} AND {_utc("source_audit.created_at")}
                AND {_utc("receipt_audit.created_at")}
                AND source_audit.reason=observed.reason AND {_hash("source_audit.after_sha256")}
                AND original.created_at<=entitlement.created_at
                AND entitlement.created_at<=receipt.created_at
                AND observed.created_at<=source_audit.created_at
                AND source_audit.created_at<=receipt.created_at
                AND receipt.created_at<=cash.created_at AND cash.created_at<=NEW.created_at
                AND receipt_audit.created_at=NEW.created_at
                AND receipt_audit.before_sha256 IS NULL AND {_hash("receipt_audit.after_sha256")}
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=observed.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_receipt' AND a.entity_id=receipt.id
                       AND a.action='DIVIDEND_RECEIPT_RECORDED')=1
                AND NOT EXISTS(SELECT 1 FROM cash_ledger_entries reversal
                               WHERE reversal.reverses_entry_id=cash.id)
                AND COALESCE((
                    (receipt.currency='HKD' AND receipt.fx_rate_hkd_e12=1000000000000
                     AND NEW.fx_source='IDENTITY' AND typeof(receipt.received_hkd_micro)='integer')
                    OR (receipt.currency='USD' AND (
                        (receipt.fx_rate_hkd_e12 IS NULL AND NEW.fx_source IS NULL
                         AND receipt.received_hkd_micro IS NULL AND NEW.actual_hkd_reason IS NULL)
                        OR (typeof(receipt.fx_rate_hkd_e12)='integer' AND receipt.fx_rate_hkd_e12>0
                            AND {_text("NEW.fx_source", 1, 100)} AND NEW.fx_source!='IDENTITY'
                            AND typeof(receipt.received_hkd_micro)='integer')))
                ),0)
          )
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_RECEIPT_ORIGIN'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_receipt_origin_{operation.lower()}
            BEFORE {operation} ON dividend_receipt_origins
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_RECEIPT_ORIGIN'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_RECEIPT_ORIGINS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; do not infer historical receipt provenance",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of removing receipt origins")
