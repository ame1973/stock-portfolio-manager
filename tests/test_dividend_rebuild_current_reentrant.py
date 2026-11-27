"""Detect same-connection changes during a serialized revision observation recheck."""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api.audit import audit
from spm_api.db import savepoint, unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("changed_table", ["security", "broker"])
@pytest.mark.parametrize("rolled_back", [False, True])
@pytest.mark.parametrize("catch_inside", [False, True])
async def test_recheck_detects_same_connection_changes_during_its_last_read(
    engine: Engine,
    scope: dict[str, Any],
    changed_table: str,
    rolled_back: bool,
    catch_inside: bool,
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    fee_id = observed.projections.fee_settings[0][0]
    before = database_snapshot(engine)
    injected: list[str] = []

    def change_during_read(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if injected or "from audit_events where entity_id=" not in statement.lower():
            return
        if fee_id not in parameters:
            return
        injected.append(changed_table)
        table, identifier = (
            ("securities", observed.security.id)
            if changed_table == "security"
            else ("brokers", book["broker_id"])
        )

        def change() -> None:
            assert (
                connection.exec_driver_sql(
                    f"UPDATE {table} SET version=version+1 WHERE id=?", (str(identifier),)
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
        outer = nullcontext() if catch_inside else pytest.raises(rebuild.RebuildError)
        inner = pytest.raises(rebuild.RebuildError) if catch_inside else nullcontext()
        with outer, unit_of_work(engine, write=True) as con, inner, savepoint(con):
            current.require_current(
                con, book["owner"], scope["decision"], scope["proposal"], observed
            )
            assert injected == [changed_table]
    finally:
        sqlalchemy_event.remove(engine, "after_cursor_execute", change_during_read)
    assert injected == [changed_table]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("has_driver", [False, True])
async def test_recheck_fails_closed_for_a_non_sqlite_connection(
    engine: Engine, scope: dict[str, Any], has_driver: bool
) -> None:
    connection = SimpleNamespace(driver_connection=object()) if has_driver else object()
    fake = SimpleNamespace(
        in_transaction=lambda: True,
        get_execution_options=lambda: {"writer": True},
        connection=connection,
    )
    before = database_snapshot(engine)
    with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
        current.require_current(
            cast(Connection, fake),
            scope["book"]["owner"],
            scope["decision"],
            scope["proposal"],
            scope["observed"],
        )
    assert database_snapshot(engine) == before


async def test_recheck_allows_an_earlier_unrelated_write(
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
        result = current.require_current(
            con, book["owner"], scope["decision"], scope["proposal"], observed
        )
        assert result == observed
        raise Rollback
    assert database_snapshot(engine) == before
