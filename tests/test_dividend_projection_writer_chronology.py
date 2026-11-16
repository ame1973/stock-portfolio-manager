"""A projection cannot predate the recorded facts it consumes."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_projection_writer as writer
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from tests.helpers import create, database_snapshot
from tests.test_dividend_projection_writer import intent, prepare
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


async def test_projection_clock_cannot_precede_consumed_trade_audit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    with unit_of_work(engine) as con:
        text = con.exec_driver_sql(
            "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision' "
            "AND entity_id=? AND action='DIVIDEND_EVENT_REVISED'",
            (str(book["event"].revision_id),),
        ).scalar_one()
    earlier = datetime.fromisoformat(text.replace("Z", "+00:00")) + timedelta(microseconds=1)
    await create(client, "trades", trade_body(graph, trade_date="2026-01-07"))
    value = intent(engine, book)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def omit_invalidations(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "SELECT created_at FROM projection_invalidations WHERE portfolio_id" in str(
                statement
            ):
                return iter(())
            return execute(statement, *args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=omit_invalidations),
            patch.object(writer, "now", return_value=earlier),
            pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_TIME_CONFLICT$"),
        ):
            writer.save(con, book["owner"], value, request_id=uuid4())
    assert database_snapshot(engine) == before


async def test_projection_clock_cannot_precede_the_observed_portfolio_revision(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    with unit_of_work(engine) as con:
        timestamp = con.exec_driver_sql(
            "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision' "
            "AND entity_id=? AND action='DIVIDEND_EVENT_REVISED'",
            (str(book["event"].revision_id),),
        ).scalar_one()
    earlier = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) + timedelta(microseconds=1)
    with unit_of_work(engine, write=True) as con:
        invalidate_portfolio(
            con, str(book["portfolio_id"]), str(uuid4()), "SYNTHETIC", "2026-01-01"
        )
    value = intent(engine, book)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "now", return_value=earlier),
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_TIME_CONFLICT$"),
    ):
        writer.save(con, book["owner"], value, request_id=uuid4())
    assert database_snapshot(engine) == before
