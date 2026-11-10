"""Current unverified correction scope is exact, revocable, and never a financial grant."""

from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api import trade_history_review as review
from spm_api import trade_service
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.trade_codec import as_trade
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio
REASON = "Review this synthetic unverified correction scope"


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def decision(
    engine: Engine,
    owner: UUID,
    trade: UUID,
    *,
    trade_version: int = 1,
    revision: int = 0,
    action: Literal["ACKNOWLEDGE_UNVERIFIED", "WITHDRAW"] = "ACKNOWLEDGE_UNVERIFIED",
) -> review.Review:
    with unit_of_work(engine, write=True) as con:
        return review.save(
            con,
            owner,
            trade,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=trade_version,
            expected_revision=revision,
            decision=action,
            reason=REASON,
        )


def second_auditless_revision(engine: Engine, first: UUID) -> UUID:
    """Create new synthetic predecessor/successor facts without changing old audits."""
    successor = uuid4()
    with unit_of_work(engine, write=True) as con:
        previous = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(first),)).mappings().one()
        )
        trade_service._retire(con, previous, "SUPERSEDED")
        at = stamp()
        insert(
            con,
            "trades",
            {
                **dict(previous),
                "id": str(successor),
                "revision": 2,
                "supersedes_id": str(first),
                "created_at": at,
                "updated_at": at,
            },
        )
    return successor


async def test_scope_rechecks_the_exact_current_acknowledgment_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    pin = review.CorrectionReviewPin(trade, 1, 1)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_REQUIRED"),
    ):
        review.require_correction_reviews(con, owner, trade, (pin,))
    assert database_snapshot(engine) == before
    acknowledged = decision(engine, owner, trade)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert review.correction_requires_review(con, owner, trade)
        assert review.require_correction_reviews(con, owner, trade, (pin,)) == (acknowledged,)
    assert str(trade) not in repr(pin)
    assert database_snapshot(engine) == before
    with (
        unit_of_work(engine) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_WRITE_REQUIRED"),
    ):
        review.require_correction_reviews(con, owner, trade, (pin,))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_successful_scope_check_does_not_grant_a_trade_change(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], method: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert review.require_correction_reviews(
            con, owner, trade, (review.CorrectionReviewPin(trade, 1, 1),)
        )
    change = (
        payload(book, trade_date="2999-01-02", fx_rate_hkd=None, reason=REASON)
        if method == "PATCH"
        else {"reason": REASON}
    )
    response = await client.request(
        method, f"/api/v1/trades/{trade}", json=change, headers=version_headers(client, 1)
    )
    assert response.status_code == 409 and response.json()["code"] == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


async def test_withdrawal_and_review_revision_fence_new_use_without_changing_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    original = decision(engine, owner, trade)
    decision(engine, owner, trade, revision=1, action="WITHDRAW")
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_REQUIRED"),
    ):
        review.require_correction_reviews(
            con, owner, trade, (review.CorrectionReviewPin(trade, 1, 1),)
        )
    assert database_snapshot(engine) == before
    renewed = decision(engine, owner, trade, revision=2)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_VERSION_CONFLICT"):
            review.require_correction_reviews(
                con, owner, trade, (review.CorrectionReviewPin(trade, 1, 1),)
            )
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_VERSION_CONFLICT"):
            review.require_correction_reviews(
                con, owner, trade, (review.CorrectionReviewPin(trade, 2, 3),)
            )
        assert review.require_correction_reviews(
            con, owner, trade, (review.CorrectionReviewPin(trade, 1, 3),)
        ) == (renewed,)
        history = review.history(con, owner, trade)
        assert history is not None and history[0] == original
    assert database_snapshot(engine) == before


async def test_every_missing_creation_in_the_root_requires_an_exact_pin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, first)
    owner = UUID(book["owner"]["id"])
    first_review = decision(engine, owner, first, trade_version=2)
    current_review = decision(engine, owner, current)
    first_pin = review.CorrectionReviewPin(first, 2, 1)
    current_pin = review.CorrectionReviewPin(current, 1, 1)
    unrelated = review.CorrectionReviewPin(uuid4(), 1, 1)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        for pins in ((first_pin,), (current_pin,), (first_pin, current_pin, unrelated)):
            with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_SCOPE_CHANGED"):
                review.require_correction_reviews(con, owner, current, pins)
        assert review.require_correction_reviews(con, owner, current, (current_pin, first_pin)) == (
            first_review,
            current_review,
        )
    assert database_snapshot(engine) == before
    decision(engine, owner, first, trade_version=2, revision=1, action="WITHDRAW")
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_REQUIRED"),
    ):
        review.require_correction_reviews(con, owner, current, (first_pin, current_pin))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("withdrawn", [False, True])
async def test_later_matching_audit_does_not_reauthorize_a_reviewed_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], withdrawn: bool
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    if withdrawn:
        decision(engine, owner, trade, revision=1, action="WITHDRAW")
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
        assert review.correction_requires_review(con, owner, trade)
        with pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CHANGED"):
            review.require_correction_reviews(
                con, owner, trade, (review.CorrectionReviewPin(trade, 1, 1),)
            )
    assert database_snapshot(engine) == before


async def test_conflicting_later_audit_fails_closed_instead_of_granting_use(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "conflicting creation evidence"},
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_UNAVAILABLE"),
    ):
        review.require_correction_reviews(
            con, owner, trade, (review.CorrectionReviewPin(trade, 1, 1),)
        )
    assert database_snapshot(engine) == before


async def test_verified_trade_does_not_accept_an_unneeded_unverified_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(
        (await create(client, "trades", payload(book, trade_date="2999-01-02", fx_rate_hkd=None)))[
            "id"
        ]
    )
    owner = UUID(book["owner"]["id"])
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_NOT_REQUIRED"),
    ):
        review.require_correction_reviews(con, owner, trade, ())
    assert database_snapshot(engine) == before


async def test_void_target_cannot_get_a_new_correction_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        trade_service._retire(con, row, "VOID")
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "VOID",
            request_id=str(uuid4()),
            before=dict(row),
            after={"status": "VOID", "version": 2},
            reason="Void this synthetic unverified trade",
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_CHANGED"),
    ):
        review.require_correction_reviews(
            con, owner, trade, (review.CorrectionReviewPin(trade, 2, 1),)
        )
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_obtain_a_correction_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner, foreign = UUID(book["owner"]["id"]), uuid4()
    decision(engine, owner, trade)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(foreign),),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(review.TradeReviewError, match="TRADE_REVIEW_NOT_FOUND"),
    ):
        review.require_correction_reviews(
            con, foreign, trade, (review.CorrectionReviewPin(trade, 1, 1),)
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", ["duplicate", "version", "revision", "boolean", "foreign"])
async def test_invalid_or_cross_owner_pins_never_authorize_or_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], invalid: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    pin = review.CorrectionReviewPin(trade, 1, 1)
    pins = {
        "duplicate": (pin, pin),
        "version": (review.CorrectionReviewPin(trade, 0, 1),),
        "revision": (review.CorrectionReviewPin(trade, 1, 0),),
        "boolean": (review.CorrectionReviewPin(trade, True, 1),),
        "foreign": (review.CorrectionReviewPin(uuid4(), 1, 1),),
    }[invalid]
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con, pytest.raises(review.TradeReviewError):
        review.require_correction_reviews(con, owner, trade, pins)
    assert database_snapshot(engine) == before
