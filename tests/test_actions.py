"""Real action UoWs: rational terms, honest review, immutable history and display-only symbols."""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.action_models import CorporateAction
from spm_api.db import unit_of_work
from spm_api.finance import FinanceError, Position
from spm_api.ledger_service import validate_history
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
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic action portfolio"})
    broker = await create(client, "brokers", {"name": "Synthetic action broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic action account",
            "default_currency": "USD",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "ACT",
            "name": "Synthetic action security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    buy_body = {
        "portfolio_id": portfolio["id"],
        "account_id": account["id"],
        "security_id": security["id"],
        "side": "BUY",
        "trade_date": "2026-01-02",
        "quantity": "10",
        "price": "100",
        "fee": "1",
        "currency": "USD",
        "fx_rate_hkd": "7.8",
    }
    buy = await create(client, "trades", buy_body)
    return {
        "portfolio": portfolio,
        "broker": broker,
        "account": account,
        "security": security,
        "buy": buy,
        "buy_body": buy_body,
    }


def action_body(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "security_id": book["security"]["id"],
        "action_type": "SPLIT",
        "effective_date": "2026-01-03",
        "ratio_numerator": 3,
        "ratio_denominator": 2,
        "status": "CONFIRMED",
        "reason": "Synthetic reviewed action terms",
        **changes,
    }


def symbol_body(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return action_body(
        book,
        **{
            "action_type": "SYMBOL_CHANGE",
            "ratio_numerator": None,
            "ratio_denominator": None,
            "new_symbol": "NEW",
            **changes,
        },
    )


def position(engine: Engine, book: dict[str, Any]) -> Position:
    with unit_of_work(engine) as con:
        return validate_history(con, book["portfolio"]["id"])[
            book["account"]["id"], book["security"]["id"]
        ]


async def test_confirmed_three_for_two_split_is_exact_no_cash_and_idempotent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine) as con:
        cash_before = list(con.exec_driver_sql("SELECT * FROM cash_ledger_entries"))
    body, key = action_body(book), str(uuid4())
    action = await create(client, "corporate-actions", body, key=key)
    assert action["status"] == "CONFIRMED" and action["effect_scope"] == "QUANTITY_TERMS"
    assert action["allocation_scope"] == "TERMS_NOT_A_VALUATION" and action["is_current"] is True
    assert position(engine, book) == Position(Decimal("15"), Decimal("7807.8"))
    assert position(engine, book).average_cost_hkd == Decimal("520.52")
    before = snapshot(engine)
    assert await create(client, "corporate-actions", body, key=key) == action
    assert snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert list(con.exec_driver_sql("SELECT * FROM cash_ledger_entries")) == cash_before
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM ledger_sequences WHERE kind='ACTION'"
            ).scalar_one()
            == 1
        )
    read = await client.get(f"/api/v1/corporate-actions/{action['id']}")
    assert read.status_code == 200 and read.json() == action and read.headers["ETag"] == '"1"'


async def test_revision_keeps_root_sequence_old_ratio_and_full_history_dependence(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "corporate-actions", action_body(book))
    sale = await create(
        client,
        "trades",
        {
            **book["buy_body"],
            "side": "SELL",
            "trade_date": "2026-01-04",
            "quantity": "15",
            "price": "120",
        },
    )
    assert position(engine, book).quantity == 0
    before = snapshot(engine)
    rejected = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{original['id']}",
        json={"reason": "Synthetic orphaning split void"},
        headers=version_headers(client, 1),
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "INSUFFICIENT_SHARES"
    assert snapshot(engine) == before
    for data, code in (
        (action_body(book, ratio_numerator=4, ratio_denominator=3), "NEEDS_REVIEW"),
        (
            action_body(book, status="NEEDS_REVIEW", effective_date="2026-01-05"),
            "INSUFFICIENT_SHARES",
        ),
    ):
        rejected = await client.patch(
            f"/api/v1/corporate-actions/{original['id']}",
            json=data,
            headers=version_headers(client, 1),
        )
        assert rejected.status_code == 409 and rejected.json()["code"] == code
        assert snapshot(engine) == before
    body, headers = (
        action_body(book, ratio_numerator=2, ratio_denominator=1),
        version_headers(client, 1),
    )
    changed = await client.patch(
        f"/api/v1/corporate-actions/{original['id']}", json=body, headers=headers
    )
    assert changed.status_code == 200, changed.text
    revised = changed.json()
    assert revised["root_id"] == original["id"] and revised["supersedes_id"] == original["id"]
    assert revised["created_sequence"] == original["created_sequence"] and revised["revision"] == 2
    assert position(engine, book).quantity == Decimal("5")
    old = (await client.get(f"/api/v1/corporate-actions/{original['id']}")).json()
    assert old["ratio_numerator"] == 3 and old["status"] == "CONFIRMED"
    assert (
        old["lifecycle_status"] == "SUPERSEDED"
        and old["is_current"] is False
        and old["version"] == 2
    )
    before = snapshot(engine)
    retry = await client.patch(
        f"/api/v1/corporate-actions/{original['id']}", json=body, headers=headers
    )
    assert retry.status_code == 200 and retry.json() == revised and snapshot(engine) == before
    void_sale = await client.request(
        "DELETE",
        f"/api/v1/trades/{sale['id']}",
        json={"reason": "Void synthetic sale before action"},
        headers=version_headers(client, 1),
    )
    assert void_sale.status_code == 200
    reason, headers = {"reason": "Void synthetic adopted split"}, version_headers(client, 1)
    void = await client.request(
        "DELETE", f"/api/v1/corporate-actions/{revised['id']}", json=reason, headers=headers
    )
    assert void.status_code == 200 and position(engine, book) == Position(
        Decimal("10"), Decimal("7807.8")
    )
    before = snapshot(engine)
    retry = await client.request(
        "DELETE", f"/api/v1/corporate-actions/{revised['id']}", json=reason, headers=headers
    )
    assert retry.status_code == 200 and retry.json() == void.json() and snapshot(engine) == before


@pytest.mark.parametrize("kind", ["CASH_IN_LIEU", "MERGER", "SPIN_OFF"])
async def test_unallocated_action_is_real_review_not_fake_ratio_cash_or_successful_projection(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], kind: str
) -> None:
    body = action_body(
        book, action_type=kind, ratio_numerator=None, ratio_denominator=None, status="NEEDS_REVIEW"
    )
    saved = await create(client, "corporate-actions", body)
    assert saved["effect_scope"] == "REVIEW_ONLY" and saved["review_required"] is True
    assert saved["ratio_numerator"] is saved["ratio_denominator"] is None
    with pytest.raises(FinanceError) as review:
        position(engine, book)
    assert review.value.code == "NEEDS_REVIEW"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 1
        item = (
            con.exec_driver_sql("SELECT * FROM review_items WHERE status='OPEN'").mappings().one()
        )
        detail = json.loads(item["detail_json"])
        assert (
            detail["action_root_id"] == saved["root_id"]
            and item["portfolio_id"] == book["portfolio"]["id"]
        )
    before = snapshot(engine)
    rejected = await client.post(
        "/api/v1/corporate-actions",
        json={**body, "effective_date": "2026-01-04", "status": "CONFIRMED"},
        headers=write_headers(client),
    )
    assert rejected.status_code == 422 and snapshot(engine) == before
    removed = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{saved['id']}",
        json={"reason": "Withdraw synthetic unallocated terms"},
        headers=version_headers(client, 1),
    )
    assert removed.status_code == 200 and position(engine, book).quantity == 10
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM review_items WHERE status='OPEN'"
            ).scalar_one()
            == 0
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM review_items WHERE status='RESOLVED'"
            ).scalar_one()
            == 1
        )


async def test_confirmation_coexists_with_later_unallocated_review_without_partial_positions(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    later = await create(
        client,
        "corporate-actions",
        action_body(
            book,
            action_type="MERGER",
            effective_date="2026-01-04",
            ratio_numerator=None,
            ratio_denominator=None,
            status="NEEDS_REVIEW",
        ),
    )
    first = await create(client, "corporate-actions", action_body(book))
    assert first["status"] == "CONFIRMED" and first["review_required"] is True
    with pytest.raises(FinanceError, match="unallocated"):
        position(engine, book)
    removed = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{later['id']}",
        json={"reason": "Withdraw synthetic later candidate"},
        headers=version_headers(client, 1),
    )
    assert removed.status_code == 200 and position(engine, book).quantity == 15


@pytest.mark.parametrize(
    "changes",
    [
        {"ratio_numerator": 3.0},
        {"ratio_numerator": "3"},
        {"ratio_numerator": True},
        {"ratio_numerator": 0},
        {"ratio_numerator": 9223372036854775808},
        {"ratio_numerator": None},
        {"ratio_denominator": None},
        {"ratio_numerator": None, "ratio_denominator": None},
        {"action_type": "SYMBOL_CHANGE", "ratio_numerator": None, "ratio_denominator": None},
        {"ratio_numerator": 1, "ratio_denominator": 1},
        {"ratio_numerator": 1, "ratio_denominator": 2},
        {"action_type": "REVERSE_SPLIT"},
        {"effective_date": "2026-02-30"},
        {"reason": "short"},
        {"new_symbol": "OTHER"},
        {"evidence_url": "file:///private/evidence"},
        {"evidence_url": "https://user:secret@example.test/doc"},
        {"evidence_url": "https://example.test:999999/doc"},
        {"evidence_url": "https://example.test/ contains space"},
        {"evidence_url": "not a URL"},
        {"status": "VOID"},
        {"ratio_numerator": 3, "ratio_denominator": 2, "cash_amount": "100"},
    ],
)
async def test_invalid_action_input_is_zero_write_and_never_fabricates_terms(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    before = snapshot(engine)
    rejected = await client.post(
        "/api/v1/corporate-actions",
        json=action_body(book, **changes),
        headers=write_headers(client),
    )
    assert rejected.status_code == 422, rejected.text
    assert snapshot(engine) == before


async def test_symbol_display_change_preserves_financial_identity_and_aliases(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    aliases_before = (
        await client.get(f"/api/v1/securities/{book['security']['id']}/identifiers")
    ).json()
    provider = await create(
        client,
        f"securities/{book['security']['id']}/provider-symbols",
        {"provider_id": "yahoo", "provider_symbol": "ACT", "effective_from": "2020-01-01"},
    )
    changed = await create(
        client,
        "corporate-actions",
        symbol_body(book, new_symbol="brk.b", evidence_url="https://example.test/dated-notice"),
    )
    assert changed["new_symbol"] == "BRK.B" and changed["previous_symbol"] == "ACT"
    assert changed["effect_scope"] == "DISPLAY_ONLY" and changed["review_required"] is True
    security = (await client.get(f"/api/v1/securities/{book['security']['id']}")).json()
    assert security["id"] == book["security"]["id"] and security["display_ticker"] == "BRK.B"
    assert (await client.get(f"/api/v1/trades/{book['buy']['id']}")).json() == book["buy"]
    assert (
        await client.get(f"/api/v1/securities/{security['id']}/identifiers")
    ).json() == aliases_before
    assert (await client.get(f"/api/v1/securities/{security['id']}/provider-symbols")).json()[
        "items"
    ] == [provider]
    assert position(engine, book) == Position(Decimal("10"), Decimal("7807.8"))
    renamed = await client.patch(
        f"/api/v1/securities/{security['id']}",
        json={"name": "Independently corrected name"},
        headers=version_headers(client, security["version"]),
    )
    assert renamed.status_code == 200
    void = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{changed['id']}",
        json={"reason": "Void synthetic display-only action"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    restored = (await client.get(f"/api/v1/securities/{security['id']}")).json()
    assert (
        restored["display_ticker"] == "ACT" and restored["name"] == "Independently corrected name"
    )


async def test_symbol_revision_dependency_and_future_confirmation_never_guess_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = await create(client, "corporate-actions", symbol_body(book))
    revised = await client.patch(
        f"/api/v1/corporate-actions/{first['id']}",
        json=symbol_body(book, new_symbol="NEXT"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    current = revised.json()
    assert (
        current["previous_symbol"] == "ACT"
        and current["created_sequence"] == first["created_sequence"]
    )
    later = await create(
        client,
        "corporate-actions",
        symbol_body(book, effective_date="2026-01-04", new_symbol="LATEST"),
    )
    before = snapshot(engine)
    for method, url, data, code in (
        (
            "DELETE",
            f"/api/v1/corporate-actions/{current['id']}",
            {"reason": "Synthetic dependent identity void"},
            "SYMBOL_HISTORY_DEPENDENCY",
        ),
        (
            "POST",
            "/api/v1/corporate-actions",
            symbol_body(book, effective_date="2026-01-02", new_symbol="EARLIER"),
            "SYMBOL_HISTORY_DEPENDENCY",
        ),
        (
            "POST",
            "/api/v1/corporate-actions",
            symbol_body(book, effective_date="2050-01-01", new_symbol="FUTURE"),
            "FUTURE_SYMBOL_CHANGE",
        ),
    ):
        rejected = await client.request(method, url, json=data, headers=version_headers(client, 1))
        assert rejected.json()["code"] == code and rejected.status_code in {409, 422}
        assert snapshot(engine) == before
    pending = await create(
        client,
        "corporate-actions",
        symbol_body(book, effective_date="2050-01-01", new_symbol="FUTURE", status="NEEDS_REVIEW"),
    )
    assert pending["effect_scope"] == "REVIEW_ONLY"
    assert (await client.get(f"/api/v1/securities/{book['security']['id']}")).json()[
        "display_ticker"
    ] == later["new_symbol"]


async def test_action_duplicate_identity_security_move_and_stale_versions_do_not_mutate(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "corporate-actions", action_body(book))
    other = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Other identity",
            "quote_currency": "HKD",
        },
    )
    before = snapshot(engine)
    duplicate = await client.post(
        "/api/v1/corporate-actions", json=action_body(book), headers=write_headers(client)
    )
    assert duplicate.status_code == 409 and snapshot(engine) == before
    moved = await client.patch(
        f"/api/v1/corporate-actions/{original['id']}",
        json=action_body(book, security_id=other["id"]),
        headers=version_headers(client, 1),
    )
    assert moved.status_code == 409 and moved.json()["code"] == "IMMUTABLE_IDENTITY"
    stale = await client.patch(
        f"/api/v1/corporate-actions/{original['id']}",
        json=action_body(book),
        headers=version_headers(client, 2),
    )
    assert stale.status_code == 412 and snapshot(engine) == before


async def test_action_lists_date_identity_history_literal_cursor_and_no_store(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    actions = [
        await create(
            client,
            "corporate-actions",
            action_body(book, effective_date=day, status="NEEDS_REVIEW", note=note),
        )
        for day, note in (("2026-01-03", "100%_literal"), ("2026-01-04", "normal"))
    ]
    actions.append(
        await create(
            client,
            "corporate-actions",
            action_body(
                book,
                action_type="REVERSE_SPLIT",
                ratio_numerator=1,
                ratio_denominator=2,
                effective_date="2026-01-04",
                status="NEEDS_REVIEW",
                note="second type",
            ),
        )
    )
    expected = sorted(actions, key=lambda row: (row["effective_date"], row["id"]), reverse=True)
    found, cursor = [], None
    for _ in actions:
        query: dict[str, Any] = {"security_id": book["security"]["id"], "limit": 1}
        if cursor:
            query["cursor"] = cursor
        response = await client.get("/api/v1/corporate-actions", params=query)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        found.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
    assert found == expected and cursor is None
    literal = (await client.get("/api/v1/corporate-actions", params={"q": "%_"})).json()["items"]
    assert [row["id"] for row in literal] == [actions[0]["id"]]
    filtered = (
        await client.get(
            "/api/v1/corporate-actions",
            params={
                "action_type": "REVERSE_SPLIT",
                "status": "NEEDS_REVIEW",
                "from_date": "2026-01-04",
                "to_date": "2026-01-04",
            },
        )
    ).json()["items"]
    assert [row["id"] for row in filtered] == [actions[2]["id"]]
    revised = await client.patch(
        f"/api/v1/corporate-actions/{actions[0]['id']}",
        json=action_body(book, status="NEEDS_REVIEW", note="Corrected review evidence"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    history = (
        await client.get(
            "/api/v1/corporate-actions",
            params={"root_id": actions[0]["id"], "include_history": True},
        )
    ).json()["items"]
    assert {row["id"] for row in history} == {actions[0]["id"], revised.json()["id"]}
    current = (
        await client.get("/api/v1/corporate-actions", params={"root_id": actions[0]["id"]})
    ).json()["items"]
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
        rejected = await client.get("/api/v1/corporate-actions", params=query)
        assert rejected.status_code == 422 and snapshot(engine) == before


async def test_future_split_terms_do_not_affect_today_and_exact_reverse_split_preserves_cost(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    future = await create(
        client,
        "corporate-actions",
        action_body(book, effective_date="2050-01-03", ratio_numerator=2, ratio_denominator=1),
    )
    assert position(engine, book).quantity == 10
    monkeypatch.setattr("spm_api.ledger_service.now", lambda: datetime(2050, 1, 4, tzinfo=UTC))
    assert position(engine, book).quantity == 20
    assert (await client.get(f"/api/v1/corporate-actions/{future['id']}")).json() == future
    monkeypatch.setattr("spm_api.ledger_service.now", lambda: datetime(2026, 11, 5, tzinfo=UTC))
    reverse = await create(
        client,
        "corporate-actions",
        action_body(book, action_type="REVERSE_SPLIT", ratio_numerator=1, ratio_denominator=2),
    )
    assert reverse["effect_scope"] == "QUANTITY_TERMS"
    assert position(engine, book) == Position(Decimal("5"), Decimal("7807.8"))


async def test_unrelated_review_cannot_deadlock_confirmation_for_a_different_security(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    other = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "SAFE",
            "name": "Independent action timeline",
            "quote_currency": "USD",
        },
    )
    await create(client, "trades", {**book["buy_body"], "security_id": other["id"]})
    await create(
        client,
        "corporate-actions",
        action_body(
            book,
            action_type="MERGER",
            ratio_numerator=None,
            ratio_denominator=None,
            status="NEEDS_REVIEW",
        ),
    )
    accepted = await create(client, "corporate-actions", action_body(book, security_id=other["id"]))
    assert accepted["status"] == "CONFIRMED" and accepted["review_required"] is False
    with pytest.raises(FinanceError, match="unallocated"):
        position(engine, book)


async def test_symbol_collision_and_later_display_tampering_are_not_silently_overwritten(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "TAKEN",
            "name": "Occupied ticker",
            "quote_currency": "USD",
        },
    )
    before = snapshot(engine)
    collision = await client.post(
        "/api/v1/corporate-actions",
        json=symbol_body(book, new_symbol="TAKEN"),
        headers=write_headers(client),
    )
    assert collision.status_code == 409 and snapshot(engine) == before
    confirmed = await create(client, "corporate-actions", symbol_body(book))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE securities SET display_ticker='INDEPENDENT',version=version+1 WHERE id=?",
            (book["security"]["id"],),
        )
    before = snapshot(engine)
    conflict = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{confirmed['id']}",
        json={"reason": "Synthetic stale display rollback"},
        headers=version_headers(client, 1),
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "NEEDS_REVIEW"
    assert snapshot(engine) == before


async def test_catalogue_action_without_portfolio_keeps_global_review_and_requires_authentication(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    unused = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0999",
            "name": "Unheld action catalogue",
            "quote_currency": "HKD",
        },
    )
    review = await create(
        client,
        "corporate-actions",
        action_body(book, security_id=unused["id"], status="NEEDS_REVIEW"),
    )
    assert review["review_required"] is True
    with unit_of_work(engine) as con:
        item = con.exec_driver_sql(
            "SELECT portfolio_id FROM review_items WHERE status='OPEN'"
        ).one()
        assert item[0] is None
    before = snapshot(engine)
    for method, headers, data, expected in (
        ("PATCH", write_headers(client), action_body(book), 422),
        ("DELETE", version_headers(client, 1), {"reason": "short"}, 422),
        (
            "DELETE",
            {**version_headers(client, 1), "X-CSRF-Token": "invalid-synthetic-csrf"},
            {"reason": "Synthetic invalid action csrf"},
            403,
        ),
    ):
        rejected = await client.request(
            method, f"/api/v1/corporate-actions/{review['id']}", json=data, headers=headers
        )
        assert rejected.status_code == expected and snapshot(engine) == before
    client.cookies.clear()
    for method, path, anonymous_body in (
        ("GET", "/api/v1/corporate-actions", None),
        ("GET", f"/api/v1/corporate-actions/{review['id']}", None),
        ("POST", "/api/v1/corporate-actions", action_body(book)),
        ("PATCH", f"/api/v1/corporate-actions/{review['id']}", action_body(book)),
        (
            "DELETE",
            f"/api/v1/corporate-actions/{review['id']}",
            {"reason": "Synthetic unauthenticated action void"},
        ),
    ):
        rejected = await client.request(
            method, path, json=anonymous_body, headers=version_headers(client, 1)
        )
        assert rejected.status_code == 401 and rejected.headers["Cache-Control"] == "no-store"
        assert snapshot(engine) == before


async def test_missing_action_idempotency_conflict_and_terminal_void_are_zero_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    missing = str(uuid4())
    before = snapshot(engine)
    for method, url, data in (
        ("GET", f"/api/v1/corporate-actions/{missing}", None),
        ("GET", f"/api/v1/corporate-actions?root_id={missing}", None),
        ("PATCH", f"/api/v1/corporate-actions/{missing}", action_body(book)),
        (
            "DELETE",
            f"/api/v1/corporate-actions/{missing}",
            {"reason": "Synthetic missing action void"},
        ),
    ):
        response = await client.request(method, url, json=data, headers=version_headers(client, 1))
        assert response.status_code == 404 and snapshot(engine) == before
    key = str(uuid4())
    created = await create(client, "corporate-actions", action_body(book), key=key)
    before = snapshot(engine)
    conflict = await client.post(
        "/api/v1/corporate-actions",
        json=action_body(book, ratio_numerator=2, ratio_denominator=1),
        headers=write_headers(client, key=key),
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert snapshot(engine) == before
    url = f"/api/v1/corporate-actions/{created['id']}"
    reason = {"reason": "Synthetic terminal action void"}
    voided = await client.request("DELETE", url, json=reason, headers=version_headers(client, 1))
    assert voided.status_code == 200 and position(engine, book).quantity == 10
    record = (await client.get(url)).json()
    assert record["status"] == record["lifecycle_status"] == "VOID"
    assert record["effect_scope"] == "NONE" and record["version"] == 2
    before = snapshot(engine)
    for method in ("PATCH", "DELETE"):
        response = await client.request(
            method,
            url,
            json=action_body(book) if method == "PATCH" else reason,
            headers=version_headers(client, 2),
        )
        assert response.status_code == 409 and response.json()["code"] == "INACTIVE_FACT"
        assert snapshot(engine) == before


async def test_same_display_adoption_and_void_do_not_invent_security_edits_or_alias_changes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    url = f"/api/v1/securities/{book['security']['id']}"
    before = (await client.get(url)).json()
    action = await create(client, "corporate-actions", symbol_body(book, new_symbol="ACT"))
    assert action["effect_scope"] == "DISPLAY_ONLY" and action["review_required"] is True
    assert (await client.get(url)).json() == before
    voided = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{action['id']}",
        json={"reason": "Synthetic redundant symbol adoption"},
        headers=version_headers(client, 1),
    )
    assert voided.status_code == 200 and (await client.get(url)).json() == before
    assert position(engine, book) == Position(Decimal("10"), Decimal("7807.8"))


async def test_symbol_serialization_failure_rolls_back_label_review_audit_and_sequence(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: CorporateAction, **kwargs: Any) -> str:
        raise ValueError("Synthetic action serialization failure")

    monkeypatch.setattr(CorporateAction, "model_dump_json", broken)
    before = snapshot(engine)
    with pytest.raises(ValueError, match="Synthetic action serialization failure"):
        await client.post(
            "/api/v1/corporate-actions", json=symbol_body(book), headers=write_headers(client)
        )
    assert snapshot(engine) == before
