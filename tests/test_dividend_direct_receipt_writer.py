"""Direct recorded cash must be atomic without manufacturing an entitlement."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from threading import Barrier
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_direct_receipt_writer as writer
import spm_api.dividend_rejection_service as rejection
from spm_api.audit import audit, now
from spm_api.db import unit_of_work
from spm_api.dividend_receipt_terms import ReceiptComponents
from spm_api.ledger_service import post_cash
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.test_dividend_combined_receipt_history import add_legacy
from tests.test_dividend_direct_receipt_history import void_receipt
from tests.test_dividend_direct_receipt_migration import scenario as scenario
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent, submit
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_rejection_writer import save as reject


def intent(engine: Engine, scenario: tuple[Any, str], **changes: Any) -> Any:
    event, account = scenario
    with unit_of_work(engine) as con:
        scope = con.exec_driver_sql(
            "SELECT a.portfolio_id,a.broker_id,a.version,p.version,b.version,p.data_revision "
            "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
            "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account,),
        ).one()
    return writer.DirectReceiptIntent.model_validate(
        {
            "id": uuid4(),
            "event_id": event.event_id,
            "revision_id": event.revision_id,
            "revision_no": event.revision_no,
            "account_id": UUID(account),
            "portfolio_id": UUID(scope[0]),
            "broker_id": UUID(scope[1]),
            "account_version": scope[2],
            "portfolio_version": scope[3],
            "broker_version": scope[4],
            "expected_data_revision": scope[5],
            "received_date": "2026-01-09",
            "components": {
                "received_amount": "6",
                "currency": "USD",
                "tax": None,
                "fee": "0",
                "fx_rate_hkd": "7.8",
                "fx_source": "Synthetic private rate source",
                "actual_hkd": None,
                "actual_hkd_reason": None,
            },
            "note": "Synthetic private direct receipt note",
            "reason": REASON,
            "confirmation": "RECORDED_CASH_ONLY",
            "confirm_revision_fence": True,
            **changes,
        }
    )


def save(engine: Engine, value: Any, request: UUID, *, owner: UUID = OWNER) -> Any:
    with unit_of_work(engine, write=True) as con:
        return writer.save(con, owner, value, request_id=request)


def test_direct_record_has_exact_cash_and_zero_write_replay_without_entitlement(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, request = intent(engine, scenario), uuid4()
    result = save(engine, value, request)
    assert result.receipt.id == value.id and result.receipt.status == "ACTIVE"
    assert result.revision_id == value.revision_id and result.revision_no == value.revision_no
    assert result.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert result.origin.cash_note == value.note and result.origin.creation_reason == value.reason
    assert not hasattr(result, "entitlement_id")
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_entitlements").scalar_one() == 0
        assert tuple(
            con.exec_driver_sql(
                "SELECT received_micro,tax_micro,fee_micro,fx_rate_hkd_e12,received_hkd_micro "
                "FROM dividend_direct_receipts WHERE id=?",
                (str(value.id),),
            ).one()
        ) == (6_000_000, None, 0, 7_800_000_000_000, 46_800_000)
        assert tuple(
            con.exec_driver_sql(
                "SELECT amount_micro,booked_hkd_micro,external_flow FROM cash_ledger_entries "
                "WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=?",
                (str(value.id),),
            ).one()
        ) == (6_000_000, 46_800_000, 0)
        assert (
            con.exec_driver_sql(
                "SELECT data_revision FROM portfolios WHERE id=?", (str(value.portfolio_id),)
            ).scalar_one()
            == value.expected_data_revision + 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations WHERE source_id=?", (str(value.id),)
            ).scalar_one()
            == 1
        )
    after = database_snapshot(engine)
    assert save(engine, value, request) == result
    assert database_snapshot(engine) == after


def test_unknown_fx_is_retained_and_does_not_invent_hkd(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    components = {
        "received_amount": "6",
        "currency": "USD",
        "tax": "0",
        "fee": None,
        "fx_rate_hkd": None,
        "fx_source": None,
        "actual_hkd": None,
        "actual_hkd_reason": None,
    }
    result = save(engine, intent(engine, scenario, components=components), uuid4())
    assert result.receipt.fx_rate_hkd is None and result.receipt.received_hkd is None
    assert result.receipt.tax == 0 and result.receipt.fee is None
    assert result.origin.fx_source is None


def test_request_conflicts_and_stale_or_foreign_scope_do_not_write(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, request = intent(engine, scenario), uuid4()
    save(engine, value, request)
    changed = value.model_copy(update={"note": "Different private note"})
    stale = intent(engine, scenario).model_copy(
        update={"account_version": value.account_version + 1}
    )
    before = database_snapshot(engine)
    for candidate, key, owner, error in (
        (changed, request, OWNER, "DIVIDEND_DIRECT_RECEIPT_CONFLICT"),
        (value, uuid4(), OWNER, "DIVIDEND_DIRECT_RECEIPT_CONFLICT"),
        (stale, uuid4(), OWNER, "DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED"),
        (value, request, OTHER, "DIVIDEND_DIRECT_RECEIPT_NOT_FOUND"),
    ):
        with pytest.raises(writer.DirectReceiptWriteError, match=f"^{error}$"):
            save(engine, candidate, key, owner=owner)
    assert database_snapshot(engine) == before


def test_caught_late_failure_rolls_back_all_facts_and_allows_same_request_retry(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, request = intent(engine, scenario), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "invalidate_portfolio", side_effect=ValueError("synthetic-private")),
        pytest.raises(
            writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
        ),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


def test_scope_is_rechecked_after_invalidation_before_releasing_savepoint(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, request = intent(engine, scenario), uuid4()
    before = database_snapshot(engine)

    def move_version(*args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(*args, **kwargs)
        args[0].exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(value.account_id),)
        )

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "invalidate_portfolio", side_effect=move_version),
        pytest.raises(
            writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED$"
        ),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


def test_preopening_direct_cash_preserves_the_declared_opening_balance(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    _, account = scenario
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
    value = intent(engine, scenario)
    save(engine, value, uuid4())
    with unit_of_work(engine) as con:
        assert tuple(
            con.exec_driver_sql(
                "SELECT amount_micro,booked_hkd_micro,effective_date FROM cash_ledger_entries "
                "WHERE source_type='OPENING_REBASE' AND source_id=?",
                (str(value.id),),
            ).one()
        ) == (-6_000_000, -46_800_000, "2026-01-10")
        assert (
            con.exec_driver_sql(
                "SELECT sum(amount_micro) FROM cash_ledger_entries WHERE account_id=? "
                "AND effective_date<='2026-01-10'",
                (account,),
            ).scalar_one()
            == 100_000_000
        )


def test_competing_identical_direct_requests_post_once(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, request = intent(engine, scenario), uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, engine, value, request)
        second = pool.submit(save, engine, value, request)
        assert first.result(timeout=30) == second.result(timeout=30)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_direct_receipts").scalar_one() == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries "
                "WHERE source_type='DIVIDEND_DIRECT_RECEIPT'"
            ).scalar_one()
            == 1
        )


def test_fence_acknowledgment_is_explicit_and_future_cash_is_not_newly_posted(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = intent(engine, scenario)
    with pytest.raises(ValidationError):
        writer.DirectReceiptIntent.model_validate(
            {**value.model_dump(), "confirm_revision_fence": False}
        )
    future = value.model_copy(update={"received_date": "2999-01-01"})
    before = database_snapshot(engine)
    with pytest.raises(
        writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_DATE_INVALID$"
    ):
        save(engine, future, uuid4())
    assert database_snapshot(engine) == before


def test_historical_replay_survives_clock_rollback_without_posting_again(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, request = intent(engine, scenario), uuid4()
    saved = save(engine, value, request)
    before = database_snapshot(engine)

    class EarlierDate(date):
        @classmethod
        def today(cls) -> "EarlierDate":
            return cls(2025, 1, 1)

    with patch("spm_api.cash_models.date", EarlierDate):
        assert save(engine, value, request) == saved
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("currency,amount", [("HKD", "0"), ("HKD", "-6"), ("USD", "-6")])
def test_signed_cash_and_hkd_identity_are_exact(
    engine: Engine, scenario: tuple[Any, str], currency: str, amount: str
) -> None:
    components = {
        "received_amount": amount,
        "currency": currency,
        "tax": None,
        "fee": None,
        "fx_rate_hkd": None,
        "fx_source": None,
        "actual_hkd": None,
        "actual_hkd_reason": None,
    }
    result = save(engine, intent(engine, scenario, components=components), uuid4())
    if currency == "HKD":
        assert result.receipt.received_hkd == result.receipt.received
        assert result.receipt.fx_rate_hkd == 1 and result.origin.fx_source == "IDENTITY"
    else:
        assert result.receipt.received_hkd is None
    assert str(result.receipt.received) == amount + ".000000"


def test_explicit_actual_hkd_retains_reason_and_exact_value(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = intent(engine, scenario)
    components = ReceiptComponents.model_validate(
        {**value.components.model_dump(), "actual_hkd": "46.123456", "actual_hkd_reason": REASON}
    )
    result = save(engine, value.model_copy(update={"components": components}), uuid4())
    assert str(result.receipt.received_hkd) == "46.123456"
    assert result.origin.actual_hkd_reason == REASON


def test_direct_creation_preserves_existing_legacy_receipts(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_legacy(engine, scenario)
    before = database_snapshot(engine)
    value = intent(engine, scenario)
    save(engine, value, uuid4())
    after = database_snapshot(engine)
    for table in ("dividend_receipts", "dividend_entitlements", "dividend_receipt_origins"):
        assert after[table] == before[table]
    assert (
        after["cash_ledger_entries"][: len(before["cash_ledger_entries"])]
        == before["cash_ledger_entries"]
    )
    with unit_of_work(engine) as con:
        history = combined.history(con, OWNER, value.event_id, value.account_id)
    assert history is not None and len(history) == 2


@pytest.mark.parametrize("state", ["archive", "void"])
def test_historical_replay_does_not_repost_after_lifecycle_changes(
    engine: Engine, scenario: tuple[Any, str], state: str
) -> None:
    value, request = intent(engine, scenario), uuid4()
    save(engine, value, request)
    if state == "archive":
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
                (str(value.account_id),),
            )
    else:
        with unit_of_work(engine) as con:
            receipt = (
                con.exec_driver_sql(
                    "SELECT * FROM dividend_direct_receipts WHERE id=?", (str(value.id),)
                )
                .mappings()
                .one()
            )
            origin = (
                con.exec_driver_sql(
                    "SELECT * FROM dividend_direct_receipt_origins WHERE receipt_id=?",
                    (str(value.id),),
                )
                .mappings()
                .one()
            )
            cash = (
                con.exec_driver_sql(
                    "SELECT * FROM cash_ledger_entries WHERE id=?", (origin["cash_entry_id"],)
                )
                .mappings()
                .one()
            )
            payload = direct._creation_payload(receipt, origin, cash, value.reason)
        void_receipt(engine, {"id": str(value.id), "origin": dict(origin), "payload": payload})
    before = database_snapshot(engine)
    result = save(engine, value, request)
    assert result.receipt.status == ("VOID" if state == "void" else "ACTIVE")
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["rejected", "withdrawn", "archived", "stale_event"])
def test_new_cash_rejects_unusable_current_event_or_account_scope(
    engine: Engine, scenario: tuple[Any, str], state: str
) -> None:
    event, account = scenario
    value = intent(engine, scenario)
    if state == "rejected":
        reject(engine, rejection_intent(event))
        code = "DIVIDEND_REVISION_REJECTED"
    elif state in ("withdrawn", "stale_event"):
        next_event = next_intent(event, "WITHDRAW" if state == "withdrawn" else "VERIFY")
        submit(engine, next_event, at=now())
        if state == "withdrawn":
            value = intent(engine, (next_event, account))
        code = (
            "DIVIDEND_DIRECT_RECEIPT_EVENT_WITHDRAWN"
            if state == "withdrawn"
            else "DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED"
        )
    else:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("UPDATE accounts SET status='ARCHIVED' WHERE id=?", (account,))
        code = "DIVIDEND_DIRECT_RECEIPT_ACCOUNT_ARCHIVED"
    before = database_snapshot(engine)
    with pytest.raises(writer.DirectReceiptWriteError, match=f"^{code}$"):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["deferred", "owner", "request", "intent", "components", "forged"])
def test_invalid_internal_calls_fail_closed(
    engine: Engine, scenario: tuple[Any, str], kind: str
) -> None:
    value, request, owner = intent(engine, scenario), uuid4(), OWNER
    if kind == "owner":
        owner = "invalid"  # type: ignore[assignment]
    elif kind == "request":
        request = "invalid"  # type: ignore[assignment]
    elif kind == "intent":
        value = object()
    elif kind == "components":
        value = value.model_copy(update={"components": {}})
    elif kind == "forged":
        value = value.model_copy(update={"confirm_revision_fence": 1})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=kind != "deferred") as con,
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        writer.save(con, owner, value, request_id=request)
    assert database_snapshot(engine) == before


def test_duplicate_request_cannot_create_a_second_direct_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    request = uuid4()
    save(engine, intent(engine, scenario), request)
    before = database_snapshot(engine)
    with pytest.raises(writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_CONFLICT$"):
        save(engine, intent(engine, scenario), request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("fault", ["creation", "intent", "origin", "cash", "invalidation"])
def test_ignored_writes_never_commit_partial_direct_cash(
    engine: Engine, scenario: tuple[Any, str], fault: str
) -> None:
    value, request = intent(engine, scenario), uuid4()
    table, condition = {
        "creation": ("audit_events", "NEW.action='DIVIDEND_DIRECT_RECEIPT_RECORDED'"),
        "intent": ("audit_events", "NEW.action='DIVIDEND_DIRECT_RECEIPT_INTENT_RECORDED'"),
        "origin": ("dividend_direct_receipt_origins", "1"),
        "cash": ("cash_ledger_entries", "NEW.source_type='DIVIDEND_DIRECT_RECEIPT'"),
        "invalidation": ("projection_invalidations", "1"),
    }[fault]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"CREATE TRIGGER synthetic_ignore_direct BEFORE INSERT ON {table} "
            f"WHEN {condition} BEGIN SELECT RAISE(IGNORE); END"
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["account", "portfolio", "broker", "data"])
def test_final_audit_verification_cannot_hide_a_late_scope_change(
    engine: Engine, scenario: tuple[Any, str], field: str
) -> None:
    value, request = intent(engine, scenario), uuid4()
    original = writer._verify_intent
    table, identifier, column = {
        "account": ("accounts", value.account_id, "version"),
        "portfolio": ("portfolios", value.portfolio_id, "version"),
        "broker": ("brokers", value.broker_id, "version"),
        "data": ("portfolios", value.portfolio_id, "data_revision"),
    }[field]

    def change(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        args[0].exec_driver_sql(
            f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (str(identifier),)
        )

    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "_verify_intent", side_effect=change),
        pytest.raises(
            writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED$"
        ),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


def test_an_orphan_audit_cannot_be_adopted_as_new_direct_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = intent(engine, scenario)
    with unit_of_work(engine, write=True) as con:
        audit(con, str(OWNER), "dividend_direct_receipt_intent", str(value.id), "SYNTHETIC_ORPHAN")
    before = database_snapshot(engine)
    with pytest.raises(writer.DirectReceiptWriteError):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["creation_reason", "cash_note", "fx_source", "request_id"])
def test_replay_independently_matches_origin_to_original_intent(
    engine: Engine, scenario: tuple[Any, str], field: str
) -> None:
    value, request = intent(engine, scenario), uuid4()
    saved = save(engine, value, request)
    wrong = uuid4() if field == "request_id" else "Different synthetic retained value"
    forged = replace(saved, origin=replace(saved.origin, **{field: wrong}))
    before = database_snapshot(engine)
    with (
        patch.object(combined, "history", return_value=(forged,)),
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        save(engine, value, request)
    assert database_snapshot(engine) == before


def test_inactive_owner_cannot_post_or_replay_cash(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = intent(engine, scenario)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with pytest.raises(writer.DirectReceiptWriteError, match="^DIRECT_RECEIPT_OWNER_UNAVAILABLE$"):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


def test_forged_noncanonical_components_are_not_silently_normalized(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = intent(engine, scenario)
    forged = value.model_copy(
        update={"components": value.components.model_copy(update={"received_amount": "006"})}
    )
    before = database_snapshot(engine)
    with pytest.raises(writer.DirectReceiptWriteError, match="^INVALID_DIRECT_RECEIPT_SCOPE$"):
        save(engine, forged, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["event_id", "account_id"])
def test_missing_current_pins_are_not_accepted(
    engine: Engine, scenario: tuple[Any, str], field: str
) -> None:
    value = intent(engine, scenario).model_copy(update={field: uuid4()})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_NOT_FOUND$"),
    ):
        writer._current_scope(con, OWNER, value)
    assert database_snapshot(engine) == before


def test_unavailable_rejection_history_does_not_allow_new_cash(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value = intent(engine, scenario)
    before = database_snapshot(engine)
    with (
        patch.object(rejection, "require_clear", side_effect=rejection.RejectionError("private")),
        pytest.raises(
            writer.DirectReceiptWriteError, match="^DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE$"
        ),
    ):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bad_time", ["private", datetime(2026, 1, 1)])
def test_invalid_clock_rolls_back_receipt_and_cash(
    engine: Engine, scenario: tuple[Any, str], bad_time: object
) -> None:
    value, request = intent(engine, scenario), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "now", return_value=bad_time),
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


def test_derived_hkd_overflow_is_not_truncated(engine: Engine, scenario: tuple[Any, str]) -> None:
    value = intent(engine, scenario)
    components = ReceiptComponents.model_validate(
        {**value.components.model_dump(), "received_amount": "9223372036854.775807"}
    )
    value = value.model_copy(update={"components": components})
    before = database_snapshot(engine)
    with pytest.raises(writer.DirectReceiptWriteError):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field", ["account_version", "portfolio_version", "broker_version", "data_revision"]
)
def test_numeric_scope_corruption_never_becomes_a_valid_pin(
    engine: Engine, scenario: tuple[Any, str], field: str
) -> None:
    value = intent(engine, scenario)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "p.version AS portfolio_version,p.data_revision,b.version AS broker_version" not in str(
            statement
        ):
            return result
        observed = result.mappings().first()
        assert observed is not None
        row = dict(observed)
        row[field] = float(row[field])
        replacement = MagicMock()
        replacement.mappings.return_value.first.return_value = row
        return replacement

    before = database_snapshot(engine)
    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["none", "missing", "wrong", "duplicate", "legacy"])
def test_postwrite_reader_mismatch_aborts_every_new_fact(
    engine: Engine, scenario: tuple[Any, str], problem: str
) -> None:
    value, request = intent(engine, scenario), uuid4()
    history = combined.history
    calls = 0

    def injected(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = history(*args, **kwargs)
        calls += 1
        if calls == 1:
            return result
        assert result is not None and len(result) == 1
        if problem == "none":
            return None
        if problem == "missing":
            return ()
        if problem == "wrong":
            return (replace(result[0], receipt=replace(result[0].receipt, id=uuid4())),)
        if problem == "duplicate":
            return (*result, *result)
        return (MagicMock(receipt=result[0].receipt),)

    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(combined, "history", side_effect=injected),
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


@pytest.mark.parametrize("problem", ["missing", "duplicate", "id"])
def test_invalid_intent_audit_cannot_replay(
    engine: Engine, scenario: tuple[Any, str], problem: str
) -> None:
    value, request = intent(engine, scenario), uuid4()
    save(engine, value, request)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if (
            "SELECT * FROM audit_events WHERE entity_type='dividend_direct_receipt_intent'"
            not in str(statement)
        ):
            return result
        rows = [dict(row) for row in result.mappings().all()]
        assert len(rows) == 1
        if problem == "missing":
            rows = []
        elif problem == "duplicate":
            rows.append(dict(rows[0]))
        else:
            rows[0]["id"] = "private-invalid"
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    before = database_snapshot(engine)
    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        save(engine, value, request)
    assert database_snapshot(engine) == before


def test_competing_new_receipts_must_refresh_the_same_data_revision(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    first, second = intent(engine, scenario), intent(engine, scenario)
    barrier = Barrier(2)

    def race(value: Any) -> str:
        barrier.wait(timeout=10)
        try:
            save(engine, value, uuid4())
            return "SAVED"
        except writer.DirectReceiptWriteError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race, value) for value in (first, second)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == ["DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED", "SAVED"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_direct_receipts").scalar_one() == 1
        )


def test_direct_cash_and_rejection_cannot_both_win_a_writer_race(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    value, rejected = intent(engine, scenario), rejection_intent(scenario[0])
    barrier = Barrier(2)

    def cash() -> str:
        barrier.wait(timeout=10)
        try:
            save(engine, value, uuid4())
            return "CASH_SAVED"
        except writer.DirectReceiptWriteError as error:
            return str(error)

    def decision() -> str:
        barrier.wait(timeout=10)
        try:
            reject(engine, rejected)
            return "REJECTION_SAVED"
        except rejection.RejectionError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cash), pool.submit(decision)]
        results = [future.result(timeout=30) for future in futures]
    assert results in (
        ["CASH_SAVED", "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"],
        ["DIVIDEND_REVISION_REJECTED", "REJECTION_SAVED"],
    )
    with unit_of_work(engine) as con:
        cash_count = con.exec_driver_sql(
            "SELECT count(*) FROM dividend_direct_receipts"
        ).scalar_one()
        rejected_count = con.exec_driver_sql(
            "SELECT count(*) FROM dividend_event_rejections"
        ).scalar_one()
        assert cash_count + rejected_count == 1
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_entitlements").scalar_one() == 0
