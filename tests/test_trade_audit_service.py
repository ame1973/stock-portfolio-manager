"""Internal trade-audit origin checks never authenticate a caller or change ledger facts."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

from spm_api import trade_audit
from spm_api.audit import canonical, digest
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.trade_codec import as_trade
from tests.helpers import create, database_snapshot, version_headers
from tests.test_dividend_eligibility_service import corrupt
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


def identities(
    con: Connection, book: dict[str, Any], trade: dict[str, Any]
) -> tuple[UUID, UUID, UUID]:
    request = con.exec_driver_sql(
        "SELECT request_id FROM audit_events WHERE entity_type='trade' AND entity_id=? "
        "AND action=?",
        (trade["id"], "CREATE" if trade["revision"] == 1 else "REVISE"),
    ).scalar_one()
    return UUID(book["owner"]["id"]), UUID(trade["id"]), UUID(request)


async def test_created_origin_is_exact_and_replay_or_reads_do_not_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, trade)
        result = trade_audit.remember_created(con, owner, identifier, request)
        row = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade["id"],)).mappings().one()
        )
        assert result.before_sha256 == digest(canonical(dict(row)))
        assert (
            result.original_status == "ACTIVE" and result.original_updated_at == trade["updated_at"]
        )
        assert result.capture_mode == "AT_CREATION"
    after = database_snapshot(engine)
    assert {k: v for k, v in before.items() if k != "trade_audit_origins"} == {
        k: v for k, v in after.items() if k != "trade_audit_origins"
    }
    assert trade["id"] not in repr(result) and trade["updated_at"] not in repr(result)
    with (
        patch.object(
            ProviderHTTP, "__init__", side_effect=AssertionError("Unexpected network client")
        ),
        patch.object(
            ProviderConfigurationStore, "load", side_effect=AssertionError("Unexpected file read")
        ),
        unit_of_work(engine, write=True) as con,
    ):
        assert trade_audit.get_origin(con, owner, identifier) == result
        assert trade_audit.remember_created(con, owner, identifier, request) == result
        assert trade_audit.capture_before_retire(con, owner, identifier, uuid4()) == result
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("retirement", ["revision", "void"])
async def test_capture_legacy_current_before_real_retirement_reconstructs_both_audit_hashes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], retirement: str
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        owner, identifier, _ = identities(con, book, trade)
        captured = trade_audit.capture_before_retire(con, owner, identifier, uuid4())
        assert captured.capture_mode == "BEFORE_RETIRE"
    if retirement == "revision":
        response = await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=payload(book, quantity="11", reason="Correct this synthetic trade quantity"),
            headers=version_headers(client, 1),
        )
        successor = response.json().get("id")
        action = "REVISE"
    else:
        response = await client.request(
            "DELETE",
            f"/api/v1/trades/{trade['id']}",
            json={"reason": "Void this synthetic trade record"},
            headers=version_headers(client, 1),
        )
        successor, action = trade["id"], "VOID"
    assert response.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        restored = trade_audit.get_origin(con, owner, identifier)
        assert restored == captured
        retirement_audit = con.exec_driver_sql(
            "SELECT before_sha256 FROM audit_events WHERE entity_type='trade' "
            "AND entity_id=? AND action=?",
            (successor, action),
        ).scalar_one()
        assert retirement_audit == captured.before_sha256
    assert database_snapshot(engine) == before


async def test_scheduled_origin_is_not_guessed_from_the_clock(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book, trade_date="2999-01-02", fx_rate_hkd=None))
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, trade)
        origin = trade_audit.remember_created(con, owner, identifier, request)
    assert origin.original_status == "DRAFT_SCHEDULED"
    response = await client.patch(
        f"/api/v1/trades/{trade['id']}",
        json=payload(book, quantity="11", reason="Replace a synthetic future trade"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    with unit_of_work(engine) as con:
        assert trade_audit.get_origin(con, owner, identifier) == origin
        assert (
            origin.before_sha256
            == con.exec_driver_sql(
                "SELECT before_sha256 FROM audit_events WHERE entity_type='trade' "
                "AND entity_id=? AND action='REVISE'",
                (response.json()["id"],),
            ).scalar_one()
        )


async def test_internal_scope_requires_real_owner_writer_and_correlated_request(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        owner, identifier, request = identities(con, book, trade)
        assert trade_audit.get_origin(con, owner, identifier) is None
        assert trade_audit.get_origin(con, owner, uuid4()) is None
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_WRITE_REQUIRED$"):
            trade_audit.remember_created(con, owner, identifier, request)
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_OWNER_UNAVAILABLE$"):
            trade_audit.get_origin(con, uuid4(), identifier)
    with (
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.remember_created(con, owner, identifier, uuid4())
    with (
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_NOT_FOUND$"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.capture_before_retire(con, owner, uuid4(), uuid4())
    with (
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_NOT_FOUND$"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.remember_created(con, owner, uuid4(), request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "table,mutation",
    [
        ("trade_audit_origins", "timestamp"),
        ("trade_audit_origins", "status"),
        ("trade_audit_origins", "owner"),
        ("trade_audit_origins", "creation-id"),
        ("trade_audit_origins", "mode"),
        ("trade_audit_origins", "capture-without-audit"),
        ("trade_audit_origins", "request"),
        ("trade_audit_origins", "capture-time"),
        ("audit_events", "creation-hash"),
        ("audit_events", "creation-request"),
        ("audit_events", "creation-request-format"),
        ("audit_events", "creation-request-uppercase"),
        ("audit_events", "creation-time-invalid"),
        ("audit_events", "creation-before"),
        ("audit_events", "creation-reason"),
        ("trades", "quantity"),
        ("trades", "quantity-real"),
        ("trades", "sequence"),
        ("trades", "revision-fraction"),
        ("trades", "version"),
        ("trades", "execution-time"),
    ],
)
async def test_corrupted_created_origin_and_its_fact_fail_closed_without_repair(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], table: str, mutation: str
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, trade)
        origin = trade_audit.remember_created(con, owner, identifier, request)
    with corrupt(engine, table) as con:
        value: Any
        if table == "trade_audit_origins":
            column, value = {
                "timestamp": ("original_updated_at", "2000-01-01T00:00:00.000000Z"),
                "status": ("original_status", "DRAFT_SCHEDULED"),
                "owner": ("owner_id", str(uuid4())),
                "creation-id": ("creation_audit_id", str(uuid4())),
                "mode": ("capture_mode", "RETAINED_RESPONSE"),
                "capture-without-audit": ("capture_mode", "BEFORE_RETIRE"),
                "request": ("capture_request_id", str(uuid4())),
                "capture-time": ("captured_at", "2000-01-01T00:00:00.000000Z"),
            }[mutation]
            con.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
            con.exec_driver_sql(
                f"UPDATE trade_audit_origins SET {column}=? WHERE trade_id=?",
                (value, str(identifier)),
            )
        elif table == "audit_events":
            column, value = {
                "creation-hash": ("after_sha256", "0" * 64),
                "creation-request": ("request_id", str(uuid4())),
                "creation-request-format": ("request_id", "not-a-uuid"),
                "creation-request-uppercase": (
                    "request_id",
                    "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                ),
                "creation-time-invalid": ("created_at", "2026-02-30T00:00:00.000000Z"),
                "creation-before": ("before_sha256", "a" * 64),
                "creation-reason": ("reason", "tampered metadata"),
            }[mutation]
            con.exec_driver_sql(
                f"UPDATE audit_events SET {column}=? WHERE id=?",
                (value, str(origin.creation_audit_id)),
            )
        else:
            column, value = {
                "quantity": ("quantity_e8", 999999999),
                "quantity-real": ("quantity_e8", 999.5),
                "sequence": ("created_sequence", 999999),
                "revision-fraction": ("revision", 1.5),
                "version": ("version", 2),
                "execution-time": ("executed_at_utc", "2026-01-02T00:00:00+00:00"),
            }[mutation]
            con.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
            con.exec_driver_sql(
                f"UPDATE trades SET {column}=? WHERE id=?", (value, str(identifier))
            )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.get_origin(con, owner, identifier)


@pytest.mark.parametrize(
    "field", ["after_sha256", "reason", "request_id", "created_at", "owner_id"]
)
async def test_capture_audit_is_canonical_and_bound_to_origin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        owner, identifier, _ = identities(con, book, trade)
        origin = trade_audit.capture_before_retire(con, owner, identifier, uuid4())
        assert origin.capture_audit_id is not None
    with corrupt(engine, "audit_events") as con:
        value = {
            "after_sha256": "0" * 64,
            "reason": "Changed but valid audit reason",
            "request_id": str(uuid4()),
            "created_at": "2000-01-01T00:00:00.000000Z",
            "owner_id": str(uuid4()),
        }[field]
        con.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        con.exec_driver_sql(
            f"UPDATE audit_events SET {field}=? WHERE id=?", (value, str(origin.capture_audit_id))
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.get_origin(con, owner, identifier)


async def test_concurrent_capture_has_one_origin_and_one_capture_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine) as con:
        owner, identifier, _ = identities(con, book, trade)

    def capture(_: int) -> trade_audit.TradeAuditOrigin:
        with unit_of_work(engine, write=True) as con:
            return trade_audit.capture_before_retire(con, owner, identifier, uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(capture, range(2)))
    assert first == second
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='trade_audit_origin' "
                "AND entity_id=? AND action='TRADE_AUDIT_ORIGIN_CAPTURED'",
                (trade["id"],),
            ).scalar_one()
            == 1
        )


async def test_late_failure_rolls_back_capture_and_audit_together(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        unit_of_work(engine, write=True) as con,
    ):
        owner, identifier, _ = identities(con, book, trade)
        trade_audit.capture_before_retire(con, owner, identifier, uuid4())
        raise RuntimeError("synthetic failure")
    assert database_snapshot(engine) == before


async def test_revised_trade_and_later_void_keep_exact_original_before_hash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = await create(client, "trades", payload(book))
    response = await client.patch(
        f"/api/v1/trades/{first['id']}",
        json=payload(book, quantity="11", reason="Correct the synthetic share quantity"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    revised = response.json()
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, revised)
        origin = trade_audit.remember_created(con, owner, identifier, request)
    void = await client.request(
        "DELETE",
        f"/api/v1/trades/{revised['id']}",
        json={"reason": "Void the synthetic revised trade"},
        headers=version_headers(client, 1),
    )
    assert void.status_code == 200
    with unit_of_work(engine) as con:
        assert trade_audit.get_origin(con, owner, identifier) == origin
        assert (
            origin.before_sha256
            == con.exec_driver_sql(
                "SELECT before_sha256 FROM audit_events WHERE entity_type='trade' "
                "AND entity_id=? AND action='VOID'",
                (revised["id"],),
            ).scalar_one()
        )


async def test_missing_foreign_and_inactive_owners_do_not_grant_access(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        owner, identifier, _ = identities(con, book, trade)
        foreign = uuid4()
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(foreign),),
        )
        assert trade_audit.get_origin(con, foreign, identifier) is None
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_NOT_FOUND$"):
            trade_audit.capture_before_retire(con, foreign, identifier, uuid4())
    with corrupt(engine, "owners") as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(owner),))
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_OWNER_UNAVAILABLE$"):
            trade_audit.get_origin(con, owner, identifier)


async def test_unassociated_or_duplicate_audits_are_not_silently_adopted(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    from spm_api.audit import audit

    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine) as con:
        owner, identifier, _ = identities(con, book, trade)
    with corrupt(engine, "audit_events") as con:
        audit(
            con,
            str(owner),
            "trade_audit_origin",
            str(identifier),
            "TRADE_AUDIT_ORIGIN_CAPTURED",
            request_id=str(uuid4()),
            after={"synthetic": True},
            reason="Unassociated synthetic capture",
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.capture_before_retire(con, owner, identifier, uuid4())
    with unit_of_work(engine, write=True) as con:
        original = trade_audit.capture_before_retire(con, owner, identifier, uuid4())
    with corrupt(engine, "audit_events") as con:
        audit(
            con,
            str(owner),
            "trade_audit_origin",
            str(identifier),
            "TRADE_AUDIT_ORIGIN_CAPTURED",
            request_id=str(uuid4()),
            after={"synthetic": True},
            reason="Duplicate synthetic capture",
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.get_origin(con, owner, identifier)
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        con.exec_driver_sql(
            "DELETE FROM audit_events WHERE id=?", (str(original.capture_audit_id),)
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.get_origin(con, owner, identifier)
    with corrupt(engine, "audit_events") as con:
        audit(
            con,
            str(owner),
            "trade",
            str(identifier),
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": True},
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.get_origin(con, owner, identifier)


async def test_retired_or_conflicting_origin_cannot_be_claimed_as_newly_created(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, trade)
        trade_audit.capture_before_retire(con, owner, identifier, uuid4())
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.remember_created(con, owner, identifier, request)
    response = await client.request(
        "DELETE",
        f"/api/v1/trades/{trade['id']}",
        json={"reason": "Void this synthetic recorded trade"},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.remember_created(con, owner, identifier, request)
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.capture_before_retire(con, owner, identifier, uuid4())
    assert database_snapshot(engine) == before


async def test_invalid_scope_and_request_are_fixed_private_errors(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    from typing import cast

    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine) as con:
        owner, identifier, _ = identities(con, book, trade)
        with pytest.raises(trade_audit.TradeAuditError, match="^INVALID_TRADE_AUDIT_SCOPE$"):
            trade_audit.get_origin(con, cast(UUID, "private-invalid"), identifier)
    with engine.connect() as con:
        with pytest.raises(trade_audit.TradeAuditError, match="^INVALID_TRADE_AUDIT_SCOPE$"):
            trade_audit.get_origin(con, owner, identifier)
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_WRITE_REQUIRED$"):
            trade_audit.capture_before_retire(con, owner, identifier, uuid4())
    with unit_of_work(engine, write=True) as con:
        for func in (trade_audit.remember_created, trade_audit.capture_before_retire):
            with pytest.raises(trade_audit.TradeAuditError, match="^INVALID_TRADE_AUDIT_SCOPE$"):
                func(con, owner, identifier, cast(UUID, "private-invalid"))


@pytest.mark.parametrize("mode", ["remember", "capture"])
async def test_unmatched_current_audit_cannot_be_pinned(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], mode: str
) -> None:
    trade = await create(client, "trades", payload(book))
    with unit_of_work(engine) as con:
        owner, identifier, request = identities(con, book, trade)
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? WHERE entity_type='trade' "
            "AND entity_id=? AND action='CREATE'",
            ("0" * 64, trade["id"]),
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            if mode == "remember":
                trade_audit.remember_created(con, owner, identifier, request)
            else:
                trade_audit.capture_before_retire(con, owner, identifier, uuid4())
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 0


async def test_raw_override_flag_cannot_be_coerced_into_an_existing_boolean_hash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = await create(
        client,
        "trades",
        payload(
            book,
            actual_settlement="1000",
            override_reason="Synthetic confirmed settlement evidence",
        ),
    )
    assert trade["settlement_override"] is True
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, trade)
        trade_audit.remember_created(con, owner, identifier, request)
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            con.exec_driver_sql(
                "UPDATE trades SET settlement_override=2 WHERE id=?", (trade["id"],)
            )
            assert (
                as_trade(
                    con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade["id"],))
                    .mappings()
                    .one()
                ).settlement_override
                is True
            )
            with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
                trade_audit.get_origin(con, owner, identifier)
        finally:
            con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")


async def test_revised_fact_cannot_be_relabelled_as_its_own_root(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = await create(client, "trades", payload(book))
    response = await client.patch(
        f"/api/v1/trades/{first['id']}",
        json=payload(book, quantity="11", reason="Revise synthetic recorded trade"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    revised = response.json()
    with unit_of_work(engine, write=True) as con:
        owner, identifier, request = identities(con, book, revised)
        origin = trade_audit.remember_created(con, owner, identifier, request)
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql("DROP TRIGGER trg_ledger_sequences_update")
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql("UPDATE trades SET root_id=id WHERE id=?", (str(identifier),))
        con.exec_driver_sql(
            "UPDATE ledger_sequences SET fact_id=? WHERE sequence=?",
            (str(identifier), revised["created_sequence"]),
        )
        mutated = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(identifier),))
            .mappings()
            .one()
        )
        forged = digest(canonical(as_trade(mutated).model_dump(mode="json")))
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? WHERE id=?",
            (forged, str(origin.creation_audit_id)),
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.get_origin(con, owner, identifier)


async def test_retained_response_sql_claim_is_not_an_installed_recovery_workflow(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    from tests.provider_evidence_helpers import insert
    from tests.test_trade_audit_origin_migration import capture_values, origin_values

    trade = await create(client, "trades", payload(book))
    response = await client.patch(
        f"/api/v1/trades/{trade['id']}",
        json=payload(book, quantity="11", reason="Revise synthetic recorded trade"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    with unit_of_work(engine, write=True) as con:
        owner, identifier, _ = identities(con, book, trade)
        values = origin_values(con, str(owner), str(identifier))
        values.update(original_status=trade["status"], original_updated_at=trade["updated_at"])
        insert(con, "trade_audit_origins", capture_values(con, values, "RETAINED_RESPONSE"))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"),
    ):
        trade_audit.get_origin(con, owner, identifier)
    assert database_snapshot(engine) == before
