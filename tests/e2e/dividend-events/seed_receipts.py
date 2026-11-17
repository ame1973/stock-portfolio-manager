"""Seed only the exclusive synthetic receipt browser fixture; never use deployment data."""

import os
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from spm_api.audit import audit, stamp
from spm_api.db import make_engine, unit_of_work
from spm_api.ledger_service import post_cash, reverse_cash


def main() -> None:
    if len(sys.argv) != 5 or os.environ.get("APP_ENV") != "test":
        raise RuntimeError("SYNTHETIC_RECEIPT_FIXTURE_REQUIRED")
    event, account, old_revision, current_revision = (str(UUID(item)) for item in sys.argv[1:])
    root = Path(os.environ["SPM_E2E_CREDENTIAL_ROOT"]).resolve(strict=True)
    data = (root / "data").resolve(strict=True)
    database = data / "portfolio.sqlite3"
    if (
        not root.name.startswith("spm-credential-browser-")
        or root.parent != Path(tempfile.gettempdir()).resolve(strict=True)
        or Path(os.environ["DATA_DIR"]) != data
        or Path(os.environ["BACKUP_DIR"]) != root / "backups"
        or data != root / "data"
        or database.is_symlink()
        or not database.is_file()
    ):
        raise RuntimeError("SYNTHETIC_RECEIPT_FIXTURE_REQUIRED")
    engine = make_engine(database)
    try:
        with unit_of_work(engine, write=True) as con:
            if con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() != (
                "0029_receipt_fx_guard"
            ):
                raise RuntimeError("SYNTHETIC_RECEIPT_FIXTURE_REQUIRED")
            scope = con.execute(
                text("""
                    SELECT ctl.owner_id FROM dividend_events e
                    JOIN dividend_event_controls ctl ON ctl.event_id=e.id
                    JOIN securities s ON s.id=e.security_id
                    JOIN accounts a ON a.id=:account
                    JOIN portfolios p ON p.id=a.portfolio_id AND p.owner_id=ctl.owner_id
                    JOIN brokers b ON b.id=a.broker_id AND b.owner_id=ctl.owner_id
                    WHERE e.id=:event AND e.current_revision_id=:current AND e.version=2
                      AND e.provenance_version=1 AND s.display_ticker='RECEIPTE2E'
                      AND a.name='Synthetic receipt read account'
                      AND p.name='Synthetic receipt read portfolio'
                      AND b.name='Synthetic receipt read broker'
                """),
                {"event": event, "account": account, "current": current_revision},
            ).scalar_one()
            revisions = con.execute(
                text(
                    "SELECT id,revision_no,currency FROM dividend_event_revisions "
                    "WHERE event_id=:event ORDER BY revision_no"
                ),
                {"event": event},
            ).all()
            if [tuple(row) for row in revisions] != [
                (old_revision, 1, "USD"),
                (current_revision, 2, "USD"),
            ]:
                raise RuntimeError("SYNTHETIC_RECEIPT_FIXTURE_REQUIRED")
            for table in (
                "dividend_entitlements",
                "dividend_receipts",
                "cash_ledger_entries",
                "dividend_projection_versions",
                "jobs",
                "upstream_attempts",
            ):
                if con.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one() != 0:
                    raise RuntimeError("SYNTHETIC_RECEIPT_FIXTURE_REQUIRED")
            claims = [str(uuid4()), str(uuid4())]
            for claim, revision, expected in zip(
                claims, (old_revision, current_revision), (10_000_000, 11_000_000), strict=True
            ):
                con.execute(
                    text("""
                        INSERT INTO dividend_entitlements(id,revision_id,account_id,
                            eligible_shares_e8,gross_micro,expected_net_micro,currency,
                            quality,projection_version,created_at)
                        VALUES (:id,:revision,:account,1000000000,:expected,:expected,
                            'USD','ACCRUED',0,:created)
                    """),
                    {
                        "id": claim,
                        "revision": revision,
                        "account": account,
                        "expected": expected,
                        "created": stamp(),
                    },
                )
            for index, day, amount, currency, rate, hkd in (
                (0, "2026-11-05", 6_000_000, "USD", 7_800_000_000_000, 46_800_000),
                (1, "2026-11-06", 9_223_372_036_854_775_807, "HKD", None, None),
                (1, "2026-11-07", 2_000_000, "USD", None, None),
            ):
                identifier = str(uuid4())
                con.execute(
                    text("""
                        INSERT INTO dividend_receipts(id,entitlement_id,account_id,received_date,
                            received_micro,currency,tax_micro,fee_micro,fx_rate_hkd_e12,
                            received_hkd_micro,created_at)
                        VALUES (:id,:entitlement,:account,:day,:amount,:currency,
                            :tax,:fee,:rate,:hkd,:created)
                    """),
                    {
                        "id": identifier,
                        "entitlement": claims[index],
                        "account": account,
                        "day": day,
                        "amount": amount,
                        "currency": currency,
                        "tax": 0 if day == "2026-11-05" else None,
                        "fee": None if day == "2026-11-06" else 0,
                        "rate": rate,
                        "hkd": hkd,
                        "created": stamp(),
                    },
                )
                if day == "2026-11-06":
                    continue
                cash_id = post_cash(
                    con,
                    account=account,
                    day=day,
                    currency="USD",
                    amount=amount,
                    fx=rate,
                    booked=hkd,
                    kind="DIVIDEND_RECEIPT",
                    source_type="DIVIDEND_RECEIPT",
                    source_id=identifier,
                )
                if day == "2026-11-07":
                    reason = "Synthetic receipt reversal for private browser history."
                    row = (
                        con.execute(
                            text("SELECT * FROM cash_ledger_entries WHERE id=:id"), {"id": cash_id}
                        )
                        .mappings()
                        .one()
                    )
                    reverse_cash(con, row, reason=reason)
                    audit(
                        con,
                        scope,
                        "dividend_receipt",
                        identifier,
                        "DIVIDEND_RECEIPT_VOIDED",
                        request_id=str(uuid4()),
                        before={"status": "ACTIVE"},
                        after={"status": "VOID"},
                        reason=reason,
                    )
                    con.execute(
                        text(
                            "UPDATE dividend_receipts SET status='VOID',version=2,"
                            "voided_at=:now WHERE id=:id"
                        ),
                        {"now": stamp(), "id": identifier},
                    )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
