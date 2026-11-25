"""Pin recorded ledger inputs independently of derived dividend quantities."""

from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_ledger_scope as ledger
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api import trade_history_review
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.trade_codec import as_trade
from tests.helpers import create, database_snapshot, version_headers
from tests.test_dividend_eligibility_service import split_body
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import prepare
from tests.test_dividend_revision_preview import proposal
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_transfers import graph as graph
from tests.test_transfers import stock_body, trade_body

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "entity", ["trade", "trade_audit_origin", "security_transfer", "corporate_action"]
)
async def test_new_linked_audit_changes_the_observation_even_if_quantities_do_not(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], entity: str
) -> None:
    book = await prepare(client, engine, graph)
    transfer = await create(
        client, "security-transfers", stock_body(graph, transfer_date="2026-01-09", quantity="1")
    )
    action = await create(
        client, "corporate-actions", split_body(graph, effective_date="2026-03-01")
    )
    previous = book["event"]
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], previous.event_id, decision, proposed)
        trade = con.exec_driver_sql(
            "SELECT id FROM trades WHERE account_id=? ORDER BY created_sequence LIMIT 1",
            (str(book["account_id"]),),
        ).scalar_one()
    assert observed is not None
    before = database_snapshot(engine)
    identifier = {
        "trade": trade,
        "trade_audit_origin": trade,
        "security_transfer": transfer["id"],
        "corporate_action": action["id"],
    }[entity]
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(con, book["owner"], decision, proposed, observed)
        audit(
            con,
            str(book["owner"]),
            entity,
            identifier,
            "SYNTHETIC_LATE_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        fresh = rebuild.observe(con, book["owner"], previous.event_id, decision, proposed)
        assert fresh is not None and fresh.preview == observed.preview
        current.require_current(con, book["owner"], decision, proposed, observed)
    assert database_snapshot(engine) == before


async def test_retains_all_owned_portfolios_and_revised_roots_but_not_other_securities(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    other_portfolio = await create(client, "portfolios", {"name": "Synthetic second ledger"})
    other_account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other_portfolio["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Synthetic additional account",
            "default_currency": "USD",
        },
    )
    extra = await create(
        client,
        "trades",
        trade_body(
            graph,
            portfolio_id=other_portfolio["id"],
            account_id=other_account["id"],
            trade_date="2026-01-07",
            quantity="3",
        ),
    )
    other_security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHER",
            "name": "Synthetic unrelated security",
            "quote_currency": "USD",
        },
    )
    unrelated = await create(
        client,
        "trades",
        trade_body(graph, security_id=other_security["id"], trade_date="2026-01-07"),
    )
    with unit_of_work(engine) as con:
        original = con.exec_driver_sql(
            "SELECT id FROM trades WHERE account_id=? AND security_id=? "
            "ORDER BY created_sequence LIMIT 1",
            (str(book["account_id"]), graph["security"]["id"]),
        ).scalar_one()
    successor = await client.patch(
        f"/api/v1/trades/{original}",
        json=trade_body(graph, trade_date="2026-01-07", reason="Correct synthetic ledger details"),
        headers=version_headers(client, 1),
    )
    assert successor.status_code == 200
    previous = book["event"]
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    wire = proposal(graph).model_dump(mode="python")
    wire["accounts"] = (
        *wire["accounts"],
        {**wire["accounts"][0], "account_id": other_account["id"]},
    )
    proposed = preview.RevisionProposal.model_validate(wire)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], decision.event_id, decision, proposed)
    assert observed is not None
    raw = observed.ledger
    assert set(raw.portfolio_ids) == {UUID(graph["portfolio"]["id"]), UUID(other_portfolio["id"])}
    identifiers = {row[0] for row in raw.trades}
    assert {original, successor.json()["id"], extra["id"]} <= identifiers
    assert unrelated["id"] not in identifiers
    assert not any(row[1][3] == unrelated["id"] for row in raw.audits)

    class Rollback(Exception):
        pass

    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "trade",
            unrelated["id"],
            "SYNTHETIC_UNRELATED_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        assert current.require_current(con, book["owner"], decision, proposed, observed) == observed
        raise Rollback
    assert database_snapshot(engine) == before


async def test_private_raw_snapshot_contains_the_selected_ledger_and_origin_rows(
    engine: Engine, scope: dict[str, Any]
) -> None:
    observed = scope["observed"]
    raw = observed.ledger
    assert raw.owner_id == scope["book"]["owner"]
    assert len(raw.trades) == len(raw.trade_origins) == 2
    assert raw.transfers == raw.actions == raw.trade_reviews == ()
    trade_ids = {row[0] for row in raw.trades}
    assert trade_ids == {row[0] for row in raw.trade_origins}
    assert {row[1][3] for row in raw.audits if row[1][2] == "trade"} == trade_ids
    assert all(str(identifier) not in repr(raw) for identifier in trade_ids)
    assert str(raw.owner_id) not in repr(raw) and str(raw.owner_id) not in repr(observed)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert (
            current.require_current(
                con, raw.owner_id, scope["decision"], scope["proposal"], observed
            )
            == observed
        )
    assert database_snapshot(engine) == before


async def test_unrelated_audit_does_not_change_the_recorded_ledger_scope(
    engine: Engine, scope: dict[str, Any]
) -> None:
    class Rollback(Exception):
        pass

    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(scope["book"]["owner"]),
            "trade",
            str(uuid4()),
            "SYNTHETIC_UNRELATED_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        assert (
            current.require_current(
                con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
            )
            == scope["observed"]
        )
        raise Rollback
    assert database_snapshot(engine) == before


async def test_raw_ledger_scope_is_read_in_the_same_wal_snapshot(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, decision, proposed = scope["book"], scope["decision"], scope["proposal"]
    with unit_of_work(engine) as reader:
        observed = rebuild.observe(reader, book["owner"], decision.event_id, decision, proposed)
        assert observed is not None and observed.ledger.trades
        with unit_of_work(engine, write=True) as writer:
            audit(
                writer,
                str(book["owner"]),
                "trade",
                observed.ledger.trades[0][0],
                "SYNTHETIC_LATE_EVIDENCE",
                request_id=str(uuid4()),
                after={"scope": "synthetic"},
            )
        assert (
            rebuild.observe(reader, book["owner"], decision.event_id, decision, proposed)
            == observed
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as writer,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(writer, book["owner"], decision, proposed, observed)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", ["null_id", "binary_id", "binary_reason"])
async def test_malformed_new_linked_audit_fails_closed_without_disclosing_values(
    engine: Engine, scope: dict[str, Any], invalid: str
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    before = database_snapshot(engine)
    identity: Any = (
        None if invalid == "null_id" else b"private" if invalid == "binary_id" else str(uuid4())
    )
    reason: Any = b"private" if invalid == "binary_reason" else "Synthetic evidence"
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,reason,created_at) "
            "VALUES (?,?,'trade',?,'SYNTHETIC_LATE_EVIDENCE',?,?)",
            (identity, str(book["owner"]), observed.ledger.trades[0][0], reason, stamp()),
        )
        with pytest.raises(
            rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"
        ):
            rebuild.observe(
                con, book["owner"], scope["decision"].event_id, scope["decision"], scope["proposal"]
            )
        raise Rollback
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["missing_audit", "numeric_type", "wrong_owner"])
async def test_forged_raw_scope_cannot_replace_the_exact_observed_values(
    engine: Engine, scope: dict[str, Any], change: str
) -> None:
    observed = scope["observed"]
    raw = observed.ledger
    if change == "missing_audit":
        forged = replace(raw, audits=raw.audits[:-1])
    elif change == "wrong_owner":
        forged = replace(raw, owner_id=uuid4())
    else:
        identifier, values = raw.trades[0]
        row = list(values)
        index = next(i for i, value in enumerate(row) if type(value) is int and value != 0)
        row[index] = float(row[index])
        forged = replace(raw, trades=((identifier, tuple(row)), *raw.trades[1:]))
        assert forged == raw
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con,
            scope["book"]["owner"],
            scope["decision"],
            scope["proposal"],
            replace(observed, ledger=forged),
        )
    assert database_snapshot(engine) == before


async def test_scope_requires_the_exact_current_owner_portfolio_set(
    engine: Engine, scope: dict[str, Any]
) -> None:
    raw = scope["observed"].ledger
    with (
        engine.connect() as con,
        pytest.raises(ValueError, match="^Invalid recorded ledger scope$"),
    ):
        ledger.read(con, raw.owner_id, raw.security_id, raw.portfolio_ids)
    with unit_of_work(engine) as con:
        for owner, portfolios in (
            (raw.owner_id, ()),
            (uuid4(), raw.portfolio_ids),
            (raw.owner_id, raw.portfolio_ids + raw.portfolio_ids),
            (raw.owner_id, cast(Any, list(raw.portfolio_ids))),
        ):
            with pytest.raises(ValueError, match="recorded (ledger|portfolio) scope$"):
                ledger.read(con, owner, raw.security_id, portfolios)
        assert ledger.read(con, raw.owner_id, raw.security_id, raw.portfolio_ids) == raw


async def test_ledger_can_be_rechecked_after_an_event_only_head_change(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    decision = manual(
        event_id=book["event"].event_id,
        revision_id=uuid4(),
        revision_no=book["event"].revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], decision.event_id, decision, proposal(graph))
    assert observed is not None
    raw = observed.ledger
    with unit_of_work(engine, write=True) as con:
        result = events.save(con, book["owner"], decision, at=now(), request_id=uuid4())
        assert result.record.revision_id == decision.revision_id
        assert ledger.read(con, raw.owner_id, raw.security_id, raw.portfolio_ids) == raw
    with unit_of_work(engine) as con:
        assert ledger.read(con, raw.owner_id, raw.security_id, raw.portfolio_ids) == raw


def test_duplicate_raw_rows_must_have_exactly_equal_values() -> None:
    same = [{"id": "synthetic", "amount": 1}, {"id": "synthetic", "amount": 1}]
    assert ledger._freeze(cast(Any, same)) == (("synthetic", ("synthetic", 1)),)
    changed = [{"id": "synthetic", "amount": 1}, {"id": "synthetic", "amount": 2}]
    with pytest.raises(ValueError, match="^Inconsistent recorded ledger row$"):
        ledger._freeze(cast(Any, changed))


async def test_trade_review_and_its_audit_are_retained_without_promoting_the_review(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    trade = UUID(
        synthetic_legacy_draft(engine, {**graph, "account": graph["source"]}, unrelated_audit=False)
    )
    with unit_of_work(engine, write=True) as con:
        first = trade_history_review.save(
            con,
            book["owner"],
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason="Review this synthetic missing trade creation audit.",
        )
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        audit(
            con,
            str(book["owner"]),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json"),
        )
    decision = manual(
        event_id=book["event"].event_id,
        revision_id=uuid4(),
        revision_no=book["event"].revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], decision.event_id, decision, proposed)
    assert observed is not None
    assert {row[0] for row in observed.ledger.trade_reviews} == {str(first.id)}
    assert any(
        row[1][2:4] == ("trade_history_review", str(first.id)) for row in observed.ledger.audits
    )
    assert observed.preview.snapshot.portfolios[0].book.trade_creation_audit_scope == (
        "CREATION_GAP_REVIEWED_UNVERIFIED"
    )
    assert observed.eligibility_scope == "NOT_ESTABLISHED"
    with unit_of_work(engine, write=True) as con:
        second = trade_history_review.save(
            con,
            book["owner"],
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=1,
            decision="WITHDRAW",
            reason="Withdraw this synthetic unverified review decision.",
        )
        raw = ledger.read(
            con, book["owner"], observed.ledger.security_id, observed.ledger.portfolio_ids
        )
        assert {row[0] for row in raw.trade_reviews} == {str(first.id), str(second.id)}
        assert raw != observed.ledger
        with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
            current.require_current(con, book["owner"], decision, proposed, observed)
