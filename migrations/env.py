"""Run migrations using the same connection policy as the application."""

from alembic import context
from sqlalchemy import Connection

from spm_api.config import Settings
from spm_api.db import make_engine


def run(connection: Connection) -> None:
    context.configure(connection=connection, transactional_ddl=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline SQL generation is not supported; migrate a staging SQLite database")
elif isinstance(context.config.attributes.get("connection"), Connection):
    run(context.config.attributes["connection"])
else:
    engine = make_engine(Settings().database_path)
    try:
        with engine.connect().execution_options(writer=True) as connection:
            run(connection)
    finally:
        engine.dispose()
