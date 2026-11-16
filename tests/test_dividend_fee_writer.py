"""Internal fee decisions require current source, rejection and exact account pins."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as writer
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_candidate_service import basis as basis
from tests.test_dividend_candidate_service import decision as candidate_decision
from tests.test_dividend_candidate_service import save as save_candidate
from tests.test_dividend_event_migration import (
    OTHER,
    OWNER,
    REASON,
    add_posted_receipt,
    entitlement,
)
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT, candidate_intent, next_intent, submit
from tests.test_dividend_fee_history import history
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_rejection_writer import save as reject
from tests.test_dividend_tax_service import account_scope
from tests.test_dividend_tax_service import book as book


def intent(book: dict[str, Any], **changes: Any) -> writer.FeeIntent:
    event = book["event"]
    return writer.FeeIntent.model_validate(
        {
            "id": uuid4(),
            "event_id": event.event_id,
            "revision_id": event.revision_id,
            "revision_no": event.revision_no,
            "account_id": book["account_id"],
            "portfolio_id": book["portfolio_id"],
            "broker_id": book["broker_id"],
            "account_version": book["account_version"],
            "broker_version": book["broker_version"],
            "expected_setting_no": 0,
            "expected_head_id": None,
            "effective_from": "2026-01-01",
            "fee_status": "ESTIMATED",
            "fee": "0.000000",
            "fee_currency": "HKD",
            "reason": REASON,
            **changes,
        }
    )


def save(
    engine: Engine,
    value: writer.FeeIntent,
    *,
    request: UUID | None = None,
    owner: UUID = OWNER,
) -> fees.FeeSetting:
    with unit_of_work(engine, write=True) as con:
        return writer.save(con, owner, value, request_id=request or uuid4())


def current(
    engine: Engine,
    book: dict[str, Any],
    *,
    day: date = date(2026, 1, 8),
    head_no: int = 0,
    head_id: UUID | None = None,
) -> fees.FeeSelection:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        return writer.require_current(
            con,
            OWNER,
            event.event_id,
            event.revision_id,
            event.revision_no,
            book["account_id"],
            effective_on=day,
            expected_setting_no=head_no,
            expected_head_id=head_id,
            expected_portfolio_id=book["portfolio_id"],
            expected_broker_id=book["broker_id"],
            expected_account_version=book["account_version"],
            expected_broker_version=book["broker_version"],
        )


def test_explicit_zero_unknown_and_missing_remain_distinct_with_atomic_invalidation(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    assert current(engine, book).status == "MISSING"
    assert database_snapshot(engine) == before
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        zero = save(engine, intent(book))
    after = database_snapshot(engine)
    allowed = {"dividend_fee_settings", "audit_events", "portfolios", "projection_invalidations"}
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert all(len(after[name]) == len(before[name]) + 1 for name in allowed - {"portfolios"})
    assert zero.fee == "0.000000" and zero.fee_currency == "HKD"
    assert current(engine, book, head_no=1, head_id=zero.id).selected == zero
    with unit_of_work(engine) as con:
        marker = con.exec_driver_sql(
            "SELECT portfolio_id,source_id,reason,from_date FROM projection_invalidations "
            "WHERE source_id=?",
            (str(zero.id),),
        ).one()
    assert tuple(marker) == (
        str(book["portfolio_id"]),
        str(zero.id),
        "DIVIDEND_FEE_SETTING_CHANGED",
        "2026-01-01",
    )
    unknown = save(
        engine,
        intent(
            book,
            expected_setting_no=1,
            expected_head_id=zero.id,
            fee_status="UNKNOWN",
            fee=None,
            fee_currency="USD",
        ),
    )
    assert history(engine, book) == (zero, unknown)
    selection = current(engine, book, head_no=2, head_id=unknown.id)
    assert selection.status == "UNKNOWN" and selection.selected == unknown
    assert selection.scope == "UNVERIFIED_FEE_SELECTION_ONLY"
    assert REASON not in repr(unknown) and str(book["account_id"]) not in repr(unknown)


def test_replay_is_zero_write_after_rejection_and_explicit_successor(
    engine: Engine, book: dict[str, Any]
) -> None:
    request, value = uuid4(), intent(book, fee="2.000001", fee_currency="USD")
    first = save(engine, value, request=request)
    rejected = reject(engine, rejection_intent(book["event"]))
    before = database_snapshot(engine)
    assert save(engine, value, request=request) == first
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_REVISION_REJECTED$"):
        current(engine, book, head_no=1, head_id=first.id)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_REVISION_REJECTED$"):
        save(engine, intent(book, expected_setting_no=1, expected_head_id=first.id))
    assert database_snapshot(engine) == before
    successor = next_intent(book["event"], "VERIFY", resolve_rejection_id=rejected.id)
    submit(engine, successor, at=datetime.now(UTC))
    before = database_snapshot(engine)
    assert save(engine, value, request=request) == first
    assert history(engine, book) == (first,)
    assert current(engine, {**book, "event": successor}).status == "MISSING"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("fee", "0.000001"),
        ("fee_currency", "USD"),
        ("effective_from", "2026-01-02"),
        ("reason", "A different synthetic reason."),
        ("portfolio_id", uuid4()),
        ("broker_id", uuid4()),
        ("account_version", 2),
        ("broker_version", 2),
    ],
)
def test_same_identity_cannot_replay_a_changed_intent(
    engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    request, original = uuid4(), intent(book)
    saved = save(engine, original, request=request)
    before = database_snapshot(engine)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_CONFLICT$"):
        save(engine, original.model_copy(update={field: value}), request=request)
    assert save(engine, original, request=request) == saved
    assert database_snapshot(engine) == before


def test_head_and_account_scope_are_rechecked_after_same_transaction_changes(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book, fee="1.000000"))
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        args = (con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
        pins: dict[str, Any] = {
            "effective_on": date(2026, 1, 8),
            "expected_setting_no": 1,
            "expected_head_id": first.id,
            "expected_portfolio_id": book["portfolio_id"],
            "expected_broker_id": book["broker_id"],
            "expected_account_version": book["account_version"],
            "expected_broker_version": book["broker_version"],
        }
        assert writer.require_current(*args, **pins).selected == first
        second = writer.save(
            con,
            OWNER,
            intent(
                book,
                expected_setting_no=1,
                expected_head_id=first.id,
                fee_status="UNKNOWN",
                fee=None,
                fee_currency=None,
            ),
            request_id=uuid4(),
        )
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SETTING_CHANGED$"):
            writer.require_current(*args, **pins)
        con.exec_driver_sql(
            "UPDATE brokers SET version=version+1 WHERE id=?", (str(book["broker_id"]),)
        )
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SCOPE_CHANGED$"):
            writer.require_current(
                *args,
                effective_on=date(2026, 1, 8),
                expected_setting_no=2,
                expected_head_id=second.id,
                expected_portfolio_id=book["portfolio_id"],
                expected_broker_id=book["broker_id"],
                expected_account_version=book["account_version"],
                expected_broker_version=book["broker_version"],
            )
    assert history(engine, book) == (first, second)


def test_account_move_requires_a_new_explicit_assertion(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book))
    destination = account_scope(engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (
                str(destination["portfolio_id"]),
                str(destination["broker_id"]),
                str(book["account_id"]),
            ),
        )
    before = database_snapshot(engine)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SCOPE_CHANGED$"):
        current(engine, book, head_no=1, head_id=first.id)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SCOPE_CHANGED$"):
        save(engine, intent(book, expected_setting_no=1, expected_head_id=first.id))
    assert database_snapshot(engine) == before
    moved = {
        **book,
        "portfolio_id": destination["portfolio_id"],
        "broker_id": destination["broker_id"],
        "account_version": book["account_version"] + 1,
        "broker_version": destination["broker_version"],
    }
    second = save(
        engine,
        intent(
            moved,
            expected_setting_no=1,
            expected_head_id=first.id,
            fee="0.500000",
            fee_currency="USD",
        ),
    )
    assert history(engine, moved) == (first, second)
    assert current(engine, moved, head_no=2, head_id=second.id).selected == second


def test_candidate_dismissal_fences_new_use_but_not_historical_replay(
    engine: Engine, basis: Any
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    event = candidate_intent(candidate)
    submit(engine, event)
    book = {**account_scope(engine), "event": event}
    value, request = intent(book), uuid4()
    saved = save(engine, value, request=request)
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert save(engine, value, request=request) == saved
    with pytest.raises(fees.FeeSettingError, match="^CANDIDATE_VERSION_CONFLICT$"):
        current(engine, book, head_no=1, head_id=saved.id)
    with pytest.raises(fees.FeeSettingError, match="^CANDIDATE_VERSION_CONFLICT$"):
        save(engine, intent(book, expected_setting_no=1, expected_head_id=saved.id))
    assert database_snapshot(engine) == before


def test_caught_invalidation_failure_rolls_back_fee_and_audit_inside_outer_writer(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)

    def fail_after_invalidation(con: Connection, *args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(con, *args, **kwargs)
        raise ValueError("synthetic private failure")

    with unit_of_work(engine, write=True) as con:
        with (
            patch.object(writer, "invalidate_portfolio", side_effect=fail_after_invalidation),
            pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"),
        ):
            writer.save(con, OWNER, intent(book), request_id=uuid4())
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_fee_settings").scalar_one() == 0
    assert database_snapshot(engine) == before


def test_old_receipt_cash_and_locked_fx_are_not_modified(
    engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(event.revision_id), "currency": "USD"}, str(book["account_id"])
        )
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    save(engine, intent(book, fee="0.250000", fee_currency="USD"))
    after = database_snapshot(engine)
    for name in ("dividend_entitlements", "dividend_receipts", "cash_ledger_entries"):
        assert before[name] == after[name]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
            ).scalar_one()
            == 7_800_000_000_000
        )


def test_competing_writers_and_identical_replay_are_serialized(
    engine: Engine, book: dict[str, Any]
) -> None:
    value, request = intent(book), uuid4()
    before = database_snapshot(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: save(engine, value, request=request), range(2)))
    assert result[0] == result[1]
    after = database_snapshot(engine)
    for table in ("dividend_fee_settings", "audit_events", "projection_invalidations"):
        assert len(after[table]) == len(before[table]) + 1
    contenders = [
        intent(book, expected_setting_no=1, expected_head_id=result[0].id, fee="0.1"),
        intent(book, expected_setting_no=1, expected_head_id=result[0].id, fee="0.2"),
    ]

    def attempt(item: writer.FeeIntent) -> str:
        try:
            save(engine, item)
            return "saved"
        except fees.FeeSettingError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, contenders))
    assert sorted(outcomes) == ["DIVIDEND_FEE_VERSION_CONFLICT", "saved"]
    assert len(history(engine, book)) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"fee": 0.0},
        {"fee": "0.0000001"},
        {"fee": "-0.000001"},
        {"fee_status": "ESTIMATED", "fee": None},
        {"fee_status": "ESTIMATED", "fee_currency": None},
        {"fee_status": "UNKNOWN", "fee": "0"},
        {"fee_status": "UNKNOWN", "fee": None, "fee_currency": "CNY"},
        {"expected_setting_no": 1, "expected_head_id": None},
        {"expected_setting_no": 0, "expected_head_id": uuid4()},
        {"account_version": True},
        {"effective_from": "2026-02-30"},
    ],
)
def test_invalid_fee_intents_are_not_rounded_or_filled(
    book: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        intent(book, **changes)


def test_writer_requires_active_owner_a_real_writer_and_exact_intent(
    engine: Engine, book: dict[str, Any]
) -> None:
    value = intent(book)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(fees.FeeSettingError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        writer.save(con, OWNER, value, request_id=uuid4())
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_OWNER_UNAVAILABLE$"):
        save(engine, value, owner=uuid4())
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_NOT_FOUND$"):
        save(engine, value, owner=OTHER)
    with pytest.raises(fees.FeeSettingError, match="^INVALID_DIVIDEND_FEE_SCOPE$"):
        save(engine, value.model_copy(update={"fee": 0.0}))
    assert database_snapshot(engine) == before
