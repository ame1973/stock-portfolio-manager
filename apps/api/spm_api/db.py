"""SQLite connection policy and short, explicitly controlled units of work."""

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.pool import ConnectionPoolEntry

from spm_api.config import ROOT, Settings


def migration_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def schema_head() -> str:
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    if head is None:
        raise RuntimeError("No schema migration is installed")
    return head


def make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = create_engine(
        URL.create("sqlite", database=str(path)),
        connect_args={"check_same_thread": False, "timeout": 5},
        pool_size=5,
        max_overflow=0,
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _: ConnectionPoolEntry) -> None:
        # Own BEGIN explicitly: sqlite3 legacy mode otherwise leaves DDL outside transactions.
        connection.isolation_level = None
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Foreign key enforcement is unavailable")

    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        statement = (
            "BEGIN IMMEDIATE" if connection.get_execution_options().get("writer") else "BEGIN"
        )
        connection.exec_driver_sql(statement)

    return engine


@contextmanager
def unit_of_work(engine: Engine, *, write: bool = False) -> Iterator[Connection]:
    """Commit facts, cash effects, audit, and invalidations together or roll everything back."""
    with engine.connect().execution_options(writer=write) as connection, connection.begin():
        yield connection


def check_database(engine: Engine, settings: Settings) -> None:
    """Fail closed on schema mismatch; never mutate the schema during application startup."""
    with unit_of_work(engine) as connection:
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise RuntimeError("FOREIGN_KEYS_DISABLED")
        if connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() != (
            schema_head()
        ):
            raise RuntimeError("MIGRATION_REQUIRED")
        if connection.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok":
            raise RuntimeError("DB_INTEGRITY_FAILED")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("DB_FOREIGN_KEY_FAILED")
        if connection.exec_driver_sql("SELECT json_valid('{}')").scalar_one() != 1:
            raise RuntimeError("JSON_SUPPORT_REQUIRED")
    if shutil.disk_usage(settings.data_dir).free < settings.minimum_free_bytes:
        raise RuntimeError("INSUFFICIENT_DISK_SPACE")
