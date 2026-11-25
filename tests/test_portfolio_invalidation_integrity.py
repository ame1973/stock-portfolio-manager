"""Portfolio invalidation must never silently lose its revision or durable marker."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.master_service as master
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.errors import ApiError
from spm_api.ledger_service import post_cash
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import owned_account


@pytest.mark.parametrize(
    "problem",
    [
        "overflow",
        "fractional",
        "ignored-update",
        "ignored-marker",
        "altered-marker",
        "altered-revision",
    ],
)
def test_portfolio_invalidation_fails_closed_when_it_cannot_record_both_effects(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    portfolio, _ = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        if problem == "overflow":
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=? WHERE id=?", (INT64_MAX, portfolio)
            )
        elif problem == "fractional":
            con.exec_driver_sql("UPDATE portfolios SET data_revision=1.5 WHERE id=?", (portfolio,))
        elif problem == "ignored-update":
            con.exec_driver_sql(
                "CREATE TRIGGER synthetic_ignore_portfolio_revision BEFORE UPDATE OF data_revision "
                "ON portfolios WHEN NEW.id='" + str(UUID(portfolio)) + "' "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        elif problem == "ignored-marker":
            con.exec_driver_sql(
                "CREATE TRIGGER synthetic_ignore_projection_invalidation "
                "BEFORE INSERT ON projection_invalidations "
                "WHEN NEW.source_id='synthetic-invalidation' BEGIN SELECT RAISE(IGNORE); END"
            )
        elif problem == "altered-marker":
            con.exec_driver_sql(
                "CREATE TRIGGER synthetic_alter_projection_invalidation "
                "AFTER INSERT ON projection_invalidations "
                "WHEN NEW.source_id='synthetic-invalidation' "
                "BEGIN UPDATE projection_invalidations SET reason='CHANGED' WHERE id=NEW.id; END"
            )
        else:
            con.exec_driver_sql(
                "CREATE TRIGGER synthetic_alter_revision_after_marker "
                "AFTER INSERT ON projection_invalidations "
                "WHEN NEW.source_id='synthetic-invalidation' "
                "BEGIN UPDATE portfolios SET data_revision=data_revision+1 "
                "WHERE id=NEW.portfolio_id; END"
            )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(ApiError) as raised,
    ):
        invalidate_portfolio(
            con, portfolio, "synthetic-invalidation", "SYNTHETIC_TEST", "2026-01-09"
        )
    assert raised.value.code in {
        "PORTFOLIO_REVISION_EXHAUSTED",
        "PORTFOLIO_INVALIDATION_UNAVAILABLE",
    }
    assert database_snapshot(engine) == before


def test_last_revision_increments_exactly_and_retains_one_unresolved_marker(
    engine: Engine, source: dict[str, Any]
) -> None:
    portfolio, _ = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?", (INT64_MAX - 1, portfolio)
        )
        invalidate_portfolio(con, portfolio, "synthetic-source", "SYNTHETIC_TEST", "2026-01-09")
    with unit_of_work(engine) as con:
        revision = con.exec_driver_sql(
            "SELECT data_revision,typeof(data_revision) FROM portfolios WHERE id=?", (portfolio,)
        ).one()
        markers = con.exec_driver_sql(
            "SELECT portfolio_id,from_date,reason,source_id,resolved_at "
            "FROM projection_invalidations WHERE source_id='synthetic-source'"
        ).all()
    assert tuple(revision) == (INT64_MAX, "integer")
    assert [tuple(row) for row in markers] == [
        (portfolio, "2026-01-09", "SYNTHETIC_TEST", "synthetic-source", None)
    ]


def test_invalidation_returns_the_exact_persisted_marker(
    engine: Engine, source: dict[str, Any]
) -> None:
    portfolio, _ = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        marker = invalidate_portfolio(
            con, portfolio, "synthetic-source", "SYNTHETIC_TEST", "2026-01-09"
        )
        assert isinstance(marker, tuple) and len(marker) == 7
        persisted = con.exec_driver_sql(
            "SELECT id,portfolio_id,from_date,reason,source_id,created_at,resolved_at "
            "FROM projection_invalidations WHERE id=?",
            (marker[0],),
        ).one()
        assert tuple(persisted) == marker
        assert marker[1:5] == (portfolio, "2026-01-09", "SYNTHETIC_TEST", "synthetic-source")
        assert marker[-1] is None


def test_invalidation_without_a_day_uses_the_earliest_recorded_cash_or_today(
    engine: Engine, source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> "FixedDate":
            return cls(2026, 11, 25)

    monkeypatch.setattr(master, "date", FixedDate)
    portfolio, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        invalidate_portfolio(con, portfolio, "synthetic-empty", "SYNTHETIC_TEST")
        post_cash(
            con,
            account=account,
            day="2020-01-02",
            currency="USD",
            amount=1_000_000,
            fx=None,
            booked=None,
            kind="ADJUSTMENT",
            source_type="SYNTHETIC",
            source_id=str(uuid4()),
        )
        invalidate_portfolio(con, portfolio, "synthetic-historical", "SYNTHETIC_TEST")
    with unit_of_work(engine) as con:
        rows = {
            str(row.source_id): str(row.from_date)
            for row in con.exec_driver_sql(
                "SELECT source_id,from_date FROM projection_invalidations WHERE portfolio_id=?",
                (portfolio,),
            )
        }
    assert rows == {
        "synthetic-empty": "2026-11-25",
        "synthetic-historical": "2020-01-02",
    }


def test_invalidation_requires_a_live_writer_and_existing_portfolio(
    engine: Engine, source: dict[str, Any]
) -> None:
    portfolio, _ = owned_account(engine)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(ApiError, match="Portfolio invalidation unavailable") as read_error,
    ):
        invalidate_portfolio(con, portfolio, "synthetic", "SYNTHETIC_TEST", "2026-01-09")
    assert read_error.value.code == "PORTFOLIO_INVALIDATION_UNAVAILABLE"
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(ApiError, match="Portfolio invalidation unavailable") as missing_error,
    ):
        invalidate_portfolio(con, str(uuid4()), "synthetic", "SYNTHETIC_TEST", "2026-01-09")
    assert missing_error.value.code == "PORTFOLIO_INVALIDATION_UNAVAILABLE"
    assert database_snapshot(engine) == before


def test_database_abort_remains_an_error_and_does_not_commit_the_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    portfolio, _ = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_abort_projection_invalidation "
            "BEFORE INSERT ON projection_invalidations "
            "WHEN NEW.source_id='synthetic-abort' BEGIN SELECT RAISE(ABORT,'SYNTHETIC_ABORT'); END"
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(IntegrityError, match="SYNTHETIC_ABORT"),
    ):
        invalidate_portfolio(con, portfolio, "synthetic-abort", "SYNTHETIC_TEST", "2026-01-09")
    assert database_snapshot(engine) == before


def test_competing_invalidations_advance_twice_and_retain_both_markers(
    engine: Engine, source: dict[str, Any]
) -> None:
    portfolio, _ = owned_account(engine)

    def invalidate(source_id: str) -> None:
        with unit_of_work(engine, write=True) as con:
            invalidate_portfolio(con, portfolio, source_id, "SYNTHETIC_TEST", "2026-01-09")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invalidate, "synthetic-first")
        second = pool.submit(invalidate, "synthetic-second")
        first.result(timeout=30)
        second.result(timeout=30)
    with unit_of_work(engine) as con:
        revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (portfolio,)
        ).scalar_one()
        rows = (
            con.exec_driver_sql(
                "SELECT source_id FROM projection_invalidations "
                "WHERE portfolio_id=? ORDER BY source_id",
                (portfolio,),
            )
            .scalars()
            .all()
        )
    assert revision == 2
    assert list(rows) == ["synthetic-first", "synthetic-second"]
