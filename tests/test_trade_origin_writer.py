"""Exercise trade-origin integration using verified temporary databases only."""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api import trade_audit, trade_service
from spm_api.audit import audit
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot, version_headers, write_headers
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def origin(engine: Engine, book: dict[str, Any], trade_id: str) -> trade_audit.TradeAuditOrigin:
    with unit_of_work(engine) as con:
        result = trade_audit.get_origin(con, UUID(book["owner"]["id"]), UUID(trade_id))
        assert result is not None
        return result


async def test_creation_retry_revision_and_void_preserve_audited_originals(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    body, key = payload(book, trade_date="2999-01-02", fx_rate_hkd=None), str(uuid4())
    draft = await create(client, "trades", body, key=key)
    first = origin(engine, book, draft["id"])
    assert first.capture_mode == "AT_CREATION" and first.original_status == "DRAFT_SCHEDULED"
    before = database_snapshot(engine)
    assert await create(client, "trades", body, key=key) == draft
    assert database_snapshot(engine) == before
    result = await client.patch(
        f"/api/v1/trades/{draft['id']}",
        json={**body, "quantity": "11", "reason": "Correct this synthetic scheduled trade"},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200
    revised = result.json()
    second = origin(engine, book, revised["id"])
    assert second.capture_mode == "AT_CREATION" and second.original_status == "DRAFT_SCHEDULED"
    void = await client.request(
        "DELETE",
        f"/api/v1/trades/{revised['id']}",
        json={"reason": "Void this synthetic scheduled trade"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    with unit_of_work(engine) as con:
        revisions = con.exec_driver_sql(
            "SELECT action,before_sha256 FROM audit_events WHERE entity_type='trade' "
            "AND entity_id=? AND action IN ('REVISE','VOID') ORDER BY rowid",
            (revised["id"],),
        ).all()
        assert [tuple(row) for row in revisions] == [
            ("REVISE", first.before_sha256),
            ("VOID", second.before_sha256),
        ]
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 0
    assert origin(engine, book, draft["id"]) == first
    assert origin(engine, book, revised["id"]) == second


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_existing_verified_trade_capture_and_retirement_share_one_transaction(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], method: str
) -> None:
    body, key = payload(book), str(uuid4())
    with patch.object(trade_service, "_remember_trade_origin"):
        trade = await create(client, "trades", body, key=key)
    before = database_snapshot(engine)
    assert await create(client, "trades", body, key=key) == trade
    assert database_snapshot(engine) == before
    change = (
        payload(book, quantity="11", reason="Correct this synthetic legacy trade")
        if method == "PATCH"
        else {"reason": "Void this synthetic legacy trade"}
    )
    headers = version_headers(client, 1)
    response = await client.request(
        method, f"/api/v1/trades/{trade['id']}", json=change, headers=headers
    )
    assert response.status_code == 200
    captured = origin(engine, book, trade["id"])
    assert captured.capture_mode == "BEFORE_RETIRE"
    successor = response.json()["id"] if method == "PATCH" else trade["id"]
    action = "REVISE" if method == "PATCH" else "VOID"
    with unit_of_work(engine) as con:
        entry = con.exec_driver_sql(
            "SELECT request_id,before_sha256 FROM audit_events WHERE entity_type='trade' "
            "AND entity_id=? AND action=?",
            (successor, action),
        ).one()
        assert entry.request_id == str(captured.capture_request_id)
        assert entry.before_sha256 == captured.before_sha256
        if method == "DELETE":
            cash = con.exec_driver_sql(
                "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
                "WHERE source_type='TRADE' AND source_id=? ORDER BY rowid",
                (trade["id"],),
            ).all()
            assert [tuple(row) for row in cash] == [
                (-1000000000, 7800000000000, -7800000000),
                (1000000000, 7800000000000, 7800000000),
            ]
        else:
            assert origin(engine, book, successor).capture_mode == "AT_CREATION"
    stable = database_snapshot(engine)
    replay = await client.request(
        method, f"/api/v1/trades/{trade['id']}", json=change, headers=headers
    )
    assert replay.status_code == 200 and replay.json() == response.json()
    assert database_snapshot(engine) == stable


async def test_create_capture_failure_rolls_back_every_financial_side_effect(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with patch.object(
        trade_audit,
        "remember_created",
        side_effect=trade_audit.TradeAuditError("TRADE_AUDIT_INCONSISTENT"),
    ):
        response = await client.post(
            "/api/v1/trades", json=payload(book), headers=write_headers(client)
        )
    assert response.status_code == 503 and response.json()["code"] == "TRADE_AUDIT_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_late_revision_failure_rolls_back_legacy_capture_and_cash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with patch.object(trade_service, "_remember_trade_origin"):
        trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)

    def fail_revision(*args: Any, **kwargs: Any) -> None:
        if args[4] == "REVISE":
            raise RuntimeError("Synthetic late revision audit failure")
        audit(*args, **kwargs)

    with (
        patch.object(trade_service, "audit", side_effect=fail_revision),
        pytest.raises(RuntimeError),
    ):
        await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=payload(book, quantity="11", reason="Correct this synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
    assert database_snapshot(engine) == before


async def test_concurrent_revisions_create_only_one_successor(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    responses = await asyncio.gather(
        *(
            client.patch(
                f"/api/v1/trades/{trade['id']}",
                json=payload(
                    book, quantity=quantity, reason="Correct this synthetic concurrent trade"
                ),
                headers=version_headers(client, 1),
            )
            for quantity in ("11", "12")
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 412]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trades").scalar_one() == 2
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 2
