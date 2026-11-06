"""Bounded exact numeric conversion for upstream tokens, without financial input rounding."""

import re
from decimal import Decimal
from fractions import Fraction

from spm_api.decimal_types import INT64_MAX, from_scaled, to_scaled


def exact_number(value: object) -> Decimal:
    if isinstance(value, str):
        if len(value) > 64 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
            raise ValueError("Provider numeric string has unsupported syntax")
        number = Decimal(value)
    elif type(value) is int:
        if len(str(value)) > 64:
            raise ValueError("Provider integer is unbounded")
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    else:
        raise ValueError("Provider numbers must be exact tokens, never float or bool")
    if not number.is_finite() or number < 0:
        raise ValueError("Provider number must be finite and nonnegative")
    if number == 0:
        return Decimal(0)
    parts = number.as_tuple()
    if not isinstance(parts.exponent, int) or abs(parts.exponent) > 64 or len(parts.digits) > 64:
        raise ValueError("Provider numeric precision or exponent is unbounded")
    if abs(number.adjusted()) > 64:
        raise ValueError("Provider numeric magnitude is unbounded")
    return number


def dps_string(value: object) -> str | None:
    if value is None:
        return None
    number = exact_number(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return from_scaled(to_scaled(text, 8), 8)


def ratio_terms(new: object, old: object) -> tuple[int, int]:
    numerator, denominator = exact_number(new), exact_number(old)
    if numerator <= 0 or denominator <= 0:
        raise ValueError("Provider split terms must be positive")
    ratio = Fraction(numerator) / Fraction(denominator)
    if ratio.numerator > INT64_MAX or ratio.denominator > INT64_MAX:
        raise ValueError("Reduced provider split exceeds INT64 terms")
    return ratio.numerator, ratio.denominator


def split_ratio(value: object) -> tuple[int, int]:
    if not isinstance(value, str) or len(value) > 129 or value.count("/") != 1:
        raise ValueError("Provider split must contain bounded new/old terms")
    first, second = value.split("/")
    return ratio_terms(first, second)
