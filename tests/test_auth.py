"""Authentication security and concurrency regressions for PRD-001 / T-020."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi import Request
from sqlalchemy import Engine, text

from spm_api.audit import canonical, digest, now, stamp
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.main import create_app
from spm_api.security import (
    COOKIE,
    CSRF_COOKIE,
    issue_setup_token,
    read_auth,
    require_reauth,
    reserve_attempt,
)
from tests.helpers import PASSWORD, initialize, write_headers

pytestmark = pytest.mark.anyio
NEW_PASSWORD = "different-synthetic-passphrase-2026"


async def test_setup_creates_one_owner_hashes_only_and_consumes_token(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    assert (await client.get("/api/v1/setup/status")).json() == {
        "initialized": False,
        "requires_token": True,
    }
    owner = await initialize(client, engine)
    assert (await client.get("/api/v1/auth/me")).json() == owner
    assert (await client.get("/api/v1/setup/status")).json() == {
        "initialized": True,
        "requires_token": False,
    }
    assert issue_setup_token(engine, rotate=True) is None
    token = str(client.cookies.get(COOKIE))
    csrf = str(client.cookies.get(CSRF_COOKIE))
    assert token != csrf
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM owners")).scalar_one() == 1
        assert con.execute(text("SELECT count(*) FROM bootstrap_tokens")).scalar_one() == 0
        encoded = con.execute(text("SELECT password_hash FROM owners")).scalar_one()
        assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
        session = con.execute(text("SELECT * FROM sessions")).mappings().one()
        assert session["token_sha256"] == digest(token)
        assert session["csrf_sha256"] == digest(csrf)
        assert PASSWORD not in str(session)
        assert token not in str(session) and csrf not in str(session)
        assert con.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 2


async def test_setup_race_has_exactly_one_winner(client: httpx.AsyncClient, engine: Engine) -> None:
    token = issue_setup_token(engine, rotate=True)
    results = await asyncio.gather(
        *[
            client.post(
                "/api/v1/setup/initialize", json={"setup_token": token, "password": PASSWORD}
            )
            for _ in range(2)
        ]
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM owners")).scalar_one() == 1
        assert con.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 1


@pytest.mark.parametrize("expired", [False, True])
async def test_invalid_or_expired_bootstrap_does_not_initialize(
    client: httpx.AsyncClient,
    engine: Engine,
    expired: bool,
) -> None:
    token = issue_setup_token(engine, rotate=True)
    if expired:
        with unit_of_work(engine, write=True) as con:
            con.execute(
                text("UPDATE bootstrap_tokens SET expires_at=:past"),
                {
                    "past": stamp(now() - timedelta(seconds=1)),
                },
            )
    result = await client.post(
        "/api/v1/setup/initialize",
        json={
            "setup_token": token if expired else "invalid-token-with-sufficient-length",
            "password": PASSWORD,
        },
    )
    assert result.status_code == 401
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM owners")).scalar_one() == 0


@pytest.mark.parametrize("password", ["short", "aaaaaaaaaaaa", "password1234", "x" * 201])
async def test_weak_or_oversized_password_is_rejected_without_echo(
    client: httpx.AsyncClient,
    engine: Engine,
    password: str,
) -> None:
    token = issue_setup_token(engine, rotate=True)
    assert token is not None
    result = await client.post(
        "/api/v1/setup/initialize",
        json={
            "setup_token": token,
            "password": password,
        },
    )
    assert result.status_code == 422
    assert password not in result.text
    assert token not in result.text


async def test_origin_and_fetch_metadata_are_required(client: httpx.AsyncClient) -> None:
    for headers in (
        {"Origin": "https://attacker.invalid"},
        {"Origin": "null"},
        {
            "Sec-Fetch-Site": "cross-site",
        },
    ):
        result = await client.post(
            "/api/v1/auth/login", json={"password": PASSWORD}, headers=headers
        )
        assert result.status_code == 403
        assert result.json()["code"] == "ORIGIN_REJECTED"
    client.headers.pop("Origin")
    assert (await client.post("/api/v1/auth/login", json={"password": PASSWORD})).status_code == 403


async def test_logout_revokes_copied_cookie(client: httpx.AsyncClient, engine: Engine) -> None:
    await initialize(client, engine)
    copied = str(client.cookies.get(COOKIE))
    result = await client.post("/api/v1/auth/logout", headers=write_headers(client))
    assert result.status_code == 200
    assert COOKIE not in client.cookies and CSRF_COOKIE not in client.cookies
    assert (
        await client.get("/api/v1/auth/me", headers={"Cookie": f"{COOKIE}={copied}"})
    ).status_code == 401
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT revoked_at FROM sessions")).scalar_one() is not None
        assert con.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 1


async def test_password_change_revokes_all_sessions(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await initialize(client, engine)
    first = str(client.cookies.get(COOKIE))
    login = await client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    second = str(client.cookies.get(COOKIE))
    assert first != second
    result = await client.post(
        "/api/v1/auth/change-password",
        headers=write_headers(client),
        json={
            "old_password": PASSWORD,
            "new_password": NEW_PASSWORD,
        },
    )
    assert result.status_code == 200
    for token in (first, second):
        assert (
            await client.get("/api/v1/auth/me", headers={"Cookie": f"{COOKIE}={token}"})
        ).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={"password": NEW_PASSWORD})
    ).status_code == 200
    with unit_of_work(engine) as con:
        assert (
            con.execute(
                text("SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL")
            ).scalar_one()
            == 2
        )
        assert (
            con.execute(
                text("SELECT count(*) FROM audit_events WHERE action='CHANGE_PASSWORD'")
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("missing", ["X-CSRF-Token", "Idempotency-Key"])
async def test_protected_write_requires_headers(
    client: httpx.AsyncClient,
    engine: Engine,
    missing: str,
) -> None:
    await initialize(client, engine)
    headers = write_headers(client)
    headers.pop(missing)
    result = await client.post("/api/v1/auth/logout", headers=headers)
    assert result.status_code == 422
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_csrf_cannot_be_forged_or_reused_from_another_session(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    old_csrf = str(client.cookies.get(CSRF_COOKIE))
    assert (await client.post("/api/v1/auth/login", json={"password": PASSWORD})).status_code == 200
    for csrf in (old_csrf, "forged-csrf-token-000000"):
        headers = write_headers(client)
        headers["X-CSRF-Token"] = csrf
        result = await client.post("/api/v1/auth/logout", headers=headers)
        assert result.status_code == 403
        assert result.json()["code"] == "CSRF_REJECTED"
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_csrf_token_survives_refresh_and_rotates_if_cookie_is_lost(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    old_csrf = str(client.cookies.get(CSRF_COOKIE))
    assert (await client.get("/api/v1/auth/csrf")).json()["csrf_token"] == old_csrf
    client.cookies.delete(CSRF_COOKIE)
    fresh = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    assert fresh != old_csrf
    assert (
        await client.post("/api/v1/auth/logout", headers=write_headers(client))
    ).status_code == 200


@pytest.mark.parametrize("column", ["expires_at", "last_seen_at"])
async def test_absolute_and_idle_expiry(
    client: httpx.AsyncClient, engine: Engine, column: str
) -> None:
    await initialize(client, engine)
    with unit_of_work(engine, write=True) as con:
        con.execute(
            text(f"UPDATE sessions SET {column}=:past"),
            {
                "past": stamp(now() - timedelta(days=8)),
            },
        )
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_active_session_touches_idle_timestamp(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await initialize(client, engine)
    with unit_of_work(engine, write=True) as con:
        con.execute(
            text("UPDATE sessions SET last_seen_at=:past"),
            {
                "past": stamp(now() - timedelta(minutes=6)),
            },
        )
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT last_seen_at FROM sessions")).scalar_one() > stamp(
            now() - timedelta(minutes=1)
        )


async def test_failed_login_backoff_is_persistent(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await initialize(client, engine)
    wrong = {"password": "incorrect-synthetic-password"}
    first = await client.post("/api/v1/auth/login", json=wrong)
    assert first.status_code == 401
    with patch("spm_api.auth_routes.verify_password") as verify:
        limited = await client.post("/api/v1/auth/login", json=wrong)
        assert limited.status_code == 429
        verify.assert_not_called()
    assert int(limited.headers["Retry-After"]) > 0
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT max(failures) FROM auth_rate_limits")).scalar_one() == 1
    client.cookies.clear()
    assert (await client.post("/api/v1/auth/login", json=wrong)).status_code == 429


async def test_parallel_attempt_reservations_are_atomic(settings: Settings, engine: Engine) -> None:
    app = create_app(settings)
    app.state.engine = engine

    def attempt(_: int) -> bool:
        request = Request(
            {"type": "http", "app": app, "client": ("127.0.0.2", 1234), "headers": []}
        )
        try:
            reserve_attempt(request, "concurrency-test")
            return True
        except ApiError as exc:
            assert exc.status == 429
            return False

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(attempt, range(24)))
    assert sum(results) == 10


async def test_url_token_is_not_authentication(client: httpx.AsyncClient, engine: Engine) -> None:
    await initialize(client, engine)
    token = str(client.cookies.get(COOKIE))
    client.cookies.clear()
    assert (await client.get("/api/v1/auth/me", params={"token": token})).status_code == 401


async def test_session_revoke_is_owner_scoped_and_idempotent(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    old_token = str(client.cookies.get(COOKIE))
    await client.post("/api/v1/auth/login", json={"password": PASSWORD})
    sessions = (await client.get("/api/v1/auth/sessions?limit=1")).json()
    assert len(sessions["items"]) == 1 and sessions["next_cursor"] is not None
    all_sessions = (await client.get("/api/v1/auth/sessions")).json()["items"]
    previous = next(item for item in all_sessions if not item["current"])
    headers = write_headers(client)
    for _ in range(2):
        assert (
            await client.delete(f"/api/v1/auth/sessions/{previous['id']}", headers=headers)
        ).status_code == 200
    assert (
        await client.get("/api/v1/auth/me", headers={"Cookie": f"{COOKIE}={old_token}"})
    ).status_code == 401
    unknown = await client.delete(f"/api/v1/auth/sessions/{uuid4()}", headers=write_headers(client))
    assert unknown.status_code == 404
    with unit_of_work(engine) as con:
        assert (
            con.execute(
                text("SELECT count(*) FROM audit_events WHERE action='REVOKE'")
            ).scalar_one()
            == 1
        )


async def test_reauthentication_window_and_idempotent_audit(
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    await initialize(client, engine)
    token = str(client.cookies.get(COOKIE))
    with unit_of_work(engine, write=True) as con:
        con.execute(
            text("UPDATE sessions SET reauthenticated_at=:past"),
            {
                "past": stamp(now() - timedelta(minutes=6)),
            },
        )
        auth = read_auth(con, token)
    with pytest.raises(ApiError, match="Confirm your password"):
        require_reauth(auth)
    headers = write_headers(client)
    for _ in range(2):
        assert (
            await client.post("/api/v1/auth/reauth", headers=headers, json={"password": PASSWORD})
        ).status_code == 200
    with unit_of_work(engine) as con:
        stored = con.execute(
            text("SELECT request_sha256,response_json FROM idempotency_keys")
        ).one()
        assert stored.request_sha256 != digest(canonical({"password": PASSWORD}))
        assert PASSWORD not in stored.response_json
        auth = read_auth(con, token)
        require_reauth(auth)
        with pytest.raises(ApiError):
            require_reauth(replace(auth, reauthenticated_at=None))
        assert (
            con.execute(
                text("SELECT count(*) FROM audit_events WHERE action='REAUTHENTICATE'")
            ).scalar_one()
            == 1
        )


async def test_https_required_and_production_cookies(settings: Settings, engine: Engine) -> None:
    config = settings.model_copy(
        update={"app_env": "production", "public_origin": "https://testserver"}
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as insecure:
            response = await insecure.post(
                "/api/v1/auth/login",
                headers={"Origin": config.public_origin},
                json={"password": PASSWORD},
            )
            assert response.status_code == 403 and response.json()["code"] == "HTTPS_REQUIRED"
        token = issue_setup_token(engine, rotate=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://testserver"
        ) as secure:
            response = await secure.post(
                "/api/v1/setup/initialize",
                headers={"Origin": config.public_origin},
                json={"setup_token": token, "password": PASSWORD},
            )
            assert response.status_code == 200
            cookie = next(
                value
                for value in response.headers.get_list("set-cookie")
                if value.startswith(COOKIE)
            )
            assert all(
                flag in cookie.lower()
                for flag in ("httponly", "secure", "samesite=strict", "path=/")
            )
            assert COOKIE not in response.text and PASSWORD not in response.text
