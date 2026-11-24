"""Combined wire records preserve exact amounts, provenance and lifecycle boundaries."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.dividend_combined_receipt_models import CombinedRecordedReceipt


def record(**changes: Any) -> dict[str, Any]:
    revision = str(uuid4())
    return {
        "id": str(uuid4()),
        "event_id": str(uuid4()),
        "account_id": str(uuid4()),
        "receipt_basis": "DIRECT_EVENT_ACCOUNT",
        "entitlement_id": None,
        "revision_id": revision,
        "revision_no": 2,
        "current_revision_id": revision,
        "current_revision_no": 2,
        "received_date": "2026-01-10",
        "recorded_amount": "2.000000",
        "currency": "USD",
        "recorded_tax": None,
        "recorded_fee": "0.000000",
        "fx_rate_hkd": None,
        "received_hkd": None,
        "source": "MANUAL",
        "status": "ACTIVE",
        "version": "1",
        "created_at": "2026-11-01T00:00:00.000000Z",
        "voided_at": None,
        "cash_scope": "MATCHING_CASH_RECORDED_UNVERIFIED",
        "audit_scope": "RECORDED_AUDIT_MATCHED_ONLY",
        "scope": "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
        **changes,
    }


@pytest.mark.parametrize("direct", [True, False])
def test_explicit_basis_unknowns_and_exact_values_are_preserved(direct: bool) -> None:
    values = record(recorded_amount="9223372036854.775807")
    if not direct:
        values.update(
            receipt_basis="LEGACY_ENTITLEMENT",
            entitlement_id=str(uuid4()),
            audit_scope="RECEIPT_AUDIT_NOT_VERIFIED",
            revision_no=1,
            revision_id=str(uuid4()),
        )
    result = CombinedRecordedReceipt.model_validate(values)
    assert result.recorded_amount == "9223372036854.775807"
    assert result.recorded_tax is None and result.recorded_fee == "0.000000"
    assert result.fx_rate_hkd is result.received_hkd is None
    assert "9223372036854.775807" not in repr(result)
    assert values["id"] not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"entitlement_id": str(uuid4())},
        {"receipt_basis": "LEGACY_ENTITLEMENT"},
        {"revision_no": 3},
        {"revision_no": 1},
        {"revision_id": str(uuid4())},
        {"audit_scope": "RECEIPT_AUDIT_NOT_VERIFIED"},
        {"source": "IMPORT"},
        {"version": "2"},
        {"status": "VOID"},
        {"voided_at": "2026-11-02T00:00:00.000000Z"},
        {"cash_scope": "CASH_NOT_RECORDED"},
        {"cash_scope": "CASH_REVERSAL_NOT_RECORDED"},
        {"cash_scope": "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"},
        {"recorded_amount": "-9223372036854.775808"},
        {"recorded_amount": "0.0000001"},
        {"recorded_amount": 2},
        {"version": 1},
        {"fx_rate_hkd": "7.8"},
        {"received_hkd": "15.6"},
        {"currency": "HKD"},
        {"fx_rate_hkd": "7.8", "received_hkd": "-9223372036854.775808"},
        {"fx_rate_hkd": "7.8", "received_hkd": "-15.6"},
        {"recorded_amount": "-2", "fx_rate_hkd": "7.8", "received_hkd": "15.6"},
        {"recorded_amount": "0", "fx_rate_hkd": "7.8", "received_hkd": "0.000001"},
        {"scope": "CONFIRMED"},
        {"payment_scope": "VERIFIED"},
        {"source_url": "https://example.test/private"},
    ],
)
def test_inconsistent_direct_wire_records_fail_closed(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CombinedRecordedReceipt.model_validate(record(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "1"},
        {"voided_at": None},
        {"voided_at": "2026-10-01T00:00:00.000000Z"},
        {"cash_scope": "MATCHING_CASH_RECORDED_UNVERIFIED"},
    ],
)
def test_inconsistent_void_lifecycle_is_rejected(changes: dict[str, Any]) -> None:
    values = record(
        status="VOID",
        version="2",
        voided_at="2026-11-02T00:00:00.000000Z",
        cash_scope="MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED",
    )
    values.update(changes)
    with pytest.raises(ValidationError):
        CombinedRecordedReceipt.model_validate(values)


def test_known_fx_and_hkd_identity_remain_exact() -> None:
    usd = CombinedRecordedReceipt.model_validate(record(fx_rate_hkd="7.8", received_hkd="15.6"))
    assert usd.fx_rate_hkd == "7.800000000000" and usd.received_hkd == "15.600000"
    hkd = CombinedRecordedReceipt.model_validate(
        record(currency="HKD", fx_rate_hkd="1", received_hkd="2")
    )
    assert hkd.fx_rate_hkd == "1.000000000000" and hkd.received_hkd == hkd.recorded_amount


def test_matched_legacy_origin_does_not_allow_contradictory_cash_components() -> None:
    values = record(
        receipt_basis="LEGACY_ENTITLEMENT",
        entitlement_id=str(uuid4()),
        fx_rate_hkd="7.8",
        received_hkd="-15.6",
    )
    with pytest.raises(ValidationError):
        CombinedRecordedReceipt.model_validate(values)


def test_legacy_missing_reversal_remains_unverified_not_zero() -> None:
    result = CombinedRecordedReceipt.model_validate(
        record(
            receipt_basis="LEGACY_ENTITLEMENT",
            entitlement_id=str(uuid4()),
            status="VOID",
            version="9007199254740993",
            voided_at="2026-11-02T00:00:00.000000Z",
            cash_scope="CASH_REVERSAL_NOT_RECORDED",
            audit_scope="RECEIPT_AUDIT_NOT_VERIFIED",
        )
    )
    assert result.version == "9007199254740993"
    assert result.cash_scope == "CASH_REVERSAL_NOT_RECORDED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
