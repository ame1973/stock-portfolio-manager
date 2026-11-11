"""Projection tax provenance remains an explicit, audited historical estimate."""

from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_tax_service as tax
from spm_api.audit import audit, canonical, digest
from spm_api.db import unit_of_work
from spm_api.dividend_projection_arithmetic import _AMOUNT_FIELDS
from spm_api.dividend_projection_history import (
    ProjectionHistoryError,
    _payload,
    history,
)
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, submit, terms
from tests.test_dividend_projection_history import append, read_with_simulated_projection
from tests.test_dividend_projection_migration import TABLE, snapshot_values
from tests.test_dividend_tax_migration import TABLE as TAX_TABLE
from tests.test_dividend_tax_migration import setting_values
from tests.test_dividend_tax_service import account_scope
from tests.test_dividend_tax_service import intent as tax_intent
from tests.test_dividend_tax_service import save as save_tax


@pytest.fixture(autouse=True)
def private_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def book(engine: Engine, source: dict[str, Any]) -> dict[str, Any]:
    scope = account_scope(engine)
    event = manual(terms=terms(dps="1"))
    submit(engine, event)
    return {**scope, "event": event}


def provenance(
    head: tax.TaxSetting | None,
    selected: tax.TaxSetting | None,
    status: str,
    *,
    day: str = "2026-01-08",
) -> dict[str, Any]:
    return {
        "tax_provenance_version": 1,
        "tax_effective_on": day,
        "tax_observed_head_no": head.setting_no if head else 0,
        "tax_observed_head_id": str(head.id) if head else None,
        "tax_selected_setting_id": str(selected.id) if selected else None,
        "tax_estimate_status": status,
    }


def read(engine: Engine, book: dict[str, Any]) -> Any:
    with unit_of_work(engine) as con:
        return history(con, OWNER, book["event"].event_id, book["account_id"])


def test_unlinked_legacy_amount_does_not_become_a_tax_confirmation(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        append(con, event, account)
    rows = read(engine, book)
    assert rows is not None and rows[0].tax_rate == 0
    assert rows[0].tax_provenance is None
    assert rows[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"


def test_missing_with_an_empty_history_is_not_an_estimated_zero(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], str(book["account_id"])
    empty: dict[str, Any] = {name: None for name in _AMOUNT_FIELDS}
    with unit_of_work(engine, write=True) as con:
        append(
            con,
            event,
            account,
            state="NEEDS_REVIEW",
            **empty,
            **provenance(None, None, "MISSING"),
        )
    rows = read(engine, book)
    assert rows is not None and rows[0].tax_rate is None
    assert rows[0].tax_provenance.status == "MISSING"
    assert rows[0].tax_provenance.observed_head_no == 0
    assert rows[0].version.accrual is None


def test_missing_unknown_and_zero_keep_their_original_selected_history(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], str(book["account_id"])
    empty: dict[str, Any] = {name: None for name in _AMOUNT_FIELDS}
    future = save_tax(engine, tax_intent(book, effective_from="2026-02-01", rate="0.2"))
    with unit_of_work(engine, write=True) as con:
        first = append(
            con,
            event,
            account,
            state="NEEDS_REVIEW",
            **empty,
            **provenance(future, None, "MISSING"),
        )
    unknown = save_tax(
        engine, tax_intent(book, expected_setting_no=1, effective_from="2026-01-01", rate=None)
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
    zero = save_tax(
        engine, tax_intent(book, expected_setting_no=2, effective_from="2026-01-01", rate="0")
    )
    with unit_of_work(engine, write=True) as con:
        third = append(con, event, account, previous=second, **provenance(zero, zero, "ESTIMATED"))
    original = read(engine, book)
    assert original is not None
    assert [row.tax_provenance.status for row in original] == ["MISSING", "UNKNOWN", "ESTIMATED"]
    assert [row.tax_provenance.observed_head_no for row in original] == [1, 2, 3]
    assert original[0].tax_provenance.selected_setting_id is None
    assert original[1].tax_provenance.selected_setting_id == unknown.id
    assert original[2].tax_provenance.selected_setting_id == zero.id
    assert original[2].tax_rate == 0
    assert original[2].tax_provenance.scope == "OWNER_ESTIMATE_ONLY"
    assert str(zero.id) not in repr(original[2].tax_provenance)

    later = save_tax(
        engine, tax_intent(book, expected_setting_no=3, effective_from="2026-01-07", rate=None)
    )
    assert read(engine, book) == original
    with unit_of_work(engine) as con:
        settings = tax.history(con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
        assert settings is not None
        assert tax.select(settings, date(2026, 1, 8)).selected == later
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_TAX"),
        unit_of_work(engine, write=True) as con,
    ):
        append(con, event, account, previous=third, **provenance(zero, zero, "ESTIMATED"))
    assert database_snapshot(engine) == before


def test_explicit_provenance_is_bound_to_the_projection_fingerprint(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
    with unit_of_work(engine, write=True) as con:
        row = append(
            con, book["event"], str(book["account_id"]), **provenance(setting, setting, "ESTIMATED")
        )
    payload = _payload(row, 1, REASON)
    original = digest(canonical(payload))
    for field, value in {
        "tax_effective_on": "2026-01-09",
        "tax_observed_head_no": 2,
        "tax_observed_head_id": str(uuid4()),
        "tax_selected_setting_id": str(uuid4()),
        "tax_estimate_status": "UNKNOWN",
    }.items():
        assert digest(canonical(_payload({**row, field: value}, 1, REASON))) != original
    with pytest.raises(ValueError):
        _payload({**row, "tax_provenance_version": 2}, 1, REASON)
    with pytest.raises(ValueError):
        _payload({**row, "tax_provenance_version": None}, 1, REASON)
    assert REASON not in canonical(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"tax_provenance_version": 2},
        {"tax_provenance_version": None},
        {"tax_effective_on": "2025-01-01"},
        {"tax_effective_on": "not-a-date"},
        {"tax_observed_head_no": 2},
        {"tax_observed_head_no": True},
        {"tax_observed_head_no": 0, "tax_observed_head_id": None},
        {"tax_observed_head_id": str(uuid4())},
        {"tax_selected_setting_id": str(uuid4())},
        {"tax_selected_setting_id": None},
        {"tax_estimate_status": "UNKNOWN"},
    ],
)
def test_matching_projection_audit_does_not_mask_inconsistent_tax_history(
    engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
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
        {"tax_provenance_version": None},
        {"tax_effective_on": None},
        {"tax_effective_on": "2026-02-30"},
        {"tax_effective_on": "2025-01-01"},
        {"tax_observed_head_no": None},
        {"tax_observed_head_no": 0, "tax_observed_head_id": None},
        {"tax_observed_head_no": 2},
        {"tax_observed_head_id": None},
        {"tax_observed_head_id": str(uuid4())},
        {"tax_selected_setting_id": None},
        {"tax_selected_setting_id": str(uuid4())},
        {"tax_estimate_status": "UNKNOWN"},
        {"tax_rate_snapshot_ppm": 1},
    ],
)
def test_sql_rejects_partial_stale_or_mismatched_tax_scope(
    engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
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


def test_same_effective_day_selects_latest_setting_not_an_older_equal_rate(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save_tax(engine, tax_intent(book, rate="0"))
    later = save_tax(engine, tax_intent(book, expected_setting_no=1, rate="0"))
    event, account = book["event"], str(book["account_id"])
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_TAX"),
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
    with unit_of_work(engine, write=True) as con:
        append(con, event, account, **provenance(later, later, "ESTIMATED"))
    rows = read(engine, book)
    assert rows is not None and rows[0].tax_provenance.selected_setting_id == later.id


def test_unknown_setting_masks_old_estimate_even_when_the_old_rate_matches(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save_tax(engine, tax_intent(book, rate="0"))
    unknown = save_tax(engine, tax_intent(book, expected_setting_no=1, rate=None))
    event, account = book["event"], str(book["account_id"])
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_TAX"),
        unit_of_work(engine, write=True) as con,
    ):
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **provenance(unknown, first, "ESTIMATED")})
    assert database_snapshot(engine) == before


def test_other_account_setting_cannot_be_used_as_a_head_or_selection(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
    other_book = {**account_scope(engine), "event": book["event"]}
    other = save_tax(engine, tax_intent(other_book, rate="0"))
    event, account = book["event"], str(book["account_id"])
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_TAX"),
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


def test_changed_broker_version_cannot_use_an_old_setting_for_a_new_projection(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE brokers SET version=version+1 WHERE id=?", (str(book["broker_id"]),)
        )
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_TAX"),
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


def test_projection_cannot_predate_observed_tax_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        setting = setting_values(con, event, account, rate_ppm=0)
        insert(con, TAX_TABLE, setting)
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_DIVIDEND_PROJECTION_TAX"),
        unit_of_work(engine, write=True) as con,
    ):
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        metadata = {
            "tax_provenance_version": 1,
            "tax_effective_on": "2026-01-08",
            "tax_observed_head_no": 1,
            "tax_observed_head_id": setting["id"],
            "tax_selected_setting_id": setting["id"],
            "tax_estimate_status": "ESTIMATED",
        }
        insert(con, TABLE, {**row, **metadata})
    assert database_snapshot(engine) == before


def test_sql_tax_shape_is_not_an_authenticated_projection(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        row = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, {**row, **provenance(setting, setting, "ESTIMATED")})
    before = database_snapshot(engine)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, book)
    assert database_snapshot(engine) == before


def test_later_duplicate_tax_audit_fails_the_whole_historical_read(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        append(con, event, account, **provenance(setting, setting, "ESTIMATED"))
    assert read(engine, book) is not None
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_tax_setting",
            str(setting.id),
            "SYNTHETIC_DUPLICATE",
            request_id=str(uuid4()),
            after={"synthetic": "unrelated"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, book)
    assert database_snapshot(engine) == before


def test_read_snapshot_and_historical_selection_survive_later_backdated_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save_tax(engine, tax_intent(book, rate="0"))
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        append(con, event, account, **provenance(setting, setting, "ESTIMATED"))
    with unit_of_work(engine) as con:
        first = history(con, OWNER, event.event_id, book["account_id"])
        save_tax(
            engine, tax_intent(book, expected_setting_no=1, effective_from="2026-01-02", rate="0.5")
        )
        assert history(con, OWNER, event.event_id, book["account_id"]) == first
    assert read(engine, book) == first
