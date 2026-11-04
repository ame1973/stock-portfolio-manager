"""Version master data, enforce ownership/mapping periods, and reject scaled REAL values."""

from alembic import op

revision = "0003_master_data"
down_revision = "0002_auth"
branch_labels = None
depends_on = None


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def upgrade() -> None:
    con = op.get_bind()
    for table in ("securities", "security_identifiers", "provider_symbols"):
        con.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1)"
        )
    con.exec_driver_sql("ALTER TABLE accounts ADD COLUMN opening_cash_date TEXT")
    if con.exec_driver_sql("""
        SELECT 1 FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
        JOIN brokers b ON b.id=a.broker_id WHERE p.owner_id<>b.owner_id LIMIT 1
    """).first():
        raise RuntimeError("Account ownership must be repaired before migration")
    for event in ("INSERT", "UPDATE"):
        suffix = event.lower()
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_account_owner_{suffix} BEFORE {event} ON accounts
            WHEN (SELECT owner_id FROM portfolios WHERE id=NEW.portfolio_id)
                 IS NOT (SELECT owner_id FROM brokers WHERE id=NEW.broker_id)
            BEGIN SELECT RAISE(ABORT, 'ACCOUNT_BROKER_OWNER_MISMATCH'); END
        """)
        for table, identity in (
            (
                "security_identifiers",
                "old.exchange_code=NEW.exchange_code AND "
                "old.identifier_type=NEW.identifier_type AND "
                "old.identifier_value=NEW.identifier_value",
            ),
            (
                "provider_symbols",
                "old.provider_id=NEW.provider_id AND "
                "(old.security_id=NEW.security_id OR old.provider_symbol=NEW.provider_symbol)",
            ),
        ):
            con.exec_driver_sql(f"""
                CREATE TRIGGER trg_{table}_period_{suffix} BEFORE {event} ON {table}
                WHEN EXISTS (
                    SELECT 1 FROM {table} old WHERE old.id<>NEW.id AND {identity}
                    AND (old.effective_to IS NULL OR old.effective_to>=NEW.effective_from)
                    AND (NEW.effective_to IS NULL OR old.effective_from<=NEW.effective_to)
                )
                BEGIN SELECT RAISE(ABORT, 'OVERLAPPING_IDENTITY_PERIOD'); END
            """)
    # SQLite INTEGER affinity accepts REAL values unless typeof is enforced explicitly.
    tables = (
        con.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars().all()
    )
    for table in tables:
        columns = [
            row[1]
            for row in con.exec_driver_sql(f"PRAGMA table_info({_quote(table)})")
            if row[1].endswith(("_micro", "_e8", "_e12", "_ppm"))
        ]
        if not columns:
            continue
        invalid = " OR ".join(
            f"({_quote(col)} IS NOT NULL AND typeof({_quote(col)})!='integer')" for col in columns
        )
        if con.exec_driver_sql(f"SELECT 1 FROM {_quote(table)} WHERE {invalid} LIMIT 1").first():
            raise RuntimeError(f"Non-integer scaled value found in {table}; migration refused")
        condition = " OR ".join(
            f"(NEW.{_quote(col)} IS NOT NULL AND typeof(NEW.{_quote(col)})!='integer')"
            for col in columns
        )
        for event in ("INSERT", "UPDATE"):
            name = _quote(f"trg_scaled_{table}_{event.lower()}")
            con.exec_driver_sql(f"""
                CREATE TRIGGER {name} BEFORE {event} ON {_quote(table)} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'SCALED_VALUE_MUST_BE_INTEGER'); END
            """)


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of removing integrity guards"
    )
