"""Owner-scoped receipt void inputs are a snapshot, not a correction grant."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_audit as receipt_audit
import spm_api.dividend_receipt_void_input_routes as routes
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account as recorded_account
from spm_api.dividend_receipt_models import ReceiptVoidInputs
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt
from tests.test_dividend_receipt_history import claim
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_history_api import path as history_path
from tests.test_dividend_receipt_history_api import safe
from tests.test_dividend_receipt_void import void_intent
from tests.test_dividend_receipt_write_api import body as create_body
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any], receipt_id: str) -> str:
    return f"{history_path(book)}/{receipt_id}/void-inputs"


async def create(client: httpx.AsyncClient, book: dict[str, Any]) -> dict[str, Any]:
    payload = await create_body(client, book)
    response = await client.post(
        "/api/v1/dividends/receipts", json=payload, headers=version_headers(client, 2)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_matched_origin_exposes_current_scope_and_original_cash_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value == {
        "receipt_id": created["id"],
        "event_id": created["event_id"],
        "observed_revision_id": created["observed_revision_id"],
        "observed_revision_no": 2,
        "account_id": created["account_id"],
        "portfolio_id": created["portfolio_id"],
        "broker_id": created["broker_id"],
        "account_status": "ACTIVE",
        "account_version": created["account_version"],
        "portfolio_version": created["portfolio_version"],
        "broker_version": created["broker_version"],
        "portfolio_data_revision": str(int(created["portfolio_data_revision"]) + 1),
        "entitlement_id": created["entitlement_id"],
        "original_revision_id": created["original_revision_id"],
        "cash_entry_id": created["cash_entry_id"],
        "origin_portfolio_id": created["portfolio_id"],
        "received_date": created["received_date"],
        "received_amount": "6.000000",
        "currency": "USD",
        "tax": None,
        "fee": "0.000000",
        "fx_rate_hkd": "7.800000000000",
        "received_hkd": "46.800000",
        "status": "ACTIVE",
        "receipt_version": "1",
        "created_at": created["created_at"],
        "voided_at": None,
        "audit_scope": "RECORDED_AUDIT_MATCHED_ONLY",
        "scope": "RECORDED_RECEIPT_VOID_INPUTS_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
    }
    assert database_snapshot(engine) == before


async def test_legacy_missing_origin_is_explicitly_unverified_not_adopted(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        legacy = add_posted_receipt(con, book["older"], fx=None)
    before = database_snapshot(engine)
    response = await client.get(path(book, legacy["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value["audit_scope"] == "RECEIPT_AUDIT_NOT_VERIFIED"
    assert value["cash_entry_id"] is None and value["origin_portfolio_id"] is None
    assert value["received_hkd"] is None and value["fx_rate_hkd"] is None
    assert value["eligibility_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_voided_and_archived_history_stays_readable_without_reversal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    from spm_api.dividend_receipt_void import void

    created = await create(client, book)
    with unit_of_work(engine) as con:
        records = receipt_audit.history(
            con, book["owner"], book["event"].event_id, book["account_id"]
        )
    assert records is not None and len(records) == 1
    intent = void_intent(
        engine, (book["second"], str(book["account_id"]), book["older"]), records[0]
    )
    with unit_of_work(engine, write=True) as con:
        void(con, book["owner"], intent, request_id=uuid4())
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(book["account_id"]),),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value["status"] == "VOID" and value["receipt_version"] == "2"
    assert value["account_status"] == "ARCHIVED" and value["voided_at"] is not None
    assert value["cash_entry_id"] == created["cash_entry_id"]
    assert value["received_amount"] == created["received_amount"]
    assert value["received_hkd"] == created["received_hkd"]
    assert database_snapshot(engine) == before


async def test_foreign_missing_and_unauthenticated_scopes_are_not_disclosed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    before = database_snapshot(engine)
    for bad in (
        path(book, str(uuid4())),
        path({**book, "account_id": uuid4()}, created["id"]),
        f"/api/v1/dividends/events/{uuid4()}/accounts/{book['account_id']}/receipts/{created['id']}/void-inputs",
    ):
        response = await client.get(bad)
        assert response.status_code == 404
        safe(response)
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
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 404
    safe(response)
    client.cookies.clear()
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 401
    safe(response)
    assert database_snapshot(engine) == before_foreign


async def test_receipt_in_another_owned_account_is_not_disclosed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        other = claim(book["first"].revision_id, graph["target"]["id"], net=5_000_000)
        insert(con, "dividend_entitlements", other)
        receipt = add_posted_receipt(con, other, fx=None)
    before = database_snapshot(engine)
    response = await client.get(path(book, receipt["id"]))
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    safe(response)
    assert database_snapshot(engine) == before


async def test_history_failure_is_private_and_never_partially_returned(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    before = database_snapshot(engine)
    with patch.object(
        receipt_audit, "history", side_effect=ValueError("synthetic-private-diagnostic")
    ):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_account_change_during_read_does_not_mix_wal_snapshots(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    original = receipt_audit.history

    def archive() -> None:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
                (str(book["account_id"]),),
            )

    def read_then_archive(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(archive).result(timeout=10)
        return value

    with patch.object(receipt_audit, "history", side_effect=read_then_archive):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["account_status"] == "ACTIVE"
    assert value["account_version"] == created["account_version"]
    before_latest = database_snapshot(engine)
    latest = await client.get(path(book, created["id"]))
    assert latest.status_code == 200
    assert latest.json()["account_status"] == "ARCHIVED"
    assert int(latest.json()["account_version"]) == int(value["account_version"]) + 1
    assert database_snapshot(engine) == before_latest


async def test_receipt_void_during_read_keeps_cash_and_revision_in_one_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    from spm_api.dividend_receipt_void import void

    created = await create(client, book)
    with unit_of_work(engine) as con:
        records = receipt_audit.history(
            con, book["owner"], book["event"].event_id, book["account_id"]
        )
    assert records is not None and len(records) == 1
    intent = void_intent(
        engine, (book["second"], str(book["account_id"]), book["older"]), records[0]
    )
    original = receipt_audit.history
    started = False

    def write_void() -> None:
        with unit_of_work(engine, write=True) as con:
            void(con, book["owner"], intent, request_id=uuid4())

    def read_then_void(*args: Any, **kwargs: Any) -> Any:
        nonlocal started
        value = original(*args, **kwargs)
        if not started:
            started = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write_void).result(timeout=10)
        return value

    with patch.object(receipt_audit, "history", side_effect=read_then_void):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    earlier = response.json()
    assert earlier["status"] == "ACTIVE" and earlier["receipt_version"] == "1"
    assert earlier["portfolio_data_revision"] == str(intent.expected_data_revision)
    before_latest = database_snapshot(engine)
    later = await client.get(path(book, created["id"]))
    assert later.status_code == 200, later.text
    assert later.json()["status"] == "VOID" and later.json()["receipt_version"] == "2"
    assert later.json()["portfolio_data_revision"] == str(intent.expected_data_revision + 1)
    assert later.json()["cash_entry_id"] == earlier["cash_entry_id"]
    assert later.json()["received_hkd"] == earlier["received_hkd"]
    assert database_snapshot(engine) == before_latest


async def test_account_move_retains_original_portfolio_and_current_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(book["owner"]),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con,
            "brokers",
            {"id": str(new_broker), "owner_id": str(book["owner"]), "name": str(new_broker)},
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), str(book["account_id"])),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    safe(response)
    result = response.json()
    assert result["origin_portfolio_id"] == created["portfolio_id"]
    assert result["portfolio_id"] == str(new_portfolio)
    assert result["broker_id"] == str(new_broker)
    assert int(result["account_version"]) == int(created["account_version"]) + 1
    assert database_snapshot(engine) == before


async def test_exhausted_data_revision_is_exact_but_not_a_void_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?",
            (INT64_MAX, created["portfolio_id"]),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200, response.text
    assert response.json()["portfolio_data_revision"] == str(INT64_MAX)
    assert response.json()["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


async def test_void_input_model_rejects_inconsistent_origin_lifecycle_and_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    response = await client.get(path(book, created["id"]))
    assert response.status_code == 200
    value = response.json()
    for change in (
        {"cash_entry_id": None},
        {"origin_portfolio_id": None},
        {"cash_entry_id": None, "origin_portfolio_id": None},
        {"audit_scope": "RECEIPT_AUDIT_NOT_VERIFIED"},
        {"status": "ACTIVE", "voided_at": value["created_at"]},
        {"status": "VOID", "voided_at": None},
        {"status": "VOID", "voided_at": value["created_at"], "receipt_version": "1"},
        {"status": "VOID", "voided_at": value["created_at"], "receipt_version": "3"},
        {"status": "VOID", "voided_at": "2000-01-01T00:00:00Z", "receipt_version": "2"},
        {"receipt_version": "2"},
        {"receipt_version": "01"},
        {"account_version": "9223372036854775808"},
        {"portfolio_data_revision": "-1"},
        {"account_status": "UNKNOWN"},
        {"payment_scope": "VERIFIED"},
        {"eligibility_scope": "ESTABLISHED"},
    ):
        with pytest.raises(ValidationError):
            ReceiptVoidInputs.model_validate({**value, **change})
    assert ReceiptVoidInputs.model_validate(
        {**value, "portfolio_data_revision": str(INT64_MAX)}
    ).portfolio_data_revision == str(INT64_MAX)
    assert (
        ReceiptVoidInputs.model_validate(
            {
                **value,
                "cash_entry_id": None,
                "origin_portfolio_id": None,
                "audit_scope": "RECEIPT_AUDIT_NOT_VERIFIED",
            }
        ).audit_scope
        == "RECEIPT_AUDIT_NOT_VERIFIED"
    )


async def test_inconsistent_audit_or_account_scope_returns_private_503(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    with unit_of_work(engine) as con:
        records = receipt_audit.history(
            con, book["owner"], book["event"].event_id, book["account_id"]
        )
    assert records is not None and len(records) == 1
    before = database_snapshot(engine)
    for broken in (None, (records[0], records[0])):
        with patch.object(receipt_audit, "history", return_value=broken):
            response = await client.get(path(book, created["id"]))
        assert response.status_code == 503
        assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_INPUTS_UNAVAILABLE"
        safe(response)

    def wrong_portfolio(*args: Any, **kwargs: Any) -> dict[str, Any]:
        current = recorded_account(*args, **kwargs)
        assert current is not None
        return {**dict(current), "portfolio_id": str(uuid4())}

    with patch.object(routes, "account", side_effect=wrong_portfolio):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bad", [None, -1, INT64_MAX + 1, 1.0, True])
async def test_noncanonical_portfolio_revision_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], bad: object
) -> None:
    created = await create(client, book)
    before = database_snapshot(engine)
    execute = Connection.execute

    def corrupt(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "SELECT a.status,p.version,p.data_revision FROM accounts a" in str(statement):
            result = MagicMock()
            result.mappings.return_value.one_or_none.return_value = {
                "status": "ACTIVE",
                "version": 1,
                "data_revision": bad,
            }
            return result
        return execute(con, statement, *args, **kwargs)

    with patch.object(Connection, "execute", corrupt):
        response = await client.get(path(book, created["id"]))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
