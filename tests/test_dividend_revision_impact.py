"""Classify every recorded account and retained portfolio without granting a revision."""

from dataclasses import replace
from datetime import date
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_revision_impact as impact
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
import tests.test_dividend_direct_receipt_history as direct_fixture
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import prepare
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import receipt as legacy_receipt
from tests.test_dividend_revision_preview import proposal
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


def plan(engine: Engine, scope: dict[str, Any]) -> impact.RevisionImpact:
    with unit_of_work(engine, write=True) as con:
        return impact.inspect(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )


async def test_financial_and_recorded_zero_accounts_remain_distinct(
    engine: Engine, graph: dict[str, Any], scope: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    result = plan(engine, scope)
    accounts = {item.account_id: item for item in result.accounts}
    assert set(accounts) == {UUID(graph["source"]["id"]), UUID(graph["target"]["id"])}
    source, target = accounts[UUID(graph["source"]["id"])], accounts[UUID(graph["target"]["id"])]
    assert source.disposition == "REBUILD_REQUIRED"
    assert (source.projections, source.active_receipts, source.void_receipts) == (1, 1, 0)
    assert target.disposition == "NO_RECORDED_IMPACT" and not target.has_recorded_history
    assert {item.id for item in result.invalidation_portfolios} == {UUID(graph["portfolio"]["id"])}
    assert result.scope == "RECORDED_REVISION_IMPACT_ONLY"
    assert result.history_completeness == "UNVERIFIED"
    assert result.eligibility_scope == result.payment_scope == "NOT_ESTABLISHED"
    assert graph["source"]["id"] not in repr(result) and "6.000000" not in repr(result)
    assert database_snapshot(engine) == before


async def test_void_receipt_without_holdings_still_requires_reconciliation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    original = direct_receipt(engine, book, received_micro=6_000_000)
    with patch.object(direct_fixture, "OWNER", book["owner"]):
        direct_fixture.void_receipt(engine, original)
    event = book["event"]
    decision = manual(
        event_id=event.event_id,
        revision_id=uuid4(),
        revision_no=event.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], event.event_id, decision, proposed)
    assert observed is not None
    before = database_snapshot(engine)
    result = plan(
        engine, {"book": book, "decision": decision, "proposal": proposed, "observed": observed}
    )
    account = next(item for item in result.accounts if item.account_id == book["account_id"])
    assert account.disposition == "REBUILD_REQUIRED" and account.has_recorded_history
    assert (account.active_receipts, account.void_receipts) == (0, 1)
    assert account.recorded_position == "0.00000000"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("case", ["upcoming", "unknown_rule", "unknown_basis"])
async def test_unknown_or_future_scopes_are_not_classified_as_no_impact(
    engine: Engine, scope: dict[str, Any], case: str
) -> None:
    book = scope["book"]
    original = scope["proposal"].model_dump(mode="python")
    decision = scope["decision"]
    if case == "upcoming":
        original["as_of"] = date(2026, 1, 7).isoformat()
    elif case == "unknown_rule":
        original["eligibility_rule"] = "REQUIRES_REVIEW"
    else:
        original["adjustment_basis"] = "UNKNOWN"
        decision = decision.model_copy(
            update={"terms": decision.terms.model_copy(update={"adjustment_basis": "UNKNOWN"})}
        )
    proposed = preview.RevisionProposal.model_validate(original)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], book["event"].event_id, decision, proposed)
    assert observed is not None
    before = database_snapshot(engine)
    result = plan(
        engine, {"book": book, "decision": decision, "proposal": proposed, "observed": observed}
    )
    assert all(account.disposition == "REBUILD_REQUIRED" for account in result.accounts)
    assert database_snapshot(engine) == before


async def test_moved_account_retains_original_and_current_portfolio_scopes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    direct_receipt(engine, book, received_micro=6_000_000)
    other = await create(client, "portfolios", {"name": "Synthetic retained scope move"})
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,version=version+1 WHERE id=?",
            (other["id"], str(book["account_id"])),
        )
    event = book["event"]
    decision = manual(
        event_id=event.event_id,
        revision_id=uuid4(),
        revision_no=event.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], event.event_id, decision, proposed)
    assert observed is not None
    before = database_snapshot(engine)
    result = plan(
        engine, {"book": book, "decision": decision, "proposal": proposed, "observed": observed}
    )
    account = next(item for item in result.accounts if item.account_id == book["account_id"])
    assert account.portfolio_id == UUID(other["id"])
    assert UUID(graph["portfolio"]["id"]) in account.retained_portfolios
    assert {item.id for item in result.invalidation_portfolios} == {
        UUID(other["id"]),
        UUID(graph["portfolio"]["id"]),
    }
    assert account.historical_portfolio_scope == "UNVERIFIED"
    assert database_snapshot(engine) == before


async def test_legacy_receipt_with_missing_origin_stays_unverified_and_in_scope(
    engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    legacy_receipt(engine, book)
    event = book["event"]
    decision = manual(
        event_id=event.event_id,
        revision_id=uuid4(),
        revision_no=event.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], event.event_id, decision, proposed)
    assert observed is not None
    before = database_snapshot(engine)
    result = plan(
        engine, {"book": book, "decision": decision, "proposal": proposed, "observed": observed}
    )
    account = next(item for item in result.accounts if item.account_id == book["account_id"])
    assert account.disposition == "REBUILD_REQUIRED" and account.entitlements == 2
    assert account.active_receipts == 1 and account.unverified_origins == 1
    assert account.retained_portfolios == () and account.historical_portfolio_scope == "UNVERIFIED"
    assert database_snapshot(engine) == before


async def test_zero_positions_still_invalidate_security_linked_portfolios(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    await create(client, "trades", trade_body(graph, trade_date="2026-01-06", quantity="10"))
    await create(
        client,
        "trades",
        trade_body(graph, trade_date="2026-01-07", side="SELL", quantity="10"),
    )
    event = book["event"]
    decision = manual(
        event_id=event.event_id,
        revision_id=uuid4(),
        revision_no=event.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], event.event_id, decision, proposed)
    assert observed is not None
    before = database_snapshot(engine)
    result = plan(
        engine, {"book": book, "decision": decision, "proposal": proposed, "observed": observed}
    )
    assert all(item.disposition == "NO_RECORDED_IMPACT" for item in result.accounts)
    assert {item.id for item in result.invalidation_portfolios} == {UUID(graph["portfolio"]["id"])}
    with (
        patch.object(event_writer, "_portfolios", return_value=[]),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_IMPACT_UNAVAILABLE$"),
    ):
        plan(
            engine,
            {"book": book, "decision": decision, "proposal": proposed, "observed": observed},
        )
    assert database_snapshot(engine) == before


async def test_archived_account_with_retained_history_still_requires_rebuild(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book = scope["book"]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(book["account_id"]),),
        )
    with unit_of_work(engine) as con:
        observed = rebuild.observe(
            con, book["owner"], book["event"].event_id, scope["decision"], scope["proposal"]
        )
    assert observed is not None
    before = database_snapshot(engine)
    result = plan(engine, {**scope, "observed": observed})
    account = next(item for item in result.accounts if item.account_id == book["account_id"])
    assert account.account_status == "ARCHIVED" and account.disposition == "REBUILD_REQUIRED"
    assert database_snapshot(engine) == before


async def test_substituted_current_observation_cannot_change_the_plan(
    engine: Engine, scope: dict[str, Any]
) -> None:
    forged = replace(scope["observed"], scope="OTHER")
    before = database_snapshot(engine)
    with (
        patch.object(current, "require_current", return_value=forged),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_IMPACT_UNAVAILABLE$"),
    ):
        plan(engine, scope)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate_portfolio",
        "duplicate_retained_portfolio",
        "duplicate_history",
        "duplicate_comparison",
        "wrong_account",
        "wrong_historical_scope",
        "missing_retained_portfolio",
        "missing_comparison",
        "duplicate_account",
        "foreign_retained_portfolio",
        "orphan_history",
    ],
)
async def test_malformed_full_scope_is_never_partially_classified(
    engine: Engine, scope: dict[str, Any], problem: str
) -> None:
    observed = scope["observed"]
    comparison = observed.preview
    snapshot = comparison.snapshot
    portfolio = snapshot.portfolios[0]
    history = snapshot.retained.accounts[0]
    if problem == "duplicate_portfolio":
        snapshot = replace(snapshot, portfolios=(*snapshot.portfolios, portfolio))
    elif problem == "duplicate_retained_portfolio":
        retained = snapshot.retained
        snapshot = replace(
            snapshot,
            retained=replace(retained, portfolios=(*retained.portfolios, retained.portfolios[0])),
        )
    elif problem == "duplicate_history":
        retained = snapshot.retained
        snapshot = replace(
            snapshot, retained=replace(retained, accounts=(*retained.accounts, history))
        )
    elif problem == "duplicate_comparison":
        comparison = replace(comparison, accounts=(*comparison.accounts, comparison.accounts[0]))
    elif problem == "wrong_account":
        position = replace(portfolio.book.accounts[0], account_id=str(uuid4()))
        book = replace(portfolio.book, accounts=(position, *portfolio.book.accounts[1:]))
        snapshot = replace(
            snapshot, portfolios=(replace(portfolio, book=book), *snapshot.portfolios[1:])
        )
    elif problem == "wrong_historical_scope":
        retained = snapshot.retained
        bad_history = replace(history, portfolio_id=uuid4())
        snapshot = replace(snapshot, retained=replace(retained, accounts=(bad_history,)))
    elif problem == "missing_retained_portfolio":
        snapshot = replace(snapshot, retained=replace(snapshot.retained, portfolios=()))
    elif problem == "missing_comparison":
        comparison = replace(comparison, accounts=comparison.accounts[1:])
    elif problem == "foreign_retained_portfolio":
        retained = snapshot.retained
        foreign = replace(retained.portfolios[0], id=uuid4())
        snapshot = replace(
            snapshot, retained=replace(retained, portfolios=(*retained.portfolios, foreign))
        )
    elif problem == "orphan_history":
        retained = snapshot.retained
        orphan = replace(history, account_id=uuid4())
        snapshot = replace(
            snapshot, retained=replace(retained, accounts=(*retained.accounts, orphan))
        )
    else:
        book = replace(
            portfolio.book, accounts=(*portfolio.book.accounts, portfolio.book.accounts[0])
        )
        altered = replace(
            portfolio, book=book, accounts=(*portfolio.accounts, portfolio.accounts[0])
        )
        snapshot = replace(snapshot, portfolios=(altered, *snapshot.portfolios[1:]))
    forged = replace(observed, preview=replace(comparison, snapshot=snapshot))
    before = database_snapshot(engine)
    with (
        patch.object(current, "require_current", return_value=forged),
        patch.object(current, "_exact", return_value=True),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_IMPACT_UNAVAILABLE$"),
    ):
        plan(engine, scope)
    assert database_snapshot(engine) == before


async def test_changed_receipt_scope_cannot_yield_a_current_plan(
    engine: Engine, scope: dict[str, Any]
) -> None:
    direct_receipt(engine, scope["book"], received_micro=2_000_000)
    before = database_snapshot(engine)
    with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
        plan(engine, scope)
    assert database_snapshot(engine) == before
