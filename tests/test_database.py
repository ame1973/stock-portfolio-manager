"""Migration, transaction, and database-enforced invariants (T-019, T-025..T-027)."""

import sqlite3

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import check_database, make_engine, migration_config, schema_head, unit_of_work
from tests.provider_evidence_helpers import seed_evidence

NOW = "2026-11-04T00:00:00Z"


def seed(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners VALUES ('owner','synthetic-hash',?,?,1)", (NOW, NOW)
        )
        for portfolio in ("p1", "p2"):
            con.exec_driver_sql(
                "INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (?,'owner',?,?)",
                (portfolio, portfolio, NOW),
            )
        con.exec_driver_sql("INSERT INTO brokers(id,owner_id,name) VALUES ('b','owner','Test')")
        for account, portfolio in (("a1", "p1"), ("a2", "p2")):
            con.exec_driver_sql(
                "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
                "VALUES (?,?,'b',?,'HKD',?)",
                (account, portfolio, account, NOW),
            )
        con.exec_driver_sql(
            "INSERT INTO securities(id,exchange_code,display_ticker,name,"
            "quote_currency,created_at) "
            "VALUES ('s','HKG','0005','Synthetic','HKD',?)",
            (NOW,),
        )
        for kind in ("TRADE", "TRANSFER"):
            con.exec_driver_sql(
                "INSERT INTO ledger_sequences(kind,fact_id) VALUES (?,'t')", (kind,)
            )
        for event, revision in (("e1", "r1"), ("e2", "r2")):
            con.exec_driver_sql(
                "INSERT INTO dividend_events(id,security_id,event_identity_key,created_at) "
                "VALUES (?,'s',?,?)",
                (event, event, NOW),
            )
            con.exec_driver_sql(
                "INSERT INTO dividend_event_revisions(id,event_id,revision_no,event_type,ex_date,"
                "dps_e8,currency,adjustment_basis,review_status,created_at) "
                "VALUES (?,?,1,'REGULAR','2026-08-01',100000000,'HKD',"
                "'AS_DECLARED','PROVISIONAL',?)",
                (revision, event, NOW),
            )
        con.exec_driver_sql(
            "INSERT INTO dividend_entitlements(id,revision_id,account_id,eligible_shares_e8,"
            "gross_micro,expected_net_micro,currency,quality,projection_version,created_at) "
            "VALUES ('ent','r1','a1',100000000,1000000,1000000,'HKD','ACCRUED',0,?)",
            (NOW,),
        )
        seed_evidence(con, "owner", "s")
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,created_at) "
            "VALUES ('audit','owner','test','s','CREATE',?)",
            (NOW,),
        )


def test_empty_and_repeat_upgrade(engine: Engine, settings: Settings) -> None:
    migrate(settings)
    check_database(engine, settings)
    with engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == schema_head()
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert not con.exec_driver_sql("PRAGMA foreign_key_check").all()
        assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM exchanges").scalar() == 5
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            ).scalar()
            == 55
        )
    assert not list(settings.data_dir.glob("pre-migration-*"))


def test_every_connection_enforces_pragmas(engine: Engine) -> None:
    with engine.connect() as first, engine.connect() as second:
        for con in (first, second):
            assert con.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert con.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert con.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert con.exec_driver_sql("PRAGMA synchronous").scalar() == 2


def test_rollback_includes_schema_and_data(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="Injected"), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("CREATE TABLE rollback_test(id INTEGER PRIMARY KEY)")
        con.exec_driver_sql("INSERT INTO rollback_test VALUES (1)")
        raise RuntimeError("Injected failure")
    with engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='rollback_test'").first()
            is None
        )


@pytest.mark.parametrize(
    "table",
    [
        "dividend_observations",
        "dividend_event_revisions",
        "audit_events",
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_evidence_is_append_only(engine: Engine, table: str, operation: str) -> None:
    seed(engine)
    sql = f"UPDATE {table} SET id=id" if operation == "UPDATE" else f"DELETE FROM {table}"
    with pytest.raises(IntegrityError, match="IMMUTABLE"), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(sql)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (
            "INSERT INTO trades(id,portfolio_id,account_id,security_id,side,trade_date,quantity_e8,"
            "price_e8,currency,fx_rate_hkd_e12,gross_micro,settlement_micro,booked_hkd_micro,"
            "created_at,updated_at,root_id,created_sequence) VALUES ('t','p2','a1','s','BUY',"
            "'2026-01-01',100000000,100000000,'HKD',1000000000000,1000000,1000000,"
            "1000000,'now','now','t',1)",
            "TRADE_ACCOUNT_PORTFOLIO_MISMATCH",
        ),
        (
            "INSERT INTO security_transfers(id,portfolio_id,source_account_id,target_account_id,"
            "security_id,transfer_date,quantity_e8,transferred_cost_hkd_micro,"
            "created_at,created_sequence,root_id) "
            "VALUES ('t','p1','a1','a2','s','2026-01-01',100000000,1000000,'now',2,'t')",
            "TRANSFER_ACCOUNT_PORTFOLIO_MISMATCH",
        ),
        (
            "INSERT INTO positions VALUES ('p2','a1','s',1,100000000,1000000,0,'now')",
            "POSITION_ACCOUNT_PORTFOLIO_MISMATCH",
        ),
        (
            "UPDATE dividend_events SET current_revision_id='r2' WHERE id='e1'",
            "DIVIDEND_REVISION_EVENT_MISMATCH",
        ),
        (
            "INSERT INTO dividend_receipts(id,entitlement_id,account_id,received_date,"
            "received_micro,currency,created_at) "
            "VALUES ('receipt','ent','a2','2026-09-01',1000000,'HKD','now')",
            "RECEIPT_ACCOUNT_ENTITLEMENT_MISMATCH",
        ),
    ],
)
def test_cross_entity_invariants(engine: Engine, sql: str, code: str) -> None:
    seed(engine)
    with pytest.raises(IntegrityError, match=code), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(sql)


def test_linked_broker_cannot_be_deleted(engine: Engine) -> None:
    seed(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DELETE FROM brokers WHERE id='b'")
    with engine.connect() as con:
        assert con.exec_driver_sql("SELECT count(*) FROM brokers").scalar() == 1


def test_readiness_detects_disabled_foreign_keys(engine: Engine, settings: Settings) -> None:
    engine.dispose()
    with engine.raw_connection() as con:
        con.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(RuntimeError, match="FOREIGN_KEYS_DISABLED"):
        check_database(engine, settings)


def test_corrupted_database_migration_is_non_destructive(settings: Settings) -> None:
    settings.data_dir.mkdir()
    corrupt = b"SQLite format 3\x00" + bytes(100)
    settings.database_path.write_bytes(corrupt)
    with pytest.raises(sqlite3.DatabaseError):
        migrate(settings)
    assert settings.database_path.read_bytes() == corrupt


def test_migration_preserves_existing_database(settings: Settings) -> None:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0001_baseline")
        with engine.begin() as con:
            con.exec_driver_sql(
                "INSERT INTO owners VALUES ('owner','synthetic-hash',?,?,1)", (NOW, NOW)
            )
        migrate(settings)
        snapshots = list(settings.data_dir.glob("pre-migration-*.sqlite3"))
        assert len(snapshots) == 1
        assert snapshots[0].stat().st_mode & 0o777 == 0o600
        with engine.connect() as con:
            assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar() == 1
            assert (
                con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                == schema_head()
            )
    finally:
        engine.dispose()
