"""Protected receipt inputs retain explicit cash, provenance and exact scope pins."""

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_receipt_write_models import ReceiptCreate, ReceiptCreated


def body(**changes: Any) -> dict[str, Any]:
    return {
        "receipt_id": str(uuid4()),
        "event_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "entitlement_id": str(uuid4()),
        "account_id": str(uuid4()),
        "portfolio_id": str(uuid4()),
        "broker_id": str(uuid4()),
        "account_version": "1",
        "portfolio_version": "2",
        "broker_version": "3",
        "expected_data_revision": "4",
        "received_date": "2026-01-09",
        "received_amount": "6",
        "currency": "USD",
        "tax": None,
        "fee": "0",
        "fx_rate_hkd": "7.8",
        "fx_source": "Synthetic FX source",
        "actual_hkd": None,
        "actual_hkd_reason": None,
        "note": "synthetic-private note",
        "reason": "synthetic-private reason",
        "confirmation": "RECORDED_CASH_ONLY",
        **changes,
    }


def test_explicit_inputs_normalize_decimals_without_exposing_private_values() -> None:
    value = ReceiptCreate.model_validate(body())
    assert value.received_amount == "6.000000" and value.fee == "0.000000"
    assert value.tax is None and value.fx_rate_hkd == "7.800000000000"
    assert value.confirmation == "RECORDED_CASH_ONLY"
    assert "synthetic-private" not in repr(value)
    assert value == ReceiptCreate.model_validate(value.model_dump())


@pytest.mark.parametrize(
    "name",
    [
        "receipt_id",
        "event_id",
        "revision_id",
        "entitlement_id",
        "account_id",
        "portfolio_id",
        "broker_id",
        "account_version",
        "portfolio_version",
        "broker_version",
        "expected_data_revision",
        "received_date",
        "received_amount",
        "currency",
        "tax",
        "fee",
        "fx_rate_hkd",
        "fx_source",
        "actual_hkd",
        "actual_hkd_reason",
        "note",
        "reason",
        "confirmation",
    ],
)
def test_each_scope_and_known_or_unknown_component_is_explicit(name: str) -> None:
    payload = body()
    del payload[name]
    with pytest.raises(ValidationError):
        ReceiptCreate.model_validate(payload)


@pytest.mark.parametrize(
    ("name", "invalid"),
    [
        ("receipt_id", "synthetic-private invalid UUID"),
        ("account_version", True),
        ("account_version", 1),
        ("portfolio_version", "0"),
        ("broker_version", "01"),
        ("expected_data_revision", str(INT64_MAX)),
        ("expected_data_revision", "-1"),
        ("received_date", "2026-02-29"),
        ("received_amount", 1.0),
        ("received_amount", "1.0000001"),
        ("currency", "CNY"),
        ("tax", "-0.01"),
        ("fee", "-1"),
        ("fx_rate_hkd", "0"),
        ("fx_source", "  synthetic source  "),
        ("confirmation", "CONFIRMED_ELIGIBILITY"),
        ("reason", "short"),
        ("note", "contains\0nul"),
    ],
)
def test_invalid_wire_inputs_fail_closed_without_leaking_values(name: str, invalid: object) -> None:
    with pytest.raises(ValidationError) as error:
        ReceiptCreate.model_validate(body(**{name: invalid}))
    assert "synthetic-private" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"fx_rate_hkd": None},
        {"fx_source": None},
        {"fx_source": "IDENTITY"},
        {"currency": "HKD"},
        {"actual_hkd": "46.8"},
        {"actual_hkd_reason": "Synthetic actual HKD reason"},
        {"actual_hkd": "-1", "actual_hkd_reason": "Synthetic actual HKD reason"},
        {
            "currency": "HKD",
            "fx_rate_hkd": "1",
            "fx_source": "IDENTITY",
            "actual_hkd": "7",
            "actual_hkd_reason": "Synthetic actual HKD reason",
        },
        {"received_amount": "9223372036854.775807", "fx_rate_hkd": "2"},
        {
            "fx_rate_hkd": None,
            "fx_source": None,
            "actual_hkd": "46.8",
            "actual_hkd_reason": "Synthetic actual HKD reason",
        },
    ],
)
def test_incomplete_or_contradictory_fx_and_override_inputs_are_rejected(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ReceiptCreate.model_validate(body(**changes))


def test_unknown_fx_and_last_advanceable_revision_remain_explicit() -> None:
    value = ReceiptCreate.model_validate(
        body(
            account_version=str(INT64_MAX),
            expected_data_revision=str(INT64_MAX - 1),
            fx_rate_hkd=None,
            fx_source=None,
            tax=None,
            fee=None,
        )
    )
    assert value.account_version == str(INT64_MAX)
    assert value.expected_data_revision == str(INT64_MAX - 1)
    assert value.fx_rate_hkd is value.fx_source is value.tax is value.fee is None


def test_historical_replay_wire_model_does_not_depend_on_the_current_clock() -> None:
    value = ReceiptCreate.model_validate(body(received_date="9999-12-31"))
    assert date.fromisoformat(value.received_date) > date.today()
    # The financial writer independently rejects a new future posting.


def created(**changes: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "event_id": str(uuid4()),
        "account_id": str(uuid4()),
        "entitlement_id": str(uuid4()),
        "original_revision_id": str(uuid4()),
        "original_revision_no": 1,
        "observed_revision_id": str(uuid4()),
        "observed_revision_no": 2,
        "portfolio_id": str(uuid4()),
        "broker_id": str(uuid4()),
        "account_version": "1",
        "portfolio_version": "1",
        "broker_version": "1",
        "portfolio_data_revision": "0",
        "cash_entry_id": str(uuid4()),
        "received_date": "2026-01-09",
        "received_amount": "6",
        "currency": "USD",
        "tax": None,
        "fee": "0",
        "fx_rate_hkd": "7.8",
        "fx_source": "Synthetic FX source",
        "received_hkd": "46.8",
        "actual_hkd_override": False,
        "source": "MANUAL",
        "status": "ACTIVE",
        "version": "1",
        "created_at": "2026-11-09T12:00:03Z",
        "audit_scope": "RECORDED_AUDIT_MATCHED_ONLY",
        "scope": "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
        **changes,
    }


def test_creation_response_retains_exact_unverified_cash_scope() -> None:
    result = ReceiptCreated.model_validate(created())
    assert result.received_amount == "6.000000" and result.received_hkd == "46.800000"
    assert result.fx_rate_hkd == "7.800000000000" and result.tax is None
    assert result.scope == "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert "6.000000" not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"fx_rate_hkd": None, "fx_source": None, "received_hkd": None},
        {"actual_hkd_override": True, "received_hkd": "45.6"},
        {"currency": "HKD", "fx_rate_hkd": "1", "fx_source": "IDENTITY", "received_hkd": "6"},
    ],
)
def test_creation_response_keeps_unknown_override_and_hkd_identity_distinct(
    changes: dict[str, Any],
) -> None:
    result = ReceiptCreated.model_validate(created(**changes))
    if result.fx_rate_hkd is None:
        assert result.received_hkd is None and result.fx_source is None
    elif result.currency == "HKD":
        assert result.fx_source == "IDENTITY" and result.received_hkd == result.received_amount
    else:
        assert result.actual_hkd_override and result.received_hkd == "45.600000"


@pytest.mark.parametrize(
    "changes",
    [
        {"original_revision_no": 3},
        {"original_revision_no": 2},
        {"fx_rate_hkd": None},
        {"fx_source": None},
        {"fx_source": " Synthetic FX source "},
        {"received_hkd": None},
        {"received_hkd": "45.6"},
        {"received_hkd": "-1", "actual_hkd_override": True},
        {"received_amount": "0", "actual_hkd_override": True},
        {"fx_source": "IDENTITY", "actual_hkd_override": True},
        {"currency": "HKD", "actual_hkd_override": True},
        {"currency": "HKD", "fx_rate_hkd": "1", "fx_source": "IDENTITY", "received_hkd": "7"},
        {"status": "VOID"},
        {"version": "2"},
        {"actual_hkd_override": 1},
        {"portfolio_data_revision": str(INT64_MAX)},
        {"eligibility_scope": "ESTABLISHED"},
    ],
)
def test_forged_creation_response_cannot_be_accepted_as_original(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ReceiptCreated.model_validate(created(**changes))


def test_same_revision_requires_the_same_physical_identity() -> None:
    revision = str(uuid4())
    valid = created(
        original_revision_id=revision,
        observed_revision_id=revision,
        original_revision_no=1,
        observed_revision_no=1,
    )
    assert ReceiptCreated.model_validate(valid).original_revision_no == 1
    with pytest.raises(ValidationError):
        ReceiptCreated.model_validate({**valid, "observed_revision_no": 2})
