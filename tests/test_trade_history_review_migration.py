"""Unverified trade-history review decisions are inert, scoped and append-only."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api import trade_audit, trade_service
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import migration_config, schema_head, unit_of_work
from spm_api.ledger_service import allocate_sequence
from spm_api.trade_codec import as_trade
from spm_api.trade_models import TradeRecord
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_event_migration import old_engine as old_engine
from tests.test_schedule_migration import upgrade_to
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trades import book as book
from tests.test_trades import payload

HEAD = "0018_trade_history_reviews"
PREVIOUS = "0017_trade_audit_origins"
TABLE = "trade_history_reviews"
ACTION = "MIGRATE_TRADE_HISTORY_REVIEWS"
REASON = "Explicitly acknowledge the missing synthetic trade evidence."
pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_test_database(settings: Any, tmp_path: Path) -> None:
    assert settings.database_path.resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def previous_engine(old_engine: Engine) -> Iterator[Engine]:
    upgrade_to(old_engine, PREVIOUS)
    yield old_engine


def retired_draft(
    engine: Engine,
    book: dict[str, Any],
    *,
    has_creation_audit: bool,
    malformed: Literal["hash", "reason"] | None = None,
) -> tuple[str, TradeRecord]:
    """Create a new synthetic legacy chain, without removing guards or changing old audits."""
    first, second, at = str(uuid4()), str(uuid4()), stamp()
    with unit_of_work(engine, write=True) as con:
        sequence = allocate_sequence(con, "TRADE", first)
        values = {
            "id": first,
            "root_id": first,
            "created_sequence": sequence,
            "portfolio_id": book["portfolio"]["id"],
            "account_id": book["account"]["id"],
            "security_id": book["security"]["id"],
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
        }
        insert(con, "trades", values)
        original = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (first,)).mappings().one()
        retained = as_trade(original)
        if has_creation_audit and malformed == "hash":
            insert(
                con,
                "audit_events",
                {
                    "id": str(uuid4()),
                    "owner_id": book["owner"]["id"],
                    "entity_type": "trade",
                    "entity_id": first,
                    "action": "CREATE",
                    "after_sha256": "x" * 64,
                    "request_id": str(uuid4()),
                    "created_at": at,
                },
            )
        elif has_creation_audit:
            audit(
                con,
                book["owner"]["id"],
                "trade",
                first,
                "CREATE",
                request_id=str(uuid4()),
                after=retained.model_dump(mode="json"),
                reason="unrelated synthetic reason" if malformed == "reason" else "",
            )
        trade_service._retire(con, original, "SUPERSEDED")
        revised_at = stamp()
        insert(
            con,
            "trades",
            {
                **values,
                "id": second,
                "revision": 2,
                "supersedes_id": first,
                "quantity_e8": 1100000000,
                "gross_micro": 110000000,
                "settlement_micro": 110000000,
                "created_at": revised_at,
                "updated_at": revised_at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (second,)).mappings().one()
        )
        audit(
            con,
            book["owner"]["id"],
            "trade",
            second,
            "REVISE",
            request_id=str(uuid4()),
            before=dict(original),
            after=as_trade(successor).model_dump(mode="json"),
            reason="Revise this new synthetic historical trade.",
        )
    return first, retained


def review_values(
    con: Connection,
    owner: str,
    trade_id: str,
    *,
    gap: str = "MISSING_CREATION_AUDIT",
    decision: str = "ACKNOWLEDGE_UNVERIFIED",
    previous: dict[str, Any] | None = None,
    reason: str = REASON,
    audit_action: str | None = None,
) -> dict[str, Any]:
    trade = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade_id,)).mappings().one()
    identifier, request_id = str(uuid4()), str(uuid4())
    action = audit_action or (
        "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED"
        if decision == "ACKNOWLEDGE_UNVERIFIED"
        else "TRADE_HISTORY_UNVERIFIED_WITHDRAWN"
    )
    audit(
        con,
        owner,
        "trade_history_review",
        identifier,
        action,
        request_id=request_id,
        before={"synthetic_prior": True} if previous else None,
        after={"synthetic": "well-shaped but not an authenticated decision"},
        reason=reason,
    )
    recorded = con.exec_driver_sql(
        "SELECT id,created_at FROM audit_events WHERE entity_type='trade_history_review' "
        "AND entity_id=? AND action=?",
        (identifier, action),
    ).one()
    return {
        "id": identifier,
        "trade_id": trade_id,
        "owner_id": owner,
        "revision": 1 if previous is None else previous["revision"] + 1,
        "previous_review_id": None if previous is None else previous["id"],
        "decision": decision,
        "gap": gap,
        "observed_status": trade["status"],
        "observed_trade_version": trade["version"],
        "observed_updated_at": trade["updated_at"],
        "snapshot_sha256": digest(canonical(dict(trade))),
        "request_id": request_id,
        "audit_id": recorded.id,
        "created_at": recorded.created_at,
    }


def test_current_schema_starts_with_no_unverified_review_decisions(engine: Engine) -> None:
    with unit_of_work(engine) as con:
        assert schema_head() == "0021_dividend_tax_settings"
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0


def test_upgrade_preserves_old_facts_audits_schema_receipts_and_fx(previous_engine: Engine) -> None:
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    assert before["trades"] and before["dividend_receipts"] and before["audit_events"]
    upgrade_to(previous_engine, HEAD)
    after = database_snapshot(previous_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {k: v for k, v in before.items() if k not in {"alembic_version", "audit_events"}} == {
        k: after[k] for k in before if k not in {"alembic_version", "audit_events"}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(row in schema(previous_engine) for row in definitions)
    with unit_of_work(previous_engine) as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert con.exec_driver_sql(
            "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
        ).scalar_one() == len(before["owners"])
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    upgrade_to(previous_engine, HEAD)
    assert database_snapshot(previous_engine) == after


@pytest.mark.parametrize("failure", ["late-ddl", "second-owner-audit"])
def test_failed_upgrade_rolls_back_schema_head_facts_and_audits(
    previous_engine: Engine, failure: str
) -> None:
    with previous_engine.begin() as con:
        if failure == "late-ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_trade_history_review_delete BEFORE DELETE ON settings "
                "BEGIN SELECT 1; END"
            )
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_review_migration_failure BEFORE INSERT ON audit_events
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
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE name=?", (TABLE,)
            ).scalar_one()
            == 0
        )


async def test_acknowledge_withdraw_and_reacknowledge_are_append_only(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        first = review_values(con, book["owner"]["id"], trade_id)
        insert(con, TABLE, first)
        withdrawn = review_values(
            con,
            book["owner"]["id"],
            trade_id,
            decision="WITHDRAW",
            previous=first,
        )
        insert(con, TABLE, withdrawn)
        again = review_values(con, book["owner"]["id"], trade_id, previous=withdrawn)
        insert(con, TABLE, again)
    after = database_snapshot(engine)
    assert {k: v for k, v in before.items() if k not in {"audit_events", TABLE}} == {
        k: v for k, v in after.items() if k not in {"audit_events", TABLE}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    with unit_of_work(engine) as con:
        assert [
            row._mapping["decision"]
            for row in con.exec_driver_sql(f"SELECT decision FROM {TABLE} ORDER BY revision")
        ] == ["ACKNOWLEDGE_UNVERIFIED", "WITHDRAW", "ACKNOWLEDGE_UNVERIFIED"]
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-uuid"),
        ("id", str(uuid4()).upper()),
        ("id", str(uuid4()) + "\0tail"),
        ("trade_id", str(uuid4())),
        ("owner_id", str(uuid4())),
        ("owner_id", None),
        ("revision", 0),
        ("revision", 2),
        ("revision", 1.5),
        ("previous_review_id", str(uuid4())),
        ("decision", "WITHDRAW"),
        ("decision", "VERIFIED"),
        ("decision", "ACKNOWLEDGE_UNVERIFIED\0tail"),
        ("gap", "MISSING_RETIRED_ORIGIN"),
        ("gap", "NO_GAP"),
        ("gap", "MISSING_CREATION_AUDIT\0tail"),
        ("observed_status", "VOID"),
        ("observed_status", "DRAFT_SCHEDULED\0tail"),
        ("observed_trade_version", 2),
        ("observed_updated_at", "2020-01-01T00:00:00.000000Z"),
        ("observed_updated_at", "2026-02-30T00:00:00.000000Z"),
        ("observed_updated_at", "2026-01-01T24:00:00.000000Z"),
        ("snapshot_sha256", "x" * 64),
        ("snapshot_sha256", "a" * 63),
        ("snapshot_sha256", "A" * 64),
        ("snapshot_sha256", "a" * 64 + "\0tail"),
        ("request_id", str(uuid4())),
        ("audit_id", str(uuid4())),
        ("created_at", "2026-01-01T00:00:00Z"),
    ],
)
async def test_unscoped_or_malformed_decision_rolls_back_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = review_values(con, book["owner"]["id"], trade_id)
        values[field] = value
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("reason", ["short", " " * 10, "valid but has\0nul", " trailing reason "])
async def test_decision_requires_correlated_bounded_reason(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], reason: str
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, review_values(con, book["owner"]["id"], trade_id, reason=reason))
    assert database_snapshot(engine) == before


async def test_corrupt_existing_creation_audit_cannot_be_called_missing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=True)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, review_values(con, book["owner"]["id"], trade_id))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("has_creation_audit", [False, True])
async def test_retired_missing_evidence_stays_explicitly_unverified(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], has_creation_audit: bool
) -> None:
    trade_id, _ = retired_draft(engine, book, has_creation_audit=has_creation_audit)
    gap = "MISSING_RETIRED_ORIGIN" if has_creation_audit else "MISSING_CREATION_AUDIT"
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        values = review_values(con, book["owner"]["id"], trade_id, gap=gap)
        assert values["observed_status"] == "SUPERSEDED" and values["observed_trade_version"] == 2
        insert(con, TABLE, values)
    after = database_snapshot(engine)
    assert {k: v for k, v in before.items() if k not in {"audit_events", TABLE}} == {
        k: v for k, v in after.items() if k not in {"audit_events", TABLE}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]


async def test_retained_origin_cannot_be_relabelled_as_missing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, review_values(con, book["owner"]["id"], trade["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE_ID", "REPLACE_REVISION"])
async def test_existing_review_cannot_be_mutated_deleted_or_replaced(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        first = review_values(con, book["owner"]["id"], trade_id)
        insert(con, TABLE, first)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "UPDATE":
            con.exec_driver_sql(f"UPDATE {TABLE} SET snapshot_sha256=?", ("0" * 64,))
        elif operation == "DELETE":
            con.exec_driver_sql(f"DELETE FROM {TABLE}")
        else:
            values = review_values(con, book["owner"]["id"], trade_id)
            if operation == "REPLACE_ID":
                values["id"] = first["id"]
            fields = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE}({fields}) VALUES ({placeholders})",
                tuple(values.values()),
            )
    assert database_snapshot(engine) == before


async def test_sql_hash_shape_is_not_proof_of_the_observed_trade_or_actor(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        values = review_values(con, book["owner"]["id"], trade_id)
        values["snapshot_sha256"] = "0" * 64
        insert(con, TABLE, values)
        assert (
            con.exec_driver_sql(
                f"SELECT snapshot_sha256 FROM {TABLE} WHERE id=?", (values["id"],)
            ).scalar_one()
            == "0" * 64
        )


async def test_concurrent_first_decisions_have_one_winner_and_one_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    before = database_snapshot(engine)

    def create_review() -> bool:
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, review_values(con, book["owner"]["id"], trade_id))
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(lambda _: create_review(), range(2)))
    assert sorted(result) == [False, True]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == 1 and len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]


@pytest.mark.parametrize("failure", ["same-decision", "stale-parent", "wrong-gap", "wrong-audit"])
async def test_review_revision_requires_current_parent_transition_and_correlated_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        first = review_values(con, book["owner"]["id"], trade_id)
        insert(con, TABLE, first)
        if failure == "stale-parent":
            withdrawn = review_values(
                con,
                book["owner"]["id"],
                trade_id,
                decision="WITHDRAW",
                previous=first,
            )
            insert(con, TABLE, withdrawn)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        decision = "ACKNOWLEDGE_UNVERIFIED" if failure == "same-decision" else "WITHDRAW"
        gap = "MISSING_RETIRED_ORIGIN" if failure == "wrong-gap" else "MISSING_CREATION_AUDIT"
        values = review_values(
            con,
            book["owner"]["id"],
            trade_id,
            decision=decision,
            previous=first,
            gap=gap,
            audit_action="SYNTHETIC_UNRELATED" if failure == "wrong-audit" else None,
        )
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("malformed", ["hash", "reason"])
async def test_malformed_creation_audit_cannot_be_hidden_by_retired_origin_review(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    malformed: Literal["hash", "reason"],
) -> None:
    trade_id, _ = retired_draft(engine, book, has_creation_audit=True, malformed=malformed)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(
            con,
            TABLE,
            review_values(
                con,
                book["owner"]["id"],
                trade_id,
                gap="MISSING_RETIRED_ORIGIN",
            ),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["foreign-owner", "inactive-owner"])
async def test_review_requires_the_active_trade_owner(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    foreign = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (foreign,),
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if failure == "inactive-owner":
            con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (book["owner"]["id"],))
        owner = foreign if failure == "foreign-owner" else book["owner"]["id"]
        insert(con, TABLE, review_values(con, owner, trade_id))
    assert database_snapshot(engine) == before


async def test_multiple_creation_audits_are_not_a_single_retired_origin_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade_id, _ = retired_draft(engine, book, has_creation_audit=True)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "trade",
            trade_id,
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "a second unrelated creation"},
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(
            con,
            TABLE,
            review_values(
                con,
                book["owner"]["id"],
                trade_id,
                gap="MISSING_RETIRED_ORIGIN",
            ),
        )
    assert database_snapshot(engine) == before


async def test_later_matched_origin_can_be_recovered_and_old_review_withdrawn(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade_id, retained = retired_draft(engine, book, has_creation_audit=True)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        acknowledged = review_values(
            con,
            book["owner"]["id"],
            trade_id,
            gap="MISSING_RETIRED_ORIGIN",
        )
        insert(con, TABLE, acknowledged)
    with unit_of_work(engine, write=True) as con:
        recovered = trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(trade_id),
            retained,
            uuid4(),
            REASON,
        )
        assert recovered.capture_mode == "RETAINED_RESPONSE"
    with unit_of_work(engine, write=True) as con:
        withdrawn = review_values(
            con,
            book["owner"]["id"],
            trade_id,
            gap="MISSING_RETIRED_ORIGIN",
            decision="WITHDRAW",
            previous=acknowledged,
        )
        insert(con, TABLE, withdrawn)
    after = database_snapshot(engine)
    assert after["trades"] == before["trades"]
    assert after["cash_ledger_entries"] == before["cash_ledger_entries"]
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after[TABLE]) == 2 and len(after["trade_audit_origins"]) == 1


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_unattested_sql_review_fails_closed_for_trade_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], method: str
) -> None:
    trade_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, review_values(con, book["owner"]["id"], trade_id))
    before = database_snapshot(engine)
    body = (
        payload(book, trade_date="2999-01-02", fx_rate_hkd=None, quantity="11", reason=REASON)
        if method == "PATCH"
        else {"reason": REASON}
    )
    response = await client.request(
        method, f"/api/v1/trades/{trade_id}", json=body, headers=version_headers(client, 1)
    )
    assert response.status_code == 503 and response.json()["code"] == "TRADE_AUDIT_UNAVAILABLE"
    assert database_snapshot(engine) == before


def test_downgrade_refuses_to_delete_review_history(
    previous_engine: Engine, tmp_path: Path
) -> None:
    assert previous_engine.url.database is not None
    assert Path(previous_engine.url.database).resolve().is_relative_to(tmp_path.resolve())
    upgrade_to(previous_engine, HEAD)
    owner, portfolio, broker, account, trade_id = (str(uuid4()) for _ in range(5))
    at = stamp()
    with previous_engine.begin() as con:
        insert(
            con,
            "owners",
            {
                "id": owner,
                "password_hash": "synthetic",
                "password_changed_at": at,
                "created_at": at,
            },
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
                "security_id": "stock",
                "side": "BUY",
                "trade_date": "2999-01-02",
                "quantity_e8": 100000000,
                "price_e8": 100000000,
                "currency": "USD",
                "gross_micro": 1000000,
                "settlement_micro": 1000000,
                "status": "DRAFT_SCHEDULED",
                "created_at": at,
                "updated_at": at,
            },
        )
        insert(con, TABLE, review_values(con, owner, trade_id))
    before, definitions = database_snapshot(previous_engine), schema(previous_engine)
    with previous_engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated"):
            command.downgrade(config, PREVIOUS)
    assert database_snapshot(previous_engine) == before and schema(previous_engine) == definitions
