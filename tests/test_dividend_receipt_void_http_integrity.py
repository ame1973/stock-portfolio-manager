"""Adversarial checks for receipt void HTTP replay and commit integrity."""

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_audit as receipt_audit
import spm_api.dividend_receipt_void as voids
import spm_api.dividend_receipt_void_write_models as models
import spm_api.dividend_receipt_void_write_routes as routes
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_history_api import safe
from tests.test_dividend_receipt_void_inputs_api import create
from tests.test_dividend_receipt_void_write_api import body, inputs, path
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def test_version_and_error_mapping_are_bounded_and_fixed() -> None:
    assert routes._version('"1"') == 1
    for invalid in ("1", '"0"', '"01"', '"9223372036854775808"', '"' + "9" * 100 + '"'):
        with pytest.raises(ApiError) as error:
            routes._version(invalid)
        assert error.value.status == 422
    for code, status in (
        ("DIVIDEND_RECEIPT_NOT_FOUND", 404),
        ("DIVIDEND_RECEIPT_OWNER_UNAVAILABLE", 401),
        ("INVALID_DIVIDEND_RECEIPT_VOID_SCOPE", 422),
        ("DIVIDEND_RECEIPT_VOID_VERSION_CHANGED", 412),
        ("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED", 409),
        ("DIVIDEND_RECEIPT_VOID_CONFLICT", 409),
        ("DIVIDEND_RECEIPT_UNVERIFIED", 409),
        ("synthetic-private-diagnostic", 503),
    ):
        mapped = routes._failure(voids.ReceiptVoidError(code))
        assert mapped.status == status
        assert "synthetic-private" not in mapped.message


async def test_public_result_and_reversal_reject_inconsistent_retained_facts(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent_body = body(await inputs(client, book, original["id"]))
    response = await client.post(
        path(original["id"]), json=intent_body, headers=version_headers(client, 1)
    )
    assert response.status_code == 200, response.text
    intent = routes._intent(
        UUID(original["id"]), models.ReceiptVoidWrite.model_validate(intent_body), 1
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        retained = receipt_audit.history(
            con, book["owner"], book["event"].event_id, book["account_id"]
        )
        assert retained is not None and len(retained) == 1
        record = retained[0]
        assert record.origin is not None
        for invalid in (
            replace(record, origin=None),
            replace(record, audit_scope="RECEIPT_AUDIT_NOT_VERIFIED"),
            replace(record, receipt=replace(record.receipt, id=uuid4())),
            replace(record, receipt=replace(record.receipt, status="ACTIVE")),
            replace(record, receipt=replace(record.receipt, version=3)),
            replace(record, receipt=replace(record.receipt, voided_at=None)),
            replace(record, entitlement_id=uuid4()),
            replace(record, original_revision_id=uuid4()),
            replace(record, origin=replace(record.origin, cash_entry_id=uuid4())),
        ):
            with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"):
                routes._public(con, intent, invalid)
        with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"):
            routes._reversal(con, replace(record, origin=None))
        with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"):
            routes._request_id(con, uuid4())
    fake = MagicMock(spec=Connection)
    fake.execute.return_value.all.return_value = []
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"):
        routes._reversal(fake, record)
    assert database_snapshot(engine) == before


async def test_mismatched_http_audit_binding_cannot_be_replayed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-void-http-binding")
    response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 200, response.text
    before = database_snapshot(engine)
    with patch.object(routes, "_binding", return_value={"scope": "OTHER"}):
        replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert (
        replay.status_code == 503 and replay.json()["code"] == "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"
    )
    safe(replay)
    assert database_snapshot(engine) == before


async def test_existing_internal_void_cannot_gain_http_intent_from_a_request_id_collision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent_body = body(await inputs(client, book, original["id"]))
    intent = routes._intent(
        UUID(original["id"]), models.ReceiptVoidWrite.model_validate(intent_body), 1
    )
    request_id = uuid4()
    with unit_of_work(engine, write=True) as con:
        voids.void(con, book["owner"], intent, request_id=request_id)
    before = database_snapshot(engine)
    with patch("spm_api.main.uuid4", return_value=request_id):
        response = await client.post(
            path(original["id"]), json=intent_body, headers=version_headers(client, 1)
        )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_CONFLICT"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("problem", "status"),
    [
        ("missing-scope", 404),
        ("missing-receipt", 404),
        ("duplicate", 503),
        ("origin", 409),
        ("audit", 409),
    ],
)
async def test_new_void_rejects_missing_or_forged_retained_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str, status: int
) -> None:
    original = await create(client, book)
    intent_body = body(await inputs(client, book, original["id"]))
    with unit_of_work(engine) as con:
        retained = receipt_audit.history(
            con, book["owner"], book["event"].event_id, book["account_id"]
        )
    assert retained is not None and len(retained) == 1
    record = retained[0]
    broken = {
        "missing-scope": None,
        "missing-receipt": (),
        "duplicate": (record, record),
        "origin": (replace(record, origin=None),),
        "audit": (replace(record, audit_scope="RECEIPT_AUDIT_NOT_VERIFIED"),),
    }[problem]
    before = database_snapshot(engine)
    with patch.object(receipt_audit, "history", return_value=broken):
        response = await client.post(
            path(original["id"]), json=intent_body, headers=version_headers(client, 1)
        )
    assert response.status_code == status
    safe(response)
    assert database_snapshot(engine) == before


async def test_expired_void_idempotency_key_does_not_adopt_or_repeat_reversal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-expired-void-key")
    first = await client.post(path(original["id"]), json=intent, headers=headers)
    assert first.status_code == 200, first.text
    with unit_of_work(engine, write=True) as con:
        rows = con.exec_driver_sql(
            "SELECT id FROM idempotency_keys WHERE owner_id=? AND route=? AND idem_key=?",
            (str(book["owner"]), f"POST {path(original['id'])}", headers["Idempotency-Key"]),
        ).all()
        assert len(rows) == 1
        con.exec_driver_sql("DELETE FROM idempotency_keys WHERE id=?", (rows[0].id,))
    before = database_snapshot(engine)
    replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert replay.status_code == 409
    assert replay.json()["code"] == "DIVIDEND_RECEIPT_VOID_CONFLICT"
    safe(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["origin", "late-origin", "substitution"])
async def test_changed_writer_result_aborts_entire_transaction(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    real = voids.void
    calls = 0

    def changed(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        record = real(*args, **kwargs)
        calls += 1
        if (problem == "origin" and calls == 1) or (problem == "late-origin" and calls == 2):
            return replace(record, origin=None)
        if problem == "substitution" and calls == 2:
            return replace(record, receipt=replace(record.receipt, id=uuid4()))
        return record

    before = database_snapshot(engine)
    with patch.object(voids, "void", side_effect=changed):
        response = await client.post(
            path(original["id"]), json=intent, headers=version_headers(client, 1)
        )
    assert response.status_code == 503
    assert calls == (1 if problem == "origin" else 2)
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_retained_portfolio_change_after_idempotency_insert_aborts_void(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    old_portfolio = original["portfolio_id"]
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
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_void_old_portfolio AFTER INSERT ON idempotency_keys "
            f"WHEN NEW.route='POST {path(original['id'])}' BEGIN "
            f"UPDATE portfolios SET data_revision=data_revision+1 WHERE id='{old_portfolio}'; END"
        )
    intent = body(await inputs(client, book, original["id"]))
    before = database_snapshot(engine)
    response = await client.post(
        path(original["id"]), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_moved_account_void_invalidates_current_and_retained_portfolios_once(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    old_portfolio = original["portfolio_id"]
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
        before = {
            row.id: row.data_revision
            for row in con.exec_driver_sql(
                "SELECT id,data_revision FROM portfolios WHERE id IN (?,?)",
                (old_portfolio, str(new_portfolio)),
            )
        }
    intent = body(await inputs(client, book, original["id"]))
    headers = version_headers(client, 1, key="synthetic-moved-void")
    response = await client.post(path(original["id"]), json=intent, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["origin_portfolio_id"] == old_portfolio
    assert response.json()["portfolio_id"] == str(new_portfolio)
    with unit_of_work(engine) as con:
        after = {
            row.id: row.data_revision
            for row in con.exec_driver_sql(
                "SELECT id,data_revision FROM portfolios WHERE id IN (?,?)",
                (old_portfolio, str(new_portfolio)),
            )
        }
    assert after == {identifier: revision + 1 for identifier, revision in before.items()}
    snapshot = database_snapshot(engine)
    replay = await client.post(path(original["id"]), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert database_snapshot(engine) == snapshot


async def test_void_never_loads_private_provider_config_or_network(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, book)
    intent = body(await inputs(client, book, original["id"]))
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.post(
            path(original["id"]), json=intent, headers=version_headers(client, 1)
        )
    assert response.status_code == 200, response.text
    safe(response)
