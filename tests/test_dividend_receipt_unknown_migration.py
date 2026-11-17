"""Receipt unknowns upgrade must preserve all legacy facts, cash and safety guards."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_receipt_history as receipt_history
from spm_api.audit import stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from tests.conftest import _check_isolated_database
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import OWNER, add_posted_receipt
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_projection_fee_migration import populate_previous
from tests.test_dividend_receipt_history import claim
from tests.test_schedule_migration import upgrade_to

PREVIOUS = "0026_dividend_projection_fee"
HEAD = "0027_receipt_unknowns"
ACTION = "MIGRATE_DIVIDEND_RECEIPT_UNKNOWNS"


@pytest.fixture(autouse=True)
def private_database(
    settings: Settings, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _check_isolated_database(settings, tmp_path, tmp_path_factory.getbasetemp())


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def populated(engine: Engine) -> tuple[Any, str, dict[str, Any], dict[str, Any]]:
    event, account, _ = populate_previous(engine)
    entitlement = claim(event.revision_id, account, net=10_000_000)
    with engine.connect().execution_options(writer=True) as con, con.begin():
        insert(con, "dividend_entitlements", entitlement)
        receipt = add_posted_receipt(con, entitlement, fx=7_800_000_000_000)
    return event, account, entitlement, receipt


def test_schema_head_allows_unknown_tax_and_fee_without_defaulting_to_zero(engine: Engine) -> None:
    assert schema_head() == "0029_receipt_fx_guard"
    with unit_of_work(engine) as con:
        columns = {
            row.name: (row.notnull, row.dflt_value)
            for row in con.exec_driver_sql("PRAGMA table_info(dividend_receipts)")
        }
        assert columns["tax_micro"] == (0, None)
        assert columns["fee_micro"] == (0, None)
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize("target", ["dividend_receipts", "DIVIDEND_RECEIPTS", "Dividend_Receipts"])
def test_upgrade_preserves_financial_rows_rowids_audits_cash_fx_and_schema_guards(
    previous_engine: Engine, target: str
) -> None:
    event, account, _entitlement, receipt = populated(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql("UPDATE dividend_receipts SET rowid=37 WHERE id='legacy-receipt'")
        con.exec_driver_sql(f"CREATE INDEX synthetic_receipt_note_index ON {target}(note)")
        con.exec_driver_sql(f"""
            CREATE TRIGGER synthetic_receipt_note_guard BEFORE INSERT ON {target}
            WHEN NEW.note='synthetic-block' BEGIN SELECT RAISE(ABORT,'SYNTHETIC_GUARD'); END
        """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with previous_engine.connect() as con:
        rowids = con.exec_driver_sql("SELECT rowid,id FROM dividend_receipts ORDER BY rowid").all()
    upgrade_to(previous_engine, HEAD)
    after, upgraded = database_snapshot(previous_engine), schema(previous_engine)
    assert set(before) == set(after)
    for table, rows in before.items():
        if table not in {"alembic_version", "audit_events"}:
            assert after[table] == rows
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(
        item in upgraded
        for item in definitions
        if item[1] not in {"dividend_receipts", "trg_managed_dividend_receipt_insert"}
    )
    with unit_of_work(previous_engine) as con:
        assert (
            con.exec_driver_sql("SELECT rowid,id FROM dividend_receipts ORDER BY rowid").all()
            == rowids
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        retained = receipt_history.history(con, OWNER, event.event_id, UUID(account))
        assert retained is not None
        recorded = next(
            item for item in retained.entitlements if item.id == UUID(receipt["entitlement_id"])
        )
        assert recorded.receipts[0].id == UUID(receipt["id"])
        assert recorded.receipts[0].cash_scope == "MATCHING_CASH_RECORDED_UNVERIFIED"
        assert str(recorded.receipts[0].fx_rate_hkd) == "7.800000000000"
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    with (
        pytest.raises(IntegrityError, match="SYNTHETIC_GUARD"),
        previous_engine.connect().execution_options(writer=True) as con,
        con.begin(),
    ):
        insert(
            con,
            "dividend_receipts",
            {
                "id": str(uuid4()),
                "entitlement_id": receipt["entitlement_id"],
                "account_id": account,
                "received_date": "2026-01-10",
                "received_micro": 1_000_000,
                "currency": "USD",
                "created_at": stamp(),
                "note": "synthetic-block",
            },
        )
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_new_unknown_and_explicit_zero_remain_distinct_without_touching_old_rows(
    previous_engine: Engine,
) -> None:
    _event, account, entitlement, old = populated(previous_engine)
    upgrade_to(previous_engine, HEAD)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        unknown, zero = str(uuid4()), str(uuid4())
        base = {
            "entitlement_id": entitlement["id"],
            "account_id": account,
            "received_date": "2026-01-10",
            "received_micro": 1_000_000,
            "currency": "USD",
            "created_at": stamp(),
        }
        insert(con, "dividend_receipts", {**base, "id": unknown})
        insert(con, "dividend_receipts", {**base, "id": zero, "tax_micro": 0, "fee_micro": 0})
        rows: dict[str, str] = {
            row[0]: row[1]
            for row in con.exec_driver_sql(
                "SELECT id,json_array(tax_micro,fee_micro) FROM dividend_receipts "
                "WHERE id IN (?,?,?)",
                (old["id"], unknown, zero),
            )
        }
        assert rows[old["id"]] == "[0,0]"
        assert rows[unknown] == "[null,null]"
        assert rows[zero] == "[0,0]"


@pytest.mark.parametrize(
    "changes", [{"tax_micro": -1}, {"fee_micro": -1}, {"tax_micro": 0.25}, {"fee_micro": 0.25}]
)
def test_nullable_components_do_not_weaken_numeric_guards(
    previous_engine: Engine, changes: dict[str, Any]
) -> None:
    _event, account, entitlement, _ = populated(previous_engine)
    upgrade_to(previous_engine, HEAD)
    before = database_snapshot(previous_engine)
    with (
        pytest.raises(IntegrityError),
        previous_engine.connect().execution_options(writer=True) as con,
        con.begin(),
    ):
        insert(
            con,
            "dividend_receipts",
            {
                "id": str(uuid4()),
                "entitlement_id": entitlement["id"],
                "account_id": account,
                "received_date": "2026-01-10",
                "received_micro": 1_000_000,
                "currency": "USD",
                "created_at": stamp(),
                **changes,
            },
        )
    assert database_snapshot(previous_engine) == before


def test_cli_backup_retains_exact_previous_receipt_schema_and_values(
    previous_engine: Engine, settings: Settings
) -> None:
    populated(previous_engine)
    before = database_snapshot(previous_engine)
    migrate(settings)
    backups = list(settings.data_dir.glob("pre-migration-*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in before.items():
            escaped = table.replace('"', '""')
            assert con.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall() == rows
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS,)
    after = database_snapshot(previous_engine)
    migrate(settings)
    assert database_snapshot(previous_engine) == after


@pytest.mark.parametrize("target", ["dividend_receipts", "DIVIDEND_RECEIPTS", "Dividend_Receipts"])
def test_inbound_foreign_key_aborts_upgrade_without_touching_any_user_data(
    previous_engine: Engine, target: str
) -> None:
    _event, _account, _entitlement, receipt = populated(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql(
            "CREATE TABLE synthetic_custom_receipt_ref(id TEXT PRIMARY KEY,receipt_id TEXT "
            f"REFERENCES {target}(id) ON DELETE CASCADE)"
        )
        con.exec_driver_sql(
            "INSERT INTO synthetic_custom_receipt_ref VALUES (?,?)", (str(uuid4()), receipt["id"])
        )
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises(RuntimeError, match="foreign key"):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


@pytest.mark.parametrize("prior", [0, 1])
def test_late_audit_failure_rolls_back_rebuild_and_every_fact(
    previous_engine: Engine, prior: int
) -> None:
    populated(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql(f"""
            CREATE TRIGGER synthetic_receipt_upgrade_abort BEFORE INSERT ON audit_events
            WHEN NEW.action='{ACTION}' BEGIN SELECT RAISE(ABORT,'SYNTHETIC_FAILURE'); END
        """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con:
        con.exec_driver_sql(f"PRAGMA legacy_alter_table={prior}")
        con.commit()
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises((OperationalError, IntegrityError)):
            command.upgrade(config, HEAD)
        assert con.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == prior
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


@pytest.mark.parametrize("prior", [0, 1])
def test_connection_rename_mode_is_restored_after_upgrade(
    previous_engine: Engine, prior: int
) -> None:
    with previous_engine.connect().execution_options(writer=True) as con:
        con.exec_driver_sql(f"PRAGMA legacy_alter_table={prior}")
        con.commit()
        config = migration_config()
        config.attributes["connection"] = con
        command.upgrade(config, HEAD)
        assert con.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == prior


@pytest.mark.parametrize("name", ["rowid", "ROWID", "_rowid_", "oid", "OID"])
def test_shadowed_rowid_aborts_without_reordering_any_receipts(
    previous_engine: Engine, name: str
) -> None:
    populated(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql(
            f"ALTER TABLE dividend_receipts ADD COLUMN \"{name}\" TEXT DEFAULT 'synthetic'"
        )
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises(RuntimeError, match="rowid"):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_generated_column_shadowing_rowid_also_aborts(previous_engine: Engine) -> None:
    populated(previous_engine)
    with previous_engine.connect().execution_options(writer=True) as con, con.begin():
        con.exec_driver_sql(
            'ALTER TABLE dividend_receipts ADD COLUMN "rowid" TEXT '
            "GENERATED ALWAYS AS (note) VIRTUAL"
        )
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises(RuntimeError, match="rowid"):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


@pytest.mark.parametrize("tampered_read", [1, 2])
def test_inconsistent_column_metadata_aborts_without_mutating_storage(
    previous_engine: Engine, tampered_read: int
) -> None:
    populated(previous_engine)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    original = Connection.exec_driver_sql
    reads = 0

    def simulate(con: Connection, statement: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        result = original(con, statement, *args, **kwargs)
        if statement == "PRAGMA table_xinfo(dividend_receipts)":
            reads += 1
            if reads == tampered_read:
                rows = [list(row) for row in result.all()]
                tax = next(row for row in rows if row[1] == "tax_micro")
                tax[3] = 0 if tampered_read == 1 else 1
                replacement = MagicMock()
                replacement.all.return_value = rows
                return replacement
        return result

    with (
        patch.object(Connection, "exec_driver_sql", simulate),
        pytest.raises(RuntimeError, match="column metadata"),
    ):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


@pytest.mark.parametrize("corruption", ["value", "type", "missing", "extra", "nocase"])
def test_inconsistent_temporary_copy_aborts_before_replacing_original(
    previous_engine: Engine, corruption: str
) -> None:
    _event, _account, _entitlement, receipt = populated(previous_engine)
    if corruption == "nocase":
        with previous_engine.connect().execution_options(writer=True) as con, con.begin():
            con.exec_driver_sql(
                "ALTER TABLE dividend_receipts ADD COLUMN synthetic_case "
                "TEXT COLLATE NOCASE DEFAULT 'Mixed'"
            )
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    original = Connection.exec_driver_sql

    def simulate(con: Connection, statement: str, *args: Any, **kwargs: Any) -> Any:
        result = original(con, statement, *args, **kwargs)
        if statement.startswith('INSERT INTO "_spm_receipt_unknowns_0027"'):
            if corruption == "extra":
                original(
                    con,
                    "INSERT INTO _spm_receipt_unknowns_0027(id,entitlement_id,account_id,"
                    "received_date,received_micro,currency,created_at) "
                    "SELECT ?,entitlement_id,account_id,received_date,received_micro,currency,"
                    "created_at FROM dividend_receipts WHERE id=?",
                    (str(uuid4()), receipt["id"]),
                )
            else:
                operation = {
                    "value": (
                        "UPDATE _spm_receipt_unknowns_0027 SET received_micro=received_micro+1"
                    ),
                    "type": "UPDATE _spm_receipt_unknowns_0027 SET note=CAST(note AS BLOB)",
                    "missing": "DELETE FROM _spm_receipt_unknowns_0027",
                    "nocase": "UPDATE _spm_receipt_unknowns_0027 SET synthetic_case='MIXED'",
                }[corruption]
                original(con, f"{operation} WHERE id=?", (receipt["id"],))
        return result

    with (
        patch.object(Connection, "exec_driver_sql", simulate),
        pytest.raises(RuntimeError, match="copy"),
    ):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_downgrade_is_disabled_without_mutation(previous_engine: Engine) -> None:
    populated(previous_engine)
    upgrade_to(previous_engine, HEAD)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with (
        pytest.raises(RuntimeError, match="validated pre-upgrade backup"),
        previous_engine.connect() as con,
    ):
        config = migration_config()
        config.attributes["connection"] = con
        command.downgrade(config, PREVIOUS)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions
