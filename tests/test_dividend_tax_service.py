"""Owner-estimated tax decisions, audited history and current-use fences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit
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
from tests.test_dividend_event_writer import (
    AT,
    candidate_intent,
    manual,
    next_intent,
    owned_account,
    submit,
)
from tests.test_dividend_projection_migration import snapshot_values
from tests.test_dividend_tax_migration import TABLE, setting_values


@pytest.fixture(autouse=True)
def private_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def book(engine: Engine, source: dict[str, Any]) -> dict[str, Any]:
    scope = account_scope(engine)
    event = manual()
    submit(engine, event)
    return {**scope, "event": event}


def account_scope(engine: Engine) -> dict[str, Any]:
    portfolio, account = owned_account(engine)
    with unit_of_work(engine) as con:
        scope = con.exec_driver_sql(
            "SELECT a.broker_id,a.version AS account_version,b.version AS broker_version "
            "FROM accounts a JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account,),
        ).one()
    return {
        "portfolio_id": UUID(portfolio),
        "account_id": UUID(account),
        "broker_id": UUID(scope.broker_id),
        "account_version": scope.account_version,
        "broker_version": scope.broker_version,
    }


def intent(book: dict[str, Any], **changes: Any) -> tax.SettingIntent:
    event = book["event"]
    return tax.SettingIntent.model_validate(
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
            "effective_from": "2026-01-01",
            "rate": "0.300000",
            "reason": REASON,
            **changes,
        }
    )


def save(
    engine: Engine,
    value: tax.SettingIntent,
    *,
    request: UUID | None = None,
    owner: UUID = OWNER,
) -> tax.TaxSetting:
    with unit_of_work(engine, write=True) as con:
        return tax.save(con, owner, value, request_id=request or uuid4())


def history(engine: Engine, book: dict[str, Any], owner: UUID = OWNER) -> Any:
    event = book["event"]
    with unit_of_work(engine) as con:
        return tax.history(
            con, owner, event.event_id, event.revision_id, event.revision_no, book["account_id"]
        )


def current(
    engine: Engine,
    book: dict[str, Any],
    day: date,
    *,
    head_no: int,
    head_id: UUID | None,
) -> tax.TaxSelection:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        return tax.require_current(
            con,
            OWNER,
            event.event_id,
            event.revision_id,
            event.revision_no,
            book["account_id"],
            effective_on=day,
            expected_setting_no=head_no,
            expected_head_id=head_id,
        )


def test_missing_unknown_and_explicit_zero_have_different_meanings(
    engine: Engine, book: dict[str, Any]
) -> None:
    day = date(2026, 1, 8)
    before = database_snapshot(engine)
    missing = current(engine, book, day, head_no=0, head_id=None)
    assert missing.status == "MISSING" and missing.selected is None
    assert missing.head_no == 0 and missing.head_id is None
    assert database_snapshot(engine) == before

    unknown = save(engine, intent(book, rate=None))
    assert unknown.tax_status == "UNKNOWN" and unknown.rate is None
    selected = current(engine, book, day, head_no=1, head_id=unknown.id)
    assert selected.status == "UNKNOWN" and selected.selected == unknown
    zero = save(engine, intent(book, expected_setting_no=1, rate="0"))
    assert zero.tax_status == "ESTIMATED" and zero.rate == "0.000000"
    selected = current(engine, book, day, head_no=2, head_id=zero.id)
    assert selected.status == "ESTIMATED" and selected.selected == zero
    assert selected.scope == "OWNER_ESTIMATE_ONLY"
    assert str(book["account_id"]) not in repr(selected) and "0.000000" not in repr(selected)


def test_effective_selection_pins_history_head_and_does_not_rewrite_old_snapshots(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book, rate="0.2", effective_from="2026-01-01"))
    before_later = history(engine, book)
    assert before_later is not None
    original = tax.select(before_later, date(2026, 3, 1))
    assert original.selected == first and original.head_id == first.id and original.head_no == 1
    future = save(
        engine,
        intent(book, expected_setting_no=1, rate="0.35", effective_from="2026-06-01"),
    )
    backdated = save(
        engine,
        intent(book, expected_setting_no=2, rate=None, effective_from="2026-02-01"),
    )
    same_date = save(
        engine,
        intent(book, expected_setting_no=3, rate="0.15", effective_from="2026-02-01"),
    )
    rows = history(engine, book)
    assert rows == (first, future, backdated, same_date)
    assert tax.select(rows, date(2025, 12, 31)).status == "MISSING"
    assert tax.select(rows, date(2026, 1, 8)).selected == first
    assert tax.select(rows[:3], date(2026, 3, 1)).status == "UNKNOWN"
    assert tax.select(rows, date(2026, 3, 1)).selected == same_date
    assert tax.select(rows, date(2026, 7, 1)).selected == future
    assert tax.select(rows[:1], date(2026, 3, 1)) == original
    assert tax.select(rows, date(2026, 3, 1)).head_id == same_date.id
    before = database_snapshot(engine)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SETTING_CHANGED$"):
        current(engine, book, date(2026, 3, 1), head_no=1, head_id=first.id)
    assert database_snapshot(engine) == before


def test_audited_write_invalidates_one_portfolio_and_never_posts_financial_facts(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    request = uuid4()
    setting = save(engine, intent(book, rate="0.123456"), request=request)
    after = database_snapshot(engine)
    allowed = {TABLE, "audit_events", "portfolios", "projection_invalidations"}
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
    with unit_of_work(engine) as con:
        revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (str(book["portfolio_id"]),)
        ).scalar_one()
        marker = con.exec_driver_sql(
            "SELECT from_date,reason,source_id FROM projection_invalidations WHERE source_id=?",
            (str(setting.id),),
        ).one()
        assert revision == 1
        assert tuple(marker) == ("2026-01-01", "DIVIDEND_TAX_SETTING_CHANGED", str(setting.id))
    assert setting.rate is not None
    assert REASON not in repr(setting) and setting.rate not in repr(setting)
    assert history(engine, book) == (setting,)


def test_same_request_replays_historically_without_new_writes_after_changes(
    engine: Engine, book: dict[str, Any]
) -> None:
    first_intent, request = intent(book), uuid4()
    first = save(engine, first_intent, request=request)
    second = save(engine, intent(book, expected_setting_no=1, rate="0.12"))
    event2 = next_intent(book["event"], "VERIFY")
    submit(engine, event2, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert save(engine, first_intent, request=request) == first
    assert history(engine, book) == (first, second)
    assert database_snapshot(engine) == before
    changed_book = {**book, "event": event2}
    assert history(engine, changed_book) == ()
    missing = current(engine, changed_book, date(2026, 1, 8), head_no=0, head_id=None)
    assert missing.status == "MISSING" and missing.selected is None
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        current(engine, book, date(2026, 1, 8), head_no=2, head_id=second.id)
    assert database_snapshot(engine) == before


def test_old_receipts_locked_fx_and_existing_projection_are_not_rewritten(
    engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(event.revision_id), "currency": "USD"}, str(book["account_id"])
        )
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
        snapshot = snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            str(book["account_id"]),
        )
        insert(con, "dividend_projection_versions", snapshot)
    before = database_snapshot(engine)
    setting = save(engine, intent(book))
    after = database_snapshot(engine)
    for table in (
        "dividend_entitlements",
        "dividend_receipts",
        "cash_ledger_entries",
        "dividend_projection_versions",
        "dividend_event_revisions",
    ):
        assert after[table] == before[table]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
            ).scalar_one()
            == 7_800_000_000_000
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations WHERE source_id=?",
                (str(setting.id),),
            ).scalar_one()
            == 1
        )


def test_account_or_broker_changes_do_not_silently_reuse_an_old_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE brokers SET dividend_fee_micro=1,version=version+1 WHERE id=?",
            (str(book["broker_id"]),),
        )
    before = database_snapshot(engine)
    assert history(engine, book) == (first,)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SCOPE_CHANGED$"):
        current(engine, book, date(2026, 1, 8), head_no=1, head_id=first.id)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SCOPE_CHANGED$"):
        save(engine, intent(book, expected_setting_no=1))
    assert database_snapshot(engine) == before
    rechecked = {**book, "broker_version": 2}
    second = save(engine, intent(rechecked, expected_setting_no=1))
    assert (
        current(engine, rechecked, date(2026, 1, 8), head_no=2, head_id=second.id).selected
        == second
    )


def test_changed_candidate_source_fences_current_use_but_not_historical_replay(
    engine: Engine, basis: Any
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    event = candidate_intent(candidate)
    submit(engine, event)
    scoped = {**account_scope(engine), "event": event}
    first_intent, request = intent(scoped), uuid4()
    setting = save(engine, first_intent, request=request)
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert history(engine, scoped) == (setting,)
    assert save(engine, first_intent, request=request) == setting
    with pytest.raises(tax.TaxSettingError, match="^CANDIDATE_VERSION_CONFLICT$"):
        current(engine, scoped, date(2026, 1, 8), head_no=1, head_id=setting.id)
    with pytest.raises(tax.TaxSettingError, match="^CANDIDATE_VERSION_CONFLICT$"):
        save(engine, intent(scoped, expected_setting_no=1))
    assert database_snapshot(engine) == before


def test_false_but_sql_shaped_audit_never_becomes_a_trusted_setting(
    engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], str(book["account_id"])
    with unit_of_work(engine, write=True) as con:
        row = setting_values(con, event, account)
        insert(con, TABLE, row)
    before = database_snapshot(engine)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        history(engine, book)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        save(engine, intent(book, expected_setting_no=1))
    assert database_snapshot(engine) == before


def test_late_duplicate_audit_invalidates_history_without_rewriting_existing_audit(
    engine: Engine, book: dict[str, Any]
) -> None:
    setting = save(engine, intent(book))
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_tax_setting",
            str(setting.id),
            "DIVIDEND_TAX_SETTING_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "unrelated duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        history(engine, book)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        current(engine, book, date(2026, 1, 8), head_no=1, head_id=setting.id)
    assert database_snapshot(engine) == before


def test_caught_invalidation_error_rolls_back_setting_and_audit_in_a_savepoint(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    original = invalidate_portfolio

    def fail_after_invalidation(con: Connection, *args: Any, **kwargs: Any) -> None:
        original(con, *args, **kwargs)
        raise ValueError("synthetic private failure")

    with (
        unit_of_work(engine, write=True) as con,
        patch(
            "spm_api.dividend_tax_service.invalidate_portfolio", side_effect=fail_after_invalidation
        ),
        pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"),
    ):
        tax.save(con, OWNER, intent(book), request_id=uuid4())
    assert database_snapshot(engine) == before


def test_outer_rollback_still_undoes_a_successful_setting_savepoint(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(RuntimeError, match="rollback"), unit_of_work(engine, write=True) as con:
        tax.save(con, OWNER, intent(book), request_id=uuid4())
        raise RuntimeError("rollback")
    assert database_snapshot(engine) == before


def test_concurrent_stale_decisions_append_one_setting_and_one_audit(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    values = [intent(book), intent(book, rate="0.2")]

    def attempt(value: tax.SettingIntent) -> str:
        try:
            save(engine, value)
            return "saved"
        except tax.TaxSettingError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, values))
    assert sorted(results) == ["DIVIDEND_TAX_VERSION_CONFLICT", "saved"]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1


def test_concurrent_same_request_replay_is_zero_write_after_first_commit(
    engine: Engine, book: dict[str, Any]
) -> None:
    value, request = intent(book), uuid4()
    before = database_snapshot(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(engine, value, request=request), range(2)))
    assert results[0] == results[1]
    after = database_snapshot(engine)
    assert len(after[TABLE]) == len(before[TABLE]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1


@pytest.mark.parametrize(
    "changes",
    [
        {"rate": 0.3},
        {"rate": True},
        {"rate": "0.0000001"},
        {"rate": "1.000001"},
        {"rate": "-0.1"},
        {"rate": "NaN"},
        {"effective_from": "2026-02-30"},
        {"effective_from": "2026-1-01"},
        {"expected_setting_no": True},
        {"expected_setting_no": -1},
        {"account_version": True},
        {"revision_no": 0},
        {"reason": "too short"},
        {"reason": "reason contains\x00null"},
    ],
)
def test_rate_dates_and_versions_are_exactly_validated(
    book: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        intent(book, **changes)


def test_writer_rejects_model_copy_bypasses_without_private_errors(
    engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    value = intent(book).model_copy(update={"rate": 0.3})
    with pytest.raises(tax.TaxSettingError, match="^INVALID_DIVIDEND_TAX_SCOPE$"):
        save(engine, value)
    assert database_snapshot(engine) == before


def test_current_use_needs_a_real_writer_active_owner_and_exact_pins(
    engine: Engine, book: dict[str, Any]
) -> None:
    value = intent(book)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        with pytest.raises(tax.TaxSettingError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"):
            tax.save(con, OWNER, value, request_id=uuid4())
        with pytest.raises(tax.TaxSettingError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"):
            tax.require_current(
                con,
                OWNER,
                value.event_id,
                value.revision_id,
                value.revision_no,
                value.account_id,
                effective_on=date(2026, 1, 8),
                expected_setting_no=0,
                expected_head_id=None,
            )
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_OWNER_UNAVAILABLE$"):
        save(engine, value, owner=uuid4())
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_NOT_FOUND$"):
        save(engine, value, owner=OTHER)
    assert database_snapshot(engine) == before


def test_no_private_configuration_or_upstream_http_is_needed(
    engine: Engine, book: dict[str, Any]
) -> None:
    with (
        patch.object(
            ProviderConfigurationStore, "load", side_effect=AssertionError("private file")
        ),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider HTTP")),
    ):
        setting = save(engine, intent(book))
        assert (
            current(engine, book, date(2026, 1, 8), head_no=1, head_id=setting.id).selected
            == setting
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("effective_from", "2025-12-31"),
        ("rate", "0.250000"),
        ("reason", "A different synthetic reason"),
        ("expected_setting_no", 1),
        ("account_version", 2),
        ("broker_version", 2),
        ("portfolio_id", uuid4()),
        ("broker_id", uuid4()),
    ],
)
def test_reused_identity_cannot_change_an_original_intent(
    engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    original, request = intent(book), uuid4()
    save(engine, original, request=request)
    before = database_snapshot(engine)
    changed = original.model_copy(update={field: value})
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_CONFLICT$"):
        save(engine, changed, request=request)
    assert database_snapshot(engine) == before


def test_reused_requests_cross_scope_ids_and_orphan_audits_fail_closed(
    engine: Engine, book: dict[str, Any]
) -> None:
    original, request = intent(book), uuid4()
    save(engine, original, request=request)
    other = {**book, **account_scope(engine)}
    orphan = intent(book, expected_setting_no=1)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_tax_setting",
            str(orphan.id),
            "DIVIDEND_TAX_SETTING_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "orphan"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    for value, chosen_request in (
        (original, uuid4()),
        (intent(book, expected_setting_no=1), request),
        (intent(other, id=original.id), uuid4()),
    ):
        with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_CONFLICT$"):
            save(engine, value, request=chosen_request)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        save(engine, orphan)
    assert database_snapshot(engine) == before


def test_missing_foreign_and_mismatched_history_scopes_are_not_zero_estimates(
    engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    _, foreign_account = owned_account(engine, OTHER)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert tax.history(con, OWNER, uuid4(), event.revision_id, 1, book["account_id"]) is None
        assert (
            tax.history(con, OTHER, event.event_id, event.revision_id, 1, book["account_id"])
            is None
        )
        assert (
            tax.history(con, OWNER, event.event_id, event.revision_id, 1, UUID(foreign_account))
            is None
        )
        assert tax.history(con, OWNER, event.event_id, uuid4(), 1, book["account_id"]) is None
        assert (
            tax.history(con, OWNER, event.event_id, event.revision_id, 2, book["account_id"])
            is None
        )
        invalid_versions: tuple[Any, ...] = (True, 0, -1, 2_147_483_648, "1")
        for invalid in invalid_versions:
            with pytest.raises(tax.TaxSettingError, match="^INVALID_DIVIDEND_TAX_SCOPE$"):
                tax.history(
                    con, OWNER, event.event_id, event.revision_id, invalid, book["account_id"]
                )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "effective_on,number,head",
    [
        ("2026-01-08", 0, None),
        (datetime(2026, 1, 8, tzinfo=UTC), 0, None),
        (date(2026, 1, 8), True, None),
        (date(2026, 1, 8), -1, None),
        (date(2026, 1, 8), 0, uuid4()),
        (date(2026, 1, 8), 1, None),
        (date(2026, 1, 8), 1, "private-invalid"),
    ],
)
def test_current_use_rejects_invalid_head_pins_without_leaking_or_writing(
    engine: Engine, book: dict[str, Any], effective_on: Any, number: Any, head: Any
) -> None:
    before = database_snapshot(engine)
    event = book["event"]
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(tax.TaxSettingError, match="^INVALID_DIVIDEND_TAX_SCOPE$") as error,
    ):
        tax.require_current(
            con,
            OWNER,
            event.event_id,
            event.revision_id,
            event.revision_no,
            book["account_id"],
            effective_on=effective_on,
            expected_setting_no=number,
            expected_head_id=head,
        )
    assert "private-invalid" not in str(error.value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field",
    [
        "previous_id",
        "id",
        "request_id",
        "audit_id",
        "event_id",
        "revision_id",
        "revision_no",
        "account_id",
        "owner_id",
        "setting_no",
        "created_at",
        "tax_status",
        "rate",
    ],
)
def test_selector_rejects_inconsistent_supplied_history(
    engine: Engine, book: dict[str, Any], field: str
) -> None:
    first = save(engine, intent(book))
    second = save(engine, intent(book, expected_setting_no=1))
    corruptions: dict[str, Any] = {
        "previous_id": uuid4(),
        "id": first.id,
        "request_id": first.request_id,
        "audit_id": first.audit_id,
        "event_id": uuid4(),
        "revision_id": uuid4(),
        "revision_no": 2,
        "account_id": uuid4(),
        "owner_id": uuid4(),
        "setting_no": 3,
        "created_at": first.created_at - timedelta(microseconds=1),
        "tax_status": "UNKNOWN",
        "rate": 0.3,
    }
    altered = second.model_copy(update={field: corruptions[field]})
    before = database_snapshot(engine)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        tax.select((first, altered), date(2026, 1, 8))
    assert database_snapshot(engine) == before


def test_selector_does_not_coerce_malformed_scope_or_nonrecords() -> None:
    invalid: Any = "private-invalid"
    with pytest.raises(tax.TaxSettingError, match="^INVALID_DIVIDEND_TAX_SCOPE$"):
        tax.select([], datetime(2026, 1, 8, tzinfo=UTC))
    with pytest.raises(tax.TaxSettingError, match="^INVALID_DIVIDEND_TAX_SCOPE$"):
        tax.select(invalid, date(2026, 1, 8))
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
        tax.select([invalid], date(2026, 1, 8))


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", str(OTHER)),
        ("retained_portfolio_owner", str(OTHER)),
        ("retained_broker_owner", str(OTHER)),
        ("account_version", 2),
        ("retained_broker_version", 0),
        ("created_at", "2000-01-01T00:00:00.000000Z"),
        ("rate_ppm", 0.3),
        ("effective_from", "2026-02-30"),
        ("previous_id", str(uuid4())),
        ("tax_status", "UNKNOWN"),
    ],
)
def test_reader_fails_closed_on_corrupt_retained_row_without_modifying_storage(
    engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    save(engine, intent(book))
    before = database_snapshot(engine)
    event = book["event"]
    with unit_of_work(engine) as con:
        execute = con.execute

        def corrupt(statement: Any, parameters: Any = None, **kwargs: Any) -> Any:
            result = execute(statement, parameters, **kwargs)
            if "SELECT t.*,p.owner_id AS retained_portfolio_owner" in str(statement):
                rows = [dict(row) for row in result.mappings()]
                assert len(rows) == 1
                rows[0][field] = value
                replacement = MagicMock()
                replacement.mappings.return_value = rows
                return replacement
            return result

        with (
            patch.object(con, "execute", side_effect=corrupt),
            pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"),
        ):
            tax.history(con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
    assert database_snapshot(engine) == before


def test_account_move_requires_a_new_explicit_decision_and_retains_old_history(
    engine: Engine, book: dict[str, Any]
) -> None:
    original = save(engine, intent(book))
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
    assert history(engine, book) == (original,)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SCOPE_CHANGED$"):
        current(engine, book, date(2026, 1, 8), head_no=1, head_id=original.id)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SCOPE_CHANGED$"):
        save(engine, intent(book, expected_setting_no=1))
    assert database_snapshot(engine) == before
    moved = {
        **book,
        "portfolio_id": destination["portfolio_id"],
        "broker_id": destination["broker_id"],
        "account_version": book["account_version"] + 1,
        "broker_version": destination["broker_version"],
    }
    replacement = save(engine, intent(moved, expected_setting_no=1))
    assert history(engine, moved) == (original, replacement)
    assert (
        current(engine, moved, date(2026, 1, 8), head_no=2, head_id=replacement.id).selected
        == replacement
    )


def test_current_setting_must_be_rechecked_after_same_transaction_change(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book))
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        args = (con, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
        day = date(2026, 1, 8)
        assert (
            tax.require_current(
                *args, effective_on=day, expected_setting_no=1, expected_head_id=first.id
            ).selected
            == first
        )
        second = tax.save(
            con, OWNER, intent(book, expected_setting_no=1, rate=None), request_id=uuid4()
        )
        with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SETTING_CHANGED$"):
            tax.require_current(
                *args, effective_on=day, expected_setting_no=1, expected_head_id=first.id
            )
        selected = tax.require_current(
            *args, effective_on=day, expected_setting_no=2, expected_head_id=second.id
        )
        assert selected.selected == second and selected.status == "UNKNOWN"
    assert history(engine, book) == (first, second)


def test_current_setting_rechecks_source_after_same_transaction_candidate_change(
    engine: Engine, basis: Any
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    event = candidate_intent(candidate)
    submit(engine, event)
    scoped = {**account_scope(engine), "event": event}
    setting = save(engine, intent(scoped))
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    with unit_of_work(engine, write=True) as con:
        args = (con, OWNER, event.event_id, event.revision_id, 1, scoped["account_id"])
        pins: dict[str, Any] = {
            "effective_on": date(2026, 1, 8),
            "expected_setting_no": 1,
            "expected_head_id": setting.id,
        }
        assert tax.require_current(*args, **pins).selected == setting
        candidates.save(con, OWNER, dismissed, at=AT + timedelta(seconds=1), request_id=uuid4())
        with pytest.raises(tax.TaxSettingError, match="^CANDIDATE_VERSION_CONFLICT$"):
            tax.require_current(*args, **pins)
    assert history(engine, scoped) == (setting,)


def test_history_uses_one_wal_snapshot_while_a_new_setting_commits(
    engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, intent(book))
    event = book["event"]
    with unit_of_work(engine) as reader:
        args = (reader, OWNER, event.event_id, event.revision_id, 1, book["account_id"])
        assert tax.history(*args) == (first,)
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(save, engine, intent(book, expected_setting_no=1)).result(
                timeout=10
            )
        assert tax.history(*args) == (first,)
    assert history(engine, book) == (first, second)
    with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_SETTING_CHANGED$"):
        current(engine, book, date(2026, 1, 8), head_no=1, head_id=first.id)


def test_database_owner_lookup_failure_is_fixed_and_private(
    engine: Engine, book: dict[str, Any]
) -> None:
    value = intent(book)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(con, "execute", side_effect=SQLAlchemyError("synthetic private failure")),
    ):
        with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
            tax.save(con, OWNER, value, request_id=uuid4())
        with pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"):
            tax.require_current(
                con,
                OWNER,
                value.event_id,
                value.revision_id,
                1,
                value.account_id,
                effective_on=date(2026, 1, 8),
                expected_setting_no=0,
                expected_head_id=None,
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["begin", "release"])
def test_savepoint_database_failures_are_private_and_do_not_commit_partial_writes(
    engine: Engine, book: dict[str, Any], phase: str
) -> None:
    before = database_snapshot(engine)
    failure: Exception | None = None
    with unit_of_work(engine, write=True) as con:
        target = con if phase == "begin" else engine.dialect
        method = "begin_nested" if phase == "begin" else "do_release_savepoint"
        with patch.object(target, method, side_effect=SQLAlchemyError("synthetic private failure")):
            try:
                tax.save(con, OWNER, intent(book), request_id=uuid4())
            except (tax.TaxSettingError, SQLAlchemyError) as error:
                failure = error
    assert database_snapshot(engine) == before
    assert isinstance(failure, tax.TaxSettingError)
    assert str(failure) == "DIVIDEND_TAX_HISTORY_UNAVAILABLE"


def test_unexpected_source_error_does_not_expose_private_details(
    engine: Engine, book: dict[str, Any]
) -> None:
    value = intent(book)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        patch(
            "spm_api.dividend_tax_service.event_writer.require_current_source",
            side_effect=events.EventError("synthetic private failure"),
        ),
        pytest.raises(tax.TaxSettingError, match="^DIVIDEND_TAX_HISTORY_UNAVAILABLE$"),
    ):
        tax.require_current(
            con,
            OWNER,
            value.event_id,
            value.revision_id,
            1,
            value.account_id,
            effective_on=date(2026, 1, 8),
            expected_setting_no=0,
            expected_head_id=None,
        )
    assert database_snapshot(engine) == before
