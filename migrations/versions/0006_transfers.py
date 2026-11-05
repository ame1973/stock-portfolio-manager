"""Add atomic paired cash facts and stable immutable security-transfer revision roots."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op
from sqlalchemy import Connection

revision = "0006_transfers"
down_revision = "0005_cash"
branch_labels = None
depends_on = None


def _immutable(con: Connection, table: str, mutable: set[str]) -> None:
    columns = [row[1] for row in con.exec_driver_sql(f"PRAGMA table_info({table})")]
    changed = " OR ".join(
        f'NEW."{name}" IS NOT OLD."{name}"' for name in columns if name not in mutable
    )
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_ledger_{table}_immutable BEFORE UPDATE ON {table}
        WHEN ({changed}) OR NEW.version!=OLD.version+1
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FINANCIAL_FACT'); END
    """)


def _security_history(con: Connection) -> None:
    if con.exec_driver_sql("""
        SELECT 1 FROM security_transfers s
        LEFT JOIN ledger_sequences q ON q.sequence=s.created_sequence
        WHERE q.kind IS NOT 'TRANSFER' OR q.fact_id IS NOT s.id LIMIT 1
    """).first():
        raise RuntimeError("A retained security transfer has invalid replay provenance")
    for name in ("immutable", "state", "sequence"):
        con.exec_driver_sql(f"DROP TRIGGER trg_ledger_security_transfers_{name}")
    con.exec_driver_sql(
        "ALTER TABLE security_transfers ADD COLUMN root_id TEXT "
        "REFERENCES security_transfers(id) ON DELETE RESTRICT"
    )
    con.exec_driver_sql(
        "ALTER TABLE security_transfers ADD COLUMN supersedes_id TEXT "
        "REFERENCES security_transfers(id) ON DELETE RESTRICT"
    )
    con.exec_driver_sql(
        "ALTER TABLE security_transfers ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 "
        "CHECK(typeof(revision)='integer' AND revision>=1)"
    )
    con.exec_driver_sql(
        "ALTER TABLE security_transfers ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1 "
        "CHECK(typeof(is_current)='integer' AND is_current IN (0,1))"
    )
    con.exec_driver_sql("UPDATE security_transfers SET root_id=id")
    con.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_security_transfer_current ON security_transfers(root_id) "
        "WHERE is_current=1"
    )
    con.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_security_transfer_predecessor ON security_transfers(supersedes_id) "
        "WHERE supersedes_id IS NOT NULL"
    )
    _immutable(con, "security_transfers", {"status", "version", "is_current"})
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_security_transfers_sequence BEFORE INSERT ON security_transfers
        WHEN typeof(NEW.created_sequence)!='integer' OR NOT EXISTS(
            SELECT 1 FROM ledger_sequences WHERE sequence=NEW.created_sequence
            AND kind='TRANSFER' AND fact_id=NEW.root_id)
        BEGIN SELECT RAISE(ABORT,'INVALID_LEDGER_SEQUENCE'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_security_transfer_revision BEFORE INSERT ON security_transfers
        WHEN NEW.root_id IS NULL OR NEW.is_current!=1 OR NEW.status!='ACTIVE' OR NEW.version!=1 OR
            (NEW.supersedes_id IS NULL AND (NEW.root_id!=NEW.id OR NEW.revision!=1)) OR
            (NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM security_transfers p JOIN portfolios old ON old.id=p.portfolio_id
                JOIN portfolios current ON current.id=NEW.portfolio_id
                WHERE p.id=NEW.supersedes_id AND p.root_id=NEW.root_id AND p.is_current=0
                AND p.status='ACTIVE' AND p.created_sequence=NEW.created_sequence
                AND NEW.revision=p.revision+1 AND old.owner_id=current.owner_id))
        BEGIN SELECT RAISE(ABORT,'INVALID_SECURITY_TRANSFER_REVISION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_security_transfers_state BEFORE UPDATE ON security_transfers
        WHEN NOT (OLD.is_current=1 AND OLD.status='ACTIVE' AND (
            (NEW.is_current=0 AND NEW.status=OLD.status) OR
            (NEW.is_current=1 AND NEW.status='VOID')))
        BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_STATE_TRANSITION'); END
    """)


def _cash_facts(con: Connection) -> None:
    con.exec_driver_sql("""
        CREATE TABLE cash_transfers(
            id TEXT PRIMARY KEY,
            root_id TEXT NOT NULL REFERENCES cash_transfers(id) ON DELETE RESTRICT,
            supersedes_id TEXT REFERENCES cash_transfers(id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision>=1),
            version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
            portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT,
            source_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            transfer_date TEXT NOT NULL,
            source_currency TEXT NOT NULL CHECK(source_currency IN ('HKD','USD')),
            target_currency TEXT NOT NULL CHECK(target_currency IN ('HKD','USD')),
            source_amount_micro INTEGER NOT NULL
                CHECK(typeof(source_amount_micro)='integer' AND source_amount_micro>0),
            target_amount_micro INTEGER NOT NULL
                CHECK(typeof(target_amount_micro)='integer' AND target_amount_micro>0),
            fx_rate_hkd_e12 INTEGER CHECK(fx_rate_hkd_e12 IS NULL OR
                (typeof(fx_rate_hkd_e12)='integer' AND fx_rate_hkd_e12>0)),
            actual_hkd_micro INTEGER CHECK(actual_hkd_micro IS NULL OR
                (typeof(actual_hkd_micro)='integer' AND actual_hkd_micro>0)),
            source_booked_hkd_micro INTEGER CHECK(source_booked_hkd_micro IS NULL OR
                (typeof(source_booked_hkd_micro)='integer' AND source_booked_hkd_micro>=0)),
            target_booked_hkd_micro INTEGER CHECK(target_booked_hkd_micro IS NULL OR
                (typeof(target_booked_hkd_micro)='integer' AND target_booked_hkd_micro>=0)),
            status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUPERSEDED','VOID')),
            reason TEXT NOT NULL CHECK(length(trim(reason))>=10 AND length(reason)<=1000),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(source_account_id!=target_account_id OR source_currency!=target_currency)
        )
    """)
    con.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_cash_transfer_current ON cash_transfers(root_id) "
        "WHERE status!='SUPERSEDED'"
    )
    con.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_cash_transfer_predecessor ON cash_transfers(supersedes_id) "
        "WHERE supersedes_id IS NOT NULL"
    )
    con.exec_driver_sql(
        "CREATE INDEX idx_cash_transfer_date ON cash_transfers(portfolio_id,transfer_date,id)"
    )
    _immutable(con, "cash_transfers", {"status", "version", "updated_at"})
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_transfer_no_delete BEFORE DELETE ON cash_transfers
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FINANCIAL_FACT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_transfer_state BEFORE UPDATE ON cash_transfers
        WHEN OLD.status!='ACTIVE' OR NEW.status NOT IN ('SUPERSEDED','VOID')
        BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_STATE_TRANSITION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_transfer_graph BEFORE INSERT ON cash_transfers
        WHEN NEW.portfolio_id IS NOT (
                SELECT portfolio_id FROM accounts WHERE id=NEW.source_account_id)
          OR NEW.portfolio_id IS NOT (
                SELECT portfolio_id FROM accounts WHERE id=NEW.target_account_id)
        BEGIN SELECT RAISE(ABORT,'TRANSFER_ACCOUNT_PORTFOLIO_MISMATCH'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_transfer_revision BEFORE INSERT ON cash_transfers
        WHEN NEW.status!='ACTIVE' OR NEW.version!=1 OR
             (NEW.supersedes_id IS NULL AND (NEW.root_id!=NEW.id OR NEW.revision!=1)) OR
             (NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM cash_transfers p JOIN portfolios old ON old.id=p.portfolio_id
                JOIN portfolios current ON current.id=NEW.portfolio_id
                WHERE p.id=NEW.supersedes_id AND p.root_id=NEW.root_id AND p.status='SUPERSEDED'
                AND NEW.revision=p.revision+1 AND old.owner_id=current.owner_id))
        BEGIN SELECT RAISE(ABORT,'INVALID_CASH_TRANSFER_REVISION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_transfer_date BEFORE INSERT ON cash_transfers
        WHEN length(NEW.transfer_date)!=10 OR NEW.transfer_date NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            OR substr(NEW.transfer_date,1,4)='0000'
            OR date(NEW.transfer_date,'+0 days') IS NOT NEW.transfer_date
        BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_DATE'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_transfer_terms BEFORE INSERT ON cash_transfers
        WHEN (NEW.source_currency=NEW.target_currency AND (
                NEW.source_amount_micro!=NEW.target_amount_micro OR
                NEW.source_booked_hkd_micro IS NOT NEW.target_booked_hkd_micro))
          OR (NEW.source_currency='HKD'
              AND NEW.source_booked_hkd_micro IS NOT NEW.source_amount_micro)
          OR (NEW.target_currency='HKD'
              AND NEW.target_booked_hkd_micro IS NOT NEW.target_amount_micro)
          OR (NEW.source_currency='HKD' AND NEW.target_currency='HKD'
              AND NEW.fx_rate_hkd_e12 IS NOT 1000000000000)
          OR (NEW.source_currency='USD' AND (
                (NEW.fx_rate_hkd_e12 IS NULL AND NEW.source_booked_hkd_micro IS NOT NULL) OR
                (NEW.fx_rate_hkd_e12 IS NOT NULL AND NEW.source_booked_hkd_micro IS NULL)))
          OR (NEW.target_currency='USD' AND (
                (NEW.fx_rate_hkd_e12 IS NULL AND NEW.target_booked_hkd_micro IS NOT NULL) OR
                (NEW.fx_rate_hkd_e12 IS NOT NULL AND NEW.target_booked_hkd_micro IS NULL)))
          OR (NEW.actual_hkd_micro IS NOT NULL AND (
                NEW.fx_rate_hkd_e12 IS NULL OR
                (NEW.source_currency='HKD' AND NEW.target_currency='HKD') OR
                (NEW.source_currency='USD'
                    AND NEW.source_booked_hkd_micro IS NOT NEW.actual_hkd_micro) OR
                (NEW.target_currency='USD'
                    AND NEW.target_booked_hkd_micro IS NOT NEW.actual_hkd_micro)))
        BEGIN SELECT RAISE(ABORT,'INVALID_CASH_TRANSFER_TERMS'); END
    """)
    for suffix, sign, kind in (("OUT", "-", "TRANSFER_OUT"), ("IN", "", "TRANSFER_IN")):
        leg = "source" if suffix == "OUT" else "target"
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_cash_transfer_{suffix.lower()} BEFORE INSERT ON cash_ledger_entries
            WHEN NEW.entry_role='PRIMARY' AND NEW.source_type='CASH_TRANSFER_{suffix}'
            AND NOT EXISTS(
                SELECT 1 FROM cash_transfers p WHERE p.id=NEW.source_id AND p.status='ACTIVE'
                AND p.{leg}_account_id=NEW.account_id AND p.transfer_date=NEW.effective_date
                AND p.{leg}_currency=NEW.currency AND NEW.kind='{kind}' AND NEW.external_flow=0
                AND NEW.amount_micro={sign}p.{leg}_amount_micro
                AND NEW.booked_hkd_micro IS {sign}p.{leg}_booked_hkd_micro
                AND NEW.fx_rate_hkd_e12 IS CASE NEW.currency WHEN 'HKD' THEN 1000000000000
                                              ELSE p.fx_rate_hkd_e12 END)
            BEGIN SELECT RAISE(ABORT,'CASH_SOURCE_MISMATCH'); END
        """)


def upgrade() -> None:
    con = op.get_bind()
    _security_history(con)
    _cash_facts(con)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,"
            "before_sha256,after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_TRANSFERS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator schema upgrade; original security/cash amounts and order preserved",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of removing transfer guards")
