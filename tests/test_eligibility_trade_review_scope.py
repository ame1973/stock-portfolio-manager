"""Recorded eligibility must not erase an independently reviewed creation-audit gap."""

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

from spm_api import trade_history_review, trade_service
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.dividend_eligibility_service import (
    EligibilityHistoryError,
    RecordedEligibilityBook,
    get,
)
from spm_api.trade_codec import as_trade
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_correction_scope import decision
from tests.test_trade_history_review_migration import review_values
from tests.test_trade_unverified_correction import active_auditless_buy, call, revision
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def read(con: Connection, book: dict[str, Any]) -> RecordedEligibilityBook:
    result = get(
        con,
        UUID(book["owner"]["id"]),
        UUID(book["portfolio"]["id"]),
        UUID(book["security"]["id"]),
        ex_date=date(2026, 1, 5),
        as_of=date(2026, 1, 5),
        rule="ORDINARY_PRE_EX_DATE",
    )
    assert result is not None
    return result


def add_creation_audit(
    engine: Engine, book: dict[str, Any], trade: UUID, *, original: dict[str, Any] | None = None
) -> None:
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        audit(
            con,
            book["owner"]["id"],
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after=original if original is not None else as_trade(row).model_dump(mode="json"),
        )


async def test_no_recorded_gap_review_is_not_a_claim_of_complete_ownership(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    await create(client, "trades", payload(book))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = read(con, book)
    assert result.trade_creation_audit_scope == "NO_RECORDED_CREATION_GAP_REVIEW"
    assert result.trade_history_audit_scope == "NO_RETIRED_TRADES"
    assert all(account.history_completeness == "UNVERIFIED" for account in result.accounts)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("withdrawal", ["none", "before", "after"])
async def test_late_matching_audit_never_erases_a_creation_gap_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], withdrawal: str
) -> None:
    trade = active_auditless_buy(engine, book)
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, book)
    assert database_snapshot(engine) == before
    if withdrawal == "before":
        decision(engine, owner, trade, revision=1, action="WITHDRAW")
    add_creation_audit(engine, book, trade)
    if withdrawal == "after":
        decision(engine, owner, trade, revision=1, action="WITHDRAW")
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = read(con, book)
    assert result.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    assert result.trade_history_audit_scope == "NO_RETIRED_TRADES"
    assert {account.recorded_quantity for account in result.accounts} == {"10.00000000"}
    assert all(account.history_completeness == "UNVERIFIED" for account in result.accounts)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("matching", [True, False])
async def test_unverified_correction_does_not_grant_eligibility_or_erase_the_original_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], matching: bool
) -> None:
    trade = active_auditless_buy(engine, book)
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine) as con:
        original = as_trade(
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        ).model_dump(mode="json")
    decision(engine, owner, trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    with unit_of_work(engine, write=True) as con:
        correction = call(
            con, client, "REVISE", trade, pins, uuid4(), revision(book, day="2026-01-02")
        )
    assert correction.authorization.scope == "UNVERIFIED_TRADE_CORRECTION_ONLY"
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, book)
    assert database_snapshot(engine) == before
    late = original if matching else {**original, "quantity": "999.00000000"}
    add_creation_audit(engine, book, trade, original=late)
    before = database_snapshot(engine)
    if not matching:
        with (
            unit_of_work(engine) as con,
            pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
        ):
            read(con, book)
        assert database_snapshot(engine) == before
        return
    with unit_of_work(engine) as con:
        result = read(con, book)
    assert result.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    assert result.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
    assert {account.recorded_quantity for account in result.accounts} == {"11.00000000"}
    assert all(account.history_completeness == "UNVERIFIED" for account in result.accounts)
    assert database_snapshot(engine) == before


async def test_untrusted_review_cannot_be_hidden_by_a_matching_late_creation_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = active_auditless_buy(engine, book)
    with unit_of_work(engine, write=True) as con:
        fake = review_values(con, book["owner"]["id"], str(trade))
        insert(con, "trade_history_reviews", fake)
    add_creation_audit(engine, book, trade)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, book)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("unavailable", [None, ()])
async def test_a_recorded_review_must_remain_readable(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    unavailable: tuple[trade_history_review.Review, ...] | None,
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    add_creation_audit(engine, book, trade)
    before = database_snapshot(engine)
    with (
        patch.object(trade_history_review, "history", return_value=unavailable),
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, book)
    assert database_snapshot(engine) == before


async def test_a_corrupt_review_is_not_hidden_by_another_reviewed_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    owner = UUID(book["owner"]["id"])
    reviewed = active_auditless_buy(engine, book)
    decision(engine, owner, reviewed)
    add_creation_audit(engine, book, reviewed)
    invalid = active_auditless_buy(engine, book)
    with unit_of_work(engine, write=True) as con:
        fake = review_values(con, str(owner), str(invalid))
        insert(con, "trade_history_reviews", fake)
    add_creation_audit(engine, book, invalid)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, book)
    assert database_snapshot(engine) == before


async def test_review_and_portfolio_scope_share_one_wal_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = active_auditless_buy(engine, book)
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    add_creation_audit(engine, book, trade)
    before = database_snapshot(engine)
    original_history = trade_history_review.history
    withdrawn = False

    def concurrent_withdrawal(
        con: Connection, actual_owner: UUID, identifier: UUID
    ) -> tuple[trade_history_review.Review, ...] | None:
        nonlocal withdrawn
        reviews = original_history(con, actual_owner, identifier)
        if identifier == trade and not withdrawn:
            withdrawn = True
            decision(engine, owner, trade, revision=1, action="WITHDRAW")
            assert original_history(con, actual_owner, identifier) == reviews
        return reviews

    with (
        patch.object(trade_history_review, "history", side_effect=concurrent_withdrawal),
        unit_of_work(engine) as con,
    ):
        result = read(con, book)
    assert withdrawn and result.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    with unit_of_work(engine) as con:
        reviews = original_history(con, owner, trade)
        assert reviews is not None and [item.revision for item in reviews] == [1, 2]
        assert read(con, book).data_revision > result.data_revision
    after = database_snapshot(engine)
    changed = {"audit_events", "trade_history_reviews", "portfolios", "projection_invalidations"}
    assert {key: value for key, value in before.items() if key not in changed} == {
        key: value for key, value in after.items() if key not in changed
    }


async def test_a_creation_review_for_another_security_does_not_taint_this_book(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    await create(client, "trades", payload(book))
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHER",
            "name": "Other synthetic security",
            "quote_currency": "USD",
        },
    )
    other = {**book, "security": security}
    trade = active_auditless_buy(engine, other)
    decision(engine, UUID(book["owner"]["id"]), trade)
    add_creation_audit(engine, other, trade)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        first = read(con, book)
        second = read(con, other)
    assert first.trade_creation_audit_scope == "NO_RECORDED_CREATION_GAP_REVIEW"
    assert second.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("move", ["security", "portfolio"])
async def test_reviewed_gap_follows_a_root_moved_to_another_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], move: str
) -> None:
    if move == "security":
        security = await create(
            client,
            "securities",
            {
                "exchange_code": "NASDAQ",
                "display_ticker": "MOVED",
                "name": "Moved synthetic security",
                "quote_currency": "USD",
            },
        )
        other = {**book, "security": security}
    else:
        portfolio = await create(client, "portfolios", {"name": "Moved synthetic portfolio"})
        account = await create(
            client,
            "accounts",
            {
                "portfolio_id": portfolio["id"],
                "broker_id": book["broker"]["id"],
                "name": "Moved synthetic account",
                "default_currency": "USD",
            },
        )
        other = {**book, "portfolio": portfolio, "account": account}
    trade = active_auditless_buy(engine, book)
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine) as con:
        original = as_trade(
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        ).model_dump(mode="json")
    decision(engine, owner, trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    with unit_of_work(engine, write=True) as con:
        call(con, client, "REVISE", trade, pins, uuid4(), revision(other, day="2026-01-02"))
    add_creation_audit(engine, book, trade, original=original)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        first = read(con, book)
        second = read(con, other)
    assert first.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    assert second.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    assert {account.recorded_quantity for account in first.accounts} == {"0.00000000"}
    assert {account.recorded_quantity for account in second.accounts} == {"11.00000000"}
    assert database_snapshot(engine) == before


async def test_retired_origin_gap_remains_separate_from_a_creation_gap_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
    ):
        trade = await create(client, "trades", payload(book))
        result = await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=payload(book, quantity="11", reason="Correct synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
    assert result.status_code == 200
    decision(engine, UUID(book["owner"]["id"]), UUID(trade["id"]), trade_version=2)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        current = read(con, book)
    assert current.trade_creation_audit_scope == "NO_RECORDED_CREATION_GAP_REVIEW"
    assert current.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
    assert database_snapshot(engine) == before
