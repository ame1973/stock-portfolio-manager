"""Combine direct and legacy receipt histories without fabricating entitlements."""

from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_receipt_audit as legacy_audit
import spm_api.dividend_receipt_history as legacy
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_history import add_receipt, void_receipt
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_direct_receipt_migration import scenario as scenario
from tests.test_dividend_event_migration import OTHER, OWNER, add_posted_receipt
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_receipt_history import claim


def add_legacy(engine: Engine, scenario: tuple[Any, str]) -> dict[str, Any]:
    event, account = scenario
    entitlement = claim(event.revision_id, account, net=10_000_000)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
        receipt = add_posted_receipt(con, entitlement, fx=7_800_000_000_000)
    return {"receipt": receipt, "entitlement": entitlement}


def read(engine: Engine, scenario: tuple[Any, str]) -> Any:
    event, account = scenario
    with unit_of_work(engine) as con:
        return combined.history(con, OWNER, event.event_id, UUID(account))


def test_combines_a_legacy_receipt_and_a_direct_receipt_without_inventing_a_claim(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    older = add_legacy(engine, scenario)
    newer = add_receipt(engine, scenario, received_micro=2_000_000)
    before = database_snapshot(engine)
    values = read(engine, scenario)
    assert values is not None and len(values) == 2
    by_id = {item.receipt.id: item for item in values}
    old = by_id[UUID(older["receipt"]["id"])]
    new = by_id[UUID(newer["id"])]
    assert isinstance(old, legacy_audit.AuditedReceipt)
    assert old.entitlement_id == UUID(older["entitlement"]["id"])
    assert old.audit_scope == "RECEIPT_AUDIT_NOT_VERIFIED"
    assert isinstance(new, direct.RecordedDirectReceipt)
    assert not hasattr(new, "entitlement_id")
    assert new.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert old.eligibility_scope == new.eligibility_scope == "NOT_ESTABLISHED"
    assert str(old.receipt.received) == "6.000000" and str(new.receipt.received) == "2.000000"
    assert "6.000000" not in repr(values) and "synthetic-private" not in repr(values)
    assert database_snapshot(engine) == before
    event, account = scenario
    with (
        unit_of_work(engine) as con,
        pytest.raises(legacy.ReceiptHistoryError, match="^DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE$"),
    ):
        legacy.history(con, OWNER, event.event_id, UUID(account))


def test_empty_and_foreign_scope_remain_distinct_from_confirmed_zero(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    before = database_snapshot(engine)
    assert read(engine, scenario) == ()
    event, account = scenario
    with unit_of_work(engine) as con:
        assert combined.history(con, OTHER, event.event_id, UUID(account)) is None
        assert combined.history(con, OWNER, event.event_id, uuid4()) is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["unsealed", "forged"])
def test_bad_direct_history_does_not_return_a_partial_legacy_history(
    engine: Engine, scenario: tuple[Any, str], kind: str
) -> None:
    add_legacy(engine, scenario)
    if kind == "unsealed":
        with unit_of_work(engine, write=True) as con:
            insert(con, TABLE, receipt_values(*scenario))
    else:
        add_receipt(engine, scenario, forged_audit=True)
    before = database_snapshot(engine)
    with pytest.raises(
        combined.CombinedReceiptHistoryError, match="^COMBINED_RECEIPT_HISTORY_UNAVAILABLE$"
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def test_void_direct_receipt_remains_separate_from_active_legacy_receipt(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    older = add_legacy(engine, scenario)
    newer = add_receipt(engine, scenario)
    void_receipt(engine, newer)
    before = database_snapshot(engine)
    values = read(engine, scenario)
    assert values is not None and len(values) == 2
    by_id = {item.receipt.id: item for item in values}
    assert by_id[UUID(older["receipt"]["id"])].receipt.status == "ACTIVE"
    assert by_id[UUID(newer["id"])].receipt.status == "VOID"
    assert (
        by_id[UUID(newer["id"])].receipt.cash_scope == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    )
    assert database_snapshot(engine) == before


def test_combined_read_uses_one_wal_snapshot_across_both_stores(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    entitlement = claim(event.revision_id, account, net=10_000_000)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
    read_direct = direct.history
    appended = False

    def append_after_direct(*args: Any, **kwargs: Any) -> Any:
        nonlocal appended
        result = read_direct(*args, **kwargs)
        if not appended:
            appended = True
            with unit_of_work(engine, write=True) as con:
                add_posted_receipt(con, entitlement)
        return result

    with patch.object(direct, "history", side_effect=append_after_direct):
        assert read(engine, scenario) == ()
    values = read(engine, scenario)
    assert values is not None and len(values) == 1
    assert isinstance(values[0], legacy_audit.AuditedReceipt)


def test_duplicate_identity_cannot_be_double_counted(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    older = add_legacy(engine, scenario)
    add_receipt(engine, scenario)
    event, account = scenario
    with unit_of_work(engine) as con:
        direct_items = direct.history(con, OWNER, event.event_id, UUID(account))
    assert direct_items is not None and len(direct_items) == 1
    duplicate = replace(
        direct_items[0], receipt=replace(direct_items[0].receipt, id=UUID(older["receipt"]["id"]))
    )
    before = database_snapshot(engine)
    with (
        patch.object(direct, "history", return_value=(duplicate,)),
        pytest.raises(
            combined.CombinedReceiptHistoryError, match="^COMBINED_RECEIPT_HISTORY_UNAVAILABLE$"
        ),
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def test_scope_validation_requires_a_transaction_and_uuid_pins(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    event, account = scenario
    with (
        engine.connect() as con,
        pytest.raises(
            combined.CombinedReceiptHistoryError, match="^INVALID_COMBINED_RECEIPT_SCOPE$"
        ),
    ):
        combined.history(con, OWNER, event.event_id, UUID(account))
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            combined.CombinedReceiptHistoryError, match="^INVALID_COMBINED_RECEIPT_SCOPE$"
        ),
    ):
        combined.history(con, OWNER, event.event_id, "invalid")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind", ["missing_legacy_scope", "invalid_direct", "invalid_legacy", "legacy_error"]
)
def test_inconsistent_independent_reads_do_not_return_partial_results(
    engine: Engine, scenario: tuple[Any, str], kind: str
) -> None:
    before = database_snapshot(engine)
    target: Any
    options: dict[str, Any]
    if kind == "missing_legacy_scope":
        target, attribute, options = legacy_audit, "_history", {"return_value": None}
    elif kind == "invalid_direct":
        target, attribute, options = direct, "history", {"return_value": (object(),)}
    elif kind == "invalid_legacy":
        target, attribute, options = legacy_audit, "_history", {"return_value": (object(),)}
    else:
        target, attribute, options = (
            legacy_audit,
            "_history",
            {"side_effect": legacy_audit.ReceiptAuditError("synthetic-private")},
        )
    with (
        patch.object(target, attribute, **options),
        pytest.raises(
            combined.CombinedReceiptHistoryError, match="^COMBINED_RECEIPT_HISTORY_UNAVAILABLE$"
        ),
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before


def test_histories_are_sorted_by_recorded_date_not_storage_type(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    older = add_legacy(engine, scenario)
    earlier = add_receipt(engine, scenario, received_date="2026-01-08")
    values = read(engine, scenario)
    assert values is not None
    assert [item.receipt.id for item in values] == [
        UUID(earlier["id"]),
        UUID(older["receipt"]["id"]),
    ]


def test_invalid_receipt_identifier_is_not_treated_as_a_distinct_fact(
    engine: Engine, scenario: tuple[Any, str]
) -> None:
    add_receipt(engine, scenario)
    event, account = scenario
    with unit_of_work(engine) as con:
        values = direct.history(con, OWNER, event.event_id, UUID(account))
    assert values is not None and len(values) == 1
    invalid = replace(
        values[0],
        receipt=replace(values[0].receipt, id="invalid"),  # type: ignore[arg-type]
    )
    before = database_snapshot(engine)
    with (
        patch.object(direct, "history", return_value=(invalid,)),
        pytest.raises(
            combined.CombinedReceiptHistoryError, match="^COMBINED_RECEIPT_HISTORY_UNAVAILABLE$"
        ),
    ):
        read(engine, scenario)
    assert database_snapshot(engine) == before
