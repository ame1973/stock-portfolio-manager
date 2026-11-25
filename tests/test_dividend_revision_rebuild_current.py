"""A new revision must recheck the complete observation in a serialized writer."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Event
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api.audit import now
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_revision_preview import proposal
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.fixture
async def scope(client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]) -> dict[str, Any]:
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    save(engine, book, intent(engine, book, tax, fee), uuid4())
    direct_receipt(engine, book, received_micro=6_000_000)
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
    return {"book": book, "decision": decision, "proposal": proposed, "observed": observed}


async def test_current_recheck_is_read_only_and_does_not_grant_authority(
    engine: Engine, scope: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        result = current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert result == scope["observed"]
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.source_scope == "CURRENT_SOURCE_NOT_REVALIDATED"
    assert result.eligibility_scope == result.payment_scope == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_candidate_observation_rechecks_its_recorded_assessment_without_grant(
    engine: Engine, scope: dict[str, Any]
) -> None:
    original = scope["decision"]
    candidate = candidates.CandidateDecision(
        id=uuid4(),
        candidate_id=uuid4(),
        revision_no=1,
        state="OPEN",
        terms=original.terms,
        matches=(),
        change_reason="Synthetic explicit candidate without source observations.",
    )
    with unit_of_work(engine, write=True) as con:
        candidates.save(con, scope["book"]["owner"], candidate, at=now(), request_id=uuid4())
    decision = events.EventIntent(
        event_id=original.event_id,
        revision_id=original.revision_id,
        revision_no=original.revision_no,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason="Synthetic candidate association without an eligibility grant.",
    )
    with unit_of_work(engine) as con:
        observed = rebuild.observe(
            con, scope["book"]["owner"], decision.event_id, decision, scope["proposal"]
        )
    assert observed is not None and observed.candidate is not None
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        checked = current.require_current(
            con, scope["book"]["owner"], decision, scope["proposal"], observed
        )
    assert checked.candidate == observed.candidate
    assert checked.source_scope == "CURRENT_SOURCE_NOT_REVALIDATED"
    assert database_snapshot(engine) == before


async def test_recheck_requires_a_real_writer_transaction(
    engine: Engine, scope: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rebuild.RebuildError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", ["owner", "decision", "proposal"])
async def test_recheck_rejects_invalid_input_types(
    engine: Engine, scope: dict[str, Any], invalid: str
) -> None:
    args: dict[str, Any] = {
        "owner": scope["book"]["owner"],
        "decision": scope["decision"],
        "proposal": scope["proposal"],
        "observed": scope["observed"],
    }
    args[invalid] = object()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_SCOPE$"),
    ):
        current.require_current(con, **args)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change", ["receipt", "decision", "proposal", "owner", "account", "broker"]
)
async def test_stale_or_changed_scope_cannot_be_used_for_a_new_revision(
    engine: Engine, scope: dict[str, Any], change: str
) -> None:
    book = scope["book"]
    owner, decision, proposed = book["owner"], scope["decision"], scope["proposal"]
    if change == "receipt":
        direct_receipt(engine, book, received_micro=2_000_000)
    if change in {"account", "broker"}:
        with unit_of_work(engine, write=True) as con:
            table = "accounts" if change == "account" else "brokers"
            identifier = book["account_id"] if change == "account" else book["broker_id"]
            con.exec_driver_sql(
                f"UPDATE {table} SET version=version+1 WHERE id=?", (str(identifier),)
            )
    if change == "decision":
        decision = decision.model_copy(update={"reason": "Different synthetic owner decision."})
    if change == "proposal":
        proposed = proposed.model_copy(update={"dps": "1.2"})
    if change == "owner":
        owner = uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(con, owner, decision, proposed, scope["observed"])
    assert database_snapshot(engine) == before


async def test_competing_receipt_serializes_and_stales_the_next_writer(
    engine: Engine, scope: dict[str, Any]
) -> None:
    started = Event()

    def add_receipt() -> None:
        started.set()
        direct_receipt(engine, scope["book"], received_micro=2_000_000)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_of_work(engine, write=True) as con:
            future = pool.submit(add_receipt)
            assert started.wait(timeout=5)
            result = current.require_current(
                con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
            )
            assert result == scope["observed"] and not future.done()
        future.result(timeout=10)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


async def test_a_new_owned_account_requires_a_new_complete_proposal(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], scope: dict[str, Any]
) -> None:
    await create(
        client,
        "accounts",
        {
            "portfolio_id": graph["portfolio"]["id"],
            "broker_id": graph["source"]["broker_id"],
            "name": "Synthetic new account",
            "default_currency": "USD",
        },
    )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con, pytest.raises(rebuild.RebuildError):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


async def test_final_recheck_detects_same_transaction_account_change_and_rolls_back(
    engine: Engine, scope: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?",
            (str(scope["book"]["account_id"]),),
        )
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


async def test_same_transaction_review_acknowledgement_requires_a_fresh_observation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    first = book["event"]
    successor = manual(
        event_id=first.event_id,
        revision_id=uuid4(),
        revision_no=2,
        terms=terms(security_id=graph["security"]["id"], dps="1", adjustment_basis="UNKNOWN"),
    )
    with unit_of_work(engine, write=True) as con:
        events.save(con, book["owner"], successor, at=now(), request_id=uuid4())
    decision = manual(
        event_id=successor.event_id,
        revision_id=uuid4(),
        revision_no=3,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], successor.event_id, decision, proposed)
    assert observed is not None and observed.evidence_reviews.items
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(con, book["owner"], decision, proposed, observed)
        con.exec_driver_sql(
            "UPDATE review_items SET status='ACKNOWLEDGED' WHERE event_id=? "
            "AND kind='DIVIDEND_EVIDENCE' AND status='OPEN'",
            (str(successor.event_id),),
        )
        current.require_current(con, book["owner"], decision, proposed, observed)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "AUTHENTICATED"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "AUTHORIZED"},
        {"source_scope": "VERIFIED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"payment_scope": "ESTABLISHED"},
        {"preview": None},
    ],
)
async def test_forged_observation_cannot_be_used(
    engine: Engine, scope: dict[str, Any], change: dict[str, Any]
) -> None:
    forged = replace(scope["observed"], **change)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], forged
        )
    assert database_snapshot(engine) == before


async def test_float_version_cannot_impersonate_an_exact_integer_pin(
    engine: Engine, scope: dict[str, Any]
) -> None:
    observed = scope["observed"]
    snapshot = observed.preview.snapshot
    index, portfolio = next(
        (index, item) for index, item in enumerate(snapshot.portfolios) if item.accounts
    )
    pin = portfolio.accounts[0]
    malformed: dict[str, Any] = {"account_version": float(pin.account_version)}
    forged_portfolio = replace(
        portfolio, accounts=(replace(pin, **malformed), *portfolio.accounts[1:])
    )
    portfolios = list(snapshot.portfolios)
    portfolios[index] = forged_portfolio
    forged = replace(
        observed,
        preview=replace(observed.preview, snapshot=replace(snapshot, portfolios=tuple(portfolios))),
    )
    assert forged == observed  # Python numeric equality does not preserve input types.
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], forged
        )
    assert database_snapshot(engine) == before


async def test_undeclared_model_field_cannot_hide_inside_a_frozen_observation(
    engine: Engine, scope: dict[str, Any]
) -> None:
    observed = scope["observed"]
    record = observed.event.record.model_copy(update={"undeclared": "private synthetic value"})
    forged = replace(observed, event=replace(observed.event, record=record))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], forged
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["recorded_received", "active_receipts"])
async def test_float_money_or_boolean_receipt_count_cannot_impersonate_recorded_values(
    engine: Engine, scope: dict[str, Any], field: str
) -> None:
    observed = scope["observed"]
    account_id = scope["book"]["account_id"]
    index, account = next(
        (index, item)
        for index, item in enumerate(observed.preview.accounts)
        if item.account_id == account_id
    )
    assert account.recorded_received == Decimal("6") and account.active_receipts == 1
    changes: dict[str, Any] = {field: 6.0 if field == "recorded_received" else True}
    accounts = list(observed.preview.accounts)
    accounts[index] = replace(account, **changes)
    forged = replace(observed, preview=replace(observed.preview, accounts=tuple(accounts)))
    assert forged == observed  # Equal numeric values can still have invalid types.
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], forged
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ({"pins": [1, Decimal("6.00")]}, {"pins": [1, Decimal("6.00")]}, True),
        ({"pins": [1]}, {"pins": [True]}, False),
        ({1: "pin"}, {True: "pin"}, False),
        (frozenset({"dps", "ex_date"}), frozenset({"dps", "ex_date"}), True),
        (frozenset({1}), frozenset({True}), False),
        ([1], [1.0], False),
        (1.0, 1.0, False),
        (Decimal("6"), 6.0, False),
    ],
)
def test_exact_snapshot_values_keep_numeric_types(left: Any, right: Any, expected: bool) -> None:
    assert current._exact(left, right) is expected


async def test_unavailable_fresh_observation_does_not_become_an_authorization(
    engine: Engine, scope: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(rebuild, "observe", return_value=None),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before
