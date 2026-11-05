"""Shared synthetic onboarding, record creation, and guarded API request headers."""

from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import Engine

from spm_api.security import CSRF_COOKIE, issue_setup_token

PASSWORD = "synthetic-owner-passphrase-2026"


async def initialize(client: httpx.AsyncClient, engine: Engine) -> dict[str, Any]:
    token = issue_setup_token(engine, rotate=True)
    assert token is not None
    result = await client.post(
        "/api/v1/setup/initialize",
        json={
            "setup_token": token,
            "password": PASSWORD,
        },
    )
    assert result.status_code == 200, result.text
    return dict(result.json())


def write_headers(client: httpx.AsyncClient, *, key: str | None = None) -> dict[str, str]:
    return {
        "X-CSRF-Token": str(client.cookies.get(CSRF_COOKIE)),
        "Idempotency-Key": key or str(uuid4()),
    }


def version_headers(
    client: httpx.AsyncClient, version: int, *, key: str | None = None
) -> dict[str, str]:
    return {**write_headers(client, key=key), "If-Match": f'"{version}"'}


async def create(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], *, key: str | None = None
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/{path}", json=body, headers=write_headers(client, key=key)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())
