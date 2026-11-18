"""Receipt input reads expose existing recorded scope without authorizing cash or eligibility."""

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
from sqlalchemy import Engine

import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_receipt_history as history
import spm_api.dividend_receipt_routes as routes
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account as recorded_account
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_writer import AT, manual, next_intent
from tests.test_dividend_receipt_history import claim
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_history_api import path as receipt_path
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any]) -> str:
    return f"{receipt_path(book)}/inputs"


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "synthetic-private" not in response.text


async def test_inputs_page_existing_physical_entitlements_without_granting_eligibility(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    first = await client.get(path(book), params={"limit": 1})
    assert first.status_code == 200, first.text
    safe(first)
    scope = first.json()
    assert scope["event_id"] == str(book["event"].event_id)
    assert scope["revision_id"] == str(book["second"].revision_id)
    assert scope["revision_no"] == book["second"].revision_no
    assert scope["account_id"] == str(book["account_id"])
    assert scope["portfolio_id"] == str(book["portfolio_id"])
    assert scope["broker_id"] == str(book["broker_id"])
    assert scope["account_status"] == "ACTIVE"
    assert scope["scope"] == "RECORDED_RECEIPT_INPUTS_ONLY"
    assert scope["history_completeness"] == "UNVERIFIED"
    assert scope["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert scope["eligibility_scope"] == "NOT_ESTABLISHED"
    item = scope["entitlements"]["items"][0]
    assert item["id"] == book["newer"]["id"]
    assert item["revision_id"] == str(book["second"].revision_id) and item["revision_no"] == 2
    assert item["recorded_shares"] == "10.00000000"
    assert item["recorded_expected_net"] == "11.000000" and item["currency"] == "USD"
    assert item["recorded_quality"] == "ACCRUED"
    assert item["scope"] == "LEGACY_ENTITLEMENT_NOT_VERIFIED"
    cursor = scope["entitlements"]["next_cursor"]
    assert cursor is not None
    second = await client.get(path(book), params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200
    safe(second)
    older = second.json()["entitlements"]
    assert older["next_cursor"] is None and len(older["items"]) == 1
    assert older["items"][0]["id"] == book["older"]["id"]
    assert older["items"][0]["revision_id"] == str(book["first"].revision_id)
    assert older["items"][0]["recorded_expected_net"] == "10.000000"
    assert database_snapshot(engine) == before


async def test_empty_search_still_returns_explicit_scope_without_inventing_a_basis(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book), params={"q": "%_\\"})
    assert response.status_code == 200
    safe(response)
    result = response.json()
    assert result["entitlements"] == {"items": [], "next_cursor": None}
    assert result["eligibility_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_new_event_without_entitlements_returns_an_explicit_empty_basis_page(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = manual(terms=book["event"].terms)
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, book["owner"], event, at=AT, request_id=uuid4())
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path({**book, "event": event}))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["entitlements"] == {"items": [], "next_cursor": None}
    assert result["event_id"] == str(event.event_id)
    assert result["scope"] == "RECORDED_RECEIPT_INPUTS_ONLY"
    assert result["eligibility_scope"] == "NOT_ESTABLISHED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_inputs_hide_foreign_scopes_and_require_a_live_session(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    missing = await client.get(path({**book, "account_id": uuid4()}))
    assert missing.status_code == 404
    safe(missing)
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
    before = database_snapshot(engine)
    foreign = await client.get(path(book))
    assert foreign.status_code == 404 and foreign.json()["code"] == "NOT_FOUND"
    safe(foreign)
    client.cookies.clear()
    unauthorized = await client.get(path(book))
    assert unauthorized.status_code == 401
    safe(unauthorized)
    assert database_snapshot(engine) == before


async def test_scope_and_entitlements_share_one_wal_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = history.history

    def change_account() -> None:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE accounts SET version=version+1 WHERE id=?", (str(book["account_id"]),)
            )

    def read_then_change(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(change_account).result(timeout=10)
        return result

    with patch.object(history, "history", side_effect=read_then_change):
        response = await client.get(path(book))
    assert response.status_code == 200, response.text
    previous = response.json()
    assert previous["account_version"] == str(book["account_version"])
    after = database_snapshot(engine)
    latest = await client.get(path(book))
    assert latest.status_code == 200
    assert int(latest.json()["account_version"]) == int(previous["account_version"]) + 1
    assert latest.json()["entitlements"] == previous["entitlements"]
    assert database_snapshot(engine) == after


async def test_entitlement_append_after_history_load_is_invisible_until_next_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = manual(terms=book["event"].terms)
    second = next_intent(first, "VERIFY")
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, book["owner"], first, at=AT, request_id=uuid4())
        event_writer.save(
            con, book["owner"], second, at=AT + timedelta(seconds=1), request_id=uuid4()
        )
    new_basis = claim(first.revision_id, str(book["account_id"]), net=10_000_000)
    original = history.history

    def append_basis() -> None:
        with unit_of_work(engine, write=True) as con:
            insert(con, "dividend_entitlements", new_basis)

    def read_then_append(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(append_basis).result(timeout=10)
        return result

    target = path({**book, "event": first})
    with patch.object(history, "history", side_effect=read_then_append):
        response = await client.get(target)
    assert response.status_code == 200, response.text
    assert response.json()["entitlements"] == {"items": [], "next_cursor": None}
    before_latest = database_snapshot(engine)
    latest = await client.get(target)
    assert latest.status_code == 200
    assert [item["id"] for item in latest.json()["entitlements"]["items"]] == [new_basis["id"]]
    assert latest.json()["entitlements"]["items"][0]["revision_id"] == str(first.revision_id)
    assert latest.json()["revision_id"] == str(second.revision_id)
    assert database_snapshot(engine) == before_latest


async def test_maximum_version_pins_are_exact_and_archived_status_remains_explicit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET version=?,status='ARCHIVED' WHERE id=?",
            (INT64_MAX, str(book["account_id"])),
        )
        con.exec_driver_sql(
            "UPDATE portfolios SET version=?,data_revision=? WHERE id=?",
            (INT64_MAX, INT64_MAX, str(book["portfolio_id"])),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["account_status"] == "ARCHIVED"
    assert result["account_version"] == result["portfolio_version"] == str(INT64_MAX)
    assert result["portfolio_data_revision"] == str(INT64_MAX)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["duplicate", "revision-id", "revision-number"])
async def test_forged_retained_basis_cannot_be_selected_or_disclosed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    original = history.history

    def forged(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        assert result is not None and len(result.entitlements) == 2
        older, newer = result.entitlements
        if problem == "duplicate":
            return replace(result, entitlements=(older, older))
        if problem == "revision-id":
            newer = replace(newer, revision_id=uuid4())
        else:
            newer = replace(newer, revision_no=newer.revision_no + 1)
        return replace(result, entitlements=(older, newer))

    before = database_snapshot(engine)
    with patch.object(history, "history", side_effect=forged):
        response = await client.get(path(book), params={"q": book["newer"]["id"]})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("table", "column", "identifier"),
    [
        ("accounts", "version", "account_id"),
        ("portfolios", "version", "portfolio_id"),
        ("portfolios", "data_revision", "portfolio_id"),
        ("brokers", "version", "broker_id"),
    ],
)
async def test_fractional_database_scope_versions_fail_closed(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    table: str,
    column: str,
    identifier: str,
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(f"UPDATE {table} SET {column}=1.5 WHERE id=?", (str(book[identifier]),))
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_inconsistent_current_account_scope_fails_without_disclosing_values(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    def different_portfolio(*args: Any, **kwargs: Any) -> Any:
        current = recorded_account(*args, **kwargs)
        assert current is not None
        return {**current, "portfolio_id": str(uuid4())}

    before = database_snapshot(engine)
    with patch.object(routes, "account", side_effect=different_portfolio):
        response = await client.get(path(book))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_invalid_cursor_and_inconsistent_hidden_history_fail_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    cursor = base64.urlsafe_b64encode(json.dumps(["0", str(uuid4())]).encode()).decode()
    invalid = await client.get(path(book), params={"cursor": cursor})
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_CURSOR"
    safe(invalid)
    original = history.history

    def wrong_scope(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        assert result is not None
        return replace(result, event_id=uuid4())

    with patch.object(history, "history", side_effect=wrong_scope):
        unavailable = await client.get(path(book), params={"q": book["older"]["id"]})
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "DIVIDEND_RECEIPT_INPUTS_UNAVAILABLE"
    safe(unavailable)
    assert database_snapshot(engine) == before
