"""Pure accounting entry points reject malformed input without leaking a TypeError."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.dividend_evidence import DividendTerms
from spm_api.dividend_math import Accrual, Receipt, accrue
from spm_api.finance import (
    FinanceError,
    Position,
    apply_trade,
    conversion_rate,
    lock_money,
    native_settlement,
    net_asset_value,
    settle_trade,
    time_weighted_return,
    value_position,
)
from spm_api.providers.parsing import source_currency

D = Decimal
ZERO = D("0")
ONE = D("1")


@pytest.mark.parametrize("currency", [[], {}, "EUR", "CNY", None, 3])
@pytest.mark.parametrize(
    "caller",
    [
        "conversion_rate",
        "lock_money",
        "native_settlement",
        "settle_trade",
        "value_position",
        "accrue",
        "accrual",
        "receipt",
    ],
)
def test_all_pure_currency_entry_points_reject_invalid_inputs(caller: str, currency: Any) -> None:
    calls: dict[str, Callable[[], object]] = {
        "conversion_rate": lambda: conversion_rate(currency, None),
        "lock_money": lambda: lock_money(ONE, currency),
        "native_settlement": lambda: native_settlement("BUY", ONE, ONE, ZERO, ZERO, currency),
        "settle_trade": lambda: settle_trade("BUY", ONE, ONE, ZERO, ZERO, currency),
        "value_position": lambda: value_position(Position(), ONE, currency),
        "accrue": lambda: accrue("event", "account", currency, ONE, ONE, ZERO, ZERO),
        "accrual": lambda: Accrual(
            "event", "account", currency, ONE, ZERO, ZERO, ONE, ONE, ZERO, "OK"
        ),
        "receipt": lambda: Receipt("receipt", "event", "account", currency, ONE),
    }
    with pytest.raises(FinanceError) as error:
        calls[caller]()
    assert error.value.code == "UNSUPPORTED_CURRENCY"


def test_unsupported_source_currency_remains_evidence_not_an_event_currency() -> None:
    assert source_currency("CNY") == (None, "CNY")
    assert source_currency(None) == (None, None)
    with pytest.raises(ValidationError):
        DividendTerms.model_validate({"security_id": str(uuid4()), "currency": "CNY"})


@pytest.mark.parametrize("side", [[], {}, None, True, "UNKNOWN"])
@pytest.mark.parametrize("caller", ["native", "settled", "effect"])
def test_invalid_trade_sides_use_a_domain_error(caller: str, side: Any) -> None:
    calls: dict[str, Callable[[], object]] = {
        "native": lambda: native_settlement(side, ONE, ONE, ZERO, ZERO, "HKD"),
        "settled": lambda: settle_trade(side, ONE, ONE, ZERO, ZERO, "HKD"),
        "effect": lambda: apply_trade(Position(), side, ONE, ONE),
    }
    with pytest.raises(FinanceError) as error:
        calls[caller]()
    assert error.value.code == "INVALID_SIDE"


@pytest.mark.parametrize("completeness", [[], {}, None, True, "UNKNOWN_VALUE"])
@pytest.mark.parametrize("caller", ["asset_value", "return"])
def test_invalid_completeness_uses_a_domain_error(caller: str, completeness: Any) -> None:
    calls: dict[str, Callable[[], object]] = {
        "asset_value": lambda: net_asset_value(ONE, ONE, completeness),
        "return": lambda: time_weighted_return([(ONE, ONE)], completeness),
    }
    with pytest.raises(FinanceError) as error:
        calls[caller]()
    assert error.value.code == "INVALID_COMPLETENESS"
