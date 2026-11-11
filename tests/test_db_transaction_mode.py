"""Transaction modes are pinned to the actual SQLite transaction in isolated databases."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import Engine

from spm_api.db import unit_of_work


@pytest.fixture(autouse=True)
def private_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("write", [False, True])
def test_mode_cannot_change_during_a_unit_of_work(engine: Engine, write: bool) -> None:
    with unit_of_work(engine, write=write) as con:
        assert con.get_execution_options()["writer"] is write
        con.execution_options(writer=write, test_marker="allowed")
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            con.execution_options(writer=not write, rejected_marker="uncommitted")
        options = con.get_execution_options()
        assert options["writer"] is write
        assert options["test_marker"] == "allowed"
        assert "rejected_marker" not in options
        assert con.exec_driver_sql("SELECT 1").scalar_one() == 1


@pytest.mark.parametrize("write", [False, True])
def test_nested_savepoint_cannot_change_outer_mode(engine: Engine, write: bool) -> None:
    with unit_of_work(engine, write=write) as con:
        with con.begin_nested():
            con.execution_options(writer=write)
            with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
                con.execution_options(writer=not write)
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            con.execution_options(writer=not write)
        assert con.get_execution_options()["writer"] is write


def test_autobegin_is_a_read_transaction_until_it_ends(engine: Engine) -> None:
    with engine.connect() as con:
        assert "writer" not in con.get_execution_options()
        con.execution_options(test_marker="allowed")
        assert con.exec_driver_sql("SELECT 1").scalar_one() == 1
        assert con.in_transaction()
        con.execution_options(test_marker="still allowed", writer=False)
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            con.execution_options(writer=True)
        assert con.get_execution_options()["writer"] is False


@pytest.mark.parametrize("write", [False, True])
def test_derived_engine_inherits_frozen_mode(engine: Engine, write: bool) -> None:
    with engine.execution_options(writer=write).connect() as con:
        con.exec_driver_sql("SELECT 1")
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            con.execution_options(writer=not write)
        assert con.get_execution_options()["writer"] is write


def test_changing_engine_defaults_does_not_promote_an_existing_snapshot(engine: Engine) -> None:
    with engine.connect() as reader:
        reader.exec_driver_sql("SELECT 1")
        engine.update_execution_options(writer=True)
        assert "writer" not in reader.get_execution_options()
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            reader.execution_options(writer=True)
        assert "writer" not in reader.get_execution_options()
    with engine.connect() as writer:
        assert writer.get_execution_options()["writer"] is True
        assert writer.exec_driver_sql("SELECT 1").scalar_one() == 1


@pytest.mark.parametrize("bad_mode", [None, 0, 1, "true", [], {}])
def test_writer_mode_must_be_boolean_without_mutating_options(
    engine: Engine, bad_mode: object
) -> None:
    with engine.connect() as con:
        with pytest.raises(ValueError, match="^INVALID_TRANSACTION_MODE$"):
            con.execution_options(writer=bad_mode, rejected_marker="uncommitted")
        assert "writer" not in con.get_execution_options()
        assert "rejected_marker" not in con.get_execution_options()
        con.exec_driver_sql("SELECT 1")
        with pytest.raises(ValueError, match="^INVALID_TRANSACTION_MODE$"):
            con.execution_options(writer=bad_mode)
        assert "writer" not in con.get_execution_options()


@pytest.mark.parametrize("bad_mode", [None, 0, 1, "true", [], {}])
def test_invalid_engine_default_cannot_start_a_transaction(
    engine: Engine, bad_mode: object
) -> None:
    with (
        engine.execution_options(writer=bad_mode).connect() as con,
        pytest.raises(ValueError, match="^INVALID_TRANSACTION_MODE$"),
    ):
        con.exec_driver_sql("SELECT 1")


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_mode_can_change_after_commit_or_rollback(engine: Engine, end: str) -> None:
    with engine.connect() as con:
        for write in (False, True, False):
            con.execution_options(writer=write)
            assert con.get_execution_options()["writer"] is write
            con.exec_driver_sql("SELECT 1")
            assert con.in_transaction()
            getattr(con, end)()
            assert not con.in_transaction()


def test_actual_writer_holds_immediate_lock_until_commit(engine: Engine) -> None:
    assert engine.url.database is not None
    with closing(sqlite3.connect(engine.url.database, timeout=0, isolation_level=None)) as other:
        with (
            unit_of_work(engine, write=True),
            pytest.raises(sqlite3.OperationalError, match="database is locked"),
        ):
            other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")


def test_stale_read_snapshot_cannot_be_mislabeled_as_writer(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("CREATE TABLE synthetic_snapshot (value INTEGER NOT NULL)")
    with unit_of_work(engine) as reader:
        assert reader.exec_driver_sql("SELECT count(*) FROM synthetic_snapshot").scalar_one() == 0
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            reader.execution_options(writer=True)
        assert reader.get_execution_options()["writer"] is False
        with unit_of_work(engine, write=True) as writer:
            writer.exec_driver_sql("INSERT INTO synthetic_snapshot VALUES (1)")
        assert reader.exec_driver_sql("SELECT count(*) FROM synthetic_snapshot").scalar_one() == 0
        assert reader.get_execution_options()["writer"] is False
    with unit_of_work(engine) as reader:
        assert reader.exec_driver_sql("SELECT count(*) FROM synthetic_snapshot").scalar_one() == 1
