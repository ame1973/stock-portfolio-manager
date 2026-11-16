"""Projection fee provenance never turns missing evidence into a financial grant."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_fee_service as fees
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.dividend_projection_history import ProjectionHistoryError, history
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_fee_history import append_setting
from tests.test_dividend_fee_writer import intent as fee_intent
from tests.test_dividend_fee_writer import save as save_fee
from tests.test_dividend_projection_fee import provenance, read
from tests.test_dividend_projection_history import append, read_with_simulated_projection
from tests.test_dividend_projection_migration import TABLE, snapshot_values
from tests.test_dividend_projection_tax import book as book
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_rejection_writer import save as reject


def sql_projection(
    engine: Engine, book: dict[str, Any], metadata: dict[str, Any], **changes: Any
) -> None:
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **metadata, **changes})


def test_unknown_and_missing_cannot_become_an_expected_zero_projection(
    engine: Engine, book: dict[str, Any]
) -> None:
    future = save_fee(
        engine, fee_intent(book, fee="0", fee_currency="USD", effective_from="2026-06-01")
    )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"):
        sql_projection(engine, book, provenance(future, None, "MISSING"))
    assert database_snapshot(engine) == before
    unknown = save_fee(
        engine,
        fee_intent(
            book,
            expected_setting_no=1,
            expected_head_id=future.id,
            fee_status="UNKNOWN",
            fee=None,
            fee_currency="USD",
        ),
    )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"):
        sql_projection(engine, book, provenance(unknown, unknown, "UNKNOWN"))
    assert database_snapshot(engine) == before


def test_original_fee_currency_must_match_the_selected_estimate(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0", fee_currency="USD"))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"):
        sql_projection(
            engine,
            book,
            provenance(setting, setting, "ESTIMATED"),
            fee_original_currency="HKD",
            fee_rate_event_e12=500_000_000_000,
            fee_rate_source="MANUAL:synthetic",
        )
    assert database_snapshot(engine) == before


def test_projection_cannot_predate_any_observed_fee_audit(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = append_setting(engine, book, audit_delay=timedelta(minutes=1), fee_currency="USD")
    second = append_setting(engine, book, previous=first, fee_currency="USD")
    event = book["event"]
    with unit_of_work(engine) as con:
        rows = fees.history(con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
    assert rows is not None and str(rows[-1].id) == second["id"]
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"):
        sql_projection(engine, book, provenance(rows[-1], rows[-1], "ESTIMATED"))
    assert database_snapshot(engine) == before


def test_history_rejects_a_projection_timestamp_before_its_fee_audit(
    engine: Engine, book: dict[str, Any]
) -> None:
    stored = append_setting(
        engine, book, audit_delay=timedelta(milliseconds=500), fee_currency="HKD", fee_micro=1
    )
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine) as con:
        settings = fees.history(
            con, OWNER, event.event_id, event.revision_id, 1, book["account_id"]
        )
    assert settings is not None
    with unit_of_work(engine, write=True) as con:
        append(con, event, account, **provenance(settings[0], settings[0], "ESTIMATED"))
    before = database_snapshot(engine)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read_with_simulated_projection(engine, event, account, {"created_at": stored["created_at"]})
    assert database_snapshot(engine) == before


def test_late_duplicate_fee_audit_invalidates_retained_projection_without_repair(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0.000001", fee_currency="HKD"))
    with unit_of_work(engine, write=True) as con:
        append(
            con, book["event"], str(book["account_id"]), **provenance(setting, setting, "ESTIMATED")
        )
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
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, book)
    assert database_snapshot(engine) == before


def test_sql_shaped_projection_audit_is_not_canonical_economic_evidence(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0", fee_currency="USD"))
    sql_projection(engine, book, provenance(setting, setting, "ESTIMATED"))
    before = database_snapshot(engine)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, book)
    assert database_snapshot(engine) == before


def test_wal_snapshot_keeps_the_observed_fee_prefix_during_a_later_write(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0.000001", fee_currency="HKD"))
    with unit_of_work(engine, write=True) as con:
        append(
            con,
            book["event"],
            str(book["account_id"]),
            **provenance(setting, setting, "ESTIMATED"),
        )
    with unit_of_work(engine) as reader:
        args = (reader, OWNER, book["event"].event_id, book["account_id"])
        original = history(*args)
        assert original is not None and original[0].fee_provenance is not None
        assert original[0].fee_provenance.observed_head_id == setting.id
        later_intent = fee_intent(
            book,
            expected_setting_no=1,
            expected_head_id=setting.id,
            fee_status="UNKNOWN",
            fee=None,
            fee_currency=None,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            later = pool.submit(save_fee, engine, later_intent).result(timeout=10)
        assert history(*args) == original
    assert later.setting_no == 2 and read(engine, book) == original


def test_later_rejection_does_not_erase_a_retained_projection_or_grant_current_use(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0.000001", fee_currency="HKD"))
    with unit_of_work(engine, write=True) as con:
        append(
            con, book["event"], str(book["account_id"]), **provenance(setting, setting, "ESTIMATED")
        )
    original = read(engine, book)
    reject(engine, rejection_intent(book["event"]))
    before = database_snapshot(engine)
    retained = read(engine, book)
    assert retained == original
    assert retained[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    assert database_snapshot(engine) == before
