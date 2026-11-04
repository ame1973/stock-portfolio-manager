"""Shared mutation invariants: one commit, deterministic replay, no stale-session writes."""

from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from fastapi import Request
from sqlalchemy import Connection, Engine, text

from spm_api.audit import audit
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Success
from spm_api.main import create_app
from spm_api.mutations import check_version, mutate
from spm_api.security import COOKIE, CSRF_COOKIE, Auth, WriteHeaders, read_auth
from tests.helpers import initialize

pytestmark = pytest.mark.anyio


def context(
    client: httpx.AsyncClient, engine: Engine, settings: Settings
) -> tuple[Request, WriteHeaders]:
    app = create_app(settings)
    app.state.engine = engine
    token = str(client.cookies.get(COOKIE))
    with unit_of_work(engine) as con:
        auth = read_auth(con, token)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/synthetic-mutation",
            "query_string": b"",
            "server": ("testserver", 80),
            "app": app,
            "headers": [(b"cookie", f"{COOKIE}={token}".encode())],
            "state": {"request_id": str(uuid4())},
        }
    )
    return request, WriteHeaders(auth, str(client.cookies.get(CSRF_COOKIE)), str(uuid4()))


def portfolio_action(name: str) -> Callable[[Connection, Auth], Success]:
    def action(con: Connection, auth: Auth) -> Success:
        identifier = str(uuid4())
        con.execute(
            text("""
            INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (:id,:owner,:name,'now')
        """),
            {"id": identifier, "owner": str(auth.owner.id), "name": name},
        )
        audit(con, str(auth.owner.id), "portfolio", identifier, "CREATE", after={"name": name})
        return Success(message=name)

    return action


async def test_duplicate_is_replayed_and_key_body_mismatch_is_rejected(
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
) -> None:
    await initialize(client, engine)
    request, headers = context(client, engine, settings)
    action = portfolio_action("Synthetic")
    assert mutate(request, headers, {"name": "Synthetic"}, Success, action).message == "Synthetic"
    assert mutate(request, headers, {"name": "Synthetic"}, Success, action).message == "Synthetic"
    with pytest.raises(ApiError) as conflict:
        mutate(request, headers, {"name": "Different"}, Success, action)
    assert conflict.value.status == 409
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM portfolios")).scalar_one() == 1
        assert con.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 1
        assert (
            con.execute(
                text("SELECT count(*) FROM audit_events WHERE entity_type='portfolio'")
            ).scalar_one()
            == 1
        )


async def test_failure_rolls_back_entity_audit_and_idempotency(
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
) -> None:
    await initialize(client, engine)
    request, headers = context(client, engine, settings)

    def action(con: Connection, auth: Auth) -> Success:
        portfolio_action("Rollback")(con, auth)
        raise ApiError(409, "INJECTED", "Injected failure after audit")

    with pytest.raises(ApiError, match="Injected failure"):
        mutate(request, headers, {}, Success, action)
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM portfolios")).scalar_one() == 0
        assert con.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 0
        assert (
            con.execute(
                text("SELECT count(*) FROM audit_events WHERE entity_type='portfolio'")
            ).scalar_one()
            == 0
        )


async def test_revocation_between_dependency_and_write_is_rechecked(
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
) -> None:
    await initialize(client, engine)
    request, headers = context(client, engine, settings)
    with unit_of_work(engine, write=True) as con:
        con.execute(text("UPDATE sessions SET revoked_at='now'"))
    with pytest.raises(ApiError) as rejected:
        mutate(request, headers, {}, Success, portfolio_action("Forbidden"))
    assert rejected.value.status == 401
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM portfolios")).scalar_one() == 0


async def test_schema_mismatch_blocks_mutation(
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
) -> None:
    await initialize(client, engine)
    request, headers = context(client, engine, settings)
    with engine.begin() as con:
        con.exec_driver_sql("UPDATE alembic_version SET version_num='older-schema'")
    with pytest.raises(ApiError) as rejected:
        mutate(request, headers, {}, Success, portfolio_action("Forbidden"))
    assert rejected.value.status == 503 and rejected.value.code == "MIGRATION_REQUIRED"


@pytest.mark.parametrize(("actual", "expected"), [(1, 2), (2, 1), (10, 9)])
async def test_stale_row_version_is_not_overwritten(actual: int, expected: int) -> None:
    with pytest.raises(ApiError) as stale:
        check_version(actual, expected)
    assert stale.value.status == 412
    check_version(actual, actual)
