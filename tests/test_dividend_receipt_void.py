"""Internal receipt voids preserve the original cash and reverse it exactly once."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as receipt_audit
import spm_api.dividend_receipt_void as voids
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import post_cash, rebase_cash_changes, reverse_cash
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OTHER, OWNER, REASON, add_posted_receipt
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent, submit
from tests.test_dividend_receipt_audit import scope as scope
from tests.test_dividend_receipt_writer import intent, save


def void_intent(engine: Engine, scope: tuple[Any, str, dict[str, Any]], record: Any) -> Any:
    event, account, _ = scope
    assert record.origin is not None
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT a.portfolio_id,a.broker_id,a.version,p.version,b.version,p.data_revision "
            "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
            "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account,),
        ).one()
    return voids.ReceiptVoidIntent.model_validate(
        {
            "receipt_id": record.receipt.id,
            "event_id": event.event_id,
            "observed_revision_id": event.revision_id,
            "observed_revision_no": event.revision_no,
            "entitlement_id": record.entitlement_id,
            "original_revision_id": record.original_revision_id,
            "account_id": UUID(account),
            "portfolio_id": UUID(row[0]),
            "broker_id": UUID(row[1]),
            "account_version": row[2],
            "portfolio_version": row[3],
            "broker_version": row[4],
            "expected_data_revision": row[5],
            "cash_entry_id": record.origin.cash_entry_id,
            "expected_receipt_version": record.receipt.version,
            "reason": REASON,
            "confirmation": "VOID_RECORDED_CASH",
        }
    )


def cancel(engine: Engine, value: Any, request: UUID) -> Any:
    with unit_of_work(engine, write=True) as con:
        return voids.void(con, OWNER, value, request_id=request)


def test_void_preserves_original_and_posts_one_exact_reversal(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    creation, create_request = intent(engine, scope), uuid4()
    original = save(engine, creation, create_request)
    value, request = void_intent(engine, scope, original), uuid4()
    before = database_snapshot(engine)
    result = cancel(engine, value, request)
    assert result.receipt.status == "VOID" and result.receipt.version == 2
    assert result.receipt.id == original.receipt.id
    assert result.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.origin == original.origin
    with unit_of_work(engine) as con:
        entries = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12,entry_role,external_flow "
            "FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' AND source_id=? "
            "ORDER BY entry_role",
            (str(value.receipt_id),),
        ).all()
        row = con.exec_driver_sql(
            "SELECT received_micro,received_hkd_micro,fx_rate_hkd_e12,tax_micro,fee_micro "
            "FROM dividend_receipts WHERE id=?",
            (str(value.receipt_id),),
        ).one()
        revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (str(value.portfolio_id),)
        ).scalar_one()
    assert [tuple(entry) for entry in entries] == [
        (6_000_000, 46_800_000, 7_800_000_000_000, "PRIMARY", 0),
        (-6_000_000, -46_800_000, 7_800_000_000_000, "REVERSAL", 0),
    ]
    assert tuple(row) == (6_000_000, 46_800_000, 7_800_000_000_000, None, 0)
    assert revision == value.expected_data_revision + 1
    after = database_snapshot(engine)
    assert after != before
    assert cancel(engine, value, request) == result
    assert save(engine, creation, create_request).receipt.status == "VOID"
    assert database_snapshot(engine) == after


def test_unknown_fx_remains_unknown_in_reversal(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    components = {
        "received_amount": "6",
        "currency": "USD",
        "tax": None,
        "fee": None,
        "fx_rate_hkd": None,
        "fx_source": None,
        "actual_hkd": None,
        "actual_hkd_reason": None,
    }
    original = save(engine, intent(engine, scope, components=components), uuid4())
    value = void_intent(engine, scope, original)
    result = cancel(engine, value, uuid4())
    assert result.receipt.received_hkd is None and result.receipt.fx_rate_hkd is None
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_type='DIVIDEND_RECEIPT' AND source_id=? ORDER BY entry_role",
            (str(value.receipt_id),),
        ).all()
    assert [tuple(row) for row in rows] == [(None, None), (None, None)]


def test_void_is_allowed_for_archived_account_without_changing_other_receipts(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    first = save(engine, intent(engine, scope), uuid4())
    second = save(engine, intent(engine, scope), uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?", (scope[1],)
        )
    value = void_intent(engine, scope, first)
    cancel(engine, value, uuid4())
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT id,status FROM dividend_receipts WHERE id IN (?,?) ORDER BY id",
            (str(first.receipt.id), str(second.receipt.id)),
        ).all()
    assert {row.id: row.status for row in rows} == {
        str(first.receipt.id): "VOID",
        str(second.receipt.id): "ACTIVE",
    }


def test_wrong_void_pins_and_conflicting_retry_are_zero_write(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)
    for field, replacement in (
        ("cash_entry_id", uuid4()),
        ("entitlement_id", uuid4()),
        ("original_revision_id", uuid4()),
        ("observed_revision_id", uuid4()),
        ("account_version", value.account_version + 1),
        ("expected_data_revision", value.expected_data_revision + 1),
    ):
        with pytest.raises(voids.ReceiptVoidError):
            cancel(engine, value.model_copy(update={field: replacement}), uuid4())
    assert database_snapshot(engine) == before
    cancel(engine, value, request)
    after = database_snapshot(engine)
    for changed, key in (
        (value.model_copy(update={"reason": "Different synthetic reason"}), request),
        (value, uuid4()),
    ):
        with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_CONFLICT$"):
            cancel(engine, changed, key)
    assert database_snapshot(engine) == after


def test_caught_late_void_failure_rolls_back_and_allows_retry(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "invalidate_portfolio", side_effect=ValueError("private")),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


def test_same_void_request_race_posts_only_one_reversal(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cancel, engine, value, request)
        second = pool.submit(cancel, engine, value, request)
        assert first.result(timeout=30) == second.result(timeout=30)
    with unit_of_work(engine) as con:
        count = con.exec_driver_sql(
            "SELECT count(*) FROM cash_ledger_entries "
            "WHERE reverses_entry_id=? AND entry_role='REVERSAL'",
            (str(value.cash_entry_id),),
        ).scalar_one()
    assert count == 1


def test_void_rebases_preopening_cash_assertion(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    _, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET opening_cash_micro=100000000,opening_cash_date='2026-01-10' "
            "WHERE id=?",
            (account,),
        )
        post_cash(
            con,
            account=account,
            day="2026-01-10",
            currency="USD",
            amount=100_000_000,
            fx=7_800_000_000_000,
            booked=780_000_000,
            kind="ADJUSTMENT",
            source_type="OPENING_BALANCE",
            source_id=account,
        )
    original = save(engine, intent(engine, scope), uuid4())
    value = void_intent(engine, scope, original)
    cancel(engine, value, uuid4())
    with unit_of_work(engine) as con:
        rebases = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_type='OPENING_REBASE' ORDER BY created_at,id"
        ).all()
        anchor = con.exec_driver_sql(
            "SELECT sum(amount_micro) FROM cash_ledger_entries WHERE account_id=? "
            "AND effective_date<='2026-01-10'",
            (account,),
        ).scalar_one()
    assert sorted(tuple(row) for row in rebases) == [
        (-6_000_000, -46_800_000),
        (6_000_000, 46_800_000),
    ]
    assert anchor == 100_000_000


def test_foreign_owner_cannot_void_receipt(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value = void_intent(engine, scope, record)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_NOT_FOUND$"),
    ):
        voids.void(con, OTHER, value, request_id=uuid4())
    assert database_snapshot(engine) == before


def test_legacy_receipt_without_origin_is_not_silently_adopted_for_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    original = save(engine, intent(engine, scope), uuid4())
    with unit_of_work(engine, write=True) as con:
        legacy = add_posted_receipt(con, scope[2], fx=7_800_000_000_000)
        cash_id = con.exec_driver_sql(
            "SELECT id FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' "
            "AND source_id=? AND entry_role='PRIMARY'",
            (legacy["id"],),
        ).scalar_one()
    value = void_intent(engine, scope, original).model_copy(
        update={"receipt_id": UUID(legacy["id"]), "cash_entry_id": UUID(cash_id)}
    )
    before = database_snapshot(engine)
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_UNVERIFIED$"):
        cancel(engine, value, uuid4())
    assert database_snapshot(engine) == before


def test_same_owner_account_move_invalidates_both_portfolios_without_relabeling_origin(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    old_portfolio = record.origin.portfolio_id
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con, "brokers", {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)}
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), scope[1]),
        )
        before = {
            str(row.id): int(row.data_revision)
            for row in con.exec_driver_sql(
                "SELECT id,data_revision FROM portfolios WHERE id IN (?,?)",
                (str(old_portfolio), str(new_portfolio)),
            )
        }
    value = void_intent(engine, scope, record)
    result = cancel(engine, value, uuid4())
    assert result.origin is not None and result.origin.portfolio_id == old_portfolio
    with unit_of_work(engine) as con:
        after = {
            str(row.id): int(row.data_revision)
            for row in con.exec_driver_sql(
                "SELECT id,data_revision FROM portfolios WHERE id IN (?,?)",
                (str(old_portfolio), str(new_portfolio)),
            )
        }
    assert after == {key: number + 1 for key, number in before.items()}


def test_missing_old_portfolio_invalidation_aborts_a_moved_account_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    old_portfolio = record.origin.portfolio_id
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con, "brokers", {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)}
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), scope[1]),
        )
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)

    def skipped_old(con: Connection, portfolio: str, *args: Any, **kwargs: Any) -> None:
        if portfolio != str(old_portfolio):
            invalidate_portfolio(con, portfolio, *args, **kwargs)

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "invalidate_portfolio", side_effect=skipped_old),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("column", ["version", "data_revision"])
def test_old_portfolio_changes_during_void_abort_before_invalidation(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], column: str
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    old_portfolio = record.origin.portfolio_id
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con, "brokers", {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)}
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), scope[1]),
        )
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        rebase_cash_changes(con, *args, **kwargs)
        con.exec_driver_sql(
            f"UPDATE portfolios SET {column}={column}+1 WHERE id=?", (str(old_portfolio),)
        )

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "rebase_cash_changes", side_effect=changed),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


def test_original_portfolio_revision_overflow_fails_closed(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    old_portfolio = record.origin.portfolio_id
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con, "brokers", {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)}
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), scope[1]),
        )
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?", (INT64_MAX, str(old_portfolio))
        )
    value = void_intent(engine, scope, record)
    before = database_snapshot(engine)
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"):
        cancel(engine, value, uuid4())
    assert database_snapshot(engine) == before


def test_last_advanceable_portfolio_revision_remains_exact_after_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?",
            (INT64_MAX - 1, str(record.origin.portfolio_id)),
        )
    value = void_intent(engine, scope, record)
    assert cancel(engine, value, uuid4()).receipt.status == "VOID"
    with unit_of_work(engine) as con:
        current = con.exec_driver_sql(
            "SELECT data_revision,typeof(data_revision) FROM portfolios WHERE id=?",
            (str(value.portfolio_id),),
        ).one()
    assert tuple(current) == (INT64_MAX, "integer")


def test_void_does_not_remove_the_event_financial_history_fence(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, _, _ = scope
    record = save(engine, intent(engine, scope), uuid4())
    cancel(engine, void_intent(engine, scope, record), uuid4())
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        submit(engine, next_intent(event, "VERIFY"))
    assert database_snapshot(engine) == before


def test_void_replay_is_historical_after_the_account_is_archived(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    result = cancel(engine, value, request)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?", (scope[1],)
        )
    before = database_snapshot(engine)
    assert cancel(engine, value, request) == result
    assert database_snapshot(engine) == before


def test_competing_void_requests_append_only_one_reversal(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value = void_intent(engine, scope, record)

    def attempt() -> str:
        try:
            cancel(engine, value, uuid4())
            return "saved"
        except voids.ReceiptVoidError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt), pool.submit(attempt)]
        results = sorted(future.result(timeout=30) for future in futures)
    assert results == ["DIVIDEND_RECEIPT_VOID_CONFLICT", "saved"]
    with unit_of_work(engine) as con:
        count = con.exec_driver_sql(
            "SELECT count(*) FROM cash_ledger_entries WHERE reverses_entry_id=?",
            (str(value.cash_entry_id),),
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("receipt_id", "private-invalid-id"),
        ("account_version", True),
        ("portfolio_version", 0),
        ("broker_version", 1.0),
        ("expected_data_revision", INT64_MAX),
        ("expected_receipt_version", 0),
        ("reason", "short"),
        ("confirmation", "OTHER"),
    ],
)
def test_invalid_forged_void_intents_never_write_or_disclose_values(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], field: str, replacement: object
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value = void_intent(engine, scope, record).model_copy(update={field: replacement})
    before = database_snapshot(engine)
    with pytest.raises(
        voids.ReceiptVoidError, match="^INVALID_DIVIDEND_RECEIPT_VOID_SCOPE$"
    ) as error:
        cancel(engine, value, uuid4())
    assert "private" not in str(error.value)
    assert database_snapshot(engine) == before


def test_void_intent_requires_explicit_confirmation_and_reason(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    payload = void_intent(engine, scope, record).model_dump()
    for name in ("reason", "confirmation", "cash_entry_id", "expected_receipt_version"):
        with pytest.raises(ValidationError):
            voids.ReceiptVoidIntent.model_validate(
                {key: value for key, value in payload.items() if key != name}
            )


def test_inactive_owner_nonwriter_and_missing_receipt_cannot_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value = void_intent(engine, scope, record)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(voids.ReceiptVoidError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        voids.void(con, OWNER, value, request_id=uuid4())
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_NOT_FOUND$"):
        cancel(engine, value.model_copy(update={"receipt_id": uuid4()}), uuid4())
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_OWNER_UNAVAILABLE$"):
        cancel(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure_point", ["reverse", "second_audit", "rebase"])
def test_void_failures_roll_back_all_changes_and_allow_same_request_retry(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], failure_point: str
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)

    def injected(*args: Any, **kwargs: Any) -> None:
        if args[4] == "DIVIDEND_RECEIPT_VOID_INTENT_RECORDED":
            raise ValueError("private injected failure")
        audit(*args, **kwargs)

    target, side_effect = {
        "reverse": ("reverse_cash", ValueError("private injected failure")),
        "second_audit": ("audit", injected),
        "rebase": ("rebase_cash_changes", ValueError("private injected failure")),
    }[failure_point]
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, target, side_effect=side_effect),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


def test_same_transaction_account_change_aborts_entire_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> str:
        result = reverse_cash(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(value.account_id),)
        )
        return result

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "reverse_cash", side_effect=changed),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


def test_account_change_during_invalidation_aborts_entire_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)

    def changed(con: Connection, *args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(con, *args, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(value.account_id),)
        )

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "invalidate_portfolio", side_effect=changed),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("problem", "error"),
    [
        ("missing", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("duplicate", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("bad-id", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("owner", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("action", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("time", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("before", "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"),
        ("request", "DIVIDEND_RECEIPT_VOID_CONFLICT"),
        ("reason", "DIVIDEND_RECEIPT_VOID_CONFLICT"),
        ("after", "DIVIDEND_RECEIPT_VOID_CONFLICT"),
    ],
)
def test_void_replay_rejects_corrupt_intent_audits(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], problem: str, error: str
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    cancel(engine, value, request)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "SELECT * FROM audit_events WHERE entity_type='dividend_receipt_void_intent'" not in str(
            statement
        ):
            return result
        rows = [dict(row) for row in result.mappings().all()]
        assert len(rows) == 1
        if problem == "missing":
            rows = []
        elif problem == "duplicate":
            rows.append(dict(rows[0]))
        else:
            field, changed = {
                "bad-id": ("id", "private-invalid"),
                "owner": ("owner_id", str(OTHER)),
                "action": ("action", "OTHER"),
                "time": ("created_at", "private-invalid"),
                "before": ("before_sha256", "0" * 64),
                "request": ("request_id", str(uuid4())),
                "reason": ("reason", "Synthetic different reason"),
                "after": ("after_sha256", "0" * 64),
            }[problem]
            rows[0][field] = changed
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    before = database_snapshot(engine)
    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(voids.ReceiptVoidError, match=f"^{error}$") as raised,
    ):
        cancel(engine, value, request)
    assert "private" not in str(raised.value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bad_time", ["private-invalid", datetime(2026, 1, 1)])
def test_void_with_invalid_clock_rolls_back_reversal_and_audit(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], bad_time: object
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "now", return_value=bad_time),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


@pytest.mark.parametrize("problem", ["none", "missing", "not-void"])
def test_postvoid_reader_mismatch_rolls_back_every_financial_fact(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], problem: str
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    original = receipt_audit.history
    calls = 0

    def injected(con: Connection, owner: UUID, event_id: UUID, account_id: UUID) -> Any:
        nonlocal calls
        calls += 1
        result = original(con, owner, event_id, account_id)
        if calls == 1:
            return result
        assert result is not None and len(result) == 1
        if problem == "none":
            return None
        if problem == "missing":
            return ()
        return (replace(result[0], receipt=replace(result[0].receipt, status="ACTIVE")),)

    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(receipt_audit, "history", side_effect=injected),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


def test_existing_foreign_void_intent_and_stale_receipt_version_are_not_overwritten(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    before = database_snapshot(engine)
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_VERSION_CHANGED$"):
        cancel(engine, value.model_copy(update={"expected_receipt_version": 2}), request)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        audit(con, str(OWNER), "dividend_receipt_void_intent", str(value.receipt_id), "UNRELATED")
    before = database_snapshot(engine)
    with pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_CONFLICT$"):
        cancel(engine, value, request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["event_id", "account_id", "portfolio_id"])
def test_void_scope_rejects_missing_event_account_or_portfolio(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], field: str
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    value = void_intent(engine, scope, record).model_copy(update={field: uuid4()})
    error = (
        "DIVIDEND_RECEIPT_VOID_UNAVAILABLE"
        if field == "portfolio_id"
        else "DIVIDEND_RECEIPT_NOT_FOUND"
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(voids.ReceiptVoidError, match=f"^{error}$"),
    ):
        voids._scope(con, OWNER, value, record.origin)
    assert database_snapshot(engine) == before


def test_void_rejects_noncanonical_forged_reason_and_invalid_request_id(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value = void_intent(engine, scope, record)
    before = database_snapshot(engine)
    with pytest.raises(voids.ReceiptVoidError, match="^INVALID_DIVIDEND_RECEIPT_VOID_SCOPE$"):
        cancel(engine, value.model_copy(update={"reason": f" {REASON} "}), uuid4())
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(voids.ReceiptVoidError, match="^INVALID_DIVIDEND_RECEIPT_VOID_SCOPE$"),
    ):
        voids.void(con, OWNER, value, request_id="private")  # type: ignore[arg-type]
    assert database_snapshot(engine) == before


def test_zero_row_version_cas_rolls_back_the_reversal_and_audit(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "UPDATE dividend_receipts SET status='VOID'" in str(statement):
            result = MagicMock()
            result.rowcount = 0
            return result
        return execute(con, statement, *args, **kwargs)

    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(Connection, "execute", injected),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_VERSION_CHANGED$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


def test_missing_origin_cannot_be_returned_as_an_audited_void_replay(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    value, request = void_intent(engine, scope, record), uuid4()
    result = cancel(engine, value, request)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_UNAVAILABLE$"),
    ):
        voids._verify_void(con, OWNER, value, request, replace(result, origin=None))
    assert database_snapshot(engine) == before
