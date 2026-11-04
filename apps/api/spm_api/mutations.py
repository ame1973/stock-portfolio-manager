"""Atomic, owner-scoped idempotent writes shared by all state-changing services."""

import hmac
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import Request
from sqlalchemy import Connection, text

from spm_api.audit import canonical, digest, now, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Model
from spm_api.security import COOKIE, Auth, WriteHeaders, read_auth, validate_csrf


def sensitive_fingerprint(request: Request, payload: Any) -> str:
    # An unkeyed password fingerprint would bypass Argon2 as a fast offline password oracle.
    return hmac.new(
        request.cookies[COOKIE].encode(), canonical(payload).encode(), "sha256"
    ).hexdigest()


def mutate[M: Model](
    request: Request,
    headers: WriteHeaders,
    payload: Any,
    model: type[M],
    action: Callable[[Connection, Auth], M],
    *,
    status: int = 200,
) -> M:
    """Serialize before commit so response validation cannot leave a partial financial write."""
    route = f"{request.method} {request.url.path}"
    fingerprint = digest(canonical(payload))
    with unit_of_work(request.app.state.engine, write=True) as con:
        auth = read_auth(con, request.cookies.get(COOKIE))
        validate_csrf(auth, headers.csrf)
        owner = str(auth.owner.id)
        existing = (
            con.execute(
                text("""
            SELECT request_sha256,response_json FROM idempotency_keys
            WHERE owner_id=:owner AND route=:route AND idem_key=:key
        """),
                {"owner": owner, "route": route, "key": headers.key},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["request_sha256"] != fingerprint:
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "This key was used with different input"
                )
            return model.model_validate(json.loads(existing["response_json"]))
        result = action(con, auth)
        encoded = result.model_dump_json()
        con.execute(
            text("""
            INSERT INTO idempotency_keys(id,owner_id,route,idem_key,request_sha256,
                response_status,response_json,created_at,expires_at)
            VALUES (:id,:owner,:route,:key,:hash,:status,:response,:now,:expiry)
        """),
            {
                "id": str(uuid4()),
                "owner": owner,
                "route": route,
                "key": headers.key,
                "hash": fingerprint,
                "status": status,
                "response": encoded,
                "now": stamp(),
                "expiry": stamp(now() + timedelta(days=30)),
            },
        )
        return result


def check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApiError(412, "PRECONDITION_FAILED", "This record changed; reload before editing")
