"""Recorded eligibility fails closed if its own connection changes during the read."""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_projection_writer as projection_writer
from spm_api.db import savepoint, unit_of_work
from spm_api.ledger_service import history_events
from tests.helpers import database_snapshot
from tests.test_dividend_eligibility_service import arguments
from tests.test_dividend_projection_writer import intent, prepare, save
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


class Rollback(Exception):
    """Abort a synthetic write within a disposable test transaction."""


@pytest.mark.parametrize("point", ["_accounts", "history_events"])
@pytest.mark.parametrize("rolled_back", [False, True])
async def test_same_connection_write_during_eligibility_read_fails_closed(
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    rolled_back: bool,
) -> None:
    original = getattr(eligibility, point)
    injected: list[str] = []

    def changing_read(con: Connection, *args: Any, **kwargs: Any) -> Any:
        result = original(con, *args, **kwargs)
        injected.append(point)
        context = pytest.raises(Rollback) if rolled_back else nullcontext()
        with context, savepoint(con):
            assert (
                con.exec_driver_sql(
                    "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                    (graph["portfolio"]["id"],),
                ).rowcount
                == 1
            )
            if rolled_back:
                raise Rollback
        return result

    monkeypatch.setattr(eligibility, point, changing_read)
    before = database_snapshot(engine)
    with (
        pytest.raises(
            eligibility.EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"
        ),
        unit_of_work(engine, write=True) as con,
    ):
        eligibility.get(con, **arguments(graph))
    assert injected == [point]
    assert database_snapshot(engine) == before


async def test_write_during_missing_portfolio_lookup_does_not_return_clean_absence(
    engine: Engine, graph: dict[str, Any]
) -> None:
    injected: list[bool] = []
    before = database_snapshot(engine)

    def change_after_select(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if injected or "select * from portfolios where id=" not in statement.lower():
            return
        injected.append(True)
        assert (
            connection.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                (graph["portfolio"]["id"],),
            ).rowcount
            == 1
        )

    sqlalchemy_event.listen(engine, "after_cursor_execute", change_after_select)
    try:
        with (
            pytest.raises(
                eligibility.EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"
            ),
            unit_of_work(engine, write=True) as con,
        ):
            eligibility.get(con, **arguments(graph, portfolio_id=uuid4()))
    finally:
        sqlalchemy_event.remove(engine, "after_cursor_execute", change_after_select)
    assert injected == [True]
    assert database_snapshot(engine) == before


async def test_projection_writer_rolls_back_when_eligibility_changes_during_read(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = await prepare(client, engine, graph)
    proposed = intent(engine, book)
    before = database_snapshot(engine)
    injected: list[bool] = []

    def changing_history(con: Connection, portfolio: str) -> Any:
        result = history_events(con, portfolio)
        injected.append(True)
        assert (
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                (graph["portfolio"]["id"],),
            ).rowcount
            == 1
        )
        return result

    monkeypatch.setattr(eligibility, "history_events", changing_history)
    with pytest.raises(
        projection_writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
    ):
        save(engine, book, proposed, uuid4())
    assert injected == [True]
    assert database_snapshot(engine) == before


async def test_unrelated_write_before_eligibility_read_is_allowed(
    engine: Engine, graph: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        prior_revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (graph["portfolio"]["id"],)
        ).scalar_one()
        assert (
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                (graph["portfolio"]["id"],),
            ).rowcount
            == 1
        )
        book = eligibility.get(con, **arguments(graph))
        assert book is not None
        assert book.data_revision == prior_revision + 1
        raise Rollback
    assert database_snapshot(engine) == before


async def test_unsupported_connection_fails_closed_before_any_read(graph: dict[str, Any]) -> None:
    fake = SimpleNamespace(
        in_transaction=lambda: True, connection=SimpleNamespace(driver_connection=object())
    )
    with pytest.raises(
        eligibility.EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"
    ):
        eligibility.get(cast(Connection, fake), **arguments(graph))
