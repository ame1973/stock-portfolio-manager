"""Owner initialization, revocable sessions, password confirmation, and session management."""

import hmac
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import Connection, text

from spm_api.audit import audit, digest, stamp
from spm_api.auth_models import (
    ChangePassword,
    CsrfToken,
    Login,
    Owner,
    Session,
    SessionPage,
    SetupInitialize,
    SetupStatus,
)
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error, Success
from spm_api.mutations import mutate, sensitive_fingerprint
from spm_api.security import (
    COOKIE,
    CSRF_COOKIE,
    PASSWORD_HASHER,
    Auth,
    AuthDep,
    WriteDep,
    clear_cookies,
    hash_password,
    issue_session,
    read_auth,
    record_failure,
    require_origin,
    reserve_attempt,
    set_cookies,
    validate_setup_token,
    verify_password,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Auth"],
    responses={code: {"model": Error} for code in (400, 401, 403, 404, 409, 412, 422, 429, 503)},
)
PublicGuard = Annotated[None, Depends(require_origin)]


@router.get("/setup/status", operation_id="get_setup_status")
def setup_status(request: Request) -> SetupStatus:
    with unit_of_work(request.app.state.engine) as con:
        initialized = con.execute(text("SELECT 1 FROM owners LIMIT 1")).first() is not None
    return SetupStatus(initialized=initialized, requires_token=not initialized)


@router.post("/setup/initialize", operation_id="initialize_owner")
def initialize_owner(
    body: SetupInitialize,
    request: Request,
    response: Response,
    guard: PublicGuard,
) -> Owner:
    bucket = reserve_attempt(request, "setup")
    token = body.setup_token.get_secret_value()
    try:
        with unit_of_work(request.app.state.engine) as con:
            validate_setup_token(con, token)
    except ApiError:
        record_failure(request.app.state.engine, bucket)
        raise
    encoded = hash_password(body.password.get_secret_value())
    with unit_of_work(request.app.state.engine, write=True) as con:
        validate_setup_token(con, token)
        owner = Owner(id=uuid4(), created_at=stamp(), is_active=True)
        con.execute(
            text("""
            INSERT INTO owners(id,password_hash,password_changed_at,created_at)
            VALUES (:id,:hash,:now,:now)
        """),
            {"id": str(owner.id), "hash": encoded, "now": owner.created_at},
        )
        session, csrf, session_id = issue_session(
            con, str(owner.id), request.headers.get("user-agent", "")
        )
        audit(
            con,
            str(owner.id),
            "owner",
            str(owner.id),
            "INITIALIZE",
            request_id=request.state.request_id,
        )
        audit(
            con, str(owner.id), "session", session_id, "LOGIN", request_id=request.state.request_id
        )
        con.execute(text("DELETE FROM bootstrap_tokens"))
    set_cookies(request, response, session, csrf)
    return owner


@router.post("/auth/login", operation_id="login")
def login(body: Login, request: Request, response: Response, guard: PublicGuard) -> Owner:
    bucket = reserve_attempt(request, "login")
    with unit_of_work(request.app.state.engine) as con:
        row = con.execute(text("SELECT * FROM owners WHERE is_active=1 LIMIT 1")).mappings().first()
    if row is None or not verify_password(row["password_hash"], body.password.get_secret_value()):
        record_failure(request.app.state.engine, bucket)
        raise ApiError(401, "UNAUTHORIZED", "The password is incorrect")
    encoded = row["password_hash"]
    replacement = (
        hash_password(body.password.get_secret_value())
        if PASSWORD_HASHER.check_needs_rehash(encoded)
        else encoded
    )
    owner = Owner(id=row["id"], created_at=row["created_at"], is_active=True)
    with unit_of_work(request.app.state.engine, write=True) as con:
        current = con.execute(
            text("SELECT password_hash FROM owners WHERE id=:id AND is_active=1"),
            {
                "id": str(owner.id),
            },
        ).scalar()
        if current != encoded:
            raise ApiError(401, "UNAUTHORIZED", "The password changed; sign in again")
        if replacement != encoded:
            con.execute(
                text("UPDATE owners SET password_hash=:hash WHERE id=:id"),
                {
                    "hash": replacement,
                    "id": str(owner.id),
                },
            )
        session, csrf, session_id = issue_session(
            con, str(owner.id), request.headers.get("user-agent", "")
        )
        audit(
            con, str(owner.id), "session", session_id, "LOGIN", request_id=request.state.request_id
        )
    set_cookies(request, response, session, csrf)
    return owner


@router.get("/auth/me", operation_id="get_me")
def me(auth: AuthDep) -> Owner:
    return auth.owner


@router.get("/auth/csrf", operation_id="get_csrf_token")
def csrf_token(request: Request, response: Response, auth: AuthDep) -> CsrfToken:
    csrf = request.cookies.get(CSRF_COOKIE, "")
    if not csrf or not hmac.compare_digest(auth.csrf_sha256, digest(csrf)):
        csrf = secrets.token_urlsafe(32)
        with unit_of_work(request.app.state.engine, write=True) as con:
            current = read_auth(con, request.cookies.get(COOKIE))
            con.execute(
                text("UPDATE sessions SET csrf_sha256=:hash WHERE id=:id"),
                {
                    "hash": digest(csrf),
                    "id": current.session_id,
                },
            )
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            secure=request.app.state.settings.public_origin.startswith("https:"),
            httponly=False,
            samesite="strict",
            path="/",
        )
    return CsrfToken(csrf_token=csrf, expires_at=auth.expires_at)


@router.post("/auth/logout", operation_id="logout")
def logout(request: Request, response: Response, headers: WriteDep) -> Success:
    def action(con: Connection, auth: Auth) -> Success:
        con.execute(
            text("UPDATE sessions SET revoked_at=:now WHERE id=:id"),
            {
                "now": stamp(),
                "id": auth.session_id,
            },
        )
        audit(
            con,
            str(auth.owner.id),
            "session",
            auth.session_id,
            "LOGOUT",
            request_id=request.state.request_id,
        )
        return Success()

    result = mutate(request, headers, {}, Success, action)
    clear_cookies(request, response)
    return result


def confirm_password(request: Request, auth: Auth, password: str) -> str:
    bucket = reserve_attempt(request, "confirm-password")
    with unit_of_work(request.app.state.engine) as con:
        encoded = con.execute(
            text("SELECT password_hash FROM owners WHERE id=:id"),
            {
                "id": str(auth.owner.id),
            },
        ).scalar_one()
    if not verify_password(encoded, password):
        record_failure(request.app.state.engine, bucket)
        raise ApiError(401, "UNAUTHORIZED", "The password is incorrect")
    return str(encoded)


@router.post("/auth/reauth", operation_id="reauthenticate")
def reauthenticate(body: Login, request: Request, headers: WriteDep) -> Success:
    encoded = confirm_password(request, headers.auth, body.password.get_secret_value())

    def action(con: Connection, auth: Auth) -> Success:
        current = con.execute(
            text("SELECT password_hash FROM owners WHERE id=:id"),
            {
                "id": str(auth.owner.id),
            },
        ).scalar_one()
        if current != encoded:
            raise ApiError(401, "UNAUTHORIZED", "The password changed; sign in again")
        con.execute(
            text("UPDATE sessions SET reauthenticated_at=:now WHERE id=:id"),
            {
                "now": stamp(),
                "id": auth.session_id,
            },
        )
        audit(
            con,
            str(auth.owner.id),
            "session",
            auth.session_id,
            "REAUTHENTICATE",
            request_id=request.state.request_id,
        )
        return Success()

    return mutate(
        request,
        headers,
        sensitive_fingerprint(request, {"password": body.password.get_secret_value()}),
        Success,
        action,
    )


@router.post("/auth/change-password", operation_id="change_password")
def change_password(
    body: ChangePassword,
    request: Request,
    response: Response,
    headers: WriteDep,
) -> Success:
    old = body.old_password.get_secret_value()
    new = body.new_password.get_secret_value()
    encoded = confirm_password(request, headers.auth, old)
    replacement = hash_password(new)

    def action(con: Connection, auth: Auth) -> Success:
        changed = con.execute(
            text("""
            UPDATE owners SET password_hash=:replacement,password_changed_at=:now
            WHERE id=:id AND password_hash=:old
        """),
            {"replacement": replacement, "now": stamp(), "id": str(auth.owner.id), "old": encoded},
        )
        if changed.rowcount != 1:
            raise ApiError(401, "UNAUTHORIZED", "The password changed; sign in again")
        con.execute(
            text(
                "UPDATE sessions SET revoked_at=:now WHERE owner_id=:owner AND revoked_at IS NULL"
            ),
            {
                "now": stamp(),
                "owner": str(auth.owner.id),
            },
        )
        audit(
            con,
            str(auth.owner.id),
            "owner",
            str(auth.owner.id),
            "CHANGE_PASSWORD",
            request_id=request.state.request_id,
        )
        return Success(message="Password changed; all sessions have been revoked")

    result = mutate(
        request,
        headers,
        sensitive_fingerprint(request, {"old_password": old, "new_password": new}),
        Success,
        action,
    )
    clear_cookies(request, response)
    return result


@router.get("/auth/sessions", operation_id="list_sessions")
def list_sessions(
    request: Request,
    auth: AuthDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
) -> SessionPage:
    with unit_of_work(request.app.state.engine) as con:
        rows = (
            con.execute(
                text("""
            SELECT * FROM sessions WHERE owner_id=:owner AND (:cursor IS NULL OR id<:cursor)
            ORDER BY id DESC LIMIT :limit
        """),
                {
                    "owner": str(auth.owner.id),
                    "cursor": str(cursor) if cursor else None,
                    "limit": limit + 1,
                },
            )
            .mappings()
            .all()
        )
    return SessionPage(
        items=[
            Session(
                id=row["id"],
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
                expires_at=row["expires_at"],
                revoked_at=row["revoked_at"],
                current=row["id"] == auth.session_id,
            )
            for row in rows[:limit]
        ],
        next_cursor=rows[limit - 1]["id"] if len(rows) > limit else None,
    )


@router.delete("/auth/sessions/{id}", operation_id="revoke_session")
def revoke_session(id: UUID, request: Request, response: Response, headers: WriteDep) -> Success:
    def action(con: Connection, auth: Auth) -> Success:
        changed = con.execute(
            text("""
            UPDATE sessions SET revoked_at=COALESCE(revoked_at,:now)
            WHERE id=:id AND owner_id=:owner
        """),
            {"now": stamp(), "id": str(id), "owner": str(auth.owner.id)},
        )
        if changed.rowcount != 1:
            raise ApiError(404, "NOT_FOUND", "Session not found")
        audit(
            con,
            str(auth.owner.id),
            "session",
            str(id),
            "REVOKE",
            request_id=request.state.request_id,
        )
        return Success()

    result = mutate(request, headers, {}, Success, action)
    if str(id) == headers.auth.session_id:
        clear_cookies(request, response)
    return result
