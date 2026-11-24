"""Direct receipt void contracts retain exact pins without legacy entitlement."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.dividend_direct_receipt_void_write_models import (
    DirectReceiptVoided,
    DirectReceiptVoidWrite,
)
from tests.test_dividend_receipt_void_write_models import request as legacy_request
from tests.test_dividend_receipt_void_write_models import response as legacy_response


def request() -> dict[str, Any]:
    value = legacy_request()
    value.pop("entitlement_id")
    return {**value, "original_revision_no": 1}


def response() -> dict[str, Any]:
    value = legacy_response()
    value.pop("entitlement_id")
    return {
        **value,
        "original_revision_no": 1,
        "receipt_basis": "DIRECT_EVENT_ACCOUNT",
        "scope": "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY",
    }


def test_request_retains_exact_pins_without_inventing_entitlement() -> None:
    value = request()
    model = DirectReceiptVoidWrite.model_validate(value)
    assert model.model_dump(mode="json") == value
    for key in ("event_id", "account_id", "cash_entry_id", "reason"):
        assert value[key] not in repr(model)
    assert "entitlement_id" not in model.model_dump()
    assert model.account_version == "9007199254740993"


@pytest.mark.parametrize("field", tuple(request()))
def test_all_direct_void_request_pins_are_required(field: str) -> None:
    value = request()
    value.pop(field)
    with pytest.raises(ValidationError):
        DirectReceiptVoidWrite.model_validate(value)


@pytest.mark.parametrize("field", ["account_version", "portfolio_version", "broker_version"])
@pytest.mark.parametrize("bad", [0, 1, True, "0", "01", "-1", "1e2", "9223372036854775808"])
def test_versions_are_canonical_positive_int64_strings(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptVoidWrite.model_validate({**request(), field: bad})


@pytest.mark.parametrize("bad", [-1, 0, True, "-1", "01", "9223372036854775807"])
def test_data_revision_requires_an_advanceable_exact_string(bad: object) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptVoidWrite.model_validate({**request(), "expected_data_revision": bad})


@pytest.mark.parametrize("field", ["observed_revision_no", "original_revision_no"])
@pytest.mark.parametrize("bad", [0, -1, True, "1", 2_147_483_648])
def test_event_revision_numbers_are_strict_bounded_integers(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptVoidWrite.model_validate({**request(), field: bad})


@pytest.mark.parametrize("bad", [None, "", "short", "bad\x00reason", "x" * 1001])
def test_a_valid_explicit_reason_is_required(bad: object) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptVoidWrite.model_validate({**request(), "reason": bad})


def test_request_rejects_implicit_confirmation_and_legacy_or_unknown_fields() -> None:
    for change in (
        {"confirmation": True},
        {"confirmation": "VOID"},
        {"entitlement_id": str(uuid4())},
        {"unexpected": "private"},
    ):
        with pytest.raises(ValidationError):
            DirectReceiptVoidWrite.model_validate({**request(), **change})


@pytest.mark.parametrize("kind", ["request", "response"])
def test_original_and_observed_revision_order_and_identity_are_consistent(kind: str) -> None:
    model = DirectReceiptVoidWrite if kind == "request" else DirectReceiptVoided
    value = request() if kind == "request" else response()
    for change in (
        {"original_revision_no": 3},
        {"original_revision_no": 2},
        {"original_revision_id": value["observed_revision_id"]},
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**value, **change})
    same = {
        **value,
        "original_revision_id": value["observed_revision_id"],
        "original_revision_no": 2,
    }
    assert model.model_validate(same).original_revision_no == 2


def test_response_retains_historical_scope_and_separate_origin_portfolio() -> None:
    value = response()
    model = DirectReceiptVoided.model_validate(value)
    assert model.scope == "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY"
    assert model.portfolio_id != model.origin_portfolio_id
    assert model.cash_entry_id != model.reversal_entry_id
    assert model.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert model.model_dump(mode="json")["version"] == "2"
    for key in ("id", "cash_entry_id", "reversal_entry_id", "origin_portfolio_id"):
        assert value[key] not in repr(model)


@pytest.mark.parametrize("field", tuple(response()))
def test_response_requires_all_retained_fields(field: str) -> None:
    value = response()
    value.pop(field)
    with pytest.raises(ValidationError):
        DirectReceiptVoided.model_validate(value)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "ACTIVE"},
        {"version": "1"},
        {"version": 2},
        {"receipt_basis": "LEGACY_ENTITLEMENT"},
        {"entitlement_id": None},
        {"audit_scope": "VERIFIED"},
        {"scope": "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "GRANTED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"payment_scope": "VERIFIED"},
        {"account_version": "01"},
        {"portfolio_data_revision": "9223372036854775807"},
        {"voided_at": "2020-01-01T00:00:00Z"},
        {"created_at": "2026-11-01T10:00:00"},
        {"voided_at": None},
        {"origin_portfolio_id": None},
        {"reversal_entry_id": None},
    ],
)
def test_response_rejects_forged_lifecycle_and_scope(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptVoided.model_validate({**response(), **change})


def test_primary_cash_cannot_be_its_own_reversal() -> None:
    value = response()
    with pytest.raises(ValidationError):
        DirectReceiptVoided.model_validate({**value, "reversal_entry_id": value["cash_entry_id"]})
