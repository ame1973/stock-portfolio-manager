"""Opaque sessions, independent CSRF secrets, bootstrap tokens, and durable login limits."""

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from threading import BoundedSemaphore
from typing import Annotated
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, Request, Response, Security
from fastapi.security import APIKeyCookie
from sqlalchemy import Connection, Engine, text

from spm_api.audit import digest, now, stamp
from spm_api.auth_models import Owner
from spm_api.db import unit_of_work
from spm_api.errors import ApiError

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
# ponytail: bound Argon2 memory to 128 MiB per API process; use one Uvicorn process on a NAS.
PASSWORD_SLOTS = BoundedSemaphore(2)
SESSION_AGE = timedelta(days=30)
IDLE_AGE = timedelta(days=7)
REAUTH_AGE = timedelta(minutes=5)
COOKIE = "spm_session"
CSRF_COOKIE = "spm_csrf"
COOKIE_AUTH = APIKeyCookie(name=COOKIE, scheme_name="cookieAuth", auto_error=False)


@dataclass(frozen=True)
class Auth:
    owner: Owner
    session_id: str
    csrf_sha256: str
    expires_at: str
    last_seen_at: str
    reauthenticated_at: str | None


@dataclass(frozen=True)
class WriteHeaders:
    auth: Auth
    csrf: str
    key: str


def require_transport(request: Request) -> None:
    if (
        request.app.state.settings.public_origin.startswith("https:")
        and request.url.scheme != "https"
    ):
        raise ApiError(403, "HTTPS_REQUIRED", "Use the configured HTTPS address")


def require_origin(request: Request) -> None:
    require_transport(request)
    if request.headers.get("origin") != request.app.state.settings.public_origin:
        raise ApiError(403, "ORIGIN_REJECTED", "A matching Origin header is required")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError(403, "ORIGIN_REJECTED", "Cross-site requests are not permitted")


def read_auth(con: Connection, token: str | None) -> Auth:
    if token is None or len(token) > 200:
        raise ApiError(401, "UNAUTHORIZED", "Sign in to continue")
    row = (
        con.execute(
            text("""
        SELECT s.*, o.id AS owner_id, o.created_at AS owner_created_at, o.is_active
        FROM sessions s JOIN owners o ON o.id=s.owner_id
        WHERE s.token_sha256=:hash
    """),
            {"hash": digest(token)},
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or not row["is_active"]
        or row["revoked_at"] is not None
        or row["expires_at"] <= stamp(now())
        or row["last_seen_at"] <= stamp(now() - IDLE_AGE)
    ):
        raise ApiError(401, "UNAUTHORIZED", "Sign in to continue")
    return Auth(
        owner=Owner(id=row["owner_id"], created_at=row["owner_created_at"], is_active=True),
        session_id=row["id"],
        csrf_sha256=row["csrf_sha256"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
        reauthenticated_at=row["reauthenticated_at"],
    )


def authenticated(request: Request, token: Annotated[str | None, Security(COOKIE_AUTH)]) -> Auth:
    require_transport(request)
    with unit_of_work(request.app.state.engine) as con:
        auth = read_auth(con, token)
    if auth.last_seen_at < stamp(now() - timedelta(minutes=5)):
        with unit_of_work(request.app.state.engine, write=True) as con:
            auth = read_auth(con, token)
            con.execute(
                text("UPDATE sessions SET last_seen_at=:now WHERE id=:id"),
                {
                    "now": stamp(),
                    "id": auth.session_id,
                },
            )
    return auth


AuthDep = Annotated[Auth, Depends(authenticated)]


def validate_csrf(auth: Auth, value: str) -> None:
    if not hmac.compare_digest(auth.csrf_sha256, digest(value)):
        raise ApiError(403, "CSRF_REJECTED", "Refresh the CSRF token and retry")


def write_headers(
    request: Request,
    auth: AuthDep,
    csrf: Annotated[str, Header(alias="X-CSRF-Token", min_length=16, max_length=200)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=200)],
) -> WriteHeaders:
    require_origin(request)
    validate_csrf(auth, csrf)
    return WriteHeaders(auth, csrf, key)


WriteDep = Annotated[WriteHeaders, Depends(write_headers)]


def matching_version(
    value: Annotated[str, Header(alias="If-Match", pattern=r'^"[1-9][0-9]*"$')],
) -> int:
    return int(value[1:-1])


VersionDep = Annotated[int, Depends(matching_version)]


def matching_dividend_event_version(
    value: Annotated[str, Header(alias="If-Match", max_length=12, pattern=r'^"[1-9][0-9]{0,9}"$')],
) -> int:
    if re.fullmatch(r'"[1-9][0-9]{0,9}"', value) is None:
        raise ApiError(422, "INVALID_EVENT_VERSION", "The event version is invalid")
    number = int(value[1:-1])
    if number > 2_147_483_647:
        raise ApiError(422, "INVALID_EVENT_VERSION", "The event version is invalid")
    return number


DividendEventVersionDep = Annotated[int, Depends(matching_dividend_event_version)]


def require_reauth(auth: Auth) -> None:
    if auth.reauthenticated_at is None or auth.reauthenticated_at <= stamp(now() - REAUTH_AGE):
        raise ApiError(403, "REAUTH_REQUIRED", "Confirm your password again")


def hash_password(password: str) -> str:
    with PASSWORD_SLOTS:
        return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    with PASSWORD_SLOTS:
        try:
            return PASSWORD_HASHER.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False


def issue_setup_token(engine: Engine, *, rotate: bool = False) -> str | None:
    with unit_of_work(engine, write=True) as con:
        if con.execute(text("SELECT 1 FROM owners LIMIT 1")).first() is not None:
            return None
        previous = con.execute(text("SELECT expires_at FROM bootstrap_tokens")).scalar()
        if previous and previous > stamp() and not rotate:
            return None
        token = secrets.token_urlsafe(32)
        con.execute(
            text("""
            INSERT INTO bootstrap_tokens VALUES (1,:hash,:now,:expiry)
            ON CONFLICT(singleton) DO UPDATE SET token_sha256=excluded.token_sha256,
              created_at=excluded.created_at, expires_at=excluded.expires_at
        """),
            {"hash": digest(token), "now": stamp(), "expiry": stamp(now() + timedelta(minutes=10))},
        )
        return token


def validate_setup_token(con: Connection, token: str) -> None:
    if con.execute(text("SELECT 1 FROM owners LIMIT 1")).first():
        raise ApiError(409, "ALREADY_INITIALIZED", "An owner has already been initialized")
    row = con.execute(text("SELECT * FROM bootstrap_tokens WHERE singleton=1")).mappings().first()
    if (
        row is None
        or row["expires_at"] <= stamp()
        or not hmac.compare_digest(row["token_sha256"], digest(token))
    ):
        raise ApiError(401, "INVALID_SETUP_TOKEN", "The setup token is invalid or expired")


def issue_session(con: Connection, owner_id: str, user_agent: str) -> tuple[str, str, str]:
    token, csrf, session_id = secrets.token_urlsafe(32), secrets.token_urlsafe(32), str(uuid4())
    con.execute(
        text("""
        INSERT INTO sessions(id,owner_id,token_sha256,csrf_sha256,created_at,last_seen_at,
                             expires_at,user_agent_hash,reauthenticated_at)
        VALUES (:id,:owner,:token,:csrf,:now,:now,:expiry,:agent,:now)
    """),
        {
            "id": session_id,
            "owner": owner_id,
            "token": digest(token),
            "csrf": digest(csrf),
            "now": stamp(),
            "expiry": stamp(now() + SESSION_AGE),
            "agent": digest(user_agent[:1000]),
        },
    )
    return token, csrf, session_id


def set_cookies(request: Request, response: Response, token: str, csrf: str) -> None:
    for name, value, private in ((COOKIE, token, True), (CSRF_COOKIE, csrf, False)):
        response.set_cookie(
            name,
            value,
            max_age=int(SESSION_AGE.total_seconds()),
            secure=request.app.state.settings.public_origin.startswith("https:"),
            httponly=private,
            samesite="strict",
            path="/",
        )


def clear_cookies(request: Request, response: Response) -> None:
    for name, private in ((COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(
            name,
            path="/",
            samesite="strict",
            httponly=private,
            secure=request.app.state.settings.public_origin.startswith("https:"),
        )


def attempt_bucket(request: Request, action: str) -> str:
    # Trust only the ASGI server's configured proxy chain, not arbitrary forwarded headers here.
    address = request.client.host if request.client else "unknown"
    return digest(f"{action}:{address}")


def reserve_attempt(request: Request, action: str) -> str:
    """Reserve attempts before hashing; failures cannot roll back the rate limiter."""
    bucket = attempt_bucket(request, action)
    current = int(now().timestamp())
    blocked = False
    with unit_of_work(request.app.state.engine, write=True) as con:
        con.execute(
            text("DELETE FROM auth_rate_limits WHERE window_start < :cutoff"),
            {
                "cutoff": current - 900,
            },
        )
        for key, maximum in ((bucket, 10), (digest(f"global:{action}"), 100)):
            row = (
                con.execute(
                    text("SELECT * FROM auth_rate_limits WHERE bucket_sha256=:key"),
                    {
                        "key": key,
                    },
                )
                .mappings()
                .first()
            )
            if row is not None and (row["blocked_until"] > current or row["attempts"] >= maximum):
                blocked = True
                continue
            con.execute(
                text("""
                INSERT INTO auth_rate_limits(bucket_sha256,window_start,attempts)
                VALUES (:key,:now,1)
                ON CONFLICT(bucket_sha256) DO UPDATE SET attempts=attempts+1
            """),
                {"key": key, "now": current},
            )
    if blocked:
        raise ApiError(429, "RATE_LIMITED", "Too many authentication attempts; retry later")
    return bucket


def record_failure(engine: Engine, bucket: str) -> None:
    with unit_of_work(engine, write=True) as con:
        failures = con.execute(
            text("SELECT failures FROM auth_rate_limits WHERE bucket_sha256=:key"),
            {
                "key": bucket,
            },
        ).scalar_one()
        delay = min(2 ** min(failures, 6), 60)
        con.execute(
            text("""
            UPDATE auth_rate_limits SET failures=failures+1, blocked_until=:until
            WHERE bucket_sha256=:key
        """),
            {"key": bucket, "until": int(now().timestamp()) + delay},
        )
