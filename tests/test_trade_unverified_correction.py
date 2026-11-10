"""Explicit unverified correction writes are atomic, audited, and not HTTP authorization."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from spm_api import (
    trade_audit,
    trade_correction_authorization,
    trade_history_review,
    trade_service,
    trade_unverified_correction,
)
from spm_api.audit import audit, canonical, stamp
from spm_api.auth_models import Owner
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.finance import FinanceError
from spm_api.ledger_service import allocate_sequence, post_cash
from spm_api.security import COOKIE, read_auth
from spm_api.trade_models import TradeRevision
from tests.helpers import create, database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trade_correction_authorization import audit_payload
from tests.test_trade_correction_authorization_migration import REASON as SCHEMA_REASON
from tests.test_trade_correction_authorization_migration import authorization_values
from tests.test_trade_correction_scope import decision, second_auditless_revision
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio
REASON = "Explicitly correct this unverified synthetic trade"


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def active_auditless_buy(
    engine: Engine, book: dict[str, Any], *, with_settlement: bool = True
) -> UUID:
    identifier, at = uuid4(), stamp()
    with unit_of_work(engine, write=True) as con:
        sequence = allocate_sequence(con, "TRADE", str(identifier))
        insert(
            con,
            "trades",
            {
                "id": str(identifier),
                "root_id": str(identifier),
                "created_sequence": sequence,
                "portfolio_id": book["portfolio"]["id"],
                "account_id": book["account"]["id"],
                "security_id": book["security"]["id"],
                "side": "BUY",
                "trade_date": "2026-01-02",
                "quantity_e8": 1000000000,
                "price_e8": 10000000000,
                "currency": "USD",
                "fx_rate_hkd_e12": 7800000000000,
                "fx_source": "MANUAL",
                "gross_micro": 1000000000,
                "settlement_micro": 1000000000,
                "booked_hkd_micro": 7800000000,
                "status": "ACTIVE",
                "created_at": at,
                "updated_at": at,
            },
        )
        if with_settlement:
            post_cash(
                con,
                account=book["account"]["id"],
                day="2026-01-02",
                currency="USD",
                amount=-1000000000,
                fx=7800000000000,
                booked=-7800000000,
                kind="TRADE_SETTLEMENT",
                source_type="TRADE",
                source_id=str(identifier),
            )
    return identifier


def audited_current_with_a_missing_ancestor(
    engine: Engine, book: dict[str, Any]
) -> tuple[UUID, UUID]:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, first)
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        old = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(first),)).mappings().one()
        row = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(current),)).mappings().one()
        )
        before = dict(old)
        before.update(status="DRAFT_SCHEDULED", version=1, updated_at=old["created_at"])
        audit(
            con,
            str(owner),
            "trade",
            str(current),
            "REVISE",
            request_id=str(uuid4()),
            before=before,
            after=trade_service.as_trade(row).model_dump(mode="json"),
            reason="Revise this synthetic legacy predecessor",
        )
    decision(engine, owner, first, trade_version=2)
    return first, current


def revision(
    book: dict[str, Any], *, quantity: str = "11", day: str = "2999-01-02"
) -> TradeRevision:
    return TradeRevision.model_validate(
        payload(book, trade_date=day, quantity=quantity, fx_rate_hkd="7.9", reason=REASON)
    )


def call(
    con: Any,
    client: httpx.AsyncClient,
    operation: Literal["REVISE", "VOID"],
    trade: UUID,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
    request: UUID,
    body: TradeRevision,
) -> trade_unverified_correction.CorrectionResult:
    auth = read_auth(con, client.cookies.get(COOKIE))
    if operation == "REVISE":
        return trade_unverified_correction.revise(
            con, auth, trade, version=1, body=body, pins=pins, request_id=request
        )
    return trade_unverified_correction.void(
        con, auth, trade, version=1, reason=REASON, pins=pins, request_id=request
    )


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_draft_change_records_one_audited_scope_and_replays_without_writes(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    acknowledged = decision(engine, owner, trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    request, body = uuid4(), revision(book)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        result = call(con, client, operation, trade, pins, request, body)
    after = database_snapshot(engine)
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after["trade_correction_authorizations"]) == 1
    assert after["cash_ledger_entries"] == before["cash_ledger_entries"]
    assert result.authorization.operation == operation
    assert result.authorization.history_completeness == "UNVERIFIED"
    assert result.authorization.pins[0].review_id == acknowledged.id
    assert result.trade.status == ("DRAFT_SCHEDULED" if operation == "REVISE" else "VOID")
    assert str(trade) not in repr(result) and REASON not in repr(result)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='trade' AND entity_id=? "
                "AND action='CREATE'",
                (str(trade),),
            ).scalar_one()
            == 0
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM trade_audit_origins WHERE trade_id=?", (str(trade),)
            ).scalar_one()
            == 0
        )
        assert trade_correction_authorization.get(con, owner, result.authorization.id) == (
            result.authorization
        )
    with unit_of_work(engine, write=True) as con:
        replay = call(con, client, operation, trade, pins, request, body)
    assert replay == result and database_snapshot(engine) == after
    decision(engine, owner, trade, trade_version=2, revision=1, action="WITHDRAW")
    after_withdrawal = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        replay = call(con, client, operation, trade, pins, request, body)
    assert replay == result and database_snapshot(engine) == after_withdrawal


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_active_change_preserves_old_fx_and_posts_exact_cash_reversal(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    trade = active_auditless_buy(engine, book)
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        result = call(
            con, client, operation, trade, pins, uuid4(), revision(book, day="2026-01-02")
        )
    after = database_snapshot(engine)
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro,entry_role "
            "FROM cash_ledger_entries WHERE source_type='TRADE' AND source_id=? ORDER BY rowid",
            (str(trade),),
        ).all()
        assert [tuple(row) for row in rows] == [
            (-1000000000, 7800000000000, -7800000000, "PRIMARY"),
            (1000000000, 7800000000000, 7800000000, "REVERSAL"),
        ]
        if operation == "REVISE":
            replacement = con.exec_driver_sql(
                "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
                "WHERE source_type='TRADE' AND source_id=? AND entry_role='PRIMARY'",
                (str(result.trade.id),),
            ).one()
            assert tuple(replacement) == (-1100000000, 7900000000000, -8690000000)
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='trade' AND entity_id=? "
                "AND action='CREATE'",
                (str(trade),),
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("case", ["draft-no-fx", "actual-usd", "hkd-identity"])
async def test_revision_replay_recalculates_exact_original_terms(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], case: str
) -> None:
    trade = (
        active_auditless_buy(engine, book)
        if case == "actual-usd"
        else UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    )
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    params: dict[str, Any] = {"trade_date": "2999-01-02", "fx_rate_hkd": None}
    if case == "actual-usd":
        params = {
            "trade_date": "2026-01-02",
            "fx_rate_hkd": "7.9",
            "fx_source": "synthetic broker statement",
            "actual_settlement": "1099.125001",
            "actual_hkd": "8683.444444",
            "override_reason": "Explicit synthetic broker settlement evidence",
        }
    elif case == "hkd-identity":
        security = await create(
            client,
            "securities",
            {
                "exchange_code": "HKG",
                "display_ticker": "1234",
                "name": "Synthetic Hong Kong security",
                "quote_currency": "HKD",
            },
        )
        params.update(security_id=security["id"], currency="HKD")
    body = TradeRevision.model_validate(payload(book, reason=REASON, **params))
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    request = uuid4()
    with unit_of_work(engine, write=True) as con:
        result = call(con, client, "REVISE", trade, pins, request, body)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        replayed = call(con, client, "REVISE", trade, pins, request, body)
    assert replayed == result and database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "12.00000000"),
        ("price", "101.00000000"),
        ("fee", "1.000000"),
        ("tax", "1.000000"),
        ("side", "SELL"),
        ("trade_date", "2026-01-03"),
        ("fx_rate_hkd", "7.800000000000"),
        ("fx_source", "IDENTITY"),
        ("gross", "0.000000"),
        ("settlement", "0.000000"),
        ("rounding_delta", "1.000000"),
        ("booked_hkd", "1.000000"),
        ("settlement_override", True),
        ("hkd_override", True),
        ("status", "PENDING_FX"),
        ("note", "different synthetic note"),
    ],
)
async def test_revision_replay_rejects_each_changed_financial_term(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: object
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = revision(book, day="2026-01-02")
    with unit_of_work(engine, write=True) as con:
        result = call(con, client, "REVISE", trade, pins, uuid4(), body)
    before = database_snapshot(engine)
    assert trade_unverified_correction._matches_revision(body, result.trade)
    altered = result.trade.model_copy(update={field: value})
    assert not trade_unverified_correction._matches_revision(body, altered)
    assert database_snapshot(engine) == before


async def test_unverified_sale_revision_preserves_original_cash_and_exact_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    await create(client, "trades", payload(book, trade_date="2026-01-02", quantity="10"))
    trade, at = uuid4(), stamp()
    with unit_of_work(engine, write=True) as con:
        sequence = allocate_sequence(con, "TRADE", str(trade))
        insert(
            con,
            "trades",
            {
                "id": str(trade),
                "root_id": str(trade),
                "created_sequence": sequence,
                "portfolio_id": book["portfolio"]["id"],
                "account_id": book["account"]["id"],
                "security_id": book["security"]["id"],
                "side": "SELL",
                "trade_date": "2026-01-03",
                "quantity_e8": 400000000,
                "price_e8": 11000000000,
                "currency": "USD",
                "fx_rate_hkd_e12": 7800000000000,
                "fx_source": "MANUAL",
                "gross_micro": 440000000,
                "settlement_micro": 440000000,
                "booked_hkd_micro": 3432000000,
                "status": "ACTIVE",
                "created_at": at,
                "updated_at": at,
            },
        )
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        trade_service._post_settlement(con, row)
    decision(engine, UUID(book["owner"]["id"]), trade)
    body = TradeRevision.model_validate(
        payload(
            book,
            side="SELL",
            trade_date="2026-01-03",
            quantity="5",
            price="120",
            fee="2",
            tax="1",
            fx_rate_hkd="7.9",
            reason=REASON,
        )
    )
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    request = uuid4()
    with unit_of_work(engine, write=True) as con:
        result = call(con, client, "REVISE", trade, pins, request, body)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert call(con, client, "REVISE", trade, pins, request, body) == result
        original = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro,entry_role "
            "FROM cash_ledger_entries WHERE source_type='TRADE' AND source_id=? ORDER BY rowid",
            (str(trade),),
        ).all()
        assert [tuple(row) for row in original] == [
            (440000000, 7800000000000, 3432000000, "PRIMARY"),
            (-440000000, 7800000000000, -3432000000, "REVERSAL"),
        ]
        replacement = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_type='TRADE' AND source_id=? AND entry_role='PRIMARY'",
            (str(result.trade.id),),
        ).one()
        assert tuple(replacement) == (597000000, 7900000000000, 4716300000)
    assert database_snapshot(engine) == before


async def test_mutated_invalid_revision_input_fails_without_leaking_or_writing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = revision(book)
    body.quantity = "synthetic-private-invalid-quantity"
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="INVALID_TRADE_CORRECTION_SCOPE",
        ) as error:
            call(con, client, "REVISE", trade, pins, uuid4(), body)
        assert "synthetic-private-invalid-quantity" not in str(error.value)
    assert database_snapshot(engine) == before


async def test_unverified_writer_freezes_the_revision_intent_before_scope_validation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = revision(book)
    original_check = trade_history_review.require_correction_reviews

    def mutate_after_check(*args: Any, **kwargs: Any) -> tuple[trade_history_review.Review, ...]:
        result = original_check(*args, **kwargs)
        body.quantity = "12.00000000"
        return result

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(
            trade_history_review, "require_correction_reviews", side_effect=mutate_after_check
        ),
    ):
        result = call(con, client, "REVISE", trade, pins, uuid4(), body)
    assert result.trade.quantity == "11.00000000"


async def test_scheduled_revision_replay_does_not_depend_on_the_current_clock(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = TradeRevision.model_validate(
        payload(book, trade_date="2026-01-02", fx_rate_hkd=None, reason=REASON)
    )
    request = uuid4()
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(trade_service, "now", return_value=datetime(2026, 1, 1, 18, tzinfo=UTC)),
    ):
        result = call(con, client, "REVISE", trade, pins, request, body)
    assert result.trade.status == "DRAFT_SCHEDULED" and result.trade.booked_hkd is None
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert call(con, client, "REVISE", trade, pins, request, body) == result
    assert database_snapshot(engine) == before


async def test_oversell_rejection_rolls_back_authorization_and_all_financial_effects(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = active_auditless_buy(engine, book)
    await create(
        client, "trades", payload(book, side="SELL", trade_date="2026-01-03", quantity="6")
    )
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(FinanceError) as error:
            call(
                con,
                client,
                "REVISE",
                trade,
                pins,
                uuid4(),
                revision(book, quantity="5", day="2026-01-02"),
            )
        assert error.value.code == "INSUFFICIENT_SHARES"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_late_reader_failure_rolls_back_even_if_the_caller_catches_it(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(
            trade_correction_authorization,
            "get",
            side_effect=trade_correction_authorization.CorrectionAuthorizationError(
                "TRADE_CORRECTION_UNAVAILABLE"
            ),
        ),
        pytest.raises(trade_correction_authorization.CorrectionAuthorizationError),
    ):
        call(con, client, operation, trade, pins, uuid4(), revision(book, day="2026-01-02"))
    assert database_snapshot(engine) == before


async def test_existing_authorization_lookup_sql_failure_is_non_disclosing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    failure = IntegrityError("synthetic-private-query", {}, RuntimeError("synthetic failure"))
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(trade_unverified_correction, "_existing", side_effect=failure),
        pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ) as error,
    ):
        call(con, client, "REVISE", trade, pins, uuid4(), revision(book))
    assert "synthetic-private-query" not in str(error.value)
    assert database_snapshot(engine) == before


async def test_late_authorization_sql_failure_rolls_back_all_financial_effects(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    failure = IntegrityError("synthetic", {}, RuntimeError("synthetic failure"))
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(trade_unverified_correction, "audit", side_effect=failure),
        pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ),
    ):
        call(con, client, "REVISE", trade, pins, uuid4(), revision(book, day="2026-01-02"))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["unreviewed", "withdrawn", "stale", "missing", "extra", "late-audit"]
)
async def test_invalid_current_scope_cannot_write_anything(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    if problem != "unreviewed":
        decision(engine, owner, trade)
    if problem == "withdrawn":
        decision(engine, owner, trade, revision=1, action="WITHDRAW")
    if problem == "late-audit":
        with unit_of_work(engine, write=True) as con:
            row = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),))
                .mappings()
                .one()
            )
            audit(
                con,
                str(owner),
                "trade",
                str(trade),
                "CREATE",
                request_id=str(uuid4()),
                after=trade_service.as_trade(row).model_dump(mode="json"),
            )
    pin = trade_history_review.CorrectionReviewPin(trade, 1, 2 if problem == "stale" else 1)
    pins: tuple[trade_history_review.CorrectionReviewPin, ...] = (
        () if problem == "missing" else (pin,)
    )
    if problem == "extra":
        pins += (trade_history_review.CorrectionReviewPin(uuid4(), 1, 1),)
    expected = {
        "unreviewed": "TRADE_REVIEW_REQUIRED",
        "withdrawn": "TRADE_REVIEW_REQUIRED",
        "stale": "TRADE_REVIEW_VERSION_CONFLICT",
        "missing": "TRADE_REVIEW_SCOPE_CHANGED",
        "extra": "TRADE_REVIEW_SCOPE_CHANGED",
        "late-audit": "TRADE_REVIEW_CHANGED",
    }[problem]
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(trade_history_review.TradeReviewError, match=expected),
    ):
        call(con, client, "REVISE", trade, pins, uuid4(), revision(book))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_missing_settlement_rejects_the_whole_unverified_change(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    trade = active_auditless_buy(engine, book, with_settlement=False)
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(ApiError) as error:
            call(con, client, operation, trade, pins, uuid4(), revision(book, day="2026-01-02"))
        assert error.value.code == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_subsequent_correction_rechecks_ancestor_and_replays_the_original_result(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    initial_pin = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    initial_request, body = uuid4(), revision(book)
    with unit_of_work(engine, write=True) as con:
        first = call(con, client, "REVISE", trade, initial_pin, initial_request, body)
    second_pin = (trade_history_review.CorrectionReviewPin(trade, 2, 1),)
    with unit_of_work(engine, write=True) as con:
        second = call(con, client, operation, first.trade.id, second_pin, uuid4(), revision(book))
    before = database_snapshot(engine)
    assert len(before["trade_correction_authorizations"]) == 2
    assert second.authorization.pins[0].trade_id == trade
    assert second.authorization.pins[0].trade_version == 2
    with unit_of_work(engine, write=True) as con:
        replay = call(con, client, "REVISE", trade, initial_pin, initial_request, body)
    assert replay == first and database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_verified_current_origin_is_captured_when_only_an_ancestor_has_a_gap(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    first, current = audited_current_with_a_missing_ancestor(engine, book)
    owner, request = UUID(book["owner"]["id"]), uuid4()
    pins = (trade_history_review.CorrectionReviewPin(first, 2, 1),)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        result = call(con, client, operation, current, pins, request, revision(book))
    with unit_of_work(engine) as con:
        origin = trade_audit.get_origin(con, owner, current)
        assert origin is not None and origin.capture_mode == "BEFORE_RETIRE"
        assert origin.capture_request_id == request
        assert trade_audit.get_origin(con, owner, first) is None
        assert result.authorization.pins[0].trade_id == first
    after = database_snapshot(engine)
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after["trade_correction_authorizations"]) == 1


async def test_caught_failure_rolls_back_a_new_pre_retirement_origin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first, current = audited_current_with_a_missing_ancestor(engine, book)
    pins = (trade_history_review.CorrectionReviewPin(first, 2, 1),)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(
            trade_correction_authorization,
            "get",
            side_effect=trade_correction_authorization.CorrectionAuthorizationError(
                "TRADE_CORRECTION_UNAVAILABLE"
            ),
        ),
        pytest.raises(trade_correction_authorization.CorrectionAuthorizationError),
    ):
        call(con, client, "REVISE", current, pins, uuid4(), revision(book))
    assert database_snapshot(engine) == before


async def test_multiple_gap_pins_are_bound_and_order_independent_on_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, first)
    owner = UUID(book["owner"]["id"])
    first_review = decision(engine, owner, first, trade_version=2)
    current_review = decision(engine, owner, current)
    pins = (
        trade_history_review.CorrectionReviewPin(first, 2, 1),
        trade_history_review.CorrectionReviewPin(current, 1, 1),
    )
    request, body = uuid4(), revision(book)
    with unit_of_work(engine, write=True) as con:
        result = call(con, client, "REVISE", current, pins, request, body)
    assert [pin.review_id for pin in result.authorization.pins] == [
        first_review.id,
        current_review.id,
    ]
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        replay = call(con, client, "REVISE", current, tuple(reversed(pins)), request, body)
    assert replay == result and database_snapshot(engine) == before


@pytest.mark.parametrize("same_request", [False, True])
async def test_concurrent_corrections_publish_only_one_financial_change(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], same_request: bool
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    request = uuid4()

    def change(key: UUID) -> trade_unverified_correction.CorrectionResult | ApiError:
        try:
            with unit_of_work(engine, write=True) as con:
                return call(con, client, "REVISE", trade, pins, key, revision(book))
        except ApiError as error:
            return error

    keys = (request, request if same_request else uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(change, keys))
    successes = [
        item for item in results if isinstance(item, trade_unverified_correction.CorrectionResult)
    ]
    failures = [item for item in results if isinstance(item, ApiError)]
    if same_request:
        assert len(successes) == 2 and successes[0] == successes[1] and not failures
    else:
        assert len(successes) == 1 and len(failures) == 1 and failures[0].status == 412
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM trades WHERE root_id=?", (str(trade),)
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM trade_correction_authorizations WHERE root_id=?",
                (str(trade),),
            ).scalar_one()
            == 1
        )


async def test_replay_sql_failure_is_non_disclosing_and_zero_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = revision(book)
    with unit_of_work(engine, write=True) as con:
        original = call(con, client, "REVISE", trade, pins, uuid4(), body)
    before = database_snapshot(engine)
    failure = IntegrityError(
        "synthetic-private-replay-query", {}, RuntimeError("synthetic failure")
    )
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        with (
            patch.object(trade_unverified_correction, "_historical_result", side_effect=failure),
            pytest.raises(
                trade_unverified_correction.UnverifiedCorrectionError,
                match="TRADE_CORRECTION_UNAVAILABLE",
            ) as error,
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                original.authorization.id,
                trade,
                version=1,
                operation="REVISE",
                change=body,
                pins=pins,
            )
        assert "synthetic-private-replay-query" not in str(error.value)
    assert database_snapshot(engine) == before


async def test_authorization_replay_rechecks_intent_without_a_new_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    request, body = uuid4(), revision(book)
    with unit_of_work(engine, write=True) as con:
        original = call(con, client, "REVISE", trade, pins, request, body)
    decision(engine, owner, trade, trade_version=2, revision=1, action="WITHDRAW")
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        replay = trade_unverified_correction.replay(
            con,
            auth,
            original.authorization.id,
            trade,
            version=1,
            operation="REVISE",
            change=body,
            pins=pins,
        )
        assert replay == original
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_CONFLICT",
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                original.authorization.id,
                trade,
                version=1,
                operation="REVISE",
                change=revision(book, quantity="12"),
                pins=pins,
            )
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                uuid4(),
                trade,
                version=1,
                operation="REVISE",
                change=body,
                pins=pins,
            )
    assert database_snapshot(engine) == before


async def test_replay_does_not_accept_a_matching_intent_hash_with_a_different_audit_reason(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, scope = authorization_values(engine, book)
    trade = UUID(values["trade_id"])
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = revision(book)
    fingerprint = trade_unverified_correction._intent(trade, 1, "REVISE", body, pins).fingerprint
    values.update(id=str(uuid4()), intent_sha256=fingerprint, scope_json=canonical(scope))
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade_correction_authorization",
            values["id"],
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=values["request_id"],
            after=audit_payload(values, scope),
            reason="Correct this explicitly unverified synthetic trade",
        )
        recorded = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", values["id"]),
        ).one()
        values.update(audit_id=recorded.id, created_at=recorded.created_at)
        insert(con, "trade_correction_authorizations", values)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_CONFLICT",
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                UUID(values["id"]),
                trade,
                version=1,
                operation="REVISE",
                change=body,
                pins=pins,
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("mismatch", ["scope", "operation", "version"])
async def test_replay_compares_intent_to_retained_authorization_not_only_its_hash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], mismatch: str
) -> None:
    values, scope = authorization_values(engine, book, two_gaps=True)
    trade = UUID(values["trade_id"])
    pins = tuple(
        trade_history_review.CorrectionReviewPin(
            UUID(item["trade_id"]), item["trade_version"], int(item["review_revision"])
        )
        for item in scope
    )
    if mismatch == "scope":
        pins = pins[1:]
    operation: Literal["REVISE", "VOID"] = "VOID" if mismatch == "operation" else "REVISE"
    version = 2 if mismatch == "version" else 1
    body = TradeRevision.model_validate(
        payload(book, trade_date="2999-01-02", fx_rate_hkd=None, reason=SCHEMA_REASON)
    )
    change: TradeRevision | str = body if operation == "REVISE" else SCHEMA_REASON
    fingerprint = trade_unverified_correction._intent(
        trade, version, operation, change, pins
    ).fingerprint
    values.update(id=str(uuid4()), intent_sha256=fingerprint, scope_json=canonical(scope))
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade_correction_authorization",
            values["id"],
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=values["request_id"],
            after=audit_payload(values, scope),
            reason=SCHEMA_REASON,
        )
        recorded = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", values["id"]),
        ).one()
        values.update(audit_id=recorded.id, created_at=recorded.created_at)
        insert(con, "trade_correction_authorizations", values)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_CONFLICT",
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                UUID(values["id"]),
                trade,
                version=version,
                operation=operation,
                change=change,
                pins=pins,
            )
    assert database_snapshot(engine) == before


async def test_replay_rejects_a_matching_hash_when_the_revision_result_differs(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, scope = authorization_values(engine, book)
    trade = UUID(values["trade_id"])
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = TradeRevision.model_validate(
        payload(
            book, trade_date="2999-01-02", fx_rate_hkd=None, quantity="11", reason=SCHEMA_REASON
        )
    )
    fingerprint = trade_unverified_correction._intent(trade, 1, "REVISE", body, pins).fingerprint
    values.update(id=str(uuid4()), intent_sha256=fingerprint)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade_correction_authorization",
            values["id"],
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=values["request_id"],
            after=audit_payload(values, scope),
            reason=SCHEMA_REASON,
        )
        recorded = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", values["id"]),
        ).one()
        values.update(audit_id=recorded.id, created_at=recorded.created_at)
        insert(con, "trade_correction_authorizations", values)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_CONFLICT",
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                UUID(values["id"]),
                trade,
                version=1,
                operation="REVISE",
                change=body,
                pins=pins,
            )
    assert database_snapshot(engine) == before


async def test_replay_rejects_forged_derived_amount_even_when_input_fields_match(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, scope = authorization_values(
        engine, book, result_changes={"gross_micro": 0, "fx_source": "MANUAL"}
    )
    trade = UUID(values["trade_id"])
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    body = TradeRevision.model_validate(
        payload(book, trade_date="2999-01-02", fx_rate_hkd=None, reason=SCHEMA_REASON)
    )
    fingerprint = trade_unverified_correction._intent(trade, 1, "REVISE", body, pins).fingerprint
    values.update(id=str(uuid4()), intent_sha256=fingerprint)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade_correction_authorization",
            values["id"],
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=values["request_id"],
            after=audit_payload(values, scope),
            reason=SCHEMA_REASON,
        )
        recorded = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", values["id"]),
        ).one()
        values.update(audit_id=recorded.id, created_at=recorded.created_at)
        insert(con, "trade_correction_authorizations", values)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        assert (
            trade_correction_authorization.get(con, auth.owner.id, UUID(values["id"])) is not None
        )
        with pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_CONFLICT",
        ):
            trade_unverified_correction.replay(
                con,
                auth,
                UUID(values["id"]),
                trade,
                version=1,
                operation="REVISE",
                change=body,
                pins=pins,
            )
    assert database_snapshot(engine) == before


async def test_foreign_owner_and_stale_physical_version_cannot_mutate_the_trade(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    foreign, at = uuid4(), stamp()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic',?,?)",
            (str(foreign), at, at),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        current = read_auth(con, client.cookies.get(COOKIE))
        other = replace(current, owner=Owner(id=foreign, created_at=at, is_active=True))
        with pytest.raises(ApiError) as foreign_error:
            trade_unverified_correction.revise(
                con, other, trade, version=1, body=revision(book), pins=pins, request_id=uuid4()
            )
        assert foreign_error.value.status == 404
        with pytest.raises(ApiError) as stale_error:
            trade_unverified_correction.revise(
                con, current, trade, version=2, body=revision(book), pins=pins, request_id=uuid4()
            )
        assert stale_error.value.status == 412
    assert database_snapshot(engine) == before


async def test_verified_trade_does_not_accept_an_unverified_correction(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(
        client, "trades", payload(book, trade_date="2999-01-02", fx_rate_hkd=None)
    )
    trade = UUID(created["id"])
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(trade_history_review.TradeReviewError, match="TRADE_REVIEW_NOT_REQUIRED"),
    ):
        call(con, client, "REVISE", trade, (), uuid4(), revision(book))
    assert database_snapshot(engine) == before


async def test_writer_transaction_is_required(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(trade_history_review.TradeReviewError, match="TRADE_REVIEW_WRITE_REQUIRED"),
    ):
        call(con, client, "REVISE", trade, pins, uuid4(), revision(book))
    assert database_snapshot(engine) == before


async def test_same_request_with_changed_intent_is_rejected_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    pins = (trade_history_review.CorrectionReviewPin(trade, 1, 1),)
    request = uuid4()
    with unit_of_work(engine, write=True) as con:
        call(con, client, "REVISE", trade, pins, request, revision(book))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(
            trade_unverified_correction.UnverifiedCorrectionError,
            match="TRADE_CORRECTION_CONFLICT",
        ),
    ):
        call(con, client, "REVISE", trade, pins, request, revision(book, quantity="12"))
    assert database_snapshot(engine) == before
