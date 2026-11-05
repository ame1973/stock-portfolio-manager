"""Exact decimal string boundaries, not approximate float comparisons."""

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from spm_api.decimal_types import (
    INT64_MAX,
    INT64_MIN,
    Dps,
    PositiveQuantity,
    Price,
    Quantity,
    Rate,
    from_scaled,
    to_scaled,
)
from spm_api.master_models import AccountCreate, BrokerCreate


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        ("0", 6, 0),
        ("-0.000000", 6, 0),
        ("0.000001", 6, 1),
        ("1.234567", 6, 1234567),
        ("-1.234567", 6, -1234567),
        ("0001.2", 6, 1200000),
        ("9223372036854.775807", 6, INT64_MAX),
        ("-9223372036854.775808", 6, INT64_MIN),
        ("0.00000001", 8, 1),
        ("7.812345678901", 12, 7812345678901),
    ],
)
def test_scaled_encoding_is_exact(value: str, scale: int, expected: int) -> None:
    assert to_scaled(value, scale) == expected
    assert to_scaled(from_scaled(expected, scale), scale) == expected


@pytest.mark.parametrize(
    "value",
    [
        "1.0000001",
        "1e3",
        "NaN",
        "Infinity",
        "+1",
        " 1",
        "1 ",
        "1.",
        ".1",
        "١.٢",
        "9223372036854.775808",
        "-9223372036854.775809",
        "9" * 51,
        "",
        1.25,
        1,
        True,
        None,
    ],
)
def test_invalid_money_never_rounds_or_coerces(value: Any) -> None:
    with pytest.raises(ValueError):
        to_scaled(value, 6)
    with pytest.raises(ValidationError):
        BrokerCreate(name="Synthetic", dividend_fee=value)


@pytest.mark.parametrize("value", [-1, "-0.000001", "1.0000001", 0.25])
def test_broker_fee_is_nonnegative_decimal_string(value: Any) -> None:
    with pytest.raises(ValidationError):
        BrokerCreate(name="Synthetic", dividend_fee=value)
    assert BrokerCreate(name="Synthetic", dividend_fee="0").dividend_fee == "0.000000"


def test_decoder_rejects_corrupted_or_boolean_scaled_values() -> None:
    invalid: list[Any] = [True, 1.5, "1000000", INT64_MAX + 1]
    for value in invalid:
        with pytest.raises(ValueError):
            from_scaled(value, 6)


@pytest.mark.parametrize(
    "changes",
    [
        {"opening_cash": "10"},
        {"opening_cash": "10", "opening_cash_date": "2026-02-30"},
        {"opening_cash": "10", "opening_cash_date": "9999-01-01"},
        {"opening_cash": "10", "opening_cash_date": "2026-01-01", "opening_fx_rate_hkd": "7.8"},
        {"opening_cash_date": "2026-01-01"},
    ],
)
def test_opening_balance_terms_are_explicit(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "portfolio_id": "11111111-1111-4111-8111-111111111111",
        "broker_id": "22222222-2222-4222-8222-222222222222",
        "name": "Synthetic",
        "default_currency": "HKD",
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        AccountCreate.model_validate(values)


@pytest.mark.parametrize("financial_type", [Quantity, PositiveQuantity, Price, Dps, Rate])
@pytest.mark.parametrize("value", [1, 0.25, True, None, "1e0", "NaN", "+1", " 1", "-0.1"])
def test_share_price_rate_inputs_reject_coercion(financial_type: Any, value: Any) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter[Any](financial_type).validate_python(value)


def test_share_price_rate_boundaries_are_exact_strings() -> None:
    quantity = TypeAdapter[Any](Quantity)
    assert quantity.validate_python("0001.25") == "1.25000000"
    assert quantity.validate_python("92233720368.54775807") == "92233720368.54775807"
    assert quantity.validate_python("0") == "0.00000000"
    for invalid in ("0.000000001", "1.000000000", "92233720368.54775808"):
        with pytest.raises(ValidationError):
            quantity.validate_python(invalid)
    for financial_type in (Price, Dps):
        assert TypeAdapter[Any](financial_type).validate_python("0.12345678") == "0.12345678"
    positive = TypeAdapter[Any](PositiveQuantity)
    with pytest.raises(ValidationError):
        positive.validate_python("0")
    assert positive.validate_python("0.00000001") == "0.00000001"
    rate = TypeAdapter[Any](Rate)
    assert rate.validate_python("0") == "0.000000"
    assert rate.validate_python("0.3") == "0.300000"
    assert rate.validate_python("1") == "1.000000"
    for invalid in ("30", "1.000001", "0.3000001"):
        with pytest.raises(ValidationError):
            rate.validate_python(invalid)
    for financial_type in (Quantity, PositiveQuantity, Price, Dps, Rate):
        adapter = TypeAdapter[Any](financial_type)
        assert isinstance(adapter.validate_json('"0.25"'), str)
        for numeric_json in ("0.25", "1", "true", "null"):
            with pytest.raises(ValidationError):
                adapter.validate_json(numeric_json)
