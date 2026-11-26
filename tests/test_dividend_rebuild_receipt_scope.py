"""Retain raw receipt and cash evidence independently of a derived comparison."""

from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
import spm_api.dividend_revision_receipt_scope as receipts
import tests.test_dividend_direct_receipt_history as direct_fixture
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.ledger_service import post_cash
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import entitlement
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import prepare
from tests.test_dividend_receipt_history_api import book as book
from tests.test_dividend_receipt_void_inputs_api import create as create_legacy_receipt
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_revision_preview import proposal
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.fixture
async def mixed_scope(engine: Engine, scope: dict[str, Any]) -> dict[str, Any]:
    book = scope["book"]
    claim = entitlement(
        {"id": str(book["event"].revision_id), "currency": "USD"}, str(book["account_id"])
    )
    claim["created_at"] = stamp()
    legacy_id, missing_cash_id = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", claim)
        for identifier in (legacy_id, missing_cash_id):
            insert(
                con,
                "dividend_receipts",
                {
                    "id": identifier,
                    "entitlement_id": claim["id"],
                    "account_id": str(book["account_id"]),
                    "received_date": "2026-01-09",
                    "received_micro": 6_000_000,
                    "currency": "USD",
                    "tax_micro": None,
                    "fee_micro": None,
                    "fx_rate_hkd_e12": 7_350_000_000_000,
                    "received_hkd_micro": 44_100_000,
                    "created_at": stamp(),
                },
            )
        cash_id = post_cash(
            con,
            account=str(book["account_id"]),
            day="2026-01-09",
            currency="USD",
            amount=6_000_000,
            fx=7_350_000_000_000,
            booked=44_100_000,
            kind="DIVIDEND_RECEIPT",
            source_type="DIVIDEND_RECEIPT",
            source_id=legacy_id,
        )
    voided = direct_receipt(engine, book, received_micro=2_000_000)
    with patch.object(direct_fixture, "OWNER", book["owner"]):
        direct_fixture.void_receipt(engine, voided)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(
            con, book["owner"], scope["decision"].event_id, scope["decision"], scope["proposal"]
        )
    assert observed is not None
    return {
        **scope,
        "observed": observed,
        "entitlement": claim["id"],
        "legacy": legacy_id,
        "legacy_cash": cash_id,
        "missing_cash": missing_cash_id,
        "voided": voided,
    }


@pytest.mark.parametrize(
    "entity",
    [
        "cash_entry",
        "dividend_direct_receipt_intent",
        "dividend_direct_receipt_http_intent",
        "dividend_direct_receipt_void_intent",
        "dividend_direct_receipt_void_http_intent",
    ],
)
async def test_linked_receipt_and_cash_audits_change_the_recorded_observation(
    engine: Engine, scope: dict[str, Any], entity: str
) -> None:
    book, observed = scope["book"], scope["observed"]
    account = next(item for item in observed.preview.snapshot.retained.accounts if item.receipts)
    receipt = account.receipts[0]
    assert receipt.origin is not None
    identifier = receipt.origin.cash_entry_id if entity == "cash_entry" else receipt.receipt.id
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(con, book["owner"], scope["decision"], scope["proposal"], observed)
        audit(
            con,
            str(book["owner"]),
            entity,
            str(identifier),
            "SYNTHETIC_LATE_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        fresh = rebuild.observe(
            con, book["owner"], scope["decision"].event_id, scope["decision"], scope["proposal"]
        )
        assert fresh is not None and fresh.preview == observed.preview
        current.require_current(con, book["owner"], scope["decision"], scope["proposal"], observed)
    assert database_snapshot(engine) == before


async def test_raw_scope_preserves_mixed_receipts_cash_and_reversals(
    engine: Engine, mixed_scope: dict[str, Any]
) -> None:
    observed, book = mixed_scope["observed"], mixed_scope["book"]
    raw = observed.receipts
    assert raw.owner_id == book["owner"] and raw.event_id == book["event"].event_id
    assert [row[0] for row in raw.entitlements] == [mixed_scope["entitlement"]]
    assert {row[0] for row in raw.legacy_receipts} == {
        mixed_scope["legacy"],
        mixed_scope["missing_cash"],
    }
    assert raw.legacy_origins == ()
    assert len(raw.direct_receipts) == len(raw.direct_origins) == 2
    assert mixed_scope["voided"]["id"] in {row[0] for row in raw.direct_receipts}
    assert len(raw.cash) == 4
    with unit_of_work(engine) as con:
        reversal = con.exec_driver_sql(
            "SELECT id FROM cash_ledger_entries WHERE reverses_entry_id=?",
            (mixed_scope["voided"]["origin"]["cash_entry_id"],),
        ).scalar_one()
        cash_rows = {
            row["id"]: tuple(row.values())
            for row in con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings()
        }
        assert receipts.read(con, raw.owner_id, raw.event_id) == raw
    assert {row[0] for row in raw.cash} == {
        mixed_scope["legacy_cash"],
        mixed_scope["voided"]["origin"]["cash_entry_id"],
        reversal,
        next(
            str(item.origin.cash_entry_id)
            for account in observed.preview.snapshot.retained.accounts
            for item in account.receipts
            if item.origin is not None and str(item.receipt.id) != mixed_scope["voided"]["id"]
        ),
    }
    assert all(values == cash_rows[identifier] for identifier, values in raw.cash)
    assert raw.audits and str(raw.owner_id) not in repr(raw)
    assert mixed_scope["legacy"] not in repr(raw) and "6000000" not in repr(raw)
    retained = next(item for item in observed.preview.snapshot.retained.accounts if item.receipts)
    assert retained.unverified_origins == 2
    assert sum(item.receipt.cash_scope == "CASH_NOT_RECORDED" for item in retained.receipts) == 1


@pytest.mark.parametrize(
    "entity",
    [
        "cash_entry",
        "dividend_receipt",
        "dividend_receipt_intent",
        "dividend_receipt_http_intent",
        "dividend_receipt_void_intent",
        "dividend_receipt_void_http_intent",
    ],
)
async def test_unverified_legacy_receipt_audit_changes_the_observation(
    engine: Engine, mixed_scope: dict[str, Any], entity: str
) -> None:
    book, observed = mixed_scope["book"], mixed_scope["observed"]
    identifier = mixed_scope["legacy_cash"] if entity == "cash_entry" else mixed_scope["legacy"]
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        audit(
            con,
            str(book["owner"]),
            entity,
            identifier,
            "SYNTHETIC_LATE_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        fresh = rebuild.observe(
            con,
            book["owner"],
            mixed_scope["decision"].event_id,
            mixed_scope["decision"],
            mixed_scope["proposal"],
        )
        assert fresh is not None and fresh.preview == observed.preview
        current.require_current(
            con, book["owner"], mixed_scope["decision"], mixed_scope["proposal"], observed
        )
    assert database_snapshot(engine) == before


async def test_matched_legacy_origin_and_http_intent_stay_in_the_raw_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    created = await create_legacy_receipt(client, book)
    previous = book["second"]
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], decision.event_id, decision, proposed)
    assert observed is not None
    raw = observed.receipts
    assert [item[0] for item in raw.legacy_receipts] == [created["id"]]
    assert [item[0] for item in raw.legacy_origins] == [created["id"]]
    assert [item[0] for item in raw.cash] == [created["cash_entry_id"]]
    assert any(row[1][2] == "dividend_receipt_http_intent" for row in raw.audits)
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        audit(
            con,
            str(book["owner"]),
            "dividend_receipt_http_intent",
            created["id"],
            "SYNTHETIC_LATE_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        fresh = rebuild.observe(con, book["owner"], decision.event_id, decision, proposed)
        assert fresh is not None and fresh.preview == observed.preview
        current.require_current(con, book["owner"], decision, proposed, observed)
    assert database_snapshot(engine) == before


async def test_linked_cash_reversal_audit_changes_the_observation(
    engine: Engine, mixed_scope: dict[str, Any]
) -> None:
    book, observed = mixed_scope["book"], mixed_scope["observed"]
    with unit_of_work(engine) as con:
        reversal = con.exec_driver_sql(
            "SELECT id FROM cash_ledger_entries WHERE reverses_entry_id=?",
            (mixed_scope["voided"]["origin"]["cash_entry_id"],),
        ).scalar_one()
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        audit(
            con,
            str(book["owner"]),
            "cash_entry",
            reversal,
            "SYNTHETIC_LATE_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        current.require_current(
            con, book["owner"], mixed_scope["decision"], mixed_scope["proposal"], observed
        )
    assert database_snapshot(engine) == before


async def test_unrelated_receipt_audit_does_not_change_observation(
    engine: Engine, scope: dict[str, Any]
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_direct_receipt_http_intent",
            str(uuid4()),
            "SYNTHETIC_UNRELATED_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        assert (
            current.require_current(
                con, book["owner"], scope["decision"], scope["proposal"], observed
            )
            == observed
        )
        raise Rollback
    assert database_snapshot(engine) == before


async def test_same_account_receipt_on_another_event_is_outside_the_raw_scope(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, raw = scope["book"], scope["observed"].receipts
    other = manual(
        terms=terms(security_id=book["event"].terms.security_id, currency="USD"),
        reason="Synthetic separate distribution for the same account.",
    )
    with unit_of_work(engine, write=True) as con:
        other_event = events.save(con, book["owner"], other, at=now(), request_id=uuid4())
    other_receipt = direct_receipt(
        engine, {**book, "event": other_event.record}, received_micro=3_000_000
    )
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_direct_receipt_http_intent",
            other_receipt["id"],
            "SYNTHETIC_UNRELATED_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        assert receipts.read(con, raw.owner_id, raw.event_id) == raw
        separate = receipts.read(con, raw.owner_id, other.event_id)
        assert [row[0] for row in separate.direct_receipts] == [other_receipt["id"]]
    assert other_receipt["id"] not in {row[0] for row in raw.direct_receipts}


@pytest.mark.parametrize("invalid", ["null_id", "binary_id", "binary_reason"])
async def test_malformed_linked_audit_fails_closed(
    engine: Engine, scope: dict[str, Any], invalid: str
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    identifier = observed.receipts.direct_receipts[0][0]
    identity: Any = (
        None if invalid == "null_id" else b"private" if invalid == "binary_id" else str(uuid4())
    )
    reason: Any = b"private" if invalid == "binary_reason" else "Synthetic evidence"
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,reason,created_at) "
            "VALUES (?,?,'dividend_direct_receipt_http_intent',?,'SYNTHETIC_LATE_EVIDENCE',?,?)",
            (identity, str(book["owner"]), identifier, reason, stamp()),
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
async def test_forged_receipt_scope_cannot_replace_exact_observed_values(
    engine: Engine, scope: dict[str, Any], change: str
) -> None:
    observed, raw = scope["observed"], scope["observed"].receipts
    if change == "missing_audit":
        forged = replace(raw, audits=raw.audits[:-1])
    elif change == "wrong_owner":
        forged = replace(raw, owner_id=uuid4())
    else:
        identifier, values = raw.cash[0]
        row = list(values)
        index = next(i for i, value in enumerate(row) if type(value) is int and value != 0)
        row[index] = float(row[index])
        forged = replace(raw, cash=((identifier, tuple(row)), *raw.cash[1:]))
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
            replace(observed, receipts=forged),
        )
    assert database_snapshot(engine) == before


async def test_receipt_scope_requires_a_matching_owner_and_open_transaction(
    engine: Engine, scope: dict[str, Any]
) -> None:
    raw = scope["observed"].receipts
    with (
        engine.connect() as con,
        pytest.raises(ValueError, match="^Invalid recorded receipt scope$"),
    ):
        receipts.read(con, raw.owner_id, raw.event_id)
    with unit_of_work(engine) as con:
        for owner, event in (
            (uuid4(), raw.event_id),
            (raw.owner_id, uuid4()),
            (cast(Any, "private-owner"), raw.event_id),
            (raw.owner_id, cast(Any, "private-event")),
        ):
            with pytest.raises(ValueError, match="^Invalid recorded receipt scope$"):
                receipts.read(con, owner, event)
        assert receipts.read(con, raw.owner_id, raw.event_id) == raw


async def test_empty_receipt_scope_can_be_read_after_an_event_only_head_change(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], decision.event_id, decision, proposal(graph))
    assert observed is not None
    raw = observed.receipts
    assert raw.entitlements == raw.legacy_receipts == raw.direct_receipts == raw.cash == ()
    with unit_of_work(engine, write=True) as con:
        result = events.save(con, book["owner"], decision, at=now(), request_id=uuid4())
        assert result.record.revision_id == decision.revision_id
        assert receipts.read(con, raw.owner_id, raw.event_id) == raw
    with unit_of_work(engine) as con:
        assert receipts.read(con, raw.owner_id, raw.event_id) == raw


async def test_receipt_scope_shares_the_observation_wal_snapshot(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, decision, proposal = scope["book"], scope["decision"], scope["proposal"]
    with unit_of_work(engine) as reader:
        observed = rebuild.observe(reader, book["owner"], decision.event_id, decision, proposal)
        assert observed is not None and observed.receipts.direct_receipts
        with unit_of_work(engine, write=True) as writer:
            audit(
                writer,
                str(book["owner"]),
                "dividend_direct_receipt_http_intent",
                observed.receipts.direct_receipts[0][0],
                "SYNTHETIC_LATE_EVIDENCE",
                request_id=str(uuid4()),
                after={"scope": "synthetic"},
            )
        still_observed = rebuild.observe(
            reader, book["owner"], decision.event_id, decision, proposal
        )
        assert still_observed == observed
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as writer,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(writer, book["owner"], decision, proposal, observed)
    assert database_snapshot(engine) == before
