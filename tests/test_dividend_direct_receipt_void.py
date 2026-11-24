"""Direct receipt voids preserve original cash, locked FX and historical replay."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from threading import Barrier
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_void as voids
import spm_api.dividend_event_service as events
from spm_api.audit import now, stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import post_cash, rebase_cash_changes
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_combined_receipt_history import add_legacy
from tests.test_dividend_direct_receipt_migration import scenario as scenario
from tests.test_dividend_direct_receipt_writer import intent, save
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent, submit


def void_intent(engine: Engine, scenario: tuple[Any, str], record: Any) -> Any:
    event, account = scenario
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT a.portfolio_id,a.broker_id,a.version,p.version,b.version,p.data_revision "
            "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
            "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account,),
        ).one()
    return voids.DirectReceiptVoidIntent.model_validate(
        {
            "receipt_id": record.receipt.id,
            "event_id": event.event_id,
            "observed_revision_id": event.revision_id,
            "observed_revision_no": event.revision_no,
            "original_revision_id": record.revision_id,
            "original_revision_no": record.revision_no,
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


def cancel(engine: Engine, value: Any, request: UUID, *, owner: UUID = OWNER) -> Any:
    with unit_of_work(engine, write=True) as con:
        return voids.void(con, owner, value, request_id=request)


def test_void_retains_original_cash_and_posts_exact_reversal_once(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    creation, create_request = intent(engine, scenario), uuid4()
    original = save(engine, creation, create_request)
    value, request = void_intent(engine, scenario, original), uuid4()
    result = cancel(engine, value, request)
    assert result.receipt.id == original.receipt.id
    assert result.receipt.status == "VOID" and result.receipt.version == 2
    assert result.origin == original.origin
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    with unit_of_work(engine) as con:
        entries = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12,entry_role,external_flow "
            "FROM cash_ledger_entries WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=? "
            "ORDER BY entry_role",
            (str(value.receipt_id),),
        ).all()
        row = con.exec_driver_sql(
            "SELECT received_micro,received_hkd_micro,fx_rate_hkd_e12,tax_micro,fee_micro "
            "FROM dividend_direct_receipts WHERE id=?",
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
    assert cancel(engine, value, request) == result
    assert save(engine, creation, create_request).receipt.status == "VOID"
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    "currency,amount",
    [
        ("USD", "6"),
        ("HKD", "-6"),
        ("HKD", "0"),
        ("HKD", "9223372036854.775807"),
        ("HKD", "-9223372036854.775807"),
        ("USD", "9223372036854.775807"),
        ("USD", "-9223372036854.775807"),
    ],
)
def test_unknown_fx_or_identity_and_signed_cash_survive_void(
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
    original = save(engine, intent(engine, scenario, components=components), uuid4())
    value = void_intent(engine, scenario, original)
    result = cancel(engine, value, uuid4())
    assert result.receipt.received == original.receipt.received
    assert result.receipt.fx_rate_hkd == original.receipt.fx_rate_hkd
    assert result.receipt.received_hkd == original.receipt.received_hkd
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
            "WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=? ORDER BY entry_role",
            (str(value.receipt_id),),
        ).all()
    assert rows[1][0] == -rows[0][0] and rows[1][1] == rows[0][1]
    if currency == "USD":
        assert rows[0][2] is rows[1][2] is None
    else:
        assert rows[1][2] == -rows[0][2]


def test_void_reverses_actual_hkd_override_without_recomputing_fx(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    original_intent = intent(engine, scenario)
    components = {
        **original_intent.components.model_dump(),
        "actual_hkd": "46.123456",
        "actual_hkd_reason": REASON,
    }
    record = save(engine, intent(engine, scenario, components=components), uuid4())
    value = void_intent(engine, scenario, record)
    result = cancel(engine, value, uuid4())
    assert result.origin.actual_hkd_reason == REASON
    assert str(result.receipt.received_hkd) == "46.123456"
    with unit_of_work(engine) as con:
        reversal = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12 FROM cash_ledger_entries "
            "WHERE reverses_entry_id=?",
            (str(value.cash_entry_id),),
        ).one()
    assert tuple(reversal) == (-6_000_000, -46_123_456, 7_800_000_000_000)


def test_void_does_not_remove_the_event_revision_fence(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    cancel(engine, void_intent(engine, scenario, record), uuid4())
    next_event = next_intent(scenario[0], "VERIFY")
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        submit(engine, next_event, at=now())
    assert database_snapshot(engine) == before


def test_competing_distinct_void_requests_cannot_both_reverse_cash(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value = void_intent(engine, scenario, record)
    barrier = Barrier(2)

    def race() -> str:
        barrier.wait(timeout=10)
        try:
            cancel(engine, value, uuid4())
            return "VOIDED"
        except voids.DirectReceiptVoidError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(race) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == ["DIRECT_RECEIPT_VOID_CONFLICT", "VOIDED"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE reverses_entry_id=?",
                (str(value.cash_entry_id),),
            ).scalar_one()
            == 1
        )


def test_archived_account_can_void_without_changing_legacy_or_other_direct_receipts(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_legacy(engine, scenario)
    first = save(engine, intent(engine, scenario), uuid4())
    second = save(engine, intent(engine, scenario), uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?", (scenario[1],)
        )
    before = database_snapshot(engine)
    value = void_intent(engine, scenario, first)
    cancel(engine, value, uuid4())
    after = database_snapshot(engine)
    assert after["dividend_receipts"] == before["dividend_receipts"]
    assert after["dividend_entitlements"] == before["dividend_entitlements"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT status FROM dividend_direct_receipts WHERE id=?", (str(second.receipt.id),)
            ).scalar_one()
            == "ACTIVE"
        )


def test_wrong_original_current_or_cash_pins_and_conflicting_replay_are_zero_write(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    before = database_snapshot(engine)
    for field, replacement in (
        ("cash_entry_id", uuid4()),
        ("original_revision_id", uuid4()),
        ("original_revision_no", 2),
        ("observed_revision_id", uuid4()),
        ("account_version", value.account_version + 1),
        ("expected_receipt_version", 2),
        ("expected_data_revision", value.expected_data_revision + 1),
    ):
        with pytest.raises(voids.DirectReceiptVoidError):
            cancel(engine, value.model_copy(update={field: replacement}), uuid4())
    with pytest.raises(voids.DirectReceiptVoidError):
        cancel(engine, value, request, owner=OTHER)
    assert database_snapshot(engine) == before
    cancel(engine, value, request)
    after = database_snapshot(engine)
    for changed, key in (
        (value.model_copy(update={"reason": "Different synthetic void reason"}), request),
        (value, uuid4()),
    ):
        with pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_VOID_CONFLICT$"):
            cancel(engine, changed, key)
    assert database_snapshot(engine) == after


def test_caught_late_failure_rolls_back_exact_void_and_allows_same_request_retry(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "invalidate_portfolio", side_effect=ValueError("synthetic-private")),
        pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_VOID_UNAVAILABLE$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


def test_identical_void_requests_compete_without_duplicate_reversal(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cancel, engine, value, request)
        second = pool.submit(cancel, engine, value, request)
        assert first.result(timeout=30) == second.result(timeout=30)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE reverses_entry_id=?",
                (str(value.cash_entry_id),),
            ).scalar_one()
            == 1
        )


def test_preopening_create_and_void_preserve_the_opening_assertion(
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
    record = save(engine, intent(engine, scenario), uuid4())
    cancel(engine, void_intent(engine, scenario, record), uuid4())
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT sum(amount_micro) FROM cash_ledger_entries WHERE account_id=? "
                "AND effective_date<='2026-01-10'",
                (account,),
            ).scalar_one()
            == 100_000_000
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE source_type='OPENING_REBASE'"
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "change", ["account", "current_version", "current_data", "old_version", "old_data", "broker"]
)
def test_final_void_verification_rechecks_current_and_retained_portfolios(
    engine: Engine, scenario: tuple[Any, str], change: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    old_portfolio = record.origin.portfolio_id
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": "Synthetic moved portfolio",
                "created_at": stamp(),
            },
        )
        insert(
            con,
            "brokers",
            {
                "id": str(new_broker),
                "owner_id": str(OWNER),
                "name": "Synthetic moved broker",
            },
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), scenario[1]),
        )
    value, request = void_intent(engine, scenario, record), uuid4()
    table, identifier, column = {
        "account": ("accounts", value.account_id, "version"),
        "current_version": ("portfolios", new_portfolio, "version"),
        "current_data": ("portfolios", new_portfolio, "data_revision"),
        "old_version": ("portfolios", old_portfolio, "version"),
        "old_data": ("portfolios", old_portfolio, "data_revision"),
        "broker": ("brokers", new_broker, "version"),
    }[change]
    original = voids._verify_void

    def late(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        args[0].exec_driver_sql(
            f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (str(identifier),)
        )

    before = database_snapshot(engine)
    with (
        patch.object(voids, "_verify_void", side_effect=late),
        pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        cancel(engine, value, request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"
    with unit_of_work(engine) as con:
        portfolios = (
            con.exec_driver_sql(
                "SELECT portfolio_id FROM projection_invalidations "
                "WHERE source_id=? AND reason='DIVIDEND_DIRECT_RECEIPT_VOIDED' "
                "ORDER BY portfolio_id",
                (str(value.receipt_id),),
            )
            .scalars()
            .all()
        )
    assert portfolios == sorted([str(old_portfolio), str(new_portfolio)])


@pytest.mark.parametrize("kind", ["deferred", "owner", "request", "intent", "forged", "normalized"])
def test_invalid_internal_void_calls_fail_without_writes(
    engine: Engine, scenario: tuple[Any, str], kind: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request, owner = void_intent(engine, scenario, record), uuid4(), OWNER
    if kind == "owner":
        owner = "invalid"  # type: ignore[assignment]
    elif kind == "request":
        request = "invalid"  # type: ignore[assignment]
    elif kind == "intent":
        value = object()
    elif kind == "forged":
        value = value.model_copy(update={"expected_receipt_version": True})
    elif kind == "normalized":
        value = value.model_copy(update={"event_id": str(value.event_id)})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=kind != "deferred") as con,
        pytest.raises(voids.DirectReceiptVoidError),
    ):
        voids.void(con, owner, value, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["event_id", "account_id", "portfolio_id"])
def test_void_current_scope_checks_missing_owned_resources(
    engine: Engine, scenario: tuple[Any, str], field: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value = void_intent(engine, scenario, record).model_copy(update={field: uuid4()})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(voids.DirectReceiptVoidError),
    ):
        voids._scope(con, OWNER, value, record.origin)
    assert database_snapshot(engine) == before


def test_exhausted_retained_portfolio_revision_prevents_void(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value = void_intent(engine, scenario, record)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=? WHERE id=?", (INT64_MAX, str(value.portfolio_id))
        )
    before = database_snapshot(engine)
    with pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_VOID_UNAVAILABLE$"):
        cancel(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["missing", "legacy"])
def test_direct_void_does_not_adopt_legacy_or_missing_receipts(
    engine: Engine, scenario: tuple[Any, str], kind: str
) -> None:
    legacy = add_legacy(engine, scenario)
    record = save(engine, intent(engine, scenario), uuid4())
    value = void_intent(engine, scenario, record).model_copy(
        update={"receipt_id": UUID(legacy["receipt"]["id"]) if kind == "legacy" else uuid4()}
    )
    before = database_snapshot(engine)
    with pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_NOT_FOUND$"):
        cancel(engine, value, uuid4())
    assert database_snapshot(engine) == before


def test_void_request_cannot_be_reused_for_a_second_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    first = save(engine, intent(engine, scenario), uuid4())
    second = save(engine, intent(engine, scenario), uuid4())
    request = uuid4()
    cancel(engine, void_intent(engine, scenario, first), request)
    value = void_intent(engine, scenario, second)
    before = database_snapshot(engine)
    with pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_VOID_CONFLICT$"):
        cancel(engine, value, request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bad_time", ["private", datetime(2026, 1, 1)])
def test_invalid_void_clock_rolls_back_the_reversal(
    engine: Engine, scenario: tuple[Any, str], bad_time: object
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, "now", return_value=bad_time),
        pytest.raises(voids.DirectReceiptVoidError),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


@pytest.mark.parametrize("fault", ["receipt", "audit", "intent", "invalidation"])
def test_ignored_void_writes_never_commit_partial_reversal(
    engine: Engine, scenario: tuple[Any, str], fault: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value = void_intent(engine, scenario, record)
    table, operation, condition = {
        "receipt": ("dividend_direct_receipts", "UPDATE", "1"),
        "audit": ("audit_events", "INSERT", "NEW.action='DIVIDEND_DIRECT_RECEIPT_VOIDED'"),
        "intent": (
            "audit_events",
            "INSERT",
            "NEW.action='DIVIDEND_DIRECT_RECEIPT_VOID_INTENT_RECORDED'",
        ),
        "invalidation": ("projection_invalidations", "INSERT", "1"),
    }[fault]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"CREATE TRIGGER synthetic_ignore_direct_void BEFORE {operation} ON {table} "
            f"WHEN {condition} BEGIN SELECT RAISE(IGNORE); END"
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(voids.DirectReceiptVoidError),
    ):
        voids.void(con, OWNER, value, request_id=uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["rebase", "invalidation"])
def test_retained_portfolio_changes_inside_void_are_detected(
    engine: Engine, scenario: tuple[Any, str], phase: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    old_portfolio = str(record.origin.portfolio_id)
    with unit_of_work(engine, write=True) as con:
        new_portfolio = str(uuid4())
        insert(
            con,
            "portfolios",
            {
                "id": new_portfolio,
                "owner_id": str(OWNER),
                "name": "Synthetic moved target",
                "created_at": stamp(),
            },
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,version=version+1 WHERE id=?",
            (new_portfolio, scenario[1]),
        )
    value, request = void_intent(engine, scenario, record), uuid4()
    function = rebase_cash_changes if phase == "rebase" else invalidate_portfolio

    def late(*args: Any, **kwargs: Any) -> None:
        function(*args, **kwargs)
        args[0].exec_driver_sql(
            "UPDATE portfolios SET version=version+1 WHERE id=?", (old_portfolio,)
        )

    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(voids, function.__name__, side_effect=late),
        pytest.raises(voids.DirectReceiptVoidError, match="^DIRECT_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


@pytest.mark.parametrize("problem", ["none", "missing", "wrong", "duplicate", "legacy"])
def test_postvoid_reader_mismatch_rolls_back_all_new_effects(
    engine: Engine, scenario: tuple[Any, str], problem: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    read = combined.history
    calls = 0

    def injected(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = read(*args, **kwargs)
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
        pytest.raises(voids.DirectReceiptVoidError),
    ):
        voids.void(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert cancel(engine, value, request).receipt.status == "VOID"


@pytest.mark.parametrize("field", ["status", "version", "original_revision", "cash"])
def test_void_replay_rejects_inconsistent_retained_result(
    engine: Engine, scenario: tuple[Any, str], field: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    result = cancel(engine, value, request)
    forged = (
        replace(result, receipt=replace(result.receipt, status="ACTIVE"))
        if field == "status"
        else replace(result, receipt=replace(result.receipt, version=3))
        if field == "version"
        else replace(result, revision_id=uuid4())
        if field == "original_revision"
        else replace(result, origin=replace(result.origin, cash_entry_id=uuid4()))
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con, pytest.raises(voids.DirectReceiptVoidError):
        voids._verify_void(con, OWNER, value, request, forged)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate", "owner", "action", "before", "timestamp"]
)
def test_invalid_void_intent_audit_cannot_replay(
    engine: Engine, scenario: tuple[Any, str], problem: str
) -> None:
    record = save(engine, intent(engine, scenario), uuid4())
    value, request = void_intent(engine, scenario, record), uuid4()
    cancel(engine, value, request)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if (
            "SELECT * FROM audit_events WHERE entity_type='dividend_direct_receipt_void_intent'"
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
            field, replacement = {
                "owner": ("owner_id", str(OTHER)),
                "action": ("action", "INVALID"),
                "before": ("before_sha256", "a" * 64),
                "timestamp": ("created_at", "2026-01-01T00:00:00.000000Z"),
            }[problem]
            rows[0][field] = replacement
        mocked = MagicMock()
        mocked.mappings.return_value.all.return_value = rows
        return mocked

    before = database_snapshot(engine)
    with patch.object(Connection, "execute", injected), pytest.raises(voids.DirectReceiptVoidError):
        cancel(engine, value, request)
    assert database_snapshot(engine) == before
