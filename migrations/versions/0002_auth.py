"""Add bootstrap and persistent authentication controls without storing raw secrets."""

from alembic import op

revision = "0002_auth"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN reauthenticated_at TEXT")
    op.execute("""
        CREATE TABLE bootstrap_tokens (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            token_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE auth_rate_limits (
            bucket_sha256 TEXT PRIMARY KEY,
            window_start INTEGER NOT NULL,
            attempts INTEGER NOT NULL CHECK(attempts>=0),
            failures INTEGER NOT NULL DEFAULT 0 CHECK(failures>=0),
            blocked_until INTEGER NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting auth history")
