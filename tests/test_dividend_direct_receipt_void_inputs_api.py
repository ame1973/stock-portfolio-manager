"""Direct void inputs retain original cash and current owned pins without posting a reversal."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as receipts
import spm_api.dividend_direct_receipt_void as voids
import spm_api.dividend_event_service as events
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_direct_receipt_input_models import DirectReceiptVoidInputs
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_combined_receipt_history import add_legacy
from tests.test_dividend_direct_receipt_inputs_api import book as book
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_direct_receipt_void import void_intent
from tests.test_dividend_direct_receipt_write_api import PATH, body
from tests.test_dividend_receipt_write_api import safe
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any], receipt_id: str) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/recorded-receipts/{receipt_id}/void-inputs"
    )


async def create(client: httpx.AsyncClient, book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    response = await client.post(
        PATH, json=await body(client, book, **changes), headers=version_headers(client, 1)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_direct_void_inputs_bind_original_cash_and_current_scope_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt = await create(client, book)
    before = database_snapshot(engine)
    response = await client.get(path(book, receipt["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value["scope"] == "DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY"
    assert value["cash_entry_id"] == receipt["cash_entry_id"]
    assert value["origin_portfolio_id"] == receipt["portfolio_id"]
    current, recorded = value["current"], value["receipt"]
    assert current["event_id"] == recorded["event_id"] == receipt["event_id"]
    assert current["account_id"] == recorded["account_id"] == receipt["account_id"]
    assert recorded["id"] == receipt["id"]
    assert recorded["entitlement_id"] is None
    assert recorded["receipt_basis"] == current["receipt_basis"] == "DIRECT_EVENT_ACCOUNT"
    assert recorded["recorded_amount"] == "6.000000"
    assert recorded["received_hkd"] == "46.800000"
    assert recorded["fx_rate_hkd"] == "7.800000000000"
    assert recorded["recorded_tax"] is None and recorded["recorded_fee"] == "0.000000"
    assert recorded["status"] == "ACTIVE" and recorded["version"] == "1"
    assert current["portfolio_data_revision"] == str(int(receipt["portfolio_data_revision"]) + 1)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"fx_rate_hkd": None, "fx_source": None, "tax": None, "fee": None},
        {"currency": "HKD", "fx_rate_hkd": None, "fx_source": None},
        {"actual_hkd": "47", "actual_hkd_reason": "Synthetic original cash adjustment"},
        {"received_amount": "-6"},
        {"received_amount": "0"},
        {"received_amount": "9223372036854.775807", "fx_rate_hkd": None, "fx_source": None},
    ],
)
async def test_original_cash_is_not_repriced_or_unknowns_filled(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    created = await create(client, book, **changes)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    recorded = response.json()["receipt"]
    for old, new in (
        ("received_amount", "recorded_amount"),
        ("received_hkd", "received_hkd"),
        ("fx_rate_hkd", "fx_rate_hkd"),
        ("tax", "recorded_tax"),
        ("fee", "recorded_fee"),
    ):
        assert recorded[new] == created[old]
    assert database_snapshot(engine) == before


async def test_voided_archived_and_exhausted_scope_stays_readable_without_a_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    with unit_of_work(engine) as con:
        records = receipts.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert records is not None and len(records) == 1
    intent = void_intent(engine, (book["event"], str(book["account_id"])), records[0])
    with unit_of_work(engine, write=True) as con:
        voids.void(con, book["owner"], intent, request_id=uuid4())
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=? WHERE id=?",
            (INT64_MAX, str(book["account_id"])),
        )
        con.exec_driver_sql(
            "UPDATE portfolios SET version=?,data_revision=? WHERE id=?",
            (INT64_MAX, INT64_MAX, str(book["portfolio_id"])),
        )
        con.exec_driver_sql(
            "UPDATE brokers SET version=? WHERE id=?", (INT64_MAX, str(book["broker_id"]))
        )
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    current, recorded = response.json()["current"], response.json()["receipt"]
    assert current["account_status"] == "ARCHIVED"
    for key in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "portfolio_data_revision",
    ):
        assert current[key] == str(INT64_MAX)
    assert recorded["status"] == "VOID" and recorded["version"] == "2"
    assert recorded["voided_at"] is not None
    assert recorded["cash_scope"] == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert recorded["recorded_amount"] == created["received_amount"]
    assert recorded["received_hkd"] == created["received_hkd"]
    assert response.json()["cash_entry_id"] == created["cash_entry_id"]
    assert "can_void" not in response.text
    assert database_snapshot(engine) == before


async def test_moved_account_retains_original_portfolio_and_current_scope_separately(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    portfolio = await client.post(
        "/api/v1/portfolios",
        json={"name": "Synthetic moved receipt portfolio"},
        headers=write_headers(client),
    )
    assert portfolio.status_code == 201
    destination = portfolio.json()["id"]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,version=version+1 WHERE id=?",
            (destination, str(book["account_id"])),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    assert response.json()["origin_portfolio_id"] == created["portfolio_id"]
    assert response.json()["current"]["portfolio_id"] == destination
    assert response.json()["current"]["account_version"] == str(int(created["account_version"]) + 1)
    assert database_snapshot(engine) == before


async def test_mixed_history_is_valid_but_a_legacy_id_cannot_be_adopted_as_direct(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    legacy = add_legacy(engine, (book["event"], str(book["account_id"])))
    created = await create(client, book)
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    missing = await client.get(path(book, legacy["receipt"]["id"]))
    assert missing.status_code == 404
    safe(missing)
    assert database_snapshot(engine) == before


async def test_missing_foreign_cross_account_and_unauthenticated_scopes_are_private(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    created = await create(client, book)
    before = database_snapshot(engine)
    for url in (
        path(book, str(uuid4())),
        path({**book, "account_id": uuid4()}, created["id"]),
        path({**book, "account_id": graph["target"]["id"]}, created["id"]),
        path(book, created["id"]).replace(str(book["event"].event_id), str(uuid4())),
    ):
        response = await client.get(url)
        assert response.status_code == 404
        safe(response)
    malformed = await client.get(path(book, "not-a-uuid"))
    assert malformed.status_code == 422
    safe(malformed)
    assert database_snapshot(engine) == before
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
    before_foreign = database_snapshot(engine)
    foreign = await client.get(path(book, created["id"]))
    assert foreign.status_code == 404
    safe(foreign)
    client.cookies.clear()
    unauthenticated = await client.get(path(book, created["id"]))
    assert unauthenticated.status_code == 401
    safe(unauthenticated)
    assert database_snapshot(engine) == before_foreign


async def test_unsealed_sibling_prevents_a_partial_void_input_response(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(book["event"], str(book["account_id"])))
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 503
    assert response.json()["code"] == "DIRECT_RECEIPT_VOID_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["missing", "error", "record", "duplicate"])
async def test_unavailable_or_substituted_history_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    created = await create(client, book)
    with unit_of_work(engine) as con:
        records = receipts.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert records is not None
    options: dict[str, Any]
    if failure == "error":
        options = {"side_effect": ValueError("synthetic-private-history-error")}
    else:
        options = {
            "return_value": {
                "missing": None,
                "record": (object(),),
                "duplicate": records * 2,
            }[failure]
        }
    before = database_snapshot(engine)
    with patch.object(receipts, "history", **options):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 503
    assert response.json()["code"] == "DIRECT_RECEIPT_VOID_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["void", "account"])
async def test_concurrent_void_or_account_change_cannot_mix_wal_snapshots(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    created = await create(client, book)
    with unit_of_work(engine) as con:
        records = receipts.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert records is not None
    intent = void_intent(engine, (book["event"], str(book["account_id"])), records[0])
    previous = (await client.get(path(book, created["id"]))).json()
    original = events.get
    appended = False

    def write() -> None:
        with unit_of_work(engine, write=True) as con:
            if change == "void":
                voids.void(con, book["owner"], intent, request_id=uuid4())
            else:
                con.exec_driver_sql(
                    "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
                    (str(book["account_id"]),),
                )

    def read_then_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal appended
        result = original(*args, **kwargs)
        if not appended:
            appended = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write).result(timeout=30)
        return result

    with patch.object(events, "get", side_effect=read_then_write):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    assert response.json() == previous
    before = database_snapshot(engine)
    latest = await client.get(path(book, created["id"]))
    assert latest.status_code == 200, latest.text
    safe(latest)
    assert latest.json() != previous
    if change == "void":
        assert latest.json()["receipt"]["status"] == "VOID"
        assert latest.json()["receipt"]["version"] == "2"
        assert (
            int(latest.json()["current"]["portfolio_data_revision"])
            == int(previous["current"]["portfolio_data_revision"]) + 1
        )
    else:
        assert latest.json()["current"]["account_status"] == "ARCHIVED"
    assert database_snapshot(engine) == before


async def test_nested_model_rejects_scope_lifecycle_and_no_grant_contradictions(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200
    value = response.json()
    model = DirectReceiptVoidInputs.model_validate(value)
    assert model.model_dump(mode="json") == value
    for secret in (created["id"], created["cash_entry_id"], created["portfolio_id"], "6.000000"):
        assert secret not in repr(model)
    for section, changes in (
        ("current", {"event_id": str(uuid4())}),
        ("current", {"account_id": str(uuid4())}),
        ("current", {"revision_id": str(uuid4())}),
        ("current", {"revision_no": 2}),
        ("current", {"account_version": 1}),
        ("current", {"rejection_state": "REJECTED"}),
        ("receipt", {"receipt_basis": "LEGACY_ENTITLEMENT", "entitlement_id": str(uuid4())}),
        ("receipt", {"audit_scope": "RECEIPT_AUDIT_NOT_VERIFIED"}),
        ("receipt", {"status": "VOID"}),
        ("receipt", {"entitlement_id": str(uuid4())}),
        ("receipt", {"payment_scope": "VERIFIED"}),
        ("receipt", {"eligibility_scope": "ESTABLISHED"}),
        ("receipt", {"current_use_scope": "GRANTED"}),
        ("receipt", {"history_completeness": "COMPLETE"}),
        ("receipt", {"received_hkd": "-46.8"}),
        ("receipt", {"recorded_amount": 6}),
    ):
        forged = deepcopy(value)
        forged[section].update(changes)
        with pytest.raises(ValidationError):
            DirectReceiptVoidInputs.model_validate(forged)
    for change in (
        {"cash_entry_id": None},
        {"origin_portfolio_id": None},
        {"can_void": True},
        {"scope": "RECORDED_RECEIPT_VOID_INPUTS_ONLY"},
    ):
        with pytest.raises(ValidationError):
            DirectReceiptVoidInputs.model_validate({**value, **change})
