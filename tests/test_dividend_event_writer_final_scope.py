"""Event and rejection writers must retain every exact portfolio invalidation."""

from contextlib import nullcontext
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_service as rejection_history
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import now, stamp
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from tests.helpers import create, database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import entitlement
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import prepare
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("kind", ["event", "rejection"])
@pytest.mark.parametrize("catch_inside", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "portfolio",
        "data_revision",
        "owner",
        "marker_time",
        "marker_resolved",
        "new_portfolio",
        "marker_missing",
        "marker_duplicate",
        "marker_day",
        "marker_reason",
        "marker_source",
    ],
)
async def test_late_invalidation_changes_rollback_the_entire_decision(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    kind: str,
    catch_inside: bool,
    change: str,
) -> None:
    book = await prepare(client, engine, graph)
    destination = await create(client, "portfolios", {"name": "Synthetic new affected portfolio"})
    destination_account = await create(
        client,
        "accounts",
        {
            "portfolio_id": destination["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Synthetic new affected account",
            "default_currency": "USD",
        },
    )
    previous = book["event"]
    next_decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    rejection = rejection_writer.RejectionIntent(
        id=uuid4(),
        event_id=previous.event_id,
        revision_id=previous.revision_id,
        revision_no=previous.revision_no,
        citation="Synthetic rejection reference",
        source_url=None,
        reason="Synthetic rejection requiring an exact invalidation.",
    )
    module = event_writer if kind == "event" else rejection_writer
    original = module.invalidate_portfolio
    before = database_snapshot(engine)
    injected: list[str] = []

    def alter(
        con: Any, portfolio: str, source: str, reason: str, from_date: str | None = None
    ) -> Any:
        marker = original(con, portfolio, source, reason, from_date)
        assert not injected
        injected.append(change)
        if change in {"portfolio", "data_revision"}:
            column = "version" if change == "portfolio" else "data_revision"
            con.exec_driver_sql(
                f"UPDATE portfolios SET {column}={column}+1 WHERE id=?", (portfolio,)
            )
        elif change == "owner":
            con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(book["owner"]),))
        elif change == "new_portfolio":
            con.exec_driver_sql(
                "INSERT INTO positions(portfolio_id,account_id,security_id,projection_version,"
                "shares_e8,remaining_cost_hkd_micro,realized_hkd_micro,calculated_at_utc) "
                "VALUES (?,?,?,0,0,0,0,?)",
                (destination["id"], destination_account["id"], graph["security"]["id"], stamp()),
            )
        elif change == "marker_missing":
            assert (
                con.exec_driver_sql(
                    "DELETE FROM projection_invalidations WHERE id=?", (marker[0],)
                ).rowcount
                == 1
            )
        elif change == "marker_duplicate":
            assert (
                con.exec_driver_sql(
                    "INSERT INTO projection_invalidations(id,portfolio_id,from_date,reason,"
                    "source_id,created_at,resolved_at) SELECT ?,portfolio_id,from_date,reason,"
                    "source_id,created_at,resolved_at FROM projection_invalidations WHERE id=?",
                    (str(uuid4()), marker[0]),
                ).rowcount
                == 1
            )
        else:
            column, value = {
                "marker_time": ("created_at", "2000-01-01T00:00:00.000000Z"),
                "marker_resolved": ("resolved_at", stamp()),
                "marker_day": ("from_date", "2026-01-01"),
                "marker_reason": ("reason", "SYNTHETIC_DIFFERENT_REASON"),
                "marker_source": ("source_id", str(uuid4())),
            }[change]
            changed = con.exec_driver_sql(
                f"UPDATE projection_invalidations SET {column}=? WHERE id=?", (value, marker[0])
            )
            assert changed.rowcount == 1
        return marker

    error = events.EventError if kind == "event" else rejection_history.RejectionError
    outer = nullcontext() if catch_inside else pytest.raises(error)
    inner = pytest.raises(error) if catch_inside else nullcontext()
    with (
        patch.object(module, "invalidate_portfolio", side_effect=alter),
        outer,
        unit_of_work(engine, write=True) as con,
        inner,
    ):
        if kind == "event":
            event_writer.save(con, book["owner"], next_decision, at=now(), request_id=uuid4())
        else:
            rejection_writer.save(con, book["owner"], rejection, request_id=uuid4())
    assert injected == [change]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["event", "rejection"])
async def test_each_affected_portfolio_is_invalidated_once(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    book = await prepare(client, engine, graph)
    portfolio = await create(client, "portfolios", {"name": "Synthetic second affected portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Synthetic second affected account",
            "default_currency": "USD",
        },
    )
    await create(
        client,
        "trades",
        trade_body(graph, portfolio_id=portfolio["id"], account_id=account["id"]),
    )
    previous = book["event"]
    next_decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    rejection = rejection_writer.RejectionIntent(
        id=uuid4(),
        event_id=previous.event_id,
        revision_id=previous.revision_id,
        revision_no=previous.revision_no,
        citation="Synthetic rejection reference",
        source_url=None,
        reason="Synthetic rejection requiring exact portfolio invalidations.",
    )
    identifiers = (str(book["portfolio_id"]), portfolio["id"])
    with unit_of_work(engine) as con:
        initial = {
            row.id: (row.version, row.data_revision)
            for row in con.exec_driver_sql(
                "SELECT id,version,data_revision FROM portfolios WHERE id IN (?,?)", identifiers
            )
        }
    with unit_of_work(engine, write=True) as con:
        if kind == "event":
            event_writer.save(con, book["owner"], next_decision, at=now(), request_id=uuid4())
            source, reason = next_decision.revision_id, "DIVIDEND_EVENT_CHANGED"
        else:
            rejection_writer.save(con, book["owner"], rejection, request_id=uuid4())
            source, reason = rejection.id, "DIVIDEND_REVISION_REJECTED"
        effects = event_writer._invalidation_scope(
            con, book["owner"], previous.terms.security_id, source, reason, "2026-01-08"
        )
        assert {item[0] for item in effects.portfolios} == set(identifiers)
        assert {item[1] for item in effects.markers} == set(identifiers)
        assert all(
            (version, revision) == (initial[identifier][0], initial[identifier][1] + 1)
            for identifier, version, revision in effects.portfolios
        )


@pytest.mark.parametrize("catch_inside", [False, True])
async def test_late_old_entitlement_cannot_escape_internal_revision_fence(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], catch_inside: bool
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    claim = entitlement({"id": str(previous.revision_id), "currency": "USD"}, graph["source"]["id"])
    claim["created_at"] = stamp()
    original = invalidate_portfolio
    request, at = uuid4(), now()
    before = database_snapshot(engine)
    injected: list[str] = []

    def add_old_entitlement(
        con: Any, portfolio: str, source: str, reason: str, from_date: str | None = None
    ) -> Any:
        marker = original(con, portfolio, source, reason, from_date)
        assert not injected
        insert(con, "dividend_entitlements", claim)
        injected.append("entitlement")
        return marker

    outer = nullcontext() if catch_inside else pytest.raises(events.EventError)
    inner = pytest.raises(events.EventError) if catch_inside else nullcontext()
    with (
        patch.object(event_writer, "invalidate_portfolio", side_effect=add_old_entitlement),
        outer,
        unit_of_work(engine, write=True) as con,
        inner,
    ):
        event_writer.save(con, book["owner"], decision, at=at, request_id=request)
    assert injected == ["entitlement"]
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        saved = event_writer.save(con, book["owner"], decision, at=at, request_id=request)
    after = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert event_writer.save(con, book["owner"], decision, at=at, request_id=request) == saved
    assert database_snapshot(engine) == after
