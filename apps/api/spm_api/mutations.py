"""Atomic, owner-scoped idempotent writes shared by all state-changing services."""

import hmac
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import Request
from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import canonical, digest, now, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Model
from spm_api.security import COOKIE, Auth, WriteHeaders, read_auth, require_reauth, validate_csrf


def sensitive_fingerprint(request: Request, payload: Any) -> str:
    # Password/key fingerprints must not become fast unkeyed offline guessing oracles.
    return hmac.new(
        request.cookies[COOKIE].encode(), canonical(payload).encode(), "sha256"
    ).hexdigest()


def _checked_request(
    con: Connection, request: Request, headers: WriteHeaders, fingerprint: str, *, reauth: bool
) -> tuple[Auth, RowMapping | None]:
    auth = read_auth(con, request.cookies.get(COOKIE))
    validate_csrf(auth, headers.csrf)
    if reauth:
        require_reauth(auth)
    existing = (
        con.execute(
            text("""
        SELECT request_sha256,response_json FROM idempotency_keys
        WHERE owner_id=:owner AND route=:route AND idem_key=:key
    """),
            {
                "owner": str(auth.owner.id),
                "route": f"{request.method} {request.url.path}",
                "key": headers.key,
            },
        )
        .mappings()
        .first()
    )
    if existing is not None and existing["request_sha256"] != fingerprint:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "This key was used with different input")
    return auth, existing


def has_replay(
    request: Request, headers: WriteHeaders, payload: Any, *, reauth: bool = False
) -> bool:
    """Advisory read before private preparation; mutate must recheck before returning or writing."""
    with unit_of_work(request.app.state.engine) as con:
        return (
            _checked_request(con, request, headers, digest(canonical(payload)), reauth=reauth)[1]
            is not None
        )


def mutate[M: Model](
    request: Request,
    headers: WriteHeaders,
    payload: Any,
    model: type[M],
    action: Callable[[Connection, Auth], M],
    *,
    status: int = 200,
    reauth: bool = False,
    validate_replay: Callable[[Connection, Auth, M], None] | None = None,
    validate_commit: Callable[[Connection, Auth, M], None] | None = None,
) -> M:
    """Verify idempotency before commit; an optional final validator must be read-only."""
    route = f"{request.method} {request.url.path}"
    fingerprint = digest(canonical(payload))
    with unit_of_work(request.app.state.engine, write=True) as con:
        auth, existing = _checked_request(con, request, headers, fingerprint, reauth=reauth)
        owner = str(auth.owner.id)
        if existing is not None:
            result = model.model_validate(json.loads(existing["response_json"]))
            if validate_replay is not None:
                validate_replay(con, auth, result)
            return result
        result = action(con, auth)
        encoded = result.model_dump_json()
        created = now()
        values = {
            "id": str(uuid4()),
            "owner": owner,
            "route": route,
            "key": headers.key,
            "hash": fingerprint,
            "status": status,
            "response": encoded,
            "now": stamp(created),
            "expiry": stamp(created + timedelta(days=30)),
        }
        inserted = con.execute(
            text("""
            INSERT INTO idempotency_keys(id,owner_id,route,idem_key,request_sha256,
                response_status,response_json,created_at,expires_at)
            VALUES (:id,:owner,:route,:key,:hash,:status,:response,:now,:expiry)
        """),
            values,
        )
        retained = con.execute(
            text("""
            SELECT owner_id,route,idem_key,request_sha256,response_status,response_json,
                   created_at,expires_at FROM idempotency_keys WHERE id=:id
        """),
            {"id": values["id"]},
        ).one_or_none()
        if (
            inserted.rowcount != 1
            or retained is None
            or type(retained.response_status) is not int
            or tuple(retained)
            != (
                owner,
                route,
                headers.key,
                fingerprint,
                status,
                encoded,
                values["now"],
                values["expiry"],
            )
        ):
            raise ApiError(
                503, "IDEMPOTENCY_UNAVAILABLE", "The transaction cannot be recorded safely"
            )
        if validate_commit is not None:
            current, current_key = _checked_request(
                con, request, headers, fingerprint, reauth=reauth
            )
            if (
                current_key is None
                or current.owner.id != auth.owner.id
                or current.session_id != auth.session_id
            ):
                raise ApiError(
                    503, "IDEMPOTENCY_UNAVAILABLE", "The transaction cannot be recorded safely"
                )
            validate_commit(con, current, result)
        return result


def check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ApiError(412, "PRECONDITION_FAILED", "This record changed; reload before editing")
