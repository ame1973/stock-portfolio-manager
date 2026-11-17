"""Financial writes must retain their idempotency response in the same transaction."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.cash_routes as cash_routes
import spm_api.mutations as mutation_module
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.mutations import mutate
from spm_api.security import Auth
from tests.helpers import database_snapshot, write_headers
from tests.test_cash import cash_body
from tests.test_cash import wallet as wallet

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "fault",
    [
        "ignored",
        "missing",
        "response",
        "request",
        "status",
        "created",
        "expires",
        "id",
        "route",
        "key",
        "owner",
    ],
)
async def test_corrupt_idempotency_insert_never_commits_cash_or_audit(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], fault: str
) -> None:
    route = "POST /api/v1/cash-entries"
    foreign_owner = str(uuid4())
    if fault == "ignored":
        trigger = (
            "CREATE TRIGGER synthetic_ignore_idempotency BEFORE INSERT ON idempotency_keys "
            f"WHEN NEW.route='{route}' BEGIN SELECT RAISE(IGNORE); END"
        )
    else:
        if fault == "missing":
            statement = "DELETE FROM idempotency_keys WHERE id=NEW.id"
        else:
            field, value = {
                "response": ("response_json", "'{}'"),
                "request": ("request_sha256", "'" + "0" * 64 + "'"),
                "status": ("response_status", "202"),
                "created": ("created_at", "'2000-01-01T00:00:00.000000Z'"),
                "expires": ("expires_at", "'2000-01-01T00:00:00.000000Z'"),
                "id": ("id", f"'{uuid4()}'"),
                "route": ("route", "'POST /synthetic-other-route'"),
                "key": ("idem_key", "'synthetic-other-idempotency-key'"),
                "owner": ("owner_id", f"'{foreign_owner}'"),
            }[fault]
            statement = f"UPDATE idempotency_keys SET {field}={value} WHERE id=NEW.id"
        trigger = (
            "CREATE TRIGGER synthetic_corrupt_idempotency AFTER INSERT ON idempotency_keys "
            f"WHEN NEW.route='{route}' BEGIN {statement}; END"
        )
    with unit_of_work(engine, write=True) as con:
        if fault == "owner":
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
                (foreign_owner,),
            )
        con.exec_driver_sql(trigger)
    before = database_snapshot(engine)
    response = await client.post(
        "/api/v1/cash-entries",
        json=cash_body(wallet),
        headers=write_headers(client, key=f"synthetic-idempotency-{fault}"),
    )
    assert response.status_code == 503
    assert response.json()["code"] == "IDEMPOTENCY_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("reject", [False, True])
async def test_commit_validator_runs_after_idempotency_insert_inside_the_writer(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], reject: bool
) -> None:
    headers = write_headers(client, key="synthetic-idempotency-commit-check")
    payload = cash_body(wallet)
    called: list[str] = []

    def check(con: Connection, auth: Auth, result: Any) -> None:
        row = con.exec_driver_sql(
            "SELECT request_sha256 FROM idempotency_keys "
            "WHERE owner_id=? AND route=? AND idem_key=?",
            (str(auth.owner.id), "POST /api/v1/cash-entries", headers["Idempotency-Key"]),
        ).one()
        assert len(row.request_sha256) == 64
        called.append(str(result.id))
        if reject:
            raise ApiError(409, "SYNTHETIC_SCOPE_CHANGED", "Synthetic scope changed")

    def with_check(*args: Any, **kwargs: Any) -> Any:
        return mutate(*args, **kwargs, validate_commit=check)

    before = database_snapshot(engine)
    with patch.object(cash_routes, "mutate", side_effect=with_check):
        response = await client.post("/api/v1/cash-entries", json=payload, headers=headers)
        assert len(called) == 1
        if reject:
            assert response.status_code == 409
            assert response.json()["code"] == "SYNTHETIC_SCOPE_CHANGED"
            assert database_snapshot(engine) == before
        else:
            assert response.status_code == 201
            after = database_snapshot(engine)
            replay = await client.post("/api/v1/cash-entries", json=payload, headers=headers)
            assert replay.status_code == 201 and replay.json() == response.json()
            assert len(called) == 1 and database_snapshot(engine) == after


@pytest.mark.parametrize(
    "failure", ["revoked", "csrf", "reauth", "owner", "session-id", "session-owner"]
)
async def test_commit_validator_rechecks_session_after_the_marker_is_inserted(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], failure: str
) -> None:
    owner = wallet["owner"]["id"]
    foreign = str(uuid4())
    statement = {
        "revoked": "UPDATE sessions SET revoked_at='2026-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{owner}'",
        "csrf": "UPDATE sessions SET csrf_sha256='" + "0" * 64 + f"' WHERE owner_id='{owner}'",
        "reauth": "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{owner}'",
        "owner": f"UPDATE owners SET is_active=0 WHERE id='{owner}'",
        "session-id": f"UPDATE sessions SET id='{uuid4()}' WHERE owner_id='{owner}'",
        "session-owner": f"UPDATE sessions SET owner_id='{foreign}' WHERE owner_id='{owner}'",
    }[failure]
    with unit_of_work(engine, write=True) as con:
        if failure == "session-owner":
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
                (foreign,),
            )
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_change_idempotency_session AFTER INSERT ON idempotency_keys "
            "WHEN NEW.route='POST /api/v1/cash-entries' "
            f"BEGIN {statement}; END"
        )
    checked: list[bool] = []

    def check(con: Connection, auth: Auth, result: Any) -> None:
        checked.append(True)

    def with_check(*args: Any, **kwargs: Any) -> Any:
        return mutate(*args, **kwargs, reauth=True, validate_commit=check)

    before = database_snapshot(engine)
    with patch.object(cash_routes, "mutate", side_effect=with_check):
        response = await client.post(
            "/api/v1/cash-entries", json=cash_body(wallet), headers=write_headers(client)
        )
    expected = (
        401
        if failure in {"revoked", "owner"}
        else 503
        if failure in {"session-id", "session-owner"}
        else 403
    )
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    assert checked == [] and database_snapshot(engine) == before


async def test_idempotency_expiry_uses_the_same_clock_sample_as_creation(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    headers = write_headers(client, key="synthetic-idempotency-clock-rollback")
    with patch.object(mutation_module, "now", return_value=datetime(2020, 1, 1, tzinfo=UTC)):
        response = await client.post(
            "/api/v1/cash-entries", json=cash_body(wallet), headers=headers
        )
    assert response.status_code == 201, response.text
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT created_at,expires_at FROM idempotency_keys WHERE owner_id=? AND idem_key=?",
            (wallet["owner"]["id"], headers["Idempotency-Key"]),
        ).one()
    created = datetime.fromisoformat(row.created_at.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    assert expires - created == timedelta(days=30)


async def test_verified_idempotency_record_replays_once_without_writes(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    headers = write_headers(client, key="synthetic-verified-idempotency")
    payload = cash_body(wallet, amount="123.450001")
    response = await client.post("/api/v1/cash-entries", json=payload, headers=headers)
    assert response.status_code == 201
    after = database_snapshot(engine)
    replay = await client.post("/api/v1/cash-entries", json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == response.json()
    assert database_snapshot(engine) == after
