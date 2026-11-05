"""Action/opening upgrades preserve original facts and reject ambiguous or inconsistent lineage."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from spm_api.ledger_service import allocate_sequence, post_cash, reverse_cash
from tests.helpers import database_snapshot as snapshot
from tests.test_transfer_migration import NOW, dimensions, insert, seed_buy


def action(con: Connection, identifier: str, **changes: Any) -> None:
    values: dict[str, Any] = {
        "id": identifier,
        "root_id": identifier,
        "security_id": "stock",
        "action_type": "SPLIT",
        "effective_date": "2026-01-03",
        "ratio_num": 3,
        "ratio_den": 2,
        "status": "CONFIRMED",
        "created_at": NOW,
        **changes,
    }
    if "created_sequence" not in values:
        root = values["root_id"]
        existing = con.exec_driver_sql(
            "SELECT sequence FROM ledger_sequences WHERE kind='ACTION' AND fact_id=?", (root,)
        ).scalar()
        values["created_sequence"] = (
            existing if existing is not None else allocate_sequence(con, "ACTION", root)
        )
    insert(con, "corporate_actions", values)


def opening(con: Connection) -> str:
    con.exec_driver_sql(
        "UPDATE accounts SET opening_cash_micro=1000000000,opening_cash_date='2026-01-10',"
        "cash_completeness='COMPLETE' WHERE id='A'"
    )
    return post_cash(
        con,
        account="A",
        day="2026-01-10",
        currency="HKD",
        amount=1000000000,
        fx=1000000000000,
        booked=1000000000,
        kind="ADJUSTMENT",
        source_type="OPENING_BALANCE",
        source_id="A",
    )


def correction(con: Connection, parent: str, **changes: Any) -> str:
    values: dict[str, Any] = {
        "account": "A",
        "day": "2026-01-15",
        "currency": "HKD",
        "amount": 2000000000,
        "fx": 1000000000000,
        "booked": 2000000000,
        "kind": "ADJUSTMENT",
        "source_type": "OPENING_BALANCE_CORRECTION",
        "source_id": parent,
        **changes,
    }
    identifier = str(uuid4())
    con.exec_driver_sql(
        "INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,"
        "fx_rate_hkd_e12,booked_hkd_micro,kind,source_type,source_id,external_flow,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identifier,
            values["account"],
            values["day"],
            values["currency"],
            values["amount"],
            values["fx"],
            values["booked"],
            values["kind"],
            values["source_type"],
            values["source_id"],
            values.get("external", False),
            NOW,
        ),
    )
    return identifier


def schema(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as con:
        return [
            tuple(row)
            for row in con.exec_driver_sql(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
            )
        ]


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0006_transfers")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
            opening(con)
            post_cash(
                con,
                account="B",
                day="2026-01-02",
                currency="USD",
                amount=1000001,
                fx=None,
                booked=None,
                kind="DEPOSIT",
                source_type="MANUAL_CASH",
                source_id="retained-native-only",
                external=True,
            )
        yield engine
    finally:
        engine.dispose()


def test_action_opening_upgrade_preserves_every_original_financial_column_and_unknowns(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        action(con, "split")
        action(
            con,
            "legacy-symbol",
            action_type="SYMBOL_CHANGE",
            ratio_num=None,
            ratio_den=None,
            new_symbol="OLD",
            effective_date="2026-01-04",
        )
        columns = {
            table: [row[1] for row in con.exec_driver_sql(f"PRAGMA table_info({table})")]
            for table in (
                "corporate_actions",
                "cash_ledger_entries",
                "accounts",
                "trades",
                "cash_transfers",
                "security_transfers",
                "ledger_sequences",
            )
        }
        before = {
            table: [
                tuple(row)
                for row in con.exec_driver_sql(
                    f"SELECT {','.join(names)} FROM {table} ORDER BY rowid"
                )
            ]
            for table, names in columns.items()
        }
    migrate(settings)
    with unit_of_work(old_engine) as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
        for table, names in columns.items():
            assert [
                tuple(row)
                for row in con.exec_driver_sql(
                    f"SELECT {','.join(names)} FROM {table} ORDER BY rowid"
                )
            ] == before[table]
        assert [
            tuple(row)
            for row in con.exec_driver_sql(
                "SELECT reason,note,previous_symbol FROM corporate_actions ORDER BY id"
            )
        ] == [("", "", None), ("", "", None)]
        assert con.exec_driver_sql("SELECT display_ticker FROM securities").scalar_one() == "STK"
        assert con.exec_driver_sql(
            "SELECT fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_id='retained-native-only'"
        ).one() == (None, None)
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_ACTIONS_OPENING'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    after = snapshot(old_engine)
    migrate(settings)
    assert snapshot(old_engine) == after


@pytest.mark.parametrize("problem", ["duplicate_identity", "late_ddl"])
def test_failed_action_opening_migration_restores_old_head_schema_and_every_value(
    old_engine: Engine, settings: Settings, problem: str
) -> None:
    with old_engine.begin() as con:
        action(con, "first")
        if problem == "duplicate_identity":
            action(con, "ambiguous")
        else:
            # Failure occurs after column/trigger changes; explicit SQLite DDL rollback is required.
            con.exec_driver_sql("CREATE INDEX idx_reviews_action_root ON review_items(status)")
    before, original_schema = snapshot(old_engine), schema(old_engine)
    with pytest.raises((RuntimeError, OperationalError)):
        migrate(settings)
    assert snapshot(old_engine) == before and schema(old_engine) == original_schema
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0006_transfers"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"ratio_num": 1, "ratio_den": 2},
        {"ratio_num": 2, "ratio_den": 2},
        {"action_type": "REVERSE_SPLIT"},
        {"ratio_num": 1.5},
        {"ratio_den": None},
        {"new_symbol": "UNRELATED"},
        {"previous_symbol": "UNRELATED"},
        {"action_type": "SYMBOL_CHANGE", "ratio_num": None, "ratio_den": None},
        {"action_type": "SYMBOL_CHANGE", "new_symbol": "NEW", "previous_symbol": "OLD"},
        {"action_type": "SYMBOL_CHANGE", "ratio_num": None, "ratio_den": None, "new_symbol": "NEW"},
        {"action_type": "CASH_IN_LIEU", "ratio_num": None, "ratio_den": None},
        {"action_type": "MERGER", "ratio_num": None, "ratio_den": None},
        {"action_type": "SPIN_OFF", "ratio_num": None, "ratio_den": None},
        {"effective_date": "2026-02-30"},
        {"revision": 2},
        {"revision": 1.5},
        {"status": "VOID"},
        {"version": 2},
        {"is_current": 0},
    ],
)
def test_action_sql_rejects_invalid_rational_terms_dates_and_initial_state(
    engine: Engine, changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        action(con, "invalid", **changes)
    assert snapshot(engine) == before


def test_action_root_security_sequence_predecessor_and_terminal_state_cannot_be_rewritten(
    engine: Engine,
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        con.exec_driver_sql(
            "INSERT INTO securities(id,exchange_code,display_ticker,name,quote_currency,"
            "created_at) VALUES ('other-stock','NASDAQ','OTHER','Other','USD',?)",
            (NOW,),
        )
        action(con, "original", reason="Synthetic adopted original terms")
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id='original'"
        )
    before = snapshot(engine)
    for changes in (
        {"security_id": "other-stock"},
        {"root_id": "different-root"},
        {"created_sequence": 999999},
        {"revision": 3},
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            action(
                con,
                "invalid",
                root_id=changes.get("root_id", "original"),
                supersedes_id="original",
                revision=changes.get("revision", 2),
                **{
                    key: value
                    for key, value in changes.items()
                    if key not in {"root_id", "revision"}
                },
            )
        assert snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        action(
            con,
            "corrected",
            root_id="original",
            supersedes_id="original",
            revision=2,
            ratio_num=2,
            ratio_den=1,
            reason="Synthetic corrected rational terms",
        )
    for sql in (
        "UPDATE corporate_actions SET reason='rewrite',version=version+1 WHERE id='corrected'",
        "UPDATE corporate_actions SET ratio_num=4,version=version+1 WHERE id='corrected'",
        "UPDATE corporate_actions SET status='NEEDS_REVIEW',version=version+1 WHERE id='corrected'",
        "UPDATE corporate_actions SET is_current=1,version=version+1 WHERE id='original'",
        "DELETE FROM corporate_actions WHERE id='corrected'",
    ):
        before = snapshot(engine)
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(sql)
        assert snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET status='VOID',version=version+1 WHERE id='corrected'"
        )
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id='corrected'"
        )
    assert snapshot(engine) == before
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        action(con, "resurrected", root_id="original", supersedes_id="corrected", revision=3)
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"account": "B"},
        {"account": "F"},
        {"source_id": "missing"},
        {"day": "2026-01-16"},
        {"currency": "USD"},
        {"amount": 1000000000},
        {"amount": 1.5},
        {"booked": None},
        {"booked": 1},
        {"fx": None},
        {"fx": 7800000000000},
        {"kind": "DEPOSIT"},
        {"external": True},
    ],
)
def test_opening_correction_sql_requires_exact_account_metadata_known_booking_and_parent(
    engine: Engine, changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        parent = opening(con)
        entry = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (parent,))
            .mappings()
            .one()
        )
        reverse_cash(con, entry, reason="Synthetic exact opening retirement")
        con.exec_driver_sql(
            "UPDATE accounts SET opening_cash_micro=2000000000,opening_cash_date='2026-01-15',"
            "version=version+1 WHERE id='A'"
        )
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        correction(con, parent, **changes)
    assert snapshot(engine) == before


def test_opening_correction_parent_must_be_retired_and_has_one_immutable_successor(
    engine: Engine,
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        parent = opening(con)
        con.exec_driver_sql(
            "UPDATE accounts SET opening_cash_micro=2000000000,opening_cash_date='2026-01-15',"
            "version=version+1 WHERE id='A'"
        )
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        correction(con, parent)
    assert snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        entry = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (parent,))
            .mappings()
            .one()
        )
        reverse_cash(con, entry, reason="Synthetic exact opening retirement")
        current = correction(con, parent)
        entry = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (current,))
            .mappings()
            .one()
        )
        reverse_cash(con, entry, reason="Synthetic cleared replacement assertion")
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        correction(con, parent)
    assert snapshot(engine) == before
    for sql in (
        "UPDATE cash_ledger_entries SET source_id='different',version=version+1 WHERE id=?",
        "UPDATE cash_ledger_entries SET amount_micro=1,version=version+1 WHERE id=?",
        "DELETE FROM cash_ledger_entries WHERE id=?",
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(sql, (current,))
        assert snapshot(engine) == before


def test_action_opening_downgrade_requires_a_validated_backup(engine: Engine) -> None:
    before, original_schema = snapshot(engine), schema(engine)
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated pre-upgrade backup"):
            command.downgrade(config, "0006_transfers")
    assert snapshot(engine) == before and schema(engine) == original_schema
