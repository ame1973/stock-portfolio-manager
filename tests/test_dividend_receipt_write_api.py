"""Protected recorded receipt creation binds cash, origin and HTTP intent atomically."""

import asyncio
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as history
import spm_api.dividend_receipt_void as voids
import spm_api.dividend_receipt_write_routes as routes
import spm_api.dividend_receipt_writer as writer
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import to_scaled
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_history_api import path as history_path
from tests.test_dividend_receipt_void import void_intent
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/receipts"


async def body(client: httpx.AsyncClient, book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    response = await client.get(f"{history_path(book)}/inputs")
    assert response.status_code == 200, response.text
    observed = response.json()
    return {
        "receipt_id": str(uuid4()),
        "event_id": observed["event_id"],
        "revision_id": observed["revision_id"],
        "entitlement_id": book["older"]["id"],
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
        "note": "synthetic-private receipt note",
        "reason": "synthetic-private receipt reason",
        "confirmation": "RECORDED_CASH_ONLY",
        **changes,
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "synthetic-private" not in response.text


async def test_create_and_replay_bind_actual_cash_and_original_physical_revision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-create-key")
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        created = await client.post(PATH, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    safe(created)
    result = created.json()
    assert result["id"] == payload["receipt_id"]
    assert result["original_revision_id"] == str(book["first"].revision_id)
    assert result["original_revision_no"] == 1 and result["observed_revision_no"] == 2
    assert result["observed_revision_id"] == str(book["second"].revision_id)
    assert result["received_amount"] == "6.000000" and result["received_hkd"] == "46.800000"
    assert result["tax"] is None and result["fee"] == "0.000000"
    assert result["scope"] == "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"
    assert result["eligibility_scope"] == "NOT_ESTABLISHED"
    assert result["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    after = database_snapshot(engine)
    allowed = {
        "audit_events",
        "cash_ledger_entries",
        "dividend_receipt_origins",
        "dividend_receipts",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: value for key, value in before.items() if key not in allowed} == {
        key: value for key, value in after.items() if key not in allowed
    }
    assert len(after["dividend_receipts"]) == len(before["dividend_receipts"]) + 1
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    replay = await client.post(PATH, json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == result
    safe(replay)
    assert database_snapshot(engine) == after


async def test_later_void_and_clock_rollback_return_original_response_without_reposting(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-void-replay")
    created = await client.post(PATH, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    with unit_of_work(engine) as con:
        records = history.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert records is not None and len(records) == 1
    scope = (book["second"], str(book["account_id"]), book["older"])
    value = void_intent(engine, scope, records[0])
    with unit_of_work(engine, write=True) as con:
        voids.void(con, book["owner"], value, request_id=uuid4())
    after = database_snapshot(engine)

    class EarlierDate(date):
        @classmethod
        def today(cls) -> "EarlierDate":
            return cls(2025, 1, 1)

    with patch("spm_api.cash_models.date", EarlierDate):
        replay = await client.post(PATH, json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == created.json()
    assert replay.json()["status"] == "ACTIVE"
    safe(replay)
    assert database_snapshot(engine) == after


async def test_expired_idempotency_entry_cannot_repost_a_stable_receipt_id(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-expired-key")
    response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    with unit_of_work(engine, write=True) as con:
        rows = con.exec_driver_sql(
            "SELECT id FROM idempotency_keys WHERE owner_id=? AND route=? AND idem_key=?",
            (str(book["owner"]), f"POST {PATH}", headers["Idempotency-Key"]),
        ).all()
        assert len(rows) == 1
        con.exec_driver_sql("DELETE FROM idempotency_keys WHERE id=?", (rows[0].id,))
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=payload, headers=headers)
    assert replay.status_code == 409 and replay.json()["code"] == "DIVIDEND_RECEIPT_CONFLICT"
    safe(replay)
    assert database_snapshot(engine) == before


async def test_same_key_race_posts_one_receipt_and_different_key_cannot_reuse_its_id(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-race-key")
    first, second = await asyncio.gather(
        client.post(PATH, json=payload, headers=headers),
        client.post(PATH, json=payload, headers=headers),
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    before = database_snapshot(engine)
    conflict = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert conflict.status_code == 409 and conflict.json()["code"] == "DIVIDEND_RECEIPT_CONFLICT"
    safe(conflict)
    assert database_snapshot(engine) == before


async def test_reauthentication_origin_and_csrf_are_required_before_any_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-reauth-key")
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
    bad_origin = await client.post(
        PATH, json=payload, headers={**headers, "Origin": "http://untrusted.test"}
    )
    assert bad_origin.status_code == 403 and bad_origin.json()["code"] == "ORIGIN_REJECTED"
    bad_csrf = await client.post(PATH, json=payload, headers={**headers, "X-CSRF-Token": "x" * 32})
    assert bad_csrf.status_code == 403 and bad_csrf.json()["code"] == "CSRF_REJECTED"
    assert database_snapshot(engine) == before
    saved = await client.post(PATH, json=payload, headers=headers)
    assert saved.status_code == 201, saved.text


async def test_late_http_audit_error_rolls_back_receipt_cash_and_allows_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-late-failure")
    before = database_snapshot(engine)
    with patch.object(routes, "audit", side_effect=ValueError("synthetic-private")):
        failed = await client.post(PATH, json=payload, headers=headers)
    assert failed.status_code == 503
    assert failed.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(failed)
    assert database_snapshot(engine) == before
    recovered = await client.post(PATH, json=payload, headers=headers)
    assert recovered.status_code == 201, recovered.text


@pytest.mark.parametrize("problem", ["no-origin", "void", "unverified"])
async def test_inconsistent_posting_result_rolls_back_even_when_writer_returned(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    original = writer.save

    def forged(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if problem == "no-origin":
            return replace(result, origin=None)
        if problem == "void":
            return replace(result, receipt=replace(result.receipt, status="VOID"))
        return replace(result, audit_scope="RECEIPT_AUDIT_NOT_VERIFIED")

    before = database_snapshot(engine)
    with patch.object(writer, "save", side_effect=forged):
        response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_scope_change_during_http_intent_audit_rolls_back_the_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        audit(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(book["account_id"]),)
        )

    with patch.object(routes, "audit", side_effect=changed):
        response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_SCOPE_CHANGED"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["account", "portfolio", "broker", "data", "archive"])
async def test_scope_change_during_idempotency_insert_rolls_back_the_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    account = str(book["account_id"])
    portfolio = str(book["portfolio_id"])
    broker = str(book["broker_id"])
    statement = {
        "account": f"UPDATE accounts SET version=version+1 WHERE id='{account}'",
        "portfolio": f"UPDATE portfolios SET version=version+1 WHERE id='{portfolio}'",
        "broker": f"UPDATE brokers SET version=version+1 WHERE id='{broker}'",
        "data": f"UPDATE portfolios SET data_revision=data_revision+1 WHERE id='{portfolio}'",
        "archive": f"UPDATE accounts SET status='ARCHIVED' WHERE id='{account}'",
    }[problem]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_move_receipt_scope AFTER INSERT ON idempotency_keys "
            "WHEN NEW.route='POST /api/v1/dividends/receipts' "
            f"BEGIN {statement}; END"
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 409
    expected = (
        "DIVIDEND_RECEIPT_ACCOUNT_ARCHIVED"
        if problem == "archive"
        else ("DIVIDEND_RECEIPT_SCOPE_CHANGED")
    )
    assert response.json()["code"] == expected
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth", "owner"])
async def test_security_change_during_idempotency_insert_aborts_the_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    payload = await body(client, book)
    owner = str(book["owner"])
    statement = {
        "revoked": "UPDATE sessions SET revoked_at='2026-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{owner}'",
        "csrf": "UPDATE sessions SET csrf_sha256='" + "0" * 64 + f"' WHERE owner_id='{owner}'",
        "reauth": "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{owner}'",
        "owner": f"UPDATE owners SET is_active=0 WHERE id='{owner}'",
    }[failure]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_revoke_receipt_security AFTER INSERT ON idempotency_keys "
            "WHEN NEW.route='POST /api/v1/dividends/receipts' "
            f"BEGIN {statement}; END"
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == (401 if failure in {"revoked", "owner"} else 403)
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["void", "substituted"])
async def test_commit_revalidation_rejects_a_changed_or_substituted_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    original = writer.save
    calls = 0

    def changed(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            return result
        if problem == "void":
            return replace(result, receipt=replace(result.receipt, status="VOID"))
        return replace(
            result,
            receipt=replace(result.receipt, received=Decimal("7"), received_hkd=Decimal("54.6")),
        )

    before = database_snapshot(engine)
    with patch.object(writer, "save", side_effect=changed):
        response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 503 and calls == 2
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_http_audit_duplicate_during_idempotency_insert_aborts_every_fact(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    receipt_id = payload["receipt_id"]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_duplicate_receipt_http_audit "
            "AFTER INSERT ON idempotency_keys "
            "WHEN NEW.route='POST /api/v1/dividends/receipts' BEGIN "
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,request_id,created_at) "
            f"SELECT '{uuid4()}',owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,request_id,created_at FROM audit_events "
            "WHERE entity_type='dividend_receipt_http_intent' "
            f"AND entity_id='{receipt_id}'; END"
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_ignored_idempotency_insert_cannot_commit_recorded_cash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-ignored-cache")
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_ignore_receipt_idempotency "
            "BEFORE INSERT ON idempotency_keys "
            "WHEN NEW.route='POST /api/v1/dividends/receipts' "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    before = database_snapshot(engine)
    result = await client.post(PATH, json=payload, headers=headers)
    assert result.status_code == 503
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("changes", "expected_rate", "expected_hkd", "override"),
    [
        ({"fx_rate_hkd": None, "fx_source": None}, None, None, False),
        (
            {"actual_hkd": "45.6", "actual_hkd_reason": "Synthetic broker actual HKD"},
            "7.800000000000",
            "45.600000",
            True,
        ),
        (
            {"currency": "HKD", "fx_rate_hkd": None, "fx_source": None},
            "1.000000000000",
            "6.000000",
            False,
        ),
    ],
)
async def test_explicit_unknown_fx_override_and_hkd_identity_remain_distinct(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    changes: dict[str, Any],
    expected_rate: str | None,
    expected_hkd: str | None,
    override: bool,
) -> None:
    payload = await body(client, book, **changes)
    response = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["fx_rate_hkd"] == expected_rate and result["received_hkd"] == expected_hkd
    assert result["actual_hkd_override"] is override
    assert result["tax"] is None and result["fee"] == "0.000000"
    with unit_of_work(engine) as con:
        cash = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro FROM cash_ledger_entries WHERE id=?",
            (result["cash_entry_id"],),
        ).one()
    assert cash.amount_micro == 6_000_000
    assert cash.booked_hkd_micro == (None if expected_hkd is None else to_scaled(expected_hkd, 6))


async def test_same_key_with_changed_intent_fails_without_another_cash_entry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-conflicting-intent")
    first = await client.post(PATH, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    before = database_snapshot(engine)
    conflict = await client.post(
        PATH, json={**payload, "note": "Different synthetic-private note"}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    safe(conflict)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_writer_rechecks_security_inside_the_serialized_transaction(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    failure: str,
    replay: bool,
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-security-race")
    if replay:
        first = await client.post(PATH, json=payload, headers=headers)
        assert first.status_code == 201, first.text
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


async def test_foreign_or_missing_session_cannot_create_a_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    other = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(other),),
        )
        token, csrf, _ = issue_session(con, str(other), "synthetic-foreign-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    headers = version_headers(client, 2)
    before = database_snapshot(engine)
    foreign = await client.post(PATH, json=payload, headers=headers)
    assert foreign.status_code == 404 and foreign.json()["code"] == "NOT_FOUND"
    safe(foreign)
    client.cookies.clear()
    missing = await client.post(PATH, json=payload, headers=headers)
    assert missing.status_code == 401
    safe(missing)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", str(uuid4())),
        ("entitlement_id", str(uuid4())),
        ("cash_entry_id", str(uuid4())),
        ("received_amount", "7"),
        ("original_revision_id", str(uuid4())),
        ("status", "VOID"),
        ("payment_scope", "VERIFIED"),
    ],
)
async def test_forged_cached_responses_cannot_replace_the_audited_creation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: str
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-forged-cache")
    first = await client.post(PATH, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    before = database_snapshot(engine)
    execute = Connection.execute

    def forged(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "SELECT request_sha256,response_json FROM idempotency_keys" not in str(statement):
            return result
        cached = result.mappings().first()
        assert cached is not None
        changed = dict(cached)
        response = json.loads(changed["response_json"])
        response[field] = value
        changed["response_json"] = json.dumps(response)
        fake = MagicMock()
        fake.mappings.return_value.first.return_value = changed
        return fake

    with patch.object(Connection, "execute", forged):
        response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_missing_origin_or_original_revision_never_produces_a_creation_response(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    first = await client.post(PATH, json=payload, headers=version_headers(client, 2))
    assert first.status_code == 201, first.text
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        records = history.history(con, book["owner"], book["event"].event_id, book["account_id"])
        assert records is not None and len(records) == 1
        record = records[0]
        for bad in (
            replace(record, origin=None),
            replace(record, audit_scope="RECEIPT_AUDIT_NOT_VERIFIED"),
            replace(record, original_revision_id=uuid4()),
        ):
            with pytest.raises(
                writer.ReceiptWriteError, match="DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
            ):
                routes._public(con, book["owner"], book["event"].event_id, book["account_id"], bad)
        with (
            patch.object(events, "get", return_value=None),
            pytest.raises(writer.ReceiptWriteError, match="DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"),
        ):
            routes._public(con, book["owner"], book["event"].event_id, book["account_id"], record)
        without_origin = replace(record, origin=None)
        with pytest.raises(writer.ReceiptWriteError, match="DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"):
            routes._binding(MagicMock(), MagicMock(), {}, without_origin)
        with pytest.raises(writer.ReceiptWriteError, match="DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"):
            routes._check_intent(con, MagicMock(), MagicMock(), MagicMock(), {}, without_origin)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["missing-scope", "missing-receipt", "origin", "writer-conflict"]
)
async def test_replay_rechecks_the_entire_audited_receipt_before_returning_cache(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-replay-integrity")
    first = await client.post(PATH, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    before = database_snapshot(engine)
    original = history.history

    def altered(*args: Any, **kwargs: Any) -> Any:
        retained = original(*args, **kwargs)
        assert retained is not None and len(retained) == 1
        if problem == "missing-scope":
            return None
        if problem == "missing-receipt":
            return ()
        return (replace(retained[0], origin=None),)

    if problem == "writer-conflict":
        target = patch.object(
            writer, "save", side_effect=writer.ReceiptWriteError("DIVIDEND_RECEIPT_CONFLICT")
        )
    else:
        target = patch.object(history, "history", side_effect=altered)
    with target:
        response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


class AuditRow(NamedTuple):
    id: str
    owner_id: str
    action: str
    request_id: str
    reason: str
    before_sha256: str | None
    after_sha256: str
    created_at: str


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "duplicate",
        "id",
        "owner",
        "action",
        "request",
        "reason",
        "before",
        "after",
        "time",
    ],
)
async def test_forged_http_intent_audits_fail_closed_on_historical_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 2, key="synthetic-receipt-forged-audit")
    first = await client.post(PATH, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    before = database_snapshot(engine)
    execute = Connection.execute

    def forged(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "FROM audit_events WHERE entity_type='dividend_receipt_http_intent'" not in str(
            statement
        ):
            return result
        rows = [AuditRow(*row) for row in result.all()]
        assert len(rows) == 1
        if problem == "missing":
            rows = []
        elif problem == "duplicate":
            rows.append(rows[0])
        else:
            field, value = {
                "id": ("id", "synthetic-private invalid UUID"),
                "owner": ("owner_id", str(uuid4())),
                "action": ("action", "OTHER"),
                "request": ("request_id", str(uuid4())),
                "reason": ("reason", "synthetic-private"),
                "before": ("before_sha256", "0" * 64),
                "after": ("after_sha256", "0" * 64),
                "time": ("created_at", "2000-01-01T00:00:00.000000Z"),
            }[problem]
            values = rows[0]._asdict()
            values[field] = value
            rows = [AuditRow(**values)]
        fake = MagicMock()
        fake.all.return_value = rows
        return fake

    with patch.object(Connection, "execute", forged):
        response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("DIVIDEND_RECEIPT_OWNER_UNAVAILABLE", 401),
        ("INVALID_DIVIDEND_RECEIPT_SCOPE", 422),
        ("INVALID_DIVIDEND_RECEIPT_TERMS", 422),
        ("DIVIDEND_RECEIPT_DATE_INVALID", 422),
        ("DIVIDEND_RECEIPT_ACCOUNT_ARCHIVED", 409),
        ("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE", 503),
        ("UNEXPECTED_PRIVATE_CODE", 503),
    ],
)
def test_internal_receipt_errors_map_to_fixed_private_responses(code: str, status: int) -> None:
    result = routes._failure(writer.ReceiptWriteError(code))
    assert result.status == status
    assert "PRIVATE" not in result.message


@pytest.mark.parametrize(
    ("changes", "version", "status", "code"),
    [
        ({"account_version": "99"}, 2, 409, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ({"expected_data_revision": "999"}, 2, 409, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ({"entitlement_id": str(uuid4())}, 2, 404, "NOT_FOUND"),
        ({"received_date": "9999-12-31"}, 2, 422, "DIVIDEND_RECEIPT_DATE_INVALID"),
        ({"received_amount": 1.0}, 2, 422, "VALIDATION_ERROR"),
        ({}, 1, 412, "PRECONDITION_FAILED"),
    ],
)
async def test_stale_invalid_or_missing_inputs_are_zero_write(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    changes: dict[str, Any],
    version: int,
    status: int,
    code: str,
) -> None:
    payload = await body(client, book, **changes)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload, headers=version_headers(client, version))
    assert response.status_code == status and response.json()["code"] == code, response.text
    safe(response)
    assert database_snapshot(engine) == before
