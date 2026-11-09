"""Recover retired trade fingerprints only from matching retained responses."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api import trade_audit, trade_service
from spm_api.audit import audit, canonical, digest
from spm_api.db import unit_of_work
from spm_api.dividend_eligibility_service import get as eligibility
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.trade_models import TradeRecord
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_origin_migration import origin_values
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio
REASON = "Recover the original synthetic trade response."


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


async def legacy_trade(
    client: httpx.AsyncClient,
    book: dict[str, Any],
    *,
    retirement: str = "revision",
    scheduled: bool = False,
    unrelated_before: bool = False,
    creation_audit: str = "matching",
) -> dict[str, Any]:
    values = payload(
        book, **({"trade_date": "2999-01-02", "fx_rate_hkd": None} if scheduled else {})
    )

    def synthetic_audit(*args: Any, **kwargs: Any) -> None:
        if args[2] == "trade" and args[4] == "CREATE":
            if creation_audit == "missing":
                return
            if creation_audit == "unrelated":
                kwargs["after"] = {"synthetic": "not the original response"}
        if unrelated_before and args[2] == "trade" and args[4] in {"REVISE", "VOID"}:
            kwargs["before"] = {"synthetic": "not the predecessor"}
        audit(*args, **kwargs)

    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
        patch.object(trade_service, "audit", side_effect=synthetic_audit),
    ):
        trade = await create(client, "trades", values)
        if retirement == "revision":
            response = await client.patch(
                f"/api/v1/trades/{trade['id']}",
                json={**values, "quantity": "11", "reason": "Revise a synthetic legacy trade"},
                headers=version_headers(client, 1),
            )
        else:
            response = await client.request(
                "DELETE",
                f"/api/v1/trades/{trade['id']}",
                json={"reason": "Void a synthetic legacy trade"},
                headers=version_headers(client, 1),
            )
    assert response.status_code == 200
    return trade


@pytest.mark.parametrize(
    "retirement,scheduled", [("revision", False), ("void", False), ("revision", True)]
)
async def test_retained_response_recovers_exact_retirement_without_financial_writes(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    retirement: str,
    scheduled: bool,
) -> None:
    original = await legacy_trade(client, book, retirement=retirement, scheduled=scheduled)
    owner, identifier, request = UUID(book["owner"]["id"]), UUID(original["id"]), uuid4()
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        prior = eligibility(
            con,
            owner,
            UUID(book["portfolio"]["id"]),
            UUID(book["security"]["id"]),
            ex_date=date(2026, 1, 5),
            as_of=date(2026, 1, 5),
            rule="ORDINARY_PRE_EX_DATE",
        )
        assert (
            prior is not None
            and prior.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
        )
    with unit_of_work(engine, write=True) as con:
        recovered = trade_audit.recover_from_response(
            con, owner, identifier, TradeRecord.model_validate(original), request, REASON
        )
        assert recovered.capture_mode == "RETAINED_RESPONSE"
        assert recovered.original_status == original["status"]
        assert recovered.original_updated_at == original["updated_at"]
    after = database_snapshot(engine)
    changed = {"audit_events", "trade_audit_origins", "portfolios", "projection_invalidations"}
    assert {k: v for k, v in before.items() if k not in changed} == {
        k: v for k, v in after.items() if k not in changed
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    with unit_of_work(engine, write=True) as con:
        invalidations = con.exec_driver_sql(
            "SELECT portfolio_id,source_id,from_date,reason FROM projection_invalidations "
            "WHERE source_id=? AND reason='TRADE_AUDIT_ORIGIN_RECOVERED'",
            (str(identifier),),
        ).all()
        assert [tuple(row) for row in invalidations] == [
            (
                book["portfolio"]["id"],
                str(identifier),
                original["trade_date"],
                "TRADE_AUDIT_ORIGIN_RECOVERED",
            )
        ]
        assert trade_audit.get_origin(con, owner, identifier) == recovered
        assert (
            trade_audit.recover_from_response(
                con, owner, identifier, TradeRecord.model_validate(original), request, REASON
            )
            == recovered
        )
        result = eligibility(
            con,
            owner,
            UUID(book["portfolio"]["id"]),
            UUID(book["security"]["id"]),
            ex_date=date(2026, 1, 5),
            as_of=date(2026, 1, 5),
            rule="ORDINARY_PRE_EX_DATE",
        )
        assert (
            result is not None
            and result.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED"
        )
        assert result.data_revision == prior.data_revision + 1
        assert all(account.history_completeness == "UNVERIFIED" for account in result.accounts)
    assert database_snapshot(engine) == after


async def test_recovery_invalidates_every_owned_portfolio_in_the_trade_root(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    other = await create(client, "portfolios", {"name": "Other synthetic recovery portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other["id"],
            "broker_id": book["broker"]["id"],
            "name": "Other synthetic recovery account",
            "default_currency": "USD",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "RECOV",
            "name": "Other synthetic security",
            "quote_currency": "USD",
        },
    )
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
    ):
        original = await create(client, "trades", payload(book))
        response = await client.patch(
            f"/api/v1/trades/{original['id']}",
            json=payload(
                book,
                portfolio_id=other["id"],
                account_id=account["id"],
                security_id=security["id"],
                trade_date="2025-12-31",
                reason="Correct the synthetic portfolio and security",
            ),
            headers=version_headers(client, 1),
        )
    assert response.status_code == 200
    owner = UUID(book["owner"]["id"])
    scopes = [(book["portfolio"]["id"], book["security"]["id"]), (other["id"], security["id"])]

    def read(con: Any, portfolio: str, security_id: str) -> Any:
        return eligibility(
            con,
            owner,
            UUID(portfolio),
            UUID(security_id),
            ex_date=date(2026, 1, 5),
            as_of=date(2026, 1, 5),
            rule="ORDINARY_PRE_EX_DATE",
        )

    with unit_of_work(engine) as con:
        before = [read(con, *scope) for scope in scopes]
        assert all(
            item.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED" for item in before
        )
    with unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con, owner, UUID(original["id"]), TradeRecord.model_validate(original), uuid4(), REASON
        )
    with unit_of_work(engine) as con:
        after = [read(con, *scope) for scope in scopes]
        assert all(
            item.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED" for item in after
        )
        assert all(
            new.data_revision == old.data_revision + 1
            for old, new in zip(before, after, strict=True)
        )
        invalidations = con.exec_driver_sql(
            "SELECT portfolio_id,from_date FROM projection_invalidations "
            "WHERE source_id=? AND reason='TRADE_AUDIT_ORIGIN_RECOVERED' ORDER BY portfolio_id",
            (original["id"],),
        ).all()
        assert [tuple(row) for row in invalidations] == sorted(
            [(portfolio, "2025-12-31") for portfolio, _ in scopes]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "12.00000000"),
        ("updated_at", "2000-01-01T00:00:00.000000Z"),
        ("status", "DRAFT_SCHEDULED"),
        ("version", 2),
        ("id", str(uuid4())),
    ],
)
async def test_unrelated_or_altered_response_fails_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    original = await legacy_trade(client, book)
    owner, identifier = UUID(book["owner"]["id"]), UUID(original["id"])
    before = database_snapshot(engine)
    with pytest.raises(trade_audit.TradeAuditError), unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con,
            owner,
            identifier,
            TradeRecord.model_validate({**original, field: value}),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("creation_audit", ["missing", "unrelated"])
async def test_absent_or_unrelated_creation_audit_cannot_be_manufactured(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], creation_audit: str
) -> None:
    original = await legacy_trade(client, book, creation_audit=creation_audit)
    before = database_snapshot(engine)
    with pytest.raises(trade_audit.TradeAuditError), unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "reason", ["", "short", " leading or trailing ", "X" * 1001, "Invalid\0reason"]
)
async def test_recovery_requires_a_bounded_reason_before_writing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], reason: str
) -> None:
    original = await legacy_trade(client, book)
    before = database_snapshot(engine)
    with (
        pytest.raises(trade_audit.TradeAuditError, match="^INVALID_TRADE_AUDIT_SCOPE$"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            reason,
        )
    assert database_snapshot(engine) == before


async def test_current_foreign_or_missing_scope_never_grants_recovery(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    owner, identifier = UUID(book["owner"]["id"]), UUID(original["id"])
    response = TradeRecord.model_validate(original)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_WRITE_REQUIRED$"),
    ):
        trade_audit.recover_from_response(con, owner, identifier, response, uuid4(), REASON)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_OWNER_UNAVAILABLE$"):
            trade_audit.recover_from_response(con, uuid4(), identifier, response, uuid4(), REASON)
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_NOT_FOUND$"):
            trade_audit.recover_from_response(con, owner, uuid4(), response, uuid4(), REASON)
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"):
            trade_audit.recover_from_response(con, owner, identifier, response, uuid4(), REASON)
    assert database_snapshot(engine) == before


async def test_same_intent_is_zero_write_but_a_distinct_recovery_does_not_overwrite(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    owner, identifier, request = UUID(book["owner"]["id"]), UUID(original["id"]), uuid4()
    response = TradeRecord.model_validate(original)
    with unit_of_work(engine, write=True) as con:
        result = trade_audit.recover_from_response(
            con, owner, identifier, response, request, REASON
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert (
            trade_audit.recover_from_response(con, owner, identifier, response, request, REASON)
            == result
        )
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_ORIGIN_EXISTS$"):
            trade_audit.recover_from_response(con, owner, identifier, response, uuid4(), REASON)
        with pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_ORIGIN_EXISTS$"):
            trade_audit.recover_from_response(
                con, owner, identifier, response, request, "A different recovery reason"
            )
    assert database_snapshot(engine) == before
    assert str(identifier) not in repr(result) and original["updated_at"] not in repr(result)


@pytest.mark.parametrize("same_request", [True, False])
async def test_concurrent_recoveries_append_one_origin_and_one_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], same_request: bool
) -> None:
    original = await legacy_trade(client, book)
    owner, identifier = UUID(book["owner"]["id"]), UUID(original["id"])
    response = TradeRecord.model_validate(original)
    first = uuid4()
    requests = [first, first if same_request else uuid4()]

    def recover(request: UUID) -> str:
        try:
            with unit_of_work(engine, write=True) as con:
                trade_audit.recover_from_response(con, owner, identifier, response, request, REASON)
            return "OK"
        except trade_audit.TradeAuditError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(recover, requests))
    assert sorted(results) == (
        ["OK", "OK"] if same_request else ["OK", "TRADE_AUDIT_ORIGIN_EXISTS"]
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='trade_audit_origin' "
                "AND action='TRADE_AUDIT_ORIGIN_RECOVERED' AND entity_id=?",
                (str(identifier),),
            ).scalar_one()
            == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations WHERE source_id=? "
                "AND reason='TRADE_AUDIT_ORIGIN_RECOVERED'",
                (str(identifier),),
            ).scalar_one()
            == 1
        )


async def test_invalid_revision_is_rejected_before_appending_recovery_records(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?",
            (2**63 - 1, book["portfolio"]["id"]),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"),
    ):
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


async def test_late_failure_rolls_back_recovery_audit_origin_and_invalidation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    before = database_snapshot(engine)
    with (
        patch.object(
            trade_audit, "_invalidate_root", side_effect=RuntimeError("synthetic failure")
        ),
        pytest.raises(RuntimeError, match="synthetic failure"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


async def test_caught_late_failure_still_rolls_back_the_recovery_savepoint(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(
            trade_audit, "_invalidate_root", side_effect=RuntimeError("synthetic failure")
        ),
        pytest.raises(RuntimeError, match="synthetic failure"),
    ):
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


async def test_outer_rollback_also_removes_a_successful_recovery_savepoint(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    before = database_snapshot(engine)
    with (
        pytest.raises(RuntimeError, match="synthetic outer failure"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
        raise RuntimeError("synthetic outer failure")
    assert database_snapshot(engine) == before


async def test_recovery_remains_historical_after_a_later_trade_revision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    owner, identifier, request = UUID(book["owner"]["id"]), UUID(original["id"]), uuid4()
    retained = TradeRecord.model_validate(original)
    with (
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider I/O")),
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private I/O")),
        unit_of_work(engine, write=True) as con,
    ):
        recovered = trade_audit.recover_from_response(
            con, owner, identifier, retained, request, REASON
        )
        successor = con.exec_driver_sql(
            "SELECT id FROM trades WHERE supersedes_id=?", (str(identifier),)
        ).scalar_one()
    revision = await client.patch(
        f"/api/v1/trades/{successor}",
        json=payload(book, quantity="12", reason="Correct the later synthetic trade"),
        headers=version_headers(client, 1),
    )
    assert revision.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert (
            trade_audit.recover_from_response(con, owner, identifier, retained, request, REASON)
            == recovered
        )
        result = eligibility(
            con,
            owner,
            UUID(book["portfolio"]["id"]),
            UUID(book["security"]["id"]),
            ex_date=date(2026, 1, 5),
            as_of=date(2026, 1, 5),
            rule="ORDINARY_PRE_EX_DATE",
        )
        assert (
            result is not None
            and result.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED"
        )
    assert database_snapshot(engine) == before


async def test_existing_creation_origin_is_never_replaced_by_recovery(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await create(client, "trades", payload(book))
    response = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=payload(book, quantity="11", reason="Revise the synthetic trade"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    before = database_snapshot(engine)
    with (
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_ORIGIN_EXISTS$"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


async def test_recovery_revalidates_an_internally_modified_response_model(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    malformed = TradeRecord.model_validate(original).model_copy(update={"quantity": object()})
    before = database_snapshot(engine)
    with pytest.raises(trade_audit.TradeAuditError), unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con, UUID(book["owner"]["id"]), UUID(original["id"]), malformed, uuid4(), REASON
        )
    assert database_snapshot(engine) == before


async def test_another_active_owner_cannot_recover_a_foreign_trade(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    foreign = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(foreign),),
        )
    before = database_snapshot(engine)
    with (
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_NOT_FOUND$"),
        unit_of_work(engine, write=True) as con,
    ):
        trade_audit.recover_from_response(
            con,
            foreign,
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("status", ["SUPERSEDED", "VOID"])
async def test_retired_fact_with_no_successor_or_void_audit_cannot_be_recovered(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], status: str
) -> None:
    with patch.object(trade_service, "_remember_trade_origin"):
        original = await create(client, "trades", payload(book))
    with unit_of_work(engine, write=True) as con:
        row = trade_service.owned_trade(con, book["owner"]["id"], original["id"])
        trade_service._retire(con, row, status)
    before = database_snapshot(engine)
    with pytest.raises(trade_audit.TradeAuditError), unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


async def test_new_sql_claim_with_a_different_bound_reason_is_rejected(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    owner, identifier, request = book["owner"]["id"], original["id"], str(uuid4())
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, owner, identifier)
        creation_hash = con.exec_driver_sql(
            "SELECT after_sha256 FROM audit_events WHERE id=?", (values["creation_audit_id"],)
        ).scalar_one()
        payload = {
            "trade_id": identifier,
            "owner_id": owner,
            "creation_audit_id": values["creation_audit_id"],
            "original_status": original["status"],
            "original_updated_at": original["updated_at"],
            "capture_mode": "RETAINED_RESPONSE",
            "retained_response_sha256": creation_hash,
            "reason_sha256": digest(canonical("A different synthetic recovery reason")),
        }
        audit(
            con,
            owner,
            "trade_audit_origin",
            identifier,
            "TRADE_AUDIT_ORIGIN_RECOVERED",
            request_id=request,
            after=payload,
            reason=REASON,
        )
        recorded = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type='trade_audit_origin' "
            "AND entity_id=? AND action='TRADE_AUDIT_ORIGIN_RECOVERED'",
            (identifier,),
        ).one()
        insert(
            con,
            "trade_audit_origins",
            {
                **values,
                "original_status": original["status"],
                "original_updated_at": original["updated_at"],
                "capture_mode": "RETAINED_RESPONSE",
                "capture_request_id": request,
                "capture_audit_id": recorded.id,
                "captured_at": recorded.created_at,
            },
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(trade_audit.TradeAuditError, match="^TRADE_AUDIT_INCONSISTENT$"),
    ):
        trade_audit.get_origin(con, UUID(owner), UUID(identifier))
    assert database_snapshot(engine) == before


async def test_unassociated_synthetic_recovery_audit_is_not_adopted(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = await legacy_trade(client, book)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "trade_audit_origin",
            original["id"],
            "TRADE_AUDIT_ORIGIN_RECOVERED",
            request_id=str(uuid4()),
            after={"synthetic": "not the original response"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(trade_audit.TradeAuditError), unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before


async def test_recovering_each_revision_does_not_conceal_another_missing_origin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
    ):
        original = await create(client, "trades", payload(book))
        response = await client.patch(
            f"/api/v1/trades/{original['id']}",
            json=payload(book, quantity="11", reason="Revise a synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
        assert response.status_code == 200
        revised = response.json()
        void = await client.request(
            "DELETE",
            f"/api/v1/trades/{revised['id']}",
            json={"reason": "Void the synthetic legacy revision"},
            headers=version_headers(client, 1),
        )
        assert void.status_code == 200
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con, owner, UUID(revised["id"]), TradeRecord.model_validate(revised), uuid4(), REASON
        )
        partial = eligibility(
            con,
            owner,
            UUID(book["portfolio"]["id"]),
            UUID(book["security"]["id"]),
            ex_date=date(2026, 1, 5),
            as_of=date(2026, 1, 5),
            rule="ORDINARY_PRE_EX_DATE",
        )
        assert (
            partial is not None
            and partial.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
        )
        trade_audit.recover_from_response(
            con, owner, UUID(original["id"]), TradeRecord.model_validate(original), uuid4(), REASON
        )
        complete = eligibility(
            con,
            owner,
            UUID(book["portfolio"]["id"]),
            UUID(book["security"]["id"]),
            ex_date=date(2026, 1, 5),
            as_of=date(2026, 1, 5),
            rule="ORDINARY_PRE_EX_DATE",
        )
        assert (
            complete is not None
            and complete.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED"
        )
        assert complete.data_revision == partial.data_revision + 1


@pytest.mark.parametrize("retirement", ["revision", "void"])
async def test_unrelated_retirement_fingerprint_is_not_recovered(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], retirement: str
) -> None:
    original = await legacy_trade(client, book, retirement=retirement, unrelated_before=True)
    before = database_snapshot(engine)
    with pytest.raises(trade_audit.TradeAuditError), unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con,
            UUID(book["owner"]["id"]),
            UUID(original["id"]),
            TradeRecord.model_validate(original),
            uuid4(),
            REASON,
        )
    assert database_snapshot(engine) == before
