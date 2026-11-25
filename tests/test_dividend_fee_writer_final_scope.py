"""Recheck a fee decision after its invalidation and before releasing its savepoint."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as writer
import spm_api.dividend_rejection_writer as rejections
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_fee_writer import intent
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_tax_service import book as book


@pytest.mark.parametrize("catch_inside", [False, True])
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("account", "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("broker", "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("portfolio", "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("data_revision", "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("owner", "DIVIDEND_FEE_OWNER_UNAVAILABLE"),
        ("rejection", "DIVIDEND_REVISION_REJECTED"),
        ("audit", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_deleted", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_duplicate", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_date", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_reason", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_source", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_time", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("marker_canonical_time", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
    ],
)
def test_late_setting_scope_changes_cannot_commit_even_when_caught(
    engine: Engine, book: dict[str, Any], change: str, code: str, catch_inside: bool
) -> None:
    chosen, request = intent(book), uuid4()
    before = database_snapshot(engine)
    injected: list[str] = []

    def change_after_invalidation(con: Connection, *args: Any, **kwargs: Any) -> Any:
        marker = invalidate_portfolio(con, *args, **kwargs)
        injected.append(change)
        if change in {"account", "broker", "portfolio", "data_revision"}:
            table, column, identifier = {
                "account": ("accounts", "version", book["account_id"]),
                "broker": ("brokers", "version", book["broker_id"]),
                "portfolio": ("portfolios", "version", book["portfolio_id"]),
                "data_revision": ("portfolios", "data_revision", book["portfolio_id"]),
            }[change]
            con.exec_driver_sql(
                f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (str(identifier),)
            )
        elif change == "owner":
            con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
        elif change == "rejection":
            rejections.save(con, OWNER, rejection_intent(book["event"]), request_id=uuid4())
        elif change == "audit":
            con.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,owner_id,entity_type,entity_id,action,before_sha256,after_sha256,"
                "reason,request_id,created_at FROM audit_events "
                "WHERE entity_type='dividend_fee_setting' AND entity_id=?",
                (str(uuid4()), str(chosen.id)),
            )
        elif change == "marker_deleted":
            changed = con.exec_driver_sql(
                "DELETE FROM projection_invalidations WHERE source_id=?", (str(chosen.id),)
            )
            assert changed.rowcount == 1
        elif change == "marker_duplicate":
            changed = con.exec_driver_sql(
                "INSERT INTO projection_invalidations(id,portfolio_id,from_date,reason,source_id,"
                "created_at,resolved_at) "
                "SELECT ?,portfolio_id,from_date,reason,source_id,created_at,resolved_at "
                "FROM projection_invalidations WHERE source_id=?",
                (str(uuid4()), str(chosen.id)),
            )
            assert changed.rowcount == 1
        else:
            column, value = {
                "marker": ("resolved_at", stamp()),
                "marker_date": ("from_date", "2026-01-02"),
                "marker_reason": ("reason", "SYNTHETIC_CHANGED"),
                "marker_source": ("source_id", str(uuid4())),
                "marker_time": ("created_at", "invalid-private"),
                "marker_canonical_time": ("created_at", "2000-01-01T00:00:00.000000Z"),
            }[change]
            changed_count = con.exec_driver_sql(
                f"UPDATE projection_invalidations SET {column}=? WHERE source_id=?",
                (value, str(chosen.id)),
            ).rowcount
            assert changed_count == 1
        return marker

    with patch.object(writer, "invalidate_portfolio", side_effect=change_after_invalidation):
        outer = nullcontext() if catch_inside else pytest.raises(fees.FeeSettingError, match=code)
        inner = pytest.raises(fees.FeeSettingError, match=code) if catch_inside else nullcontext()
        with outer, unit_of_work(engine, write=True) as con, inner:
            writer.save(con, OWNER, chosen, request_id=request)
            assert injected == [change]
    assert injected == [change]
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        saved = writer.save(con, OWNER, chosen, request_id=request)
    after = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert writer.save(con, OWNER, chosen, request_id=request) == saved
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_no", 1.0),
        ("setting_no", True),
        ("account_version", 1.0),
        ("broker_version", True),
        ("unrecognized", "private"),
        ("fee", "2.000000"),
        ("request_id", uuid4()),
    ],
)
def test_substituted_fee_record_cannot_impersonate_the_saved_scope(
    engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    chosen, request = intent(book), uuid4()
    before = database_snapshot(engine)
    original = writer.require_current

    def substitute(*args: Any, **kwargs: Any) -> Any:
        selected = original(*args, **kwargs)
        assert selected.selected is not None
        return replace(
            selected,
            selected=selected.selected.model_copy(update={field: value}),
        )

    with (
        patch.object(writer, "require_current", side_effect=substitute),
        pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"),
        unit_of_work(engine, write=True) as con,
    ):
        writer.save(con, OWNER, chosen, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("version", "revision"),
    [
        (0, 0),
        (True, 0),
        (1.0, 0),
        (2**63, 0),
        (1, -1),
        (1, True),
        (1, 0.0),
        (1, 2**63),
    ],
)
def test_portfolio_scope_rejects_malformed_exact_integers(version: Any, revision: Any) -> None:
    con = Mock(spec=Connection)
    con.execute.return_value.one_or_none.return_value = SimpleNamespace(
        version=version, data_revision=revision
    )
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        writer._portfolio_scope(con, OWNER, uuid4())


def test_portfolio_scope_rejects_missing_owned_portfolio() -> None:
    con = Mock(spec=Connection)
    con.execute.return_value.one_or_none.return_value = None
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SCOPE_CHANGED$"):
        writer._portfolio_scope(con, OWNER, uuid4())
