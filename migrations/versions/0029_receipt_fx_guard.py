"""Fence impossible receipt HKD values without rewriting existing origin history."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0029_receipt_fx_guard"
down_revision = "0028_receipt_origins"
branch_labels = None
depends_on = None


def _valid(receipt: str) -> str:
    return f"""COALESCE((
        typeof({receipt}.received_micro)='integer' AND (
          ({receipt}.currency='HKD' AND {receipt}.fx_rate_hkd_e12=1000000000000
           AND typeof({receipt}.received_hkd_micro)='integer'
           AND {receipt}.received_hkd_micro={receipt}.received_micro)
          OR ({receipt}.currency='USD' AND (
            ({receipt}.fx_rate_hkd_e12 IS NULL AND {receipt}.received_hkd_micro IS NULL)
            OR (typeof({receipt}.fx_rate_hkd_e12)='integer' AND {receipt}.fx_rate_hkd_e12>0
                AND typeof({receipt}.received_hkd_micro)='integer'
                AND (({receipt}.received_micro>0 AND {receipt}.received_hkd_micro>=0)
                  OR ({receipt}.received_micro<0 AND {receipt}.received_hkd_micro<=0)
                  OR ({receipt}.received_micro=0 AND {receipt}.received_hkd_micro=0)))))
        )
    ),0)"""


def upgrade() -> None:
    con = op.get_bind()
    invalid = con.exec_driver_sql(f"""
        SELECT 1 FROM dividend_receipt_origins origin
        LEFT JOIN dividend_receipts receipt ON receipt.id=origin.receipt_id
        WHERE NOT {_valid("receipt")} LIMIT 1
    """).first()
    if invalid is not None:
        raise RuntimeError(
            "Existing receipt origin has an inconsistent HKD value; migration refused"
        )
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_receipt_origin_fx_guard
        BEFORE INSERT ON dividend_receipt_origins
        WHEN NOT EXISTS(SELECT 1 FROM dividend_receipts receipt
                        WHERE receipt.id=NEW.receipt_id AND {_valid("receipt")})
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_RECEIPT_ORIGIN_FX'); END
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_RECEIPT_FX_GUARD',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; retain valid receipt identity and sign relationships",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of removing the receipt guard"
    )
