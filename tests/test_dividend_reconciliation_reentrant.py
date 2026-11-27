"""Recorded reconciliation rejects same-connection changes during its snapshot read."""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_event_service as events
import spm_api.dividend_projection_history as projections
import spm_api.dividend_reconciliation_history as reconciliation
from spm_api.audit import stamp
from spm_api.db import savepoint, unit_of_work
from spm_api.ledger_service import post_cash
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import projection, receipt
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


class Rollback(Exception):
    """Roll back only the synthetic write used by a test."""


def append_receipt(con: Connection, book: dict[str, Any]) -> None:
    value: dict[str, Any] = {
        "id": str(uuid4()),
        "entitlement_id": book["older"]["id"],
        "account_id": str(book["account_id"]),
        "received_date": "2026-01-09",
        "received_micro": 2_000_000,
        "currency": "USD",
        "tax_micro": None,
        "fee_micro": None,
        "fx_rate_hkd_e12": None,
        "received_hkd_micro": None,
        "created_at": stamp(),
    }
    insert(con, "dividend_receipts", value)
    post_cash(
        con,
        account=str(book["account_id"]),
        day=value["received_date"],
        currency="USD",
        amount=2_000_000,
        fx=None,
        booked=None,
        kind="DIVIDEND_RECEIPT",
        source_type="DIVIDEND_RECEIPT",
        source_id=value["id"],
    )


@pytest.mark.parametrize("rolled_back", [False, True])
async def test_late_same_connection_receipt_never_yields_a_stale_comparison(
    engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch, rolled_back: bool
) -> None:
    receipt(engine, book)
    projection(engine, book)
    before = database_snapshot(engine)
    original = combined.history
    injected: list[bool] = []

    def changing_history(con: Connection, *args: Any, **kwargs: Any) -> Any:
        result = original(con, *args, **kwargs)
        injected.append(True)
        context = pytest.raises(Rollback) if rolled_back else nullcontext()
        with context, savepoint(con):
            append_receipt(con, book)
            if rolled_back:
                raise Rollback
        return result

    monkeypatch.setattr(combined, "history", changing_history)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(
            reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
        ),
        savepoint(con),
    ):
        reconciliation.read(con, book["owner"], book["event"].event_id, book["account_id"])
    assert injected == [True]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("point", ["event", "projection"])
async def test_same_connection_write_cannot_turn_an_absence_into_a_clean_404(
    engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    before = database_snapshot(engine)
    module = events if point == "event" else projections
    name = "get" if point == "event" else "history"
    original = getattr(module, name)
    injected: list[bool] = []

    def changing_read(con: Connection, *args: Any, **kwargs: Any) -> Any:
        result = original(con, *args, **kwargs)
        assert result is None
        injected.append(True)
        assert (
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                (str(book["portfolio_id"]),),
            ).rowcount
            == 1
        )
        return result

    monkeypatch.setattr(module, name, changing_read)
    event_id = uuid4() if point == "event" else book["event"].event_id
    account_id = uuid4() if point == "projection" else book["account_id"]
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(
            reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
        ),
        savepoint(con),
    ):
        reconciliation.read(con, book["owner"], event_id, account_id)
    assert injected == [True]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("has_driver", [False, True])
async def test_unsupported_connection_fails_closed_before_a_read(
    book: dict[str, Any], has_driver: bool
) -> None:
    driver = SimpleNamespace(driver_connection=object()) if has_driver else object()
    fake = SimpleNamespace(in_transaction=lambda: True, connection=driver)
    with pytest.raises(
        reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
    ):
        reconciliation.read(
            cast(Connection, fake), book["owner"], book["event"].event_id, book["account_id"]
        )


async def test_earlier_unrelated_write_does_not_invalidate_the_read(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        assert (
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                (str(book["portfolio_id"]),),
            ).rowcount
            == 1
        )
        result = reconciliation.read(con, book["owner"], book["event"].event_id, book["account_id"])
        assert result is not None and result.state == "RECORDED_PARTIAL"
        raise Rollback
    assert database_snapshot(engine) == before


async def test_api_rejects_late_receipt_and_rolls_back(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt(engine, book)
    projection(engine, book)
    before = database_snapshot(engine)
    original = combined.history
    injected: list[bool] = []

    def changing_history(con: Connection, *args: Any, **kwargs: Any) -> Any:
        result = original(con, *args, **kwargs)
        injected.append(True)
        append_receipt(con, book)
        return result

    monkeypatch.setattr(combined, "history", changing_history)
    response = await client.get(
        f"/api/v1/dividends/events/{book['event'].event_id}/accounts/{book['account_id']}/reconciliation"
    )
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_RECONCILIATION_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert injected == [True]
    assert database_snapshot(engine) == before
