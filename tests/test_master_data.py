"""Master data, identity periods, ownership, atomic opening cash, and CAS regressions."""

import base64
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from spm_api.audit import stamp
from spm_api.db import unit_of_work
from tests.helpers import create, initialize, version_headers, write_headers

pytestmark = pytest.mark.anyio


@pytest.fixture
async def graph(client: httpx.AsyncClient, engine: Engine) -> dict[str, dict[str, Any]]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic portfolio"})
    broker = await create(
        client, "brokers", {"name": "Synthetic broker", "dividend_fee": "0.125001"}
    )
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic account",
            "default_currency": "HKD",
        },
    )
    return {"owner": owner, "portfolio": portfolio, "broker": broker, "account": account}


async def test_master_create_read_and_exact_fee(
    client: httpx.AsyncClient, graph: dict[str, Any]
) -> None:
    assert graph["broker"]["dividend_fee"] == "0.125001"
    assert graph["account"]["opening_cash"] is None
    assert graph["account"]["cash_completeness"] == "UNKNOWN"
    for path, item in (("portfolios", graph["portfolio"]), ("accounts", graph["account"])):
        result = await client.get(f"/api/v1/{path}/{item['id']}")
        assert result.status_code == 200 and result.headers["ETag"] == '"1"'
        assert result.json()["id"] == item["id"]
    exchanges = (await client.get("/api/v1/exchanges")).json()["items"]
    assert {row["code"] for row in exchanges} == {
        "HKG",
        "NASDAQ",
        "NYSE",
        "NYSEAMERICAN",
        "NYSEARCA",
    }


@pytest.mark.parametrize("currency", ["HKD", "USD"])
async def test_opening_cash_is_posted_once_and_not_an_external_deposit(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    currency: str,
) -> None:
    key = str(uuid4())
    body = {
        "portfolio_id": graph["portfolio"]["id"],
        "broker_id": graph["broker"]["id"],
        "name": f"Opening {currency}",
        "default_currency": currency,
        "opening_cash": "123.450001",
        "opening_cash_date": "2026-01-01",
        "cash_completeness": "COMPLETE",
    }
    if currency == "USD":
        body["opening_fx_rate_hkd"] = "7.812345678901"
    first = await create(client, "accounts", body, key=key)
    second = await create(client, "accounts", body, key=key)
    assert first == second
    assert first["opening_cash"] == "123.450001"
    with unit_of_work(engine) as con:
        row = (
            con.execute(
                text("SELECT * FROM cash_ledger_entries WHERE account_id=:id"),
                {
                    "id": first["id"],
                },
            )
            .mappings()
            .one()
        )
        assert row["amount_micro"] == 123450001
        assert row["external_flow"] == 0 and row["source_type"] == "OPENING_BALANCE"
        assert row["fx_rate_hkd_e12"] == (7812345678901 if currency == "USD" else 10**12)
        assert row["booked_hkd_micro"] == (964434082 if currency == "USD" else 123450001)
        assert (
            con.execute(
                text("SELECT count(*) FROM projection_invalidations WHERE source_id=:id"),
                {
                    "id": first["id"],
                },
            ).scalar_one()
            == 1
        )


async def test_missing_opening_fx_never_posts_partial_account(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
) -> None:
    response = await client.post(
        "/api/v1/accounts",
        headers=write_headers(client),
        json={
            "portfolio_id": graph["portfolio"]["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Missing FX",
            "default_currency": "USD",
            "opening_cash": "100",
            "opening_cash_date": "2026-01-01",
        },
    )
    assert response.status_code == 422 and response.json()["code"] == "MISSING_FX"
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 1
        assert con.execute(text("SELECT count(*) FROM cash_ledger_entries")).scalar_one() == 0


async def test_opening_hkd_overflow_rolls_back_account_audit_and_idempotency(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    tables = (
        "accounts",
        "cash_ledger_entries",
        "audit_events",
        "idempotency_keys",
        "projection_invalidations",
    )
    with unit_of_work(engine) as con:
        before = {
            table: con.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
            for table in tables
        }
        revision = con.exec_driver_sql("SELECT data_revision FROM portfolios").scalar_one()
    response = await client.post(
        "/api/v1/accounts",
        headers=write_headers(client),
        json={
            "portfolio_id": graph["portfolio"]["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Cannot store converted opening amount",
            "default_currency": "USD",
            "opening_cash": "2000000",
            "opening_cash_date": "2026-01-01",
            "opening_fx_rate_hkd": "9000000.000000000000",
        },
    )
    assert response.status_code == 422 and response.json()["code"] == "FINANCIAL_OVERFLOW"
    with unit_of_work(engine) as con:
        assert {
            table: con.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
            for table in tables
        } == before
        assert con.exec_driver_sql("SELECT data_revision FROM portfolios").scalar_one() == revision


async def test_idempotency_mismatch_and_unique_name_do_not_duplicate(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
) -> None:
    key = str(uuid4())
    await create(client, "brokers", {"name": "One"}, key=key)
    for name, expected in (("Two", "IDEMPOTENCY_CONFLICT"), ("One", "CONFLICT")):
        response = await client.post(
            "/api/v1/brokers",
            json={"name": name},
            headers=write_headers(client, key=key if name == "Two" else None),
        )
        assert response.status_code == 409 and response.json()["code"] == expected
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM brokers")).scalar_one() == 2


@pytest.mark.parametrize(
    ("path", "entity", "body"),
    [
        ("portfolios", "portfolio", {"name": "Updated portfolio"}),
        ("brokers", "broker", {"dividend_fee": "12.000001"}),
        ("accounts", "account", {"name": "Updated account"}),
    ],
)
async def test_optimistic_locking_prevents_lost_updates(
    client: httpx.AsyncClient,
    graph: dict[str, Any],
    path: str,
    entity: str,
    body: dict[str, Any],
) -> None:
    endpoint = f"/api/v1/{path}/{graph[entity]['id']}"
    first = await client.patch(endpoint, json=body, headers=version_headers(client, 1))
    assert first.status_code == 200 and first.json()["version"] == 2
    assert first.headers["ETag"] == '"2"'
    second = await client.patch(endpoint, json=body, headers=version_headers(client, 1))
    assert second.status_code == 412
    missing = await client.patch(endpoint, json=body, headers=write_headers(client))
    assert missing.status_code == 422


async def test_broker_and_account_archive_retain_history_and_allow_reopen(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
) -> None:
    broker_id, account_id = graph["broker"]["id"], graph["account"]["id"]
    assert (
        await client.delete(f"/api/v1/brokers/{broker_id}", headers=version_headers(client, 1))
    ).status_code == 200
    result = await client.post(
        "/api/v1/accounts",
        headers=write_headers(client),
        json={
            "portfolio_id": graph["portfolio"]["id"],
            "broker_id": broker_id,
            "name": "Closed broker",
            "default_currency": "HKD",
        },
    )
    assert result.status_code == 409
    assert (await client.get(f"/api/v1/accounts/{account_id}")).status_code == 200
    reopened = await client.patch(
        f"/api/v1/brokers/{broker_id}",
        headers=version_headers(client, 2),
        json={"status": "ACTIVE"},
    )
    assert reopened.status_code == 200 and reopened.json()["status"] == "ACTIVE"
    assert (
        await client.delete(f"/api/v1/accounts/{account_id}", headers=version_headers(client, 1))
    ).status_code == 200
    assert (await client.get(f"/api/v1/accounts/{account_id}")).json()["status"] == "ARCHIVED"
    with unit_of_work(engine) as con:
        assert con.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 1
        assert not con.exec_driver_sql("PRAGMA foreign_key_check").all()


async def test_other_owner_entities_are_not_accessible(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
) -> None:
    other, portfolio, broker, account = (str(uuid4()) for _ in range(4))
    with unit_of_work(engine, write=True) as con:
        con.execute(
            text("INSERT INTO owners VALUES (:id,'synthetic-hash',:now,:now,1)"),
            {"id": other, "now": stamp()},
        )
        con.execute(
            text(
                "INSERT INTO portfolios(id,owner_id,name,created_at) "
                "VALUES (:id,:owner,'Other',:now)"
            ),
            {"id": portfolio, "owner": other, "now": stamp()},
        )
        con.execute(
            text("INSERT INTO brokers(id,owner_id,name) VALUES (:id,:owner,'Other')"),
            {"id": broker, "owner": other},
        )
        con.execute(
            text(
                "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
                "VALUES (:id,:p,:b,'Other','HKD',:now)"
            ),
            {"id": account, "p": portfolio, "b": broker, "now": stamp()},
        )
    for path, identifier in (("portfolios", portfolio), ("accounts", account)):
        assert (await client.get(f"/api/v1/{path}/{identifier}")).status_code == 404
        assert (
            await client.patch(
                f"/api/v1/{path}/{identifier}",
                headers=version_headers(client, 1),
                json={"name": "Stolen"},
            )
        ).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/brokers/{broker}", headers=version_headers(client, 1), json={"name": "Stolen"}
        )
    ).status_code == 404
    for path in ("portfolios", "brokers", "accounts"):
        assert all(
            row["name"] != "Other" for row in (await client.get(f"/api/v1/{path}")).json()["items"]
        )
    rejected = await client.post(
        "/api/v1/accounts",
        headers=write_headers(client),
        json={
            "portfolio_id": graph["portfolio"]["id"],
            "broker_id": broker,
            "name": "Wrong owner",
            "default_currency": "HKD",
        },
    )
    assert rejected.status_code == 404
    with (
        pytest.raises(IntegrityError, match="ACCOUNT_BROKER_OWNER_MISMATCH"),
        unit_of_work(engine, write=True) as con,
    ):
        con.execute(
            text(
                "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
                "VALUES (:id,:p,:b,'Wrong','HKD',:now)"
            ),
            {"id": str(uuid4()), "p": graph["portfolio"]["id"], "b": broker, "now": stamp()},
        )


async def test_same_ticker_across_markets_and_leading_zero_dot_identity(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
) -> None:
    hk = await create(
        client,
        "securities",
        {
            "exchange_code": "hkg",
            "display_ticker": "0005",
            "name": "HK synthetic",
            "quote_currency": "HKD",
            "identifier_effective_from": "2026-01-01",
        },
    )
    us = await create(
        client,
        "securities",
        {
            "exchange_code": "NYSE",
            "display_ticker": "0005",
            "name": "US synthetic",
            "quote_currency": "USD",
        },
    )
    dot = await create(
        client,
        "securities",
        {
            "exchange_code": "NYSE",
            "display_ticker": "brk.b",
            "name": "Dot synthetic",
            "quote_currency": "USD",
        },
    )
    assert len({hk["id"], us["id"], dot["id"]}) == 3
    assert hk["display_ticker"] == "0005" and dot["display_ticker"] == "BRK.B"
    assert hk["verified"] is False and hk["version"] == 1
    identifiers = (await client.get(f"/api/v1/securities/{hk['id']}/identifiers")).json()["items"]
    assert (
        identifiers[0]["identifier_value"] == "0005"
        and identifiers[0]["effective_from"] == "2026-01-01"
    )
    conflict = await client.post(
        "/api/v1/securities",
        headers=write_headers(client),
        json={
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Duplicate",
            "quote_currency": "HKD",
        },
    )
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"exchange_code": "TSE"},
        {"quote_currency": "JPY"},
        {"display_ticker": "../AAPL"},
        {"display_ticker": ""},
        {"display_ticker": 5},
        {"name": "   "},
    ],
)
async def test_unsupported_or_unsafe_security_input_is_rejected(
    client: httpx.AsyncClient,
    graph: dict[str, Any],
    changes: dict[str, Any],
) -> None:
    body = {
        "exchange_code": "NASDAQ",
        "display_ticker": "TEST",
        "name": "Synthetic",
        "quote_currency": "USD",
        **changes,
    }
    assert (
        await client.post("/api/v1/securities", json=body, headers=write_headers(client))
    ).status_code == 422


async def test_manual_verification_needs_reason_and_cas(
    client: httpx.AsyncClient, graph: dict[str, Any]
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "TEST",
            "name": "Synthetic",
            "quote_currency": "USD",
        },
    )
    path = f"/api/v1/securities/{security['id']}"
    assert (
        await client.patch(path, json={"verified": True}, headers=version_headers(client, 1))
    ).status_code == 422
    accepted = await client.patch(
        path,
        json={"verified": True, "reason": "Manually checked synthetic identity"},
        headers=version_headers(client, 1),
    )
    assert accepted.status_code == 200 and accepted.json()["verified"] is True
    assert (
        await client.patch(path, json={"name": "Stale"}, headers=version_headers(client, 1))
    ).status_code == 412


async def test_provider_alias_period_correction_and_no_symbol_guessing(
    client: httpx.AsyncClient,
    graph: dict[str, Any],
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NYSE",
            "display_ticker": "BRK.B",
            "name": "Synthetic",
            "quote_currency": "USD",
        },
    )
    path = f"/api/v1/securities/{security['id']}/provider-symbols"
    assert (await client.get(path)).json()["items"] == []
    first = await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {"provider_id": "yahoo", "provider_symbol": "BRK-B", "effective_from": "2026-01-01"},
    )
    assert first["provider_symbol"] == "BRK-B" and first["verified"] is False
    overlap = await client.post(
        path,
        headers=write_headers(client),
        json={"provider_id": "yahoo", "provider_symbol": "OTHER", "effective_from": "2026-02-01"},
    )
    assert overlap.status_code == 409
    closed = await client.patch(
        f"{path}/{first['id']}",
        headers=version_headers(client, 1),
        json={
            "effective_from": "2026-01-01",
            "effective_to": "2026-01-31",
            "reason": "Corrected the explicit alias period",
        },
    )
    assert closed.status_code == 200 and closed.json()["version"] == 2
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {"provider_id": "yahoo", "provider_symbol": "NEW-B", "effective_from": "2026-02-01"},
    )
    assert len((await client.get(path)).json()["items"]) == 2
    unsafe = await client.post(
        path,
        headers=write_headers(client),
        json={
            "provider_id": "yahoo",
            "provider_symbol": "https://attacker.invalid/",
            "effective_from": "2026-03-01",
        },
    )
    assert unsafe.status_code == 422


async def test_identifier_period_correction_preserves_historical_alias(
    client: httpx.AsyncClient,
    graph: dict[str, Any],
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0700",
            "name": "Synthetic",
            "quote_currency": "HKD",
            "identifier_effective_from": "2026-01-01",
        },
    )
    path = f"/api/v1/securities/{security['id']}/identifiers"
    initial = (await client.get(path)).json()["items"][0]
    response = await client.patch(
        f"{path}/{initial['id']}",
        headers=version_headers(client, 1),
        json={
            "effective_from": "2025-01-01",
            "effective_to": None,
            "reason": "Corrected historical identity start",
        },
    )
    assert response.status_code == 200 and response.json()["effective_from"] == "2025-01-01"
    mismatch = await client.post(
        path,
        headers=write_headers(client),
        json={
            "exchange_code": "NYSE",
            "identifier_type": "TICKER",
            "identifier_value": "OTHER",
            "effective_from": "2026-01-01",
        },
    )
    assert mismatch.status_code == 422
    overlap = await client.post(
        path,
        headers=write_headers(client),
        json={
            "exchange_code": "HKG",
            "identifier_type": "TICKER",
            "identifier_value": "0700",
            "effective_from": "2026-02-01",
        },
    )
    assert overlap.status_code == 409


async def test_alphabetical_pagination_search_and_literal_wildcards(
    client: httpx.AsyncClient,
    graph: dict[str, Any],
) -> None:
    for name in ("Alpha", "Beta", "100% literal", "under_score"):
        await create(client, "brokers", {"name": name})
    first = (await client.get("/api/v1/brokers?limit=2")).json()
    second = (
        await client.get("/api/v1/brokers", params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    assert [row["name"] for row in first["items"]] == ["100% literal", "Alpha"]
    assert [row["name"] for row in second["items"]] == ["Beta", "Synthetic broker"]
    for search, expected in (("%", "100% literal"), ("_", "under_score"), ("alpha", "Alpha")):
        result = (await client.get("/api/v1/brokers", params={"q": search})).json()
        assert [item["name"] for item in result["items"]] == [expected]
    assert (await client.get("/api/v1/brokers", params={"q": "' OR 1=1 --"})).json()["items"] == []


@pytest.mark.parametrize(
    "cursor", ["not-base64", "%%%", base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode()]
)
async def test_malformed_cursor_is_a_safe_validation_error(
    client: httpx.AsyncClient,
    graph: dict[str, Any],
    cursor: str,
) -> None:
    response = await client.get("/api/v1/brokers", params={"cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"


async def test_scaled_integer_affinity_cannot_accept_real_money(
    engine: Engine,
    graph: dict[str, Any],
) -> None:
    with (
        pytest.raises(IntegrityError, match="SCALED_VALUE_MUST_BE_INTEGER"),
        unit_of_work(engine, write=True) as con,
    ):
        con.execute(
            text("UPDATE brokers SET dividend_fee_micro=1.5 WHERE id=:id"),
            {"id": graph["broker"]["id"]},
        )
    with (
        pytest.raises(IntegrityError, match="SCALED_VALUE_MUST_BE_INTEGER"),
        unit_of_work(engine, write=True) as con,
    ):
        con.execute(
            text("""
            INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,
                                           kind,source_type,source_id,created_at)
            VALUES (:id,:account,'2026-01-01','HKD',1.5,'ADJUSTMENT','SYNTHETIC',:id,:now)
        """),
            {"id": str(uuid4()), "account": graph["account"]["id"], "now": stamp()},
        )
    with unit_of_work(engine) as con:
        assert (
            con.execute(
                text("SELECT dividend_fee_micro FROM brokers WHERE id=:id"),
                {"id": graph["broker"]["id"]},
            ).scalar_one()
            == 125001
        )


@pytest.mark.parametrize("path", ["portfolios", "brokers", "accounts", "securities", "exchanges"])
async def test_master_lists_require_a_session(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(f"/api/v1/{path}")
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
