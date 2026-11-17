"""A receipt writer must recheck account pins after its own transaction callbacks."""

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_writer as writer
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import rebase_cash_changes
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_receipt_audit import scope as scope
from tests.test_dividend_receipt_writer import intent, save


def test_account_move_during_receipt_rebase_cannot_escape_original_scope(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con, "brokers", {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)}
        )
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        rebase_cash_changes(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), str(value.account_id)),
        )

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "rebase_cash_changes", side_effect=changed),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_SCOPE_CHANGED$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


def test_account_change_during_invalidation_cannot_escape_original_scope(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(value.account_id),)
        )

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "invalidate_portfolio", side_effect=changed),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_SCOPE_CHANGED$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


def test_wrong_data_revision_increment_aborts_every_receipt_fact(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
            (str(value.portfolio_id),),
        )

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "invalidate_portfolio", side_effect=changed),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_SCOPE_CHANGED$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("account_version", True),
        ("portfolio_version", 1.0),
        ("portfolio_version", 0),
        ("broker_version", 1.0),
        ("data_revision", False),
        ("data_revision", 0.0),
        ("data_revision", INT64_MAX + 1),
    ],
)
def test_malformed_numeric_scope_never_compares_equal_to_an_integer_pin(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], field: str, replacement: object
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "SELECT a.portfolio_id,a.broker_id,a.version AS account_version,a.status" not in str(
            statement
        ):
            return result
        row = dict(result.mappings().one())
        row[field] = replacement
        replacement_result = MagicMock()
        replacement_result.mappings.return_value.first.return_value = row
        return replacement_result

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(Connection, "execute", injected),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


def test_last_advanceable_data_revision_stays_an_integer(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    current = intent(engine, scope)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?",
            (INT64_MAX - 1, str(current.portfolio_id)),
        )
    value = intent(engine, scope)
    assert save(engine, value, uuid4()).receipt.id == value.id
    with unit_of_work(engine) as con:
        actual = con.exec_driver_sql(
            "SELECT data_revision,typeof(data_revision) FROM portfolios WHERE id=?",
            (str(value.portfolio_id),),
        ).one()
    assert tuple(actual) == (INT64_MAX, "integer")


def test_historical_replay_after_account_move_does_not_repost_or_require_old_scope(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    original = save(engine, value, request)
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con, "brokers", {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)}
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), str(value.account_id)),
        )
    before = database_snapshot(engine)
    assert save(engine, value, request) == original
    assert database_snapshot(engine) == before
