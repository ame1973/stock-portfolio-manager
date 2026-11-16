"""Fee writes and protected revision rejections serialize through one writer."""

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_fee_api import body, path, safe, selection
from tests.test_dividend_rejection_decision_api import body as rejection_body
from tests.test_dividend_rejection_decision_api import endpoint as rejection_endpoint
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio


async def test_competing_fee_and_rejection_never_append_after_rejection(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    scope = await selection(client, book)
    before = database_snapshot(engine)
    fee, rejection = await asyncio.gather(
        client.post(
            path(book),
            json=body(scope, fee="1"),
            headers=version_headers(client, 1, key="synthetic-fee-race"),
        ),
        client.post(
            rejection_endpoint(event.event_id),
            json=rejection_body(event.revision_id),
            headers=version_headers(client, 1, key="synthetic-rejection-race"),
        ),
    )
    assert rejection.status_code == 200
    assert fee.status_code in {201, 409}
    safe(fee)
    if fee.status_code == 409:
        assert fee.json()["code"] == "DIVIDEND_REVISION_REJECTED"
    after = database_snapshot(engine)
    saved = 1 if fee.status_code == 201 else 0
    assert len(after["dividend_fee_settings"]) == len(before["dividend_fee_settings"]) + saved
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 2 + 2 * saved
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1 + saved
    with unit_of_work(engine) as con:
        rejected = con.exec_driver_sql(
            "SELECT created_at FROM dividend_event_rejections WHERE revision_id=?",
            (str(event.revision_id),),
        ).scalar_one()
        rows = con.exec_driver_sql(
            "SELECT created_at FROM dividend_fee_settings WHERE revision_id=?",
            (str(event.revision_id),),
        ).all()
    assert len(rows) == saved
    assert all(row.created_at <= rejected for row in rows)
    current = await selection(client, book)
    assert current["revision_rejection"] == "REJECTED"
    assert (current["selected"] is not None) == bool(saved)
