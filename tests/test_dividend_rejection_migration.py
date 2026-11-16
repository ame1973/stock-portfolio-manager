"""Rejection storage preserves prior decisions and blocks unreviewed financial use."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import audit, stamp
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import migration_config, savepoint, schema_head, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import (
    OTHER,
    OWNER,
    REASON,
    add_posted_receipt,
    entitlement,
)
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, next_intent, owned_account, submit
from tests.test_dividend_projection_migration import snapshot_values
from tests.test_dividend_projection_tax_migration import populate
from tests.test_dividend_tax_migration import setting_values
from tests.test_schedule_migration import upgrade_to

HEAD = "0023_dividend_rejections"
PREVIOUS = "0022_dividend_projection_tax"
TABLE = "dividend_event_rejections"
ACTION = "MIGRATE_DIVIDEND_REJECTIONS"


@pytest.fixture(autouse=True)
def private_database(settings: Settings, tmp_path: Path) -> None:
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


@pytest.fixture
def scenario(engine: Engine, source: dict[str, Any]) -> tuple[Any, str]:
    _, account = owned_account(engine)
    event = manual()
    submit(engine, event)
    return event, account


def rejection_values(con: Connection, event: Any, **changes: Any) -> dict[str, Any]:
    identifier, request = str(uuid4()), str(uuid4())
    created = datetime.now(UTC) + timedelta(seconds=1)
    with patch("spm_api.audit.now", return_value=created):
        audit(
            con,
            str(OWNER),
            "dividend_event_rejection",
            identifier,
            "DIVIDEND_EVENT_REVISION_REJECTED",
            request_id=request,
            before={"synthetic": "unverified prior revision"},
            after={"synthetic": "unverified owner decision"},
            reason=REASON,
        )
    audit_id = con.exec_driver_sql(
        "SELECT id FROM audit_events WHERE entity_type='dividend_event_rejection' AND entity_id=?",
        (identifier,),
    ).scalar_one()
    return {
        "id": identifier,
        "owner_id": str(OWNER),
        "event_id": str(event.event_id),
        "revision_id": str(event.revision_id),
        "revision_no": event.revision_no,
        "citation": "Synthetic rejection reference",
        "source_url": "https://example.test/notice?private=synthetic",
        "reason": REASON,
        "request_id": request,
        "audit_id": audit_id,
        "created_at": stamp(created),
        **changes,
    }


def test_head_adds_empty_rejection_storage_without_inference(engine: Engine) -> None:
    assert schema_head() == "0026_dividend_projection_fee"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_upgrade_preserves_financial_history_and_existing_guards(previous_engine: Engine) -> None:
    populate(previous_engine)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["dividend_entitlements"] and before["dividend_receipts"]
    assert before["dividend_projection_versions"] and before["dividend_tax_settings"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {k: v for k, v in before.items() if k not in {"alembic_version", "audit_events"}} == {
        k: after[k] for k in before if k not in {"alembic_version", "audit_events"}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(item in schema(previous_engine) for item in definitions)
    with unit_of_work(previous_engine) as con:
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_cli_backup_is_an_exact_private_pre_upgrade_snapshot(
    previous_engine: Engine, settings: Settings
) -> None:
    populate(previous_engine)
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
    assert list(settings.data_dir.glob("pre-migration-*.sqlite3")) == backups


@pytest.mark.parametrize("failure", ["late-ddl", "second-owner-audit"])
def test_failed_upgrade_rolls_back_schema_audits_and_facts(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_dividend_rejections_delete BEFORE DELETE ON settings "
                "BEGIN SELECT 1; END"
            )
        else:
            for owner in (OWNER, OTHER):
                con.exec_driver_sql(
                    "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                    "VALUES (?,?,?,?)",
                    (str(owner), "synthetic-only", stamp(), stamp()),
                )
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_rejection_migration_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


def test_nonempty_history_rejects_downgrade_without_mutation(previous_engine: Engine) -> None:
    populate(previous_engine)
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


def test_rejection_preserves_prior_receipt_cash_projection_and_locked_fx(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with unit_of_work(engine, write=True) as con:
        claim = entitlement({"id": str(event.revision_id), "currency": "USD"}, account)
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
        projection = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, "dividend_projection_versions", projection)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        rejected = rejection_values(con, event)
        insert(con, TABLE, rejected)
    after = database_snapshot(engine)
    assert {k: v for k, v in before.items() if k not in {TABLE, "audit_events"}} == {
        k: v for k, v in after.items() if k not in {TABLE, "audit_events"}
    }
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1


@pytest.mark.parametrize(
    ("citation", "source_url"),
    [
        ("x", None),
        ("x" * 300, "https://example.test/" + "x" * (2048 - len("https://example.test/"))),
    ],
)
def test_rejection_accepts_explicit_reference_boundaries(
    engine: Engine, scenario: tuple[Any, str], citation: str, source_url: str | None
) -> None:
    event, _ = scenario
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event, citation=citation, source_url=source_url))
        stored = con.exec_driver_sql(f"SELECT citation,source_url FROM {TABLE}").one()
        assert tuple(stored) == (citation, source_url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-uuid"),
        ("id", str(uuid4()).upper()),
        ("owner_id", str(OTHER)),
        ("event_id", str(uuid4())),
        ("revision_id", str(uuid4())),
        ("revision_no", 2),
        ("revision_no", 0),
        ("revision_no", 2_147_483_648),
        ("revision_no", 1.5),
        ("citation", " "),
        ("citation", "bad\x00reference"),
        ("citation", "x" * 301),
        ("source_url", "bad\x00reference"),
        ("source_url", "x" * 2049),
        ("reason", "short"),
        ("reason", "bad\x00reason with enough length"),
        ("reason", "A different synthetic reason"),
        ("request_id", str(uuid4())),
        ("audit_id", str(uuid4())),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2026-11-16T00:00:00+00:00"),
        ("created_at", "2020-01-01T00:00:00.000000Z"),
    ],
)
def test_rejection_requires_current_owned_audited_exact_scope(
    engine: Engine, scenario: tuple[Any, str], field: str, value: Any
) -> None:
    event, _ = scenario
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event, **{field: value}))
    assert database_snapshot(engine) == before


def test_inactive_owner_cannot_record_a_new_rejection(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "failure",
    [
        "owner",
        "entity_type",
        "entity_id",
        "action",
        "request",
        "reason",
        "missing_before",
        "missing_after",
        "early_audit",
        "duplicate",
    ],
)
def test_rejection_requires_exactly_one_correlated_audit_shape(
    engine: Engine, scenario: tuple[Any, str], failure: str
) -> None:
    event, _ = scenario
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = rejection_values(con, event)
        timestamp = datetime.fromisoformat(values["created_at"].replace("Z", "+00:00"))
        if failure == "early_audit":
            timestamp -= timedelta(seconds=1)
        with patch("spm_api.audit.now", return_value=timestamp):
            audit(
                con,
                str(OTHER if failure == "owner" else OWNER),
                "other_type" if failure == "entity_type" else "dividend_event_rejection",
                str(uuid4()) if failure == "entity_id" else values["id"],
                "OTHER_ACTION" if failure == "action" else "DIVIDEND_EVENT_REVISION_REJECTED",
                request_id=str(uuid4()) if failure == "request" else values["request_id"],
                before=None if failure == "missing_before" else {"synthetic": "before"},
                after=None if failure == "missing_after" else {"synthetic": "after"},
                reason="A different synthetic reason" if failure == "reason" else REASON,
            )
        if failure != "duplicate":
            values["audit_id"] = con.exec_driver_sql(
                "SELECT id FROM audit_events ORDER BY rowid DESC LIMIT 1"
            ).scalar_one()
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_rejection_refuses_ambiguous_original_revision_audit(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            str(event.revision_id),
            "DIVIDEND_EVENT_REVISED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event))
    assert database_snapshot(engine) == before


def test_rejection_is_immutable_and_cannot_be_replaced(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    with unit_of_work(engine, write=True) as con:
        rejected = rejection_values(con, event)
        insert(con, TABLE, rejected)
        for sql in (
            f"UPDATE {TABLE} SET reason=reason WHERE id=?",
            f"DELETE FROM {TABLE} WHERE id=?",
        ):
            with pytest.raises(IntegrityError), savepoint(con):
                con.exec_driver_sql(sql, (rejected["id"],))
        for same_identity in (True, False):
            with pytest.raises(IntegrityError), savepoint(con):
                replacement = rejected if same_identity else rejection_values(con, event)
                columns = ",".join(replacement)
                marks = ",".join("?" for _ in replacement)
                con.exec_driver_sql(
                    f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES ({marks})",
                    tuple(replacement.values()),
                )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='DIVIDEND_EVENT_REVISION_REJECTED'"
            ).scalar_one()
            == 1
        )


def test_rejection_blocks_new_revision_and_leaves_no_partial_history(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        event_writer.save(
            con,
            OWNER,
            next_intent(event, "VERIFY"),
            at=datetime.now(UTC) + timedelta(seconds=2),
            request_id=uuid4(),
        )
    # Bypass only the application read to prove the separate SQL fence on a shaped false audit.
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(rejections, "history", return_value=()),
        pytest.raises(IntegrityError, match="DIVIDEND_EVENT_REJECTION_PENDING"),
    ):
        event_writer.save(
            con,
            OWNER,
            next_intent(event, "VERIFY"),
            at=datetime.now(UTC) + timedelta(seconds=2),
            request_id=uuid4(),
        )
    assert database_snapshot(engine) == before


def test_old_revision_cannot_be_rejected_after_a_successor_is_published(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con,
            OWNER,
            next_intent(event, "VERIFY"),
            at=datetime.now(UTC),
            request_id=uuid4(),
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("target", ["entitlement", "projection", "tax"])
def test_rejection_fences_new_financial_snapshots_and_tax_settings(
    engine: Engine, scenario: tuple[Any, str], target: str
) -> None:
    event, account = scenario
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, rejection_values(con, event))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if target == "entitlement":
            claim = entitlement({"id": str(event.revision_id), "currency": "USD"}, account)
            claim["created_at"] = stamp()
            insert(con, "dividend_entitlements", claim)
        elif target == "projection":
            value = snapshot_values(
                con,
                {"id": str(event.event_id)},
                {"id": str(event.revision_id), "currency": "USD"},
                account,
            )
            insert(con, "dividend_projection_versions", value)
        else:
            insert(con, "dividend_tax_settings", setting_values(con, event, account))
    assert database_snapshot(engine) == before


def test_rejection_and_competing_revision_cannot_both_commit(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    barrier = Barrier(2)

    def reject() -> str:
        barrier.wait()
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, rejection_values(con, event))
            return "rejected"
        except IntegrityError:
            return "rejection_blocked"

    def revise() -> str:
        barrier.wait()
        try:
            with unit_of_work(engine, write=True) as con:
                event_writer.save(
                    con,
                    OWNER,
                    next_intent(event, "VERIFY"),
                    at=datetime.now(UTC) + timedelta(seconds=2),
                    request_id=uuid4(),
                )
            return "revised"
        except events.EventError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(reject), pool.submit(revise)
        outcomes = {first.result(), second.result()}
    assert outcomes in (
        {"rejected", "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"},
        {"rejection_blocked", "revised"},
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT count(*) FROM dividend_event_revisions"
        ).scalar_one() == (1 if "rejected" in outcomes else 2)
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == (
            1 if "rejected" in outcomes else 0
        )
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action='DIVIDEND_EVENT_REVISION_REJECTED'"
        ).scalar_one() == (1 if "rejected" in outcomes else 0)


def test_competing_rejections_append_only_one_decision_and_audit(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    before = database_snapshot(engine)

    def attempt() -> str:
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, rejection_values(con, event))
            return "saved"
        except IntegrityError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["conflict", "saved"]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
