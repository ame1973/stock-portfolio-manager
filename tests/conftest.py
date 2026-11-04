"""Isolated application configuration and real Alembic migrations for tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        public_origin="http://testserver",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        minimum_free_bytes=0,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    migrate(settings)
    engine = make_engine(settings.database_path)
    try:
        yield engine
    finally:
        engine.dispose()
