"""Reconcile both legacy and direct recorded cash without granting income."""

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_reconciliation_history as reconciliation
import tests.test_dividend_direct_receipt_history as direct_fixture
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_reconciliation_api import path, safe
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import projection, read, receipt
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def direct_receipt(engine: Engine, book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    with patch.object(direct_fixture, "OWNER", book["owner"]):
        return direct_fixture.add_receipt(
            engine, (book["event"], str(book["account_id"])), **changes
        )


async def test_cross_revision_legacy_and_current_direct_cash_are_counted_once(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000, older=True)
    latest = projection(engine, book)
    direct_receipt(engine, book, received_micro=2_000_000)
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None
    assert result.revision_id == book["second"].revision_id
    assert result.projection_id == latest.version.id and result.projection_no == 1
    assert (result.recorded_expected, result.recorded_received, result.recorded_outstanding) == (
        Decimal("11.000000"),
        Decimal("8.000000"),
        Decimal("3.000000"),
    )
    assert result.active_receipts == 2 and result.unverified_origins == 1
    assert result.unavailable_cash_links == 0 and result.state == "RECORDED_PARTIAL"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    wire = response.json()
    assert (wire["recorded_expected"], wire["recorded_received"], wire["recorded_outstanding"]) == (
        "11.000000",
        "8.000000",
        "3.000000",
    )
    assert (wire["active_receipts"], wire["unverified_origins"]) == ("2", "1")
    assert database_snapshot(engine) == before


async def test_void_direct_cash_is_excluded_without_rewriting_old_receipts(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    direct = direct_receipt(engine, book, received_micro=2_000_000)
    with patch.object(direct_fixture, "OWNER", book["owner"]):
        direct_fixture.void_receipt(engine, direct)
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None
    assert result.recorded_received == Decimal("6.000000")
    assert result.recorded_outstanding == Decimal("5.000000")
    assert result.active_receipts == 1 and result.void_receipts == 1
    assert database_snapshot(engine) == before


async def test_direct_and_legacy_cash_in_different_currencies_are_not_subtracted(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, currency="USD")
    projection(engine, book)
    direct_receipt(
        engine,
        book,
        currency="HKD",
        received_micro=2_000_000,
        fx_rate_hkd_e12=1_000_000_000_000,
        received_hkd_micro=2_000_000,
    )
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None and result.state == "CURRENCY_REVIEW"
    assert result.recorded_expected == Decimal("11.000000")
    assert result.recorded_received is result.recorded_outstanding is None
    assert result.comparison_quality == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


async def test_no_projection_does_not_convert_direct_cash_to_expected_zero(
    engine: Engine, book: dict[str, Any]
) -> None:
    direct_receipt(engine, book)
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None and result.state == "NO_RECORDED_PROJECTION"
    assert result.active_receipts == 1
    assert (
        result.recorded_expected is result.recorded_received is result.recorded_outstanding is None
    )
    assert database_snapshot(engine) == before


async def test_unsealed_direct_record_cannot_be_hidden_by_legacy_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(book["event"], str(book["account_id"])))
    before = database_snapshot(engine)
    with pytest.raises(
        reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
    ):
        read(engine, book)
    response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "DIVIDEND_RECONCILIATION_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_cross_store_sum_overflow_is_not_rounded_or_truncated(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000)
    projection(engine, book)
    direct_receipt(engine, book, received_micro=INT64_MAX)
    before = database_snapshot(engine)
    with pytest.raises(
        reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
    ):
        read(engine, book)
    response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before
