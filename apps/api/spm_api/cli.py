"""Administrative commands; migrations never run implicitly inside HTTP requests."""

import argparse
import fcntl
import os
import shutil
import sqlite3
from uuid import uuid4

from alembic import command
from sqlalchemy import Engine

from spm_api.config import Settings
from spm_api.db import check_database, make_engine, migration_config, schema_head


def migrate(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = settings.data_dir / ".migration.lock"
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        db_path = settings.database_path
        if db_path.exists() and db_path.stat().st_size:
            with sqlite3.connect(db_path) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("DB_INTEGRITY_FAILED")
                if source.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("DB_FOREIGN_KEY_FAILED")
                has_version = source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
                ).fetchone()
                if has_version:
                    current = source.execute("SELECT version_num FROM alembic_version").fetchone()
                    if current and current[0] == schema_head():
                        return
                if shutil.disk_usage(settings.data_dir).free < max(
                    settings.minimum_free_bytes, db_path.stat().st_size * 3
                ):
                    raise RuntimeError("INSUFFICIENT_DISK_SPACE")
                # This local rollback snapshot is not a portable disaster-recovery backup.
                snapshot = settings.data_dir / f"pre-migration-{uuid4()}.sqlite3"
                snapshot.touch(mode=0o600, exist_ok=False)
                with sqlite3.connect(snapshot) as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("MIGRATION_SNAPSHOT_FAILED")
        engine = make_engine(db_path)
        try:
            _upgrade(engine)
            check_database(engine, settings)
            os.chmod(db_path, 0o600)
        finally:
            engine.dispose()


def _upgrade(engine: Engine) -> None:
    with engine.connect().execution_options(writer=True) as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stock Portfolio Manager administration")
    parser.add_argument("command", choices=["migrate", "check"])
    args = parser.parse_args()
    settings = Settings()
    if args.command == "migrate":
        migrate(settings)
        print("Migrations applied and database integrity verified.")
    else:
        engine = make_engine(settings.database_path)
        try:
            check_database(engine, settings)
            print("Database ready.")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
