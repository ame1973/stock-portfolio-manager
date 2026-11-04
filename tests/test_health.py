"""Public health remains sanitized; uninitialized databases never pass readiness."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from spm_api.config import Settings
from spm_api.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client,
    ):
        yield client


@pytest.mark.anyio
async def test_liveness_and_readiness_without_migration(settings: Settings) -> None:
    async with client_for(settings) as client:
        assert (await client.get("/api/v1/health/live")).json() == {"status": "ok"}
        result = await client.get("/api/v1/health/ready")
        assert result.status_code == 503
        assert result.json()["code"] == "NOT_READY"
        assert "sqlite" not in result.text.lower()
        assert str(settings.data_dir) not in result.text
        assert result.headers["Cache-Control"] == "no-store"
        assert result.headers["X-Request-ID"] == result.json()["request_id"]


@pytest.mark.anyio
async def test_ready_after_migration(engine: Engine, settings: Settings) -> None:
    async with client_for(settings) as client:
        result = await client.get("/api/v1/health/ready")
        assert result.status_code == 200
        assert result.json() == {"status": "ready"}
        assert result.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.anyio
async def test_readiness_rejects_disk_full(engine: Engine, settings: Settings) -> None:
    async with client_for(settings) as client:
        with patch("spm_api.db.shutil.disk_usage") as usage:
            usage.return_value.free = -1
            assert (await client.get("/api/v1/health/ready")).status_code == 503


@pytest.mark.anyio
async def test_production_does_not_expose_docs(settings: Settings) -> None:
    production = settings.model_copy(
        update={"app_env": "production", "public_origin": "https://stocks.example.invalid"}
    )
    async with client_for(production) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize(
    "origin",
    [
        "http://stocks.example.com",
        "https://user:secret@localhost",
        "https://localhost/path",
        "javascript:alert(1)",
        "https://localhost?token=secret",
        "https://localhost#fragment",
    ],
)
def test_invalid_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(public_origin=origin)


def test_production_requires_https() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(app_env="production", public_origin="http://localhost")


def test_application_construction_has_no_storage_side_effects(tmp_path: Path) -> None:
    config = Settings(data_dir=tmp_path / "absent")
    create_app(config).openapi()
    assert not config.data_dir.exists()
