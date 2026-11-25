"""Inert rebuild manifests must preserve every existing financial publication fence."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from hashlib import sha256
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_projection_writer as projection_writer
import spm_api.dividend_revision_rebuild as rebuild
import tests.test_dividend_direct_receipt_migration as direct_fixture
from spm_api.audit import audit, canonical, now, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, savepoint, schema_head, unit_of_work
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_revision_preview import proposal
from tests.test_schedule_migration import upgrade_to
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio

PREVIOUS = "0030_direct_receipts"
HEAD = "0031_dividend_rebuild"
OPERATIONS = "dividend_revision_rebuild_operations"
ACCOUNTS = "dividend_revision_rebuild_accounts"
PORTFOLIOS = "dividend_revision_rebuild_portfolios"
COMPLETIONS = "dividend_revision_rebuild_completions"


def operation(book: dict[str, Any]) -> dict[str, Any]:
    event = book["event"]
    identifier = str(uuid4())
    return {
        "id": identifier,
        "owner_id": str(book["owner"]),
        "event_id": str(event.event_id),
        "previous_revision_id": str(event.revision_id),
        "previous_revision_no": event.revision_no,
        "new_revision_id": str(uuid4()),
        "new_revision_no": event.revision_no + 1,
        "request_id": str(uuid4()),
        "intent_sha256": "1" * 64,
        "scope_sha256": "2" * 64,
        "as_of": "2026-01-09",
        "eligibility_rule": "ORDINARY_PRE_EX_DATE",
        "account_count": 2,
        "portfolio_count": 1,
        "completion_id": identifier,
        "created_at": stamp(),
    }


def account_rows(con: Any, book: dict[str, Any], operation_id: str) -> list[dict[str, Any]]:
    rows = con.exec_driver_sql(
        "SELECT a.id,a.portfolio_id,a.broker_id,a.version,p.version,p.data_revision,b.version,"
        "a.status,b.status FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
        "JOIN brokers b ON b.id=a.broker_id WHERE p.owner_id=? ORDER BY a.id",
        (str(book["owner"]),),
    ).all()
    return [
        {
            "operation_id": operation_id,
            "account_id": row[0],
            "portfolio_id": row[1],
            "broker_id": row[2],
            "account_version": row[3],
            "portfolio_version": row[4],
            "portfolio_data_revision": row[5],
            "broker_version": row[6],
            "account_status": row[7],
            "broker_status": row[8],
            "eligibility_state": "RECORDED_PRE_EX_DATE",
            "recorded_shares_e8": 0,
            "disposition": "NO_RECORDED_IMPACT",
            "previous_projection_id": None,
            "projection_id": None,
            "review_id": None,
            "review_audit_id": None,
        }
        for row in rows
    ]


def test_upgrade_preserves_all_legacy_financial_data(old_engine: Engine) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    assert before["dividend_receipts"] and before["cash_ledger_entries"] and before["audit_events"]
    upgrade_to(old_engine, HEAD)
    after = database_snapshot(old_engine)
    assert schema_head() == HEAD
    assert set(after) == set(before) | {OPERATIONS, ACCOUNTS, PORTFOLIOS, COMPLETIONS}
    assert all(after[name] == [] for name in (OPERATIONS, ACCOUNTS, PORTFOLIOS, COMPLETIONS))
    for name, rows in before.items():
        if name not in {"alembic_version", "audit_events"}:
            assert after[name] == rows
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(item in schema(old_engine) for item in definitions)
    with unit_of_work(old_engine) as con:
        audits = con.exec_driver_sql(
            "SELECT owner_id,entity_type,entity_id,before_sha256,after_sha256 "
            "FROM audit_events WHERE action='MIGRATE_DIVIDEND_REVISION_REBUILDS' "
            "ORDER BY owner_id"
        ).all()
        owners = con.exec_driver_sql("SELECT id FROM owners ORDER BY id").scalars().all()
        assert [tuple(row) for row in audits] == [
            (
                owner,
                "schema",
                HEAD,
                sha256(PREVIOUS.encode()).hexdigest(),
                sha256(HEAD.encode()).hexdigest(),
            )
            for owner in owners
        ]
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    upgrade_to(old_engine, HEAD)
    assert database_snapshot(old_engine) == after


def test_failed_manifest_migration_rolls_back_ddl_and_retained_rows(old_engine: Engine) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before, definitions = database_snapshot(old_engine), schema(old_engine)

    def fail_ddl(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "CREATE TABLE dividend_revision_rebuild_accounts" in statement:
            raise RuntimeError("synthetic manifest migration failure")

    sqlalchemy_event.listen(old_engine, "before_cursor_execute", fail_ddl)
    try:
        with pytest.raises(RuntimeError, match="synthetic manifest migration failure"):
            upgrade_to(old_engine, HEAD)
    finally:
        sqlalchemy_event.remove(old_engine, "before_cursor_execute", fail_ddl)
    assert database_snapshot(old_engine) == before
    assert schema(old_engine) == definitions


@pytest.mark.parametrize("after_first_audit", [False, True])
def test_manifest_upgrade_audit_failure_rolls_back_all_rows_and_ddl(
    old_engine: Engine, after_first_audit: bool
) -> None:
    upgrade_to(old_engine, PREVIOUS)
    action = "MIGRATE_DIVIDEND_REVISION_REBUILDS"
    with old_engine.connect().execution_options(writer=True) as con, con.begin():
        assert con.exec_driver_sql("SELECT count(*) FROM owners").scalar_one() >= 2
        condition = (
            f"AND EXISTS(SELECT 1 FROM audit_events WHERE action='{action}')"
            if after_first_audit
            else ""
        )
        con.exec_driver_sql(f"""
            CREATE TRIGGER synthetic_manifest_audit_abort BEFORE INSERT ON audit_events
            WHEN NEW.action='{action}' {condition}
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_AUDIT_FAILURE'); END
        """)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises(IntegrityError, match="SYNTHETIC_AUDIT_FAILURE"):
        upgrade_to(old_engine, HEAD)
    assert database_snapshot(old_engine) == before
    assert schema(old_engine) == definitions
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_manifest_cli_backup_retains_every_pre_upgrade_row(
    old_engine: Engine, settings: Settings
) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before = database_snapshot(old_engine)
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
    after = database_snapshot(old_engine)
    assert all(after[table] == [] for table in (OPERATIONS, ACCOUNTS, PORTFOLIOS, COMPLETIONS))
    migrate(settings)
    assert database_snapshot(old_engine) == after


def test_manifest_downgrade_refuses_to_remove_history(old_engine: Engine) -> None:
    upgrade_to(old_engine, HEAD)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with (
        pytest.raises(RuntimeError, match="validated pre-upgrade backup"),
        old_engine.connect() as con,
    ):
        config = migration_config()
        config.attributes["connection"] = con
        command.downgrade(config, PREVIOUS)
    assert database_snapshot(old_engine) == before
    assert schema(old_engine) == definitions


def test_manifest_upgrade_serializes_with_a_competing_owner_write(old_engine: Engine) -> None:
    upgrade_to(old_engine, PREVIOUS)
    before = database_snapshot(old_engine)
    barrier, owner = Barrier(2), str(uuid4())
    order: list[str] = []

    def upgrade() -> None:
        barrier.wait(timeout=15)
        with old_engine.connect().execution_options(writer=True) as con, con.begin():
            order.append("upgrade")
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, HEAD)

    def create_owner() -> None:
        barrier.wait(timeout=15)
        with old_engine.connect().execution_options(writer=True) as con, con.begin():
            order.append("owner")
            at = stamp()
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic-only',?,?)",
                (owner, at, at),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upgrade), pool.submit(create_owner)]
        for future in futures:
            future.result(timeout=30)
    assert order in (["upgrade", "owner"], ["owner", "upgrade"])
    after = database_snapshot(old_engine)
    for table, rows in before.items():
        if table not in {"owners", "audit_events", "alembic_version"}:
            assert after[table] == rows
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after["owners"]) == len(before["owners"]) + 1
    with unit_of_work(old_engine) as con:
        audited = (
            con.exec_driver_sql(
                "SELECT owner_id FROM audit_events "
                "WHERE action='MIGRATE_DIVIDEND_REVISION_REBUILDS'"
            )
            .scalars()
            .all()
        )
        assert len(audited) == len(before["owners"]) + int(order[0] == "owner")
        assert (owner in audited) == (order[0] == "owner")
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert all(after[table] == [] for table in (OPERATIONS, ACCOUNTS, PORTFOLIOS, COMPLETIONS))


async def test_pending_operation_cannot_commit_even_after_nested_savepoint_release(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    assert schema_head() == HEAD
    book = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        with savepoint(con):
            insert(con, OPERATIONS, operation(book))
        assert con.exec_driver_sql(f"SELECT count(*) FROM {OPERATIONS}").scalar_one() == 1
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid", "completion_id": "not-a-uuid"},
        {"owner_id": str(uuid4())},
        {"event_id": str(uuid4())},
        {"previous_revision_id": str(uuid4())},
        {"previous_revision_no": 9, "new_revision_no": 10},
        {"new_revision_no": 1},
        {"request_id": "not-a-uuid"},
        {"intent_sha256": "G" * 64},
        {"scope_sha256": "short"},
        {"as_of": "2026-02-30"},
        {"eligibility_rule": "AUTOMATIC"},
        {"account_count": -1},
        {"portfolio_count": -1},
        {"completion_id": str(uuid4())},
        {"created_at": "2026-01-01T00:00:00Z"},
        {"created_at": "2020-01-01T00:00:00.000000Z"},
    ],
)
async def test_invalid_operation_is_rejected_before_any_pending_row(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], changes: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    value = {**operation(book), **changes}
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(IntegrityError):
            insert(con, OPERATIONS, value)
        assert con.exec_driver_sql(f"SELECT count(*) FROM {OPERATIONS}").scalar_one() == 0
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": str(uuid4())},
        {"account_id": str(uuid4())},
        {"portfolio_id": str(uuid4())},
        {"broker_id": str(uuid4())},
        {"account_version": 0},
        {"account_version": 999},
        {"portfolio_version": 999},
        {"portfolio_data_revision": 999},
        {"broker_version": 999},
        {"account_status": "ARCHIVED"},
        {"broker_status": "ARCHIVED"},
        {"eligibility_state": "CONFIRMED"},
        {"recorded_shares_e8": -1},
        {"recorded_shares_e8": None},
        {"disposition": "OMIT"},
        {"previous_projection_id": str(uuid4())},
        {"projection_id": str(uuid4())},
        {"review_id": str(uuid4())},
        {"review_audit_id": str(uuid4())},
    ],
)
async def test_invalid_account_scope_fails_before_manifest_completion(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], changes: dict[str, Any]
) -> None:
    class AbortSynthetic(Exception):
        pass

    book = await prepare(client, engine, graph, holdings=False)
    op = operation(book)
    before = database_snapshot(engine)
    with pytest.raises(AbortSynthetic), unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        value = {**account_rows(con, book, op["id"])[0], **changes}
        with pytest.raises(IntegrityError):
            insert(con, ACCOUNTS, value)
        assert con.exec_driver_sql(f"SELECT count(*) FROM {ACCOUNTS}").scalar_one() == 0
        raise AbortSynthetic
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("late_duplicate_audit", "late_account_after"),
    [(False, False), (True, False), (False, True)],
)
async def test_sql_shape_completion_on_an_empty_scope_does_not_authorize_finance(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    late_duplicate_audit: bool,
    late_account_after: bool,
) -> None:
    assert schema_head() == HEAD
    book = await prepare(client, engine, graph, holdings=False)
    previous = book["event"]
    op = operation(book)
    op["portfolio_count"] = 0
    decision = manual(
        event_id=previous.event_id,
        revision_id=UUID(op["new_revision_id"]),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    before = database_snapshot(engine)
    outer = pytest.raises(IntegrityError) if late_duplicate_audit else nullcontext()
    with outer, unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        account_rows_snapshot = account_rows(con, book, op["id"])
        for row in account_rows_snapshot:
            insert(con, ACCOUNTS, row)
        event_writer.save(con, book["owner"], decision, at=now(), request_id=UUID(op["request_id"]))
        if late_duplicate_audit:
            audit(
                con,
                str(book["owner"]),
                "dividend_event_revision",
                str(decision.revision_id),
                "DIVIDEND_EVENT_REVISED",
                request_id=op["request_id"],
                after={"synthetic": "late duplicate event audit"},
                reason=decision.reason,
            )
        completed_at = now()
        audit(
            con,
            str(book["owner"]),
            "dividend_revision_rebuild",
            op["id"],
            "DIVIDEND_REVISION_REBUILD_COMPLETED",
            request_id=op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=decision.reason,
            at=completed_at,
        )
        audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_revision_rebuild' "
            "AND entity_id=? AND action='DIVIDEND_REVISION_REBUILD_COMPLETED'",
            (op["id"],),
        ).scalar_one()
        insert(
            con,
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        )
        if late_account_after:
            con.exec_driver_sql(
                "UPDATE accounts SET version=version+1 WHERE id=?", (str(book["account_id"]),)
            )
    if late_duplicate_audit:
        assert database_snapshot(engine) == before
        return
    after = database_snapshot(engine)
    replacement_rows = (
        (OPERATIONS, op),
        (ACCOUNTS, account_rows_snapshot[0]),
        (
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        ),
    )
    for table, values in replacement_rows:
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
    for name in (
        "dividend_entitlements",
        "dividend_projection_versions",
        "dividend_receipts",
        "dividend_direct_receipts",
        "cash_ledger_entries",
        "fx_rates",
    ):
        assert after[name] == before[name]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(f"SELECT count(*) FROM {ACCOUNTS}").scalar_one() == 2
        if late_account_after:
            pin, current_version = con.exec_driver_sql(
                f"SELECT manifest.account_version,account.version FROM {ACCOUNTS} manifest "
                "JOIN accounts account ON account.id=manifest.account_id "
                "WHERE manifest.operation_id=? AND manifest.account_id=?",
                (op["id"], str(book["account_id"])),
            ).one()
            assert current_version == pin + 1
    for table in (OPERATIONS, ACCOUNTS, COMPLETIONS):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(f"UPDATE {table} SET rowid=rowid")
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(f"DELETE FROM {table}")
    assert database_snapshot(engine) == after


async def test_completion_rejects_a_missing_owned_account_and_rolls_back_head(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    assert schema_head() == HEAD
    book = await prepare(client, engine, graph, holdings=False)
    previous = book["event"]
    op = operation(book)
    op["portfolio_count"] = 0
    decision = manual(
        event_id=previous.event_id,
        revision_id=UUID(op["new_revision_id"]),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        insert(con, ACCOUNTS, account_rows(con, book, op["id"])[0])
        event_writer.save(con, book["owner"], decision, at=now(), request_id=UUID(op["request_id"]))
        completed_at = now()
        audit(
            con,
            str(book["owner"]),
            "dividend_revision_rebuild",
            op["id"],
            "DIVIDEND_REVISION_REBUILD_COMPLETED",
            request_id=op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=decision.reason,
            at=completed_at,
        )
        audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_revision_rebuild' "
            "AND entity_id=? AND action='DIVIDEND_REVISION_REBUILD_COMPLETED'",
            (op["id"],),
        ).scalar_one()
        insert(
            con,
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        )
    assert database_snapshot(engine) == before


async def test_completion_rejects_a_retained_entitlement_moved_outside_owner_scope(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    previous = book["event"]
    foreign_owner, foreign_portfolio, foreign_broker = (str(uuid4()) for _ in range(3))
    with unit_of_work(engine, write=True) as con:
        at = stamp()
        insert(
            con,
            "dividend_entitlements",
            {
                "id": str(uuid4()),
                "revision_id": str(previous.revision_id),
                "account_id": str(book["account_id"]),
                "eligible_shares_e8": 100_000_000,
                "gross_micro": 1_000_000,
                "expected_net_micro": 1_000_000,
                "currency": "USD",
                "quality": "ACCRUED",
                "projection_version": 0,
                "created_at": at,
            },
        )
        insert(
            con,
            "owners",
            {
                "id": foreign_owner,
                "password_hash": "synthetic-only",
                "password_changed_at": at,
                "created_at": at,
            },
        )
        insert(
            con,
            "portfolios",
            {
                "id": foreign_portfolio,
                "owner_id": foreign_owner,
                "name": "Foreign",
                "created_at": at,
            },
        )
        insert(con, "brokers", {"id": foreign_broker, "owner_id": foreign_owner, "name": "Foreign"})
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (foreign_portfolio, foreign_broker, str(book["account_id"])),
        )
    op = operation(book)
    op.update(account_count=1, portfolio_count=0)
    before = database_snapshot(engine)
    reached_completion = False
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        for row in account_rows(con, book, op["id"]):
            insert(con, ACCOUNTS, row)
        event_at = now()
        con.exec_driver_sql(
            "INSERT INTO dividend_event_revisions "
            "(id,event_id,revision_no,event_type,ex_date,announce_date,record_date,pay_date,"
            "dps_e8,currency,adjustment_basis,review_status,manual_locked,reason,created_at) "
            "SELECT ?,event_id,2,event_type,ex_date,announce_date,record_date,pay_date,"
            "110000000,currency,adjustment_basis,'PROVISIONAL',0,reason,? "
            "FROM dividend_event_revisions WHERE id=?",
            (op["new_revision_id"], stamp(event_at), op["previous_revision_id"]),
        )
        insert(
            con,
            "dividend_revision_decisions",
            {
                "revision_id": op["new_revision_id"],
                "event_id": op["event_id"],
                "owner_id": op["owner_id"],
                "decision_kind": "MANUAL",
                "evidence_count": 0,
                "citation": "Synthetic SQL shape only",
                "request_id": op["request_id"],
                "created_at": stamp(event_at),
            },
        )
        audit(
            con,
            op["owner_id"],
            "dividend_event_revision",
            op["new_revision_id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=op["request_id"],
            before={"synthetic": "previous"},
            after={"synthetic": "successor"},
            reason=previous.reason,
            at=event_at,
        )
        con.exec_driver_sql(
            "UPDATE dividend_events SET current_revision_id=?,workflow_status='PROVISIONAL',"
            "version=2 WHERE id=?",
            (op["new_revision_id"], op["event_id"]),
        )
        completed_at = now()
        audit(
            con,
            op["owner_id"],
            "dividend_revision_rebuild",
            op["id"],
            "DIVIDEND_REVISION_REBUILD_COMPLETED",
            request_id=op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=previous.reason,
            at=completed_at,
        )
        audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_revision_rebuild' "
            "AND entity_id=? AND action='DIVIDEND_REVISION_REBUILD_COMPLETED'",
            (op["id"],),
        ).scalar_one()
        reached_completion = True
        insert(
            con,
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        )
    assert reached_completion
    assert database_snapshot(engine) == before


async def test_retained_receipt_cannot_be_declared_as_no_recorded_impact(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    direct_receipt(engine, book, received_micro=6_000_000)
    op = operation(book)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        row = next(
            row
            for row in account_rows(con, book, op["id"])
            if row["account_id"] == str(book["account_id"])
        )
        insert(con, ACCOUNTS, row)
    assert database_snapshot(engine) == before


async def test_completion_rechecks_a_receipt_added_after_account_manifest(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    previous = book["event"]
    op = operation(book)
    decision = manual(
        event_id=previous.event_id,
        revision_id=UUID(op["new_revision_id"]),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    before = database_snapshot(engine)
    with (
        patch.object(direct_fixture, "OWNER", book["owner"]),
        pytest.raises(IntegrityError),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, OPERATIONS, op)
        rows = account_rows(con, book, op["id"])
        for row in rows:
            insert(con, ACCOUNTS, row)
        event_writer.save(con, book["owner"], decision, at=now(), request_id=UUID(op["request_id"]))
        value = direct_fixture.receipt_values(decision, str(book["account_id"]))
        insert(con, direct_fixture.TABLE, value)
        insert(con, direct_fixture.ORIGINS, direct_fixture.seal(con, value))
        portfolio = next(row for row in rows if row["account_id"] == str(book["account_id"]))
        invalidate_portfolio(
            con,
            portfolio["portfolio_id"],
            str(decision.revision_id),
            "DIVIDEND_EVENT_CHANGED",
            "2026-01-08",
        )
        marker = con.exec_driver_sql(
            "SELECT id FROM projection_invalidations WHERE portfolio_id=? AND source_id=? "
            "AND reason='DIVIDEND_EVENT_CHANGED'",
            (portfolio["portfolio_id"], str(decision.revision_id)),
        ).scalar_one()
        final = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (portfolio["portfolio_id"],)
        ).scalar_one()
        insert(
            con,
            PORTFOLIOS,
            {
                "operation_id": op["id"],
                "portfolio_id": portfolio["portfolio_id"],
                "portfolio_version": portfolio["portfolio_version"],
                "initial_data_revision": portfolio["portfolio_data_revision"],
                "final_data_revision": final,
                "invalidation_id": marker,
            },
        )
        completed_at = now()
        audit(
            con,
            str(book["owner"]),
            "dividend_revision_rebuild",
            op["id"],
            "DIVIDEND_REVISION_REBUILD_COMPLETED",
            request_id=op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=decision.reason,
            at=completed_at,
        )
        audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_revision_rebuild' "
            "AND entity_id=? AND action='DIVIDEND_REVISION_REBUILD_COMPLETED'",
            (op["id"],),
        ).scalar_one()
        insert(
            con,
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "projection_request",
        "late_account",
        "late_broker_version",
        "late_portfolio_version",
        "late_data_revision",
        "late_new_account",
        "late_review_acknowledgement",
        "late_duplicate_review",
        "late_review_audit",
        "wrong_review_detail",
        "wrong_review_request",
        "late_projection_intent",
        "late_projection_successor",
        "late_invalidation_resolution",
        "late_invalidation_date",
        "duplicate_invalidation",
        "invalidation_time_before",
        "invalidation_time_after",
        "invalidation_time_invalid",
        "wrong_initial_data_revision",
        "missing_portfolio",
    ],
)
async def test_completion_checks_recorded_projection_task_and_final_account_scope(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], case: str
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    op = operation(book)
    decision = manual(
        event_id=previous.event_id,
        revision_id=UUID(op["new_revision_id"]),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    before = database_snapshot(engine)
    stages: list[str] = []
    outer = nullcontext() if case == "valid" else pytest.raises(IntegrityError)
    with outer, unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        rows = account_rows(con, book, op["id"])
        source = next(row for row in rows if row["account_id"] == str(book["account_id"]))
        review_id, projection_id = str(uuid4()), str(uuid4())
        event_at = now()
        insert(
            con,
            "review_items",
            {
                "id": review_id,
                "event_id": op["event_id"],
                "account_id": source["account_id"],
                "portfolio_id": source["portfolio_id"],
                "kind": "DIVIDEND_REVISION",
                "severity": "WARNING",
                "status": "OPEN",
                "reason_code": "RECEIPT_RECONCILIATION_REQUIRED",
                "detail_json": canonical(
                    {
                        "previous_revision_id": op["previous_revision_id"],
                        "new_revision_id": str(uuid4())
                        if case == "wrong_review_detail"
                        else op["new_revision_id"],
                    }
                ),
                "created_at": stamp(event_at),
            },
        )
        audit(
            con,
            str(book["owner"]),
            "review_item",
            review_id,
            "DIVIDEND_REVISION_RECONCILIATION_CREATED",
            request_id=str(uuid4()) if case == "wrong_review_request" else op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=decision.reason,
            at=event_at,
        )
        review_audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='review_item' AND entity_id=? "
            "AND action='DIVIDEND_REVISION_RECONCILIATION_CREATED'",
            (review_id,),
        ).scalar_one()
        source.update(
            disposition="REBUILD_REQUIRED",
            recorded_shares_e8=1_000_000_000,
            projection_id=projection_id,
            review_id=review_id,
            review_audit_id=review_audit_id,
        )
        for row in rows:
            insert(con, ACCOUNTS, row)
        event_writer.save(
            con, book["owner"], decision, at=event_at, request_id=UUID(op["request_id"])
        )
        marker = con.exec_driver_sql(
            "SELECT id FROM projection_invalidations WHERE portfolio_id=? AND source_id=? "
            "AND reason='DIVIDEND_EVENT_CHANGED'",
            (source["portfolio_id"], str(decision.revision_id)),
        ).scalar_one()
        final = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (source["portfolio_id"],)
        ).scalar_one()
        if case != "missing_portfolio":
            initial = source["portfolio_data_revision"]
            if case == "wrong_initial_data_revision":
                assert initial > 0
                initial -= 1
            insert(
                con,
                PORTFOLIOS,
                {
                    "operation_id": op["id"],
                    "portfolio_id": source["portfolio_id"],
                    "portfolio_version": source["portfolio_version"],
                    "initial_data_revision": initial,
                    "final_data_revision": final,
                    "invalidation_id": marker,
                },
            )
        projection = projection_writer.ProjectionIntent(
            id=UUID(projection_id),
            event_id=decision.event_id,
            revision_id=decision.revision_id,
            revision_no=decision.revision_no,
            account_id=UUID(source["account_id"]),
            portfolio_id=UUID(source["portfolio_id"]),
            broker_id=UUID(source["broker_id"]),
            account_version=source["account_version"],
            broker_version=source["broker_version"],
            expected_data_revision=final,
            expected_projection_no=0,
            expected_projection_id=None,
            as_of=op["as_of"],
            eligibility_rule=op["eligibility_rule"],
            tax_effective_on="2026-01-08",
            expected_tax_head_no=0,
            expected_tax_head_id=None,
            expected_tax_selected_id=None,
            fee_effective_on="2026-01-08",
            expected_fee_head_no=0,
            expected_fee_head_id=None,
            expected_fee_selected_id=None,
            fx_rate_hkd=None,
            fx_source=None,
            fee_rate_event=None,
            fee_rate_source=None,
            reason=decision.reason,
        )
        request = uuid4() if case == "projection_request" else UUID(op["request_id"])
        recorded = projection_writer.save(con, book["owner"], projection, request_id=request)
        assert recorded.version.state == "NEEDS_REVIEW" and recorded.version.accrual is None
        if case == "late_projection_successor":
            successor_projection = projection.model_copy(
                update={
                    "id": uuid4(),
                    "expected_projection_no": 1,
                    "expected_projection_id": UUID(projection_id),
                }
            )
            projection_writer.save(con, book["owner"], successor_projection, request_id=uuid4())
        if case == "late_account":
            con.exec_driver_sql(
                "UPDATE accounts SET version=version+1 WHERE id=?", (source["account_id"],)
            )
        elif case == "late_broker_version":
            con.exec_driver_sql(
                "UPDATE brokers SET version=version+1 WHERE id=?", (source["broker_id"],)
            )
        elif case == "late_portfolio_version":
            con.exec_driver_sql(
                "UPDATE portfolios SET version=version+1 WHERE id=?", (source["portfolio_id"],)
            )
        elif case == "late_data_revision":
            con.exec_driver_sql(
                "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
                (source["portfolio_id"],),
            )
        elif case == "late_new_account":
            insert(
                con,
                "accounts",
                {
                    "id": str(uuid4()),
                    "portfolio_id": source["portfolio_id"],
                    "broker_id": source["broker_id"],
                    "name": "Synthetic late account",
                    "default_currency": "USD",
                    "created_at": stamp(),
                },
            )
        elif case == "late_review_acknowledgement":
            con.exec_driver_sql(
                "UPDATE review_items SET status='ACKNOWLEDGED' WHERE id=?", (review_id,)
            )
        elif case == "late_duplicate_review":
            con.exec_driver_sql(
                "INSERT INTO review_items(id,portfolio_id,event_id,account_id,kind,severity,"
                "status,reason_code,detail_json,created_at,resolved_at) "
                "SELECT ?,portfolio_id,event_id,account_id,kind,severity,status,reason_code,"
                "detail_json,created_at,resolved_at FROM review_items WHERE id=?",
                (str(uuid4()), review_id),
            )
        elif case in {"late_invalidation_resolution", "late_invalidation_date"}:
            if case == "late_invalidation_resolution":
                con.exec_driver_sql(
                    "UPDATE projection_invalidations SET resolved_at=? WHERE id=?",
                    (stamp(), marker),
                )
            else:
                con.exec_driver_sql(
                    "UPDATE projection_invalidations SET from_date='2026-01-09' WHERE id=?",
                    (marker,),
                )
        elif case == "duplicate_invalidation":
            con.exec_driver_sql(
                "INSERT INTO projection_invalidations "
                "(id,portfolio_id,from_date,reason,source_id,created_at,resolved_at) "
                "SELECT ?,portfolio_id,from_date,reason,source_id,created_at,resolved_at "
                "FROM projection_invalidations WHERE id=?",
                (str(uuid4()), marker),
            )
        elif case.startswith("invalidation_time_"):
            invalid_time = {
                "invalidation_time_before": "2020-01-01T00:00:00.000000Z",
                "invalidation_time_after": "2099-01-01T00:00:00.000000Z",
                "invalidation_time_invalid": "not-a-timestamp",
            }[case]
            con.exec_driver_sql(
                "UPDATE projection_invalidations SET created_at=? WHERE id=?",
                (invalid_time, marker),
            )
        elif case in {"late_review_audit", "late_projection_intent"}:
            is_review = case == "late_review_audit"
            audit(
                con,
                str(book["owner"]),
                "review_item" if is_review else "dividend_projection_intent",
                review_id if is_review else projection_id,
                "DIVIDEND_REVISION_RECONCILIATION_CREATED"
                if is_review
                else "DIVIDEND_PROJECTION_INTENT_RECORDED",
                request_id=op["request_id"],
                after={"synthetic": "late duplicate audit"},
                reason=decision.reason,
            )
        completed_at = now()
        audit(
            con,
            str(book["owner"]),
            "dividend_revision_rebuild",
            op["id"],
            "DIVIDEND_REVISION_REBUILD_COMPLETED",
            request_id=op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=decision.reason,
            at=completed_at,
        )
        audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_revision_rebuild' "
            "AND entity_id=? AND action='DIVIDEND_REVISION_REBUILD_COMPLETED'",
            (op["id"],),
        ).scalar_one()
        stages.append("completion")
        insert(
            con,
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        )
    assert stages == ["completion"]
    if case == "valid":
        with unit_of_work(engine) as con:
            assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert con.exec_driver_sql(f"SELECT count(*) FROM {COMPLETIONS}").scalar_one() == 1
            saved_portfolio = dict(
                con.exec_driver_sql(f"SELECT * FROM {PORTFOLIOS} WHERE operation_id=?", (op["id"],))
                .mappings()
                .one()
            )
        completed = database_snapshot(engine)
        columns = ",".join(saved_portfolio)
        placeholders = ",".join("?" for _ in saved_portfolio)
        for statement, parameters in (
            (f"UPDATE {PORTFOLIOS} SET rowid=rowid", ()),
            (f"DELETE FROM {PORTFOLIOS}", ()),
            (
                f"INSERT OR REPLACE INTO {PORTFOLIOS} ({columns}) VALUES ({placeholders})",
                tuple(saved_portfolio.values()),
            ),
        ):
            with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
                con.exec_driver_sql(statement, parameters)
        assert database_snapshot(engine) == completed
    else:
        assert database_snapshot(engine) == before


async def test_sql_completion_shape_does_not_prove_a_claimed_zero_position(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    op = operation(book)
    decision = manual(
        event_id=previous.event_id,
        revision_id=UUID(op["new_revision_id"]),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], previous.event_id, decision, proposal(graph))
    assert observed is not None
    recorded = next(
        account
        for portfolio in observed.preview.snapshot.portfolios
        for account in portfolio.book.accounts
        if account.account_id == str(book["account_id"])
    )
    assert recorded.recorded_quantity == "10.00000000"
    with unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        rows = account_rows(con, book, op["id"])
        for row in rows:
            insert(con, ACCOUNTS, row)
        event_writer.save(con, book["owner"], decision, at=now(), request_id=UUID(op["request_id"]))
        source = next(row for row in rows if row["account_id"] == str(book["account_id"]))
        marker = con.exec_driver_sql(
            "SELECT id FROM projection_invalidations WHERE portfolio_id=? AND source_id=? "
            "AND reason='DIVIDEND_EVENT_CHANGED'",
            (source["portfolio_id"], str(decision.revision_id)),
        ).scalar_one()
        final = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (source["portfolio_id"],)
        ).scalar_one()
        insert(
            con,
            PORTFOLIOS,
            {
                "operation_id": op["id"],
                "portfolio_id": source["portfolio_id"],
                "portfolio_version": source["portfolio_version"],
                "initial_data_revision": source["portfolio_data_revision"],
                "final_data_revision": final,
                "invalidation_id": marker,
            },
        )
        completed_at = now()
        audit(
            con,
            str(book["owner"]),
            "dividend_revision_rebuild",
            op["id"],
            "DIVIDEND_REVISION_REBUILD_COMPLETED",
            request_id=op["request_id"],
            after={"synthetic": "shape is not authentication or financial proof"},
            reason=decision.reason,
            at=completed_at,
        )
        audit_id = con.exec_driver_sql(
            "SELECT id FROM audit_events WHERE entity_type='dividend_revision_rebuild' "
            "AND entity_id=? AND action='DIVIDEND_REVISION_REBUILD_COMPLETED'",
            (op["id"],),
        ).scalar_one()
        insert(
            con,
            COMPLETIONS,
            {"operation_id": op["id"], "audit_id": audit_id, "created_at": stamp(completed_at)},
        )
    with unit_of_work(engine) as con:
        stored_row = con.exec_driver_sql(
            f"SELECT recorded_shares_e8,disposition FROM {ACCOUNTS} "
            "WHERE operation_id=? AND account_id=?",
            (op["id"], str(book["account_id"])),
        ).one()
        assert tuple(stored_row) == (0, "NO_RECORDED_IMPACT")
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_projection_versions").scalar_one()
            == 0
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("projection", "DIVIDEND_PROJECTION_REBUILD_REQUIRED"),
        ("receipt", "DIRECT_RECEIPT_RECONCILIATION_REQUIRED"),
    ],
)
async def test_pending_manifest_never_relaxes_existing_sql_publication_fences(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    kind: str,
    code: str,
) -> None:
    class AbortSynthetic(Exception):
        pass

    book = await prepare(client, engine, graph)
    if kind == "projection":
        tax, fee = settings(engine, book, rate="0", fee="0")
        save(engine, book, intent(engine, book, tax, fee), uuid4())
    else:
        direct_receipt(engine, book, received_micro=6_000_000)
    op = operation(book)
    before = database_snapshot(engine)
    with pytest.raises(AbortSynthetic), unit_of_work(engine, write=True) as con:
        insert(con, OPERATIONS, op)
        with pytest.raises(IntegrityError, match=code):
            con.exec_driver_sql(
                "UPDATE dividend_events SET current_revision_id=? WHERE id=?",
                (op["new_revision_id"], op["event_id"]),
            )
        raise AbortSynthetic
    assert database_snapshot(engine) == before


async def test_recorded_projection_and_direct_receipt_still_block_publication(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    assert schema_head() == HEAD
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    save(engine, book, intent(engine, book, tax, fee), uuid4())
    direct_receipt(engine, book, received_micro=6_000_000)
    previous = book["event"]
    successor = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    before = database_snapshot(engine)
    with (
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"),
        unit_of_work(engine, write=True) as con,
    ):
        event_writer.save(con, book["owner"], successor, at=now(), request_id=uuid4())
    assert database_snapshot(engine) == before
