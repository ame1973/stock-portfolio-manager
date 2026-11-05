"""Real HTTP/SQLite trade UoWs: exact postings, immutable history, and zero-write failures."""

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
from spm_api.finance import Position
from spm_api.ledger_service import allocate_sequence, reverse_cash, validate_history
from spm_api.trade_models import TradeRecord
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
async def book(client: httpx.AsyncClient, engine: Engine) -> dict[str, Any]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic trade portfolio"})
    broker = await create(client, "brokers", {"name": "Synthetic trade broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic trade account",
            "default_currency": "USD",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "SYN",
            "name": "Synthetic trade security",
            "quote_currency": "USD",
        },
    )
    return {
        "owner": owner,
        "portfolio": portfolio,
        "broker": broker,
        "account": account,
        "security": security,
    }


def payload(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "portfolio_id": book["portfolio"]["id"],
        "account_id": book["account"]["id"],
        "security_id": book["security"]["id"],
        "side": "BUY",
        "trade_date": "2026-01-02",
        "quantity": "10",
        "price": "100",
        "fee": "0",
        "tax": "0",
        "currency": "USD",
        "fx_rate_hkd": "7.8",
        **changes,
    }


def position(engine: Engine, book: dict[str, Any]) -> Position:
    with unit_of_work(engine) as con:
        return validate_history(con, book["portfolio"]["id"]).get(
            (book["account"]["id"], book["security"]["id"]), Position()
        )


async def test_fin_001_posts_fees_once_and_replays_account_moving_average(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    buy = await create(client, "trades", payload(book, fee="1"))
    sale = await create(
        client,
        "trades",
        payload(book, side="SELL", trade_date="2026-01-03", quantity="4", price="120", fee="2"),
    )
    assert (buy["settlement"], buy["booked_hkd"]) == ("1001.000000", "7807.800000")
    assert (sale["settlement"], sale["booked_hkd"]) == ("478.000000", "3728.400000")
    assert position(engine, book) == Position(
        Decimal("6"), Decimal("4684.680000"), Decimal("605.280000")
    )
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT source_id,amount_micro,booked_hkd_micro,external_flow "
            "FROM cash_ledger_entries ORDER BY effective_date"
        ).all()
        assert [tuple(row) for row in rows] == [
            (buy["id"], -1001000000, -7807800000, 0),
            (sale["id"], 478000000, 3728400000, 0),
        ]
        assert con.exec_driver_sql("SELECT count(*) FROM expenses").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 2
        assert set(
            con.exec_driver_sql(
                "SELECT source_id FROM projection_invalidations WHERE reason='TRADE_FACT_CHANGED'"
            ).scalars()
        ) == {buy["id"], sale["id"]}
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='trade' AND action='CREATE'"
            ).scalar_one()
            == 2
        )
    read = await client.get(f"/api/v1/trades/{buy['id']}")
    assert read.status_code == 200 and read.json() == buy
    assert read.headers["ETag"] == '"1"' and read.headers["Cache-Control"] == "no-store"


async def test_fin_006_reference_fx_refresh_never_rebooks_trade_cash_or_cost(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    # fx_rates is a replaceable reference projection, not an append-only provider observation.
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO fx_rates VALUES "
            "('USD','HKD','2026-01-02','synthetic-reference',7800000000000,"
            "'HISTORICAL','2026-01-02T00:00:00Z')"
        )
    original = await create(client, "trades", payload(book))
    before = snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE fx_rates SET rate_e12=7900000000000,"
            "retrieved_at_utc='2026-01-03T00:00:00Z' "
            "WHERE base_currency='USD' AND quote_currency='HKD' "
            "AND as_of_date='2026-01-02' AND provider_id='synthetic-reference'"
        )
        assert (
            con.exec_driver_sql(
                "SELECT rate_e12 FROM fx_rates WHERE provider_id='synthetic-reference'"
            ).scalar_one()
            == 7900000000000
        )
    after = snapshot(engine)
    assert before["fx_rates"] != after["fx_rates"]
    assert {key: rows for key, rows in before.items() if key != "fx_rates"} == {
        key: rows for key, rows in after.items() if key != "fx_rates"
    }
    read = await client.get(f"/api/v1/trades/{original['id']}")
    assert read.status_code == 200 and read.json() == original
    assert (original["fx_rate_hkd"], original["booked_hkd"]) == (
        "7.800000000000",
        "7800.000000",
    )
    journal = await client.get("/api/v1/cash-entries", params={"account_id": book["account"]["id"]})
    assert journal.status_code == 200
    cash = journal.json()["items"]
    assert len(cash) == 1
    assert (cash[0]["amount"], cash[0]["fx_rate_hkd"], cash[0]["booked_hkd"]) == (
        "-1000.000000",
        "7.800000000000",
        "-7800.000000",
    )
    assert position(engine, book) == Position(Decimal("10"), Decimal("7800"))


async def test_fin_008_oversell_changes_zero_database_rows(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    await create(client, "trades", payload(book, quantity="2", price="50", fx_rate_hkd="1"))
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/trades",
        json=payload(book, side="SELL", trade_date="2026-01-03", quantity="3"),
        headers=write_headers(client),
    )
    assert response.status_code == 409 and response.json()["code"] == "INSUFFICIENT_SHARES"
    assert response.headers["Cache-Control"] == "no-store"
    assert snapshot(engine) == before
    assert position(engine, book) == Position(Decimal("2"), Decimal("100"))


async def test_create_retry_is_replayed_without_any_second_side_effect(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    key, body = str(uuid4()), payload(book)
    first = await create(client, "trades", body, key=key)
    before = snapshot(engine)
    assert await create(client, "trades", body, key=key) == first
    assert snapshot(engine) == before
    mismatch = await client.post(
        "/api/v1/trades", json={**body, "quantity": "11"}, headers=write_headers(client, key=key)
    )
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert snapshot(engine) == before


async def test_revision_preserves_root_sequence_and_exact_original_cash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book, fee="1"))
    key = str(uuid4())
    body = payload(book, quantity="12", fx_rate_hkd="7.9", reason="Correct synthetic statement")
    headers = version_headers(client, 1, key=key)
    response = await client.patch(f"/api/v1/trades/{original['id']}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["id"] != original["id"]
    assert revised["root_id"] == original["root_id"] == original["id"]
    assert revised["created_sequence"] == original["created_sequence"]
    assert revised["supersedes_id"] == original["id"]
    assert (revised["revision"], revised["version"], revised["booked_hkd"]) == (2, 1, "9480.000000")
    old = (await client.get(f"/api/v1/trades/{original['id']}")).json()
    assert old == {
        **original,
        "status": "SUPERSEDED",
        "version": 2,
        "updated_at": old["updated_at"],
    }
    with unit_of_work(engine) as con:
        rows = list(
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries ORDER BY rowid").mappings()
        )
        assert len(rows) == 3
        primary, reversal, replacement = rows
        assert primary["amount_micro"] == -1001000000 and primary["booked_hkd_micro"] == -7807800000
        assert primary["version"] == 2
        assert reversal["reverses_entry_id"] == primary["id"]
        for name in ("amount_micro", "booked_hkd_micro"):
            assert reversal[name] == -primary[name]
        for name in (
            "account_id",
            "effective_date",
            "currency",
            "fx_rate_hkd_e12",
            "external_flow",
        ):
            assert reversal[name] == primary[name]
        assert replacement["source_id"] == revised["id"]
        assert replacement["amount_micro"] == -1200000000
        assert replacement["booked_hkd_micro"] == -9480000000
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 1
    before = snapshot(engine)
    replayed = await client.patch(f"/api/v1/trades/{original['id']}", json=body, headers=headers)
    assert replayed.status_code == 200 and replayed.json() == revised
    assert snapshot(engine) == before
    stale = await client.patch(
        f"/api/v1/trades/{original['id']}", json=body, headers=version_headers(client, 1)
    )
    assert stale.status_code == 412 and snapshot(engine) == before
    inactive = await client.patch(
        f"/api/v1/trades/{original['id']}", json=body, headers=version_headers(client, 2)
    )
    assert inactive.status_code == 409 and inactive.json()["code"] == "INACTIVE_FACT"
    assert snapshot(engine) == before


async def test_void_is_append_only_versioned_and_idempotent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    url, body = f"/api/v1/trades/{trade['id']}", {"reason": "Remove duplicate synthetic record"}
    headers = version_headers(client, 1)
    response = await client.request("DELETE", url, json=body, headers=headers)
    assert response.status_code == 200 and response.json()["ok"] is True
    current = (await client.get(url)).json()
    assert current["status"] == "VOID" and current["version"] == 2
    assert current["quantity"] == trade["quantity"] and current["booked_hkd"] == trade["booked_hkd"]
    assert position(engine, book) == Position()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trades").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 2
        assert (
            con.exec_driver_sql("SELECT sum(amount_micro) FROM cash_ledger_entries").scalar_one()
            == 0
        )
        assert (
            con.exec_driver_sql(
                "SELECT sum(booked_hkd_micro) FROM cash_ledger_entries"
            ).scalar_one()
            == 0
        )
    before = snapshot(engine)
    retry = await client.request("DELETE", url, json=body, headers=headers)
    assert retry.status_code == 200 and retry.json() == response.json()
    assert snapshot(engine) == before
    inactive = await client.request("DELETE", url, json=body, headers=version_headers(client, 2))
    assert inactive.status_code == 409 and inactive.json()["code"] == "INACTIVE_FACT"
    assert snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_rejects_historical_correction_that_orphans_a_later_sale(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str
) -> None:
    buy = await create(client, "trades", payload(book))
    await create(
        client, "trades", payload(book, side="SELL", trade_date="2026-01-03", quantity="8")
    )
    before = snapshot(engine)
    url = f"/api/v1/trades/{buy['id']}"
    body = {"reason": "Synthetic historical correction"}
    if operation == "revise":
        body = payload(book, quantity="7", **body)
    response = await client.request(
        "PATCH" if operation == "revise" else "DELETE",
        url,
        json=body,
        headers=version_headers(client, 1),
    )
    assert response.status_code == 409 and response.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before
    assert position(engine, book).quantity == Decimal("2")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"quantity": 1}, "VALIDATION_ERROR"),
        ({"quantity": 0.1}, "VALIDATION_ERROR"),
        ({"quantity": True}, "VALIDATION_ERROR"),
        ({"quantity": "0"}, "VALIDATION_ERROR"),
        ({"quantity": "1e1"}, "VALIDATION_ERROR"),
        ({"quantity": "0.000000001"}, "VALIDATION_ERROR"),
        ({"quantity": "92233720368.54775808"}, "VALIDATION_ERROR"),
        ({"price": "NaN"}, "VALIDATION_ERROR"),
        ({"price": "1.000000001"}, "VALIDATION_ERROR"),
        ({"fee": "-1"}, "VALIDATION_ERROR"),
        ({"tax": "0.0000001"}, "VALIDATION_ERROR"),
        ({"fx_rate_hkd": "0"}, "VALIDATION_ERROR"),
        ({"fx_rate_hkd": None}, "MISSING_FX"),
        ({"fx_rate_hkd": "7.8000000000001"}, "VALIDATION_ERROR"),
        ({"trade_date": "2026-02-30"}, "VALIDATION_ERROR"),
        ({"executed_at_utc": "2026-01-02T15:00:00"}, "VALIDATION_ERROR"),
        ({"executed_at_utc": "2026-01-02T15:00:00+00:60"}, "VALIDATION_ERROR"),
        ({"executed_at_utc": "2026-01-02T15:00:00+24:00"}, "VALIDATION_ERROR"),
        ({"executed_at_utc": "0001-01-01T00:00:00+23:59"}, "VALIDATION_ERROR"),
        ({"executed_at_utc": "2026-01-02T00:30:00Z"}, "EXECUTION_DATE_MISMATCH"),
        ({"currency": "HKD"}, "CURRENCY_MISMATCH"),
        ({"actual_settlement": "1000"}, "VALIDATION_ERROR"),
        ({"actual_hkd": "7800", "override_reason": "short"}, "VALIDATION_ERROR"),
        (
            {"actual_settlement": "-1", "override_reason": "Synthetic negative actual"},
            "INVALID_FINANCIAL_VALUE",
        ),
        ({"actual_hkd": "-1", "override_reason": "Synthetic opposite sign"}, "CURRENCY_MISMATCH"),
        (
            {
                "side": "SELL",
                "actual_settlement": "-9223372036854.775808",
                "fx_rate_hkd": "1",
                "override_reason": "Synthetic non-reversible cash bound",
            },
            "FINANCIAL_OVERFLOW",
        ),
        ({"price": "92233720368", "quantity": "1000"}, "FINANCIAL_OVERFLOW"),
        ({"fx_rate_hkd": "9000000", "quantity": "20000"}, "FINANCIAL_OVERFLOW"),
    ],
)
async def test_invalid_financial_input_never_mutates_any_table(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    changes: dict[str, Any],
    code: str,
) -> None:
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/trades", json=payload(book, **changes), headers=write_headers(client)
    )
    assert response.status_code == 422 and response.json()["code"] == code, response.text
    assert snapshot(engine) == before


async def test_actuals_rounding_timestamp_and_manual_provenance_are_explicit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    body = payload(
        book,
        quantity="3.125",
        price="12.34567891",
        fee="0.125001",
        tax="0.000001",
        fx_rate_hkd="7.812345678901",
        actual_settlement="38.710249",
        actual_hkd="302.420001",
        override_reason="Synthetic broker statement actuals",
        fx_source="synthetic statement 1",
        executed_at_utc="2026-01-02T10:00:00-05:00",
    )
    trade = await create(client, "trades", body)
    assert trade["gross"] == "38.580247"
    assert trade["settlement"] == "38.710249" and trade["booked_hkd"] == "302.420001"
    assert trade["rounding_delta"] == "0.005000"
    assert trade["settlement_override"] is True and trade["hkd_override"] is True
    assert trade["fx_source"] == "MANUAL:synthetic statement 1"
    assert trade["executed_at_utc"] == "2026-01-02T15:00:00.000000Z"
    assert position(engine, book).cost_hkd == Decimal("302.420001")
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings().one()
        assert row["amount_micro"] == -38710249 and row["booked_hkd_micro"] == -302420001
    await create(client, "trades", payload(book, trade_date="2026-01-03", fx_rate_hkd="7.9"))
    assert (await client.get(f"/api/v1/trades/{trade['id']}")).json() == trade


async def test_hkd_identity_and_negative_sell_net_are_not_clamped(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Synthetic HK",
            "quote_currency": "HKD",
        },
    )
    changes = {"security_id": security["id"], "currency": "HKD", "fx_rate_hkd": None}
    buy = await create(client, "trades", payload(book, quantity="1", **changes))
    sale = await create(
        client,
        "trades",
        payload(
            book, side="SELL", trade_date="2026-01-03", quantity="1", price="0", fee="2", **changes
        ),
    )
    assert buy["fx_rate_hkd"] == "1.000000000000" and buy["fx_source"] == "IDENTITY"
    assert sale["settlement"] == "-2.000000" and sale["booked_hkd"] == "-2.000000"
    with unit_of_work(engine) as con:
        state = validate_history(con, book["portfolio"]["id"])[
            (book["account"]["id"], security["id"])
        ]
        assert state == Position(Decimal("0"), Decimal("0"), Decimal("-102"))
        assert con.exec_driver_sql(
            "SELECT currency,amount_micro FROM cash_ledger_entries ORDER BY rowid DESC LIMIT 1"
        ).one() == ("HKD", -2000000)
    before = snapshot(engine)
    mismatch = await client.post(
        "/api/v1/trades",
        json=payload(book, **{**changes, "fx_rate_hkd": "7.8"}),
        headers=write_headers(client),
    )
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "CURRENCY_MISMATCH"
    assert snapshot(engine) == before


async def test_scheduled_draft_requires_explicit_revision_to_post_and_lock_fx(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    body, key = payload(book, trade_date="2050-01-02", fx_rate_hkd=None), str(uuid4())
    draft = await create(client, "trades", body, key=key)
    assert draft["status"] == "DRAFT_SCHEDULED" and draft["booked_hkd"] is None
    assert draft["fx_rate_hkd"] is None and position(engine, book) == Position()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 0
    monkeypatch.setattr("spm_api.trade_service.now", lambda: datetime(2050, 1, 3, tzinfo=UTC))
    before = snapshot(engine)
    assert await create(client, "trades", body, key=key) == draft
    assert snapshot(engine) == before
    revision = {**body, "reason": "Explicit synthetic draft activation"}
    rejected = await client.patch(
        f"/api/v1/trades/{draft['id']}", json=revision, headers=version_headers(client, 1)
    )
    assert rejected.status_code == 422 and rejected.json()["code"] == "MISSING_FX"
    assert snapshot(engine) == before
    activated = await client.patch(
        f"/api/v1/trades/{draft['id']}",
        json={**revision, "fx_rate_hkd": "7.85"},
        headers=version_headers(client, 1),
    )
    assert activated.status_code == 200, activated.text
    assert (
        activated.json()["status"] == "ACTIVE" and activated.json()["booked_hkd"] == "7850.000000"
    )
    assert position(engine, book) == Position(Decimal("10"), Decimal("7850"))


async def test_response_serialization_failure_rolls_back_the_entire_uow(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_dump(self: TradeRecord, **kwargs: Any) -> str:
        raise ValueError("Synthetic response serialization failure")

    before = snapshot(engine)
    monkeypatch.setattr(TradeRecord, "model_dump_json", broken_dump)
    with pytest.raises(ValueError, match="Synthetic response serialization failure"):
        await client.post("/api/v1/trades", json=payload(book), headers=write_headers(client))
    assert snapshot(engine) == before


async def test_date_search_filters_and_descending_bounded_cursor(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trades = [
        await create(client, "trades", payload(book, trade_date=day, note=note))
        for day, note in (
            ("2026-01-02", "100%_literal"),
            ("2026-01-03", "regular"),
            ("2026-01-03", "last"),
        )
    ]
    expected = sorted(trades, key=lambda trade: (trade["trade_date"], trade["id"]), reverse=True)
    cursor = None
    found = []
    for _ in trades:
        query: dict[str, Any] = {"limit": 1, "portfolio_id": book["portfolio"]["id"]}
        if cursor:
            query["cursor"] = cursor
        response = await client.get("/api/v1/trades", params=query)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        page = response.json()
        found.extend(page["items"])
        cursor = page["next_cursor"]
    assert found == expected and cursor is None
    literal = await client.get("/api/v1/trades", params={"q": "%_"})
    assert [trade["id"] for trade in literal.json()["items"]] == [trades[0]["id"]]
    ticker = await client.get("/api/v1/trades", params={"q": "SYN", "from_date": "2026-01-03"})
    assert len(ticker.json()["items"]) == 2
    for query in (
        {"limit": 101},
        {"from_date": "2026-02-30"},
        {"from_date": "2026-02-01", "to_date": "2026-01-01"},
        {"cursor": "not-base64"},
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps(["not-a-date", str(uuid4())]).encode()
            ).decode()
        },
    ):
        before = snapshot(engine)
        rejected = await client.get("/api/v1/trades", params=query)
        assert rejected.status_code == 422 and snapshot(engine) == before


async def test_trade_owner_scope_covers_reads_filters_and_all_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    other, portfolio, broker, account, identifier = (str(uuid4()) for _ in range(5))
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
                "VALUES (:id,:portfolio,:broker,'Other','USD',:now)"
            ),
            {"id": account, "portfolio": portfolio, "broker": broker, "now": stamp()},
        )
        seq = con.execute(
            text("INSERT INTO ledger_sequences(kind,fact_id) VALUES ('TRADE',:id)"),
            {"id": identifier},
        ).lastrowid
        con.execute(
            text(
                "INSERT INTO trades(id,root_id,created_sequence,portfolio_id,account_id,"
                "security_id,"
                "side,trade_date,quantity_e8,price_e8,currency,gross_micro,settlement_micro,status,"
                "created_at,updated_at) VALUES (:id,:id,:seq,:portfolio,:account,:security,'BUY',"
                "'2050-01-02',100000000,100000000,'USD',1000000,1000000,'DRAFT_SCHEDULED',:now,:now)"
            ),
            {
                "id": identifier,
                "seq": seq,
                "portfolio": portfolio,
                "account": account,
                "security": book["security"]["id"],
                "now": stamp(),
            },
        )
    original = await create(client, "trades", payload(book))
    before = snapshot(engine)
    foreign_body = payload(book, portfolio_id=portfolio, account_id=account)
    for method, url, body in (
        ("GET", f"/api/v1/trades/{identifier}", None),
        ("POST", "/api/v1/trades", foreign_body),
        ("POST", "/api/v1/trades", payload(book, account_id=account)),
        ("PATCH", f"/api/v1/trades/{identifier}", payload(book, reason="Foreign synthetic edit")),
        (
            "PATCH",
            f"/api/v1/trades/{original['id']}",
            {**foreign_body, "reason": "Foreign synthetic edit"},
        ),
        ("DELETE", f"/api/v1/trades/{identifier}", {"reason": "Foreign synthetic void"}),
    ):
        response = await client.request(method, url, json=body, headers=version_headers(client, 1))
        assert response.status_code == 404, response.text
        assert snapshot(engine) == before
    for query in ({"portfolio_id": portfolio}, {"account_id": account}, {"root_id": identifier}):
        response = await client.get("/api/v1/trades", params=query)
        assert response.status_code == 404 and snapshot(engine) == before
    visible = (await client.get("/api/v1/trades", params={"include_history": True})).json()["items"]
    assert [row["id"] for row in visible] == [original["id"]]


async def test_revision_can_move_owned_account_and_portfolio_without_rewriting_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    portfolio = await create(client, "portfolios", {"name": "Corrected synthetic portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": book["broker"]["id"],
            "name": "Corrected synthetic account",
            "default_currency": "USD",
        },
    )
    mismatch = await client.post(
        "/api/v1/trades",
        json=payload(book, account_id=account["id"]),
        headers=write_headers(client),
    )
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "PORTFOLIO_MISMATCH"
    with unit_of_work(engine) as con:
        revisions: dict[str, int] = {
            row[0]: row[1] for row in con.exec_driver_sql("SELECT id,data_revision FROM portfolios")
        }
    response = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=payload(
            book,
            portfolio_id=portfolio["id"],
            account_id=account["id"],
            reason="Correct account from synthetic statement",
        ),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200, response.text
    assert position(engine, book) == Position()
    corrected = {**book, "portfolio": portfolio, "account": account}
    assert position(engine, corrected) == Position(Decimal("10"), Decimal("7800"))
    with unit_of_work(engine) as con:
        assert {
            row[0]: row[1] for row in con.exec_driver_sql("SELECT id,data_revision FROM portfolios")
        } == {identifier: revision + 1 for identifier, revision in revisions.items()}
        assert set(
            con.exec_driver_sql(
                "SELECT portfolio_id FROM projection_invalidations WHERE source_id=?",
                (response.json()["id"],),
            ).scalars()
        ) == {book["portfolio"]["id"], portfolio["id"]}
    history = (
        await client.get(
            "/api/v1/trades",
            params={
                "root_id": original["id"],
                "include_history": True,
            },
        )
    ).json()["items"]
    assert {row["id"] for row in history} == {original["id"], response.json()["id"]}
    current = (await client.get("/api/v1/trades", params={"root_id": original["id"]})).json()[
        "items"
    ]
    assert [row["id"] for row in current] == [response.json()["id"]]


async def test_archived_account_blocks_new_facts_but_allows_voiding_a_mistake(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    archived = await client.delete(
        f"/api/v1/accounts/{book['account']['id']}", headers=version_headers(client, 1)
    )
    assert archived.status_code == 200
    before = snapshot(engine)
    for method, url, body in (
        ("POST", "/api/v1/trades", payload(book)),
        (
            "PATCH",
            f"/api/v1/trades/{original['id']}",
            payload(book, reason="Synthetic archived edit"),
        ),
    ):
        rejected = await client.request(method, url, json=body, headers=version_headers(client, 1))
        assert rejected.status_code == 409 and rejected.json()["code"] == "ARCHIVED"
        assert snapshot(engine) == before
    void = await client.request(
        "DELETE",
        f"/api/v1/trades/{original['id']}",
        json={"reason": "Void synthetic duplicate"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200 and position(engine, book) == Position()


@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_unknown_execution_time_is_not_invented_to_allow_a_sale(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], side: str
) -> None:
    await create(client, "trades", payload(book, executed_at_utc="2026-01-02T15:00:00Z"))
    before = snapshot(engine)
    body = payload(book, side="SELL", quantity="1")
    if side == "BUY":
        original = (await client.get("/api/v1/trades")).json()["items"][0]
        method, url = "PATCH", f"/api/v1/trades/{original['id']}"
        await create(
            client, "trades", payload(book, side="SELL", executed_at_utc="2026-01-02T16:00:00Z")
        )
        body = payload(
            book, executed_at_utc="2026-01-02T17:00:00Z", reason="Synthetic execution correction"
        )
        before = snapshot(engine)
    else:
        method, url = "POST", "/api/v1/trades"
    response = await client.request(method, url, json=body, headers=version_headers(client, 1))
    assert response.status_code == 409 and response.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before


@pytest.fixture
async def anchored(client: httpx.AsyncClient, book: dict[str, Any]) -> dict[str, Any]:
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": book["portfolio"]["id"],
            "broker_id": book["broker"]["id"],
            "name": "Synthetic dated opening",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-10",
            "opening_fx_rate_hkd": "7.7",
            "cash_completeness": "COMPLETE",
        },
    )
    return {**book, "account": account}


async def test_pre_anchor_create_revision_and_void_preserve_declared_opening_and_original_fx(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any]
) -> None:
    def cash() -> list[dict[str, Any]]:
        with unit_of_work(engine) as con:
            return [
                dict(row)
                for row in con.execute(
                    text("SELECT * FROM cash_ledger_entries WHERE account_id=:id ORDER BY rowid"),
                    {"id": anchored["account"]["id"]},
                ).mappings()
            ]

    opening = cash()[0]
    key = str(uuid4())
    buy = await create(client, "trades", payload(anchored, fee="1"), key=key)
    rebases = [row for row in cash() if row["source_type"] == "OPENING_REBASE"]
    assert len(rebases) == 1
    assert rebases[0]["amount_micro"] == 1001000000
    assert rebases[0]["booked_hkd_micro"] == 7707700000
    assert rebases[0]["fx_rate_hkd_e12"] == 7700000000000 and rebases[0]["external_flow"] == 0
    assert sum(row["amount_micro"] for row in cash()) == 1000000000
    before = snapshot(engine)
    assert await create(client, "trades", payload(anchored, fee="1"), key=key) == buy
    assert snapshot(engine) == before
    revised = await client.patch(
        f"/api/v1/trades/{buy['id']}",
        json=payload(anchored, quantity="12", reason="Correct synthetic quantity"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200, revised.text
    assert sum(row["amount_micro"] for row in cash()) == 1000000000
    voided = await client.request(
        "DELETE",
        f"/api/v1/trades/{revised.json()['id']}",
        json={"reason": "Void synthetic historical trade"},
        headers=version_headers(client, 1),
    )
    assert voided.status_code == 200, voided.text
    rows = cash()
    assert rows[0] == opening and sum(row["amount_micro"] for row in rows) == 1000000000
    rebases = [row for row in rows if row["source_type"] == "OPENING_REBASE"]
    assert [row["amount_micro"] for row in rebases] == [1001000000, 199000000, -1200000000]
    assert sum(row["booked_hkd_micro"] for row in rebases) == 0
    assert all(row["fx_rate_hkd_e12"] == opening["fx_rate_hkd_e12"] for row in rebases)
    assert (await client.get(f"/api/v1/accounts/{anchored['account']['id']}")).json() == anchored[
        "account"
    ]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='REBASE_OPENING'"
            ).scalar_one()
            == 3
        )


@pytest.mark.parametrize("day", ["2026-01-10", "2026-01-11", "2050-01-02"])
async def test_anchor_does_not_absorb_same_day_later_or_unposted_trade_cash(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any], day: str
) -> None:
    trade = await create(client, "trades", payload(anchored, trade_date=day))
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE source_type='OPENING_REBASE'"
            ).scalar_one()
            == 0
        )
        balance = con.exec_driver_sql(
            "SELECT sum(amount_micro) FROM cash_ledger_entries"
        ).scalar_one()
        assert balance == (1000000000 if trade["status"] == "DRAFT_SCHEDULED" else 0)


async def test_opening_rebase_overflow_rolls_back_trade_cash_sequence_audit_and_revision(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any]
) -> None:
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/trades",
        json=payload(anchored, quantity="20000000000", fx_rate_hkd="0.000000000001"),
        headers=write_headers(client),
    )
    assert response.status_code == 422 and response.json()["code"] == "FINANCIAL_OVERFLOW"
    assert snapshot(engine) == before


async def test_missing_current_opening_provenance_blocks_the_whole_trade(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        original = con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings().one()
        reverse_cash(con, original, reason="Synthetic reversed opening provenance")
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/trades", json=payload(anchored), headers=write_headers(client)
    )
    assert response.status_code == 409 and response.json()["code"] == "NEEDS_REVIEW"
    assert snapshot(engine) == before


async def test_opening_assertion_does_not_create_implicit_cross_currency_funding(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any]
) -> None:
    stock = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Synthetic HK wallet",
            "quote_currency": "HKD",
        },
    )
    await create(
        client,
        "trades",
        payload(
            anchored,
            security_id=stock["id"],
            currency="HKD",
            fx_rate_hkd=None,
        ),
    )
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT currency,amount_micro,source_type FROM cash_ledger_entries ORDER BY rowid"
        ).all()
        assert [tuple(row) for row in rows] == [
            ("USD", 1000000000, "OPENING_BALANCE"),
            ("HKD", -1000000000, "TRADE"),
        ]


@pytest.mark.parametrize(
    ("action", "status", "day", "num", "den", "quantity", "code"),
    [
        ("SPLIT", "CONFIRMED", "2026-01-03", 3, 2, "15", None),
        ("REVERSE_SPLIT", "CONFIRMED", "2026-01-03", 1, 3, "3", "NEEDS_REVIEW"),
        ("MERGER", "NEEDS_REVIEW", "2026-01-03", None, None, "1", "NEEDS_REVIEW"),
        ("SYMBOL_CHANGE", "CONFIRMED", "2026-01-03", None, None, "10", None),
        ("SPLIT", "CONFIRMED", "2050-01-03", 3, 2, "11", "INSUFFICIENT_SHARES"),
    ],
)
async def test_trade_replays_stored_actions_and_never_guesses_unallocated_terms(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    action: str,
    status: str,
    day: str,
    num: int | None,
    den: int | None,
    quantity: str,
    code: str | None,
) -> None:
    await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        identifier = str(uuid4())
        seq = allocate_sequence(con, "ACTION", identifier)
        con.execute(
            text(
                "INSERT INTO corporate_actions(id,root_id,created_sequence,security_id,action_type,"
                "effective_date,ratio_num,ratio_den,status,created_at,new_symbol,previous_symbol) "
                "VALUES (:id,:id,:seq,:security,:action,:day,:num,:den,:status,:now,:new,:previous)"
            ),
            {
                "id": identifier,
                "seq": seq,
                "security": book["security"]["id"],
                "action": action,
                "new": "RENAMED" if action == "SYMBOL_CHANGE" else None,
                "previous": "SYN" if action == "SYMBOL_CHANGE" else None,
                "day": day,
                "num": num,
                "den": den,
                "status": status,
                "now": stamp(),
            },
        )
    before = snapshot(engine)
    response = await client.post(
        "/api/v1/trades",
        json=payload(book, side="SELL", trade_date="2026-01-04", quantity=quantity),
        headers=write_headers(client),
    )
    if code is not None:
        assert response.status_code == 409 and response.json()["code"] == code, response.text
        assert snapshot(engine) == before
    else:
        assert response.status_code == 201, response.text
        assert position(engine, book).quantity == 0 and position(engine, book).cost_hkd == 0


async def test_trade_cannot_oversell_or_void_shares_already_transferred_to_another_account(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    buy = await create(client, "trades", payload(book))
    target = await create(
        client,
        "accounts",
        {
            "portfolio_id": book["portfolio"]["id"],
            "broker_id": book["broker"]["id"],
            "name": "Synthetic transfer destination",
            "default_currency": "USD",
        },
    )
    with unit_of_work(engine, write=True) as con:
        identifier = str(uuid4())
        seq = allocate_sequence(con, "TRANSFER", identifier)
        con.execute(
            text(
                "INSERT INTO security_transfers(id,created_sequence,portfolio_id,source_account_id,"
                "target_account_id,security_id,transfer_date,quantity_e8,transferred_cost_hkd_micro,"
                "created_at,root_id) VALUES (:id,:seq,:portfolio,:source,:target,:security,"
                "'2026-01-03',400000000,3120000000,:now,:id)"
            ),
            {
                "id": identifier,
                "seq": seq,
                "portfolio": book["portfolio"]["id"],
                "source": book["account"]["id"],
                "target": target["id"],
                "security": book["security"]["id"],
                "now": stamp(),
            },
        )
    assert position(engine, book) == Position(Decimal("6"), Decimal("4680"))
    before = snapshot(engine)
    rejected = await client.post(
        "/api/v1/trades",
        json=payload(book, side="SELL", trade_date="2026-01-04", quantity="7"),
        headers=write_headers(client),
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before
    void = await client.request(
        "DELETE",
        f"/api/v1/trades/{buy['id']}",
        json={"reason": "Synthetic correction orphaning transfer"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 409 and void.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before
    assert position(engine, {**book, "account": target}) == Position(Decimal("4"), Decimal("3120"))


async def test_trade_guards_reject_missing_csrf_version_and_unauthenticated_access(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    before = snapshot(engine)
    url, body = f"/api/v1/trades/{original['id']}", payload(book, reason="Synthetic guarded edit")
    for method, headers, expected in (
        ("PATCH", write_headers(client), 422),
        ("PATCH", {**write_headers(client), "If-Match": "1"}, 422),
        ("PATCH", version_headers(client, 2), 412),
        ("PATCH", {**version_headers(client, 1), "X-CSRF-Token": "invalid-synthetic-csrf"}, 403),
    ):
        rejected = await client.request(method, url, json=body, headers=headers)
        assert rejected.status_code == expected and rejected.headers["Cache-Control"] == "no-store"
        assert snapshot(engine) == before
    for reason in (None, "short", "          "):
        rejected = await client.request(
            "DELETE", url, json={"reason": reason}, headers=version_headers(client, 1)
        )
        assert rejected.status_code == 422 and snapshot(engine) == before
    client.cookies.clear()
    for method, path, data in (
        ("GET", "/api/v1/trades", None),
        ("GET", url, None),
        ("POST", "/api/v1/trades", payload(book)),
        ("PATCH", url, body),
        ("DELETE", url, {"reason": "Synthetic unauthenticated void"}),
    ):
        rejected = await client.request(method, path, json=data, headers=version_headers(client, 1))
        assert rejected.status_code == 401 and snapshot(engine) == before


async def test_scheduled_sale_has_no_current_effect_and_cannot_activate_into_a_short_position(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = await create(client, "trades", payload(book, side="SELL", trade_date="2050-01-02"))
    assert draft["status"] == "DRAFT_SCHEDULED" and position(engine, book) == Position()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 0
    before = snapshot(engine)
    monkeypatch.setattr("spm_api.trade_service.now", lambda: datetime(2050, 1, 3, tzinfo=UTC))
    rejected = await client.patch(
        f"/api/v1/trades/{draft['id']}",
        json=payload(
            book, side="SELL", trade_date="2050-01-02", reason="Synthetic sale activation"
        ),
        headers=version_headers(client, 1),
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before
    voided = await client.request(
        "DELETE",
        f"/api/v1/trades/{draft['id']}",
        json={"reason": "Cancel synthetic scheduled sale"},
        headers=version_headers(client, 1),
    )
    assert voided.status_code == 200 and position(engine, book) == Position()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 0


async def test_market_local_midnight_does_not_apply_a_us_split_on_the_hong_kong_date(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = datetime(2026, 11, 5, 1, tzinfo=UTC)
    monkeypatch.setattr("spm_api.ledger_service.now", lambda: clock)
    monkeypatch.setattr("spm_api.trade_service.now", lambda: clock)
    await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        identifier = str(uuid4())
        seq = allocate_sequence(con, "ACTION", identifier)
        con.execute(
            text(
                "INSERT INTO corporate_actions(id,root_id,created_sequence,security_id,action_type,"
                "effective_date,ratio_num,ratio_den,status,created_at) "
                "VALUES (:id,:id,:seq,:security,'SPLIT','2026-11-05',3,2,'CONFIRMED',:now)"
            ),
            {"id": identifier, "seq": seq, "security": book["security"]["id"], "now": stamp()},
        )
    assert position(engine, book).quantity == Decimal("10")
    before = snapshot(engine)
    early = await client.post(
        "/api/v1/trades",
        json=payload(book, side="SELL", trade_date="2026-11-04", quantity="11"),
        headers=write_headers(client),
    )
    assert early.status_code == 409 and early.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before
    clock = datetime(2026, 11, 5, 15, tzinfo=UTC)
    await create(
        client, "trades", payload(book, side="SELL", trade_date="2026-11-05", quantity="15")
    )
    assert position(engine, book).quantity == 0


async def test_moving_an_unposted_draft_does_not_rebase_its_unchanged_former_cash_account(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any], book: dict[str, Any]
) -> None:
    draft = await create(
        client, "trades", payload(anchored, trade_date="2050-01-02", fx_rate_hkd=None)
    )
    with unit_of_work(engine, write=True) as con:
        opening = con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings().one()
        reverse_cash(con, opening, reason="Synthetic unresolved original opening")
    revised = await client.patch(
        f"/api/v1/trades/{draft['id']}",
        json=payload(book, reason="Correct synthetic draft account and date"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["account_id"] == book["account"]["id"]
    assert revised.json()["status"] == "ACTIVE"
    assert position(engine, book) == Position(Decimal("10"), Decimal("7800"))
    with unit_of_work(engine) as con:
        assert (
            con.execute(
                text("SELECT count(*) FROM cash_ledger_entries WHERE account_id=:id"),
                {"id": anchored["account"]["id"]},
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize("day", ["2026-01-10", "2026-01-11"])
async def test_same_day_or_later_trade_does_not_repair_an_unrelated_reversed_opening(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any], day: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        opening = con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings().one()
        reverse_cash(con, opening, reason="Synthetic unresolved opening")
    trade = await create(client, "trades", payload(anchored, trade_date=day))
    assert trade["status"] == "ACTIVE"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 3
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE source_type='OPENING_REBASE'"
            ).scalar_one()
            == 0
        )


async def test_other_currency_does_not_rebase_the_declared_wallet(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any]
) -> None:
    stock = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0006",
            "name": "Synthetic separate wallet",
            "quote_currency": "HKD",
        },
    )
    with unit_of_work(engine, write=True) as con:
        opening = con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings().one()
        reverse_cash(con, opening, reason="Synthetic unresolved USD opening")
    trade = await create(
        client,
        "trades",
        payload(
            anchored,
            security_id=stock["id"],
            currency="HKD",
            fx_rate_hkd=None,
        ),
    )
    assert trade["booked_hkd"] == "1000.000000"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 3


async def test_zero_net_native_revision_does_not_repair_an_unchanged_opening_assertion(
    client: httpx.AsyncClient, engine: Engine, anchored: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(anchored))
    with unit_of_work(engine, write=True) as con:
        opening = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE source_type='OPENING_BALANCE'"
            )
            .mappings()
            .one()
        )
        reverse_cash(con, opening, reason="Synthetic unresolved opening")
    revised = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=payload(
            anchored, note="Synthetic note correction", reason="Correct only the recorded note"
        ),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200, revised.text
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE source_type='OPENING_REBASE'"
            ).scalar_one()
            == 1
        )


async def test_int64_share_boundary_remains_exact_and_full_closeout_clears_cost(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    maximum = "92233720368.54775807"
    await create(client, "trades", payload(book, quantity=maximum, price="0"))
    assert position(engine, book).quantity == Decimal(maximum)
    before = snapshot(engine)
    overflow = await client.post(
        "/api/v1/trades",
        json=payload(book, quantity="0.00000001", price="0"),
        headers=write_headers(client),
    )
    assert overflow.status_code == 422 and snapshot(engine) == before
    await create(
        client,
        "trades",
        payload(book, side="SELL", trade_date="2026-01-03", quantity=maximum, price="0"),
    )
    assert position(engine, book) == Position()
