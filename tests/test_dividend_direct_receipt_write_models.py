"""Direct receipt HTTP contracts require explicit cash and never manufacture entitlement."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.dividend_direct_receipt_write_models import DirectReceiptCreate, DirectReceiptCreated
from tests.test_dividend_receipt_write_models import body as legacy_body
from tests.test_dividend_receipt_write_models import created as legacy_created


def body(**changes: Any) -> dict[str, Any]:
    value = legacy_body()
    del value["entitlement_id"]
    return {**value, "confirm_revision_fence": True, **changes}


def created(**changes: Any) -> dict[str, Any]:
    value = legacy_created()
    revision = value.pop("observed_revision_id")
    version = value.pop("observed_revision_no")
    for name in (
        "entitlement_id",
        "original_revision_id",
        "original_revision_no",
        "actual_hkd_override",
    ):
        del value[name]
    return {
        **value,
        "revision_id": revision,
        "revision_no": version,
        "receipt_basis": "DIRECT_EVENT_ACCOUNT",
        "actual_hkd": None,
        "actual_hkd_reason": None,
        "scope": "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY",
        **changes,
    }


def test_explicit_direct_cash_has_no_entitlement_and_no_implicit_zero_or_fx() -> None:
    request = DirectReceiptCreate.model_validate(
        body(tax=None, fee="0", fx_rate_hkd=None, fx_source=None)
    )
    assert request.tax is None and request.fee == "0.000000"
    assert request.fx_rate_hkd is request.fx_source is None
    assert request.confirm_revision_fence is True
    assert "entitlement_id" not in request.model_dump()
    assert "synthetic-private" not in repr(request)
    assert DirectReceiptCreate.model_validate(request.model_dump()) == request


@pytest.mark.parametrize("name", list(body()))
def test_all_direct_creation_intent_fields_are_explicit(name: str) -> None:
    value = body()
    del value[name]
    with pytest.raises(ValidationError):
        DirectReceiptCreate.model_validate(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"entitlement_id": str(uuid4())},
        {"receipt_id": "private"},
        {"confirm_revision_fence": False},
        {"confirm_revision_fence": 1},
        {"confirm_revision_fence": "true"},
        {"account_version": 1},
        {"account_version": True},
        {"portfolio_version": "0"},
        {"broker_version": "01"},
        {"expected_data_revision": "9223372036854775807"},
        {"received_amount": 0.1},
        {"received_amount": "1.0000001"},
        {"received_amount": "-9223372036854.775808"},
        {"currency": "CNY"},
        {"tax": "-1"},
        {"fee": "-1"},
        {"fx_source": None},
        {"fx_rate_hkd": None},
        {"fx_source": "IDENTITY"},
        {"received_amount": "9223372036854.775807", "fx_rate_hkd": "2"},
        {"actual_hkd": "-1", "actual_hkd_reason": "Synthetic contrary cash signs"},
        {"confirmation": "CONFIRMED_PAYMENT"},
        {"reason": "short"},
    ],
)
def test_unsafe_direct_intent_is_not_accepted(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptCreate.model_validate(body(**changes))


def test_replay_contract_is_clock_independent_and_keeps_maximum_pins_exact() -> None:
    value = DirectReceiptCreate.model_validate(
        body(
            received_date="9999-12-31",
            account_version="9223372036854775807",
            expected_data_revision="9223372036854775806",
        )
    )
    assert value.received_date == "9999-12-31"
    assert value.account_version == "9223372036854775807"
    assert value.expected_data_revision == "9223372036854775806"


def test_direct_creation_response_preserves_original_components_and_no_grant_labels() -> None:
    value = DirectReceiptCreated.model_validate(created())
    assert value.received_amount == "6.000000" and value.received_hkd == "46.800000"
    assert value.tax is None and value.fee == "0.000000"
    assert value.fx_rate_hkd == "7.800000000000" and value.actual_hkd is None
    assert value.receipt_basis == "DIRECT_EVENT_ACCOUNT"
    assert value.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert value.eligibility_scope == "NOT_ESTABLISHED"
    assert "entitlement_id" not in value.model_dump()
    assert "6.000000" not in repr(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"fx_rate_hkd": None, "fx_source": None, "received_hkd": None},
        {"currency": "HKD", "fx_rate_hkd": "1", "fx_source": "IDENTITY", "received_hkd": "6"},
        {
            "actual_hkd": "47",
            "actual_hkd_reason": "Synthetic explicit original HKD adjustment",
            "received_hkd": "47",
        },
        {"received_amount": "-6", "received_hkd": "-46.8"},
        {"received_amount": "0", "received_hkd": "0"},
        {
            "received_amount": "9223372036854.775807",
            "fx_rate_hkd": None,
            "fx_source": None,
            "received_hkd": None,
        },
    ],
)
def test_exact_unknown_identity_signed_and_override_responses(changes: dict[str, Any]) -> None:
    value = DirectReceiptCreated.model_validate(created(**changes))
    assert DirectReceiptCreated.model_validate(value.model_dump()) == value


@pytest.mark.parametrize(
    "changes",
    [
        {"entitlement_id": None},
        {"receipt_basis": "LEGACY_ENTITLEMENT"},
        {"status": "VOID"},
        {"version": "2"},
        {"source": "IMPORT"},
        {"revision_no": "2"},
        {"revision_no": 2_147_483_648},
        {"received_hkd": "46.7"},
        {"received_hkd": None},
        {"fx_rate_hkd": None, "fx_source": None},
        {"currency": "HKD", "fx_rate_hkd": None, "fx_source": None, "received_hkd": "6"},
        {"actual_hkd": "47", "received_hkd": "47"},
        {"actual_hkd": "47", "actual_hkd_reason": "Synthetic explicit original HKD adjustment"},
        {
            "actual_hkd": "-9223372036854.775808",
            "actual_hkd_reason": "Synthetic explicit original HKD adjustment",
        },
        {"audit_scope": "VERIFIED"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "GRANTED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"payment_scope": "VERIFIED"},
        {"scope": "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"},
    ],
)
def test_contradictory_direct_creation_response_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DirectReceiptCreated.model_validate(created(**changes))
