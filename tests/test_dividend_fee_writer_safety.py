"""Failure, replay and concurrency boundaries for internal dividend fee decisions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as writer
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import audit
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_fee_history import append_setting, history
from tests.test_dividend_fee_writer import current, intent, save
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_rejection_writer import save as reject
from tests.test_dividend_tax_service import account_scope
from tests.test_dividend_tax_service import book as book


def test_cross_scope_identity_request_reuse_and_orphan_audit_fail_closed(
    engine: Engine, book: dict[str, Any]
) -> None:
    original, request = intent(book), uuid4()
    first = save(engine, original, request=request)
    other = {**book, **account_scope(engine)}
    orphan = intent(book, expected_setting_no=1, expected_head_id=first.id)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_fee_setting",
            str(orphan.id),
            "DIVIDEND_FEE_SETTING_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "orphan"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    for value, chosen_request in (
        (original, uuid4()),
        (intent(book, expected_setting_no=1, expected_head_id=first.id), request),
        (intent(other, id=original.id), uuid4()),
    ):
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_CONFLICT$"):
            save(engine, value, request=chosen_request)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        save(engine, orphan)
    assert database_snapshot(engine) == before


def test_missing_fee_still_requires_exact_observed_account_and_broker_pins(
    engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    pins: dict[str, Any] = {
        "effective_on": date(2026, 1, 8),
        "expected_setting_no": 0,
        "expected_head_id": None,
        "expected_portfolio_id": book["portfolio_id"],
        "expected_broker_id": book["broker_id"],
        "expected_account_version": book["account_version"],
        "expected_broker_version": book["broker_version"],
    }
    with unit_of_work(engine, write=True) as con:
        args = (con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
        assert writer.require_current(*args, **pins).status == "MISSING"
        con.exec_driver_sql(
            "UPDATE brokers SET version=version+1 WHERE id=?", (str(book["broker_id"]),)
        )
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SCOPE_CHANGED$"):
            writer.require_current(*args, **pins)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("expected_portfolio_id", "private-invalid", "INVALID_DIVIDEND_FEE_SCOPE"),
        ("expected_broker_id", "private-invalid", "INVALID_DIVIDEND_FEE_SCOPE"),
        ("expected_account_version", True, "INVALID_DIVIDEND_FEE_SCOPE"),
        ("expected_account_version", 0, "INVALID_DIVIDEND_FEE_SCOPE"),
        ("expected_broker_version", 9223372036854775808, "INVALID_DIVIDEND_FEE_SCOPE"),
        ("expected_broker_version", "1", "INVALID_DIVIDEND_FEE_SCOPE"),
        ("expected_portfolio_id", uuid4(), "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("expected_broker_id", uuid4(), "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("expected_account_version", 2, "DIVIDEND_FEE_SCOPE_CHANGED"),
        ("expected_broker_version", 2, "DIVIDEND_FEE_SCOPE_CHANGED"),
    ],
)
def test_current_scope_pins_are_exact_even_when_fee_is_missing(
    engine: Engine, book: dict[str, Any], field: str, value: Any, code: str
) -> None:
    event = book["event"]
    pins: dict[str, Any] = {
        "effective_on": date(2026, 1, 8),
        "expected_setting_no": 0,
        "expected_head_id": None,
        "expected_portfolio_id": book["portfolio_id"],
        "expected_broker_id": book["broker_id"],
        "expected_account_version": book["account_version"],
        "expected_broker_version": book["broker_version"],
        field: value,
    }
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(fees.FeeSettingError, match=f"^{code}$") as error,
    ):
        writer.require_current(
            con, OWNER, event.event_id, event.revision_id, 1, book["account_id"], **pins
        )
    assert "private-invalid" not in str(error.value)
    assert database_snapshot(engine) == before


def test_rejection_in_the_same_writer_invalidates_prior_current_check(
    engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    pins: dict[str, Any] = {
        "effective_on": date(2026, 1, 8),
        "expected_setting_no": 0,
        "expected_head_id": None,
        "expected_portfolio_id": book["portfolio_id"],
        "expected_broker_id": book["broker_id"],
        "expected_account_version": book["account_version"],
        "expected_broker_version": book["broker_version"],
    }
    with unit_of_work(engine, write=True) as con:
        args = (con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
        assert writer.require_current(*args, **pins).status == "MISSING"
        rejection_writer.save(con, OWNER, rejection_intent(event), request_id=uuid4())
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_REVISION_REJECTED$"):
            writer.require_current(*args, **pins)
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_REVISION_REJECTED$"):
            writer.save(con, OWNER, intent(book), request_id=uuid4())
    assert history(engine, book) == ()


def test_wrong_head_id_is_not_interchangeable_with_a_matching_number(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book))
    wrong = uuid4()
    before = database_snapshot(engine)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_VERSION_CONFLICT$"):
        save(engine, intent(book, expected_setting_no=1, expected_head_id=wrong))
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_SETTING_CHANGED$"):
        current(engine, book, head_no=1, head_id=wrong)
    assert history(engine, book) == (first,)
    assert database_snapshot(engine) == before


def test_future_and_backdated_estimates_keep_the_explicit_effective_day_and_head(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book, effective_from="2026-01-01", fee="1"))
    future = save(
        engine,
        intent(
            book,
            expected_setting_no=1,
            expected_head_id=first.id,
            effective_from="2026-06-01",
            fee="2",
        ),
    )
    backdated = save(
        engine,
        intent(
            book,
            expected_setting_no=2,
            expected_head_id=future.id,
            effective_from="2026-01-01",
            fee="3",
        ),
    )
    before = database_snapshot(engine)
    assert (
        current(engine, book, day=date(2025, 12, 31), head_no=3, head_id=backdated.id).status
        == "MISSING"
    )
    early = current(engine, book, day=date(2026, 2, 1), head_no=3, head_id=backdated.id)
    assert early.selected == backdated and early.head_id == backdated.id
    late = current(engine, book, day=date(2026, 6, 1), head_no=3, head_id=backdated.id)
    assert late.selected == future and late.head_id == backdated.id
    with unit_of_work(engine) as con:
        invalidation = con.exec_driver_sql(
            "SELECT from_date FROM projection_invalidations WHERE source_id=?", (str(future.id),)
        ).scalar_one()
    assert invalidation == book["event"].terms.ex_date
    assert database_snapshot(engine) == before


def test_caught_audit_failure_rolls_back_even_after_audit_insert(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)

    def fail_after_audit(con: Connection, *args: Any, **kwargs: Any) -> None:
        audit(con, *args, **kwargs)
        raise ValueError("synthetic private failure")

    with unit_of_work(engine, write=True) as con:
        with (
            patch.object(writer, "audit", side_effect=fail_after_audit),
            pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"),
        ):
            writer.save(con, OWNER, intent(book), request_id=uuid4())
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_fee_settings").scalar_one() == 0
    assert database_snapshot(engine) == before


def test_missing_audit_never_publishes_a_setting(engine: Engine, book: dict[str, Any]) -> None:
    before = database_snapshot(engine)
    with (
        patch.object(writer, "audit", return_value=None),
        pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, intent(book))
    assert database_snapshot(engine) == before


def test_owner_lookup_database_error_is_redacted(engine: Engine, book: dict[str, Any]) -> None:
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(con, "execute", side_effect=SQLAlchemyError("synthetic-private")),
        pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$") as error,
    ):
        writer.save(con, OWNER, intent(book), request_id=uuid4())
    assert "synthetic-private" not in str(error.value)
    assert database_snapshot(engine) == before


def test_noncanonical_or_invalid_intent_is_not_silently_normalized(
    engine: Engine, book: dict[str, Any]
) -> None:
    value = intent(book)
    invalid: Any = object()
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        for supplied in (invalid, value.model_copy(update={"fee": "0"})):
            with pytest.raises(fees.FeeSettingError, match="^INVALID_DIVIDEND_FEE_SCOPE$"):
                writer.save(con, OWNER, supplied, request_id=uuid4())
    assert database_snapshot(engine) == before


def test_post_insert_history_failure_rolls_back_the_entire_decision(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    real_history = fees.history
    calls = 0

    def fail_after_insert(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real_history(*args, **kwargs) if calls == 1 else None

    with unit_of_work(engine, write=True) as con:
        with (
            patch.object(fees, "history", side_effect=fail_after_insert),
            pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"),
        ):
            writer.save(con, OWNER, intent(book), request_id=uuid4())
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_fee_settings").scalar_one() == 0
    assert calls == 2 and database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["begin", "release"])
def test_uncertain_savepoint_failure_aborts_the_outer_writer(
    engine: Engine, book: dict[str, Any], phase: str
) -> None:
    before = database_snapshot(engine)
    failure: Exception | None = None
    with unit_of_work(engine, write=True) as con:
        target = con if phase == "begin" else engine.dialect
        method = "begin_nested" if phase == "begin" else "do_release_savepoint"
        with patch.object(target, method, side_effect=SQLAlchemyError("synthetic private failure")):
            try:
                writer.save(con, OWNER, intent(book), request_id=uuid4())
            except (fees.FeeSettingError, SQLAlchemyError) as error:
                failure = error
    assert isinstance(failure, fees.FeeSettingError)
    assert str(failure) == "DIVIDEND_FEE_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before


def test_uncertain_clock_cannot_create_fee_or_audit(engine: Engine, book: dict[str, Any]) -> None:
    before = database_snapshot(engine)
    for value, code in (
        (datetime(2000, 1, 1, tzinfo=UTC), "DIVIDEND_FEE_TIME_CONFLICT"),
        (datetime(2026, 1, 1), "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        ("not-a-clock", "DIVIDEND_FEE_HISTORY_UNAVAILABLE"),
        (datetime.now(UTC) + timedelta(minutes=1), "DIVIDEND_FEE_TIME_CONFLICT"),
    ):
        with (
            patch.object(writer, "now", return_value=value),
            pytest.raises(fees.FeeSettingError, match=f"^{code}$"),
        ):
            save(engine, intent(book))
        assert database_snapshot(engine) == before


def test_new_decision_cannot_predate_the_previous_correlated_audit(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = append_setting(engine, book, audit_delay=timedelta(minutes=1))
    previous = history(engine, book)[0]
    assert str(previous.id) == first["id"]
    before = database_snapshot(engine)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_TIME_CONFLICT$"):
        save(engine, intent(book, expected_setting_no=1, expected_head_id=previous.id))
    assert database_snapshot(engine) == before


def test_late_duplicate_fee_audit_blocks_even_an_unchanged_replay(
    engine: Engine, book: dict[str, Any]
) -> None:
    value, request = intent(book), uuid4()
    setting = save(engine, value, request=request)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_fee_setting",
            str(setting.id),
            "DIVIDEND_FEE_SETTING_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        save(engine, value, request=request)
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        current(engine, book, head_no=1, head_id=setting.id)
    assert database_snapshot(engine) == before


def test_rejection_and_fee_append_serialize_without_a_post_rejection_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, value = book["event"], intent(book)

    def fee_attempt() -> str:
        try:
            save(engine, value)
            return "saved"
        except fees.FeeSettingError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fee_future = pool.submit(fee_attempt)
        rejection_future = pool.submit(reject, engine, rejection_intent(event))
        outcome, rejected = fee_future.result(timeout=20), rejection_future.result(timeout=20)
    rows = history(engine, book)
    assert rejected.revision_id == event.revision_id
    assert outcome in {"saved", "DIVIDEND_REVISION_REJECTED"}
    assert len(rows) == (1 if outcome == "saved" else 0)
    if rows:
        assert rows[0].created_at <= rejected.created_at
    with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_REVISION_REJECTED$"):
        current(engine, book, head_no=len(rows), head_id=rows[-1].id if rows else None)


@pytest.mark.parametrize(
    "effective_on,number,head",
    [
        ("2026-01-08", 0, None),
        (datetime(2026, 1, 8, tzinfo=UTC), 0, None),
        (date(2026, 1, 8), True, None),
        (date(2026, 1, 8), -1, None),
        (date(2026, 1, 8), 0, uuid4()),
        (date(2026, 1, 8), 1, None),
        (date(2026, 1, 8), 1, "synthetic-private"),
    ],
)
def test_invalid_current_use_pins_never_write_or_leak(
    engine: Engine, book: dict[str, Any], effective_on: Any, number: Any, head: Any
) -> None:
    event = book["event"]
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(fees.FeeSettingError, match="^INVALID_DIVIDEND_FEE_SCOPE$") as error,
    ):
        writer.require_current(
            con,
            OWNER,
            event.event_id,
            event.revision_id,
            1,
            book["account_id"],
            effective_on=effective_on,
            expected_setting_no=number,
            expected_head_id=head,
            expected_portfolio_id=book["portfolio_id"],
            expected_broker_id=book["broker_id"],
            expected_account_version=book["account_version"],
            expected_broker_version=book["broker_version"],
        )
    assert "synthetic-private" not in str(error.value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("component", ["source", "rejection", "account"])
def test_unexpected_private_failures_are_redacted(
    engine: Engine, book: dict[str, Any], component: str
) -> None:
    target, method, exception = {
        "source": (
            event_writer,
            "require_current_source",
            events.EventError("synthetic-private"),
        ),
        "rejection": (rejections, "require_clear", rejections.RejectionError("synthetic-private")),
        "account": (writer, "account", SQLAlchemyError("synthetic-private")),
    }[component]
    before = database_snapshot(engine)
    with (
        patch.object(target, method, side_effect=exception),
        pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$") as error,
    ):
        current(engine, book)
    assert "synthetic-private" not in str(error.value)
    assert database_snapshot(engine) == before


def test_missing_account_and_invalid_owner_do_not_grant_current_use(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    event = book["event"]
    invalid_owner: Any = "private-invalid"
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(fees.FeeSettingError, match="^INVALID_DIVIDEND_FEE_SCOPE$"):
            writer.save(con, invalid_owner, intent(book), request_id=uuid4())
        with pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_NOT_FOUND$"):
            writer.require_current(
                con,
                OWNER,
                event.event_id,
                event.revision_id,
                1,
                uuid4(),
                effective_on=date(2026, 1, 8),
                expected_setting_no=0,
                expected_head_id=None,
                expected_portfolio_id=book["portfolio_id"],
                expected_broker_id=book["broker_id"],
                expected_account_version=book["account_version"],
                expected_broker_version=book["broker_version"],
            )
        with (
            patch.object(writer, "account", return_value=None),
            pytest.raises(fees.FeeSettingError, match="^DIVIDEND_FEE_NOT_FOUND$"),
        ):
            writer.save(con, OWNER, intent(book), request_id=uuid4())
    assert database_snapshot(engine) == before
