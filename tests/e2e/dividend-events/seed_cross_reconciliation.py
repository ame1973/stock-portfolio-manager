"""Seed an exclusive synthetic cross-store comparison, never deployment data."""

import os
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from spm_api.audit import audit, now, stamp
from spm_api.db import make_engine, unit_of_work
from spm_api.dividend_direct_receipt_history import _creation_payload
from spm_api.ledger_service import post_cash

ERROR = "SYNTHETIC_RECONCILIATION_FIXTURE_REQUIRED"


def main() -> None:
    if (
        len(sys.argv) != 5
        or os.environ.get("APP_ENV") != "test"
        or os.environ.get("SPM_E2E_COMPOSE_PROJECT") != "spm-e2e-credential-browser-fixture"
    ):
        raise RuntimeError(ERROR)
    event, account, old_revision, current_revision = (str(UUID(item)) for item in sys.argv[1:])
    root = Path(os.environ["SPM_E2E_CREDENTIAL_ROOT"]).resolve(strict=True)
    data = (root / "data").resolve(strict=True)
    database = data / "portfolio.sqlite3"
    if (
        not root.name.startswith("spm-credential-browser-")
        or root.parent != Path(tempfile.gettempdir()).resolve(strict=True)
        or Path(os.environ["SPM_E2E_CREDENTIAL_ROOT"]) != root
        or Path(os.environ["DATA_DIR"]) != data
        or Path(os.environ["BACKUP_DIR"]) != root / "backups"
        or data != root / "data"
        or database.is_symlink()
        or not database.is_file()
    ):
        raise RuntimeError(ERROR)
    engine = make_engine(database)
    try:
        with unit_of_work(engine, write=True) as con:
            if con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() != (
                "0031_dividend_rebuild"
            ):
                raise RuntimeError(ERROR)
            scope = (
                con.execute(
                    text("""
                        SELECT ctl.owner_id,a.portfolio_id,a.broker_id,a.version AS account_version,
                               p.version AS portfolio_version,p.data_revision,
                               b.version AS broker_version
                        FROM dividend_events e
                        JOIN dividend_event_controls ctl ON ctl.event_id=e.id
                        JOIN securities s ON s.id=e.security_id
                        JOIN accounts a ON a.id=:account
                        JOIN portfolios p ON p.id=a.portfolio_id AND p.owner_id=ctl.owner_id
                        JOIN brokers b ON b.id=a.broker_id AND b.owner_id=ctl.owner_id
                        WHERE e.id=:event AND e.current_revision_id=:current AND e.version=2
                          AND e.provenance_version=1 AND s.display_ticker='CROSSREC'
                          AND a.name='Synthetic cross reconciliation account'
                          AND p.name='Synthetic cross reconciliation portfolio'
                          AND b.name='Synthetic cross reconciliation broker'
                    """),
                    {"event": event, "account": account, "current": current_revision},
                )
                .mappings()
                .one()
            )
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
                raise RuntimeError(ERROR)
            projection = con.execute(
                text("""
                    SELECT event_id,revision_id,account_id,state,expected_net_micro,currency
                    FROM dividend_projection_versions
                """)
            ).one()
            if tuple(projection) != (
                event,
                current_revision,
                account,
                "EXPECTED",
                11_000_000,
                "USD",
            ):
                raise RuntimeError(ERROR)
            for table in (
                "dividend_entitlements",
                "dividend_receipts",
                "dividend_receipt_origins",
                "dividend_direct_receipts",
                "dividend_direct_receipt_origins",
                "jobs",
                "upstream_attempts",
            ):
                if con.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one() != 0:
                    raise RuntimeError(ERROR)
            claim, legacy, direct = (str(uuid4()) for _ in range(3))
            con.execute(
                text("""
                    INSERT INTO dividend_entitlements(id,revision_id,account_id,
                        eligible_shares_e8,gross_micro,expected_net_micro,currency,
                        quality,projection_version,created_at)
                    VALUES (:id,:revision,:account,1000000000,10000000,10000000,
                        'USD','ACCRUED',0,:created)
                """),
                {"id": claim, "revision": old_revision, "account": account, "created": stamp()},
            )
            con.execute(
                text("""
                    INSERT INTO dividend_receipts(id,entitlement_id,account_id,received_date,
                        received_micro,currency,tax_micro,fee_micro,created_at)
                    VALUES (:id,:claim,:account,'2026-11-05',6000000,'USD',NULL,NULL,:created)
                """),
                {"id": legacy, "claim": claim, "account": account, "created": stamp()},
            )
            post_cash(
                con,
                account=account,
                day="2026-11-05",
                currency="USD",
                amount=6_000_000,
                fx=None,
                booked=None,
                kind="DIVIDEND_RECEIPT",
                source_type="DIVIDEND_RECEIPT",
                source_id=legacy,
            )
            con.execute(
                text("""
                    INSERT INTO dividend_direct_receipts(id,event_id,revision_id,account_id,
                        received_date,received_micro,currency,tax_micro,fee_micro,
                        fx_rate_hkd_e12,received_hkd_micro,note,created_at)
                    VALUES (:id,:event,:revision,:account,'2026-11-06',2000000,'USD',
                        NULL,0,NULL,NULL,:note,:created)
                """),
                {
                    "id": direct,
                    "event": event,
                    "revision": current_revision,
                    "account": account,
                    "note": "Synthetic direct comparison receipt",
                    "created": stamp(),
                },
            )
            cash_id = post_cash(
                con,
                account=account,
                day="2026-11-06",
                currency="USD",
                amount=2_000_000,
                fx=None,
                booked=None,
                kind="DIVIDEND_RECEIPT",
                source_type="DIVIDEND_DIRECT_RECEIPT",
                source_id=direct,
                note="Synthetic direct comparison receipt",
            )
            receipt = (
                con.execute(
                    text("SELECT * FROM dividend_direct_receipts WHERE id=:id"), {"id": direct}
                )
                .mappings()
                .one()
            )
            cash = (
                con.execute(text("SELECT * FROM cash_ledger_entries WHERE id=:id"), {"id": cash_id})
                .mappings()
                .one()
            )
            at = now()
            origin = {
                "receipt_id": direct,
                "owner_id": scope["owner_id"],
                "event_id": event,
                "revision_id": current_revision,
                "revision_no": 2,
                "account_id": account,
                "portfolio_id": scope["portfolio_id"],
                "broker_id": scope["broker_id"],
                "account_version": scope["account_version"],
                "portfolio_version": scope["portfolio_version"],
                "portfolio_data_revision": scope["data_revision"],
                "broker_version": scope["broker_version"],
                "cash_entry_id": cash_id,
                "fx_source": None,
                "actual_hkd_reason": None,
                "request_id": str(uuid4()),
                "created_at": stamp(at),
            }
            reason = "Synthetic direct receipt for cross-store comparison only."
            audit(
                con,
                scope["owner_id"],
                "dividend_direct_receipt",
                direct,
                "DIVIDEND_DIRECT_RECEIPT_RECORDED",
                request_id=origin["request_id"],
                after=_creation_payload(receipt, origin, cash, reason),
                reason=reason,
                at=at,
            )
            origin["audit_id"] = con.execute(
                text("""
                    SELECT id FROM audit_events WHERE entity_type='dividend_direct_receipt'
                    AND entity_id=:id AND action='DIVIDEND_DIRECT_RECEIPT_RECORDED'
                """),
                {"id": direct},
            ).scalar_one()
            columns = ",".join(origin)
            parameters = ",".join(f":{name}" for name in origin)
            con.execute(
                text(
                    f"INSERT INTO dividend_direct_receipt_origins ({columns}) VALUES ({parameters})"
                ),
                origin,
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
