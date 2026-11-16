"""Current projection checks stay serialized with competing source changes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_fee_writer as fees
import spm_api.dividend_projection_current as current_service
import spm_api.dividend_projection_writer as writer
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import REASON
from tests.test_dividend_projection_current import current
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


async def test_current_check_and_fee_change_serialize_in_separate_writers(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
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
    started = Event()

    def change() -> None:
        started.set()
        with unit_of_work(engine, write=True) as con:
            fees.save(con, book["owner"], newer, request_id=uuid4())

    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_of_work(engine, write=True) as con:
            future = pool.submit(change)
            assert started.wait(timeout=5)
            assert (
                current_service.require_current(
                    con,
                    book["owner"],
                    book["event"].event_id,
                    book["account_id"],
                    projection_id=record.version.id,
                    projection_no=record.version.projection_no,
                    as_of=date(2026, 1, 8),
                )
                == record
            )
            assert not future.done()
        future.result(timeout=10)
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_INPUT_CHANGED$"):
        current(engine, book, record, as_of=date(2026, 1, 8))
    assert database_snapshot(engine) == before
