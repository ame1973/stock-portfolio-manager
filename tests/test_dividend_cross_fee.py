"""Foreign dividend fees retain their precision until each result is persisted."""

from decimal import ROUND_DOWN, Decimal, localcontext
from fractions import Fraction
from itertools import product
from typing import Any

import pytest

from spm_api.dividend_math import Accrual, accrue
from spm_api.finance import FinanceError
from spm_api.master_models import Currency

D = Decimal


def rounded_micro(value: Fraction) -> int:
    scaled = value * 1_000_000
    sign = -1 if scaled < 0 else 1
    return sign * ((2 * abs(scaled.numerator) + scaled.denominator) // (2 * scaled.denominator))


def calculate(**changes: Any) -> Accrual:
    values: dict[str, Any] = {
        "event_id": "synthetic-event",
        "account_id": "synthetic-account",
        "currency": "USD",
        "eligible_quantity": D("1"),
        "dps": D("1"),
        "tax_rate": D("0"),
        "fee": D("0.000001"),
        "rate": D("7.8"),
        "fee_currency": "HKD",
        "fee_rate_event": D("0.5"),
        **changes,
    }
    return accrue(**values)


def test_fee_conversion_is_not_rounded_before_net_or_hkd() -> None:
    result = calculate()
    assert result.gross_local == D("1.000000")
    assert result.fee_local == D("0.000001")
    assert result.net_local == D("1.000000")
    assert result.rounding_delta_local == D("0.000001")
    assert result.booked_hkd == D("7.799996")
    assert result.quality == "OK"


def test_reverse_fee_conversion_uses_event_currency_per_fee_currency() -> None:
    result = calculate(currency="HKD", fee_currency="USD", fee_rate_event=D("7.8"), rate=None)
    assert result.fee_local == D("0.000008")
    assert result.net_local == result.booked_hkd == D("0.999992")
    assert result.rounding_delta_local == D("0.000000")


@pytest.mark.parametrize("fee_currency", [None, "USD"])
@pytest.mark.parametrize("fee_rate_event", [None, D("1")])
def test_same_currency_fee_retains_identity_semantics(
    fee_currency: str | None, fee_rate_event: Decimal | None
) -> None:
    result = calculate(fee_currency=fee_currency, fee_rate_event=fee_rate_event, fee=D("0.5"))
    assert result.fee_local == D("0.500000")
    assert result.net_local == D("0.500000") and result.booked_hkd == D("3.900000")


def test_explicit_zero_foreign_fee_needs_no_invented_conversion_rate() -> None:
    result = calculate(fee=D("0"), fee_rate_event=None, rate=None)
    assert result.fee_local == D("0.000000") and result.net_local == D("1.000000")
    assert result.booked_hkd is None and result.quality == "MISSING_FX"


def test_nonzero_foreign_fee_with_unknown_conversion_is_not_zero() -> None:
    with pytest.raises(FinanceError) as failure:
        calculate(fee_rate_event=None)
    assert failure.value.code == "MISSING_FX"


def test_valuation_fx_and_fee_fx_are_independent() -> None:
    result = calculate(rate=None)
    assert result.net_local == D("1.000000") and result.booked_hkd is None
    assert result.quality == "MISSING_FX"


def test_negative_unrounded_net_requires_review_even_when_stored_as_zero() -> None:
    result = calculate(eligible_quantity=D("0.000001"), fee_rate_event=D("1.000001"))
    assert result.net_local == D("0.000000")
    assert result.quality == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"fee_currency": "EUR"}, "UNSUPPORTED_CURRENCY"),
        ({"fee_currency": "CNY", "fee": D("0")}, "UNSUPPORTED_CURRENCY"),
        ({"fee_currency": []}, "UNSUPPORTED_CURRENCY"),
        ({"fee_currency": {}}, "UNSUPPORTED_CURRENCY"),
        ({"fee_currency": True}, "UNSUPPORTED_CURRENCY"),
        ({"currency": []}, "UNSUPPORTED_CURRENCY"),
        ({"fee_currency": "USD", "fee_rate_event": D("0.5")}, "CURRENCY_MISMATCH"),
        ({"fee_rate_event": D("0")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee": D("0"), "fee_rate_event": D("0")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee_rate_event": D("-1")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee_rate_event": D("0.1234567890123")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee_rate_event": D("9223372.036854775808")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee_rate_event": D("NaN")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee_rate_event": D("Infinity")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee_rate_event": 0.5}, "INVALID_FINANCIAL_VALUE"),
        ({"fee": D("0.0000001")}, "INVALID_FINANCIAL_VALUE"),
        ({"fee": D("-0.000001")}, "INVALID_FINANCIAL_VALUE"),
    ],
)
def test_bad_fee_inputs_fail_without_implicit_conversion_or_rounding(
    changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(FinanceError) as failure:
        calculate(**changes)
    assert failure.value.code == code


def test_converted_fee_overflow_fails_at_the_persistence_boundary() -> None:
    with pytest.raises(FinanceError) as failure:
        calculate(fee=D("9223372036854.775807"), fee_rate_event=D("2"))
    assert failure.value.code == "FINANCIAL_OVERFLOW"


def test_valuation_overflow_is_not_hidden_by_a_valid_zero_foreign_fee() -> None:
    with pytest.raises(FinanceError) as failure:
        calculate(
            eligible_quantity=D("1000000.00000001"),
            fee=D("0"),
            fee_rate_event=None,
            rate=D("9223372.036854775807"),
        )
    assert failure.value.code == "FINANCIAL_OVERFLOW"


@pytest.mark.parametrize(
    ("quantity", "price", "tax", "fee", "fee_rate", "valuation_rate"),
    [
        (
            "92233720368.54775807",
            "100",
            "0",
            "0.000001",
            "0.000000000001",
            "0.999999999999",
        ),
        (
            "92233720368.54775807",
            "99.99999999",
            "0",
            "9223372036854.775807",
            "0.999999999999",
            "9223372.036854775807",
        ),
        ("0", "0", "1", "9223372036854.775807", "0.000000000001", "9223372.036854775807"),
    ],
)
def test_extreme_exact_inputs_match_rational_oracle(
    quantity: str, price: str, tax: str, fee: str, fee_rate: str, valuation_rate: str
) -> None:
    result = calculate(
        eligible_quantity=D(quantity),
        dps=D(price),
        tax_rate=D(tax),
        fee=D(fee),
        fee_rate_event=D(fee_rate),
        rate=D(valuation_rate),
    )
    gross = Fraction(quantity) * Fraction(price)
    tax_value = gross * Fraction(tax)
    converted_fee = Fraction(fee) * Fraction(fee_rate)
    net = gross - tax_value - converted_fee
    gm, tm, fm, nm = map(rounded_micro, (gross, tax_value, converted_fee, net))
    assert result.gross_local == D(gm).scaleb(-6)
    assert result.tax_local == D(tm).scaleb(-6)
    assert result.fee_local == D(fm).scaleb(-6)
    assert result.net_local == D(nm).scaleb(-6)
    assert result.rounding_delta_local == D(nm - (gm - tm - fm)).scaleb(-6)
    assert result.booked_hkd == D(rounded_micro(net * Fraction(valuation_rate))).scaleb(-6)
    assert result.quality == ("NEEDS_REVIEW" if net < 0 else "OK")


def test_explicit_cross_currency_rate_one_is_not_an_assumed_identity() -> None:
    result = calculate(fee=D("0.5"), fee_rate_event=D("1"))
    assert result.fee_local == D("0.500000")
    assert result.net_local == D("0.500000") and result.booked_hkd == D("3.900000")


def test_original_positional_calls_match_explicit_identity_fee() -> None:
    currencies: tuple[Currency, ...] = ("HKD", "USD")
    for currency, rate, quantity, price, tax, fee in product(
        currencies,
        (None, D("7.8")),
        (D("0"), D("0.000001"), D("1.00000001")),
        (D("0"), D("0.5"), D("1.23456789")),
        (D("0"), D("0.333333")),
        (D("0"), D("0.000001"), D("1.234567")),
    ):
        if currency == "HKD" and rate is not None:
            continue
        original = accrue("event", "account", currency, quantity, price, tax, fee, rate)
        gross = Fraction(quantity) * Fraction(price)
        tax_value = gross * Fraction(tax)
        net = gross - tax_value - Fraction(fee)
        gm, tm, fm, nm = map(rounded_micro, (gross, tax_value, Fraction(fee), net))
        assert original.gross_local == D(gm).scaleb(-6)
        assert original.tax_local == D(tm).scaleb(-6)
        assert original.fee_local == D(fm).scaleb(-6)
        assert original.net_local == D(nm).scaleb(-6)
        assert original.rounding_delta_local == D(nm - (gm - tm - fm)).scaleb(-6)
        fx = Fraction(1) if currency == "HKD" else Fraction(rate) if rate is not None else None
        assert original.booked_hkd == (
            D(rounded_micro(net * fx)).scaleb(-6) if fx is not None else None
        )
        assert original.quality == (
            "NEEDS_REVIEW" if net < 0 else "MISSING_FX" if fx is None else "OK"
        )
        assert (
            accrue(
                "event",
                "account",
                currency,
                quantity,
                price,
                tax,
                fee,
                rate,
                fee_currency=currency,
                fee_rate_event=D("1"),
            )
            == original
        )


def test_caller_decimal_context_cannot_change_fee_or_hkd_rounding() -> None:
    normal = calculate(eligible_quantity=D("123.45678901"), dps=D("0.98765432"))
    with localcontext(prec=3, rounding=ROUND_DOWN):
        assert calculate(eligible_quantity=D("123.45678901"), dps=D("0.98765432")) == normal


def test_exact_rational_oracle_across_amounts_rates_and_signs() -> None:
    quantities = ("0", "0.00000001", "0.000001", "1.00000001", "100")
    prices = ("0", "0.00000001", "0.50000000", "1.23456789")
    taxes = ("0", "0.333333", "1")
    fees = ("0", "0.000001", "1.234567")
    fee_rates = ("0.000000000001", "0.5", "1.234567890123", "7.8")
    valuation_rates = (None, "7.812345678901")
    for quantity, price, tax, fee, fee_rate, valuation_rate in product(
        quantities, prices, taxes, fees, fee_rates, valuation_rates
    ):
        result = calculate(
            eligible_quantity=D(quantity),
            dps=D(price),
            tax_rate=D(tax),
            fee=D(fee),
            fee_rate_event=D(fee_rate),
            rate=D(valuation_rate) if valuation_rate else None,
        )
        gross = Fraction(quantity) * Fraction(price)
        tax_value = gross * Fraction(tax)
        converted_fee = Fraction(fee) * Fraction(fee_rate)
        net = gross - tax_value - converted_fee
        gm, tm, fm, nm = map(rounded_micro, (gross, tax_value, converted_fee, net))
        assert result.gross_local == D(gm).scaleb(-6)
        assert result.tax_local == D(tm).scaleb(-6)
        assert result.fee_local == D(fm).scaleb(-6)
        assert result.net_local == D(nm).scaleb(-6)
        assert result.rounding_delta_local == D(nm - (gm - tm - fm)).scaleb(-6)
        if valuation_rate is None:
            assert result.booked_hkd is None
        else:
            assert result.booked_hkd == D(rounded_micro(net * Fraction(valuation_rate))).scaleb(-6)
        assert result.quality == (
            "NEEDS_REVIEW" if net < 0 else "MISSING_FX" if valuation_rate is None else "OK"
        )
