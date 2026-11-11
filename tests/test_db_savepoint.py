"""Fail-closed savepoint behavior in isolated SQLite writer transactions."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError

from spm_api.db import savepoint, unit_of_work


@pytest.fixture
def probe(engine: Engine, tmp_path: Path) -> Engine:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("CREATE TABLE savepoint_probe(value INTEGER NOT NULL)")
    return engine


def contents(engine: Engine) -> list[int]:
    with unit_of_work(engine) as con:
        return list(
            con.exec_driver_sql("SELECT value FROM savepoint_probe ORDER BY value").scalars()
        )


def test_success_and_caught_body_failure_keep_unrelated_outer_work(probe: Engine) -> None:
    with unit_of_work(probe, write=True) as con:
        con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (1)")
        with savepoint(con):
            con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (2)")
        try:
            with savepoint(con):
                con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (3)")
                raise ValueError("synthetic failure")
        except ValueError:
            pass
        con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (4)")
    assert contents(probe) == [1, 2, 4]


@pytest.mark.parametrize("phase", ["begin", "release_before", "release_after", "rollback"])
def test_uncertain_savepoint_outcome_aborts_the_entire_outer_transaction(
    probe: Engine, phase: str
) -> None:
    with unit_of_work(probe, write=True) as con:
        con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (1)")
        target: Any
        if phase == "begin":
            target, method = con, "begin_nested"
            failure: Any = SQLAlchemyError("synthetic begin failure")
        elif phase == "rollback":
            target, method = probe.dialect, "do_rollback_to_savepoint"
            failure = SQLAlchemyError("synthetic rollback failure")
        else:
            target, method = probe.dialect, "do_release_savepoint"
            release = probe.dialect.do_release_savepoint

            def failed_release(connection: Any, name: str) -> None:
                if phase == "release_after":
                    release(connection, name)
                raise SQLAlchemyError("synthetic release failure")

            failure = failed_release
        with (
            patch.object(target, method, side_effect=failure),
            pytest.raises(SQLAlchemyError),
            savepoint(con),
        ):
            con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (2)")
            if phase == "rollback":
                raise ValueError("synthetic body failure")
        assert not con.in_transaction()
        with pytest.raises(InvalidRequestError):
            con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (3)")
    assert contents(probe) == []


def test_failed_outer_rollback_invalidates_the_connection_before_it_can_commit(
    probe: Engine,
) -> None:
    with unit_of_work(probe, write=True) as con:
        con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (1)")
        with (
            patch.object(
                probe.dialect,
                "do_release_savepoint",
                side_effect=SQLAlchemyError("synthetic release failure"),
            ),
            patch.object(
                probe.dialect,
                "do_rollback",
                side_effect=SQLAlchemyError("synthetic rollback failure"),
            ),
            pytest.raises(SQLAlchemyError),
            savepoint(con),
        ):
            con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (2)")
        assert con.invalidated
    assert contents(probe) == []


def test_failed_nested_release_cannot_be_caught_to_commit_an_enclosing_savepoint(
    probe: Engine,
) -> None:
    with pytest.raises(SQLAlchemyError), unit_of_work(probe, write=True) as con:
        con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (1)")
        with savepoint(con):
            con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (2)")
            with patch.object(
                probe.dialect,
                "do_release_savepoint",
                side_effect=SQLAlchemyError("synthetic release failure"),
            ):
                try:
                    with savepoint(con):
                        con.exec_driver_sql("INSERT INTO savepoint_probe VALUES (3)")
                except SQLAlchemyError:
                    pass
    assert contents(probe) == []
