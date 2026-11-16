"""Current projection use must recheck every observed boundary without writing."""

from datetime import date, datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_fee_writer as fees
import spm_api.dividend_projection_current as current_service
import spm_api.dividend_projection_writer as writer
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import REASON
from tests.test_dividend_projection_current import current
from tests.test_dividend_projection_history import append
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


async def test_current_fence_handles_a_recorded_no_position_state(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    no_position = save(engine, book, intent(engine, book), uuid4())
    assert no_position.version.state == "NO_POSITION"
    before = database_snapshot(engine)
    assert current(engine, book, no_position, as_of=date(2026, 1, 8)) == no_position
    assert database_snapshot(engine) == before


async def test_current_fence_preserves_independent_missing_fee_and_valuation_fx(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book, fee="2", fee_currency="HKD")
    missing_fee = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    assert missing_fee.version.state == "NEEDS_REVIEW"
    assert current(engine, book, missing_fee, as_of=date(2026, 1, 8)) == missing_fee
    missing_hkd = save(
        engine,
        book,
        intent(
            engine,
            book,
            tax_setting,
            fee_setting,
            expected_projection_no=1,
            expected_projection_id=missing_fee.version.id,
            fee_rate_event="0.125",
            fee_rate_source="MANUAL:fee-to-usd",
            fx_rate_hkd=None,
            fx_source=None,
        ),
        uuid4(),
    )
    assert missing_hkd.version.accrual is not None
    assert missing_hkd.version.accrual.quality == "MISSING_FX"
    before = database_snapshot(engine)
    assert current(engine, book, missing_hkd, as_of=date(2026, 1, 8)) == missing_hkd
    assert database_snapshot(engine) == before


async def test_current_fence_reconstructs_explicit_hkd_identity_without_assuming_usd_fx(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, currency="HKD")
    tax_setting, fee_setting = settings(engine, book, fee_currency="HKD")
    record = save(
        engine,
        book,
        intent(engine, book, tax_setting, fee_setting, fx_rate_hkd=None, fx_source=None),
        uuid4(),
    )
    before = database_snapshot(engine)
    assert current(engine, book, record, as_of=date(2026, 1, 8)) == record
    assert record.fx_source == "IDENTITY" and record.fee_rate_source == "IDENTITY"
    assert database_snapshot(engine) == before


async def test_current_fence_rejects_legacy_projection_without_tax_and_fee_sources(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    with (
        patch("tests.test_dividend_projection_history.OWNER", book["owner"]),
        unit_of_work(engine, write=True) as con,
    ):
        raw = append(con, book["event"], str(book["account_id"]))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_UNVERIFIED$"),
    ):
        current_service.require_current(
            con,
            book["owner"],
            book["event"].event_id,
            book["account_id"],
            projection_id=UUID(raw["id"]),
            projection_no=1,
            as_of=date(2026, 1, 8),
        )
    assert database_snapshot(engine) == before


async def test_fee_head_change_is_detected_independently_of_portfolio_revision(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    record = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    newer = fees.FeeIntent(
        id=uuid4(),
        event_id=book["event"].event_id,
        revision_id=book["event"].revision_id,
        revision_no=book["event"].revision_no,
        portfolio_id=book["portfolio_id"],
        account_id=book["account_id"],
        broker_id=book["broker_id"],
        account_version=book["account_version"],
        broker_version=book["broker_version"],
        expected_setting_no=fee_setting.setting_no,
        expected_head_id=fee_setting.id,
        effective_from="2026-01-01",
        fee_status="ESTIMATED",
        fee="0.6",
        fee_currency="USD",
        reason=REASON,
    )
    with unit_of_work(engine, write=True) as con:
        fees.save(con, book["owner"], newer, request_id=uuid4())
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_INPUT_CHANGED$"):
        current(engine, book, record, as_of=date(2026, 1, 8))
    actual_get = eligibility.get

    def stale_revision(*args: Any, **kwargs: Any) -> Any:
        from dataclasses import replace

        result = actual_get(*args, **kwargs)
        assert result is not None
        return replace(result, data_revision=record.portfolio_data_revision)

    monkeypatch.setattr(eligibility, "get", stale_revision)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_FEE_SETTING_CHANGED$"):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before


async def test_account_broker_versions_are_rechecked_even_without_revision_change(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    record = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE brokers SET version=version+1 WHERE id=?", (str(book["broker_id"]),)
        )
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_SCOPE_CHANGED$"):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before


async def test_input_change_within_the_writer_requires_revalidation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    record = save(engine, book, intent(engine, book), uuid4())
    with unit_of_work(engine, write=True) as con:
        kwargs = {
            "projection_id": record.version.id,
            "projection_no": record.version.projection_no,
            "as_of": date(2026, 1, 8),
        }
        args = (con, book["owner"], book["event"].event_id, book["account_id"])
        assert current_service.require_current(*args, **kwargs) == record
        invalidate_portfolio(
            con, str(book["portfolio_id"]), str(uuid4()), "SYNTHETIC", "2026-01-01"
        )
        with pytest.raises(
            writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_INPUT_CHANGED$"
        ):
            current_service.require_current(*args, **kwargs)


async def test_invalid_or_missing_current_scope_fails_without_writes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    before = database_snapshot(engine)
    invalid: tuple[Any, ...] = (0, -1, True, 2**63, "1")
    with unit_of_work(engine, write=True) as con:
        for bad in invalid:
            with pytest.raises(
                writer.ProjectionWriteError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"
            ):
                current_service.require_current(
                    con,
                    book["owner"],
                    book["event"].event_id,
                    book["account_id"],
                    projection_id=value.id,
                    projection_no=bad,
                    as_of=date(2026, 1, 8),
                )
        with pytest.raises(
            writer.ProjectionWriteError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"
        ):
            current_service.require_current(
                con,
                book["owner"],
                book["event"].event_id,
                book["account_id"],
                projection_id=value.id,
                projection_no=1,
                as_of=datetime(2026, 1, 8),
            )
        with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_NOT_FOUND$"):
            current_service.require_current(
                con,
                book["owner"],
                book["event"].event_id,
                book["account_id"],
                projection_id=value.id,
                projection_no=1,
                as_of=date(2026, 1, 8),
            )
    assert database_snapshot(engine) == before


async def test_database_error_is_private_and_does_not_write(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    record = save(engine, book, intent(engine, book), uuid4())
    before = database_snapshot(engine)
    with (
        patch.object(current_service, "history", side_effect=SQLAlchemyError("synthetic private")),
        pytest.raises(writer.ProjectionWriteError) as error,
    ):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert str(error.value) == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    with (
        patch.object(
            writer,
            "_check_time",
            side_effect=writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"),
        ),
        pytest.raises(writer.ProjectionWriteError) as time_error,
    ):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert str(time_error.value) == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before
