"""A recorded projection must recheck scope and authentication before HTTP commit."""

from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_fee_writer as fees
import spm_api.dividend_projection_current as current
import spm_api.dividend_projection_routes as routes
import spm_api.dividend_tax_service as tax
from spm_api.audit import stamp
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_fee_writer import intent as fee_intent
from tests.test_dividend_projection_api import book as book
from tests.test_dividend_projection_write_api import body, path, safe, scope
from tests.test_dividend_tax_service import intent as tax_intent
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "broker",
        "portfolio",
        "owner",
        "revoked",
        "csrf",
        "reauth",
        "http_audit",
        "projection_audit",
        "intent_audit",
        "trade_audit",
        "event_audit",
        "tax",
        "fee",
    ],
)
async def test_late_same_transaction_change_rolls_back_projection_and_all_effects(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    headers = version_headers(client, observed["revision_no"], key=f"synthetic-late-{change}")
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(change)
        if change in {"account", "broker", "portfolio"}:
            table, column, identifier = {
                "account": ("accounts", "version", book["account_id"]),
                "broker": ("brokers", "version", book["broker_id"]),
                "portfolio": ("portfolios", "data_revision", book["portfolio_id"]),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (str(identifier),)
            )
        elif change == "owner":
            connection.exec_driver_sql(
                "UPDATE owners SET is_active=0 WHERE id=?", (str(book["owner"]),)
            )
        elif change in {"revoked", "csrf", "reauth"}:
            column, value = {
                "revoked": ("revoked_at", stamp()),
                "csrf": ("csrf_sha256", "0" * 64),
                "reauth": ("reauthenticated_at", "2000-01-01T00:00:00.000000Z"),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE sessions SET {column}=? WHERE owner_id=?", (value, str(book["owner"]))
            )
        elif change == "tax":
            tax.save(connection, book["owner"], tax_intent(book), request_id=uuid4())
        elif change == "fee":
            fees.save(connection, book["owner"], fee_intent(book), request_id=uuid4())
        else:
            entity = {
                "http_audit": "dividend_projection_http_intent",
                "projection_audit": "dividend_projection",
                "intent_audit": "dividend_projection_intent",
                "trade_audit": "trade",
                "event_audit": "dividend_event_revision",
            }[change]
            connection.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,owner_id,entity_type,entity_id,action,before_sha256,after_sha256,"
                "reason,request_id,created_at FROM audit_events WHERE entity_type=? LIMIT 1",
                (str(uuid4()), entity),
            )

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", inject)
    try:
        result = await client.post(path(book), json=payload, headers=headers)
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", inject)
    assert injected == [change], (result.status_code, result.json().get("code"))
    expected = {
        "account": (409, "DIVIDEND_PROJECTION_SCOPE_CHANGED"),
        "broker": (409, "DIVIDEND_PROJECTION_SCOPE_CHANGED"),
        "portfolio": (409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
        "owner": (401, "UNAUTHORIZED"),
        "revoked": (401, "UNAUTHORIZED"),
        "csrf": (403, "CSRF_REJECTED"),
        "reauth": (403, "REAUTH_REQUIRED"),
        "http_audit": (503, "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"),
        "projection_audit": (503, "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"),
        "intent_audit": (503, "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"),
        "trade_audit": (503, "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"),
        "event_audit": (503, "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"),
        "tax": (409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
        "fee": (409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
    }
    assert (result.status_code, result.json()["code"]) == expected[change]
    safe(result)
    assert database_snapshot(engine) == before

    retry = await client.post(path(book), json=payload, headers=headers)
    assert retry.status_code == 201
    after = database_snapshot(engine)
    replay = await client.post(path(book), json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("change", ["request", "response"])
async def test_substituted_final_projection_or_response_cannot_commit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    headers = version_headers(client, observed["revision_no"], key=f"synthetic-result-{change}")
    before = database_snapshot(engine)
    calls = 0
    original_current, original_public = current.require_current, routes._public

    def substituted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if change == "request":
            record = original_current(*args, **kwargs)
            return replace(record, request_id=uuid4()) if calls == 2 else record
        response = original_public(*args, **kwargs)
        return (
            response.model_copy(update={"reason": "Synthetic substituted response."})
            if calls == 2
            else response
        )

    target = current if change == "request" else routes
    name = "require_current" if change == "request" else "_public"
    with patch.object(target, name, side_effect=substituted):
        result = await client.post(path(book), json=payload, headers=headers)
    assert calls == 2
    assert result.status_code == 503
    assert result.json()["code"] == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.post(path(book), json=payload, headers=headers)
    assert retry.status_code == 201
