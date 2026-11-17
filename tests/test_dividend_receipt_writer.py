"""Internal receipt postings are atomic recorded facts, not economic authorization."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_audit as receipt_audit
import spm_api.dividend_receipt_writer as writer
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import post_cash, reverse_cash
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_receipt_audit import scope as scope
from tests.test_dividend_receipt_history import prepared


def intent(engine: Engine, scope: tuple[Any, str, dict[str, Any]], **changes: Any) -> Any:
    event, account, entitlement = scope
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT a.portfolio_id,a.broker_id,a.version,p.version,b.version,p.data_revision "
            "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
            "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account,),
        ).one()
    return writer.ReceiptIntent.model_validate(
        {
            "id": uuid4(),
            "event_id": event.event_id,
            "revision_id": event.revision_id,
            "revision_no": event.revision_no,
            "entitlement_id": UUID(entitlement["id"]),
            "account_id": UUID(account),
            "portfolio_id": UUID(row[0]),
            "broker_id": UUID(row[1]),
            "account_version": row[2],
            "portfolio_version": row[3],
            "broker_version": row[4],
            "expected_data_revision": row[5],
            "received_date": "2026-01-09",
            "components": {
                "received_amount": "6",
                "currency": "USD",
                "tax": None,
                "fee": "0",
                "fx_rate_hkd": "7.8",
                "fx_source": "Synthetic private FX source",
                "actual_hkd": None,
                "actual_hkd_reason": None,
            },
            "note": "Synthetic private receipt note",
            "reason": REASON,
            "confirmation": "RECORDED_CASH_ONLY",
            **changes,
        }
    )


def save(engine: Engine, value: Any, request: UUID) -> Any:
    with unit_of_work(engine, write=True) as con:
        return writer.save(con, OWNER, value, request_id=request)


def test_create_posts_exact_cash_and_audited_origin_once(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    result = save(engine, value, request)
    assert result.receipt.id == value.id and result.receipt.status == "ACTIVE"
    assert result.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert result.current_use_scope == "REVALIDATION_REQUIRED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.origin is not None and result.origin.fx_source == "Synthetic private FX source"
    assert result.origin.cash_note == value.note and result.origin.creation_reason == value.reason
    assert value.note not in repr(result.origin) and value.reason not in repr(result.origin)
    with unit_of_work(engine) as con:
        receipt = con.exec_driver_sql(
            "SELECT received_micro,tax_micro,fee_micro,fx_rate_hkd_e12,received_hkd_micro "
            "FROM dividend_receipts WHERE id=?",
            (str(value.id),),
        ).one()
        cash = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,external_flow FROM cash_ledger_entries "
            "WHERE source_type='DIVIDEND_RECEIPT' AND source_id=?",
            (str(value.id),),
        ).one()
        revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (str(value.portfolio_id),)
        ).scalar_one()
        invalidations = con.exec_driver_sql(
            "SELECT count(*) FROM projection_invalidations WHERE source_id=?", (str(value.id),)
        ).scalar_one()
    assert tuple(receipt) == (6_000_000, None, 0, 7_800_000_000_000, 46_800_000)
    assert tuple(cash) == (6_000_000, 46_800_000, 0)
    assert revision == value.expected_data_revision + 1 and invalidations == 1
    after = database_snapshot(engine)
    assert after != before
    assert save(engine, value, request) == result
    assert database_snapshot(engine) == after


def test_unknown_fx_stays_unknown_and_does_not_invent_cash_valuation(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
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
    value = intent(engine, scope, components=components)
    result = save(engine, value, uuid4())
    assert result.receipt.received_hkd is None and result.receipt.fx_rate_hkd is None
    assert result.origin is not None and result.origin.fx_source is None
    with unit_of_work(engine) as con:
        assert tuple(
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
                "WHERE source_type='DIVIDEND_RECEIPT' AND source_id=?",
                (str(value.id),),
            ).one()
        ) == (None, None)


def test_existing_receipt_replays_after_clock_rollback_but_new_future_post_is_rejected(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    result = save(engine, value, request)
    next_value = intent(engine, scope)
    before = database_snapshot(engine)

    class EarlierDate(date):
        @classmethod
        def today(cls) -> "EarlierDate":
            return cls(2025, 1, 1)

    with patch("spm_api.cash_models.date", EarlierDate):
        assert save(engine, value, request) == result
        with pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_DATE_INVALID$"):
            save(engine, next_value, uuid4())
    assert database_snapshot(engine) == before


def test_intent_conflict_and_stale_account_scope_are_zero_write(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    save(engine, value, request)
    changed = value.model_copy(update={"note": "Different private note"})
    stale = intent(engine, scope).model_copy(update={"account_version": value.account_version + 1})
    before = database_snapshot(engine)
    for candidate, key, error in (
        (changed, request, "DIVIDEND_RECEIPT_CONFLICT"),
        (value, uuid4(), "DIVIDEND_RECEIPT_CONFLICT"),
        (stale, uuid4(), "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
    ):
        with pytest.raises(writer.ReceiptWriteError, match=f"^{error}$"):
            save(engine, candidate, key)
    assert database_snapshot(engine) == before


def test_caught_late_failure_rolls_back_all_facts_and_allows_retry(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "invalidate_portfolio", side_effect=ValueError("private")),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    result = save(engine, value, request)
    assert result.receipt.id == value.id


def test_preopening_receipt_rebases_the_explicit_opening_assertion(
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
    value = intent(engine, scope)
    save(engine, value, uuid4())
    with unit_of_work(engine) as con:
        rebase = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,effective_date FROM cash_ledger_entries "
            "WHERE source_type='OPENING_REBASE' AND source_id=?",
            (str(value.id),),
        ).one()
        anchor = con.exec_driver_sql(
            "SELECT sum(amount_micro) FROM cash_ledger_entries WHERE account_id=? "
            "AND effective_date<='2026-01-10'",
            (account,),
        ).scalar_one()
    assert tuple(rebase) == (-6_000_000, -46_800_000, "2026-01-10")
    assert anchor == 100_000_000


def test_same_request_competing_writers_produce_one_record_and_one_cash_effect(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, engine, value, request)
        second = pool.submit(save, engine, value, request)
        assert first.result(timeout=30) == second.result(timeout=30)
    with unit_of_work(engine) as con:
        count = con.exec_driver_sql(
            "SELECT count(*) FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' "
            "AND source_id=?",
            (str(value.id),),
        ).scalar_one()
    assert count == 1


def test_foreign_owner_cannot_write_or_obtain_private_details(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_NOT_FOUND$"),
    ):
        writer.save(con, OTHER, value, request_id=request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("components", "expected_rate", "expected_source", "expected_hkd"),
    [
        (
            {
                "received_amount": "6",
                "currency": "HKD",
                "tax": None,
                "fee": None,
                "fx_rate_hkd": None,
                "fx_source": None,
                "actual_hkd": None,
                "actual_hkd_reason": None,
            },
            1_000_000_000_000,
            "IDENTITY",
            6_000_000,
        ),
        (
            {
                "received_amount": "6",
                "currency": "USD",
                "tax": "0.5",
                "fee": "0.25",
                "fx_rate_hkd": "7.8",
                "fx_source": "Synthetic FX",
                "actual_hkd": "45.123456",
                "actual_hkd_reason": REASON,
            },
            7_800_000_000_000,
            "Synthetic FX",
            45_123_456,
        ),
        (
            {
                "received_amount": "-1",
                "currency": "USD",
                "tax": None,
                "fee": "0",
                "fx_rate_hkd": "7.8",
                "fx_source": "Synthetic FX",
                "actual_hkd": None,
                "actual_hkd_reason": None,
            },
            7_800_000_000_000,
            "Synthetic FX",
            -7_800_000,
        ),
    ],
)
def test_identity_override_and_negative_cash_are_recorded_exactly(
    engine: Engine,
    scope: tuple[Any, str, dict[str, Any]],
    components: dict[str, Any],
    expected_rate: int,
    expected_source: str,
    expected_hkd: int,
) -> None:
    value = intent(engine, scope, components=components)
    result = save(engine, value, uuid4())
    assert result.origin is not None and result.origin.fx_source == expected_source
    assert result.origin.actual_hkd_reason == components["actual_hkd_reason"]
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT fx_rate_hkd_e12,received_hkd_micro FROM dividend_receipts WHERE id=?",
            (str(value.id),),
        ).one()
    assert tuple(row) == (expected_rate, expected_hkd)


def test_derived_hkd_overflow_is_an_invalid_input_not_a_partial_cash_post(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value = intent(engine, scope)
    components = value.components.model_dump()
    components.update(received_amount="9223372036854.775807", fx_rate_hkd="2")
    value = writer.ReceiptIntent.model_validate({**value.model_dump(), "components": components})
    before = database_snapshot(engine)
    with pytest.raises(writer.ReceiptWriteError, match="^INVALID_DIVIDEND_RECEIPT_TERMS$"):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


def test_two_partial_receipts_keep_separate_actual_cash_without_double_deducting_fees(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    first = intent(engine, scope)
    save(engine, first, uuid4())
    second = intent(engine, scope)
    parts = second.components.model_dump()
    parts.update(received_amount="4", tax="1", fee="0.5")
    second = writer.ReceiptIntent.model_validate({**second.model_dump(), "components": parts})
    save(engine, second, uuid4())
    with unit_of_work(engine) as con:
        entries = (
            con.exec_driver_sql(
                "SELECT amount_micro FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' "
                "AND source_id IN (?,?) ORDER BY amount_micro",
                (str(first.id), str(second.id)),
            )
            .scalars()
            .all()
        )
    assert list(entries) == [4_000_000, 6_000_000]


def test_replay_normalizes_equivalent_decimal_strings_without_new_writes(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    result = save(engine, value, request)
    components = value.components.model_dump()
    components.update(received_amount="006.000", fx_rate_hkd="07.8000", fee="0.000")
    equivalent = writer.ReceiptIntent.model_validate(
        {**value.model_dump(), "components": components}
    )
    before = database_snapshot(engine)
    assert equivalent == value
    assert save(engine, equivalent, request) == result
    assert database_snapshot(engine) == before


def test_old_entitlement_keeps_its_original_physical_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, second, account, older, _ = prepared(engine)
    value = intent(engine, (second, str(account), older))
    result = save(engine, value, uuid4())
    assert result.original_revision_id == first.revision_id
    assert result.origin is not None and result.origin.observed_revision_id == second.revision_id


def test_replay_remains_historical_after_account_archive(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    result = save(engine, value, request)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(value.account_id),),
        )
    new_value = intent(engine, scope)
    before = database_snapshot(engine)
    assert save(engine, value, request) == result
    with pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_ACCOUNT_ARCHIVED$"):
        save(engine, new_value, uuid4())
    assert database_snapshot(engine) == before


def test_create_replay_after_exact_void_reports_current_state_without_reposting(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    save(engine, value, request)
    with unit_of_work(engine, write=True) as con:
        receipt = dict(
            con.exec_driver_sql("SELECT * FROM dividend_receipts WHERE id=?", (str(value.id),))
            .mappings()
            .one()
        )
        origin = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_receipt_origins WHERE receipt_id=?", (str(value.id),)
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
        reversal_id = reverse_cash(con, cash, reason=REASON)
        reversal = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (reversal_id,))
            .mappings()
            .one()
        )
        at, void_request = datetime.now(UTC), str(uuid4())
        at_text = stamp(at)
        audit(
            con,
            str(OWNER),
            "dividend_receipt",
            str(value.id),
            "DIVIDEND_RECEIPT_VOIDED",
            request_id=void_request,
            before=receipt_audit._creation_payload(receipt, origin, cash, REASON),
            after=receipt_audit._void_payload(
                receipt, origin, reversal, void_request, REASON, at_text
            ),
            reason=REASON,
            at=at,
        )
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
            (at_text, str(value.id)),
        )
    before = database_snapshot(engine)
    replay = save(engine, value, request)
    assert replay.receipt.status == "VOID" and replay.receipt.version == 2
    assert replay.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "replacement", "error"),
    [
        ("revision_id", uuid4(), "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("revision_no", 2, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("entitlement_id", uuid4(), "DIVIDEND_RECEIPT_NOT_FOUND"),
        ("portfolio_id", uuid4(), "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("broker_id", uuid4(), "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("account_version", 2, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("portfolio_version", 2, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("broker_version", 2, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
        ("expected_data_revision", 1, "DIVIDEND_RECEIPT_SCOPE_CHANGED"),
    ],
)
def test_every_observed_current_pin_is_required(
    engine: Engine,
    scope: tuple[Any, str, dict[str, Any]],
    field: str,
    replacement: object,
    error: str,
) -> None:
    value = intent(engine, scope).model_copy(update={field: replacement})
    before = database_snapshot(engine)
    with pytest.raises(writer.ReceiptWriteError, match=f"^{error}$"):
        save(engine, value, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("id", "private-not-a-uuid"),
        ("account_version", True),
        ("portfolio_version", 0),
        ("broker_version", 1.0),
        ("expected_data_revision", INT64_MAX),
        ("received_date", "not-a-date"),
        ("note", "private\x00note"),
        ("reason", "short"),
        ("confirmation", "OTHER"),
        ("components", "private-not-a-model"),
    ],
)
def test_forged_models_fail_closed_without_serializing_private_values(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], field: str, replacement: object
) -> None:
    value = intent(engine, scope).model_copy(update={field: replacement})
    before = database_snapshot(engine)
    with pytest.raises(writer.ReceiptWriteError, match="^INVALID_DIVIDEND_RECEIPT_SCOPE$") as error:
        save(engine, value, uuid4())
    assert "private" not in str(error.value)
    assert database_snapshot(engine) == before


def test_all_intent_fields_and_confirmation_are_explicit(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value = intent(engine, scope)
    payload = value.model_dump()
    for key in ("note", "reason", "confirmation", "components"):
        missing = {name: item for name, item in payload.items() if name != key}
        with pytest.raises(ValidationError):
            writer.ReceiptIntent.model_validate(missing)
    for key in ("tax", "fee", "fx_rate_hkd", "fx_source", "actual_hkd", "actual_hkd_reason"):
        components = {name: item for name, item in payload["components"].items() if name != key}
        with pytest.raises(ValidationError):
            writer.ReceiptIntent.model_validate({**payload, "components": components})


@pytest.mark.parametrize("field", ["note", "reason", "fx_source", "actual_hkd_reason"])
def test_non_utf8_receipt_text_is_an_invalid_input_without_private_details(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], field: str
) -> None:
    payload = intent(engine, scope).model_dump()
    private = "Synthetic private \ud800 evidence"
    if field in {"note", "reason"}:
        payload[field] = private
    else:
        payload["components"][field] = private
        if field == "actual_hkd_reason":
            payload["components"]["actual_hkd"] = "45"
    with pytest.raises(ValidationError) as error:
        writer.ReceiptIntent.model_validate(payload)
    assert "Synthetic private" not in str(error.value)


def test_nonwriter_invalid_request_and_reused_request_fail_without_writes(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(writer.ReceiptWriteError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.ReceiptWriteError, match="^INVALID_DIVIDEND_RECEIPT_SCOPE$"),
    ):
        writer.save(con, OWNER, value, request_id="private")  # type: ignore[arg-type]
    assert database_snapshot(engine) == before
    save(engine, value, request)
    other = intent(engine, scope)
    before = database_snapshot(engine)
    with pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_CONFLICT$"):
        save(engine, other, request)
    assert database_snapshot(engine) == before


def test_competing_new_receipts_with_same_scope_have_one_winner(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    first, second = intent(engine, scope), intent(engine, scope)

    def attempt(value: Any) -> str:
        try:
            save(engine, value, uuid4())
            return "saved"
        except writer.ReceiptWriteError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, item) for item in (first, second)]
        results = sorted(future.result(timeout=30) for future in futures)
    assert results == ["DIVIDEND_RECEIPT_SCOPE_CHANGED", "saved"]
    with unit_of_work(engine) as con:
        count = con.exec_driver_sql(
            "SELECT count(*) FROM dividend_receipts WHERE id IN (?,?)",
            (str(first.id), str(second.id)),
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize("failure_point", ["post_cash", "second_audit", "rebase"])
def test_caught_failure_at_each_financial_stage_rolls_back_and_preserves_retry(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], failure_point: str
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    original_audit = audit

    def injected(*args: Any, **kwargs: Any) -> None:
        if args[4] == "DIVIDEND_RECEIPT_INTENT_RECORDED":
            raise ValueError("private injected failure")
        original_audit(*args, **kwargs)

    target, side_effect = {
        "post_cash": ("post_cash", ValueError("private injected failure")),
        "second_audit": ("audit", injected),
        "rebase": ("rebase_cash_changes", ValueError("private injected failure")),
    }[failure_point]
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, target, side_effect=side_effect),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


def test_replay_rejects_a_creation_reason_that_disagrees_with_the_bound_intent(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    save(engine, value, request)
    with unit_of_work(engine) as con:
        receipt = dict(
            con.exec_driver_sql("SELECT * FROM dividend_receipts WHERE id=?", (str(value.id),))
            .mappings()
            .one()
        )
        origin = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_receipt_origins WHERE receipt_id=?", (str(value.id),)
            )
            .mappings()
            .one()
        )
        cash = dict(
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE id=?", (origin["cash_entry_id"],)
            )
            .mappings()
            .one()
        )
    different_reason = "Synthetic different recorded reason"
    forged = digest(
        canonical(receipt_audit._creation_payload(receipt, origin, cash, different_reason))
    )
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if (
            "SELECT * FROM audit_events WHERE entity_type='dividend_receipt' AND entity_id=:id"
            not in str(statement)
        ):
            return result
        rows = [dict(row) for row in result.mappings().all()]
        assert len(rows) == 1
        rows[0].update(reason=different_reason, after_sha256=forged)
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    before = database_snapshot(engine)
    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, value, request)
    assert database_snapshot(engine) == before


def test_replay_rejects_a_cash_note_that_disagrees_with_the_bound_intent(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    save(engine, value, request)
    with unit_of_work(engine) as con:
        receipt = dict(
            con.exec_driver_sql("SELECT * FROM dividend_receipts WHERE id=?", (str(value.id),))
            .mappings()
            .one()
        )
        origin = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_receipt_origins WHERE receipt_id=?", (str(value.id),)
            )
            .mappings()
            .one()
        )
        cash = dict(
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE id=?", (origin["cash_entry_id"],)
            )
            .mappings()
            .one()
        )
    cash["note"] = "Synthetic different cash note"
    forged = digest(canonical(receipt_audit._creation_payload(receipt, origin, cash, REASON)))
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        sql = str(statement)
        replacement = MagicMock()
        if "SELECT * FROM cash_ledger_entries WHERE id=:id" in sql:
            replacement.mappings.return_value.one.return_value = cash
        elif (
            "SELECT * FROM audit_events WHERE entity_type='dividend_receipt' AND entity_id=:id"
            in sql
        ):
            rows = [dict(row) for row in result.mappings().all()]
            assert len(rows) == 1
            rows[0]["after_sha256"] = forged
            replacement.mappings.return_value.all.return_value = rows
        else:
            return result
        return replacement

    before = database_snapshot(engine)
    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, value, request)
    assert database_snapshot(engine) == before


def test_same_transaction_account_change_rolls_back_the_attempted_post(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    original = post_cash

    def changed(con: Connection, **kwargs: Any) -> str:
        result = original(con, **kwargs)
        con.exec_driver_sql(
            "UPDATE accounts SET version=version+1 WHERE id=?", (str(value.account_id),)
        )
        return result

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "post_cash", side_effect=changed),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


def test_inactive_and_malformed_owners_cannot_write(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.ReceiptWriteError, match="^INVALID_DIVIDEND_RECEIPT_SCOPE$"),
    ):
        writer.save(con, "private", value, request_id=request)  # type: ignore[arg-type]
    with pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_OWNER_UNAVAILABLE$"):
        save(engine, value, request)
    assert database_snapshot(engine) == before


def test_forged_noncanonical_components_cannot_be_revalidated_as_new_intent(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value = intent(engine, scope)
    components = value.components.model_copy(update={"received_amount": "006"})
    forged = value.model_copy(update={"components": components})
    before = database_snapshot(engine)
    with pytest.raises(writer.ReceiptWriteError, match="^INVALID_DIVIDEND_RECEIPT_SCOPE$"):
        save(engine, forged, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["missing", "duplicate", "bad-id"])
def test_missing_duplicate_or_malformed_intent_audit_cannot_replay(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], problem: str
) -> None:
    value, request = intent(engine, scope), uuid4()
    save(engine, value, request)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "SELECT * FROM audit_events WHERE entity_type='dividend_receipt_intent'" not in str(
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
            rows[0]["id"] = "private-invalid"
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    before = database_snapshot(engine)
    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, value, request)
    assert database_snapshot(engine) == before


def test_unrelated_audit_with_the_same_receipt_id_cannot_be_adopted(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    with unit_of_work(engine, write=True) as con:
        audit(con, str(OWNER), "dividend_receipt_intent", str(value.id), "UNRELATED")
    before = database_snapshot(engine)
    with pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"):
        save(engine, value, request)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bad_time", ["private-invalid", datetime(2026, 1, 1)])
def test_invalid_audit_clock_rolls_back_prior_receipt_and_cash(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], bad_time: object
) -> None:
    value, request = intent(engine, scope), uuid4()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(writer, "now", return_value=bad_time),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


@pytest.mark.parametrize("problem", ["missing-result", "wrong-result"])
def test_postwrite_reader_mismatch_aborts_the_whole_savepoint(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], problem: str
) -> None:
    value, request = intent(engine, scope), uuid4()
    original = receipt_audit.history
    calls = 0

    def injected(con: Connection, owner: UUID, event_id: UUID, account_id: UUID) -> Any:
        nonlocal calls
        calls += 1
        result = original(con, owner, event_id, account_id)
        if calls == 1:
            return result
        assert result is not None and len(result) == 1
        if problem == "missing-result":
            return ()
        return (replace(result[0], receipt=replace(result[0].receipt, id=uuid4())),)

    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(receipt_audit, "history", side_effect=injected),
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer.save(con, OWNER, value, request_id=request)
    assert database_snapshot(engine) == before
    assert save(engine, value, request).receipt.id == value.id


@pytest.mark.parametrize("field", ["event_id", "account_id"])
def test_missing_current_scope_fails_closed_without_writes(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], field: str
) -> None:
    value = intent(engine, scope).model_copy(update={field: uuid4()})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_NOT_FOUND$"),
    ):
        writer._current_scope(con, OWNER, value)
    assert database_snapshot(engine) == before


def test_receipt_origin_missing_from_a_replay_fails_closed(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    value, request = intent(engine, scope), uuid4()
    result = save(engine, value, request)
    forged = replace(result, origin=None)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(writer.ReceiptWriteError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        writer._verify_intent(con, OWNER, value, request, forged)
    assert database_snapshot(engine) == before
