"""Protected receipt void must reverse once, retain cash and bind HTTP intent."""

import asyncio
import json
from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_audit as receipt_audit
import spm_api.dividend_receipt_void as voids
import spm_api.dividend_receipt_void_write_routes as routes
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.test_dividend_event_migration import add_posted_receipt
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_history_api import path as history_path
from tests.test_dividend_receipt_history_api import safe
from tests.test_dividend_receipt_void_inputs_api import create
from tests.test_dividend_receipt_write_api import body as create_body
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(identifier: str) -> str:
    return f"/api/v1/dividends/receipts/{identifier}/void"


async def inputs(
    client: httpx.AsyncClient, book: dict[str, Any], identifier: str
) -> dict[str, Any]:
    response = await client.get(f"{history_path(book)}/{identifier}/void-inputs")
    assert response.status_code == 200, response.text
    return dict(response.json())


def body(value: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: value[key]
            for key in (
                "event_id",
                "observed_revision_id",
                "observed_revision_no",
                "entitlement_id",
                "original_revision_id",
                "account_id",
                "portfolio_id",
                "broker_id",
                "account_version",
                "portfolio_version",
                "broker_version",
                "cash_entry_id",
            )
        },
        "expected_data_revision": value["portfolio_data_revision"],
        "reason": "synthetic-private recorded receipt correction reason",
        "confirmation": "VOID_RECORDED_CASH",
    }


async def test_void_posts_exact_reversal_and_replays_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    observed = await inputs(client, book, original["id"])
    intent = body(observed)
    headers = version_headers(client, 1, key="synthetic-receipt-void-key")
    before = database_snapshot(engine)
    response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value["id"] == original["id"]
    assert value["event_id"] == intent["event_id"]
    assert value["entitlement_id"] == original["entitlement_id"]
    assert value["original_revision_id"] == original["original_revision_id"]
    assert value["observed_revision_id"] == intent["observed_revision_id"]
    assert value["cash_entry_id"] == original["cash_entry_id"]
    assert value["reversal_entry_id"] != value["cash_entry_id"]
    assert value["status"] == "VOID" and value["version"] == "2"
    assert value["scope"] == "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY"
    assert value["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert value["eligibility_scope"] == "NOT_ESTABLISHED"
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT id,amount_micro,booked_hkd_micro,fx_rate_hkd_e12,entry_role "
            "FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' AND source_id=? "
            "ORDER BY entry_role",
            (original["id"],),
        ).all()
    assert [tuple(row) for row in rows] == [
        (original["cash_entry_id"], 6_000_000, 46_800_000, 7_800_000_000_000, "PRIMARY"),
        (value["reversal_entry_id"], -6_000_000, -46_800_000, 7_800_000_000_000, "REVERSAL"),
    ]
    after = database_snapshot(engine)
    allowed = {
        "audit_events",
        "cash_ledger_entries",
        "dividend_receipts",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: rows for key, rows in before.items() if key not in allowed} == {
        key: rows for key, rows in after.items() if key not in allowed
    }
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == value
    safe(replay)
    assert database_snapshot(engine) == after


async def test_concurrent_same_key_void_and_changed_intents_do_not_reverse_twice(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-receipt-void-race")
    first, second = await asyncio.gather(
        client.post(path(original["id"]), json=intent, headers=headers),
        client.post(path(original["id"]), json=intent, headers=headers),
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    after = database_snapshot(engine)
    changed = await client.post(
        path(original["id"]),
        json={**intent, "reason": "Different synthetic correction reason"},
        headers=headers,
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    different_key = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert different_key.status_code == 409
    assert different_key.json()["code"] == "DIVIDEND_RECEIPT_VOID_CONFLICT"
    assert database_snapshot(engine) == after


async def test_stale_or_foreign_scope_fails_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    before = database_snapshot(engine)
    for change, status in (
        ({"observed_revision_id": str(uuid4())}, 409),
        ({"entitlement_id": str(uuid4())}, 409),
        ({"original_revision_id": str(uuid4())}, 409),
        ({"portfolio_id": str(uuid4())}, 503),
        ({"broker_id": str(uuid4())}, 409),
        ({"account_version": "99"}, 409),
        ({"expected_data_revision": "99"}, 409),
        ({"cash_entry_id": str(uuid4())}, 409),
    ):
        response = await client.post(
            path(original["id"]), json={**intent, **change}, headers=version_headers(client, 1)
        )
        assert response.status_code == status, (list(change), response.status_code)
        safe(response)
    missing = await client.post(path(str(uuid4())), json=intent, headers=version_headers(client, 1))
    assert missing.status_code == 404
    safe(missing)
    stale_version = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 2)
    )
    assert stale_version.status_code == 412
    safe(stale_version)
    assert database_snapshot(engine) == before


async def test_unverified_legacy_receipt_cannot_be_adopted_or_reversed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        legacy = add_posted_receipt(con, book["older"], fx=None)
    observed = await inputs(client, book, legacy["id"])
    intent = {**body(observed), "cash_entry_id": str(uuid4())}
    before = database_snapshot(engine)
    response = await client.post(
        path(legacy["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_UNVERIFIED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_reauth_csrf_and_origin_precede_void_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path(original["id"]), json=intent, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    reauth = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert reauth.status_code == 200
    before = database_snapshot(engine)
    invalid = [
        {**headers, "Origin": "http://untrusted.test"},
        {**headers, "X-CSRF-Token": "x" * 32},
    ]
    for bad in invalid:
        response = await client.post(path(original["id"]), json=intent, headers=bad)
        assert response.status_code == 403
        safe(response)
    token, csrf = client.cookies.get(COOKIE), client.cookies.get(CSRF_COOKIE)
    assert token is not None and csrf is not None
    client.cookies.clear()
    response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 401
    safe(response)
    assert database_snapshot(engine) == before
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)


async def test_late_http_audit_failure_rolls_back_and_same_key_can_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-receipt-void-late-failure")
    before = database_snapshot(engine)
    with patch.object(routes, "audit", side_effect=ValueError("synthetic-private-diagnostic")):
        response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(path(original["id"]), json=intent, headers=headers)
    assert retry.status_code == 200, retry.text


async def test_replay_after_account_archive_remains_historical_and_zero_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-receipt-void-archive-replay")
    response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 200
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(book["account_id"]),),
        )
    before = database_snapshot(engine)
    replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert database_snapshot(engine) == before


async def test_scope_change_during_http_audit_rolls_back_the_reversal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        audit(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(book["account_id"]),)
        )

    with patch.object(routes, "audit", side_effect=changed):
        response = await client.post(
            path(original["id"]), json=intent, headers=version_headers(client, 1)
        )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["account", "portfolio", "broker", "data"])
async def test_scope_change_during_idempotency_insert_rolls_back_void(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    account, portfolio, broker = intent["account_id"], intent["portfolio_id"], intent["broker_id"]
    statement = {
        "account": f"UPDATE accounts SET version=version+1 WHERE id='{account}'",
        "portfolio": f"UPDATE portfolios SET version=version+1 WHERE id='{portfolio}'",
        "broker": f"UPDATE brokers SET version=version+1 WHERE id='{broker}'",
        "data": f"UPDATE portfolios SET data_revision=data_revision+1 WHERE id='{portfolio}'",
    }[problem]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_move_void_scope AFTER INSERT ON idempotency_keys "
            f"WHEN NEW.route='POST {path(original['id'])}' BEGIN {statement}; END"
        )
    before = database_snapshot(engine)
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["revoked", "csrf", "reauth", "owner"])
async def test_security_change_during_idempotency_insert_aborts_void(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    owner = str(book["owner"])
    statement = {
        "revoked": "UPDATE sessions SET revoked_at='2026-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{owner}'",
        "csrf": "UPDATE sessions SET csrf_sha256='" + "0" * 64 + f"' WHERE owner_id='{owner}'",
        "reauth": "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z' "
        f"WHERE owner_id='{owner}'",
        "owner": f"UPDATE owners SET is_active=0 WHERE id='{owner}'",
    }[problem]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_revoke_void_security AFTER INSERT ON idempotency_keys "
            f"WHEN NEW.route='POST {path(original['id'])}' BEGIN {statement}; END"
        )
    before = database_snapshot(engine)
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == (401 if problem in {"revoked", "owner"} else 403)
    safe(response)
    assert database_snapshot(engine) == before


async def test_duplicate_http_audit_after_marker_aborts_every_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_duplicate_void_http_audit AFTER INSERT ON idempotency_keys "
            f"WHEN NEW.route='POST {path(original['id'])}' BEGIN "
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,request_id,created_at) "
            f"SELECT '{uuid4()}',owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,request_id,created_at FROM audit_events "
            "WHERE entity_type='dividend_receipt_void_http_intent' "
            f"AND entity_id='{original['id']}'; END"
        )
    before = database_snapshot(engine)
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_ignored_idempotency_insert_cannot_commit_a_reversal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_ignore_void_marker BEFORE INSERT ON idempotency_keys "
            f"WHEN NEW.route='POST {path(original['id'])}' BEGIN SELECT RAISE(IGNORE); END"
        )
    before = database_snapshot(engine)
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 503 and response.json()["code"] == "IDEMPOTENCY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["missing-scope", "missing-receipt", "origin", "writer-conflict"]
)
async def test_replay_rechecks_retained_receipt_and_void_intent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-void-replay-integrity")
    first = await client.post(path(original["id"]), json=intent, headers=headers)
    assert first.status_code == 200, first.text
    before = database_snapshot(engine)
    read = receipt_audit.history

    def altered(*args: Any, **kwargs: Any) -> Any:
        retained = read(*args, **kwargs)
        assert retained is not None and len(retained) == 1
        if problem == "missing-scope":
            return None
        if problem == "missing-receipt":
            return ()
        return (replace(retained[0], origin=None),)

    target = (
        patch.object(
            voids, "void", side_effect=voids.ReceiptVoidError("DIVIDEND_RECEIPT_VOID_CONFLICT")
        )
        if problem == "writer-conflict"
        else patch.object(receipt_audit, "history", side_effect=altered)
    )
    with target:
        replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert replay.status_code == 503
    assert replay.json()["code"] == "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"
    safe(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["id", "status", "reversal"])
async def test_cached_void_response_cannot_substitute_receipt_or_reversal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-void-cache-integrity")
    first = await client.post(path(original["id"]), json=intent, headers=headers)
    assert first.status_code == 200, first.text
    forged = first.json()
    if problem == "status":
        forged["status"] = "ACTIVE"
    elif problem == "id":
        forged["id"] = str(uuid4())
    else:
        forged["reversal_entry_id"] = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE route=? AND idem_key=?",
            (json.dumps(forged), f"POST {path(original['id'])}", headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert replay.status_code == 503
    assert replay.json()["code"] == "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"
    safe(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("changes", "original_micro", "booked_micro", "fx"),
    [
        ({"received_amount": "-6", "fx_rate_hkd": None, "fx_source": None}, -6_000_000, None, None),
        (
            {"actual_hkd": "45.6", "actual_hkd_reason": "Synthetic actual HKD evidence"},
            6_000_000,
            45_600_000,
            7_800_000_000_000,
        ),
        (
            {"currency": "HKD", "fx_rate_hkd": None, "fx_source": None},
            6_000_000,
            6_000_000,
            1_000_000_000_000,
        ),
    ],
)
async def test_void_preserves_explicit_unknown_fx_override_and_identity(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    changes: dict[str, Any],
    original_micro: int,
    booked_micro: int | None,
    fx: int | None,
) -> None:
    payload = await create_body(client, book, **changes)
    created = await client.post(
        "/api/v1/dividends/receipts", json=payload, headers=version_headers(client, 2)
    )
    assert created.status_code == 201, created.text
    original = created.json()
    intent = body(await inputs(client, book, original["id"]))
    with unit_of_work(engine) as con:
        before = con.exec_driver_sql(
            "SELECT received_micro,received_hkd_micro,fx_rate_hkd_e12,tax_micro,fee_micro "
            "FROM dividend_receipts WHERE id=?",
            (original["id"],),
        ).one()
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 200, response.text
    safe(response)
    with unit_of_work(engine) as con:
        after = con.exec_driver_sql(
            "SELECT received_micro,received_hkd_micro,fx_rate_hkd_e12,tax_micro,fee_micro "
            "FROM dividend_receipts WHERE id=?",
            (original["id"],),
        ).one()
        entries = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12 FROM cash_ledger_entries "
            "WHERE source_type='DIVIDEND_RECEIPT' AND source_id=? ORDER BY entry_role",
            (original["id"],),
        ).all()
    assert tuple(after) == tuple(before)
    assert [tuple(row) for row in entries] == [
        (original_micro, booked_micro, fx),
        (-original_micro, -booked_micro if booked_micro is not None else None, fx),
    ]


async def test_archived_account_can_void_a_matched_recorded_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(book["account_id"]),),
        )
    observed = await inputs(client, book, original["id"])
    assert observed["account_status"] == "ARCHIVED"
    response = await client.post(
        path(original["id"]), json=body(observed), headers=version_headers(client, 1)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "VOID"
    safe(response)


async def test_foreign_owner_cannot_void_another_owners_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
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
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 404
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "bad", ['"0"', '"01"', '"9223372036854775808"', '"' + "9" * 100 + '"', "1"]
)
async def test_invalid_if_match_never_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], bad: str
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    before = database_snapshot(engine)
    headers = {**version_headers(client, 1), "If-Match": bad}
    response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before
