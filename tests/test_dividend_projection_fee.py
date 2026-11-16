"""Projection fee provenance is a retained owner estimate, never an income grant."""

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_fee_service as fees
from spm_api.audit import canonical, digest
from spm_api.db import unit_of_work
from spm_api.dividend_projection_arithmetic import _AMOUNT_FIELDS
from spm_api.dividend_projection_history import ProjectionHistoryError, _payload, history
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_fee_writer import intent as fee_intent
from tests.test_dividend_fee_writer import save as save_fee
from tests.test_dividend_projection_history import append, read_with_simulated_projection
from tests.test_dividend_projection_migration import TABLE, snapshot_values
from tests.test_dividend_projection_tax import book as book
from tests.test_dividend_projection_tax import provenance as tax_provenance
from tests.test_dividend_tax_service import account_scope
from tests.test_dividend_tax_service import intent as tax_intent
from tests.test_dividend_tax_service import save as save_tax


def provenance(
    head: fees.FeeSetting | None,
    selected: fees.FeeSetting | None,
    status: str,
    *,
    day: str = "2026-01-08",
) -> dict[str, Any]:
    return {
        "fee_provenance_version": 1,
        "fee_effective_on": day,
        "fee_observed_head_no": head.setting_no if head else 0,
        "fee_observed_head_id": str(head.id) if head else None,
        "fee_selected_setting_id": str(selected.id) if selected else None,
        "fee_estimate_status": status,
    }


def read(engine: Engine, book: dict[str, Any]) -> Any:
    with unit_of_work(engine) as con:
        return history(con, OWNER, book["event"].event_id, book["account_id"])


def test_legacy_fee_amount_remains_unlinked_not_a_confirmed_estimate(
    engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        append(con, book["event"], str(book["account_id"]))
    rows = read(engine, book)
    assert rows is not None and rows[0].fee_provenance is None
    assert rows[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"


def test_missing_unknown_zero_and_backdated_head_preserve_each_snapshot(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], str(book["account_id"])
    empty: dict[str, Any] = {name: None for name in _AMOUNT_FIELDS}
    future = save_fee(engine, fee_intent(book, effective_from="2026-06-01", fee="1"))
    with unit_of_work(engine, write=True) as con:
        first = append(
            con,
            event,
            account,
            state="NEEDS_REVIEW",
            **empty,
            **provenance(future, None, "MISSING"),
        )
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
    with unit_of_work(engine, write=True) as con:
        second = append(
            con,
            event,
            account,
            previous=first,
            state="NEEDS_REVIEW",
            **empty,
            **provenance(unknown, unknown, "UNKNOWN"),
        )
    zero = save_fee(
        engine,
        fee_intent(
            book,
            expected_setting_no=2,
            expected_head_id=unknown.id,
            fee="0",
            fee_currency="USD",
        ),
    )
    with unit_of_work(engine, write=True) as con:
        third = append(
            con,
            event,
            account,
            previous=second,
            fee_micro=0,
            fee_original_micro=0,
            fee_original_currency="USD",
            fee_rate_event_e12=1_000_000_000_000,
            fee_rate_source="IDENTITY",
            expected_net_micro=1_000_000,
            rounding_delta_micro=0,
            expected_hkd_micro=7_800_000,
            **provenance(zero, zero, "ESTIMATED"),
        )
    original = read(engine, book)
    assert original is not None
    assert [row.fee_provenance.status for row in original] == ["MISSING", "UNKNOWN", "ESTIMATED"]
    assert [row.fee_provenance.observed_head_no for row in original] == [1, 2, 3]
    assert original[0].fee_provenance.selected_setting_id is None
    assert original[1].fee_provenance.selected_setting_id == unknown.id
    assert original[2].fee_provenance.selected_setting_id == zero.id
    assert original[2].fee_original == 0 and original[2].fee_currency == "USD"
    assert original[2].fee_provenance.scope == "OWNER_ESTIMATE_ONLY"
    assert str(zero.id) not in repr(original[2].fee_provenance)
    later = save_fee(
        engine,
        fee_intent(
            book,
            expected_setting_no=3,
            expected_head_id=zero.id,
            fee_status="UNKNOWN",
            fee=None,
            fee_currency=None,
            effective_from="2026-01-07",
        ),
    )
    assert read(engine, book) == original
    with unit_of_work(engine) as con:
        settings = fees.history(
            con, OWNER, event.event_id, event.revision_id, 1, book["account_id"]
        )
        assert settings is not None
        assert fees.select(settings, date(2026, 1, 8)).selected == later
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"),
        unit_of_work(engine, write=True) as con,
    ):
        append(con, event, account, previous=third, **provenance(zero, zero, "ESTIMATED"))
    assert database_snapshot(engine) == before


def test_tax_and_fee_provenance_remain_independent_after_later_backdated_settings(
    engine: Engine, book: dict[str, Any]
) -> None:
    tax_setting = save_tax(engine, tax_intent(book, rate="0"))
    fee_setting = save_fee(engine, fee_intent(book, fee="0", fee_currency="USD"))
    with unit_of_work(engine, write=True) as con:
        append(
            con,
            book["event"],
            str(book["account_id"]),
            fee_micro=0,
            fee_original_micro=0,
            fee_original_currency="USD",
            fee_rate_event_e12=1_000_000_000_000,
            fee_rate_source="IDENTITY",
            expected_net_micro=1_000_000,
            rounding_delta_micro=0,
            expected_hkd_micro=7_800_000,
            **tax_provenance(tax_setting, tax_setting, "ESTIMATED"),
            **provenance(fee_setting, fee_setting, "ESTIMATED"),
        )
    original = read(engine, book)
    assert original is not None
    assert original[0].tax_provenance.selected_setting_id == tax_setting.id
    assert original[0].fee_provenance.selected_setting_id == fee_setting.id
    save_tax(engine, tax_intent(book, expected_setting_no=1, rate=None))
    save_fee(
        engine,
        fee_intent(
            book,
            expected_setting_no=1,
            expected_head_id=fee_setting.id,
            fee_status="UNKNOWN",
            fee=None,
            fee_currency=None,
        ),
    )
    assert read(engine, book) == original


def test_fee_provenance_is_bound_into_the_projection_audit_fingerprint(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0.000001", fee_currency="HKD"))
    with unit_of_work(engine, write=True) as con:
        row = append(
            con, book["event"], str(book["account_id"]), **provenance(setting, setting, "ESTIMATED")
        )
    payload = _payload(row, 1, REASON)
    original = digest(canonical(payload))
    for field, value in {
        "fee_effective_on": "2026-01-09",
        "fee_observed_head_no": 2,
        "fee_observed_head_id": str(uuid4()),
        "fee_selected_setting_id": str(uuid4()),
        "fee_estimate_status": "UNKNOWN",
    }.items():
        assert digest(canonical(_payload({**row, field: value}, 1, REASON))) != original
    with pytest.raises(ValueError):
        _payload({**row, "fee_provenance_version": 2}, 1, REASON)
    with pytest.raises(ValueError):
        _payload({**row, "fee_provenance_version": None}, 1, REASON)
    assert REASON not in canonical(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"fee_provenance_version": 2},
        {"fee_provenance_version": None},
        {"fee_effective_on": "2025-01-01"},
        {"fee_effective_on": "not-a-date"},
        {"fee_observed_head_no": 2},
        {"fee_observed_head_no": True},
        {"fee_observed_head_no": 0, "fee_observed_head_id": None},
        {"fee_observed_head_id": str(uuid4())},
        {"fee_selected_setting_id": str(uuid4())},
        {"fee_selected_setting_id": None},
        {"fee_estimate_status": "UNKNOWN"},
        {"fee_original_micro": 2},
    ],
)
def test_matching_projection_audit_does_not_mask_inconsistent_fee_history(
    engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0.000001", fee_currency="HKD"))
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        append(con, event, account, **provenance(setting, setting, "ESTIMATED"))
    before = database_snapshot(engine)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read_with_simulated_projection(engine, event, account, changes)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"fee_provenance_version": None},
        {"fee_effective_on": None},
        {"fee_effective_on": "2026-02-30"},
        {"fee_effective_on": "2025-01-01"},
        {"fee_observed_head_no": None},
        {"fee_observed_head_no": 0, "fee_observed_head_id": None},
        {"fee_observed_head_no": 2},
        {"fee_observed_head_id": None},
        {"fee_observed_head_id": str(uuid4())},
        {"fee_selected_setting_id": None},
        {"fee_selected_setting_id": str(uuid4())},
        {"fee_estimate_status": "UNKNOWN"},
        {"fee_original_micro": 1},
    ],
)
def test_sql_rejects_partial_stale_or_mismatched_fee_scope(
    engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0", fee_currency="USD"))
    event, account = book["event"], str(book["account_id"])
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **provenance(setting, setting, "ESTIMATED"), **changes})
    assert database_snapshot(engine) == before


def test_equal_fee_assertions_still_require_the_exact_latest_effective_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save_fee(engine, fee_intent(book, fee="0", fee_currency="USD"))
    later = save_fee(
        engine,
        fee_intent(
            book, expected_setting_no=1, expected_head_id=first.id, fee="0", fee_currency="USD"
        ),
    )
    event, account = book["event"], str(book["account_id"])
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"),
        unit_of_work(engine, write=True) as con,
    ):
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **provenance(later, first, "ESTIMATED")})
    assert database_snapshot(engine) == before


def test_other_account_or_changed_broker_cannot_reuse_a_fee_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0", fee_currency="USD"))
    other_book = {**account_scope(engine), "event": book["event"]}
    other = save_fee(engine, fee_intent(other_book, fee="0", fee_currency="USD"))
    event, account = book["event"], str(book["account_id"])
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"),
        unit_of_work(engine, write=True) as con,
    ):
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **provenance(other, setting, "ESTIMATED")})
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE brokers SET version=version+1 WHERE id=?", (str(book["broker_id"]),)
        )
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_FEE"),
        unit_of_work(engine, write=True) as con,
    ):
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **provenance(setting, setting, "ESTIMATED")})
    assert database_snapshot(engine) == before


def test_explicit_zero_foreign_fee_needs_no_invented_fee_fx(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_fee(engine, fee_intent(book, fee="0", fee_currency="HKD"))
    with unit_of_work(engine, write=True) as con:
        append(
            con,
            book["event"],
            str(book["account_id"]),
            fee_micro=0,
            fee_original_micro=0,
            fee_original_currency="HKD",
            fee_rate_event_e12=None,
            fee_rate_source=None,
            expected_net_micro=1_000_000,
            rounding_delta_micro=0,
            expected_hkd_micro=7_800_000,
            **provenance(setting, setting, "ESTIMATED"),
        )
    rows = read(engine, book)
    assert rows is not None and rows[0].fee_original == 0 and rows[0].fee_rate_event is None
    assert rows[0].fee_provenance.status == "ESTIMATED"
