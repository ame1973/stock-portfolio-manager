"""Exact SQLite decimal codecs and string-only API financial types."""

import re
from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated

from pydantic import AfterValidator, Field

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def to_scaled(value: str, scale: int) -> int:
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Financial values must be bounded decimal strings")
    fraction = rf"(?:\.[0-9]{{1,{scale}}})?" if scale else ""
    if re.fullmatch(rf"-?[0-9]+{fraction}", value) is None:
        raise ValueError("Invalid decimal syntax or precision")
    with localcontext() as context:
        context.prec = 64
        result = int(Decimal(value).scaleb(scale))
    if not INT64_MIN <= result <= INT64_MAX:
        raise ValueError("Financial value exceeds INT64 storage")
    return result


def from_scaled(value: int, scale: int) -> str:
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise ValueError("Expected an INT64 scaled integer")
    with localcontext() as context:
        context.prec = 64
        return format(Decimal(value).scaleb(-scale), f".{scale}f")


def _money(value: str) -> str:
    return from_scaled(to_scaled(value, 6), 6)


def _nonnegative(value: str) -> str:
    if Decimal(value) < 0:
        raise ValueError("Amount must be nonnegative")
    return value


def _fx(value: str) -> str:
    encoded = to_scaled(value, 12)
    if encoded <= 0:
        raise ValueError("FX must be positive")
    return from_scaled(encoded, 12)


def _e8(value: str) -> str:
    return _nonnegative(from_scaled(to_scaled(value, 8), 8))


def _positive(value: str) -> str:
    if Decimal(value) <= 0:
        raise ValueError("Value must be positive")
    return value


def _rate(value: str) -> str:
    encoded = to_scaled(value, 6)
    if not 0 <= encoded <= 1_000_000:
        raise ValueError("A rate is a fraction between zero and one")
    return from_scaled(encoded, 6)


def _date(value: str) -> str:
    date.fromisoformat(value)
    return value


Money = Annotated[
    str,
    Field(strict=True, max_length=50, pattern=r"^-?[0-9]+(\.[0-9]{1,6})?$"),
    AfterValidator(_money),
]
NonNegativeMoney = Annotated[Money, AfterValidator(_nonnegative)]
Quantity = Annotated[
    str,
    Field(strict=True, max_length=50, pattern=r"^[0-9]+(\.[0-9]{1,8})?$"),
    AfterValidator(_e8),
]
PositiveQuantity = Annotated[Quantity, AfterValidator(_positive)]
Price = Quantity
Dps = Quantity
Rate = Annotated[
    str,
    Field(strict=True, max_length=50, pattern=r"^[0-9]+(\.[0-9]{1,6})?$"),
    AfterValidator(_rate),
]
FxRate = Annotated[
    str,
    Field(strict=True, max_length=50, pattern=r"^[0-9]+(\.[0-9]{1,12})?$"),
    AfterValidator(_fx),
]
IsoDate = Annotated[
    str,
    Field(
        strict=True, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", json_schema_extra={"format": "date"}
    ),
    AfterValidator(_date),
]
