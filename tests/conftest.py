"""Isolated application configuration and real Alembic migrations for tests."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine
from spm_api.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        public_origin="http://testserver",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        minimum_free_bytes=0,
    )


def _check_isolated_database(settings: Settings, tmp_path: Path, base: Path) -> None:
    """Reject an existing or non-temporary database before any test migration."""
    root = tmp_path.resolve(strict=True)
    base = base.resolve(strict=True)
    if (
        settings.app_env != "test"
        or root == base
        or not root.is_relative_to(base)
        or settings.data_dir.resolve() != root / "data"
        or settings.backup_dir.resolve() != root / "backups"
        or settings.database_path.resolve() != root / "data" / "portfolio.sqlite3"
        or settings.database_path.exists()
    ):
        raise RuntimeError("TEST_DATABASE_SCOPE_REJECTED")


@pytest.fixture
def engine(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Engine]:
    _check_isolated_database(settings, tmp_path, tmp_path_factory.getbasetemp())
    migrate(settings)
    engine = make_engine(settings.database_path)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(settings: Settings, engine: Engine) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://testserver",
            headers={"Origin": settings.public_origin},
        ) as client,
    ):
        yield client
