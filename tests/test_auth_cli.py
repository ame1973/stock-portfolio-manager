"""Administrative bootstrap is explicit; proxy trust cannot be asserted by an untrusted peer."""

from typing import cast

import httpx
import pytest
from sqlalchemy import Engine, text
from starlette.types import ASGIApp
from uvicorn._types import ASGI3Application
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from spm_api.audit import digest
from spm_api.cli import main
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.main import create_app
from tests.helpers import PASSWORD, initialize

pytestmark = pytest.mark.anyio


async def test_server_startup_does_not_issue_or_log_a_setup_credential(
    client: httpx.AsyncClient,
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (await client.get("/api/v1/setup/status")).json()["requires_token"] is True
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM bootstrap_tokens")).scalar_one() == 0
    assert "token" not in capsys.readouterr().out.lower()


async def test_setup_cli_rotates_expiring_hash_and_check_command(
    settings: Settings,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("spm_api.cli.Settings", lambda: settings)
    previous: str | None = None
    for _ in range(2):
        monkeypatch.setattr("sys.argv", ["spm-manage", "setup-token"])
        main()
        output = capsys.readouterr().out
        token = output.strip().rsplit(": ", 1)[-1]
        assert len(token) >= 40 and token != previous
        with unit_of_work(engine) as con:
            assert con.execute(
                text("SELECT token_sha256 FROM bootstrap_tokens")
            ).scalar_one() == digest(token)
        previous = token
    monkeypatch.setattr("sys.argv", ["spm-manage", "check"])
    main()
    assert capsys.readouterr().out.strip() == "Database ready."


async def test_cli_cannot_reopen_setup_after_owner_exists(
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await initialize(client, engine)
    monkeypatch.setattr("spm_api.cli.Settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["spm-manage", "setup-token"])
    with pytest.raises(SystemExit, match="An owner already exists"):
        main()


async def test_https_forwarding_is_accepted_only_from_trusted_proxy(
    settings: Settings,
    engine: Engine,
) -> None:
    config = settings.model_copy(
        update={"app_env": "production", "public_origin": "https://testserver"}
    )
    app = create_app(config)
    # Both implement ASGI 3; Uvicorn and Starlette publish distinct protocol aliases.
    proxy = ProxyHeadersMiddleware(cast(ASGI3Application, app), trusted_hosts=["127.0.0.1"])
    async with app.router.lifespan_context(app):
        for address, status in (("192.0.2.2", 403), ("127.0.0.1", 401)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(cast(ASGIApp, proxy), client=(address, 1234)),
                base_url="http://testserver",
            ) as client:
                result = await client.post(
                    "/api/v1/auth/login",
                    headers={
                        "Origin": config.public_origin,
                        "X-Forwarded-Proto": "https",
                    },
                    json={"password": PASSWORD},
                )
                assert result.status_code == status
                assert result.headers["Cache-Control"] == "no-store"
