"""Exact, explicitly scoped receipt void contracts never grant payment verification."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.dividend_receipt_void_write_models import ReceiptVoided, ReceiptVoidWrite


def request() -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "observed_revision_id": str(uuid4()),
        "observed_revision_no": 2,
        "entitlement_id": str(uuid4()),
        "original_revision_id": str(uuid4()),
        "account_id": str(uuid4()),
        "portfolio_id": str(uuid4()),
        "broker_id": str(uuid4()),
        "account_version": "9007199254740993",
        "portfolio_version": "9223372036854775807",
        "broker_version": "2",
        "expected_data_revision": "9223372036854775806",
        "cash_entry_id": str(uuid4()),
        "reason": "Synthetic recorded receipt correction reason",
        "confirmation": "VOID_RECORDED_CASH",
    }


def response() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "event_id": str(uuid4()),
        "observed_revision_id": str(uuid4()),
        "observed_revision_no": 2,
        "entitlement_id": str(uuid4()),
        "original_revision_id": str(uuid4()),
        "account_id": str(uuid4()),
        "portfolio_id": str(uuid4()),
        "origin_portfolio_id": str(uuid4()),
        "broker_id": str(uuid4()),
        "account_version": "9007199254740993",
        "portfolio_version": "9223372036854775807",
        "broker_version": "2",
        "portfolio_data_revision": "9223372036854775806",
        "cash_entry_id": str(uuid4()),
        "reversal_entry_id": str(uuid4()),
        "status": "VOID",
        "version": "2",
        "created_at": "2026-11-01T10:00:00Z",
        "voided_at": "2026-11-02T10:00:00Z",
        "audit_scope": "RECORDED_AUDIT_MATCHED_ONLY",
        "scope": "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
    }


def test_request_retains_explicit_exact_pins_and_reason() -> None:
    value = request()
    result = ReceiptVoidWrite.model_validate(value)
    assert result.model_dump(mode="json") == value
    assert "Synthetic recorded receipt correction reason" not in repr(result)


@pytest.mark.parametrize("field", tuple(request()))
def test_request_requires_every_scope_and_confirmation_field(field: str) -> None:
    value = request()
    value.pop(field)
    with pytest.raises(ValidationError):
        ReceiptVoidWrite.model_validate(value)


@pytest.mark.parametrize("field", ["account_version", "portfolio_version", "broker_version"])
@pytest.mark.parametrize("bad", [0, 1, True, "0", "01", "-1", "1e2", "9223372036854775808"])
def test_request_rejects_noncanonical_positive_versions(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        ReceiptVoidWrite.model_validate({**request(), field: bad})


@pytest.mark.parametrize("bad", [-1, 0, True, "-1", "01", "9223372036854775807"])
def test_request_rejects_invalid_or_exhausted_data_revision(bad: object) -> None:
    with pytest.raises(ValidationError):
        ReceiptVoidWrite.model_validate({**request(), "expected_data_revision": bad})


@pytest.mark.parametrize("bad", [0, -1, True, "1", 2_147_483_648])
def test_request_rejects_invalid_event_versions(bad: object) -> None:
    with pytest.raises(ValidationError):
        ReceiptVoidWrite.model_validate({**request(), "observed_revision_no": bad})


@pytest.mark.parametrize("bad", [None, "", "short", "bad\x00reason", "x" * 1001])
def test_request_rejects_missing_or_invalid_reason(bad: object) -> None:
    with pytest.raises(ValidationError):
        ReceiptVoidWrite.model_validate({**request(), "reason": bad})


def test_request_rejects_implicit_confirmation_or_extra_fields() -> None:
    for change in ({"confirmation": True}, {"confirmation": "VOID"}, {"unexpected": "private"}):
        with pytest.raises(ValidationError):
            ReceiptVoidWrite.model_validate({**request(), **change})


def test_response_retains_historical_scope_and_distinct_cash_ids() -> None:
    value = response()
    result = ReceiptVoided.model_validate(value)
    assert result.model_dump(mode="json")["version"] == "2"
    assert result.origin_portfolio_id != result.portfolio_id
    assert result.scope == "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"


@pytest.mark.parametrize(
    "change",
    [
        {"status": "ACTIVE"},
        {"version": "1"},
        {"audit_scope": "VERIFIED"},
        {"scope": "CURRENT"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "GRANTED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"payment_scope": "VERIFIED"},
        {"account_version": "01"},
        {"portfolio_data_revision": "9223372036854775807"},
        {"voided_at": "2020-01-01T00:00:00Z"},
        {"created_at": "2026-11-01T10:00:00"},
    ],
)
def test_response_rejects_forged_lifecycle_or_scope(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReceiptVoided.model_validate({**response(), **change})


def test_response_rejects_primary_cash_as_its_own_reversal() -> None:
    value = response()
    with pytest.raises(ValidationError):
        ReceiptVoided.model_validate({**value, "reversal_entry_id": value["cash_entry_id"]})
