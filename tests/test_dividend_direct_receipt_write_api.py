"""Protected direct cash creation must bind HTTP intent without inventing entitlement."""

import asyncio
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_void as voids
import spm_api.dividend_direct_receipt_write_routes as routes
import spm_api.dividend_direct_receipt_writer as writer
import spm_api.dividend_event_writer as event_writer
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_direct_receipt_write_models import DirectReceiptCreate
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.test_dividend_direct_receipt_inputs_api import book as book
from tests.test_dividend_direct_receipt_inputs_api import path as input_path
from tests.test_dividend_direct_receipt_void import void_intent
from tests.test_dividend_event_writer import next_intent
from tests.test_dividend_receipt_write_api import safe
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_rejection_writer import save as reject
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/direct-receipts"


async def body(client: httpx.AsyncClient, book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    response = await client.get(input_path(book))
    assert response.status_code == 200, response.text
    observed = response.json()
    return {
        "receipt_id": str(uuid4()),
        "event_id": observed["event_id"],
        "revision_id": observed["revision_id"],
        "account_id": observed["account_id"],
        "portfolio_id": observed["portfolio_id"],
        "broker_id": observed["broker_id"],
        "account_version": observed["account_version"],
        "portfolio_version": observed["portfolio_version"],
        "broker_version": observed["broker_version"],
        "expected_data_revision": observed["portfolio_data_revision"],
        "received_date": "2026-01-09",
        "received_amount": "6",
        "currency": "USD",
        "tax": None,
        "fee": "0",
        "fx_rate_hkd": "7.8",
        "fx_source": "Synthetic FX source",
        "actual_hkd": None,
        "actual_hkd_reason": None,
        "note": "synthetic-private direct receipt note",
        "reason": "synthetic-private direct receipt reason",
        "confirmation": "RECORDED_CASH_ONLY",
        "confirm_revision_fence": True,
        **changes,
    }


async def test_direct_create_and_replay_bind_cash_without_entitlement(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, book["event"].revision_no)
    before = database_snapshot(engine)
    created = await client.post(PATH, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    safe(created)
    result = created.json()
    assert result["id"] == payload["receipt_id"]
    assert result["revision_id"] == payload["revision_id"]
    assert result["revision_no"] == book["event"].revision_no
    assert result["receipt_basis"] == "DIRECT_EVENT_ACCOUNT"
    assert result["received_amount"] == "6.000000" and result["received_hkd"] == "46.800000"
    assert result["tax"] is None and result["fee"] == "0.000000"
    assert result["scope"] == "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY"
    assert result["eligibility_scope"] == "NOT_ESTABLISHED"
    assert result["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert "entitlement_id" not in result
    after = database_snapshot(engine)
    allowed = {
        "audit_events",
        "cash_ledger_entries",
        "dividend_direct_receipt_origins",
        "dividend_direct_receipts",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: value for key, value in before.items() if key not in allowed} == {
        key: value for key, value in after.items() if key not in allowed
    }
    assert after["dividend_entitlements"] == after["dividend_receipts"] == []
    assert len(after["dividend_direct_receipts"]) == 1
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    replay = await client.post(PATH, json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == result
    safe(replay)
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"fx_rate_hkd": None, "fx_source": None, "tax": None, "fee": None}, None),
        ({"currency": "HKD", "fx_rate_hkd": None, "fx_source": None}, "6.000000"),
        (
            {"actual_hkd": "47", "actual_hkd_reason": "Synthetic original direct cash adjustment"},
            "47.000000",
        ),
        ({"received_amount": "-6"}, "-46.800000"),
        ({"received_amount": "0"}, "0.000000"),
        ({"received_amount": "9223372036854.775807", "fx_rate_hkd": None, "fx_source": None}, None),
    ],
)
async def test_exact_cash_unknowns_identity_and_original_override_are_preserved(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    changes: dict[str, Any],
    expected: str | None,
) -> None:
    payload = await body(client, book, **changes)
    headers = version_headers(client, 1)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    safe(response)
    assert response.json()["received_hkd"] == expected
    assert response.json()["actual_hkd_reason"] == payload["actual_hkd_reason"]
    if payload["currency"] == "HKD":
        assert response.json()["fx_rate_hkd"] == "1.000000000000"
        assert response.json()["fx_source"] == "IDENTITY"
    before = database_snapshot(engine)
    repeated = await client.post(PATH, json=payload, headers=headers)
    assert repeated.json() == response.json() and repeated.status_code == 201
    assert database_snapshot(engine) == before


async def test_historical_replay_after_void_archive_and_clock_rollback_never_reposts(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    created = await client.post(PATH, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    with unit_of_work(engine) as con:
        records = combined.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert records is not None and len(records) == 1
    value = void_intent(engine, (book["event"], str(book["account_id"])), records[0])
    with unit_of_work(engine, write=True) as con:
        voids.void(con, book["owner"], value, request_id=uuid4())
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED' WHERE id=?", (str(book["account_id"]),)
        )
    before = database_snapshot(engine)

    class EarlierDate(date):
        @classmethod
        def today(cls) -> "EarlierDate":
            return cls(2025, 1, 1)

    with patch("spm_api.cash_models.date", EarlierDate):
        replay = await client.post(PATH, json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == created.json()
    safe(replay)
    assert replay.json()["status"] == "ACTIVE" and replay.json()["version"] == "1"
    assert database_snapshot(engine) == before


async def test_concurrent_identical_requests_commit_one_cash_and_a_changed_key_conflicts(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    first, second = await asyncio.gather(
        client.post(PATH, json=payload, headers=headers),
        client.post(PATH, json=payload, headers=headers),
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    before = database_snapshot(engine)
    assert len(before["dividend_direct_receipts"]) == 1
    changed = await client.post(PATH, json={**payload, "received_amount": "7"}, headers=headers)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    other = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert other.status_code == 409 and other.json()["code"] == "DIVIDEND_DIRECT_RECEIPT_CONFLICT"
    assert database_snapshot(engine) == before


async def test_old_internal_operation_cannot_gain_http_intent_on_request_id_collision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    request_id = uuid4()
    with unit_of_work(engine, write=True) as con:
        writer.save(
            con,
            book["owner"],
            routes._intent(DirectReceiptCreate.model_validate(payload), 1),
            request_id=request_id,
        )
    before = database_snapshot(engine)
    response = await client.post(
        PATH, json=payload, headers={**version_headers(client, 1), "X-Request-ID": str(request_id)}
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "DIVIDEND_DIRECT_RECEIPT_CONFLICT"
    )
    safe(response)
    assert database_snapshot(engine) == before


async def test_missing_idempotency_marker_does_not_adopt_an_older_http_operation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    request_id = uuid4()
    headers = {**version_headers(client, 1), "X-Request-ID": str(request_id)}
    response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "DELETE FROM idempotency_keys WHERE route=? AND idem_key=?",
            (f"POST {PATH}", headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=payload, headers=headers)
    assert replay.status_code == 409
    safe(replay)
    assert database_snapshot(engine) == before


async def test_origin_csrf_reauthentication_and_session_are_required(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(PATH, json=payload, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    before = database_snapshot(engine)
    for overrides, code in [
        ({"Origin": "http://untrusted.test"}, "ORIGIN_REJECTED"),
        ({"X-CSRF-Token": "x" * 32}, "CSRF_REJECTED"),
    ]:
        result = await client.post(PATH, json=payload, headers={**headers, **overrides})
        assert result.status_code == 403 and result.json()["code"] == code
        safe(result)
    assert database_snapshot(engine) == before
    client.cookies.clear()
    missing = await client.post(PATH, json=payload, headers=headers)
    assert missing.status_code == 401
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_create_or_disclose_direct_cash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    with unit_of_work(engine, write=True) as con:
        other = uuid4()
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(other),),
        )
        token, csrf, _ = issue_session(con, str(other), "synthetic-foreign-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert response.status_code == 404
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["rejected", "withdrawn", "archived"])
async def test_new_cash_is_fenced_but_current_inputs_remain_readable(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], state: str
) -> None:
    version = 1
    if state == "rejected":
        reject(engine, rejection_intent(book["event"]), owner=book["owner"])
    elif state == "withdrawn":
        successor = next_intent(book["event"], "WITHDRAW")
        with unit_of_work(engine, write=True) as con:
            event_writer.save(con, book["owner"], successor, at=now(), request_id=uuid4())
        version = 2
    else:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE accounts SET status='ARCHIVED' WHERE id=?", (str(book["account_id"]),)
            )
    payload = await body(client, book)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, version))
    assert response.status_code == 409
    assert (
        response.json()["code"]
        == {
            "rejected": "DIVIDEND_REVISION_REJECTED",
            "withdrawn": "DIVIDEND_DIRECT_RECEIPT_EVENT_WITHDRAWN",
            "archived": "DIVIDEND_DIRECT_RECEIPT_ACCOUNT_ARCHIVED",
        }[state]
    )
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    ["account", "portfolio", "broker", "data", "archive", "revoked", "csrf", "reauth", "owner"],
)
async def test_late_scope_or_security_change_during_idempotency_insert_rolls_back_everything(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    statements = {
        "account": f"UPDATE accounts SET version=version+1 WHERE id='{book['account_id']}'",
        "portfolio": f"UPDATE portfolios SET version=version+1 WHERE id='{book['portfolio_id']}'",
        "broker": f"UPDATE brokers SET version=version+1 WHERE id='{book['broker_id']}'",
        "data": "UPDATE portfolios SET data_revision=data_revision+1 "
        f"WHERE id='{book['portfolio_id']}'",
        "archive": f"UPDATE accounts SET status='ARCHIVED' WHERE id='{book['account_id']}'",
        "revoked": "UPDATE sessions SET revoked_at='2026-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{book['owner']}'",
        "csrf": "UPDATE sessions SET csrf_sha256='"
        + "0" * 64
        + f"' WHERE owner_id='{book['owner']}'",
        "reauth": "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{book['owner']}'",
        "owner": f"UPDATE owners SET is_active=0 WHERE id='{book['owner']}'",
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_direct_late_scope AFTER INSERT ON idempotency_keys "
            f"WHEN NEW.route='POST {PATH}' BEGIN {statements[problem]}; END"
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    expected = (
        401 if problem in {"owner", "revoked"} else 403 if problem in {"csrf", "reauth"} else 409
    )
    assert response.status_code == expected, response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_security_is_rechecked_inside_the_serialized_transaction(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str, replay: bool
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    if replay:
        created = await client.post(PATH, json=payload, headers=headers)
        assert created.status_code == 201, created.text
    checkpoint: dict[str, Any] = {}

    def changed(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            elif failure == "csrf":
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
            else:
                con.exec_driver_sql(
                    "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'"
                )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    with patch.object(routes, "mutate", side_effect=changed):
        result = await client.post(PATH, json=payload, headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize("failure", ["missing", "error", "scope"])
async def test_http_intent_failure_rolls_back_cash_and_allows_unchanged_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        if failure == "missing":
            return
        audit(con, *args, **kwargs)
        if failure == "error":
            raise ValueError("synthetic-private late intent failure")
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(book["account_id"]),)
        )

    with patch.object(routes, "audit", side_effect=changed):
        response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == (409 if failure == "scope" else 503)
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(PATH, json=payload, headers=headers)
    assert retry.status_code == 201, retry.text


@pytest.mark.parametrize("problem", ["void", "substituted", "wrong-type"])
@pytest.mark.parametrize("at_commit", [False, True])
async def test_changed_writer_result_cannot_commit_a_different_record(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str, at_commit: bool
) -> None:
    payload = await body(client, book)
    original = writer.save
    calls = 0

    def changed(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if at_commit and calls == 1:
            return result
        if problem == "wrong-type":
            return object()
        if problem == "void":
            return replace(result, receipt=replace(result.receipt, status="VOID"))
        return replace(
            result,
            receipt=replace(result.receipt, received=Decimal("7"), received_hkd=Decimal("54.6")),
        )

    before = database_snapshot(engine)
    with patch.object(writer, "save", side_effect=changed):
        response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert response.status_code == 503, response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["ignore-key", "duplicate-audit"])
async def test_late_idempotency_or_http_audit_corruption_aborts_every_fact(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    with unit_of_work(engine, write=True) as con:
        if problem == "ignore-key":
            con.exec_driver_sql(
                "CREATE TRIGGER synthetic_direct_ignore_key BEFORE INSERT ON idempotency_keys "
                f"WHEN NEW.route='POST {PATH}' BEGIN SELECT RAISE(IGNORE); END"
            )
        else:
            con.exec_driver_sql(
                "CREATE TRIGGER synthetic_direct_duplicate_intent AFTER INSERT ON idempotency_keys "
                f"WHEN NEW.route='POST {PATH}' BEGIN "
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                f"SELECT '{uuid4()}',owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at FROM audit_events "
                "WHERE entity_type='dividend_direct_receipt_http_intent' "
                f"AND entity_id='{payload['receipt_id']}'; END"
            )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert response.status_code == 503, response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["id", "cash", "scope"])
async def test_modified_cached_response_never_replaces_audited_creation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    created = await client.post(PATH, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    cache = created.json()
    if problem == "id":
        cache["id"] = str(uuid4())
    elif problem == "cash":
        cache.update(received_amount="7.000000", received_hkd="54.600000")
    else:
        cache["portfolio_id"] = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE route=? AND idem_key=?",
            (json.dumps(cache), f"POST {PATH}", headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("revision_id", str(uuid4()), 409),
        ("account_id", str(uuid4()), 404),
        ("event_id", str(uuid4()), 404),
        ("account_version", "999", 409),
        ("expected_data_revision", "999", 409),
        ("received_date", "9999-12-31", 422),
        ("confirm_revision_fence", False, 422),
        ("entitlement_id", str(uuid4()), 422),
    ],
)
async def test_invalid_missing_or_stale_inputs_do_not_write(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    field: str,
    value: Any,
    status: int,
) -> None:
    payload = await body(client, book, **{field: value})
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert response.status_code == status, response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("competitor", ["cash", "rejection"])
async def test_competing_new_cash_or_rejection_has_only_one_financial_winner(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], competitor: str
) -> None:
    payload = await body(client, book)
    before = database_snapshot(engine)
    if competitor == "cash":
        other = client.post(
            PATH, json={**payload, "receipt_id": str(uuid4())}, headers=version_headers(client, 1)
        )
    else:
        other = client.post(
            f"/api/v1/dividends/{book['event'].event_id}/verify",
            json={
                "revision_id": payload["revision_id"],
                "decision": "REJECT_REVISION",
                "citation": "Synthetic competing direct cash rejection",
                "evidence_url": None,
                "reason": "Synthetic explicit rejection competing with direct cash.",
            },
            headers=version_headers(client, 1),
        )
    cash, competing = await asyncio.gather(
        client.post(PATH, json=payload, headers=version_headers(client, 1)), other
    )
    safe(cash)
    safe(competing)
    if competitor == "cash":
        assert sorted([cash.status_code, competing.status_code]) == [201, 409]
        count = 1
    else:
        assert (cash.status_code, competing.status_code) in {(201, 409), (409, 200)}
        count = int(cash.status_code == 201)
    after = database_snapshot(engine)
    assert len(after["dividend_direct_receipts"]) == count
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + count
    assert after["dividend_entitlements"] == after["dividend_receipts"] == []
    assert len(after["dividend_event_rejections"]) == (0 if competitor == "cash" else 1 - count)


async def test_wrong_observed_event_version_is_a_zero_write_precondition_failure(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 412
    safe(response)
    assert database_snapshot(engine) == before
