"""Guard corporate-action terms and dated opening-cash correction lineage."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0007_actions_opening"
down_revision = "0006_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    con = op.get_bind()
    if con.exec_driver_sql("""
        SELECT 1 FROM corporate_actions WHERE is_current=1 AND status!='VOID'
        GROUP BY security_id,action_type,effective_date HAVING count(*)>1 LIMIT 1
    """).first():
        raise RuntimeError("Ambiguous retained corporate-action identities need explicit review")
    con.exec_driver_sql("DROP TRIGGER trg_ledger_corporate_actions_immutable")
    for name, definition in (
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("note", "TEXT NOT NULL DEFAULT ''"),
        ("previous_symbol", "TEXT"),
    ):
        con.exec_driver_sql(f"ALTER TABLE corporate_actions ADD COLUMN {name} {definition}")
    columns = [row[1] for row in con.exec_driver_sql("PRAGMA table_info(corporate_actions)")]
    changed = " OR ".join(
        f'NEW."{name}" IS NOT OLD."{name}"'
        for name in columns
        if name not in {"status", "version", "is_current"}
    )
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_ledger_corporate_actions_immutable BEFORE UPDATE ON corporate_actions
        WHEN ({changed}) OR NEW.version!=OLD.version+1
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FINANCIAL_FACT'); END
    """)
    con.exec_driver_sql("DROP TRIGGER trg_ledger_action_revision")
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_action_revision BEFORE INSERT ON corporate_actions
        WHEN NEW.root_id IS NULL OR NEW.version!=1 OR NEW.is_current!=1 OR NEW.status='VOID'
          OR typeof(NEW.revision)!='integer'
          OR (NEW.supersedes_id IS NULL AND (NEW.root_id!=NEW.id OR NEW.revision!=1))
          OR (NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM corporate_actions p WHERE p.id=NEW.supersedes_id
                AND p.root_id=NEW.root_id AND p.created_sequence=NEW.created_sequence
                AND p.security_id=NEW.security_id AND p.is_current=0
                AND p.revision+1=NEW.revision AND p.status!='VOID'))
          OR (NEW.ratio_num IS NOT NULL AND typeof(NEW.ratio_num)!='integer')
          OR (NEW.ratio_den IS NOT NULL AND typeof(NEW.ratio_den)!='integer')
        BEGIN SELECT RAISE(ABORT,'INVALID_ACTION_REVISION_OR_RATIO'); END
    """)
    con.exec_driver_sql("""
        CREATE UNIQUE INDEX uq_action_identity_current
        ON corporate_actions(security_id,action_type,effective_date)
        WHERE is_current=1 AND status!='VOID'
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_action_supported_terms BEFORE INSERT ON corporate_actions
        WHEN (NEW.ratio_num IS NULL)!=(NEW.ratio_den IS NULL)
          OR (NEW.action_type!='SYMBOL_CHANGE'
              AND (NEW.new_symbol IS NOT NULL OR NEW.previous_symbol IS NOT NULL))
          OR (NEW.action_type='SPLIT' AND NEW.ratio_num<=NEW.ratio_den)
          OR (NEW.action_type='REVERSE_SPLIT' AND NEW.ratio_num>=NEW.ratio_den)
          OR (NEW.action_type='SYMBOL_CHANGE' AND (
                NEW.new_symbol IS NULL OR length(trim(NEW.new_symbol))=0
                OR NEW.ratio_num IS NOT NULL OR NEW.ratio_den IS NOT NULL
                OR (NEW.status='CONFIRMED' AND NEW.previous_symbol IS NULL)))
          OR (NEW.action_type IN ('CASH_IN_LIEU','MERGER','SPIN_OFF') AND NEW.status='CONFIRMED')
        BEGIN SELECT RAISE(ABORT,'UNALLOCATED_OR_INVALID_ACTION_TERMS'); END
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_reviews_action_root
        ON review_items(json_extract(detail_json,'$.action_root_id'),status)
    """)
    con.exec_driver_sql("""
        CREATE UNIQUE INDEX uq_opening_correction_successor ON cash_ledger_entries(source_id)
        WHERE source_type='OPENING_BALANCE_CORRECTION' AND entry_role='PRIMARY'
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_opening_correction_source BEFORE INSERT ON cash_ledger_entries
        WHEN NEW.source_type='OPENING_BALANCE_CORRECTION' AND NEW.entry_role='PRIMARY'
        AND NOT EXISTS(
            SELECT 1 FROM cash_ledger_entries p JOIN accounts a ON a.id=NEW.account_id
            WHERE p.id=NEW.source_id AND p.account_id=NEW.account_id AND p.entry_role='PRIMARY'
              AND p.source_type IN ('OPENING_BALANCE','OPENING_BALANCE_CORRECTION')
              AND EXISTS(SELECT 1 FROM cash_ledger_entries r WHERE r.reverses_entry_id=p.id)
              AND NEW.currency=a.default_currency AND NEW.effective_date=a.opening_cash_date
              AND NEW.amount_micro=a.opening_cash_micro AND NEW.kind='ADJUSTMENT'
              AND NEW.external_flow=0 AND NEW.fx_rate_hkd_e12 IS NOT NULL
              AND NEW.booked_hkd_micro IS NOT NULL
              AND (NEW.currency!='HKD' OR NEW.booked_hkd_micro=NEW.amount_micro))
        BEGIN SELECT RAISE(ABORT,'INVALID_OPENING_CORRECTION_SOURCE'); END
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,"
            "before_sha256,after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_ACTIONS_OPENING',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator schema upgrade; original actions and opening cash preserved",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of removing action/opening guards"
    )
