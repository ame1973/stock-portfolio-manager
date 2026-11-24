"""Recorded reconciliation must preserve historical receipts without granting income."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_projection_history as projections
import spm_api.dividend_receipt_audit as receipts
import spm_api.dividend_reconciliation_history as reconciliation
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import post_cash
from spm_api.master_models import Currency
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import reverse_receipt
from tests.test_dividend_event_writer import AT, next_intent, terms
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_receipt_history import claim
from tests.test_dividend_receipt_void_inputs_api import create as create_recorded_receipt
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.fixture
async def book(client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]) -> dict[str, Any]:
    book = await prepare(client, engine, graph)
    first = book["event"]
    second = next_intent(
        first,
        "MANUAL",
        terms=terms(
            security_id=graph["security"]["id"],
            dps="1.1",
            currency="USD",
            pay_date="2026-01-10",
        ),
    )
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con, book["owner"], second, at=AT + timedelta(seconds=1), request_id=uuid4()
        )
        older = claim(first.revision_id, str(book["account_id"]), net=10_000_000)
        newer = claim(second.revision_id, str(book["account_id"]), net=11_000_000)
        insert(con, "dividend_entitlements", older)
        insert(con, "dividend_entitlements", newer)
    book.update(event=second, first=first, second=second, older=older, newer=newer)
    return book


def projection(engine: Engine, book: dict[str, Any], *, unknown: bool = False) -> Any:
    tax, fee = settings(engine, book, rate=None if unknown else "0", fee="0")
    return save(engine, book, intent(engine, book, tax, fee), uuid4())


def receipt(
    engine: Engine,
    book: dict[str, Any],
    *,
    amount: int = 6_000_000,
    currency: Currency = "USD",
    older: bool = True,
    cash: bool = True,
) -> dict[str, Any]:
    value = {
        "id": str(uuid4()),
        "entitlement_id": book["older" if older else "newer"]["id"],
        "account_id": str(book["account_id"]),
        "received_date": "2026-01-09",
        "received_micro": amount,
        "currency": currency,
        "tax_micro": None,
        "fee_micro": None,
        "fx_rate_hkd_e12": 1_000_000_000_000 if currency == "HKD" else None,
        "received_hkd_micro": amount if currency == "HKD" else None,
        "created_at": stamp(),
    }
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_receipts", value)
        if cash:
            post_cash(
                con,
                account=str(book["account_id"]),
                day=value["received_date"],
                currency=currency,
                amount=amount,
                fx=value["fx_rate_hkd_e12"],
                booked=value["received_hkd_micro"],
                kind="DIVIDEND_RECEIPT",
                source_type="DIVIDEND_RECEIPT",
                source_id=value["id"],
            )
    return value


def read(engine: Engine, book: dict[str, Any]) -> Any:
    with unit_of_work(engine) as con:
        return reconciliation.read(con, book["owner"], book["event"].event_id, book["account_id"])


async def test_cross_revision_recorded_comparison_never_adds_old_expectations(
    engine: Engine, book: dict[str, Any]
) -> None:
    old_receipt = receipt(engine, book)
    snapshot = projection(engine, book)
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None
    assert result.event_id == book["first"].event_id
    assert result.revision_id == book["second"].revision_id and result.revision_no == 2
    assert result.projection_id == snapshot.version.id and result.projection_no == 1
    assert result.projection_as_of == snapshot.as_of
    assert result.currency == "USD"
    assert (result.recorded_expected, result.recorded_received, result.recorded_outstanding) == (
        Decimal("11.000000"),
        Decimal("6.000000"),
        Decimal("5.000000"),
    )
    assert result.state == "RECORDED_PARTIAL"
    assert result.active_receipts == 1 and result.void_receipts == 0
    assert result.unverified_origins == 1 and result.unavailable_cash_links == 0
    assert result.scope == "UNVERIFIED_RECORDED_RECONCILIATION_ONLY"
    assert result.history_completeness == "UNVERIFIED"
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert old_receipt["id"] not in repr(result) and "6.000000" not in repr(result)
    assert database_snapshot(engine) == before


async def test_multiple_receipts_across_revisions_are_counted_once(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000)
    receipt(engine, book, amount=2_000_000, older=False)
    projection(engine, book)
    result = read(engine, book)
    assert result is not None and result.active_receipts == 2
    assert result.recorded_received == Decimal("8.000000")
    assert result.recorded_outstanding == Decimal("3.000000")
    assert result.unverified_origins == 2 and result.state == "RECORDED_PARTIAL"


async def test_latest_retained_projection_replaces_earlier_versions(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    tax, fee = settings(engine, book, rate="0", fee="0")
    first = save(engine, book, intent(engine, book, tax, fee, as_of="2026-01-07"), uuid4())
    second = save(
        engine,
        book,
        intent(
            engine,
            book,
            tax,
            fee,
            expected_projection_no=1,
            expected_projection_id=first.version.id,
        ),
        uuid4(),
    )
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None and result.projection_id == second.version.id
    assert result.projection_no == 2 and result.projection_as_of == date(2026, 1, 8)
    assert result.recorded_expected == Decimal("11.000000")
    assert result.recorded_received == Decimal("6.000000")
    assert result.recorded_outstanding == Decimal("5.000000")
    assert database_snapshot(engine) == before


async def test_zero_expected_and_explicit_zero_receipt_are_not_an_empty_history(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=0)
    tax, fee = settings(engine, book, rate="0", fee="11")
    save(engine, book, intent(engine, book, tax, fee), uuid4())
    result = read(engine, book)
    assert result is not None and result.state == "RECORDED_RECEIVED"
    assert result.active_receipts == 1
    assert result.recorded_expected == result.recorded_received == result.recorded_outstanding == 0
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"


@pytest.mark.parametrize(
    ("amount", "received", "outstanding", "state"),
    [
        (11_000_000, "11.000000", "0.000000", "RECORDED_RECEIVED"),
        (12_000_000, "12.000000", "-1.000000", "RECORDED_OVER_RECEIVED"),
        (0, "0.000000", "11.000000", "RECORDED_PARTIAL"),
        (-1_000_000, "-1.000000", "12.000000", "RECORDED_NEEDS_REVIEW"),
    ],
)
async def test_recorded_receipt_states_preserve_sign_and_explicit_zero(
    engine: Engine,
    book: dict[str, Any],
    amount: int,
    received: str,
    outstanding: str,
    state: str,
) -> None:
    receipt(engine, book, amount=amount)
    projection(engine, book)
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None and result.state == state and result.active_receipts == 1
    assert result.recorded_received == Decimal(received)
    assert result.recorded_outstanding == Decimal(outstanding)
    assert result.projection_quality == "OK"
    assert result.comparison_quality == (
        "NEEDS_REVIEW" if state in ("RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW") else "OK"
    )
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("amounts", [(INT64_MAX, 1), (-INT64_MAX,)])
async def test_recorded_sum_and_difference_overflow_fail_closed(
    engine: Engine, book: dict[str, Any], amounts: tuple[int, ...]
) -> None:
    for amount in amounts:
        receipt(engine, book, amount=amount)
    projection(engine, book)
    before = database_snapshot(engine)
    with pytest.raises(
        reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
    ):
        read(engine, book)
    assert database_snapshot(engine) == before


async def test_protected_receipt_origin_is_matched_without_granting_payment_or_income(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create_recorded_receipt(client, book)
    assert created["original_revision_id"] == str(book["first"].revision_id)
    assert created["observed_revision_id"] == str(book["second"].revision_id)
    projection(engine, book)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        result = read(engine, book)
    assert result is not None and result.state == "RECORDED_PARTIAL"
    assert result.active_receipts == 1 and result.unverified_origins == 0
    assert result.unavailable_cash_links == 0
    assert result.recorded_expected == Decimal("11.000000")
    assert result.recorded_received == Decimal("6.000000")
    assert result.recorded_outstanding == Decimal("5.000000")
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert database_snapshot(engine) == before


async def test_no_receipt_is_not_confirmed_zero_even_after_the_pay_date(
    engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine) as con:
        current = events.get(con, book["owner"], book["event"].event_id)
    assert current is not None
    assert current.record.terms.pay_date == "2026-01-10"
    assert date.fromisoformat(current.record.terms.pay_date) < current.record.created_at.date()
    projection(engine, book)
    result = read(engine, book)
    assert result is not None and result.state == "NO_RECORDED_ACTIVE_RECEIPTS"
    assert result.active_receipts == result.void_receipts == 0
    assert result.recorded_expected == Decimal("11.000000")
    assert result.recorded_received is None and result.recorded_outstanding is None
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"


async def test_missing_projection_and_unknown_estimates_are_not_zero(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    missing = read(engine, book)
    assert missing is not None and missing.state == "NO_RECORDED_PROJECTION"
    assert missing.active_receipts == 1 and missing.projection_id is None
    assert missing.projection_quality is missing.comparison_quality is None
    assert (
        missing.recorded_expected
        is missing.recorded_received
        is missing.recorded_outstanding
        is None
    )
    projection(engine, book, unknown=True)
    unavailable = read(engine, book)
    assert unavailable is not None and unavailable.state == "PROJECTION_NOT_COMPARABLE"
    assert unavailable.projection_state == "NEEDS_REVIEW"
    assert (
        unavailable.recorded_expected
        is unavailable.recorded_received
        is unavailable.recorded_outstanding
        is None
    )


async def test_recorded_projection_and_comparison_quality_remain_explicit(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    tax, fee = settings(engine, book, rate="0", fee="0")
    saved = save(
        engine,
        book,
        intent(engine, book, tax, fee, fx_rate_hkd=None, fx_source=None),
        uuid4(),
    )
    assert saved.version.accrual is not None
    assert saved.version.accrual.quality == "MISSING_FX"
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None and result.state == "RECORDED_PARTIAL"
    assert result.projection_quality == result.comparison_quality == "MISSING_FX"
    flagged = replace(saved.version.accrual, quality="NEEDS_REVIEW")
    with patch.object(
        projections,
        "history",
        return_value=(replace(saved, version=replace(saved.version, accrual=flagged)),),
    ):
        result = read(engine, book)
    assert result is not None and result.state == "RECORDED_PARTIAL"
    assert result.projection_quality == result.comparison_quality == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


async def test_negative_expected_amount_retains_review_quality_without_receipts(
    engine: Engine, book: dict[str, Any]
) -> None:
    tax, fee = settings(engine, book, rate="0", fee="12")
    saved = save(engine, book, intent(engine, book, tax, fee), uuid4())
    assert saved.version.accrual is not None
    assert saved.version.accrual.quality == "NEEDS_REVIEW"
    result = read(engine, book)
    assert result is not None and result.state == "NO_RECORDED_ACTIVE_RECEIPTS"
    assert result.recorded_expected == Decimal("-1.000000")
    assert result.projection_quality == "NEEDS_REVIEW" and result.comparison_quality is None
    receipt(engine, book)
    result = read(engine, book)
    assert result is not None and result.state == "RECORDED_NEEDS_REVIEW"
    assert result.projection_quality == result.comparison_quality == "NEEDS_REVIEW"


async def test_different_receipt_currency_is_not_netted_or_converted(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    receipt(engine, book, amount=10_000_000, currency="HKD", older=False)
    projection(engine, book)
    result = read(engine, book)
    assert result is not None and result.active_receipts == 2
    assert result.state == "CURRENCY_REVIEW" and result.currency == "USD"
    assert result.projection_quality == "OK" and result.comparison_quality == "NEEDS_REVIEW"
    assert result.recorded_received is None and result.recorded_outstanding is None


async def test_missing_cash_link_never_counts_as_settled(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, cash=False)
    projection(engine, book)
    result = read(engine, book)
    assert result is not None and result.state == "CASH_LINK_UNAVAILABLE"
    assert result.unavailable_cash_links == 1 and result.active_receipts == 1
    assert result.recorded_received is None and result.recorded_outstanding is None


async def test_void_with_exact_reversal_is_excluded_without_deleting_history(
    engine: Engine, book: dict[str, Any]
) -> None:
    original = receipt(engine, book)
    with unit_of_work(engine, write=True) as con:
        reverse_receipt(con, original, owner=book["owner"])
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
            (stamp(), original["id"]),
        )
    projection(engine, book)
    before = database_snapshot(engine)
    result = read(engine, book)
    assert result is not None and result.state == "NO_RECORDED_ACTIVE_RECEIPTS"
    assert result.active_receipts == 0 and result.void_receipts == 1
    assert result.recorded_received is None and result.recorded_outstanding is None
    assert database_snapshot(engine) == before


async def test_duplicate_receipt_or_unavailable_history_fails_closed(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    with unit_of_work(engine) as con:
        retained = receipts.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert retained is not None and len(retained) == 1
    before = database_snapshot(engine)
    with (
        patch.object(combined, "history", return_value=(retained[0], retained[0])),
        pytest.raises(
            reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
        ),
    ):
        read(engine, book)
    with (
        patch.object(
            projections,
            "history",
            side_effect=projections.ProjectionHistoryError("synthetic-private"),
        ),
        pytest.raises(
            reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
        ) as error,
    ):
        read(engine, book)
    assert "synthetic-private" not in str(error.value)
    with (
        patch.object(combined, "history", return_value=None),
        pytest.raises(
            reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
        ),
    ):
        read(engine, book)
    with (
        patch.object(
            combined,
            "history",
            side_effect=combined.CombinedReceiptHistoryError("synthetic-private"),
        ),
        pytest.raises(
            reconciliation.ReconciliationHistoryError, match="^DIVIDEND_RECONCILIATION_UNAVAILABLE$"
        ) as error,
    ):
        read(engine, book)
    assert "synthetic-private" not in str(error.value)
    assert database_snapshot(engine) == before


async def test_read_rejects_out_of_scope_and_requires_a_transaction(
    engine: Engine, book: dict[str, Any]
) -> None:
    projection(engine, book)
    with unit_of_work(engine) as con:
        assert reconciliation.read(con, uuid4(), book["event"].event_id, book["account_id"]) is None
        assert reconciliation.read(con, book["owner"], uuid4(), book["account_id"]) is None
        assert reconciliation.read(con, book["owner"], book["event"].event_id, uuid4()) is None
    with (
        engine.connect() as con,
        pytest.raises(
            reconciliation.ReconciliationHistoryError,
            match="^INVALID_DIVIDEND_RECONCILIATION_SCOPE$",
        ),
    ):
        reconciliation.read(con, book["owner"], book["event"].event_id, book["account_id"])


async def test_receipts_and_projection_share_one_wal_snapshot(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    original = projections.history
    appended: list[dict[str, Any]] = []

    def concurrent(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            appended.append(
                pool.submit(receipt, engine, book, amount=2_000_000, older=False).result()
            )
        return result

    with patch.object(projections, "history", side_effect=concurrent):
        first = read(engine, book)
    second = read(engine, book)
    assert len(appended) == 1 and first is not None and second is not None
    assert first.active_receipts == 1 and first.recorded_received == Decimal("6.000000")
    assert second.active_receipts == 2 and second.recorded_received == Decimal("8.000000")


async def test_stale_projection_and_missing_reversal_fail_closed(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    saved = projection(engine, book)
    stale_version = replace(saved.version, revision_id=book["first"].revision_id, revision_no=1)
    with patch.object(
        projections, "history", return_value=(replace(saved, version=stale_version),)
    ):
        stale = read(engine, book)
    assert stale is not None and stale.state == "STALE_RECORDED_PROJECTION"
    assert stale.recorded_expected is stale.recorded_received is stale.recorded_outstanding is None
    with unit_of_work(engine) as con:
        retained = receipts.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert retained is not None and len(retained) == 1
    broken = replace(
        retained[0].receipt,
        status="VOID",
        version=2,
        voided_at=retained[0].receipt.created_at,
        cash_scope="CASH_REVERSAL_NOT_RECORDED",
    )
    with patch.object(combined, "history", return_value=(replace(retained[0], receipt=broken),)):
        unavailable = read(engine, book)
    assert unavailable is not None and unavailable.state == "CASH_LINK_UNAVAILABLE"
    assert unavailable.unavailable_cash_links == 1 and unavailable.recorded_outstanding is None
