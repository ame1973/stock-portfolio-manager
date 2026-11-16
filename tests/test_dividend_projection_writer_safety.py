"""Exercise projection writer boundaries with isolated synthetic ledger data."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_projection_writer as writer
import spm_api.dividend_rejection_writer as rejection
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


async def test_upcoming_and_no_recorded_position_are_not_final_eligibility(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    upcoming = intent(engine, book, tax_setting, fee_setting, as_of="2026-01-07")
    first = save(engine, book, upcoming, uuid4())
    assert first.as_of.isoformat() < book["event"].terms.ex_date
    assert first.version.state == "EXPECTED" and first.eligible_shares == Decimal("10")
    assert first.history_completeness == "UNVERIFIED"
    earlier = intent(
        engine,
        book,
        tax_setting,
        fee_setting,
        as_of="2026-01-06",
        expected_projection_no=1,
        expected_projection_id=first.version.id,
    )
    second = save(engine, book, earlier, uuid4())
    assert second.version.state == "NO_POSITION" and second.eligible_shares is None
    assert second.scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"


async def test_independent_fee_fx_and_hkd_fx_never_fall_back_to_a_default(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book, fee="2", fee_currency="HKD")
    missing_fee_fx = intent(engine, book, tax_setting, fee_setting)
    review = save(engine, book, missing_fee_fx, uuid4())
    assert review.version.state == "NEEDS_REVIEW" and review.version.accrual is None
    assert review.fee_provenance is not None and review.fee_provenance.status == "ESTIMATED"
    explicit = intent(
        engine,
        book,
        tax_setting,
        fee_setting,
        expected_projection_no=1,
        expected_projection_id=review.version.id,
        fee_rate_event="0.125",
        fee_rate_source="MANUAL:fee-to-usd",
        fx_rate_hkd=None,
        fx_source=None,
    )
    projected = save(engine, book, explicit, uuid4())
    assert projected.version.accrual is not None
    assert projected.version.accrual.fee_local == Decimal("0.25")
    assert projected.version.accrual.net_local == Decimal("7.75")
    assert projected.version.accrual.quality == "MISSING_FX"
    assert projected.version.accrual.booked_hkd is None
    assert projected.fee_rate_event == Decimal("0.125") and projected.fx_rate_hkd is None
    converted = intent(
        engine,
        book,
        tax_setting,
        fee_setting,
        expected_projection_no=2,
        expected_projection_id=projected.version.id,
        fee_rate_event="0.125",
        fee_rate_source="MANUAL:fee-to-usd",
    )
    final = save(engine, book, converted, uuid4())
    assert final.version.accrual is not None
    assert final.version.accrual.booked_hkd == Decimal("60.45")
    assert final.fee_rate_event == Decimal("0.125") and final.fx_rate_hkd == Decimal("7.8")


async def test_explicit_zero_foreign_fee_needs_no_invented_rate(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book, fee="0", fee_currency="HKD")
    result = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    assert result.version.state == "EXPECTED" and result.fee_original == 0
    assert result.fee_currency == "HKD" and result.fee_rate_event is None
    assert result.version.accrual is not None and result.version.accrual.net_local == Decimal("8")


async def test_unknown_tax_and_fee_are_not_converted_to_zero(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book, rate=None, fee=None, fee_currency="USD")
    result = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    assert result.version.state == "NEEDS_REVIEW" and result.version.accrual is None
    assert result.tax_provenance is not None and result.tax_provenance.status == "UNKNOWN"
    assert result.fee_provenance is not None and result.fee_provenance.status == "UNKNOWN"


async def test_changed_ledger_requires_a_new_head_and_retry_stays_historical(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    first_intent, request = intent(engine, book, tax_setting, fee_setting), uuid4()
    first = save(engine, book, first_intent, request)
    await create(client, "trades", trade_body(graph, trade_date="2026-01-06", quantity="4"))
    before = database_snapshot(engine)
    assert save(engine, book, first_intent, request) == first
    stale = first_intent.model_copy(
        update={
            "id": uuid4(),
            "expected_projection_no": 1,
            "expected_projection_id": first.version.id,
        }
    )
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_INPUT_CHANGED$"):
        save(engine, book, stale, uuid4())
    assert database_snapshot(engine) == before
    refreshed = intent(
        engine,
        book,
        tax_setting,
        fee_setting,
        expected_projection_no=1,
        expected_projection_id=first.version.id,
    )
    second = save(engine, book, refreshed, uuid4())
    assert second.version.projection_no == 2 and second.eligible_shares == Decimal("14")
    assert second.portfolio_data_revision > first.portfolio_data_revision


async def test_exact_tax_fee_and_account_pins_are_required(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    value = intent(engine, book, tax_setting, fee_setting)
    attempts = [
        (value.model_copy(update={"expected_tax_selected_id": uuid4()}), "INPUT_CHANGED"),
        (value.model_copy(update={"expected_fee_selected_id": uuid4()}), "INPUT_CHANGED"),
        (value.model_copy(update={"expected_tax_head_id": uuid4()}), "TAX_SETTING_CHANGED"),
        (value.model_copy(update={"expected_fee_head_id": uuid4()}), "FEE_SETTING_CHANGED"),
        (value.model_copy(update={"account_version": value.account_version + 1}), "SCOPE_CHANGED"),
    ]
    before = database_snapshot(engine)
    for attempted, code in attempts:
        prefix = (
            "DIVIDEND_PROJECTION_" if code in {"INPUT_CHANGED", "SCOPE_CHANGED"} else "DIVIDEND_"
        )
        with pytest.raises(writer.ProjectionWriteError, match=f"^{prefix}{code}$"):
            save(engine, book, attempted, uuid4())
        assert database_snapshot(engine) == before


async def test_rejected_current_revision_cannot_gain_a_new_projection(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    with unit_of_work(engine, write=True) as con:
        rejection.save(con, book["owner"], rejection_intent(book["event"]), request_id=uuid4())
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_REVISION_REJECTED$"):
        save(engine, book, value, uuid4())
    assert database_snapshot(engine) == before


async def test_caught_intent_audit_failure_rolls_back_both_audits_and_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value, request = intent(engine, book), uuid4()
    before = database_snapshot(engine)
    count = 0

    def interrupted(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic audit interruption")
        audit(*args, **kwargs)

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "audit", side_effect=interrupted),
        pytest.raises(RuntimeError, match="synthetic audit interruption"),
    ):
        writer.save(con, book["owner"], value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, book, value, request).version.state == "NEEDS_REVIEW"


async def test_matching_intent_digest_cannot_hide_a_substituted_retained_projection(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value, request = intent(engine, book), uuid4()
    original = save(engine, book, value, request)
    before = database_snapshot(engine)
    altered = replace(original, account_version=original.account_version + 1)
    with (
        patch.object(writer, "projection_history", return_value=(altered,)),
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_CONFLICT$"),
    ):
        save(engine, book, value, request)
    assert database_snapshot(engine) == before


async def test_two_writers_cannot_publish_the_same_projection_head(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    first, second = intent(engine, book), intent(engine, book)

    def attempt(value: writer.ProjectionIntent) -> str:
        try:
            save(engine, book, value, uuid4())
            return "saved"
        except writer.ProjectionWriteError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    assert sorted(results) == ["DIVIDEND_PROJECTION_VERSION_CONFLICT", "saved"]


async def test_writing_requires_a_real_writer_and_valid_clock(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(writer.ProjectionWriteError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        writer.save(con, book["owner"], value, request_id=uuid4())
    with (
        patch.object(writer, "now", return_value=datetime(2026, 1, 1, tzinfo=UTC)),
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_TIME_CONFLICT$"),
    ):
        save(engine, book, value, uuid4())
    with (
        patch.object(writer, "now", return_value=datetime(2026, 1, 1)),
        pytest.raises(
            writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
        ),
    ):
        save(engine, book, value, uuid4())
    assert database_snapshot(engine) == before


async def test_intent_rejects_inexact_inputs_and_inconsistent_head_pairs(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    for changes in (
        {"expected_projection_no": True},
        {"expected_tax_head_no": 0, "expected_tax_head_id": uuid4()},
        {"expected_fee_head_no": 0, "expected_fee_selected_id": uuid4()},
        {"fx_rate_hkd": 7.8},
        {"fx_rate_hkd": "7.8000000000001"},
        {"fx_source": " IDENTITY "},
        {"fx_source": None},
        {"reason": "short"},
    ):
        with pytest.raises(ValidationError):
            writer.ProjectionIntent.model_validate({**value.model_dump(), **changes})
    before = database_snapshot(engine)
    invalid = value.model_copy(update={"account_version": True})
    with pytest.raises(writer.ProjectionWriteError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        save(engine, book, invalid, uuid4())
    assert database_snapshot(engine) == before


async def test_audit_accepts_only_explicit_aware_timestamps_and_normalizes_to_utc(
    engine: Engine, graph: dict[str, Any]
) -> None:
    owner = graph["owner"]["id"]
    timestamp = datetime(2026, 11, 10, 13, tzinfo=UTC) + timedelta(hours=1)
    with unit_of_work(engine, write=True) as con:
        audit(con, owner, "synthetic", "clock-test", "CLOCK_TEST", at=timestamp)
        stored = con.exec_driver_sql(
            "SELECT created_at FROM audit_events WHERE entity_type='synthetic' "
            "AND entity_id='clock-test'"
        ).scalar_one()
        assert stored == stamp(timestamp)
        with pytest.raises(ValueError, match="timezone-aware"):
            audit(con, owner, "synthetic", "naive-clock", "CLOCK_TEST", at=datetime(2026, 1, 1))
