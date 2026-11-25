"""Preview recorded ledger scope for a possible dividend revision without authorizing it."""

from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_event_financial_scope as financial_scope
import spm_api.dividend_revision_scope as revision_scope
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_event_writer import owned_account
from tests.test_dividend_projection_writer import prepare
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import receipt
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


def preview(engine: Engine, prepared: dict[str, Any], ex_date: date = date(2026, 1, 8)) -> Any:
    with unit_of_work(engine) as con:
        return revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=ex_date,
            as_of=date(2026, 1, 9),
            rule="ORDINARY_PRE_EX_DATE",
        )


def observations(result: Any) -> dict[str, Any]:
    return {
        item.account_id: item for portfolio in result.portfolios for item in portfolio.book.accounts
    }


async def test_ledger_only_accounts_are_included_without_manufacturing_event_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("network")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("network")),
    ):
        result = preview(engine, prepared)
    assert result is not None and result.retained.accounts == ()
    values = observations(result)
    assert set(values) == {graph["source"]["id"], graph["target"]["id"]}
    assert values[graph["source"]["id"]].recorded_quantity == "10.00000000"
    assert values[graph["target"]["id"]].recorded_quantity == "0.00000000"
    assert values[graph["source"]["id"]].state == "RECORDED_PRE_EX_DATE"
    assert result.scope == "RECORDED_REVISION_SCOPE_ONLY"
    assert result.history_completeness == "UNVERIFIED"
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert graph["source"]["id"] not in repr(result)
    assert "2026-01-09" not in repr(result) and "10.00000000" not in repr(result)
    assert database_snapshot(engine) == before


async def test_proposed_ex_date_changes_recorded_shares_without_changing_event_or_ledger(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    prior = preview(engine, prepared, date(2026, 1, 8))
    later = preview(engine, prepared, date(2026, 1, 9))
    assert observations(prior)[graph["source"]["id"]].recorded_quantity == "10.00000000"
    assert observations(later)[graph["source"]["id"]].recorded_quantity == "8.00000000"
    assert prior.retained.revision_id == later.retained.revision_id == prepared["event"].revision_id
    assert database_snapshot(engine) == before


async def test_financial_history_and_newly_affected_ledger_account_share_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    legacy = receipt(engine, book, amount=6_000_000, older=True)
    direct = direct_receipt(engine, book, received_micro=2_000_000)
    await create(
        client,
        "trades",
        trade_body(graph, account_id=graph["target"]["id"], quantity="3", trade_date="2026-01-07"),
    )
    before = database_snapshot(engine)
    result = preview(engine, book)
    assert result is not None and len(result.retained.accounts) == 1
    assert result.retained.accounts[0].account_id == book["account_id"]
    assert {item.receipt.id for item in result.retained.accounts[0].receipts} == {
        UUID(legacy["id"]),
        UUID(direct["id"]),
    }
    assert observations(result)[graph["target"]["id"]].recorded_quantity == "3.00000000"
    assert UUID(graph["target"]["id"]) not in {item.account_id for item in result.retained.accounts}
    assert database_snapshot(engine) == before


async def test_review_rule_preserves_unknown_quantities_and_never_invents_zero(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=date(2026, 1, 8),
            as_of=date(2026, 1, 9),
            rule="REQUIRES_REVIEW",
        )
    assert result is not None
    assert all(
        item.state == "NEEDS_REVIEW" and item.recorded_quantity is None
        for item in observations(result).values()
    )
    assert database_snapshot(engine) == before


async def test_foreign_event_is_not_disclosed_and_bad_inputs_fail_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert (
            revision_scope.read(
                con,
                uuid4(),
                prepared["event"].event_id,
                ex_date=date(2026, 1, 8),
                as_of=date(2026, 1, 9),
                rule="ORDINARY_PRE_EX_DATE",
            )
            is None
        )
        with pytest.raises(
            revision_scope.RevisionScopeError, match="^INVALID_DIVIDEND_REVISION_SCOPE$"
        ):
            revision_scope.read(
                con,
                prepared["owner"],
                prepared["event"].event_id,
                ex_date="2026-01-08",  # type: ignore[arg-type]
                as_of=date(2026, 1, 9),
                rule="ORDINARY_PRE_EX_DATE",
            )
    assert database_snapshot(engine) == before


async def test_inconsistent_or_missing_ledger_book_fails_the_whole_preview(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with (
        patch.object(eligibility, "get", return_value=None),
        pytest.raises(
            revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
        ),
    ):
        preview(engine, prepared)
    assert database_snapshot(engine) == before


@pytest.fixture
async def prepared_book(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> tuple[dict[str, Any], eligibility.RecordedEligibilityBook]:
    prepared = await prepare(client, engine, graph)
    with unit_of_work(engine) as con:
        value = eligibility.get(
            con,
            prepared["owner"],
            prepared["portfolio_id"],
            UUID(graph["security"]["id"]),
            ex_date=date(2026, 1, 8),
            as_of=date(2026, 1, 9),
            rule="ORDINARY_PRE_EX_DATE",
        )
    assert value is not None
    return prepared, value


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "synthetic-private"},
        {"history_completeness": "COMPLETE"},
        {"state": "NEEDS_REVIEW", "recorded_quantity": "1.00000000"},
        {"state": "NEEDS_REVIEW", "recorded_quantity": None, "reasons": ()},
        {"reasons": ["not-a-tuple"]},
        {"reasons": ("unexpected-review",)},
        {"reasons": ("",)},
        {"pending_action_ids": ["not-a-tuple"]},
        {"pending_action_ids": ("",)},
        {"pending_action_ids": ("duplicate", "duplicate")},
        {"pending_action_ids": ("unexpected-action",)},
        {"state": "UNKNOWN"},
        {"state": "UPCOMING_RECORDED_HOLDINGS"},
        {"recorded_quantity": None},
        {"recorded_quantity": -1.0},
        {"recorded_quantity": "-1.00000000"},
        {"recorded_quantity": "0001.00000000"},
        {"recorded_quantity": "1.0"},
        {"recorded_quantity": "92233720369.00000000"},
    ],
)
async def test_invalid_recorded_observations_fail_closed_without_leaking_inputs(
    engine: Engine,
    prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook],
    changes: dict[str, Any],
) -> None:
    prepared, original = prepared_book
    forged = replace(original.accounts[0], **changes)
    altered = replace(original, accounts=(forged, *original.accounts[1:]))
    before = database_snapshot(engine)
    with (
        patch.object(eligibility, "get", return_value=altered),
        pytest.raises(
            revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
        ),
    ):
        preview(engine, prepared)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field",
    [
        "owner",
        "portfolio",
        "security",
        "revision",
        "revision_float",
        "trade_scope",
        "creation_scope",
        "timestamp_type",
        "timestamp_naive",
        "accounts_list",
        "account_type",
        "ex_date",
        "as_of",
        "rule",
        "accounts",
        "duplicate",
    ],
)
async def test_substituted_ledger_book_or_account_set_is_rejected(
    engine: Engine,
    prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook],
    field: str,
) -> None:
    prepared, original = prepared_book
    changes: dict[str, Any] = {
        "owner": {"owner_id": uuid4()},
        "portfolio": {"portfolio_id": uuid4()},
        "security": {"security_id": uuid4()},
        "revision": {"data_revision": original.data_revision + 1},
        "revision_float": {"data_revision": float(original.data_revision)},
        "trade_scope": {"trade_history_audit_scope": "CONFIRMED"},
        "creation_scope": {"trade_creation_audit_scope": "CONFIRMED"},
        "timestamp_type": {"latest_input_at": "synthetic-private"},
        "timestamp_naive": {"latest_input_at": datetime(2026, 1, 9)},
        "accounts_list": {"accounts": list(original.accounts)},
        "account_type": {"accounts": (object(), *original.accounts[1:])},
        "ex_date": {"ex_date": date(2026, 1, 7)},
        "as_of": {"as_of": date(2026, 1, 8)},
        "rule": {"rule": "REQUIRES_REVIEW"},
        "accounts": {"accounts": original.accounts[:-1]},
        "duplicate": {"accounts": (original.accounts[0], original.accounts[0])},
    }
    forged = replace(original, **changes[field])
    before = database_snapshot(engine)
    with (
        patch.object(eligibility, "get", return_value=forged),
        pytest.raises(
            revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
        ),
    ):
        preview(engine, prepared)
    assert database_snapshot(engine) == before


async def test_upcoming_date_keeps_future_holdings_unverified(
    engine: Engine, prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook]
) -> None:
    prepared, _ = prepared_book
    before = database_snapshot(engine)
    result = preview(engine, prepared, date(2026, 1, 10))
    assert result is not None
    item = observations(result)[str(prepared["account_id"])]
    assert item.state == "UPCOMING_RECORDED_HOLDINGS" and item.recorded_quantity == "8.00000000"
    assert (
        item.history_completeness == "UNVERIFIED" and result.eligibility_scope == "NOT_ESTABLISHED"
    )
    assert database_snapshot(engine) == before


async def test_account_and_broker_archive_pins_remain_explicit(
    engine: Engine, prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook]
) -> None:
    prepared, _ = prepared_book
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(prepared["account_id"]),),
        )
        con.exec_driver_sql(
            "UPDATE brokers SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(prepared["broker_id"]),),
        )
    before = database_snapshot(engine)
    result = preview(engine, prepared)
    assert result is not None
    account = next(
        item
        for p in result.portfolios
        for item in p.accounts
        if item.account_id == prepared["account_id"]
    )
    assert account.account_status == account.broker_status == "ARCHIVED"
    assert account.account_version == prepared["account_version"] + 1
    assert account.broker_version == prepared["broker_version"] + 1
    assert database_snapshot(engine) == before


async def test_ledger_and_financial_scopes_share_one_wal_snapshot_during_trade(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    with unit_of_work(engine) as con:

        def get() -> Any:
            return revision_scope.read(
                con,
                prepared["owner"],
                prepared["event"].event_id,
                ex_date=date(2026, 1, 8),
                as_of=date(2026, 1, 9),
                rule="ORDINARY_PRE_EX_DATE",
            )

        first = get()
        await create(
            client,
            "trades",
            trade_body(
                graph, account_id=graph["target"]["id"], quantity="3", trade_date="2026-01-07"
            ),
        )
        assert get() == first
    latest = preview(engine, prepared)
    assert observations(first)[graph["target"]["id"]].recorded_quantity == "0.00000000"
    assert observations(latest)[graph["target"]["id"]].recorded_quantity == "3.00000000"
    assert (
        latest.portfolios[0].portfolio.data_revision > first.portfolios[0].portfolio.data_revision
    )


async def test_altered_retained_scope_and_unknown_portfolio_are_rejected(
    engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine) as con:
        original = financial_scope.history(con, book["owner"], book["event"].event_id)
    assert original is not None
    for altered in (
        replace(original, revision_id=uuid4()),
        replace(original, portfolios=(financial_scope.PortfolioScope(uuid4(), 1, 0),)),
        replace(original, accounts=(replace(original.accounts[0], account_id=uuid4()),)),
    ):
        with (
            patch.object(financial_scope, "history", return_value=altered),
            pytest.raises(
                revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
            ),
        ):
            preview(engine, book)


async def test_missing_account_or_noncanonical_status_is_private_error(
    engine: Engine, prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook]
) -> None:
    prepared, _ = prepared_book
    before = database_snapshot(engine)
    with (
        patch.object(revision_scope, "account", return_value=None),
        pytest.raises(
            revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
        ),
    ):
        preview(engine, prepared)
    with unit_of_work(engine) as con:
        execute = con.execute

        def mock_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = execute(statement, *args, **kwargs)
            if "SELECT a.id,a.status,b.status AS broker_status FROM accounts" in str(statement):
                rows = [dict(row) for row in result.mappings().all()]
                rows[0]["broker_status"] = "synthetic-private"
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))
            return result

        with (
            patch.object(con, "execute", side_effect=mock_execute),
            pytest.raises(
                revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
            ),
        ):
            revision_scope.read(
                con,
                prepared["owner"],
                prepared["event"].event_id,
                ex_date=date(2026, 1, 8),
                as_of=date(2026, 1, 9),
                rule="ORDINARY_PRE_EX_DATE",
            )
    assert database_snapshot(engine) == before


async def test_reader_rejects_missing_transaction_and_wrong_date_type(
    engine: Engine, prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook]
) -> None:
    prepared, _ = prepared_book
    with (
        engine.connect() as con,
        pytest.raises(revision_scope.RevisionScopeError, match="^INVALID_DIVIDEND_REVISION_SCOPE$"),
    ):
        revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=date(2026, 1, 8),
            as_of=date(2026, 1, 9),
            rule="ORDINARY_PRE_EX_DATE",
        )
    with unit_of_work(engine) as con:
        values: list[dict[str, Any]] = [
            {"owner": "bad"},
            {"event_id": "bad"},
            {"ex_date": datetime(2026, 1, 8)},
            {"as_of": "2026-01-09"},
            {"rule": "BAD"},
            {"rule": []},
        ]
        for changed in values:
            base = {
                "owner": prepared["owner"],
                "event_id": prepared["event"].event_id,
                "ex_date": date(2026, 1, 8),
                "as_of": date(2026, 1, 9),
                "rule": "ORDINARY_PRE_EX_DATE",
                **changed,
            }
            with pytest.raises(
                revision_scope.RevisionScopeError, match="^INVALID_DIVIDEND_REVISION_SCOPE$"
            ):
                revision_scope.read(con, **base)


async def test_every_owned_portfolio_is_scanned_but_foreign_portfolios_are_not(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    empty = await create(client, "portfolios", {"name": "Synthetic empty portfolio"})
    other = uuid4()
    at = stamp()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "owners",
            {
                "id": str(other),
                "password_hash": "synthetic",
                "password_changed_at": at,
                "created_at": at,
            },
        )
    foreign_portfolio, _ = owned_account(engine, other)
    before = database_snapshot(engine)
    result = preview(engine, prepared)
    assert result is not None
    portfolios = {item.portfolio.id: item for item in result.portfolios}
    assert set(portfolios) == {prepared["portfolio_id"], UUID(empty["id"])}
    assert UUID(foreign_portfolio) not in portfolios
    assert portfolios[UUID(empty["id"])].accounts == ()
    assert portfolios[UUID(empty["id"])].book.accounts == ()
    assert database_snapshot(engine) == before


async def test_inactive_owner_cannot_obtain_a_new_revision_scope(
    engine: Engine, prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook]
) -> None:
    prepared, _ = prepared_book
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(prepared["owner"]),))
    before = database_snapshot(engine)
    with pytest.raises(
        revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
    ):
        preview(engine, prepared)
    assert database_snapshot(engine) == before


async def test_moved_account_with_ledger_history_is_not_silently_omitted(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    moved = await create(client, "portfolios", {"name": "Synthetic moved ledger portfolio"})
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,version=version+1 WHERE id=?",
            (moved["id"], str(prepared["account_id"])),
        )
    before = database_snapshot(engine)
    with pytest.raises(
        revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
    ):
        preview(engine, prepared)
    assert database_snapshot(engine) == before


async def test_revised_trade_origin_evidence_is_retained_without_granting_eligibility(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    original = await create(
        client,
        "trades",
        trade_body(graph, account_id=graph["target"]["id"], quantity="3", trade_date="2026-01-07"),
    )
    response = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=trade_body(
            graph,
            account_id=graph["target"]["id"],
            quantity="4",
            trade_date="2026-01-07",
            reason="Correct a synthetic recorded quantity",
        ),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    before = database_snapshot(engine)
    result = preview(engine, prepared)
    assert result is not None
    observed = next(
        item for item in result.portfolios if item.portfolio.id == prepared["portfolio_id"]
    )
    assert observed.book.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED"
    assert observations(result)[graph["target"]["id"]].recorded_quantity == "4.00000000"
    assert result.history_completeness == "UNVERIFIED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_unverified_retired_trade_scope_is_preserved_not_promoted(
    engine: Engine, prepared_book: tuple[dict[str, Any], eligibility.RecordedEligibilityBook]
) -> None:
    prepared, original = prepared_book
    unverified = replace(original, trade_history_audit_scope="RETIRED_TRADE_HISTORY_UNVERIFIED")
    before = database_snapshot(engine)
    with patch.object(eligibility, "get", return_value=unverified):
        result = preview(engine, prepared)
    assert result is not None and result.portfolios[0].book.trade_history_audit_scope == (
        "RETIRED_TRADE_HISTORY_UNVERIFIED"
    )
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_unsealed_direct_receipt_prevents_partial_revision_scope(
    engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(book["event"], str(book["account_id"])))
    before = database_snapshot(engine)
    with pytest.raises(
        revision_scope.RevisionScopeError, match="^DIVIDEND_REVISION_SCOPE_UNAVAILABLE$"
    ):
        preview(engine, book)
    assert database_snapshot(engine) == before
