"""A projection writer must roll back caught late changes to its recorded scope."""

from contextlib import nullcontext
from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_projection_current as current
import spm_api.dividend_projection_writer as writer
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_projection_writer import intent, prepare, settings
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("change", ["account", "broker", "portfolio", "trade_audit"])
@pytest.mark.parametrize("catch_inside", [False, True])
async def test_late_scope_change_rolls_back_even_if_the_writer_error_is_caught(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    change: str,
    catch_inside: bool,
) -> None:
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    projection, request = intent(engine, book, tax, fee), uuid4()
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into audit_events" not in " ".join(statement.lower().split()):
            return
        found = connection.exec_driver_sql(
            "SELECT 1 FROM audit_events WHERE entity_type='dividend_projection_intent' "
            "AND entity_id=?",
            (str(projection.id),),
        ).first()
        if found is None or injected:
            return
        injected.append(change)
        if change == "trade_audit":
            connection.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,owner_id,entity_type,entity_id,action,before_sha256,after_sha256,"
                "reason,request_id,created_at FROM audit_events "
                "WHERE entity_type='trade' AND action='CREATE' LIMIT 1",
                (str(uuid4()),),
            )
        else:
            table, column, identifier = {
                "account": ("accounts", "version", book["account_id"]),
                "broker": ("brokers", "version", book["broker_id"]),
                "portfolio": ("portfolios", "data_revision", book["portfolio_id"]),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (str(identifier),)
            )

    sqlalchemy_event.listen(engine, "after_cursor_execute", inject)
    try:
        outer = nullcontext() if catch_inside else pytest.raises(writer.ProjectionWriteError)
        inner = pytest.raises(writer.ProjectionWriteError) if catch_inside else nullcontext()
        with outer, unit_of_work(engine, write=True) as con, inner:
            writer.save(con, book["owner"], projection, request_id=request)
    finally:
        sqlalchemy_event.remove(engine, "after_cursor_execute", inject)
    assert injected == [change]
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        saved = writer.save(con, book["owner"], projection, request_id=request)
    assert saved.version.id == projection.id
    after = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert writer.save(con, book["owner"], projection, request_id=request) == saved
    assert database_snapshot(engine) == after


async def test_substituted_final_projection_check_rolls_back_the_writer_savepoint(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    projection, request = intent(engine, book, tax, fee), uuid4()
    original = current.require_current
    before = database_snapshot(engine)

    def substituted(*args: Any, **kwargs: Any) -> Any:
        record = original(*args, **kwargs)
        return replace(record, account_version=record.account_version + 1)

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(current, "require_current", side_effect=substituted),
        pytest.raises(
            writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
        ),
    ):
        writer.save(con, book["owner"], projection, request_id=request)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        retry = writer.save(con, book["owner"], projection, request_id=request)
    assert retry.version.id == projection.id
