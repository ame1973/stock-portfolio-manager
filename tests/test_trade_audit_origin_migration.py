"""Trade-origin metadata is inert, scoped and immutable; it is not proof by itself."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.audit import audit, canonical, digest, stamp
from spm_api.config import Settings
from spm_api.db import migration_config, schema_head, unit_of_work
from spm_api.ledger_service import allocate_sequence
from spm_api.trade_service import as_trade
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_basis_migration import SECURITY
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_schedule_migration import upgrade_to
from tests.test_trades import book as book
from tests.test_trades import payload

HEAD = "0017_trade_audit_origins"
PREVIOUS = "0016_dividend_event_controls"
TABLE = "trade_audit_origins"
ACTION = "MIGRATE_TRADE_AUDIT_ORIGINS"
REASON = "Preserve the original synthetic trade audit inputs."
pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def legacy_trade_writer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Exercise storage in an exclusive temporary DB without automatic writer capture."""
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setattr("spm_api.trade_service._remember_trade_origin", lambda *args: None)
    monkeypatch.setattr("spm_api.trade_service._capture_trade_origin", lambda *args: None)


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def origin_values(con: Connection, owner: str, trade_id: str) -> dict[str, Any]:
    trade = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade_id,)).mappings().one()
    event = (
        con.exec_driver_sql(
            "SELECT * FROM audit_events WHERE entity_type='trade' AND entity_id=? AND action=?",
            (trade_id, "CREATE" if trade["revision"] == 1 else "REVISE"),
        )
        .mappings()
        .one()
    )
    return {
        "trade_id": trade_id,
        "owner_id": owner,
        "creation_audit_id": event["id"],
        "original_status": trade["status"],
        "original_updated_at": trade["updated_at"],
        "capture_mode": "AT_CREATION",
        "capture_request_id": event["request_id"],
        "capture_audit_id": None,
        "captured_at": event["created_at"],
    }


def capture_values(
    con: Connection, origin: dict[str, Any], mode: str, *, reason: str = REASON
) -> dict[str, Any]:
    request_id = str(uuid4())
    action = (
        "TRADE_AUDIT_ORIGIN_CAPTURED" if mode == "BEFORE_RETIRE" else "TRADE_AUDIT_ORIGIN_RECOVERED"
    )
    audit(
        con,
        origin["owner_id"],
        "trade_audit_origin",
        origin["trade_id"],
        action,
        request_id=request_id,
        after={"synthetic": "not a verified original"},
        reason=reason,
    )
    recorded = con.exec_driver_sql(
        "SELECT id,created_at FROM audit_events WHERE entity_type='trade_audit_origin' "
        "AND entity_id=? AND action=?",
        (origin["trade_id"], action),
    ).one()
    return {
        **origin,
        "capture_mode": mode,
        "capture_request_id": request_id,
        "capture_audit_id": recorded.id,
        "captured_at": recorded.created_at,
    }


def rejected(engine: Engine, values: dict[str, Any]) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_upgrade_preserves_legacy_facts_schema_receipts_and_fx(previous_engine: Engine) -> None:
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["trades"] and before["dividend_receipts"] and before["audit_events"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {
        name: after[name] for name in before if name not in {"alembic_version", "audit_events"}
    } == {
        name: rows
        for name, rows in before.items()
        if name not in {"alembic_version", "audit_events"}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(row in schema(previous_engine) for row in definitions)
    with unit_of_work(previous_engine) as con:
        assert schema_head() == "0020_dividend_projection_history"
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


def test_downgrade_refuses_to_remove_retained_origin(previous_engine: Engine) -> None:
    # Exercise the historical revision directly, not the later head's downgrade guard.
    upgrade_to(previous_engine, HEAD)
    owner, portfolio, broker, account, trade_id = (str(uuid4()) for _ in range(5))
    at = stamp()
    with previous_engine.begin() as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic',?,?)",
            (owner, at, at),
        )
        insert(
            con,
            "portfolios",
            {"id": portfolio, "owner_id": owner, "name": "Synthetic", "created_at": at},
        )
        insert(con, "brokers", {"id": broker, "owner_id": owner, "name": "Synthetic"})
        insert(
            con,
            "accounts",
            {
                "id": account,
                "portfolio_id": portfolio,
                "broker_id": broker,
                "name": "Synthetic",
                "default_currency": "USD",
                "created_at": at,
            },
        )
        sequence = allocate_sequence(con, "TRADE", trade_id)
        insert(
            con,
            "trades",
            {
                "id": trade_id,
                "root_id": trade_id,
                "created_sequence": sequence,
                "portfolio_id": portfolio,
                "account_id": account,
                "security_id": str(SECURITY),
                "side": "BUY",
                "trade_date": "2999-01-02",
                "quantity_e8": 1000000000,
                "price_e8": 1000000000,
                "currency": "USD",
                "gross_micro": 100000000,
                "settlement_micro": 100000000,
                "status": "DRAFT_SCHEDULED",
                "created_at": at,
                "updated_at": at,
            },
        )
        trade = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade_id,)).mappings().one()
        audit(
            con,
            owner,
            "trade",
            trade_id,
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(trade).model_dump(mode="json"),
        )
        insert(con, TABLE, origin_values(con, owner, trade_id))
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with previous_engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated"):
            command.downgrade(config, PREVIOUS)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions


@pytest.mark.parametrize("failure", ["late-ddl", "second-owner-audit"])
def test_failed_upgrade_rolls_back_schema_head_facts_and_all_audits(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_trade_audit_origin_delete BEFORE DELETE ON settings "
                "BEGIN SELECT 1; END"
            )
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_origin_audit_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == before
    assert schema(previous_engine) == definitions
    with previous_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == PREVIOUS
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE name=?", (TABLE,)
            ).scalar_one()
            == 0
        )


async def test_current_trade_origin_retains_original_audit_without_changing_financial_facts(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        event_hash = con.exec_driver_sql(
            "SELECT after_sha256 FROM audit_events WHERE id=?", (values["creation_audit_id"],)
        ).scalar_one()
        assert event_hash == digest(canonical(trade))
        insert(con, TABLE, values)
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name != TABLE} == {
        name: rows for name, rows in after.items() if name != TABLE
    }
    with unit_of_work(engine) as con:
        assert dict(con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()) == values


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trade_id", "not-a-uuid"),
        ("trade_id", str(uuid4())),
        ("trade_id", str(uuid4()).upper()),
        ("trade_id", str(uuid4()) + "\0tail"),
        ("owner_id", str(uuid4())),
        ("owner_id", None),
        ("creation_audit_id", str(uuid4())),
        ("creation_audit_id", str(uuid4()) + "\0tail"),
        ("original_status", "DRAFT_SCHEDULED"),
        ("original_status", "ACTIVE\0tail"),
        ("original_status", "VOID"),
        ("original_status", None),
        ("original_updated_at", "2020-01-01T00:00:00.000000Z"),
        ("original_updated_at", "2026-02-30T00:00:00.000000Z"),
        ("original_updated_at", "0000-01-01T00:00:00.000000Z"),
        ("original_updated_at", "2026-01-01T24:00:00.000000Z"),
        ("original_updated_at", "2026-01-01T00:00:00Z"),
        ("original_updated_at", "2026-01-01T00:00:00.000000Z\0tail"),
        ("capture_mode", "BEFORE_RETIRE"),
        ("capture_mode", "RETAINED_RESPONSE"),
        ("capture_mode", "AT_CREATION\0tail"),
        ("capture_request_id", str(uuid4())),
        ("capture_request_id", str(uuid4()) + "\0tail"),
        ("capture_audit_id", str(uuid4())),
        ("captured_at", "2020-01-01T00:00:00.000000Z"),
        ("captured_at", "2026-01-01T00:60:00.000000Z"),
        ("captured_at", "2026-01-01T00:00:00.000000+00:00"),
    ],
)
async def test_unproven_or_malformed_origin_is_rejected_without_writes(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
    rejected(engine, {**values, field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", None),
        ("request_id", "not-a-uuid"),
        ("request_id", str(uuid4()) + "\0tail"),
        ("after_sha256", None),
        ("after_sha256", "a" * 63),
        ("after_sha256", "F" * 64),
        ("after_sha256", "a" * 64 + "\0tail"),
        ("before_sha256", "a" * 64),
        ("entity_type", "expense"),
        ("entity_id", str(uuid4())),
        ("action", "REVISE"),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2026-01-01T00:00:00.000000Z\0tail"),
    ],
)
async def test_invalid_original_audit_cannot_be_used_as_evidence(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            f"UPDATE audit_events SET {field}=? WHERE id=?", (value, values["creation_audit_id"])
        )
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


async def test_duplicate_creation_audit_rejects_ambiguous_origin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        audit(
            con,
            book["owner"]["id"],
            "trade",
            trade["id"],
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": True},
        )
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
async def test_origin_cannot_be_updated_deleted_or_replaced(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        insert(con, TABLE, values)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="IMMUTABLE"), unit_of_work(engine, write=True) as con:
        if operation == "UPDATE":
            con.exec_driver_sql(f"UPDATE {TABLE} SET original_status=original_status")
        elif operation == "DELETE":
            con.exec_driver_sql(f"DELETE FROM {TABLE}")
        else:
            names = ",".join(values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({names}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("conflict", ["creation_audit_id", "capture_audit_id"])
async def test_replace_cannot_bypass_other_unique_keys(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], conflict: str
) -> None:
    first = await create(client, "trades", payload(book))
    second = await create(client, "trades", payload(book, trade_date="2026-01-03"))
    with unit_of_work(engine, write=True) as con:
        old = origin_values(con, book["owner"]["id"], first["id"])
        old = capture_values(con, old, "BEFORE_RETIRE")
        insert(con, TABLE, old)
        new = origin_values(con, book["owner"]["id"], second["id"])
    before = database_snapshot(engine)
    new[conflict] = old[conflict]
    with pytest.raises(IntegrityError, match="IMMUTABLE"), unit_of_work(engine, write=True) as con:
        names = ",".join(new)
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO {TABLE} ({names}) VALUES ({','.join('?' for _ in new)})",
            tuple(new.values()),
        )
    assert database_snapshot(engine) == before


async def test_capture_before_retirement_survives_real_trade_revision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], original["id"])
        values = capture_values(con, values, "BEFORE_RETIRE")
        insert(con, TABLE, values)
    revised = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=payload(book, quantity="11", reason="Revise this synthetic recorded trade"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    with unit_of_work(engine, write=True) as con:
        assert dict(con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()) == values
        successor = origin_values(con, book["owner"]["id"], revised.json()["id"])
        insert(con, TABLE, successor)
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 2


@pytest.mark.parametrize("mutation", ["request", "time", "reason", "action", "hash", "duplicate"])
async def test_before_retire_requires_correlated_capture_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], mutation: str
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        values = capture_values(con, values, "BEFORE_RETIRE")
        if mutation == "request":
            values["capture_request_id"] = str(uuid4())
        elif mutation == "time":
            values["captured_at"] = "2020-01-01T00:00:00.000000Z"
        elif mutation == "duplicate":
            audit(
                con,
                book["owner"]["id"],
                "trade_audit_origin",
                trade["id"],
                "TRADE_AUDIT_ORIGIN_CAPTURED",
                request_id=str(uuid4()),
                after={"synthetic": True},
                reason=REASON,
            )
        else:
            column, changed = {
                "reason": ("reason", "too short"),
                "action": ("action", "TRADE_AUDIT_ORIGIN_RECOVERED"),
                "hash": ("after_sha256", "F" * 64),
            }[mutation]
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            con.exec_driver_sql(
                f"UPDATE audit_events SET {column}=? WHERE id=?",
                (changed, values["capture_audit_id"]),
            )
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


async def test_recovered_origin_sql_is_not_proof_of_the_original_audit_hash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    response = await client.patch(
        f"/api/v1/trades/{trade['id']}",
        json=payload(book, quantity="11", reason="Revise this synthetic recorded trade"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        values.update(original_status="ACTIVE", original_updated_at="2000-01-01T00:00:00.000000Z")
        values = capture_values(con, values, "RETAINED_RESPONSE")
        insert(con, TABLE, values)
        row = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade["id"],)).mappings().one()
        )
        claimed = as_trade(row).model_copy(
            update={
                "status": values["original_status"],
                "version": 1,
                "updated_at": values["original_updated_at"],
            }
        )
        original_hash = con.exec_driver_sql(
            "SELECT after_sha256 FROM audit_events WHERE id=?", (values["creation_audit_id"],)
        ).scalar_one()
        assert digest(canonical(claimed.model_dump(mode="json"))) != original_hash


async def test_competing_origin_inserts_allow_only_one_immutable_record(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])

    def attempt() -> str:
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, values)
        except IntegrityError:
            return "rejected"
        return "inserted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["inserted", "rejected"]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 1


async def test_capture_audit_and_origin_roll_back_together_on_late_failure(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        unit_of_work(engine, write=True) as con,
    ):
        values = origin_values(con, book["owner"]["id"], trade["id"])
        insert(con, TABLE, capture_values(con, values, "BEFORE_RETIRE"))
        raise RuntimeError("synthetic failure")
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "inactive-owner",
        "foreign-broker",
        "foreign-audit",
        "sequence-kind",
        "sequence-root",
        "fractional-revision",
    ],
)
async def test_owner_scope_and_actual_ledger_sequence_are_fenced(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], trade["id"])
        if corruption in {"foreign-broker", "foreign-audit"}:
            other = str(uuid4())
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic',?,?)",
                (other, values["captured_at"], values["captured_at"]),
            )
        if corruption == "inactive-owner":
            con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (values["owner_id"],))
        elif corruption == "foreign-broker":
            broker = str(uuid4())
            con.exec_driver_sql(
                "INSERT INTO brokers(id,owner_id,name) VALUES (?,?,'Other')", (broker, other)
            )
            con.exec_driver_sql("DROP TRIGGER trg_account_owner_update")
            con.exec_driver_sql(
                "UPDATE accounts SET broker_id=? WHERE id=?", (broker, trade["account_id"])
            )
        elif corruption == "foreign-audit":
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            con.exec_driver_sql(
                "UPDATE audit_events SET owner_id=? WHERE id=?",
                (other, values["creation_audit_id"]),
            )
        elif corruption in {"sequence-kind", "sequence-root"}:
            con.exec_driver_sql("DROP TRIGGER trg_ledger_sequences_update")
            column, value = (
                ("kind", "TRANSFER") if corruption == "sequence-kind" else ("fact_id", str(uuid4()))
            )
            con.exec_driver_sql(
                f"UPDATE ledger_sequences SET {column}=? WHERE sequence=?",
                (value, trade["created_sequence"]),
            )
        else:
            con.exec_driver_sql("DROP TRIGGER trg_ledger_trades_immutable")
            con.exec_driver_sql("UPDATE trades SET revision=1.5 WHERE id=?", (trade["id"],))
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


async def test_scheduled_original_status_can_be_preserved_without_fx_or_cash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(
        client, "trades", payload(book, trade_date="2999-01-02", fx_rate_hkd=None)
    )
    assert original["status"] == "DRAFT_SCHEDULED" and original["booked_hkd"] is None
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, book["owner"]["id"], original["id"])
        assert values["original_status"] == "DRAFT_SCHEDULED"
        insert(con, TABLE, values)
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 0
    revised = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=payload(
            book,
            trade_date="2999-01-02",
            quantity="11",
            fx_rate_hkd=None,
            reason="Revise this synthetic future trade",
        ),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200 and revised.json()["status"] == "DRAFT_SCHEDULED"
    with unit_of_work(engine) as con:
        assert dict(con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()) == values


async def test_legacy_writer_does_not_automatically_capture_or_recover_old_origin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    revised = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=payload(book, quantity="11", reason="Revise this synthetic recorded trade"),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        retired = origin_values(con, book["owner"]["id"], original["id"])
    rejected(engine, {**retired, "original_status": "ACTIVE", "capture_mode": "BEFORE_RETIRE"})
