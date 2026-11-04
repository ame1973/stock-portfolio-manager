"""Failed integrity upgrades preserve the previous schema and every original fact."""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine

from spm_api.audit import stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config


def old_database(settings: Settings) -> Engine:
    engine = make_engine(settings.database_path)
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        command.upgrade(config, "0002_auth")
    return engine


def test_real_scaled_value_aborts_upgrade_atomically(settings: Settings) -> None:
    engine = old_database(settings)
    owner, broker = str(uuid4()), str(uuid4())
    try:
        with engine.begin() as con:
            con.exec_driver_sql(
                "INSERT INTO owners VALUES (?,'synthetic-hash',?,?,1)", (owner, stamp(), stamp())
            )
            con.exec_driver_sql(
                "INSERT INTO brokers(id,owner_id,name,dividend_fee_micro) "
                "VALUES (?,?,'Corrupted scaled value',1.5)",
                (broker, owner),
            )
        with pytest.raises(RuntimeError, match="Non-integer scaled value"):
            migrate(settings)
        with engine.connect() as con:
            assert (
                con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "0002_auth"
            )
            assert con.exec_driver_sql("SELECT dividend_fee_micro FROM brokers").scalar_one() == 1.5
            assert "version" not in {
                row[1] for row in con.exec_driver_sql("PRAGMA table_info(securities)")
            }
        assert len(list(settings.data_dir.glob("pre-migration-*.sqlite3"))) == 1
    finally:
        engine.dispose()


def test_cross_owner_account_aborts_upgrade_without_reassigning_it(settings: Settings) -> None:
    engine = old_database(settings)
    owner1, owner2, portfolio, broker, account = (str(uuid4()) for _ in range(5))
    try:
        with engine.begin() as con:
            for owner in (owner1, owner2):
                con.exec_driver_sql(
                    "INSERT INTO owners VALUES (?,'synthetic-hash',?,?,1)",
                    (owner, stamp(), stamp()),
                )
            con.exec_driver_sql(
                "INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (?,?,'Synthetic',?)",
                (portfolio, owner1, stamp()),
            )
            con.exec_driver_sql(
                "INSERT INTO brokers(id,owner_id,name) VALUES (?,?,'Other owner')", (broker, owner2)
            )
            con.exec_driver_sql(
                "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
                "VALUES (?,?,?,'Invalid ownership','HKD',?)",
                (account, portfolio, broker, stamp()),
            )
        with pytest.raises(RuntimeError, match="ownership must be repaired"):
            migrate(settings)
        with engine.connect() as con:
            assert (
                con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == "0002_auth"
            )
            assert con.exec_driver_sql("SELECT portfolio_id,broker_id FROM accounts").one() == (
                portfolio,
                broker,
            )
            assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    finally:
        engine.dispose()
