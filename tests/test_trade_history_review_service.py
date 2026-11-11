"""Audited trade-history reviews are unverified, revocable and owner-scoped."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from spm_api import trade_audit, trade_service
from spm_api import trade_history_review as review
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.trade_codec import as_trade
from tests.helpers import create, database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trade_history_review_migration import retired_draft, review_values
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio
REASON = "Explicitly review missing synthetic trade history."


@pytest.fixture(autouse=True)
def exclusive(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


async def test_acknowledgment_withdrawal_and_historical_replay_are_atomic(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, identity, request = UUID(book["owner"]["id"]), uuid4(), uuid4()
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        first = review.save(
            con,
            owner,
            trade,
            review_id=identity,
            request_id=request,
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        assert first.gap == "MISSING_CREATION_AUDIT"
        assert first.scope == "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"
        assert first.history_completeness == "UNVERIFIED"
        assert (
            REASON not in repr(first)
            and str(trade) not in repr(first)
            and str(owner) not in repr(first)
        )
        assert review.require_current(con, owner, trade, trade_version=1) == first
    after = database_snapshot(engine)
    changed = {"audit_events", "trade_history_reviews", "portfolios", "projection_invalidations"}
    assert {k: v for k, v in before.items() if k not in changed} == {
        k: v for k, v in after.items() if k not in changed
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(engine, write=True) as con:
        assert (
            review.save(
                con,
                owner,
                trade,
                review_id=identity,
                request_id=request,
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
            == first
        )
    assert database_snapshot(engine) == after
    with unit_of_work(engine, write=True) as con:
        withdrawn = review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
        assert withdrawn.gap == first.gap
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_REQUIRED"):
            review.require_current(con, owner, trade, trade_version=1)
    after_withdraw = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert (
            review.save(
                con,
                owner,
                trade,
                review_id=identity,
                request_id=request,
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
            == first
        )
    assert database_snapshot(engine) == after_withdraw


async def test_fake_sql_review_cannot_be_read_or_used(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        fake = review_values(con, str(owner), str(trade))
        fake["snapshot_sha256"] = "0" * 64
        insert(con, "trade_history_reviews", fake)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
            review.history(con, owner, trade)
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
            review.require_current(con, owner, trade, trade_version=1)
    assert database_snapshot(engine) == before


async def test_forged_snapshot_with_a_matching_review_audit_still_fails(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, identity, request = UUID(book["owner"]["id"]), uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        values = {
            "id": str(identity),
            "trade_id": str(trade),
            "owner_id": str(owner),
            "revision": 1,
            "previous_review_id": None,
            "decision": "ACKNOWLEDGE_UNVERIFIED",
            "gap": "MISSING_CREATION_AUDIT",
            "observed_status": row["status"],
            "observed_trade_version": row["version"],
            "observed_updated_at": row["updated_at"],
            "snapshot_sha256": "0" * 64,
            "request_id": str(request),
        }
        payload_value = {
            **values,
            "reason_sha256": digest(canonical(REASON)),
            "scope": "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY",
        }
        audit(
            con,
            str(owner),
            "trade_history_review",
            str(identity),
            "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED",
            request_id=str(request),
            after=payload_value,
            reason=REASON,
        )
        event = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events "
            "WHERE entity_id=? AND entity_type='trade_history_review'",
            (str(identity),),
        ).one()
        insert(
            con,
            "trade_history_reviews",
            {**values, "audit_id": event.id, "created_at": event.created_at},
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.history(con, owner, trade)
    assert database_snapshot(engine) == before


async def test_mismatched_audit_is_not_reclassified_as_missing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=True))
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_duplicate_creation_audits_cannot_be_treated_as_a_reviewable_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=True))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "second unrelated creation"},
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_missing_successor_is_not_hidden_by_a_retired_trade_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        trade_service._retire(con, row, "SUPERSEDED")
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=2,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_unicode_whitespace_reason_in_synthetic_sql_review_fails_canonical_read(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        values = review_values(con, str(owner), str(trade), reason=chr(160) + REASON)
        insert(con, "trade_history_reviews", values)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.history(con, owner, trade)
    assert database_snapshot(engine) == before


async def test_duplicate_earlier_audit_cannot_hide_behind_a_valid_withdrawal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, identity, request = UUID(book["owner"]["id"]), uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            trade,
            review_id=identity,
            request_id=request,
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "trade_history_review",
            str(identity),
            "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED",
            request_id=str(uuid4()),
            after={"synthetic": "unrelated duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
            review.history(con, owner, trade)
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
            review.save(
                con,
                owner,
                trade,
                review_id=identity,
                request_id=request,
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
    assert database_snapshot(engine) == before


async def test_later_conflicting_creation_audit_is_not_hidden_by_an_existing_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "unrelated later audit"},
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.history(con, owner, trade)
    assert database_snapshot(engine) == before


async def test_later_matching_creation_audit_keeps_history_but_ends_review_use(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        first = review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json"),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert review.history(con, owner, trade) == (first,)
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CHANGED"):
            review.require_current(con, owner, trade, trade_version=1)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("matching", [False, True])
async def test_reviewed_current_state_requires_actual_retirement_hash_after_retirement(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], matching: bool
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, identity, request = UUID(book["owner"]["id"]), uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        first = review.save(
            con,
            owner,
            trade,
            review_id=identity,
            request_id=request,
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        trade_service._retire(con, row, "SUPERSEDED")
        next_id, at = str(uuid4()), stamp()
        insert(
            con,
            "trades",
            {
                **dict(row),
                "id": next_id,
                "revision": 2,
                "supersedes_id": str(trade),
                "created_at": at,
                "updated_at": at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (next_id,)).mappings().one()
        )
        audit(
            con,
            str(owner),
            "trade",
            next_id,
            "REVISE",
            request_id=str(uuid4()),
            before=dict(row) if matching else {"synthetic": "unrelated predecessor"},
            after=as_trade(successor).model_dump(mode="json"),
            reason=REASON,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        if matching:
            assert review.history(con, owner, trade) == (first,)
            assert review.require_current(con, owner, trade, trade_version=2) == first
            assert (
                review.save(
                    con,
                    owner,
                    trade,
                    review_id=identity,
                    request_id=request,
                    expected_trade_version=1,
                    expected_revision=0,
                    decision="ACKNOWLEDGE_UNVERIFIED",
                    reason=REASON,
                )
                == first
            )
        else:
            with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
                review.history(con, owner, trade)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("matching", [False, True])
async def test_later_creation_audit_changes_the_gap_after_reviewed_trade_is_retired(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], matching: bool
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        first = review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        original = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        )
        trade_service._retire(con, original, "SUPERSEDED")
        next_id, at = str(uuid4()), stamp()
        insert(
            con,
            "trades",
            {
                **dict(original),
                "id": next_id,
                "revision": 2,
                "supersedes_id": str(trade),
                "created_at": at,
                "updated_at": at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (next_id,)).mappings().one()
        )
        audit(
            con,
            str(owner),
            "trade",
            next_id,
            "REVISE",
            request_id=str(uuid4()),
            before=dict(original),
            after=as_trade(successor).model_dump(mode="json"),
            reason=REASON,
        )
        assert review.require_current(con, owner, trade, trade_version=2) == first
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after=(
                as_trade(original).model_dump(mode="json")
                if matching
                else {"synthetic": "unrelated later audit"}
            ),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        if matching:
            assert review.history(con, owner, trade) == (first,)
            with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CHANGED"):
                review.require_current(con, owner, trade, trade_version=2)
        else:
            with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
                review.history(con, owner, trade)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["valid", "missing", "incorrect"])
async def test_void_history_requires_its_real_structural_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], state: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        trade_service._retire(con, row, "VOID")
        if state != "missing":
            audit(
                con,
                str(owner),
                "trade",
                str(trade),
                "VOID",
                request_id=str(uuid4()),
                before=dict(row),
                after={"status": "VOID", "version": 2 if state == "valid" else 99},
                reason=REASON,
            )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        if state == "valid":
            result = review.save(
                con,
                owner,
                trade,
                review_id=uuid4(),
                request_id=uuid4(),
                expected_trade_version=2,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
            assert result.observed_status == "VOID" and result.gap == "MISSING_CREATION_AUDIT"
        else:
            with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"):
                review.save(
                    con,
                    owner,
                    trade,
                    review_id=uuid4(),
                    request_id=uuid4(),
                    expected_trade_version=2,
                    expected_revision=0,
                    decision="ACKNOWLEDGE_UNVERIFIED",
                    reason=REASON,
                )
    after = database_snapshot(engine)
    if state != "valid":
        assert after == before
    else:
        assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]


async def test_retired_review_survives_origin_recovery_and_withdrawal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    identifier, original = retired_draft(engine, book, has_creation_audit=True)
    trade, owner, identity, request = UUID(identifier), UUID(book["owner"]["id"]), uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        first = review.save(
            con,
            owner,
            trade,
            review_id=identity,
            request_id=request,
            expected_trade_version=2,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        assert first.gap == "MISSING_RETIRED_ORIGIN"
    with unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(con, owner, trade, original, uuid4(), REASON)
    with unit_of_work(engine, write=True) as con:
        assert review.history(con, owner, trade) == (first,)
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CHANGED"):
            review.require_current(con, owner, trade, trade_version=2)
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=2,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    after = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert (
            review.save(
                con,
                owner,
                trade,
                review_id=identity,
                request_id=request,
                expected_trade_version=2,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
            == first
        )
    assert database_snapshot(engine) == after


async def test_late_failure_caught_by_caller_rolls_back_the_inner_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)

    def fail(*args: Any) -> None:
        raise ValueError("synthetic late invalidation failure")

    monkeypatch.setattr(trade_audit, "_invalidate_root", fail)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_caught_savepoint_release_failure_aborts_review_and_outer_work(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with (
            patch.object(
                engine.dialect,
                "do_release_savepoint",
                side_effect=SQLAlchemyError("synthetic release failure"),
            ),
            pytest.raises(SQLAlchemyError),
        ):
            review.save(
                con,
                owner,
                trade,
                review_id=uuid4(),
                request_id=uuid4(),
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
        assert not con.in_transaction()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["orphan", "missing"])
async def test_unassociated_or_missing_review_audit_fails_safely_without_partial_write(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, identity = UUID(book["owner"]["id"]), uuid4()
    if failure == "orphan":
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                str(owner),
                "trade_history_review",
                str(identity),
                "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED",
                request_id=str(uuid4()),
                after={"synthetic": "unassociated audit"},
                reason=REASON,
            )
    else:
        monkeypatch.setattr(review, "audit", lambda *args, **kwargs: None)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=identity,
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_backward_clock_aborts_new_decision_without_partial_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    before = database_snapshot(engine)
    monkeypatch.setattr("spm_api.audit.now", lambda: datetime(2000, 1, 1, tzinfo=UTC))
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_TIME_CONFLICT"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_same_request_id_cannot_be_reused_for_a_different_decision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, request = UUID(book["owner"]["id"]), uuid4()
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=request,
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CONFLICT"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=request,
            expected_trade_version=1,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    assert database_snapshot(engine) == before


async def test_concurrent_decisions_fence_revision_and_invalidate_once(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)

    def attempt() -> str:
        try:
            with unit_of_work(engine, write=True) as con:
                review.save(
                    con,
                    owner,
                    trade,
                    review_id=uuid4(),
                    request_id=uuid4(),
                    expected_trade_version=1,
                    expected_revision=0,
                    decision="ACKNOWLEDGE_UNVERIFIED",
                    reason=REASON,
                )
            return "SAVED"
        except review.TradeReviewError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["SAVED", "TRADE_REVIEW_VERSION_CONFLICT"]
    after = database_snapshot(engine)
    assert len(after["trade_history_reviews"]) == 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1


async def test_review_invalidates_all_owned_portfolios_in_the_trade_root(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    other = await create(client, "portfolios", {"name": "Other synthetic review portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other["id"],
            "broker_id": book["broker"]["id"],
            "name": "Other synthetic review account",
            "default_currency": "USD",
        },
    )
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        trade_service._retire(con, row, "SUPERSEDED")
        next_id, at = str(uuid4()), stamp()
        insert(
            con,
            "trades",
            {
                **dict(row),
                "id": next_id,
                "revision": 2,
                "supersedes_id": str(trade),
                "portfolio_id": other["id"],
                "account_id": account["id"],
                "trade_date": "2998-12-31",
                "created_at": at,
                "updated_at": at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (next_id,)).mappings().one()
        )
        audit(
            con,
            str(owner),
            "trade",
            next_id,
            "REVISE",
            request_id=str(uuid4()),
            before=dict(row),
            after=as_trade(successor).model_dump(mode="json"),
            reason=REASON,
        )
    with unit_of_work(engine) as con:
        before = {
            str(row[0]): int(row[1])
            for row in con.exec_driver_sql(
                "SELECT id,data_revision FROM portfolios WHERE id IN (?,?)",
                (book["portfolio"]["id"], other["id"]),
            )
        }
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=2,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        after = {
            str(row[0]): int(row[1])
            for row in con.exec_driver_sql(
                "SELECT id,data_revision FROM portfolios WHERE id IN (?,?)",
                (book["portfolio"]["id"], other["id"]),
            )
        }
        assert after == {key: value + 1 for key, value in before.items()}
        rows = con.exec_driver_sql(
            "SELECT portfolio_id,from_date,reason FROM projection_invalidations "
            "WHERE source_id=? AND reason='TRADE_HISTORY_REVIEW_CHANGED'",
            (str(trade),),
        ).all()
        assert set(map(tuple, rows)) == {
            (book["portfolio"]["id"], "2998-12-31", "TRADE_HISTORY_REVIEW_CHANGED"),
            (other["id"], "2998-12-31", "TRADE_HISTORY_REVIEW_CHANGED"),
        }


async def test_outer_transaction_rolls_back_a_successful_nested_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)
    with (
        pytest.raises(RuntimeError, match="synthetic outer rollback"),
        unit_of_work(engine, write=True) as con,
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
        raise RuntimeError("synthetic outer rollback")
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "case",
    [
        "owner",
        "trade",
        "review",
        "request",
        "trade-version",
        "review-version",
        "decision",
        "reason",
    ],
)
async def test_invalid_scope_or_reason_never_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], case: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)
    values: dict[str, Any] = {
        "owner": owner,
        "identifier": trade,
        "review_id": uuid4(),
        "request_id": uuid4(),
        "expected_trade_version": 1,
        "expected_revision": 0,
        "decision": "ACKNOWLEDGE_UNVERIFIED",
        "reason": REASON,
    }
    key = {
        "owner": "owner",
        "trade": "identifier",
        "review": "review_id",
        "request": "request_id",
        "trade-version": "expected_trade_version",
        "review-version": "expected_revision",
        "decision": "decision",
        "reason": "reason",
    }[case]
    values[key] = {
        "trade-version": True,
        "review-version": -1,
        "decision": "VERIFIED",
        "reason": "short",
    }.get(case, "not-a-uuid")
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="INVALID_TRADE_REVIEW_SCOPE"),
    ):
        review.save(con, **values)
    assert database_snapshot(engine) == before


async def test_writer_transaction_is_required_and_foreign_owner_cannot_read(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, foreign = UUID(book["owner"]["id"]), uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(foreign),),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert review.history(con, foreign, trade) is None
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_WRITE_REQUIRED"):
            review.save(
                con,
                owner,
                trade,
                review_id=uuid4(),
                request_id=uuid4(),
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_WRITE_REQUIRED"):
            review.require_current(con, owner, trade, trade_version=1)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_NOT_FOUND"):
            review.save(
                con,
                foreign,
                trade,
                review_id=uuid4(),
                request_id=uuid4(),
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_NOT_FOUND"):
            review.require_current(con, foreign, trade, trade_version=1)
    assert database_snapshot(engine) == before


async def test_review_id_cannot_be_reused_for_another_trade(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    owner, identity = UUID(book["owner"]["id"]), uuid4()
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    second = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            first,
            review_id=identity,
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        for version in (2, True):
            with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_VERSION_CONFLICT"):
                review.require_current(con, owner, first, trade_version=version)
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CONFLICT"):
            review.save(
                con,
                owner,
                second,
                review_id=identity,
                request_id=uuid4(),
                expected_trade_version=1,
                expected_revision=0,
                decision="ACKNOWLEDGE_UNVERIFIED",
                reason=REASON,
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["trade-version", "review-version", "decision", "same-id"])
async def test_stale_or_changed_review_intents_leave_no_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, identity, request = UUID(book["owner"]["id"]), uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        review.save(
            con,
            owner,
            trade,
            review_id=identity,
            request_id=request,
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    before = database_snapshot(engine)
    values: dict[str, Any] = {
        "review_id": uuid4(),
        "request_id": uuid4(),
        "expected_trade_version": 1,
        "expected_revision": 1,
        "decision": "WITHDRAW",
        "reason": REASON,
    }
    code = "TRADE_REVIEW_VERSION_CONFLICT"
    if failure == "trade-version":
        values["expected_trade_version"] = 2
    elif failure == "review-version":
        values["expected_revision"] = 0
    elif failure == "decision":
        values["decision"] = "ACKNOWLEDGE_UNVERIFIED"
        code = "TRADE_REVIEW_DECISION_CONFLICT"
    else:
        values.update(
            review_id=identity, request_id=request, expected_revision=0, reason=REASON + " Changed."
        )
        code = "TRADE_REVIEW_CONFLICT"
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match=code),
    ):
        review.save(con, owner, trade, **values)
    assert database_snapshot(engine) == before


async def test_a_valid_current_trade_with_origin_does_not_need_an_unverified_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID((await create(client, "trades", payload(book)))["id"])
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_NOT_REQUIRED"),
    ):
        review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=1,
            expected_revision=0,
            decision="ACKNOWLEDGE_UNVERIFIED",
            reason=REASON,
        )
    assert database_snapshot(engine) == before
