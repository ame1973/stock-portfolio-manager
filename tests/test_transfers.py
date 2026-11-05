"""Atomic cash pairs, exact cost continuity, and stable transfer revision ordering."""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, text

from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.finance import Position
from spm_api.ledger_service import allocate_sequence, post_cash, validate_history
from spm_api.transfer_models import CashTransfer, SecurityTransfer
from tests.helpers import (
    create,
    initialize,
    version_headers,
    write_headers,
)
from tests.helpers import (
    database_snapshot as snapshot,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def graph(client: httpx.AsyncClient, engine: Engine) -> dict[str, Any]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic transfer portfolio"})
    broker = await create(client, "brokers", {"name": "Synthetic transfer broker"})
    accounts = [
        await create(
            client,
            "accounts",
            {
                "portfolio_id": portfolio["id"],
                "broker_id": broker["id"],
                "name": name,
                "default_currency": "USD",
            },
        )
        for name in ("Synthetic source", "Synthetic destination")
    ]
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "TRN",
            "name": "Synthetic transfer security",
            "quote_currency": "USD",
        },
    )
    return {
        "owner": owner,
        "portfolio": portfolio,
        "broker": broker,
        "source": accounts[0],
        "target": accounts[1],
        "security": security,
    }


def cash_body(graph: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "portfolio_id": graph["portfolio"]["id"],
        "source_account_id": graph["source"]["id"],
        "target_account_id": graph["target"]["id"],
        "transfer_date": "2026-01-03",
        "source_currency": "USD",
        "target_currency": "USD",
        "source_amount": "100",
        "target_amount": "100",
        "fx_rate_hkd": "7.8",
        "reason": "Synthetic reviewed transfer",
        **changes,
    }


def stock_body(graph: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "portfolio_id": graph["portfolio"]["id"],
        "source_account_id": graph["source"]["id"],
        "target_account_id": graph["target"]["id"],
        "transfer_date": "2026-01-03",
        "security_id": graph["security"]["id"],
        "quantity": "4",
        **changes,
    }


def trade_body(graph: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "portfolio_id": graph["portfolio"]["id"],
        "account_id": graph["source"]["id"],
        "security_id": graph["security"]["id"],
        "side": "BUY",
        "trade_date": "2026-01-02",
        "quantity": "10",
        "price": "100",
        "fee": "1",
        "currency": "USD",
        "fx_rate_hkd": "7.8",
        **changes,
    }


@pytest.fixture
async def holdings(client: httpx.AsyncClient, graph: dict[str, Any]) -> dict[str, Any]:
    buy = await create(client, "trades", trade_body(graph))
    return {**graph, "buy": buy}


def positions(engine: Engine, graph: dict[str, Any]) -> tuple[Position, Position]:
    with unit_of_work(engine) as con:
        current = validate_history(con, graph["portfolio"]["id"])
    return (
        current.get((graph["source"]["id"], graph["security"]["id"]), Position()),
        current.get((graph["target"]["id"], graph["security"]["id"]), Position()),
    )


async def test_cash_transfer_posts_both_native_legs_once_without_external_flow(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    body, key = cash_body(graph), str(uuid4())
    result = await create(client, "cash-transfers", body, key=key)
    assert result["source_amount"] == result["target_amount"] == "100.000000"
    assert result["source_booked_hkd"] == result["target_booked_hkd"] == "780.000000"
    assert result["booking_delta_hkd"] == "0.000000" and result["quality"] == "OK"
    before = snapshot(engine)
    assert await create(client, "cash-transfers", body, key=key) == result
    assert snapshot(engine) == before
    with unit_of_work(engine) as con:
        entries = list(
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries ORDER BY rowid").mappings()
        )
        assert len(entries) == 2
        assert [row["amount_micro"] for row in entries] == [-100000000, 100000000]
        assert sum(row["booked_hkd_micro"] for row in entries) == 0
        assert {row["source_type"] for row in entries} == {"CASH_TRANSFER_OUT", "CASH_TRANSFER_IN"}
        assert all(
            row["source_id"] == result["id"] and row["external_flow"] == 0 for row in entries
        )
        assert con.exec_driver_sql("SELECT count(*) FROM cash_transfers").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 0
    read = await client.get(f"/api/v1/cash-transfers/{result['id']}")
    assert read.status_code == 200 and read.json() == result and read.headers["ETag"] == '"1"'


@pytest.mark.parametrize(
    ("target", "actual", "rate", "quality", "delta"),
    [
        ("780", None, "7.8", "OK", "0.000000"),
        ("790", None, "7.8", "NEEDS_REVIEW", "10.000000"),
        ("790", "790", "7.8", "OK", "0.000000"),
        ("780", None, None, "MISSING_FX", None),
    ],
)
async def test_explicit_same_account_currency_conversion_retains_actuals_and_review_delta(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    target: str,
    actual: str | None,
    rate: str | None,
    quality: str,
    delta: str | None,
) -> None:
    result = await create(
        client,
        "cash-transfers",
        cash_body(
            graph,
            target_account_id=graph["source"]["id"],
            target_currency="HKD",
            target_amount=target,
            actual_hkd=actual,
            fx_rate_hkd=rate,
        ),
    )
    assert result["quality"] == quality and result["booking_delta_hkd"] == delta
    assert result["fx_rate_hkd"] == ("7.800000000000" if rate else None)
    with unit_of_work(engine) as con:
        entries = list(
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries ORDER BY rowid").mappings()
        )
        assert len(entries) == 2 and entries[0]["account_id"] == entries[1]["account_id"]
        assert entries[0]["currency"] == "USD" and entries[1]["currency"] == "HKD"
        assert entries[0]["fx_rate_hkd_e12"] == (7800000000000 if rate else None)
        assert entries[1]["fx_rate_hkd_e12"] == 1000000000000
        assert (entries[0]["booked_hkd_micro"] is None) == (rate is None)


async def test_cash_revision_and_void_preserve_root_original_null_fx_and_two_exact_reversals(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    original = await create(client, "cash-transfers", cash_body(graph, fx_rate_hkd=None))
    assert original["quality"] == "MISSING_FX"
    body = cash_body(graph, source_amount="120", target_amount="120", fx_rate_hkd="7.9")
    headers = version_headers(client, 1)
    response = await client.patch(
        f"/api/v1/cash-transfers/{original['id']}", json=body, headers=headers
    )
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["root_id"] == original["id"] and revised["supersedes_id"] == original["id"]
    assert revised["revision"] == 2 and revised["version"] == 1
    assert revised["source_booked_hkd"] == revised["target_booked_hkd"] == "948.000000"
    before = snapshot(engine)
    replay = await client.patch(
        f"/api/v1/cash-transfers/{original['id']}", json=body, headers=headers
    )
    assert replay.status_code == 200 and replay.json() == revised and snapshot(engine) == before
    old = (await client.get(f"/api/v1/cash-transfers/{original['id']}")).json()
    assert old == {
        **original,
        "status": "SUPERSEDED",
        "version": 2,
        "updated_at": old["updated_at"],
    }
    reason, headers = {"reason": "Void synthetic paired movement"}, version_headers(client, 1)
    void = await client.request(
        "DELETE", f"/api/v1/cash-transfers/{revised['id']}", json=reason, headers=headers
    )
    assert void.status_code == 200
    before = snapshot(engine)
    retry = await client.request(
        "DELETE", f"/api/v1/cash-transfers/{revised['id']}", json=reason, headers=headers
    )
    assert retry.status_code == 200 and retry.json() == void.json() and snapshot(engine) == before
    with unit_of_work(engine) as con:
        rows = list(con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings())
        assert len(rows) == 8
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            if row["reverses_entry_id"] is not None:
                primary = by_id[row["reverses_entry_id"]]
                assert row["amount_micro"] == -primary["amount_micro"]
                assert row["booked_hkd_micro"] == (
                    -primary["booked_hkd_micro"]
                    if primary["booked_hkd_micro"] is not None
                    else None
                )
                assert all(
                    row[field] == primary[field]
                    for field in (
                        "currency",
                        "fx_rate_hkd_e12",
                        "source_type",
                        "source_id",
                        "effective_date",
                        "external_flow",
                    )
                )
        for account in (graph["source"]["id"], graph["target"]["id"]):
            assert sum(row["amount_micro"] for row in rows if row["account_id"] == account) == 0


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"source_amount": 100}, "VALIDATION_ERROR"),
        ({"source_amount": "0"}, "VALIDATION_ERROR"),
        ({"target_amount": "-1"}, "VALIDATION_ERROR"),
        ({"source_amount": "0.0000001"}, "VALIDATION_ERROR"),
        ({"source_amount": "1e2"}, "VALIDATION_ERROR"),
        ({"target_amount": "99"}, "VALIDATION_ERROR"),
        ({"fx_rate_hkd": "0"}, "VALIDATION_ERROR"),
        ({"fx_rate_hkd": "7.8000000000001"}, "VALIDATION_ERROR"),
        ({"transfer_date": "2050-01-02"}, "VALIDATION_ERROR"),
        ({"transfer_date": "2026-02-30"}, "VALIDATION_ERROR"),
        ({"reason": "short"}, "VALIDATION_ERROR"),
        ({"actual_hkd": "0"}, "VALIDATION_ERROR"),
        ({"actual_hkd": "780", "fx_rate_hkd": None}, "MISSING_FX"),
        ({"source_currency": "HKD", "target_currency": "HKD"}, "CURRENCY_MISMATCH"),
        (
            {"source_amount": "2000000", "target_amount": "2000000", "fx_rate_hkd": "9000000"},
            "FINANCIAL_OVERFLOW",
        ),
        ({"external_flow": True}, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_cash_pair_changes_zero_rows(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    changes: dict[str, Any],
    code: str,
) -> None:
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/cash-transfers", json=cash_body(graph, **changes), headers=write_headers(client)
    )
    assert response.status_code == 422 and response.json()["code"] == code, response.text
    assert snapshot(engine) == before


async def test_second_cash_leg_failure_rolls_back_first_leg_fact_and_all_metadata(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spm_api import transfer_service

    actual = post_cash
    count = 0

    def fail_second(*args: Any, **kwargs: Any) -> str:
        nonlocal count
        count += 1
        if count == 2:
            raise ApiError(409, "INJECTED", "Synthetic second cash leg failure")
        return actual(*args, **kwargs)

    monkeypatch.setattr(transfer_service, "post_cash", fail_second)
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/cash-transfers", json=cash_body(graph), headers=write_headers(client)
    )
    assert response.status_code == 409 and response.json()["code"] == "INJECTED"
    assert count == 2 and snapshot(engine) == before


async def test_fin_002_security_transfer_preserves_total_cost_and_posts_no_cash(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any]
) -> None:
    with unit_of_work(engine) as con:
        cash_before = [
            tuple(row) for row in con.exec_driver_sql("SELECT * FROM cash_ledger_entries")
        ]
    body, key = stock_body(holdings), str(uuid4())
    transferred = await create(client, "security-transfers", body, key=key)
    assert transferred["cost_moved_hkd"] == "3123.120000"
    assert transferred["cost_scope"] == "BOOKED_AT_CREATION"
    source, target = positions(engine, holdings)
    assert source == Position(Decimal("6"), Decimal("4684.680000"))
    assert target == Position(Decimal("4"), Decimal("3123.120000"))
    before = snapshot(engine)
    assert await create(client, "security-transfers", body, key=key) == transferred
    assert snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert [
            tuple(row) for row in con.exec_driver_sql("SELECT * FROM cash_ledger_entries")
        ] == cash_before
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM ledger_sequences WHERE kind='TRANSFER'"
            ).scalar_one()
            == 1
        )
    read = await client.get(f"/api/v1/security-transfers/{transferred['id']}")
    assert read.status_code == 200 and read.json() == transferred and read.headers["ETag"] == '"1"'


async def test_security_revision_keeps_root_sequence_and_cannot_orphan_destination_sales(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any]
) -> None:
    original = await create(client, "security-transfers", stock_body(holdings))
    await create(
        client,
        "trades",
        trade_body(
            holdings,
            account_id=holdings["target"]["id"],
            side="SELL",
            trade_date="2026-01-04",
            quantity="2",
            price="120",
            fee="1",
        ),
    )
    body = stock_body(holdings, quantity="5", reason="Correct synthetic transfer quantity")
    headers = version_headers(client, 1)
    response = await client.patch(
        f"/api/v1/security-transfers/{original['id']}", json=body, headers=headers
    )
    assert response.status_code == 200, response.text
    revised = response.json()
    assert (
        revised["root_id"] == original["id"]
        and revised["created_sequence"] == original["created_sequence"]
    )
    assert revised["supersedes_id"] == original["id"] and revised["revision"] == 2
    assert revised["cost_moved_hkd"] == "3903.900000"
    old = (await client.get(f"/api/v1/security-transfers/{original['id']}")).json()
    assert old == {**original, "status": "SUPERSEDED", "is_current": False, "version": 2}
    source, target = positions(engine, holdings)
    assert source == Position(Decimal("5"), Decimal("3903.900000"))
    assert target == Position(Decimal("3"), Decimal("2342.340000"), Decimal("302.640000"))
    before = snapshot(engine)
    retry = await client.patch(
        f"/api/v1/security-transfers/{original['id']}", json=body, headers=headers
    )
    assert retry.status_code == 200 and retry.json() == revised and snapshot(engine) == before
    for method, data in (
        ("DELETE", {"reason": "Synthetic orphaning void"}),
        ("PATCH", stock_body(holdings, quantity="1", reason="Synthetic orphaning correction")),
    ):
        rejected = await client.request(
            method,
            f"/api/v1/security-transfers/{revised['id']}",
            json=data,
            headers=version_headers(client, 1),
        )
        assert rejected.status_code == 409 and rejected.json()["code"] == "INSUFFICIENT_SHARES"
        assert snapshot(engine) == before


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"quantity": "11"}, 409, "INSUFFICIENT_SHARES"),
        ({"quantity": "0"}, 422, "VALIDATION_ERROR"),
        ({"quantity": 4}, 422, "VALIDATION_ERROR"),
        ({"quantity": True}, 422, "VALIDATION_ERROR"),
        ({"quantity": "0.000000001"}, 422, "VALIDATION_ERROR"),
        ({"quantity": "4e0"}, 422, "VALIDATION_ERROR"),
        ({"quantity": "NaN"}, 422, "VALIDATION_ERROR"),
        ({"transfer_date": "2050-01-02"}, 422, "FUTURE_TRANSFER"),
        ({"transfer_date": "2026-02-30"}, 422, "VALIDATION_ERROR"),
        ({"cost_moved_hkd": "1"}, 422, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_security_transfer_is_zero_write_including_sequence(
    client: httpx.AsyncClient,
    engine: Engine,
    holdings: dict[str, Any],
    changes: dict[str, Any],
    status: int,
    code: str,
) -> None:
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/security-transfers",
        json=stock_body(holdings, **changes),
        headers=write_headers(client),
    )
    assert response.status_code == status and response.json()["code"] == code, response.text
    assert snapshot(engine) == before


async def test_transfer_cost_boundary_is_before_known_same_day_executions_not_end_of_day(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(
        client,
        "trades",
        trade_body(graph, trade_date="2026-01-03", price="10", fee="0", fx_rate_hkd="1"),
    )
    await create(
        client,
        "trades",
        trade_body(
            graph,
            trade_date="2026-01-03",
            price="100",
            fee="0",
            fx_rate_hkd="1",
            executed_at_utc="2026-01-03T15:00:00Z",
        ),
    )
    transfer = await create(client, "security-transfers", stock_body(graph, quantity="5"))
    assert transfer["cost_moved_hkd"] == "50.000000"
    assert positions(engine, graph) == (
        Position(Decimal("15"), Decimal("1050")),
        Position(Decimal("5"), Decimal("50")),
    )


async def test_transfer_does_not_invent_execution_time_to_use_later_same_day_buys(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(
        client,
        "trades",
        trade_body(graph, trade_date="2026-01-03", executed_at_utc="2026-01-03T15:00:00Z"),
    )
    before = snapshot(engine)
    rejected = await client.post(
        "/api/v1/security-transfers", json=stock_body(graph), headers=write_headers(client)
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before


async def test_recorded_transfer_cost_is_preserved_while_replay_restates_prior_trade_correction(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any]
) -> None:
    transferred = await create(client, "security-transfers", stock_body(holdings))
    changed = await client.patch(
        f"/api/v1/trades/{holdings['buy']['id']}",
        json=trade_body(holdings, fee="2", reason="Correct synthetic prior commission"),
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    read = (await client.get(f"/api/v1/security-transfers/{transferred['id']}")).json()
    assert read == transferred and read["cost_scope"] == "BOOKED_AT_CREATION"
    source, target = positions(engine, holdings)
    assert source == Position(Decimal("6"), Decimal("4689.360000"))
    assert target == Position(Decimal("4"), Decimal("3126.240000"))
    assert source.cost_hkd + target.cost_hkd == Decimal("7815.600000")


@pytest.mark.parametrize("path", ["cash-transfers", "security-transfers"])
async def test_transfer_idempotency_cas_current_state_and_void_control(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-transfers" else stock_body
    body, key = make(holdings), str(uuid4())
    original = await create(client, path, body, key=key)
    before = snapshot(engine)
    changed = (
        {**body, "source_amount": "101", "target_amount": "101"}
        if path == "cash-transfers"
        else {**body, "quantity": "5"}
    )
    mismatch = await client.post(
        f"/api/v1/{path}", json=changed, headers=write_headers(client, key=key)
    )
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await client.patch(
        f"/api/v1/{path}/{original['id']}",
        json={**body, "reason": "Synthetic stale transfer edit"},
        headers=version_headers(client, 2),
    )
    assert stale.status_code == 412 and snapshot(engine) == before
    for headers, data, expected in (
        (write_headers(client), {**body, "reason": "Synthetic missing version"}, 422),
        (
            {**version_headers(client, 1), "X-CSRF-Token": "invalid-synthetic-csrf"},
            {**body, "reason": "Synthetic invalid CSRF"},
            403,
        ),
    ):
        rejected = await client.patch(
            f"/api/v1/{path}/{original['id']}", json=data, headers=headers
        )
        assert rejected.status_code == expected and snapshot(engine) == before
    headers, reason = version_headers(client, 1), {"reason": "Void synthetic account movement"}
    void = await client.request(
        "DELETE", f"/api/v1/{path}/{original['id']}", json=reason, headers=headers
    )
    assert void.status_code == 200
    current = (await client.get(f"/api/v1/{path}/{original['id']}")).json()
    assert current["status"] == "VOID" and current["version"] == 2
    before = snapshot(engine)
    retry = await client.request(
        "DELETE", f"/api/v1/{path}/{original['id']}", json=reason, headers=headers
    )
    assert retry.status_code == 200 and retry.json() == void.json() and snapshot(engine) == before
    inactive = await client.request(
        "DELETE",
        f"/api/v1/{path}/{original['id']}",
        json=reason,
        headers=version_headers(client, 2),
    )
    assert inactive.status_code == 409 and inactive.json()["code"] == "INACTIVE_FACT"
    assert snapshot(engine) == before
    if path == "security-transfers":
        assert positions(engine, holdings) == (
            Position(Decimal("10"), Decimal("7807.8")),
            Position(),
        )


@pytest.mark.parametrize("path", ["cash-transfers", "security-transfers"])
async def test_transfer_rejects_cross_portfolio_and_same_wallet_without_any_side_effect(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-transfers" else stock_body
    portfolio = await create(client, "portfolios", {"name": "Separate synthetic portfolio"})
    other = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": holdings["broker"]["id"],
            "name": "Separate synthetic account",
            "default_currency": "USD",
        },
    )
    before = snapshot(engine)
    mismatch = await client.post(
        f"/api/v1/{path}",
        json=make(holdings, target_account_id=other["id"]),
        headers=write_headers(client),
    )
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "PORTFOLIO_MISMATCH"
    assert snapshot(engine) == before
    same = await client.post(
        f"/api/v1/{path}",
        json=make(holdings, target_account_id=holdings["source"]["id"]),
        headers=write_headers(client),
    )
    assert same.status_code == 422 and snapshot(engine) == before


@pytest.mark.parametrize("path", ["cash-transfers", "security-transfers"])
async def test_archive_blocks_new_or_revised_transfers_but_not_void(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-transfers" else stock_body
    original = await create(client, path, make(holdings))
    archived = await client.delete(
        f"/api/v1/accounts/{holdings['target']['id']}", headers=version_headers(client, 1)
    )
    assert archived.status_code == 200
    before = snapshot(engine)
    for method, url, data in (
        ("POST", f"/api/v1/{path}", make(holdings)),
        (
            "PATCH",
            f"/api/v1/{path}/{original['id']}",
            make(holdings, reason="Synthetic archived transfer edit"),
        ),
    ):
        rejected = await client.request(method, url, json=data, headers=version_headers(client, 1))
        assert rejected.status_code == 409 and rejected.json()["code"] == "ARCHIVED"
        assert snapshot(engine) == before
    void = await client.request(
        "DELETE",
        f"/api/v1/{path}/{original['id']}",
        json={"reason": "Void synthetic archived movement"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200


@pytest.mark.parametrize(
    ("path", "model"), [("cash-transfers", CashTransfer), ("security-transfers", SecurityTransfer)]
)
async def test_transfer_serialization_failure_rolls_back_facts_sequences_and_both_effects(
    client: httpx.AsyncClient,
    engine: Engine,
    holdings: dict[str, Any],
    path: str,
    model: type[CashTransfer] | type[SecurityTransfer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(self: CashTransfer | SecurityTransfer, **kwargs: Any) -> str:
        raise ValueError("Synthetic transfer response failure")

    monkeypatch.setattr(model, "model_dump_json", broken)
    before = snapshot(engine)
    body = cash_body(holdings) if path == "cash-transfers" else stock_body(holdings)
    with pytest.raises(ValueError, match="Synthetic transfer response failure"):
        await client.post(f"/api/v1/{path}", json=body, headers=write_headers(client))
    assert snapshot(engine) == before


async def test_transfer_cash_legs_are_source_managed_not_generic_manual_entries(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    transferred = await create(client, "cash-transfers", cash_body(graph))
    entries = (await client.get("/api/v1/cash-entries")).json()["items"]
    assert len(entries) == 2 and all(
        row["source_id"] == transferred["id"] and not row["editable"] for row in entries
    )
    before = snapshot(engine)
    for entry in entries:
        response = await client.request(
            "DELETE",
            f"/api/v1/cash-entries/{entry['id']}",
            json={"reason": "Synthetic disallowed one-leg void"},
            headers=version_headers(client, 1),
        )
        assert response.status_code == 409 and response.json()["code"] == "SOURCE_MANAGED"
        assert snapshot(engine) == before


async def test_full_security_closeout_transfer_keeps_each_accounts_realized_profit(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(holdings, side="SELL", quantity="2", price="120"))
    transferred = await create(client, "security-transfers", stock_body(holdings, quantity="8"))
    assert transferred["cost_moved_hkd"] == "6246.240000"
    assert positions(engine, holdings) == (
        Position(Decimal("0"), Decimal("0"), Decimal("302.64")),
        Position(Decimal("8"), Decimal("6246.24")),
    )


@pytest.mark.parametrize(
    ("action", "status", "num", "den", "quantity", "code"),
    [
        ("SPLIT", "CONFIRMED", 3, 2, "6", None),
        ("REVERSE_SPLIT", "CONFIRMED", 1, 3, "1", "NEEDS_REVIEW"),
        ("MERGER", "NEEDS_REVIEW", None, None, "1", "NEEDS_REVIEW"),
    ],
)
async def test_transfer_boundary_applies_confirmed_opening_split_and_rejects_unallocated_terms(
    client: httpx.AsyncClient,
    engine: Engine,
    holdings: dict[str, Any],
    action: str,
    status: str,
    num: int | None,
    den: int | None,
    quantity: str,
    code: str | None,
) -> None:
    with unit_of_work(engine, write=True) as con:
        identifier = str(uuid4())
        sequence = allocate_sequence(con, "ACTION", identifier)
        con.execute(
            text(
                "INSERT INTO corporate_actions(id,root_id,created_sequence,security_id,action_type,"
                "effective_date,ratio_num,ratio_den,status,created_at) "
                "VALUES (:id,:id,:seq,:security,:action,'2026-01-03',:num,:den,:status,:now)"
            ),
            {
                "id": identifier,
                "seq": sequence,
                "security": holdings["security"]["id"],
                "action": action,
                "num": num,
                "den": den,
                "status": status,
                "now": stamp(),
            },
        )
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/security-transfers",
        json=stock_body(holdings, quantity=quantity),
        headers=write_headers(client),
    )
    if code:
        assert response.status_code == 409 and response.json()["code"] == code, response.text
        assert snapshot(engine) == before
    else:
        assert response.status_code == 201 and response.json()["cost_moved_hkd"] == "3123.120000"
        source, target = positions(engine, holdings)
        assert source.quantity == Decimal("9") and target.quantity == Decimal("6")
        assert source.cost_hkd + target.cost_hkd == Decimal("7807.8")


async def test_security_transfer_future_date_uses_its_exchange_not_the_nas_date(
    client: httpx.AsyncClient,
    engine: Engine,
    holdings: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spm_api.transfer_service.now", lambda: datetime(2026, 11, 5, 1, tzinfo=UTC)
    )
    before = snapshot(engine)
    result = await client.post(
        "/api/v1/security-transfers",
        json=stock_body(holdings, transfer_date="2026-11-05"),
        headers=write_headers(client),
    )
    assert result.status_code == 422 and result.json()["code"] == "FUTURE_TRANSFER"
    assert snapshot(engine) == before


@pytest.mark.parametrize("path", ["cash-transfers", "security-transfers"])
async def test_transfer_owner_scope_covers_both_sides_reads_filters_and_corrections(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-transfers" else stock_body
    owner, portfolio, broker, source, target, identifier = (str(uuid4()) for _ in range(6))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners VALUES (?,'synthetic-hash',?,?,1)", (owner, stamp(), stamp())
        )
        con.exec_driver_sql(
            "INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (?,?,'Other',?)",
            (portfolio, owner, stamp()),
        )
        con.exec_driver_sql(
            "INSERT INTO brokers(id,owner_id,name) VALUES (?,?,'Other')", (broker, owner)
        )
        for account in (source, target):
            con.exec_driver_sql(
                "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
                "VALUES (?,?,?,?,'HKD',?)",
                (account, portfolio, broker, account, stamp()),
            )
        if path == "cash-transfers":
            con.exec_driver_sql(
                "INSERT INTO cash_transfers(id,root_id,portfolio_id,source_account_id,"
                "target_account_id,transfer_date,"
                "source_currency,target_currency,source_amount_micro,target_amount_micro,fx_rate_hkd_e12,"
                "source_booked_hkd_micro,target_booked_hkd_micro,reason,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'2026-01-03','HKD','HKD',1,1,1000000000000,1,1,"
                "'Synthetic foreign transfer',?,?)",
                (identifier, identifier, portfolio, source, target, stamp(), stamp()),
            )
        else:
            sequence = allocate_sequence(con, "TRANSFER", identifier)
            con.exec_driver_sql(
                "INSERT INTO security_transfers(id,root_id,created_sequence,portfolio_id,"
                "source_account_id,target_account_id,"
                "security_id,transfer_date,quantity_e8,transferred_cost_hkd_micro,created_at) "
                "VALUES (?,?,?,?,?,?,?,'2026-01-03',1,1,?)",
                (
                    identifier,
                    identifier,
                    sequence,
                    portfolio,
                    source,
                    target,
                    holdings["security"]["id"],
                    stamp(),
                ),
            )
    original = await create(client, path, make(holdings))
    before = snapshot(engine)
    for method, url, data in (
        ("GET", f"/api/v1/{path}/{identifier}", None),
        ("POST", f"/api/v1/{path}", make(holdings, source_account_id=source)),
        ("POST", f"/api/v1/{path}", make(holdings, target_account_id=target)),
        ("POST", f"/api/v1/{path}", make(holdings, portfolio_id=portfolio)),
        (
            "PATCH",
            f"/api/v1/{path}/{identifier}",
            make(holdings, reason="Synthetic foreign correction"),
        ),
        (
            "PATCH",
            f"/api/v1/{path}/{original['id']}",
            make(holdings, target_account_id=target, reason="Synthetic foreign destination"),
        ),
        ("DELETE", f"/api/v1/{path}/{identifier}", {"reason": "Synthetic foreign transfer void"}),
    ):
        response = await client.request(method, url, json=data, headers=version_headers(client, 1))
        assert response.status_code == 404, response.text
        assert snapshot(engine) == before
    for query in ({"portfolio_id": portfolio}, {"account_id": source}, {"root_id": identifier}):
        response = await client.get(f"/api/v1/{path}", params=query)
        assert response.status_code == 404 and snapshot(engine) == before
    assert all(
        row["id"] != identifier for row in (await client.get(f"/api/v1/{path}")).json()["items"]
    )


@pytest.mark.parametrize("path", ["cash-transfers", "security-transfers"])
async def test_transfer_corrected_to_another_owned_portfolio_invalidates_both_once(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-transfers" else stock_body
    original = await create(client, path, make(holdings))
    portfolio = await create(client, "portfolios", {"name": "Corrected transfer portfolio"})
    accounts = [
        await create(
            client,
            "accounts",
            {
                "portfolio_id": portfolio["id"],
                "broker_id": holdings["broker"]["id"],
                "name": name,
                "default_currency": "USD",
            },
        )
        for name in ("Corrected source", "Corrected destination")
    ]
    corrected_graph = {
        **holdings,
        "portfolio": portfolio,
        "source": accounts[0],
        "target": accounts[1],
    }
    if path == "security-transfers":
        await create(client, "trades", trade_body(corrected_graph))
    with unit_of_work(engine) as con:
        revisions = {
            row[0]: row[1] for row in con.exec_driver_sql("SELECT id,data_revision FROM portfolios")
        }
    response = await client.patch(
        f"/api/v1/{path}/{original['id']}",
        json=make(corrected_graph, reason="Correct synthetic transfer portfolio"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200, response.text
    assert response.json()["root_id"] == original["root_id"]
    if path == "security-transfers":
        assert response.json()["created_sequence"] == original["created_sequence"]
        assert positions(engine, holdings)[0].quantity == 10
        assert positions(engine, corrected_graph)[0].quantity == 6
    with unit_of_work(engine) as con:
        assert {
            row[0]: row[1] for row in con.exec_driver_sql("SELECT id,data_revision FROM portfolios")
        } == {key: value + 1 for key, value in revisions.items()}
        changed = (
            con.exec_driver_sql(
                "SELECT portfolio_id FROM projection_invalidations WHERE source_id=?",
                (response.json()["id"],),
            )
            .scalars()
            .all()
        )
        assert len(changed) == 2 and set(changed) == {holdings["portfolio"]["id"], portfolio["id"]}


@pytest.mark.parametrize("path", ["cash-transfers", "security-transfers"])
async def test_transfer_date_history_literal_filters_and_descending_cursor(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-transfers" else stock_body
    extra = {} if path == "cash-transfers" else {"quantity": "1"}
    records = [
        await create(client, path, make(holdings, transfer_date=day, note=note, **extra))
        for day, note in (
            ("2026-01-03", "100%_literal"),
            ("2026-01-04", "normal"),
            ("2026-01-04", "last"),
        )
    ]
    expected = sorted(records, key=lambda row: (row["transfer_date"], row["id"]), reverse=True)
    found, cursor = [], None
    for _ in records:
        query: dict[str, Any] = {
            "limit": 1,
            "account_id": holdings["target"]["id"],
            "portfolio_id": holdings["portfolio"]["id"],
        }
        if cursor:
            query["cursor"] = cursor
        response = await client.get(f"/api/v1/{path}", params=query)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        found.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
    assert found == expected and cursor is None
    literal = (await client.get(f"/api/v1/{path}", params={"q": "%_"})).json()["items"]
    assert [row["id"] for row in literal] == [records[0]["id"]]
    selector = (
        {"currency": "USD"}
        if path == "cash-transfers"
        else {"security_id": holdings["security"]["id"]}
    )
    filtered = (
        await client.get(
            f"/api/v1/{path}",
            params={"from_date": "2026-01-04", "to_date": "2026-01-04", **selector},
        )
    ).json()["items"]
    assert len(filtered) == 2
    revised = await client.patch(
        f"/api/v1/{path}/{records[0]['id']}",
        json=make(
            holdings, note="Corrected transfer", reason="Synthetic journal correction", **extra
        ),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    history = (
        await client.get(
            f"/api/v1/{path}", params={"root_id": records[0]["id"], "include_history": True}
        )
    ).json()["items"]
    assert {row["id"] for row in history} == {records[0]["id"], revised.json()["id"]}
    current = (await client.get(f"/api/v1/{path}", params={"root_id": records[0]["id"]})).json()[
        "items"
    ]
    assert [row["id"] for row in current] == [revised.json()["id"]]
    for query in (
        {"limit": 101},
        {"from_date": "2026-02-30"},
        {"from_date": "2026-02-01", "to_date": "2026-01-01"},
        {"cursor": "invalid"},
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps(["not-a-date", str(uuid4())]).encode()
            ).decode()
        },
    ):
        before = snapshot(engine)
        rejected = await client.get(f"/api/v1/{path}", params=query)
        assert rejected.status_code == 422 and snapshot(engine) == before


async def test_replacement_target_leg_failure_restores_old_active_cash_pair_and_control_state(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spm_api import transfer_service

    original = await create(client, "cash-transfers", cash_body(graph))
    real_post = post_cash
    count = 0

    def fail_target(*args: Any, **kwargs: Any) -> str:
        nonlocal count
        count += 1
        if count == 2:
            raise ApiError(409, "INJECTED", "Synthetic replacement target failure")
        return real_post(*args, **kwargs)

    monkeypatch.setattr(transfer_service, "post_cash", fail_target)
    before = snapshot(engine)
    rejected = await client.patch(
        f"/api/v1/cash-transfers/{original['id']}",
        json=cash_body(graph, source_amount="120", target_amount="120"),
        headers=version_headers(client, 1),
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "INJECTED"
    assert count == 2 and snapshot(engine) == before


async def test_same_hkd_transfer_uses_identity_and_cash_pair_cannot_survive_anchor_overflow(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    identity = await create(
        client,
        "cash-transfers",
        cash_body(graph, source_currency="HKD", target_currency="HKD", fx_rate_hkd=None),
    )
    assert identity["fx_rate_hkd"] == "1.000000000000" and identity["quality"] == "OK"
    accounts = [
        await create(
            client,
            "accounts",
            {
                "portfolio_id": graph["portfolio"]["id"],
                "broker_id": graph["broker"]["id"],
                "name": name,
                "default_currency": "USD",
                "opening_cash": "1000",
                "opening_cash_date": "2026-01-10",
                "opening_fx_rate_hkd": "7.7",
            },
        )
        for name in ("Overflow source", "Overflow destination")
    ]
    before = snapshot(engine)
    rejected = await client.post(
        "/api/v1/cash-transfers",
        json=cash_body(
            graph,
            source_account_id=accounts[0]["id"],
            target_account_id=accounts[1]["id"],
            source_amount="2000000000000",
            target_amount="2000000000000",
            fx_rate_hkd="0.000000000001",
        ),
        headers=write_headers(client),
    )
    assert rejected.status_code == 422 and rejected.json()["code"] == "FINANCIAL_OVERFLOW"
    assert snapshot(engine) == before


async def test_all_transfer_operations_require_current_session_and_no_store_errors(
    client: httpx.AsyncClient, engine: Engine, holdings: dict[str, Any]
) -> None:
    records = [
        (path, await create(client, path, make(holdings)))
        for path, make in (("cash-transfers", cash_body), ("security-transfers", stock_body))
    ]
    client.cookies.clear()
    before = snapshot(engine)
    for path, record in records:
        make = cash_body if path == "cash-transfers" else stock_body
        for method, url, data in (
            ("GET", f"/api/v1/{path}", None),
            ("GET", f"/api/v1/{path}/{record['id']}", None),
            ("POST", f"/api/v1/{path}", make(holdings)),
            (
                "PATCH",
                f"/api/v1/{path}/{record['id']}",
                make(holdings, reason="Synthetic unauthenticated correction"),
            ),
            (
                "DELETE",
                f"/api/v1/{path}/{record['id']}",
                {"reason": "Synthetic unauthenticated transfer void"},
            ),
        ):
            response = await client.request(
                method, url, json=data, headers=version_headers(client, 1)
            )
            assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
            assert snapshot(engine) == before


async def test_cash_pair_preserves_both_opening_assertions_at_their_own_original_fx(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    accounts = [
        await create(
            client,
            "accounts",
            {
                "portfolio_id": graph["portfolio"]["id"],
                "broker_id": graph["broker"]["id"],
                "name": name,
                "default_currency": "USD",
                "opening_cash": "1000",
                "opening_cash_date": "2026-01-10",
                "opening_fx_rate_hkd": fx,
                "cash_completeness": "COMPLETE",
            },
        )
        for name, fx in (("Opening source", "7.7"), ("Opening target", "7.9"))
    ]
    data = {**graph, "source": accounts[0], "target": accounts[1]}
    original = await create(
        client, "cash-transfers", cash_body(data, source_amount="10", target_amount="10")
    )
    revised = await client.patch(
        f"/api/v1/cash-transfers/{original['id']}",
        json=cash_body(data, source_amount="20", target_amount="20"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    void = await client.request(
        "DELETE",
        f"/api/v1/cash-transfers/{revised.json()['id']}",
        json={"reason": "Void synthetic pre-opening transfer"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    for account in accounts:
        balances = (await client.get(f"/api/v1/accounts/{account['id']}/cash-balances")).json()[
            "balances"
        ]
        assert balances[0]["balance"] == "1000.000000"
        with unit_of_work(engine) as con:
            rows = (
                con.exec_driver_sql(
                    "SELECT * FROM cash_ledger_entries "
                    "WHERE account_id=? AND source_type='OPENING_REBASE'",
                    (account["id"],),
                )
                .mappings()
                .all()
            )
            assert len(rows) == 3 and sum(row["amount_micro"] for row in rows) == 0
            assert all(
                row["fx_rate_hkd_e12"]
                == (7700000000000 if account["id"] == accounts[0]["id"] else 7900000000000)
                for row in rows
            )
