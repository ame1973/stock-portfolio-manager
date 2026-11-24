"""A late same-transaction old-portfolio change must abort a receipt void."""

from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_void as voids
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_void_inputs_api import create
from tests.test_dividend_receipt_void_write_api import body, inputs, path
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


async def test_old_portfolio_change_after_void_verification_rolls_back_all_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    old_portfolio = created["portfolio_id"]
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
    intent = body(await inputs(client, book, created["id"]))
    before = database_snapshot(engine)
    original = voids._verify_void
    changed = False

    def late(
        con: Connection,
        owner: UUID,
        value: voids.ReceiptVoidIntent,
        request_id: UUID,
        result: Any,
    ) -> None:
        nonlocal changed
        original(con, owner, value, request_id, result)
        if not changed:
            changed = True
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?", (old_portfolio,)
            )

    with patch.object(voids, "_verify_void", side_effect=late):
        response = await client.post(
            path(created["id"]), json=intent, headers=version_headers(client, 1)
        )
    assert changed
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED"
    assert database_snapshot(engine) == before
