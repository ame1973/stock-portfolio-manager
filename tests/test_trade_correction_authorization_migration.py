"""Unverified correction authorization storage is inert and append-only."""

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api import trade_audit, trade_history_review, trade_service
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from spm_api.trade_codec import as_trade
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_schedule_migration import upgrade_to
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trade_correction_scope import second_auditless_revision
from tests.test_trades import book as book

HEAD = "0019_trade_correction_scope"
PREVIOUS = "0018_trade_history_reviews"
TABLE = "trade_correction_authorizations"
ACTION = "MIGRATE_TRADE_CORRECTION_SCOPE"
REASON = "Correct this explicitly unverified synthetic trade"


def authorization_values(
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"] = "REVISE",
    *,
    two_gaps: bool = False,
    remember_result: bool = True,
    result_changes: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Create only new synthetic facts and audits; the returned SQL record is not a grant."""
    root = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    target = str(second_auditless_revision(engine, UUID(root))) if two_gaps else root
    owner, request, identifier = book["owner"]["id"], str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        earlier = (
            trade_history_review.save(
                con,
                UUID(owner),
                UUID(root),
                review_id=uuid4(),
                request_id=uuid4(),
                expected_trade_version=2,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
            if two_gaps
            else None
        )
        reviewed = trade_history_review.save(
            con,
            UUID(owner),
            UUID(target),
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        original = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (target,)).mappings().one()
        )
        trade_service._retire(con, original, "SUPERSEDED" if operation == "REVISE" else "VOID")
        result = target
        if operation == "REVISE":
            result, at = str(uuid4()), stamp()
            insert(
                con,
                "trades",
                {
                    **dict(original),
                    "id": result,
                    "revision": original["revision"] + 1,
                    "supersedes_id": target,
                    "created_at": at,
                    "updated_at": at,
                    **(result_changes or {}),
                },
            )
            successor = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (result,)).mappings().one()
            )
            after = as_trade(successor).model_dump(mode="json")
        else:
            after = {"status": "VOID", "version": 2}
        audit(
            con,
            owner,
            "trade",
            result,
            operation,
            request_id=request,
            before=dict(original),
            after=after,
            reason=REASON,
        )
        if operation == "REVISE" and remember_result:
            trade_audit.remember_created(con, UUID(owner), UUID(result), UUID(request))
        audit(
            con,
            owner,
            "trade_correction_authorization",
            identifier,
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=request,
            after={"synthetic": "well-shaped audit is not an authenticated authorization"},
            reason=REASON,
        )
        recorded = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", identifier),
        ).one()
    pins = [
        {
            "trade_id": target,
            "trade_version": 1,
            "review_id": str(reviewed.id),
            "review_revision": str(reviewed.revision),
            "gap": "MISSING_CREATION_AUDIT",
        }
    ]
    if earlier is not None:
        pins.insert(
            0,
            {
                "trade_id": root,
                "trade_version": 2,
                "review_id": str(earlier.id),
                "review_revision": str(earlier.revision),
                "gap": "MISSING_CREATION_AUDIT",
            },
        )
    return {
        "id": identifier,
        "owner_id": owner,
        "root_id": root,
        "trade_id": target,
        "trade_version": 1,
        "operation": operation,
        "result_trade_id": result,
        "scope_json": canonical(pins),
        "intent_sha256": "a" * 64,
        "request_id": request,
        "audit_id": recorded.id,
        "created_at": recorded.created_at,
    }, pins


def reject(con: Connection, values: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError), con.begin_nested():
        insert(con, TABLE, values)


@pytest.fixture(autouse=True)
def exclusive_test_database(settings: Any, tmp_path: Path) -> None:
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def previous_engine(old_engine: Engine, tmp_path: Path) -> Iterator[Engine]:
    assert old_engine.url.database is not None
    assert Path(old_engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def test_head_starts_with_no_correction_authorizations(engine: Engine) -> None:
    with unit_of_work(engine) as con:
        assert schema_head() == "0022_dividend_projection_tax"
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0


def test_upgrade_preserves_all_existing_facts_and_audits(previous_engine: Engine) -> None:
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["trades"] and before["dividend_receipts"] and before["audit_events"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {
        key: value
        for key, value in before.items()
        if key not in {"alembic_version", "audit_events"}
    } == {key: after[key] for key in before if key not in {"alembic_version", "audit_events"}}
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(item in schema(previous_engine) for item in definitions)
    with unit_of_work(previous_engine) as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_upgrade_preserves_a_private_exact_rollback_snapshot(
    previous_engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    assert previous_engine.url.database is not None
    assert Path(previous_engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())
    before = database_snapshot(previous_engine)
    migrate(settings)
    snapshots = list(settings.data_dir.glob("pre-migration-*.sqlite3"))
    assert len(snapshots) == 1 and snapshots[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(f"file:{snapshots[0]}?mode=ro", uri=True) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in before.items():
            escaped = table.replace('"', '""')
            assert con.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall() == rows
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS,)
    after = database_snapshot(previous_engine)
    migrate(settings)
    assert database_snapshot(previous_engine) == after
    assert list(settings.data_dir.glob("pre-migration-*.sqlite3")) == snapshots


@pytest.mark.parametrize("failure", ["late-ddl", "second-owner-audit"])
def test_failed_upgrade_rolls_back_schema_facts_and_audits(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_trade_correction_authorization_delete "
                "BEFORE DELETE ON settings BEGIN SELECT 1; END"
            )
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_correction_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions
    with previous_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == PREVIOUS
        )
        assert not con.exec_driver_sql("SELECT 1 FROM sqlite_schema WHERE name=?", (TABLE,)).first()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
@pytest.mark.parametrize("two_gaps", [False, True])
async def test_sql_scope_is_immutable_evidence_not_a_financial_grant(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    tmp_path: Path,
    operation: Literal["REVISE", "VOID"],
    two_gaps: bool,
) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    values, pins = authorization_values(engine, book, operation, two_gaps=two_gaps)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    after = database_snapshot(engine)
    assert {key: rows for key, rows in before.items() if key != TABLE} == {
        key: rows for key, rows in after.items() if key != TABLE
    }
    assert len(after[TABLE]) == 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            f"SELECT scope_json FROM {TABLE} WHERE id=?", (values["id"],)
        ).one()
        assert row.scope_json == canonical(pins)
        fingerprint = con.exec_driver_sql(
            "SELECT after_sha256 FROM audit_events WHERE id=?", (values["audit_id"],)
        ).scalar_one()
        assert fingerprint == digest(
            canonical({"synthetic": "well-shaped audit is not an authenticated authorization"})
        )
    with unit_of_work(engine, write=True) as con:
        for sql in (
            f"UPDATE {TABLE} SET scope_json=scope_json WHERE id=:id",
            f"DELETE FROM {TABLE} WHERE id=:id",
        ):
            with pytest.raises(IntegrityError), con.begin_nested():
                con.exec_driver_sql(sql, {"id": values["id"]})
        columns = ",".join(values)
        with pytest.raises(IntegrityError), con.begin_nested():
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE}({columns}) VALUES "
                f"({','.join(':' + key for key in values)})",
                values,
            )
    assert database_snapshot(engine) == after


@pytest.mark.anyio
@pytest.mark.parametrize(
    "problem",
    [
        "invalid-json",
        "empty",
        "not-array",
        "not-object",
        "extra-key",
        "duplicate-key",
        "duplicate-pin",
        "trade-id",
        "review-id",
        "gap",
        "version",
        "version-real",
        "revision-number",
        "revision-zero",
        "revision-leading-zero",
        "revision-overflow",
        "missing-pin",
    ],
)
async def test_malformed_or_incomplete_scopes_are_rejected_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    values, pins = authorization_values(engine, book, two_gaps=problem == "missing-pin")
    bad = deepcopy(pins)
    if problem == "invalid-json":
        values["scope_json"] = "["
    elif problem == "not-array":
        values["scope_json"] = "{}"
    elif problem == "not-object":
        values["scope_json"] = '["bad"]'
    elif problem == "duplicate-key":
        encoded = canonical(pins)[1:-1]
        values["scope_json"] = f'[{encoded[:-1]},"gap":"MISSING_CREATION_AUDIT"}}]'
    else:
        if problem == "empty":
            bad = []
        elif problem == "extra-key":
            bad[0]["unexpected"] = "value"
        elif problem == "duplicate-pin":
            bad.append(deepcopy(bad[0]))
        elif problem == "trade-id":
            bad[0]["trade_id"] = str(uuid4())
        elif problem == "review-id":
            bad[0]["review_id"] = str(uuid4())
        elif problem == "gap":
            bad[0]["gap"] = "MISSING_RETIRED_ORIGIN"
        elif problem == "version":
            bad[0]["trade_version"] = 2
        elif problem == "version-real":
            bad[0]["trade_version"] = 1.0
        elif problem == "revision-number":
            bad[0]["review_revision"] = 1
        elif problem == "revision-zero":
            bad[0]["review_revision"] = "0"
        elif problem == "revision-leading-zero":
            bad[0]["review_revision"] = "01"
        elif problem == "revision-overflow":
            bad[0]["review_revision"] = "9223372036854775808"
        elif problem == "missing-pin":
            bad.pop(0)
        values["scope_json"] = canonical(bad)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        reject(con, values)
    assert database_snapshot(engine) == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    "problem",
    [
        "owner",
        "root",
        "trade",
        "version",
        "operation",
        "result",
        "request",
        "audit",
        "time",
        "hash",
    ],
)
async def test_scope_header_must_match_fact_owner_and_correlated_audits(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    values, _ = authorization_values(engine, book)
    field, value = {
        "owner": ("owner_id", str(uuid4())),
        "root": ("root_id", str(uuid4())),
        "trade": ("trade_id", str(uuid4())),
        "version": ("trade_version", 2),
        "operation": ("operation", "VOID"),
        "result": ("result_trade_id", values["trade_id"]),
        "request": ("request_id", str(uuid4())),
        "audit": ("audit_id", str(uuid4())),
        "time": ("created_at", "2000-01-01T00:00:00.000000Z"),
        "hash": ("intent_sha256", "f" * 63 + "g"),
    }[problem]
    values[field] = value
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        reject(con, values)
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_withdrawn_review_cannot_be_used_for_a_new_sql_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = authorization_values(engine, book)
    with unit_of_work(engine, write=True) as con:
        trade_history_review.save(
            con,
            UUID(values["owner_id"]),
            UUID(values["trade_id"]),
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=2,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        reject(con, values)
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_later_creation_audit_cannot_enable_a_new_sql_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = authorization_values(engine, book)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade",
            values["trade_id"],
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "late evidence, not a creation-time proof"},
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        reject(con, values)
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_later_withdrawal_preserves_existing_authorization_evidence(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = authorization_values(engine, book)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        trade_history_review.save(
            con,
            UUID(values["owner_id"]),
            UUID(values["trade_id"]),
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=2,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    after = database_snapshot(engine)
    assert after[TABLE] == before[TABLE]
    assert after["trades"] == before["trades"]
    assert after["cash_ledger_entries"] == before["cash_ledger_entries"]
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]


@pytest.mark.anyio
async def test_downgrade_cannot_destroy_authorization_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], tmp_path: Path
) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    values, _ = authorization_values(engine, book)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    before, definitions = database_snapshot(engine), schema(engine)
    assert before[TABLE]
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated"):
            command.downgrade(config, PREVIOUS)
    assert database_snapshot(engine) == before and schema(engine) == definitions
