"""Financial boundaries and deterministic replay without database or HTTP side effects."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, localcontext
from decimal import Decimal as D
from typing import Any

import pytest

from spm_api.decimal_types import INT64_MAX, from_scaled
from spm_api.finance import (
    ZERO,
    FinanceError,
    Position,
    apply_trade,
    lock_money,
    net_asset_value,
    round_money,
    settle_trade,
    split,
    time_weighted_return,
    transfer,
    value_position,
)
from spm_api.master_models import Currency
from spm_api.replay import Event, Split, Trade, Transfer, eligible_quantity, replay

DAY = date(2026, 1, 1)


def event(
    identifier: str,
    sequence: int,
    operation: Trade | Transfer | Split,
    days: int = 0,
    executed_at: datetime | None = None,
    active: bool = True,
) -> Event:
    return Event(identifier, DAY + timedelta(days=days), sequence, operation, executed_at, active)


def test_partial_cost_is_not_rounded_before_persistence_and_closeout_has_no_dust() -> None:
    position = Position(D("1"), D("0.000001"))
    first = apply_trade(position, "SELL", D("0.33333333"), ZERO)
    assert first.position.cost_hkd == D("0.00000066666667")
    second = apply_trade(first.position, "SELL", D("0.33333333"), ZERO)
    assert second.position.cost_hkd == D("0.00000033333334")
    final = apply_trade(second.position, "SELL", D("0.33333334"), ZERO)
    assert final.position.quantity == ZERO
    assert final.position.cost_hkd == ZERO
    assert final.position.realized_hkd == D("-0.000001")
    assert position.cost_hkd == D("0.000001")


def test_calculations_are_independent_of_the_ambient_decimal_context() -> None:
    with localcontext(prec=7, rounding=ROUND_DOWN):
        cash = settle_trade(
            "BUY", D("10"), D("100.12345678"), D("1.000001"), ZERO, "USD", D("7.812345678901")
        )
        assert cash.gross_local == D("1001.23456780")
        assert cash.cash.local == D("1002.230000")
        assert cash.cash.hkd == D("7829.767210")
        assert round_money(D("1.0000005")) == D("1.000001")
        assert round_money(D("-1.0000005")) == D("-1.000001")


def test_settlement_defaults_to_cents_but_actual_broker_cash_is_not_rounded() -> None:
    calculated = settle_trade("BUY", D("0.1"), D("0.15"), ZERO, ZERO, "HKD")
    assert calculated.cash.local == D("0.020000")
    assert calculated.rounding_delta_local == D("0.005")
    actual = settle_trade(
        "BUY",
        D("0.1"),
        D("0.15"),
        ZERO,
        ZERO,
        "HKD",
        actual_local=D("0.015001"),
        reason="Synthetic actual broker settlement",
    )
    assert actual.cash.local == actual.cash.hkd == D("0.015001")
    assert actual.rounding_delta_local == D("0.000001")
    with pytest.raises(FinanceError, match="evidence"):
        settle_trade("BUY", D("1"), D("1"), ZERO, ZERO, "HKD", actual_local=D("1"))


def test_sell_fees_can_exceed_proceeds_without_being_clamped_to_zero() -> None:
    cash = settle_trade("SELL", D("1"), ZERO, D("0.005"), ZERO, "HKD")
    assert cash.cash.local == D("-0.010000")
    result = apply_trade(Position(D("1"), D("0.01")), "SELL", D("1"), cash.cash.hkd)
    assert result.position.quantity == ZERO
    assert result.realized_hkd == D("-0.02")


def test_locked_fx_and_actual_hkd_override_are_immutable_facts() -> None:
    original = lock_money(
        D("10"),
        "USD",
        D("7.8"),
        actual_hkd=D("77"),
        reason="Synthetic actual HKD settlement confirmed",
    )
    assert original.fx_rate_hkd == D("7.8")
    assert original.hkd == D("77")
    assert lock_money(D("10"), "USD", D("7.9")).hkd == D("79")
    with pytest.raises(FrozenInstanceError):
        original.__setattr__("hkd", D("79"))
    assert original.hkd == D("77")
    fx_cases: tuple[tuple[Currency, D | None], ...] = (("USD", None), ("HKD", D("7.8")))
    for currency, rate in fx_cases:
        with pytest.raises(FinanceError):
            lock_money(D("10"), currency, rate)
    with pytest.raises(FinanceError, match="reverse"):
        lock_money(
            D("10"), "USD", D("7.8"), actual_hkd=D("-77"), reason="Synthetic evidence reviewed"
        )


@pytest.mark.parametrize("value", [0.5, 1, True, None, D("NaN"), D("sNaN"), D("Infinity")])
def test_domain_never_accepts_float_or_nonfinite_money(value: Any) -> None:
    with pytest.raises(FinanceError):
        round_money(value)
    with pytest.raises(FinanceError):
        Position(D("1"), value)
    with pytest.raises(FinanceError):
        apply_trade(Position(), "BUY", D("1"), value)


@pytest.mark.parametrize("quantity", [D("0"), D("-1"), D("0.000000001"), 0.5])
def test_trade_quantities_are_positive_exact_shares(quantity: Any) -> None:
    with pytest.raises(FinanceError):
        apply_trade(Position(), "BUY", quantity, D("1"))


def test_aggregate_money_and_quantity_overflows_reject_without_mutating_inputs() -> None:
    maximum = D(from_scaled(INT64_MAX, 6))
    original = Position(D("1"), maximum)
    with pytest.raises(FinanceError):
        apply_trade(original, "BUY", D("1"), D("0.000001"))
    assert original.cost_hkd == maximum
    source = Position(D("1"), D("1"))
    target = Position(D(from_scaled(INT64_MAX, 8)))
    with pytest.raises(FinanceError):
        transfer(source, target, D("1"))
    assert source == Position(D("1"), D("1"))
    assert target.quantity == D(from_scaled(INT64_MAX, 8))
    with pytest.raises(FinanceError):
        settle_trade("BUY", D("10000000000"), D("10000000000"), ZERO, ZERO, "HKD")
    with pytest.raises(FinanceError):
        round_money(D("1e100"))


def test_transfer_preserves_existing_cost_and_realized_profit_in_both_accounts() -> None:
    source, target = Position(D("3"), D("10"), D("7")), Position(D("2"), D("20"), D("9"))
    moved = transfer(source, target, D("1"))
    with localcontext(prec=64):
        assert moved.source.cost_hkd + moved.target.cost_hkd == D("30")
    assert moved.source.realized_hkd == D("7")
    assert moved.target.realized_hkd == D("9")
    closed = transfer(source, target, D("3"))
    assert closed.source.cost_hkd == ZERO
    assert closed.target.cost_hkd == D("30")
    assert closed.moved_cost_hkd == D("10")
    with pytest.raises(FinanceError) as raised:
        transfer(source, target, D("4"))
    assert raised.value.code == "INSUFFICIENT_SHARES"
    assert source.quantity == D("3")


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [(True, 2), (1.5, 2), (1, "2"), (0, 2), (1, 0), (-1, 2), (INT64_MAX + 1, 1)],
)
def test_split_ratios_require_bounded_positive_integers(numerator: Any, denominator: Any) -> None:
    with pytest.raises(FinanceError) as raised:
        split(Position(D("10"), D("100")), numerator, denominator)
    assert raised.value.code == "INVALID_RATIO"


def test_reverse_split_is_exact_and_fractional_residue_needs_explicit_review() -> None:
    original = Position(D("10"), D("100"))
    reversed_position = split(original, 1, 5)
    assert reversed_position.quantity == D("2")
    assert reversed_position.cost_hkd == D("100")
    assert reversed_position.average_cost_hkd == D("50")
    for position, numerator, denominator in (
        (original, 1, 3),
        (Position(D("0.00000001"), D("1")), 3, 2),
    ):
        with pytest.raises(FinanceError) as raised:
            split(position, numerator, denominator)
        assert raised.value.code == "NEEDS_REVIEW"
    with pytest.raises(FinanceError) as raised:
        split(Position(D(from_scaled(INT64_MAX, 8))), 2, 1)
    assert raised.value.code == "FINANCIAL_OVERFLOW"
    assert original == Position(D("10"), D("100"))


def test_empty_positions_have_no_average_and_cannot_retain_orphan_cost() -> None:
    assert Position().average_cost_hkd is None
    with pytest.raises(FinanceError):
        Position(ZERO, D("1"))
    with pytest.raises(FinanceError):
        Position(D("1"), D("-1"))


def test_account_security_subledgers_are_independent() -> None:
    events = [
        event("A-buy", 1, Trade("A", "stock", "BUY", D("10"), D("100"))),
        event("B-buy", 2, Trade("B", "stock", "BUY", D("10"), D("200"))),
        event("A-sell", 3, Trade("A", "stock", "SELL", D("5"), D("100"))),
        event("other-security", 4, Trade("A", "other", "BUY", D("1"), D("7"))),
    ]
    result = replay(events, as_of=DAY)
    assert result["A", "stock"] == Position(D("5"), D("50"), D("50"))
    assert result["B", "stock"] == Position(D("10"), D("200"))
    assert result["A", "other"] == Position(D("1"), D("7"))
    assert replay(list(reversed(events)), as_of=DAY) == result


def test_backdated_insert_replays_cost_without_rewriting_the_original_facts() -> None:
    original = [
        event("buy", 1, Trade("A", "stock", "BUY", D("10"), D("100"))),
        event("sell", 2, Trade("A", "stock", "SELL", D("5"), D("100")), 2),
    ]
    assert replay(original, as_of=DAY + timedelta(days=2))["A", "stock"].realized_hkd == D("50")
    backdated = event("backdated", 3, Trade("A", "stock", "BUY", D("10"), D("300")), 1)
    result = replay([*original, backdated], as_of=DAY + timedelta(days=2))
    assert result["A", "stock"] == Position(D("15"), D("300"), ZERO)
    assert original[1].operation == Trade("A", "stock", "SELL", D("5"), D("100"))
    assert eligible_quantity([*original, backdated], "A", "stock", DAY + timedelta(days=2)) == D(
        "20"
    )


def test_replay_uses_execution_then_sequence_then_identity_not_uuid_creation_order() -> None:
    buy = event("z-buy", 2, Trade("A", "stock", "BUY", D("2"), D("10")))
    sell = event("a-sell", 3, Trade("A", "stock", "SELL", D("1"), D("8")))
    assert replay([sell, buy], as_of=DAY)["A", "stock"] == Position(D("1"), D("5"), D("3"))
    early = datetime(2026, 1, 1, 9, tzinfo=UTC)
    late = early + timedelta(hours=1)
    timed_buy = event("timed-buy", 9, buy.operation, executed_at=early)
    timed_sell = event("timed-sell", 1, sell.operation, executed_at=late)
    assert replay([timed_sell, timed_buy], as_of=DAY)["A", "stock"] == Position(
        D("1"), D("5"), D("3")
    )
    unknown_time_buy = event("unknown", 9, buy.operation)
    assert replay([timed_sell, unknown_time_buy], as_of=DAY)["A", "stock"].quantity == D("1")


def test_confirmed_opening_split_and_transfer_preserve_cost() -> None:
    events = [
        event("buy", 1, Trade("A", "stock", "BUY", D("10"), D("100"))),
        event("same-day-buy", 2, Trade("A", "stock", "BUY", D("10"), D("100")), 1),
        event("split", 99, Split("stock", 2, 1), 1),
        event("transfer", 100, Transfer("A", "B", "stock", D("15")), 1),
    ]
    result = replay(events, as_of=DAY + timedelta(days=1))
    assert result["A", "stock"] == Position(D("15"), D("100"))
    assert result["B", "stock"] == Position(D("15"), D("100"))
    assert eligible_quantity(events, "A", "stock", DAY + timedelta(days=1)) == D("10")


def test_inactive_revisions_and_future_trades_do_not_affect_current_positions() -> None:
    events = [
        event("superseded", 1, Trade("A", "stock", "BUY", D("100"), D("100")), active=False),
        event("current", 2, Trade("A", "stock", "BUY", D("2"), D("20"))),
        event("future-sell", 3, Trade("A", "stock", "SELL", D("2"), D("50")), 10),
    ]
    assert replay(events, as_of=DAY)["A", "stock"] == Position(D("2"), D("20"))
    assert replay(events, as_of=DAY + timedelta(days=10))["A", "stock"] == Position(
        ZERO, ZERO, D("30")
    )
    assert eligible_quantity(events, "A", "stock", DAY) == ZERO


def test_replay_rejects_duplicate_facts_overselling_and_same_account_transfer() -> None:
    buy = event("buy", 1, Trade("A", "stock", "BUY", D("1"), D("1")))
    with pytest.raises(FinanceError) as duplicate:
        replay([buy, buy], as_of=DAY)
    assert duplicate.value.code == "DUPLICATE_FACT"
    with pytest.raises(FinanceError) as oversell:
        replay([buy, event("sell", 2, Trade("A", "stock", "SELL", D("2"), D("3")))], as_of=DAY)
    assert oversell.value.code == "INSUFFICIENT_SHARES"
    with pytest.raises(FinanceError):
        replay([buy, event("transfer", 2, Transfer("A", "A", "stock", D("1")))], as_of=DAY)
    assert buy.operation == Trade("A", "stock", "BUY", D("1"), D("1"))


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": True},
        {"sequence": 0},
        {"sequence": INT64_MAX + 1},
        {"id": ""},
        {"day": datetime(2026, 1, 1)},
        {"active": 1},
        {"executed_at": datetime(2026, 1, 1)},
    ],
)
def test_replay_requires_explicit_calendar_order_and_aware_execution_time(
    changes: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "id": "event",
        "day": DAY,
        "sequence": 1,
        "operation": Trade("A", "stock", "BUY", D("1"), D("1")),
    }
    values.update(changes)
    with pytest.raises(FinanceError):
        Event(**values)


def test_valid_zero_quote_is_distinct_from_missing_price_or_fx() -> None:
    position = Position(D("10"), D("100"))
    priced_zero = value_position(position, ZERO, "HKD")
    assert priced_zero.market_value_hkd == ZERO
    assert priced_zero.unrealized_hkd == D("-100")
    assert priced_zero.quality == "OK"
    assert value_position(position, None, "HKD").quality == "MISSING_QUOTE"
    missing_fx = value_position(position, D("1"), "USD")
    assert missing_fx.market_value_hkd is None
    assert missing_fx.total_pnl_hkd is None
    assert missing_fx.quality == "MISSING_FX"
    closed = value_position(Position(ZERO, ZERO, D("5")), None, "USD")
    assert closed.total_pnl_hkd == D("5")


def test_economic_pnl_uses_accrual_not_receipts_and_subtracts_only_separate_expenses() -> None:
    valued = value_position(
        Position(D("10"), D("100"), D("5")),
        D("12"),
        "HKD",
        recognized_dividend_hkd=D("11"),
        operating_expenses_hkd=D("2"),
    )
    assert valued.market_value_hkd == D("120")
    assert valued.unrealized_hkd == D("20")
    assert valued.total_pnl_hkd == D("34")
    assert net_asset_value(ZERO, D("6"), "COMPLETE", receivables_hkd=D("5")).value == D("11")
    assert net_asset_value(
        D("120"), D("10"), "COMPLETE", receivables_hkd=D("5"), liabilities_hkd=D("2")
    ).value == D("133")
    assert net_asset_value(None, D("10"), "COMPLETE").value is None
    assert net_asset_value(D("120"), None, "COMPLETE").value is None


def test_twr_chains_flow_boundary_subperiods_and_rejects_missing_or_zero_openings() -> None:
    periods = [(D("100"), D("110")), (D("210"), D("231"))]
    assert time_weighted_return(periods, "COMPLETE").value == D("0.21")
    assert time_weighted_return(periods, "PARTIAL").value is None
    assert time_weighted_return(None, "COMPLETE").quality == "NOT_COMPUTABLE"
    assert time_weighted_return([(ZERO, D("1"))], "COMPLETE").value is None
    assert time_weighted_return([(D("1"), D("-1"))], "COMPLETE").value is None
    assert time_weighted_return([(D("1"), ZERO)], "COMPLETE").value == D("-1")
