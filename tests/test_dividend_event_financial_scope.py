"""Discover retained event financial scope without changing or authorizing it."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_event_financial_scope as scope
import spm_api.dividend_projection_history as projections
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_history import add_receipt, void_receipt
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_direct_receipt_migration import scenario as scenario
from tests.test_dividend_event_migration import OTHER, OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import owned_account
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import projection, receipt
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def read(engine: Engine, owner: UUID, event: UUID) -> Any:
    with unit_of_work(engine) as con:
        return scope.history(con, owner, event)


async def test_empty_owned_scope_is_not_confirmed_no_position_or_complete_history(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    before = database_snapshot(engine)
    result = read(engine, OWNER, event.event_id)
    assert result is not None and result.accounts == () and result.portfolios == ()
    assert result.revision_id == event.revision_id and result.revision_no == event.revision_no
    assert result.scope == "RETAINED_FINANCIAL_SCOPE_ONLY"
    assert result.history_completeness == "UNVERIFIED"
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert read(engine, OTHER, event.event_id) is None
    assert read(engine, OWNER, uuid4()) is None
    assert str(event.event_id) not in repr(result)
    assert database_snapshot(engine) == before


async def test_all_recorded_accounts_include_legacy_direct_and_projection_history(
    engine: Engine, book: dict[str, Any]
) -> None:
    older = receipt(engine, book, amount=6_000_000, older=True)
    latest = projection(engine, book)
    newer = direct_receipt(engine, book, received_micro=2_000_000)
    before = database_snapshot(engine)
    result = read(engine, book["owner"], book["event"].event_id)
    assert result is not None and len(result.accounts) == 1
    account = result.accounts[0]
    assert account.account_id == book["account_id"]
    assert [item.id for item in account.entitlements] == [
        UUID(book["older"]["id"]),
        UUID(book["newer"]["id"]),
    ]
    assert len(account.projections) == 1 and account.projections[-1].version.id == latest.version.id
    assert {item.receipt.id for item in account.receipts} == {UUID(older["id"]), UUID(newer["id"])}
    assert sum(isinstance(item, direct.RecordedDirectReceipt) for item in account.receipts) == 1
    assert account.unverified_origins == 1
    assert len(result.portfolios) == 1 and result.portfolios[0].id == account.portfolio_id
    assert result.portfolios[0].data_revision == account.portfolio_data_revision
    assert "6.000000" not in repr(result) and str(account.account_id) not in repr(result)
    assert database_snapshot(engine) == before


async def test_direct_cash_without_legacy_entitlement_is_discovered_without_inventing_one(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account_id = scenario
    saved = add_receipt(engine, scenario)
    before = database_snapshot(engine)
    result = read(engine, OWNER, event.event_id)
    assert result is not None and len(result.accounts) == 1
    account = result.accounts[0]
    assert account.account_id == UUID(account_id)
    assert account.entitlements == () and account.projections == ()
    assert len(account.receipts) == 1 and account.receipts[0].receipt.id == UUID(saved["id"])
    assert account.unverified_origins == 0
    assert before["dividend_entitlements"] == []
    assert database_snapshot(engine) == before


async def test_moved_archived_account_retains_original_and_current_portfolio_scopes(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, first_account = scenario
    first = add_receipt(engine, scenario)
    _, second_account = owned_account(engine)
    add_receipt(engine, (event, second_account), received_micro=2_000_000)
    old = UUID(first["origin"]["portfolio_id"])
    new, broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new),
                "owner_id": str(OWNER),
                "name": "Synthetic moved scope",
                "created_at": stamp(),
            },
        )
        insert(
            con,
            "brokers",
            {"id": str(broker), "owner_id": str(OWNER), "name": "Synthetic moved scope broker"},
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,status='ARCHIVED',"
            "version=version+1 WHERE id=?",
            (str(new), str(broker), first_account),
        )
    before = database_snapshot(engine)
    result = read(engine, OWNER, event.event_id)
    assert result is not None and len(result.accounts) == 2
    assert [str(item.account_id) for item in result.accounts] == sorted(
        [first_account, second_account]
    )
    moved = next(item for item in result.accounts if item.account_id == UUID(first_account))
    assert (
        moved.account_status == "ARCHIVED"
        and moved.portfolio_id == new
        and moved.broker_id == broker
    )
    assert {item.id for item in result.portfolios} >= {old, new}
    assert moved.receipts[0].origin is not None and moved.receipts[0].origin.portfolio_id == old
    assert database_snapshot(engine) == before


async def test_unsealed_direct_receipt_fails_the_whole_event_scope(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(event, account))
    before = database_snapshot(engine)
    with pytest.raises(
        scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
    ):
        read(engine, OWNER, event.event_id)
    assert database_snapshot(engine) == before


async def test_scope_and_nested_histories_share_one_wal_snapshot(
    engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    with unit_of_work(engine) as con:
        first = scope.history(con, book["owner"], event.event_id)
        assert first is not None and len(first.accounts) == 1 and first.accounts[0].receipts == ()
        added = direct_receipt(engine, book, received_micro=2_000_000)
        again = scope.history(con, book["owner"], event.event_id)
        assert again == first
    latest = read(engine, book["owner"], event.event_id)
    assert latest is not None and latest.accounts[0].receipts[0].receipt.id == UUID(added["id"])


@pytest.mark.parametrize("component", ["projection", "receipts"])
async def test_inconsistent_nested_history_fails_closed_without_partial_accounts(
    engine: Engine, scenario: tuple[Any, str], component: str
) -> None:
    event, _ = scenario
    add_receipt(engine, scenario)
    before = database_snapshot(engine)
    target = projections if component == "projection" else combined
    with (
        patch.object(target, "history", return_value=None),
        pytest.raises(
            scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
        ),
    ):
        read(engine, OWNER, event.event_id)
    assert database_snapshot(engine) == before


async def test_scope_requires_an_existing_transaction_and_uuid_pins(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, _ = scenario
    with (
        engine.connect() as con,
        pytest.raises(scope.EventFinancialScopeError, match="^INVALID_DIVIDEND_FINANCIAL_SCOPE$"),
    ):
        scope.history(con, OWNER, event.event_id)
    with (
        unit_of_work(engine) as con,
        pytest.raises(scope.EventFinancialScopeError, match="^INVALID_DIVIDEND_FINANCIAL_SCOPE$"),
    ):
        scope.history(con, OWNER, "invalid")  # type: ignore[arg-type]


async def test_ledger_only_accounts_are_not_a_complete_impact_list(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    _, linked_account = owned_account(engine, prepared["owner"])
    linked_book = {**prepared, "account_id": UUID(linked_account)}
    direct_receipt(engine, linked_book)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("network")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("network")),
    ):
        result = read(engine, prepared["owner"], prepared["event"].event_id)
    assert result is not None
    assert [item.account_id for item in result.accounts] == [UUID(linked_account)]
    assert prepared["account_id"] not in [item.account_id for item in result.accounts]
    assert before["trades"] and result.history_completeness == "UNVERIFIED"
    assert result.scope == "RETAINED_FINANCIAL_SCOPE_ONLY"
    assert database_snapshot(engine) == before


async def test_projection_only_scope_preserves_old_portfolio_after_account_move(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    tax, fee = settings(engine, prepared)
    saved = save(engine, prepared, intent(engine, prepared, tax, fee), uuid4())
    original = prepared["portfolio_id"]
    new_portfolio, _ = owned_account(engine, prepared["owner"])
    new_broker = uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "brokers",
            {
                "id": str(new_broker),
                "owner_id": str(prepared["owner"]),
                "name": "Moved synthetic broker",
            },
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (new_portfolio, str(new_broker), str(prepared["account_id"])),
        )
    before = database_snapshot(engine)
    result = read(engine, prepared["owner"], prepared["event"].event_id)
    assert result is not None and len(result.accounts) == 1
    item = result.accounts[0]
    assert item.entitlements == () and item.receipts == ()
    assert item.projections[-1].version.id == saved.version.id
    assert item.projections[-1].portfolio_id == original
    assert item.portfolio_id == UUID(new_portfolio)
    assert {item.id for item in result.portfolios} == {original, UUID(new_portfolio)}
    assert database_snapshot(engine) == before


async def test_voided_direct_cash_is_still_part_of_retained_scope(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    saved = add_receipt(engine, scenario)
    void_receipt(engine, saved)
    before = database_snapshot(engine)
    result = read(engine, OWNER, scenario[0].event_id)
    assert result is not None and len(result.accounts) == 1
    item = result.accounts[0].receipts[0]
    assert item.receipt.status == "VOID" and item.receipt.version == 2
    assert item.receipt.cash_scope == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert item.origin is not None and item.origin.portfolio_id == UUID(
        saved["origin"]["portfolio_id"]
    )
    assert database_snapshot(engine) == before


async def test_foreign_current_account_makes_the_entire_scope_unavailable(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, first_account = scenario
    add_receipt(engine, scenario)
    _, second_account = owned_account(engine)
    add_receipt(engine, (event, second_account))
    foreign_portfolio, foreign_account = owned_account(engine, OTHER)
    with unit_of_work(engine, write=True) as con:
        foreign_broker = con.exec_driver_sql(
            "SELECT broker_id FROM accounts WHERE id=?", (foreign_account,)
        ).scalar_one()
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (foreign_portfolio, foreign_broker, first_account),
        )
    before = database_snapshot(engine)
    with pytest.raises(
        scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
    ):
        read(engine, OWNER, event.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("portfolios", "data_revision", 0.5),
        ("portfolios", "version", 1.5),
        ("accounts", "version", 1.5),
        ("brokers", "version", 1.5),
    ],
)
async def test_noninteger_current_scope_fails_closed(
    engine: Engine, scenario: tuple[Any, str], table: str, column: str, value: float
) -> None:
    event, account_id = scenario
    saved = add_receipt(engine, scenario)
    ids = {
        "accounts": account_id,
        "portfolios": saved["origin"]["portfolio_id"],
        "brokers": saved["origin"]["broker_id"],
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(f"UPDATE {table} SET {column}=? WHERE id=?", (value, ids[table]))
    before = database_snapshot(engine)
    with pytest.raises(
        scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
    ):
        read(engine, OWNER, event.event_id)
    assert database_snapshot(engine) == before


async def test_int64_limit_data_revision_is_kept_exact_without_claiming_mutability(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    saved = add_receipt(engine, scenario)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?",
            (INT64_MAX, saved["origin"]["portfolio_id"]),
        )
    before = database_snapshot(engine)
    result = read(engine, OWNER, scenario[0].event_id)
    assert result is not None and result.accounts[0].portfolio_data_revision == INT64_MAX
    assert result.portfolios[0].data_revision == INT64_MAX
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


async def test_nested_reader_cannot_silently_drop_recorded_direct_receipts(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_receipt(engine, scenario)
    before = database_snapshot(engine)
    with (
        patch.object(combined, "history", return_value=()),
        pytest.raises(
            scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
        ),
    ):
        read(engine, OWNER, scenario[0].event_id)
    assert database_snapshot(engine) == before


async def test_nested_reader_cannot_silently_drop_recorded_legacy_receipts(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    before = database_snapshot(engine)
    with (
        patch.object(combined, "history", return_value=()),
        pytest.raises(
            scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
        ),
    ):
        read(engine, book["owner"], book["event"].event_id)
    assert database_snapshot(engine) == before


async def test_foreign_or_missing_retained_portfolio_is_rejected(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    foreign, _ = owned_account(engine, OTHER)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        for identifier in (UUID(foreign), uuid4()):
            with pytest.raises(ValueError, match="^Invalid retained portfolio$"):
                scope._portfolio(con, OWNER, identifier)
    assert database_snapshot(engine) == before


async def test_malformed_account_status_is_a_private_scope_error(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_receipt(engine, scenario)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        execute = con.execute

        def mock_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if str(statement) == "SELECT status FROM accounts WHERE id=:id":
                return SimpleNamespace(scalar_one=lambda: "synthetic-private")
            return execute(statement, *args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=mock_execute),
            pytest.raises(
                scope.EventFinancialScopeError, match="^DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE$"
            ),
        ):
            scope.history(con, OWNER, scenario[0].event_id)
    assert database_snapshot(engine) == before


async def test_account_added_during_empty_read_is_seen_only_in_next_snapshot(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account_id = scenario
    with unit_of_work(engine) as con:
        first = scope.history(con, OWNER, event.event_id)
        assert first is not None and first.accounts == ()
        saved = add_receipt(engine, scenario)
        assert scope.history(con, OWNER, event.event_id) == first
    result = read(engine, OWNER, event.event_id)
    assert result is not None and len(result.accounts) == 1
    assert result.accounts[0].account_id == UUID(account_id)
    assert result.accounts[0].receipts[0].receipt.id == UUID(saved["id"])


async def test_complete_projection_chain_is_retained_without_summing_versions(
    engine: Engine, book: dict[str, Any]
) -> None:
    tax, fee = settings(engine, book, rate="0", fee="0")
    first = save(engine, book, intent(engine, book, tax, fee, as_of="2026-01-07"), uuid4())
    second = save(
        engine,
        book,
        intent(
            engine,
            book,
            tax,
            fee,
            expected_projection_no=1,
            expected_projection_id=first.version.id,
        ),
        uuid4(),
    )
    before = database_snapshot(engine)
    result = read(engine, book["owner"], book["event"].event_id)
    assert result is not None and len(result.accounts) == 1
    versions = result.accounts[0].projections
    assert [item.version.id for item in versions] == [first.version.id, second.version.id]
    assert [item.version.projection_no for item in versions] == [1, 2]
    assert versions[1].version.previous_id == first.version.id
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before
