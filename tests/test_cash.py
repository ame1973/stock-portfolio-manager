"""Actual HTTP/SQLite cash/expense UoWs, native wallets, and immutable correction history."""

import base64
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.audit import stamp
from spm_api.cash_models import CashEntry, Expense
from spm_api.db import unit_of_work
from spm_api.ledger_service import reverse_cash
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
async def wallet(client: httpx.AsyncClient, engine: Engine) -> dict[str, Any]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic cash portfolio"})
    broker = await create(client, "brokers", {"name": "Synthetic cash broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic cash account",
            "default_currency": "USD",
        },
    )
    return {"owner": owner, "portfolio": portfolio, "broker": broker, "account": account}


def cash_body(wallet: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "account_id": wallet["account"]["id"],
        "effective_date": "2026-01-02",
        "kind": "DEPOSIT",
        "currency": "USD",
        "amount": "100",
        "fx_rate_hkd": "7.8",
        **changes,
    }


def expense_body(wallet: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "account_id": wallet["account"]["id"],
        "date": "2026-01-03",
        "category": "Custody fee",
        "currency": "USD",
        "amount": "2.000001",
        "fx_rate_hkd": "7.8",
        **changes,
    }


async def balance(client: httpx.AsyncClient, account: str, **query: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/accounts/{account}/cash-balances", params=query)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return dict(response.json())


async def test_exact_cash_is_posted_once_and_incomplete_balance_is_not_a_claimed_zero(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    initial = await balance(client, wallet["account"]["id"])
    assert initial["balances"][0]["recorded_balance"] == "0.000000"
    assert initial["balances"][0]["balance"] is None
    assert initial["balances"][0]["balance_quality"] == "UNKNOWN"
    body, key = cash_body(wallet, amount="123.450001", fx_rate_hkd="7.812345678901"), str(uuid4())
    created = await create(client, "cash-entries", body, key=key)
    assert created["amount"] == "123.450001" and created["booked_hkd"] == "964.434082"
    assert created["external_flow"] is True and created["status"] == "ACTIVE"
    assert created["editable"] is True and created["quality"] == "OK"
    before = snapshot(engine)
    assert await create(client, "cash-entries", body, key=key) == created
    assert snapshot(engine) == before
    read = await client.get(f"/api/v1/cash-entries/{created['id']}")
    assert read.status_code == 200 and read.json() == created and read.headers["ETag"] == '"1"'
    await create(
        client,
        "cash-entries",
        cash_body(wallet, kind="WITHDRAW", amount="-23.450001", fx_rate_hkd="7.9"),
    )
    current = (await balance(client, wallet["account"]["id"]))["balances"][0]
    assert current["recorded_balance"] == "100.000000" and current["balance"] is None
    assert current["historical_booked_hkd_net"] == "779.179074"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 2
        assert con.exec_driver_sql("SELECT count(*) FROM expenses").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations WHERE reason='CASH_FACT_CHANGED'"
            ).scalar_one()
            == 2
        )


async def test_native_currency_wallets_do_not_sum_unlike_currencies_or_invent_live_fx(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    await create(client, "cash-entries", cash_body(wallet, amount="10", fx_rate_hkd=None))
    await create(
        client, "cash-entries", cash_body(wallet, amount="20", currency="HKD", fx_rate_hkd=None)
    )
    unknown = (await balance(client, wallet["account"]["id"]))["balances"]
    assert {row["currency"]: row["recorded_balance"] for row in unknown} == {
        "HKD": "20.000000",
        "USD": "10.000000",
    }
    assert all(row["balance"] is None for row in unknown)
    asserted = await client.patch(
        f"/api/v1/accounts/{wallet['account']['id']}",
        json={"cash_completeness": "COMPLETE"},
        headers=version_headers(client, 1),
    )
    assert asserted.status_code == 200
    balances = {
        row["currency"]: row for row in (await balance(client, wallet["account"]["id"]))["balances"]
    }
    assert balances["USD"]["balance"] == "10.000000"
    assert balances["USD"]["historical_booked_hkd_net"] is None
    assert balances["USD"]["historical_booking_quality"] == "MISSING_FX"
    assert balances["USD"]["unvalued_entries"] == 1
    assert balances["HKD"]["balance"] == balances["HKD"]["historical_booked_hkd_net"] == "20.000000"
    assert balances["HKD"]["historical_booking_quality"] == "OK"
    assert "total_hkd" not in await balance(client, wallet["account"]["id"])


async def test_cash_correction_chain_can_move_wallets_and_preserves_every_original(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    original = await create(client, "cash-entries", cash_body(wallet))
    target = await create(
        client,
        "accounts",
        {
            "portfolio_id": wallet["portfolio"]["id"],
            "broker_id": wallet["broker"]["id"],
            "name": "Synthetic correction destination",
            "default_currency": "HKD",
        },
    )
    body = cash_body(
        wallet,
        account_id=target["id"],
        amount="120",
        currency="HKD",
        fx_rate_hkd=None,
        effective_date="2026-01-03",
        reason="Correct synthetic currency and account",
    )
    headers = version_headers(client, 1)
    response = await client.patch(
        f"/api/v1/cash-entries/{original['id']}", json=body, headers=headers
    )
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["supersedes_id"] == original["id"] and revised["source_id"] == original["id"]
    assert revised["source_type"] == "CASH_CORRECTION" and revised["version"] == 1
    before = snapshot(engine)
    retry = await client.patch(f"/api/v1/cash-entries/{original['id']}", json=body, headers=headers)
    assert retry.status_code == 200 and retry.json() == revised and snapshot(engine) == before
    old = (await client.get(f"/api/v1/cash-entries/{original['id']}")).json()
    assert old["status"] == "SUPERSEDED" and old["superseded_by_id"] == revised["id"]
    assert old["amount"] == "100.000000" and old["booked_hkd"] == "780.000000"
    assert old["fx_rate_hkd"] == "7.800000000000" and old["version"] == 2
    second = await client.patch(
        f"/api/v1/cash-entries/{revised['id']}",
        json={**body, "amount": "130"},
        headers=version_headers(client, 1),
    )
    assert second.status_code == 200, second.text
    latest = second.json()
    assert latest["supersedes_id"] == revised["id"]
    assert (await balance(client, wallet["account"]["id"]))["balances"][0][
        "recorded_balance"
    ] == "0.000000"
    assert (await balance(client, target["id"]))["balances"][0]["recorded_balance"] == "130.000000"
    void_headers = version_headers(client, 1)
    reason = {"reason": "Void synthetic mistaken correction"}
    void = await client.request(
        "DELETE", f"/api/v1/cash-entries/{latest['id']}", json=reason, headers=void_headers
    )
    assert void.status_code == 200
    before = snapshot(engine)
    replay = await client.request(
        "DELETE", f"/api/v1/cash-entries/{latest['id']}", json=reason, headers=void_headers
    )
    assert replay.status_code == 200 and replay.json() == void.json() and snapshot(engine) == before
    assert (await balance(client, target["id"]))["balances"][0]["recorded_balance"] == "0.000000"
    with unit_of_work(engine) as con:
        rows = list(con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings())
        assert len(rows) == 6
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            if row["reverses_entry_id"] is not None:
                previous = by_id[row["reverses_entry_id"]]
                assert row["amount_micro"] == -previous["amount_micro"]
                assert row["booked_hkd_micro"] == -previous["booked_hkd_micro"]
                assert (
                    row["currency"] == previous["currency"]
                    and row["fx_rate_hkd_e12"] == previous["fx_rate_hkd_e12"]
                )


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_unknown_fx_can_be_corrected_or_voided_without_poisoning_zero_cash(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    body = make(wallet, amount="10", fx_rate_hkd=None)
    original = await create(client, path, body)
    assert original["quality"] == "MISSING_FX"
    before = (await balance(client, wallet["account"]["id"]))["balances"][0]
    assert before["unvalued_entries"] == 1 and before["historical_booked_hkd_net"] is None
    revised = await client.patch(
        f"/api/v1/{path}/{original['id']}",
        json={**body, "fx_rate_hkd": "7.9", "reason": "Adopt synthetic historical FX evidence"},
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["quality"] == "OK"
    assert (await client.get(f"/api/v1/{path}/{original['id']}")).json()["fx_rate_hkd"] is None
    current = (await balance(client, wallet["account"]["id"]))["balances"][0]
    assert current["unvalued_entries"] == 0
    assert current["historical_booked_hkd_net"] == (
        "79.000000" if path == "cash-entries" else "-79.000000"
    )
    void = await client.request(
        "DELETE",
        f"/api/v1/{path}/{revised.json()['id']}",
        json={"reason": "Void synthetic corrected entry"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    final = (await balance(client, wallet["account"]["id"]))["balances"][0]
    assert (
        final["recorded_balance"] == "0.000000" and final["historical_booked_hkd_net"] == "0.000000"
    )
    assert final["unvalued_entries"] == 0 and final["historical_booking_quality"] == "OK"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"amount": 1}, "VALIDATION_ERROR"),
        ({"amount": 0.1}, "VALIDATION_ERROR"),
        ({"amount": True}, "VALIDATION_ERROR"),
        ({"amount": "0"}, "VALIDATION_ERROR"),
        ({"amount": "1e1"}, "VALIDATION_ERROR"),
        ({"amount": "NaN"}, "VALIDATION_ERROR"),
        ({"amount": "1.0000001"}, "VALIDATION_ERROR"),
        ({"amount": "9223372036854.775808"}, "VALIDATION_ERROR"),
        ({"amount": "-100"}, "VALIDATION_ERROR"),
        ({"kind": "WITHDRAW", "amount": "100"}, "VALIDATION_ERROR"),
        ({"kind": "TRANSFER_IN"}, "VALIDATION_ERROR"),
        ({"kind": "TRANSFER_OUT"}, "VALIDATION_ERROR"),
        ({"kind": "ADJUSTMENT"}, "VALIDATION_ERROR"),
        ({"external_flow": False}, "VALIDATION_ERROR"),
        ({"external_flow": "true"}, "VALIDATION_ERROR"),
        (
            {"kind": "ADJUSTMENT", "reason": "Synthetic correction", "external_flow": True},
            "VALIDATION_ERROR",
        ),
        ({"fx_rate_hkd": "0"}, "VALIDATION_ERROR"),
        ({"fx_rate_hkd": "7.8000000000001"}, "VALIDATION_ERROR"),
        ({"effective_date": "2050-01-02"}, "VALIDATION_ERROR"),
        ({"effective_date": "2026-02-30"}, "VALIDATION_ERROR"),
        ({"actual_hkd": "780"}, "VALIDATION_ERROR"),
        (
            {"actual_hkd": "780", "reason": "Synthetic actual booking", "fx_rate_hkd": None},
            "MISSING_FX",
        ),
        ({"actual_hkd": "-780", "reason": "Synthetic opposite sign"}, "CURRENCY_MISMATCH"),
        ({"currency": "HKD"}, "CURRENCY_MISMATCH"),
        ({"amount": "2000000", "fx_rate_hkd": "9000000"}, "FINANCIAL_OVERFLOW"),
        (
            {"kind": "WITHDRAW", "amount": "-9223372036854.775808", "fx_rate_hkd": None},
            "FINANCIAL_OVERFLOW",
        ),
        ({"source_type": "TRADE"}, "VALIDATION_ERROR"),
    ],
)
async def test_invalid_cash_never_changes_any_table_or_control_value(
    client: httpx.AsyncClient,
    engine: Engine,
    wallet: dict[str, Any],
    changes: dict[str, Any],
    code: str,
) -> None:
    before = snapshot(engine)
    result = await client.post(
        "/api/v1/cash-entries", json=cash_body(wallet, **changes), headers=write_headers(client)
    )
    assert result.status_code == 422 and result.json()["code"] == code, result.text
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"amount": 1}, "VALIDATION_ERROR"),
        ({"amount": "0"}, "VALIDATION_ERROR"),
        ({"amount": "0.0000001"}, "VALIDATION_ERROR"),
        ({"amount": "Infinity"}, "VALIDATION_ERROR"),
        ({"amount": "-1"}, "VALIDATION_ERROR"),
        (
            {"amount": "-9223372036854.775808", "reason": "Synthetic non-reversible refund"},
            "FINANCIAL_OVERFLOW",
        ),
        ({"date": "2050-01-02"}, "VALIDATION_ERROR"),
        ({"date": "2026-02-30"}, "VALIDATION_ERROR"),
        ({"category": ""}, "VALIDATION_ERROR"),
        ({"category": "a" * 101}, "VALIDATION_ERROR"),
        ({"actual_hkd": "15.6"}, "VALIDATION_ERROR"),
        ({"actual_hkd": "-15.6", "reason": "Synthetic invalid actual"}, "CURRENCY_MISMATCH"),
        (
            {"actual_hkd": "15.6", "reason": "Synthetic actual missing FX", "fx_rate_hkd": None},
            "MISSING_FX",
        ),
        ({"amount": "2000000", "fx_rate_hkd": "9000000"}, "FINANCIAL_OVERFLOW"),
    ],
)
async def test_invalid_expense_never_leaves_partial_cash_or_audit(
    client: httpx.AsyncClient,
    engine: Engine,
    wallet: dict[str, Any],
    changes: dict[str, Any],
    code: str,
) -> None:
    before = snapshot(engine)
    result = await client.post(
        "/api/v1/expenses", json=expense_body(wallet, **changes), headers=write_headers(client)
    )
    assert result.status_code == 422 and result.json()["code"] == code, result.text
    assert snapshot(engine) == before


async def test_expense_rounding_refund_revision_and_void_each_post_exact_cash_once(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    body, key = expense_body(wallet), str(uuid4())
    charge = await create(client, "expenses", body, key=key)
    assert charge["hkd_amount"] == "15.600008" and charge["quality"] == "OK"
    before = snapshot(engine)
    assert await create(client, "expenses", body, key=key) == charge and snapshot(engine) == before
    revised = await client.patch(
        f"/api/v1/expenses/{charge['id']}",
        json={
            **body,
            "amount": "3",
            "fx_rate_hkd": "7.9",
            "reason": "Correct synthetic operating charge",
        },
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200, revised.text
    replacement = revised.json()
    assert replacement["supersedes_id"] == charge["id"] and replacement["hkd_amount"] == "23.700000"
    old = (await client.get(f"/api/v1/expenses/{charge['id']}")).json()
    assert old == {**charge, "status": "SUPERSEDED", "version": 2}
    refund = await create(
        client,
        "expenses",
        expense_body(wallet, amount="-1.000001", reason="Synthetic broker refund evidence"),
    )
    assert refund["hkd_amount"] == "-7.800008"
    wallet_balance = (await balance(client, wallet["account"]["id"]))["balances"][0]
    assert wallet_balance["recorded_balance"] == "-1.999999"
    assert wallet_balance["historical_booked_hkd_net"] == "-15.899992"
    for record in (replacement, refund):
        deleted = await client.request(
            "DELETE",
            f"/api/v1/expenses/{record['id']}",
            json={"reason": "Void synthetic operating movement"},
            headers=version_headers(client, 1),
        )
        assert deleted.status_code == 200, deleted.text
    assert (await balance(client, wallet["account"]["id"]))["balances"][0][
        "recorded_balance"
    ] == "0.000000"
    with unit_of_work(engine) as con:
        rows = list(con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings())
        assert len(rows) == 6 and all(row["external_flow"] == 0 for row in rows)
        assert (
            sum(row["amount_micro"] for row in rows)
            == sum(row["booked_hkd_micro"] for row in rows)
            == 0
        )
        assert con.exec_driver_sql("SELECT count(*) FROM expenses").scalar_one() == 3


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_cash_expense_idempotency_stale_version_and_inactive_controls(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    body, key = make(wallet), str(uuid4())
    original = await create(client, path, body, key=key)
    before = snapshot(engine)
    mismatch = await client.post(
        f"/api/v1/{path}", json={**body, "amount": "101"}, headers=write_headers(client, key=key)
    )
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await client.patch(
        f"/api/v1/{path}/{original['id']}",
        json={**body, "reason": "Synthetic stale edit"},
        headers=version_headers(client, 2),
    )
    assert stale.status_code == 412 and snapshot(engine) == before
    reason, headers = {"reason": "Void synthetic financial movement"}, version_headers(client, 1)
    void = await client.request(
        "DELETE", f"/api/v1/{path}/{original['id']}", json=reason, headers=headers
    )
    assert void.status_code == 200
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


@pytest.fixture
async def anchored_wallet(client: httpx.AsyncClient, wallet: dict[str, Any]) -> dict[str, Any]:
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": wallet["portfolio"]["id"],
            "broker_id": wallet["broker"]["id"],
            "name": "Synthetic cash anchor",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-10",
            "opening_fx_rate_hkd": "7.7",
            "cash_completeness": "COMPLETE",
        },
    )
    return {**wallet, "account": account}


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_pre_anchor_cash_and_expense_round_trips_preserve_original_assertion_and_fx(
    client: httpx.AsyncClient, engine: Engine, anchored_wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    body = make(anchored_wallet, amount="2")
    original = await create(client, path, body)
    earlier = (await balance(client, anchored_wallet["account"]["id"], as_of="2026-01-05"))[
        "balances"
    ][0]
    assert earlier["balance"] is None and earlier["balance_quality"] == "BEFORE_OPENING"
    assert (await balance(client, anchored_wallet["account"]["id"]))["balances"][0][
        "balance"
    ] == "1000.000000"
    revised = await client.patch(
        f"/api/v1/{path}/{original['id']}",
        json={**body, "amount": "3", "reason": "Correct synthetic pre-anchor amount"},
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200, revised.text
    assert (await balance(client, anchored_wallet["account"]["id"]))["balances"][0][
        "balance"
    ] == "1000.000000"
    void = await client.request(
        "DELETE",
        f"/api/v1/{path}/{revised.json()['id']}",
        json={"reason": "Void synthetic pre-anchor movement"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    after = (await balance(client, anchored_wallet["account"]["id"]))["balances"][0]
    assert after["balance"] == "1000.000000" and after["historical_booked_hkd_net"] == "7700.000000"
    with unit_of_work(engine) as con:
        opening = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE source_type='OPENING_BALANCE'"
            )
            .mappings()
            .one()
        )
        assert opening["amount_micro"] == 1000000000 and opening["version"] == 1
        rebases = list(
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE source_type='OPENING_REBASE' "
                "ORDER BY rowid"
            ).mappings()
        )
        sign = -1 if path == "cash-entries" else 1
        assert [row["amount_micro"] for row in rebases] == [
            sign * 2000000,
            sign * 1000000,
            sign * -3000000,
        ]
        assert all(
            row["fx_rate_hkd_e12"] == 7700000000000 and row["external_flow"] == 0 for row in rebases
        )
        assert sum(row["booked_hkd_micro"] for row in rebases) == 0


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_anchor_overflow_rolls_back_every_fact_cash_audit_and_invalidation(
    client: httpx.AsyncClient, engine: Engine, anchored_wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    before = snapshot(engine)
    body = make(anchored_wallet, amount="2000000000000", fx_rate_hkd="0.000000000001")
    rejected = await client.post(f"/api/v1/{path}", json=body, headers=write_headers(client))
    assert rejected.status_code == 422 and rejected.json()["code"] == "FINANCIAL_OVERFLOW"
    assert snapshot(engine) == before


async def test_reversed_opening_is_review_not_a_complete_claim_or_implicit_repair(
    client: httpx.AsyncClient, engine: Engine, anchored_wallet: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        original = con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings().one()
        reverse_cash(con, original, reason="Synthetic unresolved opening")
    await create(client, "cash-entries", cash_body(anchored_wallet, effective_date="2026-01-11"))
    current = (await balance(client, anchored_wallet["account"]["id"]))["balances"][0]
    assert current["recorded_balance"] == "100.000000" and current["balance"] is None
    assert current["balance_quality"] == "NEEDS_REVIEW"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE source_type='OPENING_REBASE'"
            ).scalar_one()
            == 0
        )


async def test_source_managed_opening_trade_expense_and_derived_cash_cannot_be_edited_directly(
    client: httpx.AsyncClient, engine: Engine, anchored_wallet: dict[str, Any]
) -> None:
    stock = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "SCM",
            "name": "Synthetic source ownership",
            "quote_currency": "USD",
        },
    )
    await create(
        client,
        "trades",
        {
            "portfolio_id": anchored_wallet["portfolio"]["id"],
            "account_id": anchored_wallet["account"]["id"],
            "security_id": stock["id"],
            "trade_date": "2026-01-02",
            "side": "BUY",
            "quantity": "1",
            "price": "1",
            "currency": "USD",
            "fx_rate_hkd": "7.8",
        },
    )
    await create(client, "expenses", expense_body(anchored_wallet))
    entries = (
        await client.get(
            "/api/v1/cash-entries", params={"account_id": anchored_wallet["account"]["id"]}
        )
    ).json()["items"]
    assert {row["source_type"] for row in entries} == {
        "OPENING_BALANCE",
        "OPENING_REBASE",
        "TRADE",
        "EXPENSE",
    }
    before = snapshot(engine)
    for entry in entries:
        assert entry["editable"] is False
        for method, data in (
            ("PATCH", cash_body(anchored_wallet, reason="Synthetic direct cash edit")),
            ("DELETE", {"reason": "Synthetic direct cash void"}),
        ):
            result = await client.request(
                method,
                f"/api/v1/cash-entries/{entry['id']}",
                json=data,
                headers=version_headers(client, entry["version"]),
            )
            assert result.status_code == 409 and result.json()["code"] == "SOURCE_MANAGED"
            assert snapshot(engine) == before


@pytest.mark.parametrize(("path", "model"), [("cash-entries", CashEntry), ("expenses", Expense)])
async def test_cash_expense_response_serialization_failure_rolls_back_every_value(
    client: httpx.AsyncClient,
    engine: Engine,
    wallet: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    model: type[CashEntry] | type[Expense],
) -> None:
    def broken_dump(self: CashEntry | Expense, **kwargs: Any) -> str:
        raise ValueError("Synthetic financial serialization failure")

    before = snapshot(engine)
    monkeypatch.setattr(model, "model_dump_json", broken_dump)
    body = cash_body(wallet) if path == "cash-entries" else expense_body(wallet)
    with pytest.raises(ValueError, match="Synthetic financial serialization failure"):
        await client.post(f"/api/v1/{path}", json=body, headers=write_headers(client))
    assert snapshot(engine) == before


async def test_recorded_cash_totals_do_not_use_sqlite_overflow_or_clip_int64_min(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    maximum = "9223372036854.775807"
    await create(client, "cash-entries", cash_body(wallet, amount=maximum, fx_rate_hkd=None))
    await create(client, "cash-entries", cash_body(wallet, amount="0.000001", fx_rate_hkd=None))
    before = snapshot(engine)
    overflow = await client.get(f"/api/v1/accounts/{wallet['account']['id']}/cash-balances")
    assert overflow.status_code == 422 and overflow.json()["code"] == "FINANCIAL_OVERFLOW"
    assert snapshot(engine) == before
    # A read aggregate is not a reversible posting: full signed INT64 remains representable.
    await create(
        client,
        "cash-entries",
        cash_body(wallet, kind="WITHDRAW", amount="-" + maximum, currency="HKD", fx_rate_hkd=None),
    )
    await create(
        client,
        "cash-entries",
        cash_body(wallet, kind="WITHDRAW", amount="-0.000001", currency="HKD", fx_rate_hkd=None),
    )
    old_day = await balance(client, wallet["account"]["id"], as_of="2026-01-01")
    assert old_day["balances"][0]["recorded_balance"] == "0.000000"
    # Cancel the overflowing USD deposits before inspecting both wallets together.
    deposits = (await client.get("/api/v1/cash-entries", params={"currency": "USD"})).json()[
        "items"
    ]
    for deposit in deposits:
        result = await client.request(
            "DELETE",
            f"/api/v1/cash-entries/{deposit['id']}",
            json={"reason": "Cancel synthetic boundary deposit"},
            headers=version_headers(client, 1),
        )
        assert result.status_code == 200
    balances = {
        row["currency"]: row for row in (await balance(client, wallet["account"]["id"]))["balances"]
    }
    assert balances["HKD"]["recorded_balance"] == "-9223372036854.775808"
    assert balances["HKD"]["historical_booked_hkd_net"] == "-9223372036854.775808"
    assert balances["USD"]["recorded_balance"] == "0.000000"


async def test_cash_and_expense_owner_scopes_cover_reads_filters_corrections_and_voids(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    owner, portfolio, broker, account, cash_id, expense_id = (str(uuid4()) for _ in range(6))
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
        con.exec_driver_sql(
            "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
            "VALUES (?,?,?,'Other','USD',?)",
            (account, portfolio, broker, stamp()),
        )
        con.exec_driver_sql(
            "INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,"
            "kind,source_type,source_id,external_flow,created_at) "
            "VALUES (?,?,'2026-01-02','USD',100000000,'DEPOSIT','MANUAL_CASH',?,1,?)",
            (cash_id, account, cash_id, stamp()),
        )
        con.exec_driver_sql(
            "INSERT INTO expenses(id,account_id,date,category,amount_micro,currency,created_at) "
            "VALUES (?,?,'2026-01-03','Other',1000000,'USD',?)",
            (expense_id, account, stamp()),
        )
    for path, identifier, make in (
        ("cash-entries", cash_id, cash_body),
        ("expenses", expense_id, expense_body),
    ):
        owned = await create(client, path, make(wallet))
        before = snapshot(engine)
        for method, url, data in (
            ("GET", f"/api/v1/{path}/{identifier}", None),
            ("POST", f"/api/v1/{path}", make(wallet, account_id=account)),
            (
                "PATCH",
                f"/api/v1/{path}/{identifier}",
                make(wallet, reason="Synthetic foreign edit"),
            ),
            (
                "PATCH",
                f"/api/v1/{path}/{owned['id']}",
                make(wallet, account_id=account, reason="Synthetic foreign target"),
            ),
            ("DELETE", f"/api/v1/{path}/{identifier}", {"reason": "Synthetic foreign void"}),
        ):
            response = await client.request(
                method, url, json=data, headers=version_headers(client, 1)
            )
            assert response.status_code == 404, response.text
            assert snapshot(engine) == before
        for query in ({"account_id": account}, {"portfolio_id": portfolio}):
            response = await client.get(f"/api/v1/{path}", params=query)
            assert response.status_code == 404 and snapshot(engine) == before
        visible = (await client.get(f"/api/v1/{path}")).json()["items"]
        assert all(row["id"] != identifier for row in visible)
    foreign_balance = await client.get(f"/api/v1/accounts/{account}/cash-balances")
    assert foreign_balance.status_code == 404


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_cross_portfolio_correction_invalidates_both_owned_scopes_once(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    original = await create(client, path, make(wallet))
    portfolio = await create(client, "portfolios", {"name": "Corrected cash portfolio"})
    target = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": wallet["broker"]["id"],
            "name": "Corrected cash account",
            "default_currency": "USD",
        },
    )
    with unit_of_work(engine) as con:
        before = {
            row[0]: row[1] for row in con.exec_driver_sql("SELECT id,data_revision FROM portfolios")
        }
    changed = await client.patch(
        f"/api/v1/{path}/{original['id']}",
        json=make(wallet, account_id=target["id"], reason="Correct synthetic brokerage account"),
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200, changed.text
    with unit_of_work(engine) as con:
        assert {
            row[0]: row[1] for row in con.exec_driver_sql("SELECT id,data_revision FROM portfolios")
        } == {identifier: revision + 1 for identifier, revision in before.items()}
        invalidations = (
            con.exec_driver_sql(
                "SELECT portfolio_id FROM projection_invalidations WHERE source_id=?",
                (changed.json()["id"],),
            )
            .scalars()
            .all()
        )
        assert (
            set(invalidations) == {wallet["portfolio"]["id"], portfolio["id"]}
            and len(invalidations) == 2
        )
    assert (await balance(client, wallet["account"]["id"]))["balances"][0][
        "recorded_balance"
    ] == "0.000000"


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_archived_accounts_reject_new_cash_and_expenses_but_keep_void_available(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    original = await create(client, path, make(wallet))
    archived = await client.delete(
        f"/api/v1/accounts/{wallet['account']['id']}", headers=version_headers(client, 1)
    )
    assert archived.status_code == 200
    before = snapshot(engine)
    for method, url, data in (
        ("POST", f"/api/v1/{path}", make(wallet)),
        (
            "PATCH",
            f"/api/v1/{path}/{original['id']}",
            make(wallet, reason="Synthetic archived edit"),
        ),
    ):
        rejected = await client.request(method, url, json=data, headers=version_headers(client, 1))
        assert rejected.status_code == 409 and rejected.json()["code"] == "ARCHIVED"
        assert snapshot(engine) == before
    void = await client.request(
        "DELETE",
        f"/api/v1/{path}/{original['id']}",
        json={"reason": "Void synthetic archived mistake"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    assert (await balance(client, wallet["account"]["id"]))["balances"][0][
        "recorded_balance"
    ] == "0.000000"


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_actual_hkd_booking_requires_evidence_and_never_changes_native_cash_currency(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    result = await create(
        client,
        path,
        make(wallet, actual_hkd="15.123456", reason="Synthetic broker actual HKD booking"),
    )
    assert result["booked_hkd" if path == "cash-entries" else "hkd_amount"] == "15.123456"
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT currency,amount_micro,booked_hkd_micro FROM cash_ledger_entries"
        ).all()
        assert len(rows) == 1 and rows[0][0] == "USD"
        assert rows[0][2] == (15123456 if path == "cash-entries" else -15123456)
    before = snapshot(engine)
    mismatch = await client.post(
        f"/api/v1/{path}",
        json=make(
            wallet,
            currency="HKD",
            fx_rate_hkd=None,
            actual_hkd="15.123456",
            reason="Synthetic invalid identity override",
        ),
        headers=write_headers(client),
    )
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "CURRENCY_MISMATCH"
    assert snapshot(engine) == before


async def test_adjustment_is_explicit_micro_cash_not_an_external_deposit_or_complete_assertion(
    client: httpx.AsyncClient, wallet: dict[str, Any]
) -> None:
    entry = await create(
        client,
        "cash-entries",
        cash_body(
            wallet,
            kind="ADJUSTMENT",
            amount="0.000001",
            currency="HKD",
            fx_rate_hkd=None,
            reason="Synthetic reconciled adjustment",
        ),
    )
    assert entry["amount"] == entry["booked_hkd"] == "0.000001"
    assert entry["external_flow"] is False and entry["fx_rate_hkd"] == "1.000000000000"
    result = await client.patch(
        f"/api/v1/accounts/{wallet['account']['id']}",
        json={"cash_completeness": "PARTIAL"},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200
    recorded = await balance(client, wallet["account"]["id"])
    assert recorded["cash_completeness"] == "PARTIAL"
    assert all(
        row["balance"] is None and row["balance_quality"] == "PARTIAL"
        for row in recorded["balances"]
    )


@pytest.mark.parametrize("path", ["cash-entries", "expenses"])
async def test_cash_expense_date_filters_literal_search_and_descending_cursor(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any], path: str
) -> None:
    make = cash_body if path == "cash-entries" else expense_body
    date_field = "effective_date" if path == "cash-entries" else "date"
    records = [
        await create(client, path, make(wallet, **{date_field: day}, note=note))
        for day, note in (
            ("2026-01-02", "100%_literal"),
            ("2026-01-03", "normal"),
            ("2026-01-03", "last"),
        )
    ]
    expected = sorted(records, key=lambda row: (row[date_field], row["id"]), reverse=True)
    found, cursor = [], None
    for _ in records:
        query: dict[str, Any] = {"limit": 1, "account_id": wallet["account"]["id"]}
        if cursor:
            query["cursor"] = cursor
        result = await client.get(f"/api/v1/{path}", params=query)
        assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        found.extend(result.json()["items"])
        cursor = result.json()["next_cursor"]
    assert found == expected and cursor is None
    literal = await client.get(f"/api/v1/{path}", params={"q": "%_"})
    assert [row["id"] for row in literal.json()["items"]] == [records[0]["id"]]
    filtered = await client.get(
        f"/api/v1/{path}",
        params={
            "from_date": "2026-01-03",
            "to_date": "2026-01-03",
            "currency": "USD",
            "portfolio_id": wallet["portfolio"]["id"],
        },
    )
    assert len(filtered.json()["items"]) == 2
    corrected = await client.patch(
        f"/api/v1/{path}/{records[0]['id']}",
        json=make(wallet, **{date_field: "2026-01-02"}, reason="Correct synthetic journal amount"),
        headers=version_headers(client, 1),
    )
    assert corrected.status_code == 200
    listing = (await client.get(f"/api/v1/{path}")).json()["items"]
    if path == "cash-entries":
        assert len(listing) == 5 and {row["status"] for row in listing} == {
            "ACTIVE",
            "SUPERSEDED",
            "REVERSAL",
        }
        source = (
            await client.get(
                "/api/v1/cash-entries",
                params={
                    "source_type": "CASH_CORRECTION",
                    "entry_role": "PRIMARY",
                    "kind": "DEPOSIT",
                },
            )
        ).json()["items"]
        assert [row["id"] for row in source] == [corrected.json()["id"]]
    else:
        assert len(listing) == 3 and all(row["status"] == "ACTIVE" for row in listing)
        history = (await client.get("/api/v1/expenses", params={"include_history": True})).json()[
            "items"
        ]
        assert len(history) == 4
    for query in (
        {"limit": 0},
        {"limit": 101},
        {"from_date": "2026-02-30"},
        {"from_date": "2026-02-01", "to_date": "2026-01-01"},
        {"cursor": "invalid"},
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps(["2026-02-30", str(uuid4())]).encode()
            ).decode()
        },
    ):
        before = snapshot(engine)
        rejected = await client.get(f"/api/v1/{path}", params=query)
        assert rejected.status_code == 422 and snapshot(engine) == before


async def test_cash_and_expense_auth_csrf_reason_and_version_are_not_optional(
    client: httpx.AsyncClient, engine: Engine, wallet: dict[str, Any]
) -> None:
    for path, make in (("cash-entries", cash_body), ("expenses", expense_body)):
        original = await create(client, path, make(wallet))
        before = snapshot(engine)
        for method, headers, body, expected in (
            ("PATCH", write_headers(client), make(wallet, reason="Synthetic missing version"), 422),
            ("DELETE", version_headers(client, 1), {"reason": "short"}, 422),
            (
                "DELETE",
                {**version_headers(client, 1), "X-CSRF-Token": "synthetic-invalid-csrf"},
                {"reason": "Synthetic invalid csrf"},
                403,
            ),
        ):
            response = await client.request(
                method, f"/api/v1/{path}/{original['id']}", json=body, headers=headers
            )
            assert response.status_code == expected and snapshot(engine) == before
    client.cookies.clear()
    before = snapshot(engine)
    for method, url, anonymous_body in (
        ("GET", "/api/v1/cash-entries", None),
        ("GET", "/api/v1/expenses", None),
        ("GET", f"/api/v1/accounts/{wallet['account']['id']}/cash-balances", None),
        ("GET", f"/api/v1/cash-entries/{uuid4()}", None),
        ("GET", f"/api/v1/expenses/{uuid4()}", None),
        ("POST", "/api/v1/cash-entries", cash_body(wallet)),
        ("POST", "/api/v1/expenses", expense_body(wallet)),
        (
            "PATCH",
            f"/api/v1/cash-entries/{uuid4()}",
            cash_body(wallet, reason="Synthetic unauthenticated edit"),
        ),
        ("DELETE", f"/api/v1/expenses/{uuid4()}", {"reason": "Synthetic unauthenticated void"}),
    ):
        response = await client.request(
            method, url, json=anonymous_body, headers=version_headers(client, 1)
        )
        assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
        assert snapshot(engine) == before
