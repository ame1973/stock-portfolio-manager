"""Private receipt-history reads preserve historical links without granting financial use."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_receipt_history as history
import spm_api.dividend_receipt_models as models
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, reverse_receipt
from tests.test_dividend_event_writer import AT, next_intent
from tests.test_dividend_projection_writer import prepare
from tests.test_dividend_receipt_history import claim
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.fixture
async def book(client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]) -> dict[str, Any]:
    result = await prepare(client, engine, graph, holdings=False)
    first = result["event"]
    second = next_intent(first, "VERIFY")
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con, result["owner"], second, at=AT + timedelta(seconds=1), request_id=uuid4()
        )
        older = claim(first.revision_id, str(result["account_id"]), net=10_000_000)
        newer = claim(second.revision_id, str(result["account_id"]), net=11_000_000)
        insert(con, "dividend_entitlements", older)
        insert(con, "dividend_entitlements", newer)
    return {**result, "first": first, "second": second, "older": older, "newer": newer}


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}/accounts/{book['account_id']}/receipts"
    )


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert "synthetic-private-diagnostic" not in response.text
        assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_empty_history_is_not_a_zero_receipt_or_authorization(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    assert response.json() == {"items": [], "next_cursor": None}
    assert database_snapshot(engine) == before


async def test_paged_receipts_keep_their_historical_physical_revision_and_cash_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        older = add_posted_receipt(con, book["older"], fx=7_800_000_000_000)
        newer = add_posted_receipt(con, book["newer"], fx=None)
        unlinked = {
            "id": str(uuid4()),
            "entitlement_id": book["newer"]["id"],
            "account_id": str(book["account_id"]),
            "received_date": "2026-01-10",
            "received_micro": 2_000_000,
            "currency": "USD",
            "created_at": stamp(),
        }
        insert(con, "dividend_receipts", unlinked)
    before = database_snapshot(engine)
    first_page = await client.get(path(book), params={"limit": 1})
    assert first_page.status_code == 200
    safe(first_page)
    page = first_page.json()
    assert page["next_cursor"] and len(page["items"]) == 1
    item = page["items"][0]
    assert item["id"] == unlinked["id"] and item["entitlement_id"] == book["newer"]["id"]
    assert item["revision_id"] == str(book["second"].revision_id) and item["revision_no"] == 2
    assert item["current_revision_id"] == str(book["second"].revision_id)
    assert item["recorded_amount"] == "2.000000" and item["currency"] == "USD"
    assert item["cash_scope"] == "CASH_NOT_RECORDED" and item["received_hkd"] is None
    assert item["audit_scope"] == "RECEIPT_AUDIT_NOT_VERIFIED"
    assert item["history_completeness"] == "UNVERIFIED"
    assert item["scope"] == "UNVERIFIED_RECEIPT_HISTORY_ONLY"
    assert item["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert item["eligibility_scope"] == "NOT_ESTABLISHED"
    items = [item]
    cursor = page["next_cursor"]
    while cursor is not None:
        response = await client.get(path(book), params={"limit": 1, "cursor": cursor})
        assert response.status_code == 200
        safe(response)
        page = response.json()
        items.extend(page["items"])
        cursor = page["next_cursor"]
    assert {row["id"] for row in items} == {older["id"], newer["id"], unlinked["id"]}
    historical = next(row for row in items if row["id"] == older["id"])
    assert historical["revision_id"] == str(book["first"].revision_id)
    assert historical["revision_no"] == 1 and historical["fx_rate_hkd"] == "7.800000000000"
    assert historical["recorded_amount"] == "6.000000" and historical["received_hkd"] == "46.800000"
    assert historical["cash_scope"] == "MATCHING_CASH_RECORDED_UNVERIFIED"
    assert historical["audit_scope"] == "RECEIPT_AUDIT_NOT_VERIFIED"
    assert database_snapshot(engine) == before


async def test_receipt_page_uses_one_wal_snapshot_when_another_writer_appends(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        first = add_posted_receipt(con, book["older"], fx=None)
    original = history.history
    appended: list[dict[str, Any]] = []

    def append_receipt() -> dict[str, Any]:
        with unit_of_work(engine, write=True) as con:
            return add_posted_receipt(con, book["newer"], fx=None)

    def read_then_append(*args: Any, **kwargs: Any) -> Any:
        retained = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            appended.append(pool.submit(append_receipt).result(timeout=10))
        return retained

    with patch.object(history, "history", side_effect=read_then_append):
        response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    assert [row["id"] for row in response.json()["items"]] == [first["id"]]
    before_latest = database_snapshot(engine)
    latest = await client.get(path(book))
    assert latest.status_code == 200
    safe(latest)
    assert {row["id"] for row in latest.json()["items"]} == {first["id"], appended[0]["id"]}
    assert database_snapshot(engine) == before_latest


async def test_void_receipt_preserves_original_amount_and_exact_reversal_label(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = add_posted_receipt(con, book["older"], fx=7_800_000_000_000)
        reverse_receipt(con, receipt, owner=book["owner"])
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(), receipt["id"]),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    item = response.json()["items"][0]
    assert item["status"] == "VOID" and item["version"] == "2"
    assert item["recorded_amount"] == "6.000000" and item["fx_rate_hkd"] == "7.800000000000"
    assert item["cash_scope"] == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert item["audit_scope"] == "RECEIPT_AUDIT_NOT_VERIFIED"
    assert database_snapshot(engine) == before


async def test_missing_or_foreign_scope_and_session_are_not_disclosed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    for bad in (
        path({**book, "account_id": uuid4()}),
        f"/api/v1/dividends/events/{uuid4()}/accounts/{book['account_id']}/receipts",
    ):
        response = await client.get(bad)
        assert response.status_code == 404
        safe(response)
    assert database_snapshot(engine) == before
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
    before_foreign = database_snapshot(engine)
    foreign = await client.get(path(book))
    assert foreign.status_code == 404 and foreign.json()["code"] == "NOT_FOUND"
    safe(foreign)
    assert database_snapshot(engine) == before_foreign
    client.cookies.clear()
    unauthorized = await client.get(path(book))
    assert unauthorized.status_code == 401
    safe(unauthorized)
    assert database_snapshot(engine) == before_foreign


async def test_invalid_cursor_fails_without_reading_or_writing_private_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book), params={"cursor": "not-a-valid-cursor"})
    assert response.status_code == 422
    safe(response)
    assert response.json()["code"] == "INVALID_CURSOR"
    noncanonical = base64.urlsafe_b64encode(
        json.dumps(["20260109", str(uuid4())]).encode()
    ).decode()
    response = await client.get(path(book), params={"cursor": noncanonical})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    safe(response)
    assert database_snapshot(engine) == before


async def test_search_is_literal_and_receipts_from_other_owned_accounts_stay_separate(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        own = add_posted_receipt(con, book["older"], fx=None)
        other_claim = claim(book["first"].revision_id, graph["target"]["id"], net=5_000_000)
        insert(con, "dividend_entitlements", other_claim)
        foreign_scope = add_posted_receipt(con, other_claim, fx=None)
    before = database_snapshot(engine)
    literal = await client.get(path(book), params={"q": "%_\\\\"})
    assert literal.status_code == 200 and literal.json()["items"] == []
    found = await client.get(path(book), params={"q": own["id"]})
    assert found.status_code == 200 and [item["id"] for item in found.json()["items"]] == [
        own["id"]
    ]
    hidden = await client.get(path(book), params={"q": foreign_scope["id"]})
    assert hidden.status_code == 200 and hidden.json()["items"] == []
    for response in (literal, found, hidden):
        safe(response)
    assert database_snapshot(engine) == before


async def test_unknown_tax_and_fee_remain_null_and_explicit_zero_remains_zero(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    unknown, zero = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        base = {
            "entitlement_id": book["older"]["id"],
            "account_id": str(book["account_id"]),
            "received_date": "2026-01-10",
            "received_micro": 2_000_000,
            "currency": "USD",
            "created_at": stamp(),
        }
        insert(con, "dividend_receipts", {**base, "id": unknown})
        insert(con, "dividend_receipts", {**base, "id": zero, "tax_micro": 0, "fee_micro": 0})
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    rows = {row["id"]: row for row in response.json()["items"]}
    assert rows[unknown]["recorded_tax"] is None and rows[unknown]["recorded_fee"] is None
    assert rows[zero]["recorded_tax"] == rows[zero]["recorded_fee"] == "0.000000"
    assert database_snapshot(engine) == before


async def test_exact_int64_limits_are_serialized_as_decimal_strings(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values = [9_223_372_036_854_775_807, -9_223_372_036_854_775_807]
    with unit_of_work(engine, write=True) as con:
        for value in values:
            insert(
                con,
                "dividend_receipts",
                {
                    "id": str(uuid4()),
                    "entitlement_id": book["older"]["id"],
                    "account_id": str(book["account_id"]),
                    "received_date": "2026-01-10",
                    "received_micro": value,
                    "currency": "USD",
                    "created_at": stamp(),
                },
            )
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    assert {row["recorded_amount"] for row in response.json()["items"]} == {
        "9223372036854.775807",
        "-9223372036854.775807",
    }
    assert all(row["cash_scope"] == "CASH_NOT_RECORDED" for row in response.json()["items"])
    assert database_snapshot(engine) == before


async def test_large_receipt_version_uses_an_exact_string_on_the_wire(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        add_posted_receipt(con, book["older"], fx=None)
    with unit_of_work(engine) as con:
        retained = history.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert retained is not None
    entitlement = retained.entitlements[0]
    modified = replace(entitlement.receipts[0], version=9_223_372_036_854_775_807)
    mocked = replace(retained, entitlements=(replace(entitlement, receipts=(modified,)),))
    before = database_snapshot(engine)
    with patch.object(history, "history", return_value=mocked):
        response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    assert response.json()["items"][0]["version"] == "9223372036854775807"
    assert database_snapshot(engine) == before


async def test_duplicate_retained_receipt_identity_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        add_posted_receipt(con, book["older"], fx=None)
    with unit_of_work(engine) as con:
        retained = history.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert retained is not None
    corrupted = replace(retained, entitlements=(retained.entitlements[0], retained.entitlements[0]))
    before = database_snapshot(engine)
    with patch.object(history, "history", return_value=corrupted):
        response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ACTIVE", "voided_at": "2099-01-01T00:00:00Z"},
        {"status": "VOID", "voided_at": None},
        {"status": "VOID", "voided_at": "2026-01-01T00:00:00Z"},
        {"status": "ACTIVE", "cash_scope": "CASH_REVERSAL_NOT_RECORDED"},
        {"status": "ACTIVE", "cash_scope": "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"},
        {
            "status": "VOID",
            "voided_at": "2099-01-01T00:00:00Z",
            "cash_scope": "MATCHING_CASH_RECORDED_UNVERIFIED",
        },
        {
            "status": "VOID",
            "voided_at": "2099-01-01T00:00:00Z",
            "cash_scope": "CASH_NOT_RECORDED",
            "version": "1",
        },
        {"version": "01"},
        {"version": "9223372036854775808"},
        {"recorded_amount": 0.1},
        {"recorded_tax": "-1"},
        {"currency": "CNY"},
        {"audit_scope": "VERIFIED"},
        {"scope": "GRANTED"},
        {"eligibility_scope": "ESTABLISHED"},
    ],
)
async def test_recorded_receipt_model_rejects_unsafe_cached_or_forged_fields(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], changes: dict[str, object]
) -> None:
    with unit_of_work(engine, write=True) as con:
        add_posted_receipt(con, book["older"], fx=None)
    response = await client.get(path(book))
    assert response.status_code == 200
    valid = response.json()["items"][0]
    retained = models.RecordedReceipt.model_validate(valid)
    assert valid["recorded_amount"] not in repr(retained) and valid["received_date"] not in repr(
        retained
    )
    with pytest.raises(ValidationError):
        models.RecordedReceipt.model_validate({**valid, **changes})


async def test_corrupt_unseen_receipt_cannot_be_hidden_by_limit_or_search(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        first = add_posted_receipt(con, book["older"], fx=None)
        second = add_posted_receipt(con, book["newer"], fx=None)
    before = database_snapshot(engine)
    real = history._receipt

    def unavailable(con: Any, row: Any, account_id: Any) -> Any:
        if row["id"] == second["id"]:
            raise ValueError("synthetic-private-diagnostic")
        return real(con, row, account_id)

    with patch.object(history, "_receipt", side_effect=unavailable):
        response = await client.get(path(book), params={"limit": 1, "q": first["id"]})
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before
