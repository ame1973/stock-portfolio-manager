"""Create the complete reference schema, integrity triggers, and exchange defaults."""

import sqlite3
from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "sql/0001_baseline.sql"
    statement = ""
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("--"):
            continue
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            # Connection PRAGMAs are initialized outside the migration transaction.
            if not statement.lstrip().upper().startswith("PRAGMA"):
                op.get_bind().exec_driver_sql(statement.strip())
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete baseline SQL statement")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a validated pre-upgrade backup")
