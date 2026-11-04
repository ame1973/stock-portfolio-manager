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


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
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
