"""Owner-scoped HTTP contract for explicit dividend fee estimates."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import audit, now
from spm_api.db import unit_of_work
from spm_api.security import COOKIE
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
REASON = "Synthetic owner fee estimate, not a confirmed broker charge."


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account']['id']}/fee-settings"
    )


def body(
    scope: dict[str, Any],
    *,
    fee_status: str = "ESTIMATED",
    fee: Any = "0",
    currency: str | None = "HKD",
    day: str = "2026-01-01",
) -> dict[str, Any]:
    return {
        "revision_id": scope["revision_id"],
        "portfolio_id": scope["portfolio_id"],
        "broker_id": scope["broker_id"],
        "account_version": scope["account_version"],
        "broker_version": scope["broker_version"],
        "expected_head_no": scope["head_no"],
        "expected_head_id": scope["head_id"],
        "effective_from": day,
        "fee_status": fee_status,
        "fee": fee,
        "fee_currency": currency,
        "reason": REASON,
    }


async def selection(client: httpx.AsyncClient, book: dict[str, Any]) -> dict[str, Any]:
    result = await client.get(path(book), params={"effective_on": "2026-01-08"})
    assert result.status_code == 200, result.text
    safe(result)
    return dict(result.json())


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        for private in (REASON, "private-value", "123456.789012"):
            assert private not in response.text


def only_fee(before: Any, after: Any) -> None:
    allowed = {
        "dividend_fee_settings",
        "audit_events",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: rows for key, rows in before.items() if key not in allowed} == {
        key: rows for key, rows in after.items() if key not in allowed
    }


async def test_missing_and_explicit_zero_are_audited_and_replay_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    missing = await selection(client, book)
    assert (missing["fee_status"], missing["selected"], missing["head_no"], missing["head_id"]) == (
        "MISSING",
        None,
        "0",
        None,
    )
    assert missing["revision_rejection"] == "NOT_REJECTED"
    assert missing["selected_scope"] == "NO_SELECTED_SETTING"
    assert missing["current_use_scope"] == "REVALIDATION_REQUIRED"
    headers = version_headers(client, 1, key="synthetic-fee-http-replay")
    response = await client.post(path(book), json=body(missing), headers=headers)
    assert response.status_code == 201, response.text
    safe(response)
    saved = response.json()
    assert saved["fee_status"] == "ESTIMATED" and saved["fee"] == "0.000000"
    assert saved["fee_currency"] == "HKD" and saved["setting_no"] == "1"
    assert saved["scope"] == "OWNER_ESTIMATE_ONLY"
    assert saved["current_use_scope"] == "REVALIDATION_REQUIRED"
    after = database_snapshot(engine)
    only_fee(before, after)
    assert len(after["dividend_fee_settings"]) == len(before["dividend_fee_settings"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 2
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT fee_micro,request_id FROM dividend_fee_settings WHERE id=?", (saved["id"],)
        ).one()
        assert row.fee_micro == 0 and row.request_id == response.headers["x-request-id"]
        types = (
            con.exec_driver_sql(
                "SELECT entity_type FROM audit_events WHERE entity_id=? ORDER BY entity_type",
                (saved["id"],),
            )
            .scalars()
            .all()
        )
        assert types == ["dividend_fee_http_intent", "dividend_fee_setting"]
    replay = await client.post(path(book), json=body(missing), headers=headers)
    assert replay.status_code == 201 and replay.json() == saved
    assert database_snapshot(engine) == after
    current = await selection(client, book)
    assert current["selected"] == saved and current["selected_scope"] == "ACCOUNT_SCOPE_MATCH"


async def test_unknown_currency_backdated_history_and_keyset_pagination(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first_scope = await selection(client, book)
    first = await client.post(
        path(book),
        json=body(first_scope, fee="2", currency="USD", day="2026-06-01"),
        headers=version_headers(client, 1),
    )
    assert first.status_code == 201
    future = await selection(client, book)
    assert future["head_no"] == "1" and future["fee_status"] == "MISSING"
    unknown_response = await client.post(
        path(book),
        json=body(future, fee_status="UNKNOWN", fee=None, currency="USD"),
        headers=version_headers(client, 1),
    )
    assert unknown_response.status_code == 201
    unknown = await selection(client, book)
    assert unknown["fee_status"] == "UNKNOWN"
    assert unknown["selected"]["fee"] is None and unknown["selected"]["fee_currency"] == "USD"
    zero = await client.post(
        path(book),
        json=body(unknown, fee="0", currency="HKD"),
        headers=version_headers(client, 1),
    )
    assert zero.status_code == 201
    latest = await selection(client, book)
    assert latest["selected"]["id"] == zero.json()["id"] and latest["head_no"] == "3"
    params: dict[str, Any] = {
        "revision_id": str(book["event"].revision_id),
        "revision_no": 1,
        "limit": 2,
    }
    before = database_snapshot(engine)
    page = await client.get(path(book) + "/history", params=params)
    assert page.status_code == 200
    safe(page)
    assert [item["setting_no"] for item in page.json()["items"]] == ["3", "2"]
    last = await client.get(
        path(book) + "/history", params={**params, "cursor": page.json()["next_cursor"]}
    )
    assert last.status_code == 200 and [item["setting_no"] for item in last.json()["items"]] == [
        "1"
    ]
    assert last.json()["next_cursor"] is None
    assert database_snapshot(engine) == before


async def test_rejection_is_explicit_new_write_is_fenced_and_replay_is_historical(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-before-rejection")
    response = await client.post(path(book), json=body(scope), headers=headers)
    assert response.status_code == 201
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        rejected = rejection_writer.save(
            con,
            UUID(book["owner"]["id"]),
            rejection_writer.RejectionIntent(
                id=uuid4(),
                event_id=event.event_id,
                revision_id=event.revision_id,
                revision_no=1,
                citation="Synthetic rejection reference",
                reason=REASON,
            ),
            request_id=uuid4(),
        )
    current = await selection(client, book)
    assert current["revision_rejection"] == "REJECTED" and current["selected"] == response.json()
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=body(current), headers=version_headers(client, 1))
    assert denied.status_code == 409 and denied.json()["code"] == "DIVIDEND_REVISION_REJECTED"
    safe(denied)
    replay = await client.post(path(book), json=body(scope), headers=headers)
    assert replay.status_code == 201 and replay.json() == response.json()
    assert database_snapshot(engine) == before
    successor = event_writer.EventIntent(
        event_id=event.event_id,
        revision_id=uuid4(),
        revision_no=2,
        kind="VERIFY",
        citation="Synthetic resolution",
        reason=REASON,
        resolve_rejection_id=rejected.id,
    )
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, UUID(book["owner"]["id"]), successor, at=now(), request_id=uuid4())
    before = database_snapshot(engine)
    new_scope = await selection(client, book)
    assert new_scope["revision_id"] == str(successor.revision_id)
    assert (
        new_scope["fee_status"] == "MISSING" and new_scope["revision_rejection"] == "NOT_REJECTED"
    )
    replay = await client.post(path(book), json=body(scope), headers=headers)
    assert replay.status_code == 201 and replay.json() == response.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_write_requires_session_origin_csrf_and_idempotency(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1)
    if failure == "cookie":
        client.cookies.delete(COOKIE)
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "synthetic-invalid"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        headers.pop("Idempotency-Key")
    before = database_snapshot(engine)
    response = await client.post(path(book), json=body(scope), headers=headers)
    assert response.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"fee": 0.0},
        {"fee": "0.0000001"},
        {"fee": "-1"},
        {"fee_status": "UNKNOWN", "fee": "0"},
        {"fee_status": "ESTIMATED", "fee": None},
        {"fee_status": "ESTIMATED", "fee_currency": None},
        {"fee_status": "UNKNOWN", "fee": None, "fee_currency": "CNY"},
        {"account_version": 1},
        {"broker_version": "01"},
        {"expected_head_no": "9223372036854775807"},
        {"expected_head_no": "0", "expected_head_id": str(uuid4())},
    ],
)
async def test_invalid_inputs_are_not_rounded_echoed_or_written(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json={**body(scope), **changes}, headers=version_headers(client, 1)
    )
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


async def test_reauth_is_checked_again_before_historical_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-reauth")
    initial = await client.post(path(book), json=body(scope), headers=headers)
    assert initial.status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=body(scope), headers=headers)
    assert denied.status_code == 403
    safe(denied)
    assert database_snapshot(engine) == before
    reauth = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert reauth.status_code == 200
    after_reauth = database_snapshot(engine)
    replay = await client.post(path(book), json=body(scope), headers=headers)
    assert replay.status_code == 201 and replay.json() == initial.json()
    assert database_snapshot(engine) == after_reauth


async def test_cached_response_substitution_or_http_intent_corruption_is_rejected(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-substitution")
    response = await client.post(path(book), json=body(scope), headers=headers)
    assert response.status_code == 201
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_fee_http_intent",
            response.json()["id"],
            "DIVIDEND_FEE_HTTP_INTENT",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason="",
        )
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=body(scope), headers=headers)
    assert denied.status_code == 503
    safe(denied)
    assert database_snapshot(engine) == before


async def test_competing_http_requests_produce_one_head_and_one_conflict(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    first, second = await asyncio.gather(
        client.post(path(book), json=body(scope, fee="1"), headers=version_headers(client, 1)),
        client.post(path(book), json=body(scope, fee="2"), headers=version_headers(client, 1)),
    )
    assert sorted([first.status_code, second.status_code]) == [201, 412]
    after = database_snapshot(engine)
    only_fee(before, after)
    assert len(after["dividend_fee_settings"]) == len(before["dividend_fee_settings"]) + 1
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
