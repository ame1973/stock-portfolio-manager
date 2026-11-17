"""Receipt audit fingerprints describe retained facts, not a financial or session grant."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import localcontext
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_event_service as event_service
import spm_api.dividend_receipt_audit as receipts
import spm_api.dividend_receipt_history as retained_history
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.ledger_service import post_cash, reverse_cash
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, owned_account, submit
from tests.test_dividend_receipt_history import claim, prepared
from tests.test_dividend_receipt_origin_migration import TABLE


@pytest.fixture
def scope(engine: Engine, source: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    _, account = owned_account(engine)
    event = manual()
    submit(engine, event)
    entitlement = claim(event.revision_id, account, net=10_000_000)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", entitlement)
    return event, account, entitlement


def record(
    con: Connection,
    scope: tuple[Any, str, dict[str, Any]],
    *,
    currency: str = "USD",
    amount: int = 6_000_000,
    rate: int | None = 7_800_000_000_000,
    booked: int | None = 46_800_000,
    fx_source: str | None = "Synthetic private FX source",
    actual_hkd_reason: str | None = None,
    receipt_note: str = "Synthetic private receipt note",
    cash_note: str = "",
    forged_audit: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    event, account, entitlement = scope
    row = con.exec_driver_sql(
        "SELECT a.portfolio_id,a.broker_id,a.version,p.version,b.version,p.data_revision "
        "FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id "
        "JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
        (account,),
    ).one()
    receipt = {
        "id": str(uuid4()),
        "entitlement_id": entitlement["id"],
        "account_id": account,
        "received_date": "2026-01-09",
        "received_micro": amount,
        "currency": currency,
        "tax_micro": None,
        "fee_micro": 0,
        "fx_rate_hkd_e12": rate,
        "received_hkd_micro": booked,
        "note": receipt_note,
        "source": "MANUAL",
        "created_at": stamp(),
    }
    insert(con, "dividend_receipts", receipt)
    cash_id = post_cash(
        con,
        account=account,
        day=receipt["received_date"],
        currency=currency,  # type: ignore[arg-type]
        amount=receipt["received_micro"],
        fx=rate,
        booked=booked,
        kind="DIVIDEND_RECEIPT",
        source_type="DIVIDEND_RECEIPT",
        source_id=receipt["id"],
        note=cash_note,
    )
    cash = dict(
        con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (cash_id,))
        .mappings()
        .one()
    )
    created, request = datetime.now(UTC), str(uuid4())
    origin = {
        "receipt_id": receipt["id"],
        "owner_id": str(OWNER),
        "event_id": str(event.event_id),
        "observed_revision_id": str(event.revision_id),
        "observed_revision_no": event.revision_no,
        "account_id": account,
        "portfolio_id": row[0],
        "broker_id": row[1],
        "account_version": row[2],
        "portfolio_version": row[3],
        "broker_version": row[4],
        "portfolio_data_revision": row[5],
        "cash_entry_id": cash_id,
        "fx_source": fx_source,
        "actual_hkd_reason": actual_hkd_reason,
        "request_id": request,
        "created_at": stamp(created),
    }
    creation = receipts._creation_payload(receipt, origin, cash, "")
    audit(
        con,
        str(OWNER),
        "dividend_receipt",
        receipt["id"],
        "DIVIDEND_RECEIPT_RECORDED",
        request_id=request,
        after={"synthetic": "wrong fingerprint"} if forged_audit else creation,
        at=created,
    )
    origin["audit_id"] = con.exec_driver_sql(
        "SELECT id FROM audit_events WHERE entity_type='dividend_receipt' AND entity_id=?",
        (receipt["id"],),
    ).scalar_one()
    insert(con, TABLE, origin)
    return receipt, origin, cash


def void(
    con: Connection,
    receipt: dict[str, Any],
    origin: dict[str, Any],
    cash: dict[str, Any],
    *,
    forged_audit: bool = False,
    forged_before: bool = False,
    audit_at: datetime | None = None,
    voided_at: datetime | None = None,
) -> None:
    primary = (
        con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (cash["id"],))
        .mappings()
        .one()
    )
    reversal_id = reverse_cash(con, primary, reason=REASON)
    reversal = dict(
        con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (reversal_id,))
        .mappings()
        .one()
    )
    created, request = audit_at or datetime.now(UTC), str(uuid4())
    voided_time = stamp(voided_at or created)
    after = receipts._void_payload(receipt, origin, reversal, request, REASON, voided_time)
    audit(
        con,
        str(OWNER),
        "dividend_receipt",
        receipt["id"],
        "DIVIDEND_RECEIPT_VOIDED",
        request_id=request,
        before={"synthetic": "wrong original"}
        if forged_before
        else receipts._creation_payload(receipt, origin, cash, ""),
        after={"synthetic": "wrong void"} if forged_audit else after,
        reason=REASON,
        at=created,
    )
    con.exec_driver_sql(
        "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
        (voided_time, receipt["id"]),
    )


@pytest.mark.parametrize(
    ("currency", "rate", "booked", "source_name", "override_reason"),
    [
        ("USD", None, None, None, None),
        ("USD", 7_800_000_000_000, 46_800_000, "Synthetic rate", None),
        ("USD", 7_800_000_000_000, 46_123_456, "Synthetic rate", REASON),
        ("HKD", 1_000_000_000_000, 6_000_000, "IDENTITY", None),
    ],
)
def test_audited_origin_rechecks_exact_cash_and_preserves_unknowns(
    engine: Engine,
    scope: tuple[Any, str, dict[str, Any]],
    currency: str,
    rate: int | None,
    booked: int | None,
    source_name: str | None,
    override_reason: str | None,
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        receipt, origin, _ = record(
            con,
            scope,
            currency=currency,
            rate=rate,
            booked=booked,
            fx_source=source_name,
            actual_hkd_reason=override_reason,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and len(result) == 1
    item = result[0]
    assert item.receipt.id == UUID(receipt["id"])
    assert item.entitlement_id == UUID(receipt["entitlement_id"])
    assert item.origin is not None and item.origin.fx_source == source_name
    assert item.origin.actual_hkd_reason == override_reason
    assert item.origin.request_id == UUID(origin["request_id"])
    assert item.audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert item.eligibility_scope == "NOT_ESTABLISHED"
    assert "Synthetic private" not in repr(item) and "6.000000" not in repr(item)
    assert database_snapshot(engine) == before


def test_creation_fingerprint_binds_every_stored_input_without_relabeling_on_void(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt, origin, cash = record(con, scope)
    payload = receipts._creation_payload(receipt, origin, cash, "")
    expected = digest(canonical(payload))
    fields = {
        "receipt": (
            "id",
            "entitlement_id",
            "account_id",
            "received_date",
            "received_micro",
            "currency",
            "tax_micro",
            "fee_micro",
            "fx_rate_hkd_e12",
            "received_hkd_micro",
            "note",
            "source",
            "created_at",
        ),
        "origin": (
            "receipt_id",
            "owner_id",
            "event_id",
            "observed_revision_id",
            "observed_revision_no",
            "account_id",
            "portfolio_id",
            "broker_id",
            "account_version",
            "portfolio_version",
            "broker_version",
            "portfolio_data_revision",
            "cash_entry_id",
            "fx_source",
            "actual_hkd_reason",
            "request_id",
            "created_at",
        ),
        "cash": (
            "id",
            "account_id",
            "effective_date",
            "currency",
            "amount_micro",
            "fx_rate_hkd_e12",
            "booked_hkd_micro",
            "kind",
            "source_type",
            "source_id",
            "entry_role",
            "external_flow",
            "note",
            "created_at",
            "reverses_entry_id",
        ),
    }
    for name, values in fields.items():
        for key in values:
            parts = {"receipt": dict(receipt), "origin": dict(origin), "cash": dict(cash)}
            parts[name][key] = "synthetic-change"
            changed = receipts._creation_payload(
                parts["receipt"], parts["origin"], parts["cash"], ""
            )
            assert digest(canonical(changed)) != expected, (name, key)
    assert digest(canonical(receipts._creation_payload(receipt, origin, cash, REASON))) != expected
    retired = {**receipt, "status": "VOID", "version": 2, "voided_at": stamp()}
    reversed_cash = {**cash, "version": 2}
    assert receipts._creation_payload(retired, origin, reversed_cash, "") == payload
    assert (
        receipts._creation_payload(receipt, {**origin, "audit_id": str(uuid4())}, cash, "")
        == payload
    )


def test_recorded_receipt_audit_payloads_have_stable_golden_fingerprints() -> None:
    receipt_fields = [
        "id",
        "entitlement_id",
        "account_id",
        "received_date",
        "received_micro",
        "currency",
        "tax_micro",
        "fee_micro",
        "fx_rate_hkd_e12",
        "received_hkd_micro",
        "note",
        "source",
        "created_at",
    ]
    origin_fields = [
        "receipt_id",
        "owner_id",
        "event_id",
        "observed_revision_id",
        "observed_revision_no",
        "account_id",
        "portfolio_id",
        "broker_id",
        "account_version",
        "portfolio_version",
        "broker_version",
        "portfolio_data_revision",
        "cash_entry_id",
        "fx_source",
        "actual_hkd_reason",
        "request_id",
        "created_at",
    ]
    cash_fields = [
        "id",
        "account_id",
        "effective_date",
        "currency",
        "amount_micro",
        "fx_rate_hkd_e12",
        "booked_hkd_micro",
        "kind",
        "source_type",
        "source_id",
        "entry_role",
        "external_flow",
        "note",
        "created_at",
        "reverses_entry_id",
    ]
    receipt = {name: f"r:{name}" for name in receipt_fields}
    origin = {name: f"o:{name}" for name in origin_fields}
    cash = {name: f"c:{name}" for name in cash_fields}
    creation = receipts._creation_payload(receipt, origin, cash, "Synthetic receipt reason")
    assert (
        digest(canonical(creation))
        == "a75bba30b2d533c85257608ac6912b212aca0871f256669273ac112ab4f6e828"
    )
    reversal = receipts._void_payload(
        receipt,
        origin,
        cash,
        "request-id",
        "Synthetic receipt reason",
        "2026-01-09T00:00:00.000000Z",
    )
    assert (
        digest(canonical(reversal))
        == "cd415f212fdf893721b3e307a64855c5b0a235662871190e4fe3097e82b6c3b6"
    )


@pytest.mark.parametrize("reason", ["short", " invalid reason ", "\x00private value"])
def test_creation_fingerprint_rejects_invalid_reason_without_echoing_it(reason: str) -> None:
    with pytest.raises(ValueError, match="^Invalid receipt audit reason$"):
        receipts._creation_payload({}, {}, {}, reason)


def test_legacy_receipt_without_origin_stays_explicitly_unverified(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, entitlement = scope
    with unit_of_work(engine, write=True) as con:
        receipt = {
            "id": str(uuid4()),
            "entitlement_id": entitlement["id"],
            "account_id": account,
            "received_date": "2026-01-09",
            "received_micro": 6_000_000,
            "currency": "USD",
            "created_at": stamp(),
        }
        insert(con, "dividend_receipts", receipt)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and len(result) == 1
    assert result[0].origin is None and result[0].audit_scope == "RECEIPT_AUDIT_NOT_VERIFIED"
    assert result[0].receipt.cash_scope == "CASH_NOT_RECORDED"
    assert database_snapshot(engine) == before


def test_sql_audit_shape_with_wrong_fingerprint_is_not_adopted(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope, forged_audit=True)
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("action", ["DIVIDEND_RECEIPT_RECORDED", "SYNTHETIC_OTHER"])
def test_later_duplicate_or_unknown_receipt_audit_does_not_silently_extend_history(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], action: str
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        receipt, _, _ = record(con, scope)
        audit(
            con,
            str(OWNER),
            "dividend_receipt",
            receipt["id"],
            action,
            request_id=str(uuid4()),
            after={"synthetic": True},
        )
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_matching_audit_cannot_hide_wrong_hkd_arithmetic(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope, booked=46_000_000)
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"fx_source": " Synthetic rate"},
        {"actual_hkd_reason": " Synthetic actual HKD reason ", "booked": 46_123_456},
        {"receipt_note": "synthetic\x00private"},
        {"receipt_note": "x" * 1001},
        {"cash_note": "synthetic\x00private"},
        {"cash_note": "x" * 1001},
    ],
)
def test_private_noncanonical_inputs_fail_closed_even_with_a_matching_audit(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], changes: dict[str, Any]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope, **changes)
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", [True, -1, 1.5, "1", INT64_MAX + 1, None])
def test_untrusted_audit_ordinals_require_real_int64_integers(value: object) -> None:
    with pytest.raises(ValueError, match="^Invalid receipt audit ordinal$"):
        receipts._integer(value)


@pytest.mark.parametrize("value", [True, 1.5, "1", [], {}])
def test_untrusted_scaled_values_do_not_coerce_to_integers(value: object) -> None:
    with pytest.raises(ValueError, match="^Invalid scaled receipt value$"):
        receipts._optional_scaled(value, 6)


@pytest.mark.parametrize(("amount", "booked"), [(1, 1), (-1, -1)])
def test_recorded_half_micro_fx_is_exact_despite_ambient_decimal_precision(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], amount: int, booked: int
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope, amount=amount, rate=500_000_000_000, booked=booked)
    with localcontext() as context, unit_of_work(engine) as con:
        context.prec = 3
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and result[0].audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"


def test_same_sign_hkd_that_would_overflow_exact_calculation_fails_closed(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope, amount=INT64_MAX, rate=2_000_000_000_000, booked=INT64_MAX)
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_void_verifies_original_fingerprint_and_exact_reversal_without_income_grant(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        receipt, origin, cash = record(con, scope)
        void(con, receipt, origin, cash)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and result[0].receipt.status == "VOID"
    assert result[0].receipt.cash_scope == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert result[0].audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert result[0].eligibility_scope == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("forged", ["before", "after"])
def test_well_shaped_wrong_void_fingerprint_fails_closed(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], forged: str
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        receipt, origin, cash = record(con, scope)
        void(
            con,
            receipt,
            origin,
            cash,
            forged_audit=forged == "after",
            forged_before=forged == "before",
        )
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_later_duplicate_void_audit_makes_history_unavailable(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        receipt, origin, cash = record(con, scope)
        void(con, receipt, origin, cash)
        audit(
            con,
            str(OWNER),
            "dividend_receipt",
            receipt["id"],
            "DIVIDEND_RECEIPT_VOIDED",
            request_id=str(uuid4()),
            before={"synthetic": "old"},
            after={"synthetic": "new"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["audit-before-reversal", "void-time-mismatch"])
def test_well_shaped_void_audit_requires_consistent_recorded_times(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], problem: str
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        receipt, origin, cash = record(con, scope)
        if problem == "audit-before-reversal":
            void(con, receipt, origin, cash, audit_at=datetime.fromisoformat(receipt["created_at"]))
        else:
            void(con, receipt, origin, cash, voided_at=datetime.now(UTC) + timedelta(seconds=1))
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_old_entitlement_keeps_original_revision_when_observed_head_differs(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, second, account, older, _ = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        record(con, (second, str(account), older))
    with unit_of_work(engine) as con:
        result = receipts.history(con, OWNER, first.event_id, account)
    assert result is not None and len(result) == 1 and result[0].origin is not None
    assert result[0].original_revision_id == first.revision_id
    assert result[0].origin.observed_revision_id == second.revision_id


def test_observed_revision_cannot_precede_the_original_entitlement_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, second, account, _, newer = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        receipt, origin, cash = record(con, (second, str(account), newer))
    before = database_snapshot(engine)
    forged_origin = {
        **origin,
        "observed_revision_id": str(first.revision_id),
        "observed_revision_no": first.revision_no,
    }
    forged_hash = digest(canonical(receipts._creation_payload(receipt, forged_origin, cash, "")))
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        sql = str(statement)
        if "FROM dividend_receipt_origins o" in sql:
            rows = [dict(row) for row in result.mappings().all()]
            assert len(rows) == 1
            rows[0].update(forged_origin)
        elif "SELECT * FROM audit_events WHERE entity_type='dividend_receipt'" in sql:
            rows = [dict(row) for row in result.mappings().all()]
            assert len(rows) == 1
            rows[0]["after_sha256"] = forged_hash
        else:
            return result
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before


def test_same_owner_account_move_does_not_relabel_historical_origin(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        _, origin, _ = record(con, scope)
        portfolio, broker = str(uuid4()), str(uuid4())
        insert(
            con,
            "portfolios",
            {"id": portfolio, "owner_id": str(OWNER), "name": portfolio, "created_at": stamp()},
        )
        insert(con, "brokers", {"id": broker, "owner_id": str(OWNER), "name": broker})
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (portfolio, broker, account),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and result[0].origin is not None
    assert result[0].origin.portfolio_id == UUID(origin["portfolio_id"])
    assert result[0].origin.broker_id == UUID(origin["broker_id"])
    assert result[0].origin.account_version == 1
    assert result[0].current_use_scope == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("dimension", ["account", "portfolio", "broker", "data_revision"])
def test_rolled_back_current_scope_cannot_validate_a_later_origin(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], dimension: str
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        links = con.exec_driver_sql(
            "SELECT portfolio_id,broker_id FROM accounts WHERE id=?", (account,)
        ).one()
        table, column, identifier = {
            "account": ("accounts", "version", account),
            "portfolio": ("portfolios", "version", links.portfolio_id),
            "broker": ("brokers", "version", links.broker_id),
            "data_revision": ("portfolios", "data_revision", links.portfolio_id),
        }[dimension]
        con.exec_driver_sql(f"UPDATE {table} SET {column}=2 WHERE id=?", (identifier,))
        record(con, scope)
        con.exec_driver_sql(f"UPDATE {table} SET {column}=1 WHERE id=?", (identifier,))
    before = database_snapshot(engine)
    with (
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_recorded_audit_does_not_depend_on_the_current_wall_clock(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)

    class EarlierDate(date):
        @classmethod
        def today(cls) -> "EarlierDate":
            return cls(2025, 1, 1)

    with patch("spm_api.cash_models.date", EarlierDate), unit_of_work(engine) as con:
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and result[0].audit_scope == "RECORDED_AUDIT_MATCHED_ONLY"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_id": str(uuid4())},
        {"owner_id": str(OTHER)},
        {"event_id": str(uuid4())},
        {"account_id": str(uuid4())},
        {"observed_revision_id": str(uuid4())},
        {"observed_revision_no": 2},
        {"observed_revision_no": 2_147_483_648},
        {"account_version": True},
        {"retained_portfolio_owner": str(OTHER)},
        {"retained_broker_owner": str(OTHER)},
        {"retained_data_revision": False},
        {"cash_entry_id": str(uuid4())},
        {"request_id": "not-a-uuid"},
        {"created_at": "2000-01-01T00:00:00.000000Z"},
        {"fx_source": None},
        {"__duplicate__": True},
        {"__extra_origin__": True},
    ],
)
def test_mocked_corrupt_or_hidden_origin_fails_the_complete_read_without_private_details(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], changes: dict[str, Any]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "FROM dividend_receipt_origins o" not in str(statement):
            return result
        rows = [dict(row) for row in result.mappings().all()]
        assert len(rows) == 1
        if "__duplicate__" in changes:
            rows.append(dict(rows[0]))
        elif "__extra_origin__" in changes:
            rows.append({**rows[0], "receipt_id": str(uuid4())})
        else:
            rows[0].update(changes)
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"source_type": "SYNTHETIC_OTHER"},
        {"source_id": str(uuid4())},
        {"entry_role": "REVERSAL"},
        {"created_at": "2000-01-01T00:00:00.000000Z"},
        {"created_at": "9999-01-01T00:00:00.000000Z"},
        {"booked_hkd_micro": 46_000_000},
    ],
)
def test_mocked_cash_corruption_fails_closed_after_the_retained_snapshot(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], changes: dict[str, Any]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "SELECT * FROM cash_ledger_entries WHERE id=:id" not in str(statement):
            return result
        row = dict(result.mappings().one())
        row.update(changes)
        replacement = MagicMock()
        replacement.mappings.return_value.one.return_value = row
        return replacement

    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_one_observed_revision_is_verified_once_for_many_receipts(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        for _ in range(4):
            record(con, scope)
    with (
        patch.object(event_service, "get", wraps=event_service.get) as get_event,
        unit_of_work(engine) as con,
    ):
        result = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert result is not None and len(result) == 4
    assert get_event.call_count == 3


def test_cached_revision_still_rejects_a_conflicting_physical_pin(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
        second, _, _ = record(con, scope)
    before = database_snapshot(engine)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if "FROM dividend_receipt_origins o" not in str(statement):
            return result
        rows = [dict(row) for row in result.mappings().all()]
        target = next(row for row in rows if row["receipt_id"] == second["id"])
        target["observed_revision_id"] = str(uuid4())
        replacement = MagicMock()
        replacement.mappings.return_value.all.return_value = rows
        return replacement

    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_complete_origin_and_cash_reads_share_one_wal_snapshot(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        first, _, _ = record(con, scope)
    started, finished = Event(), Event()
    original = retained_history.history

    def paused(con: Connection, owner: UUID, event_id: UUID, account_id: UUID) -> Any:
        base = original(con, owner, event_id, account_id)
        started.set()
        assert finished.wait(timeout=15)
        return base

    def read() -> tuple[receipts.AuditedReceipt, ...] | None:
        with unit_of_work(engine) as con:
            return receipts.history(con, OWNER, event.event_id, UUID(account))

    with (
        patch.object(retained_history, "history", side_effect=paused),
        ThreadPoolExecutor() as pool,
    ):
        future = pool.submit(read)
        assert started.wait(timeout=15)
        try:
            with unit_of_work(engine, write=True) as con:
                second, _, _ = record(con, scope)
        finally:
            finished.set()
        observed = future.result(timeout=30)
    assert observed is not None and [row.receipt.id for row in observed] == [UUID(first["id"])]
    with unit_of_work(engine) as con:
        latest = receipts.history(con, OWNER, event.event_id, UUID(account))
    assert latest is not None
    assert {row.receipt.id for row in latest} == {UUID(first["id"]), UUID(second["id"])}


@pytest.mark.parametrize("revision_no", [True, 0, 2_147_483_648])
def test_mocked_original_revision_number_must_be_a_bounded_integer(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], revision_no: Any
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        base = retained_history.history(con, OWNER, event.event_id, UUID(account))
    assert base is not None and len(base.entitlements) == 1
    forged = replace(base, entitlements=(replace(base.entitlements[0], revision_no=revision_no),))
    with (
        patch.object(retained_history, "history", return_value=forged),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_mocked_receipt_preceding_source_audit_fails_closed(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        base = retained_history.history(con, OWNER, event.event_id, UUID(account))
    assert base is not None and len(base.entitlements) == 1
    entitlement = base.entitlements[0]
    item = replace(entitlement.receipts[0], created_at=datetime(2000, 1, 1, tzinfo=UTC))
    forged = replace(base, entitlements=(replace(entitlement, receipts=(item,)),))
    with (
        patch.object(retained_history, "history", return_value=forged),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_mocked_receipt_cannot_predate_its_entitlement_after_source_audit(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        base = retained_history.history(con, OWNER, event.event_id, UUID(account))
        source_time = event_service._time(
            con.exec_driver_sql(
                "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision' "
                "AND entity_id=? AND action='DIVIDEND_EVENT_REVISED'",
                (str(event.revision_id),),
            ).scalar_one()
        )
    assert base is not None and len(base.entitlements) == 1
    entitlement = base.entitlements[0]
    item = replace(entitlement.receipts[0], created_at=source_time)
    forged_entitlement = replace(
        entitlement, created_at=source_time + timedelta(microseconds=1), receipts=(item,)
    )
    forged = replace(base, entitlements=(forged_entitlement,))
    with (
        patch.object(retained_history, "history", return_value=forged),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_mocked_entitlement_cannot_predate_its_original_event_revision(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        base = retained_history.history(con, OWNER, event.event_id, UUID(account))
        original = event_service.get(con, OWNER, event.event_id, version=1)
    assert base is not None and original is not None and len(base.entitlements) == 1
    forged = replace(
        base,
        entitlements=(
            replace(
                base.entitlements[0],
                created_at=original.record.created_at - timedelta(microseconds=1),
            ),
        ),
    )
    with (
        patch.object(retained_history, "history", return_value=forged),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_old_entitlement_source_audit_cannot_appear_after_its_receipt(
    engine: Engine, source: dict[str, Any]
) -> None:
    first, second, account, older, _ = prepared(engine)
    with unit_of_work(engine, write=True) as con:
        record(con, (second, str(account), older))
    before = database_snapshot(engine)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        parameters = args[0] if args else kwargs.get("parameters", {})
        if (
            "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'"
            not in str(statement)
            or parameters.get("id") != str(first.revision_id)
        ):
            return result
        replacement = MagicMock()
        replacement.all.return_value = [SimpleNamespace(created_at="9999-01-01T00:00:00.000000Z")]
        return replacement

    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, first.event_id, account)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["earlier", "missing", "duplicate", "invalid-time"])
def test_mocked_source_audit_requires_unique_canonical_chronology(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], problem: str
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    execute = Connection.execute

    def injected(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(con, statement, *args, **kwargs)
        if (
            "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'"
            not in str(statement)
        ):
            return result
        replacement = MagicMock()
        if problem == "missing":
            replacement.all.return_value = []
        elif problem == "duplicate":
            rows = list(result.all())
            replacement.all.return_value = rows + rows
        else:
            created_at = "2000-01-01T00:00:00.000000Z" if problem == "earlier" else "invalid"
            replacement.all.return_value = [SimpleNamespace(created_at=created_at)]
        return replacement

    with (
        patch.object(Connection, "execute", injected),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_mocked_disappearing_account_fails_closed_inside_the_snapshot(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    before = database_snapshot(engine)
    with (
        patch.object(receipts, "account", return_value=None),
        pytest.raises(receipts.ReceiptAuditError, match="^DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE$"),
        unit_of_work(engine) as con,
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_foreign_scope_returns_none_and_invalid_transaction_fails_closed(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]]
) -> None:
    event, account, _ = scope
    with unit_of_work(engine, write=True) as con:
        record(con, scope)
    with unit_of_work(engine) as con:
        assert receipts.history(con, OTHER, event.event_id, UUID(account)) is None
        assert receipts.history(con, OWNER, uuid4(), UUID(account)) is None
        assert receipts.history(con, OWNER, event.event_id, uuid4()) is None
    with (
        engine.connect() as con,
        pytest.raises(receipts.ReceiptAuditError, match="^INVALID_DIVIDEND_RECEIPT_AUDIT_SCOPE$"),
    ):
        receipts.history(con, OWNER, event.event_id, UUID(account))
