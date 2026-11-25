"""A protected tax decision must recheck authentication and scope before commit."""

from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_rejection_writer as rejections
import spm_api.dividend_tax_routes as routes
import spm_api.dividend_tax_service as tax
from spm_api.audit import stamp
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_tax_api import book as book
from tests.test_dividend_tax_api import decision, path, safe, selection

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("account", 409, "DIVIDEND_TAX_SCOPE_CHANGED"),
        ("broker", 409, "DIVIDEND_TAX_SCOPE_CHANGED"),
        ("portfolio", 409, "DIVIDEND_TAX_SCOPE_CHANGED"),
        ("data_revision", 409, "DIVIDEND_TAX_SCOPE_CHANGED"),
        ("owner", 401, "UNAUTHORIZED"),
        ("revoked", 401, "UNAUTHORIZED"),
        ("csrf", 403, "CSRF_REJECTED"),
        ("reauth", 403, "REAUTH_REQUIRED"),
        ("rejection", 409, "DIVIDEND_REVISION_REJECTED"),
        ("tax", 409, "DIVIDEND_TAX_SETTING_CHANGED"),
        ("tax_audit", 503, "DIVIDEND_TAX_HISTORY_UNAVAILABLE"),
        ("event_audit", 503, "DIVIDEND_TAX_HISTORY_UNAVAILABLE"),
        ("marker", 503, "DIVIDEND_TAX_HISTORY_UNAVAILABLE"),
    ],
)
async def test_late_same_transaction_changes_roll_back_tax_and_http_effects(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    change: str,
    status: int,
    code: str,
) -> None:
    observed = await selection(client, book)
    payload = decision(observed)
    headers = version_headers(client, 1, key=f"synthetic-tax-final-{change}")
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(change)
        if change in {"account", "broker", "portfolio", "data_revision"}:
            table, column, identifier = {
                "account": ("accounts", "version", book["account"]["id"]),
                "broker": ("brokers", "version", book["broker"]["id"]),
                "portfolio": ("portfolios", "version", book["portfolio"]["id"]),
                "data_revision": ("portfolios", "data_revision", book["portfolio"]["id"]),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (identifier,)
            )
        elif change == "owner":
            connection.exec_driver_sql(
                "UPDATE owners SET is_active=0 WHERE id=?", (book["owner"]["id"],)
            )
        elif change in {"revoked", "csrf", "reauth"}:
            column, value = {
                "revoked": ("revoked_at", stamp()),
                "csrf": ("csrf_sha256", "0" * 64),
                "reauth": ("reauthenticated_at", "2000-01-01T00:00:00.000000Z"),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE sessions SET {column}=? WHERE owner_id=?", (value, book["owner"]["id"])
            )
        elif change == "rejection":
            rejections.save(
                connection,
                UUID(book["owner"]["id"]),
                rejection_intent(book["event"]),
                request_id=uuid4(),
            )
        elif change == "tax":
            tax.save(
                connection,
                UUID(book["owner"]["id"]),
                tax.SettingIntent(
                    id=uuid4(),
                    event_id=book["event"].event_id,
                    revision_id=book["event"].revision_id,
                    revision_no=1,
                    account_id=UUID(book["account"]["id"]),
                    portfolio_id=UUID(book["portfolio"]["id"]),
                    broker_id=UUID(book["broker"]["id"]),
                    account_version=int(observed["account_version"]),
                    broker_version=int(observed["broker_version"]),
                    expected_setting_no=1,
                    effective_from="2026-01-01",
                    rate="0.1",
                    reason="Synthetic competing tax within the writer transaction.",
                ),
                request_id=uuid4(),
            )
        elif change == "marker":
            changed = connection.exec_driver_sql(
                "UPDATE projection_invalidations SET created_at=? WHERE source_id IN "
                "(SELECT id FROM dividend_tax_settings WHERE account_id=?)",
                ("2000-01-01T00:00:00.000000Z", book["account"]["id"]),
            )
            assert changed.rowcount == 1
        else:
            entity = {
                "tax_audit": "dividend_tax_setting",
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
    assert (result.status_code, result.json().get("code")) == (status, code)
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.post(path(book), json=payload, headers=headers)
    assert retry.status_code == 201
    after = database_snapshot(engine)
    replay = await client.post(path(book), json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("change", ["writer", "request", "response"])
async def test_substituted_saved_tax_or_response_cannot_commit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    observed = await selection(client, book)
    payload = decision(observed)
    headers = version_headers(client, 1, key=f"synthetic-tax-substitution-{change}")
    before = database_snapshot(engine)
    calls = 0
    original_save, original_saved = tax.save, tax._require_saved
    original_public = routes._public

    def substituted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if change == "writer":
            record = original_save(*args, **kwargs)
            return record.model_copy(update={"rate": "0.900000"})
        if change == "request":
            record = original_saved(*args, **kwargs)
            return record.model_copy(update={"request_id": uuid4()}) if calls == 2 else record
        result = original_public(*args, **kwargs)
        return (
            result.model_copy(update={"reason": "Synthetic substituted response."})
            if calls == 2
            else result
        )

    target, name = {
        "writer": (tax, "save"),
        "request": (tax, "_require_saved"),
        "response": (routes, "_public"),
    }[change]
    with patch.object(target, name, side_effect=substituted):
        result = await client.post(path(book), json=payload, headers=headers)
    assert calls == (1 if change == "writer" else 2)
    assert result.status_code == 503 and result.json()["code"] == "DIVIDEND_TAX_HISTORY_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.post(path(book), json=payload, headers=headers)
    assert retry.status_code == 201
