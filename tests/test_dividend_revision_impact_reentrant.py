"""Detect writes while a recorded revision impact is being classified."""

from contextlib import nullcontext
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_revision_impact as impact
import spm_api.dividend_revision_rebuild as rebuild
from spm_api.audit import audit
from spm_api.db import savepoint, unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("changed_table", ["account", "broker", "portfolio", "security"])
@pytest.mark.parametrize("rolled_back", [False, True])
@pytest.mark.parametrize("catch_inside", [False, True])
async def test_impact_detects_changes_during_its_last_portfolio_scan(
    engine: Engine,
    scope: dict[str, Any],
    changed_table: str,
    rolled_back: bool,
    catch_inside: bool,
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    before = database_snapshot(engine)
    injected: list[str] = []

    def change_during_read(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        sql = " ".join(statement.lower().split())
        if injected or "select p.id from portfolios p where p.owner_id=" not in sql:
            return
        injected.append(changed_table)
        table, column, identifier = {
            "account": ("accounts", "version", book["account_id"]),
            "broker": ("brokers", "version", book["broker_id"]),
            "portfolio": ("portfolios", "data_revision", book["portfolio_id"]),
            "security": ("securities", "version", observed.security.id),
        }[changed_table]

        def change() -> None:
            assert (
                connection.exec_driver_sql(
                    f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (str(identifier),)
                ).rowcount
                == 1
            )

        if rolled_back:
            with pytest.raises(Rollback), savepoint(connection):
                change()
                raise Rollback
        else:
            change()

    sqlalchemy_event.listen(engine, "after_cursor_execute", change_during_read)
    try:
        outer = (
            nullcontext()
            if catch_inside
            else pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$")
        )
        inner = (
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$")
            if catch_inside
            else nullcontext()
        )
        with outer, unit_of_work(engine, write=True) as con, inner, savepoint(con):
            impact.inspect(con, book["owner"], scope["decision"], scope["proposal"], observed)
            assert injected == [changed_table]
    finally:
        sqlalchemy_event.remove(engine, "after_cursor_execute", change_during_read)
    assert injected == [changed_table]
    assert database_snapshot(engine) == before


async def test_impact_allows_an_earlier_unrelated_write(
    engine: Engine, scope: dict[str, Any]
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "synthetic_unrelated",
            str(uuid4()),
            "SYNTHETIC_UNRELATED_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        result = impact.inspect(con, book["owner"], scope["decision"], scope["proposal"], observed)
        assert result.scope == "RECORDED_REVISION_IMPACT_ONLY"
        assert result.eligibility_scope == result.payment_scope == "NOT_ESTABLISHED"
        raise Rollback
    assert database_snapshot(engine) == before
