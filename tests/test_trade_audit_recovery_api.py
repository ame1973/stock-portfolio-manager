"""Protected recovery exposes database audit consistency, never verified ownership."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.trade_routes as routes
from spm_api import trade_service
from spm_api.audit import canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from spm_api.trade_models import TradeAuditOriginRecord
from tests.helpers import PASSWORD, create, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_origin_migration import capture_values, origin_values
from tests.test_trade_audit_recovery import REASON, legacy_trade
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def path(identifier: str) -> str:
    return f"/api/v1/trades/{identifier}/audit-origin"


def body(original: dict[str, Any]) -> dict[str, Any]:
    return {"original_response": original, "reason": REASON}


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "private-untrusted" not in response.text


async def test_owned_origin_read_distinguishes_missing_from_retained_without_adoption(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    current = await create(client, "trades", payload(book))
    original = await legacy_trade(client, book)
    before = database_snapshot(engine)
    retained = await client.get(path(current["id"]))
    missing = await client.get(path(original["id"]))
    assert retained.status_code == missing.status_code == 200
    safe(retained)
    safe(missing)
    assert retained.json() == {
        "trade_id": current["id"],
        "trade_version": 1,
        "trade_status": "ACTIVE",
        "origin": "AT_CREATION",
        "captured_at": retained.json()["captured_at"],
        "scope": "DATABASE_AUDIT_ORIGIN_ONLY",
        "history_completeness": "UNVERIFIED",
    }
    assert missing.json() == {
        "trade_id": original["id"],
        "trade_version": 2,
        "trade_status": "SUPERSEDED",
        "origin": "MISSING",
        "captured_at": None,
        "scope": "DATABASE_AUDIT_ORIGIN_ONLY",
        "history_completeness": "UNVERIFIED",
    }
    assert database_snapshot(engine) == before


async def test_release_failure_returns_private_error_and_keeps_original_retry_available(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    headers = version_headers(client, 2, key="synthetic-origin-release-failure")
    before = database_snapshot(engine)
    with patch.object(
        Connection,
        "_release_savepoint_impl",
        side_effect=SQLAlchemyError("private-untrusted release failure"),
    ):
        response = await client.post(
            path(original["id"]) + "/recover", json=body(original), headers=headers
        )
    assert response.status_code == 503 and response.json()["code"] == "DATABASE_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert retry.status_code == 200
    safe(retry)


async def test_protected_recovery_replays_original_receipt_without_financial_changes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    headers = version_headers(client, 2, key="uncertain-synthetic-origin-recovery")
    before = database_snapshot(engine)
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert result.status_code == 200
    safe(result)
    record = result.json()
    assert record["trade_id"] == original["id"] and record["trade_version"] == 2
    assert (
        record["origin"] == "RETAINED_RESPONSE" and record["history_completeness"] == "UNVERIFIED"
    )
    assert record["scope"] == "DATABASE_AUDIT_ORIGIN_ONLY"
    assert "quantity" not in result.text and "owner_id" not in result.text
    after = database_snapshot(engine)
    changed = {
        "audit_events",
        "trade_audit_origins",
        "portfolios",
        "projection_invalidations",
        "idempotency_keys",
    }
    assert {k: v for k, v in before.items() if k not in changed} == {
        k: v for k, v in after.items() if k not in changed
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT owner_id,request_id FROM audit_events "
            "WHERE action='TRADE_AUDIT_ORIGIN_RECOVERED' AND entity_id=?",
            (original["id"],),
        ).one()
        assert tuple(row) == (book["owner"]["id"], result.headers["x-request-id"])
    replay = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert replay.status_code == 200 and replay.json() == record
    read = await client.get(path(original["id"]))
    assert read.status_code == 200 and read.json() == record
    safe(replay)
    safe(read)
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_security_dependencies_reject_before_recovery(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    original = await legacy_trade(client, book)
    headers = version_headers(client, 2)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "invalid-synthetic-csrf"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        headers["Idempotency-Key"] = "short"
    before = database_snapshot(engine)
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert result.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(result)
    if failure == "cookie":
        read = await client.get(path(original["id"]))
        assert read.status_code == 401
        safe(read)
    assert database_snapshot(engine) == before


async def test_fresh_password_is_required_even_for_a_completed_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    headers = version_headers(client, 2, key="synthetic-reauth-recovery-intent")
    first = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    safe(denied)
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    after_confirmation = database_snapshot(engine)
    replay = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == after_confirmation


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_current_security_is_rechecked_inside_writer_before_new_or_cached_result(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    original = await legacy_trade(client, book)
    headers = version_headers(client, 2)
    if replay:
        assert (
            await client.post(
                path(original["id"]) + "/recover", json=body(original), headers=headers
            )
        ).status_code == 200
    checkpoint: dict[str, Any] = {}

    def changed_after_dependency(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            elif failure == "csrf":
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
            else:
                con.exec_driver_sql(
                    "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'"
                )
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed_after_dependency)
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["before"]


@pytest.mark.parametrize(
    "header", [None, "2", '"0"', '"02"', '"9223372036854775808"', '"' + "9" * 4500 + '"']
)
async def test_if_match_is_bounded_before_integer_conversion(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], header: str | None
) -> None:
    original = await legacy_trade(client, book)
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=headers
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", ["reason", "quantity", "overflow", "extra", "unknown-id"])
async def test_invalid_recovery_input_is_not_echoed_or_written(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], invalid: str
) -> None:
    original = await legacy_trade(client, book)
    data = body(original)
    if invalid == "reason":
        data["reason"] = "private-untrusted\0reason"
    elif invalid == "quantity":
        data["original_response"] = {**original, "quantity": 10.0}
    elif invalid == "overflow":
        data["original_response"] = {**original, "quantity": "92233720368.54775808"}
    elif invalid == "extra":
        data["original_response"] = {**original, "owner_id": "private-untrusted"}
    else:
        data["original_response"] = {**original, "id": "private-untrusted"}
    before = database_snapshot(engine)
    result = await client.post(
        path(original["id"]) + "/recover", json=data, headers=version_headers(client, 2)
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("creation_audit", ["missing", "unrelated"])
async def test_missing_or_unrelated_creation_audit_remains_a_review_requirement(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], creation_audit: str
) -> None:
    original = await legacy_trade(client, book, creation_audit=creation_audit)
    before = database_snapshot(engine)
    read = await client.get(path(original["id"]))
    assert read.status_code == 200 and read.json()["origin"] == "MISSING"
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=version_headers(client, 2)
    )
    assert result.status_code == 409 and result.json()["code"] == "TRADE_ORIGIN_REVIEW_REQUIRED"
    safe(result)
    assert database_snapshot(engine) == before


async def test_foreign_active_owner_cannot_read_or_recover_another_trade(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    foreign = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (foreign,),
        )
        token, csrf, _ = issue_session(con, foreign, "synthetic-foreign-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    read = await client.get(path(original["id"]))
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=version_headers(client, 2)
    )
    assert read.status_code == result.status_code == 404
    safe(read)
    safe(result)
    assert database_snapshot(engine) == before


async def test_concurrent_requests_are_atomic_and_idempotency_includes_version_and_body(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    url = path(original["id"]) + "/recover"
    headers = version_headers(client, 2, key="concurrent-synthetic-recovery-intent")
    first, replay = await asyncio.gather(
        *(client.post(url, json=body(original), headers=headers) for _ in range(2))
    )
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    before = database_snapshot(engine)
    conflicting = await client.post(
        url,
        json={**body(original), "reason": "A different synthetic recovery reason"},
        headers=headers,
    )
    assert conflicting.status_code == 409 and conflicting.json()["code"] == "IDEMPOTENCY_CONFLICT"
    other_version = await client.post(
        url, json=body(original), headers={**headers, "If-Match": '"1"'}
    )
    assert (
        other_version.status_code == 409 and other_version.json()["code"] == "IDEMPOTENCY_CONFLICT"
    )
    new_intent = await client.post(url, json=body(original), headers=version_headers(client, 2))
    assert (
        new_intent.status_code == 409
        and new_intent.json()["code"] == "TRADE_ORIGIN_ALREADY_RETAINED"
    )
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='TRADE_AUDIT_ORIGIN_RECOVERED'"
            ).scalar_one()
            == 1
        )


async def test_competing_recovery_intents_cannot_append_two_origins(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    url = path(original["id"]) + "/recover"
    first, second = await asyncio.gather(
        client.post(url, json=body(original), headers=version_headers(client, 2)),
        client.post(url, json=body(original), headers=version_headers(client, 2)),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='TRADE_AUDIT_ORIGIN_RECOVERED'"
            ).scalar_one()
            == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations "
                "WHERE reason='TRADE_AUDIT_ORIGIN_RECOVERED'"
            ).scalar_one()
            == 1
        )


async def test_scheduled_response_recovery_does_not_open_files_or_provider_clients(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book, scheduled=True)
    assert original["status"] == "DRAFT_SCHEDULED"
    url = path(original["id"]) + "/recover"
    headers = version_headers(client, 2)
    with (
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider I/O")),
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private I/O")),
    ):
        result = await client.post(url, json=body(original), headers=headers)
        assert result.status_code == 200 and result.json()["origin"] == "RETAINED_RESPONSE"
        before = database_snapshot(engine)
        replay = await client.post(url, json=body(original), headers=headers)
        read = await client.get(path(original["id"]))
        assert replay.status_code == read.status_code == 200
        assert replay.json() == read.json() == result.json()
        assert database_snapshot(engine) == before


@pytest.mark.parametrize("tamper", ["captured-at", "trade-id", "origin", "extra", "intent"])
async def test_cached_recovery_is_revalidated_against_owned_audited_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], tamper: str
) -> None:
    original = await legacy_trade(client, book)
    headers = version_headers(client, 2)
    url = path(original["id"]) + "/recover"
    first = await client.post(url, json=body(original), headers=headers)
    assert first.status_code == 200
    altered = first.json()
    requested = body(original)
    if tamper == "captured-at":
        altered["captured_at"] = "2000-01-01T00:00:00.000000Z"
    elif tamper == "trade-id":
        altered["trade_id"] = str(uuid4())
    elif tamper == "origin":
        altered["origin"] = "AT_CREATION"
    elif tamper == "extra":
        altered["private"] = "private-untrusted"
    else:
        requested = {**requested, "reason": "A different synthetic recovery reason"}
    with unit_of_work(engine, write=True) as con:
        if tamper == "intent":
            fingerprint = digest(canonical({"version": 2, **requested}))
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
                (fingerprint, headers["Idempotency-Key"]),
            )
        else:
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                (json.dumps(altered), headers["Idempotency-Key"]),
            )
    before = database_snapshot(engine)
    result = await client.post(url, json=requested, headers=headers)
    assert result.status_code == 503 and result.json()["code"] == "TRADE_AUDIT_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_well_shaped_but_unaudited_sql_origin_is_not_exposed_or_adopted(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], original["id"])
        values.update(
            original_status=original["status"], original_updated_at=original["updated_at"]
        )
        insert(con, "trade_audit_origins", capture_values(con, values, "RETAINED_RESPONSE"))
    before = database_snapshot(engine)
    read = await client.get(path(original["id"]))
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=version_headers(client, 2)
    )
    assert read.status_code == result.status_code == 503
    assert read.json()["code"] == result.json()["code"] == "TRADE_AUDIT_UNAVAILABLE"
    safe(read)
    safe(result)
    assert database_snapshot(engine) == before


async def test_failed_response_serialization_rolls_back_origin_audit_and_invalidation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = await legacy_trade(client, book)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("private-untrusted serialization failure")

    monkeypatch.setattr(TradeAuditOriginRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=version_headers(client, 2)
    )
    assert result.status_code == 503 and result.json()["code"] == "TRADE_AUDIT_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_current_legacy_trade_cannot_use_retired_response_recovery(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with patch.object(trade_service, "_remember_trade_origin"):
        original = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    read = await client.get(path(original["id"]))
    assert read.status_code == 200 and read.json()["origin"] == "MISSING"
    assert read.json()["trade_status"] == "ACTIVE" and read.json()["trade_version"] == 1
    result = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=version_headers(client, 1)
    )
    assert result.status_code == 409 and result.json()["code"] == "TRADE_ORIGIN_REVIEW_REQUIRED"
    assert database_snapshot(engine) == before


async def test_http_replay_survives_a_later_successor_revision_without_new_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    url = path(original["id"]) + "/recover"
    headers = version_headers(client, 2)
    first = await client.post(url, json=body(original), headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine) as con:
        successor = con.exec_driver_sql(
            "SELECT id FROM trades WHERE supersedes_id=?", (original["id"],)
        ).scalar_one()
    changed = await client.patch(
        f"/api/v1/trades/{successor}",
        json=payload(book, quantity="12", reason="Correct the later synthetic trade"),
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(url, json=body(original), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == before


async def test_stale_version_and_mismatched_response_fail_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    before = database_snapshot(engine)
    stale = await client.post(
        path(original["id"]) + "/recover", json=body(original), headers=version_headers(client, 1)
    )
    assert stale.status_code == 412
    changed = {**original, "quantity": "12.00000000"}
    invalid = await client.post(
        path(original["id"]) + "/recover", json=body(changed), headers=version_headers(client, 2)
    )
    assert invalid.status_code == 409 and invalid.json()["code"] == "TRADE_ORIGIN_REVIEW_REQUIRED"
    safe(stale)
    safe(invalid)
    assert database_snapshot(engine) == before
    assert UUID(original["id"]).version == 4
