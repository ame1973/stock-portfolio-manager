"""Enforce manual cash direction and one same-owner successor per corrected journal row."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0005_cash"
down_revision = "0004_ledger"
branch_labels = None
depends_on = None

MANUAL_INVALID = """
    {r}.entry_role='PRIMARY' AND {r}.source_type IN ('MANUAL_CASH','CASH_CORRECTION')
    AND NOT (({r}.kind='DEPOSIT' AND {r}.amount_micro>0 AND {r}.external_flow=1)
          OR ({r}.kind='WITHDRAW' AND {r}.amount_micro<0 AND {r}.external_flow=1)
          OR ({r}.kind='ADJUSTMENT' AND {r}.amount_micro!=0 AND {r}.external_flow=0))
"""
CORRECTION_INVALID = """
    {r}.entry_role='PRIMARY' AND {r}.source_type='CASH_CORRECTION' AND NOT EXISTS(
        SELECT 1 FROM cash_ledger_entries p JOIN accounts a ON a.id=p.account_id
        JOIN portfolios old ON old.id=a.portfolio_id
        JOIN accounts b ON b.id={r}.account_id JOIN portfolios current ON current.id=b.portfolio_id
        WHERE p.id={r}.source_id AND p.entry_role='PRIMARY'
          AND p.source_type IN ('MANUAL','MANUAL_CASH','CASH_CORRECTION')
          AND p.kind IN ('DEPOSIT','WITHDRAW','ADJUSTMENT') AND old.owner_id=current.owner_id
          AND EXISTS(SELECT 1 FROM cash_ledger_entries x WHERE x.reverses_entry_id=p.id))
"""


def upgrade() -> None:
    con = op.get_bind()
    checks = (("manual_terms", MANUAL_INVALID), ("correction_source", CORRECTION_INVALID))
    for name, expression in checks:
        if con.exec_driver_sql(
            "SELECT 1 FROM cash_ledger_entries c WHERE " + expression.format(r="c") + " LIMIT 1"
        ).first():
            raise RuntimeError(f"Invalid existing cash {name}; review original journal facts")
    if con.exec_driver_sql("""
        SELECT 1 FROM cash_ledger_entries WHERE source_type='CASH_CORRECTION'
        AND entry_role='PRIMARY' GROUP BY source_id HAVING count(*)>1 LIMIT 1
    """).first():
        raise RuntimeError("An existing cash correction has multiple successors")
    con.exec_driver_sql("""
        CREATE UNIQUE INDEX uq_cash_correction_successor ON cash_ledger_entries(source_id)
        WHERE source_type='CASH_CORRECTION' AND entry_role='PRIMARY'
    """)
    # This index also bounds predecessor/successor lookups in paginated journal reads.
    con.exec_driver_sql(
        "CREATE INDEX idx_cash_source ON cash_ledger_entries(source_type,source_id,entry_role)"
    )
    for name, expression in checks:
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_cash_{name} BEFORE INSERT ON cash_ledger_entries
            WHEN {expression.format(r="NEW")}
            BEGIN SELECT RAISE(ABORT,'INVALID_CASH_{name.upper()}'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,"
            "before_sha256,after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_CASH',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator schema upgrade; cash facts and historical amounts preserved",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of removing cash guards")
