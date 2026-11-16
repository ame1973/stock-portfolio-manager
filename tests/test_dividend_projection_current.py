"""A historical projection replay is not a current-use decision."""

from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_projection_current as current_service
import spm_api.dividend_projection_writer as writer
import spm_api.dividend_rejection_writer as rejection
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


def current(engine: Engine, book: dict[str, Any], record: Any, *, as_of: date) -> Any:
    with unit_of_work(engine, write=True) as con:
        return current_service.require_current(
            con,
            book["owner"],
            book["event"].event_id,
            book["account_id"],
            projection_id=record.version.id,
            projection_no=record.version.projection_no,
            as_of=as_of,
        )


async def test_current_fence_rechecks_recorded_inputs_without_writes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    record = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    before = database_snapshot(engine)
    assert current(engine, book, record, as_of=date(2026, 1, 8)) == record
    assert record.history_completeness == "UNVERIFIED"
    assert database_snapshot(engine) == before
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_AS_OF_CHANGED$"):
        current(engine, book, record, as_of=date(2026, 1, 9))
    assert database_snapshot(engine) == before


async def test_historical_replay_cannot_restore_current_after_ledger_changes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    value, request = intent(engine, book, tax_setting, fee_setting), uuid4()
    record = save(engine, book, value, request)
    await create(client, "trades", trade_body(graph, trade_date="2026-01-06", quantity="1"))
    before = database_snapshot(engine)
    assert save(engine, book, value, request) == record
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_INPUT_CHANGED$"):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before


async def test_current_fence_recomputes_shares_even_if_data_revision_is_unchanged(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    record = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    original_get = eligibility.get

    def altered(*args: Any, **kwargs: Any) -> Any:
        observed = original_get(*args, **kwargs)
        assert observed is not None
        accounts = tuple(
            replace(item, recorded_quantity="11.00000000")
            if item.account_id == str(book["account_id"])
            else item
            for item in observed.accounts
        )
        return replace(observed, accounts=accounts)

    before = database_snapshot(engine)
    with (
        patch.object(eligibility, "get", side_effect=altered),
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_STALE$"),
    ):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before


async def test_current_fence_rejects_late_evidence_and_rejection_without_writes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    record = save(engine, book, value, uuid4())
    original_get = eligibility.get

    def later(*args: Any, **kwargs: Any) -> Any:
        observed = original_get(*args, **kwargs)
        assert observed is not None
        return replace(observed, latest_input_at=record.created_at + timedelta(microseconds=1))

    before = database_snapshot(engine)
    with (
        patch.object(eligibility, "get", side_effect=later),
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_STALE$"),
    ):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        rejection.save(con, book["owner"], rejection_intent(book["event"]), request_id=uuid4())
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_REVISION_REJECTED$"):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before


async def test_current_fence_requires_the_exact_latest_projection_head(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    first = save(engine, book, intent(engine, book), uuid4())
    second = save(
        engine,
        book,
        intent(
            engine,
            book,
            expected_projection_no=first.version.projection_no,
            expected_projection_id=first.version.id,
        ),
        uuid4(),
    )
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_VERSION_CONFLICT$"):
        current(engine, book, first, as_of=date(2026, 1, 8))
    assert current(engine, book, second, as_of=date(2026, 1, 8)) == second
    with (
        unit_of_work(engine) as con,
        pytest.raises(writer.ProjectionWriteError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        current_service.require_current(
            con,
            book["owner"],
            book["event"].event_id,
            book["account_id"],
            projection_id=UUID(str(second.version.id)),
            projection_no=second.version.projection_no,
            as_of=date(2026, 1, 8),
        )
    assert database_snapshot(engine) == before
