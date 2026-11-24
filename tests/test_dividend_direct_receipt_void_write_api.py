"""Protected direct receipt reversal binds original cash and historical HTTP intent."""

import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_direct_receipt_void as voids
import spm_api.dividend_direct_receipt_void_write_routes as routes
from spm_api.db import unit_of_work
from spm_api.dividend_direct_receipt_void_write_models import DirectReceiptVoidWrite
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.test_dividend_direct_receipt_inputs_api import book as book
from tests.test_dividend_direct_receipt_void_inputs_api import create
from tests.test_dividend_direct_receipt_void_inputs_api import path as input_path
from tests.test_dividend_receipt_write_api import safe
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(identifier: str) -> str:
    return f"/api/v1/dividends/direct-receipts/{identifier}/void"


async def body(client: httpx.AsyncClient, book: dict[str, Any], identifier: str) -> dict[str, Any]:
    response = await client.get(input_path(book, identifier))
    assert response.status_code == 200, response.text
    observed = response.json()
    current, receipt = observed["current"], observed["receipt"]
    return {
        **{
            name: current[name]
            for name in (
                "event_id",
                "account_id",
                "portfolio_id",
                "broker_id",
                "account_version",
                "portfolio_version",
                "broker_version",
            )
        },
        "observed_revision_id": current["revision_id"],
        "observed_revision_no": current["revision_no"],
        "original_revision_id": receipt["revision_id"],
        "original_revision_no": receipt["revision_no"],
        "expected_data_revision": current["portfolio_data_revision"],
        "cash_entry_id": observed["cash_entry_id"],
        "reason": "Synthetic private direct cash correction reason",
        "confirmation": "VOID_RECORDED_CASH",
    }


async def test_direct_void_posts_one_original_cash_reversal_and_replays_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    before = database_snapshot(engine)
    response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value["id"] == created["id"]
    assert value["receipt_basis"] == "DIRECT_EVENT_ACCOUNT"
    assert value["original_revision_id"] == created["revision_id"]
    assert value["cash_entry_id"] == created["cash_entry_id"]
    assert value["origin_portfolio_id"] == created["portfolio_id"]
    assert value["scope"] == "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY"
    assert value["status"] == "VOID" and value["version"] == "2"
    assert value["eligibility_scope"] == "NOT_ESTABLISHED"
    assert value["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert "entitlement_id" not in value
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT id,amount_micro,booked_hkd_micro,fx_rate_hkd_e12,entry_role "
            "FROM cash_ledger_entries WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=? "
            "ORDER BY entry_role",
            (created["id"],),
        ).all()
    assert [tuple(row) for row in rows] == [
        (created["cash_entry_id"], 6_000_000, 46_800_000, 7_800_000_000_000, "PRIMARY"),
        (value["reversal_entry_id"], -6_000_000, -46_800_000, 7_800_000_000_000, "REVERSAL"),
    ]
    after = database_snapshot(engine)
    allowed = {
        "audit_events",
        "cash_ledger_entries",
        "dividend_direct_receipts",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: rows for key, rows in before.items() if key not in allowed} == {
        key: rows for key, rows in after.items() if key not in allowed
    }
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    assert len(after["cash_ledger_entries"]) == len(before["cash_ledger_entries"]) + 1
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    replay = await client.post(path(created["id"]), json=payload, headers=headers)
    assert replay.status_code == 200 and replay.json() == value
    safe(replay)
    assert database_snapshot(engine) == after


async def move_account(client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]) -> str:
    response = await client.post(
        "/api/v1/portfolios",
        json={"name": "Synthetic moved direct cash portfolio"},
        headers=write_headers(client),
    )
    assert response.status_code == 201
    destination = str(response.json()["id"])
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,version=version+1 WHERE id=?",
            (destination, str(book["account_id"])),
        )
    return destination


async def test_retained_portfolio_change_after_internal_void_rolls_back_http_operation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    await move_account(client, engine, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    original = voids.void

    def late(con: Connection, *args: Any, **kwargs: Any) -> Any:
        result = original(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
            (created["portfolio_id"],),
        )
        return result

    before = database_snapshot(engine)
    with patch.object(voids, "void", side_effect=late):
        response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == 409, response.text
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(path(created["id"]), json=payload, headers=headers)
    assert retry.status_code == 200, retry.text


async def test_internal_void_cannot_be_adopted_even_with_a_colliding_request_uuid(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    intent = routes._intent(UUID(created["id"]), DirectReceiptVoidWrite.model_validate(payload), 1)
    request_id = uuid4()
    with unit_of_work(engine, write=True) as con:
        voids.void(con, book["owner"], intent, request_id=request_id)
    before = database_snapshot(engine)
    with patch("spm_api.main.uuid4", return_value=request_id):
        response = await client.post(
            path(created["id"]), json=payload, headers=version_headers(client, 1)
        )
    assert response.status_code == 409
    assert response.json()["code"] == "DIRECT_RECEIPT_VOID_CONFLICT"
    safe(response)
    assert database_snapshot(engine) == before


async def test_concurrent_void_replays_once_and_new_intent_or_key_cannot_reverse_again(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    left, right = await asyncio.gather(
        client.post(path(created["id"]), json=payload, headers=headers),
        client.post(path(created["id"]), json=payload, headers=headers),
    )
    assert left.status_code == right.status_code == 200
    assert left.json() == right.json()
    before = database_snapshot(engine)
    for intent, request_headers in (
        ({**payload, "reason": "A different synthetic correction reason"}, headers),
        (payload, version_headers(client, 1)),
    ):
        response = await client.post(path(created["id"]), json=intent, headers=request_headers)
        assert response.status_code == 409
        safe(response)
    assert database_snapshot(engine) == before


async def test_historical_replay_after_account_move_archive_and_clock_rollback_is_read_only(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    original = await client.post(path(created["id"]), json=payload, headers=headers)
    assert original.status_code == 200
    await move_account(client, engine, book)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED' WHERE id=?", (str(book["account_id"]),)
        )

    class EarlierDate(date):
        @classmethod
        def today(cls) -> "EarlierDate":
            return cls(2025, 1, 1)

    before = database_snapshot(engine)
    with (
        patch("spm_api.cash_models.date", EarlierDate),
        patch.object(
            voids, "void", side_effect=AssertionError("Replay must not invoke the writer")
        ),
    ):
        replay = await client.post(path(created["id"]), json=payload, headers=headers)
    assert replay.status_code == 200 and replay.json() == original.json()
    safe(replay)
    assert database_snapshot(engine) == before


async def test_consistently_substituted_writer_cash_cannot_be_returned_as_retained(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    original = voids.void

    def forged(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        return replace(
            result,
            receipt=replace(result.receipt, received=Decimal("7"), received_hkd=Decimal("54.6")),
        )

    before = database_snapshot(engine)
    with patch.object(voids, "void", side_effect=forged):
        response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before
