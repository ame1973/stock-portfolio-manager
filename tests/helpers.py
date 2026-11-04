"""Shared synthetic onboarding and request headers for API regression tests."""

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
