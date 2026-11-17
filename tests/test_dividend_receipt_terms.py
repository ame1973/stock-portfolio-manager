"""Receipt terms preserve actual cash, unknown components and locked FX provenance."""

from datetime import date, timedelta
from decimal import localcontext
from typing import Any

import pytest
from pydantic import ValidationError

from spm_api.dividend_receipt_terms import (
    ReceiptComponents,
    ReceiptTerms,
    calculate_components,
    calculate_receipt,
)
from spm_api.finance import FinanceError


def terms(**changes: Any) -> dict[str, Any]:
    return {
        "received_date": date.today().isoformat(),
        "received_amount": "6",
        "currency": "USD",
        "tax": None,
        "fee": "0",
        "fx_rate_hkd": None,
        "fx_source": None,
        "actual_hkd": None,
        "actual_hkd_reason": None,
        **changes,
    }


def test_undated_components_are_only_arithmetic_and_cannot_replace_dated_booking_terms() -> None:
    values = terms(fx_rate_hkd="7.8", fx_source="Synthetic FX")
    del values["received_date"]
    components = ReceiptComponents.model_validate(values)
    assert calculate_components(components).received_hkd_micro == 46_800_000
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_RECEIPT_TERMS$"):
        calculate_receipt(components)  # type: ignore[arg-type]
    forged = components.model_copy(update={"received_amount": 0.1})
    with pytest.raises(ValidationError):
        calculate_components(forged)
    untyped: Any = {"synthetic-private": "do-not-log"}
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_RECEIPT_TERMS$"):
        calculate_components(untyped)


def test_unknown_tax_and_fx_are_not_zeros_and_explicit_zero_fee_is_retained() -> None:
    intent = ReceiptTerms.model_validate(terms())
    actual = calculate_receipt(intent)
    assert actual.received_micro == 6_000_000
    assert actual.tax_micro is None and actual.fee_micro == 0
    assert actual.fx_rate_hkd_e12 is None and actual.fx_source is None
    assert actual.received_hkd_micro is None and not actual.actual_hkd_override
    assert "6.000000" not in repr(intent) and "6000000" not in repr(actual)


def test_known_tax_and_fee_do_not_get_subtracted_from_the_actual_received_amount() -> None:
    intent = ReceiptTerms.model_validate(
        terms(tax="1.2", fee="0.5", fx_rate_hkd="7.8", fx_source="Synthetic broker FX")
    )
    actual = calculate_receipt(intent)
    assert actual.received_micro == 6_000_000
    assert actual.tax_micro == 1_200_000 and actual.fee_micro == 500_000
    assert actual.fx_rate_hkd_e12 == 7_800_000_000_000
    assert actual.fx_source == "Synthetic broker FX" and actual.received_hkd_micro == 46_800_000
    assert not actual.actual_hkd_override


def test_broker_override_is_explicit_locked_and_not_recomputed() -> None:
    intent = ReceiptTerms.model_validate(
        terms(
            fx_rate_hkd="7.812345678901",
            fx_source="Synthetic historical broker rate",
            actual_hkd="46.123456",
            actual_hkd_reason="Synthetic actual broker settlement amount.",
        )
    )
    actual = calculate_receipt(intent)
    assert actual.received_micro == 6_000_000 and actual.received_hkd_micro == 46_123_456
    assert actual.fx_rate_hkd_e12 == 7_812_345_678_901 and actual.actual_hkd_override
    assert "46.123456" not in repr(intent)
    assert "Synthetic actual broker" not in repr(intent)


@pytest.mark.parametrize("rate,source", [(None, None), ("1", "IDENTITY")])
def test_hkd_identity_is_exact_without_an_external_rate(
    rate: str | None, source: str | None
) -> None:
    actual = calculate_receipt(
        ReceiptTerms.model_validate(terms(currency="HKD", fx_rate_hkd=rate, fx_source=source))
    )
    assert actual.received_micro == actual.received_hkd_micro == 6_000_000
    assert actual.fx_rate_hkd_e12 == 1_000_000_000_000 and actual.fx_source == "IDENTITY"


def test_negative_receipt_and_zero_are_preserved_without_interpreting_them_as_payment() -> None:
    negative = calculate_receipt(
        ReceiptTerms.model_validate(terms(received_amount="-1.25", currency="HKD", fee=None))
    )
    explicit_zero = calculate_receipt(ReceiptTerms.model_validate(terms(received_amount="0")))
    assert negative.received_micro == negative.received_hkd_micro == -1_250_000
    assert negative.fee_micro is None
    assert explicit_zero.received_micro == 0 and explicit_zero.received_hkd_micro is None


@pytest.mark.parametrize("amount,expected", [("0.000001", 1), ("-0.000001", -1)])
def test_hkd_valuation_rounds_only_the_calculated_half_micro(amount: str, expected: int) -> None:
    actual = calculate_receipt(
        ReceiptTerms.model_validate(
            terms(received_amount=amount, fx_rate_hkd="0.5", fx_source="Synthetic FX")
        )
    )
    assert actual.received_hkd_micro == expected


def test_booked_int64_min_is_rejected_because_its_reversal_would_overflow() -> None:
    with pytest.raises(FinanceError) as failure:
        calculate_receipt(
            ReceiptTerms.model_validate(
                terms(
                    received_amount="-4611686018427.387904",
                    fx_rate_hkd="2",
                    fx_source="Synthetic FX",
                )
            )
        )
    assert failure.value.code == "FINANCIAL_OVERFLOW"


def test_calculated_values_use_exact_int64_and_ignore_ambient_decimal_precision() -> None:
    with localcontext() as context:
        context.prec = 4
        actual = calculate_receipt(
            ReceiptTerms.model_validate(
                terms(received_amount="9223372036854.775807", currency="HKD")
            )
        )
    assert actual.received_micro == actual.received_hkd_micro == 9_223_372_036_854_775_807
    with pytest.raises(FinanceError, match="INT64") as failure:
        calculate_receipt(
            ReceiptTerms.model_validate(
                terms(
                    received_amount="9223372036854.775807",
                    fx_rate_hkd="2",
                    fx_source="Synthetic FX",
                )
            )
        )
    assert failure.value.code == "FINANCIAL_OVERFLOW"


@pytest.mark.parametrize(
    "changes",
    [
        {"received_amount": 0.1},
        {"received_amount": True},
        {"received_amount": "NaN"},
        {"received_amount": "0.0000001"},
        {"received_amount": "9223372036854.775808"},
        {"received_amount": "-9223372036854.775808"},
        {"tax": "-0.01"},
        {"fee": 0},
        {"currency": "CNY"},
        {"received_date": "20261101"},
        {"fx_rate_hkd": "0"},
        {"fx_rate_hkd": "1.0000000000001", "fx_source": "Synthetic"},
        {"fx_rate_hkd": "7.8", "fx_source": None},
        {"fx_rate_hkd": None, "fx_source": "Synthetic"},
        {"fx_rate_hkd": "7.8", "fx_source": " Synthetic "},
        {"fx_rate_hkd": "7.8", "fx_source": "invalid\0source"},
        {"fx_rate_hkd": "7.8", "fx_source": "IDENTITY"},
        {"currency": "HKD", "fx_rate_hkd": "7.8", "fx_source": "Synthetic"},
        {"currency": "HKD", "fx_rate_hkd": "1", "fx_source": "Synthetic"},
        {"actual_hkd": "1", "actual_hkd_reason": "Synthetic actual amount"},
        {"actual_hkd": None, "actual_hkd_reason": "Synthetic actual amount"},
        {"fx_rate_hkd": "7.8", "fx_source": "Synthetic", "actual_hkd": "1"},
        {
            "fx_rate_hkd": "7.8",
            "fx_source": "Synthetic",
            "actual_hkd": "-1",
            "actual_hkd_reason": "Synthetic actual amount",
        },
        {"currency": "HKD", "actual_hkd": "7", "actual_hkd_reason": "Synthetic actual amount"},
        {"received_date": (date.today() + timedelta(days=1)).isoformat()},
        {"unknown_field": "synthetic"},
    ],
)
def test_rejects_invalid_or_unsubstantiated_terms(changes: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, FinanceError)):
        calculate_receipt(ReceiptTerms.model_validate(terms(**changes)))


@pytest.mark.parametrize(
    "name", ["tax", "fee", "fx_rate_hkd", "fx_source", "actual_hkd", "actual_hkd_reason"]
)
def test_unknown_or_known_components_must_be_explicitly_selected(name: str) -> None:
    body = terms()
    del body[name]
    with pytest.raises(ValidationError):
        ReceiptTerms.model_validate(body)


def test_untyped_input_is_rejected_without_inspecting_private_values() -> None:
    untyped: Any = {"synthetic-private": "do-not-log"}
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_RECEIPT_TERMS$"):
        calculate_receipt(untyped)


def test_forged_model_copy_is_revalidated_before_any_calculation() -> None:
    valid = ReceiptTerms.model_validate(terms())
    forged = valid.model_copy(update={"received_amount": 0.1})
    with pytest.raises(ValidationError):
        calculate_receipt(forged)
