"""Unmodified financial goldens exercised through the real pure domain functions."""

import hashlib
import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from typing import Any, NoReturn

import pytest

from spm_api.dividend_math import accrue, reconcile
from spm_api.finance import (
    ZERO,
    FinanceError,
    Position,
    apply_trade,
    exact_decimal,
    lock_money,
    net_asset_value,
    round_money,
    settle_trade,
    split,
    time_weighted_return,
    transfer,
    value_position,
)
from spm_api.replay import Event, Trade, eligible_quantity

FIXTURE = Path(__file__).resolve().parents[1] / "packages/finance-fixtures/finance_golden.json"


def reject_float(value: str) -> NoReturn:
    raise AssertionError(f"Financial fixtures must not contain JSON floats: {value}")


PACK: dict[str, Any] = json.loads(FIXTURE.read_text(), parse_float=reject_float)


def money(value: D) -> str:
    return format(round_money(value), ".6f")


def quantity(value: D) -> str:
    return format(exact_decimal(value, 8, nonnegative=True), ".8f")


def test_finance_fixture_integrity() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "caecd254d902b9493f98979dbf6c2e663a4ca608e5433ecba5e586950d555d2a"
    )
    assert PACK["numeric_mode"] == "DECIMAL_ONLY"
    assert {case["id"] for case in PACK["cases"]} == {f"FIN-{index:03d}" for index in range(1, 11)}


@pytest.mark.parametrize("case", PACK["cases"], ids=[case["id"] for case in PACK["cases"]])
def test_finance_domain_goldens(case: dict[str, Any]) -> None:
    expected = case["expected"]
    actual: dict[str, Any]
    match case["id"]:
        case "FIN-001":
            first, second = case["steps"]
            buy = settle_trade(
                "BUY",
                D(first["qty"]),
                D(first["price_usd"]),
                D(first["fee_usd"]),
                D(first["tax_usd"]),
                "USD",
                D(first["fx_hkd_per_usd"]),
            )
            opened = apply_trade(Position(), "BUY", D(first["qty"]), buy.cash.hkd).position
            sell = settle_trade(
                "SELL",
                D(second["qty"]),
                D(second["price_usd"]),
                D(second["fee_usd"]),
                D(second["tax_usd"]),
                "USD",
                D(second["fx_hkd_per_usd"]),
            )
            closed = apply_trade(opened, "SELL", D(second["qty"]), sell.cash.hkd)
            actual = {
                "buy_settlement_usd": money(buy.cash.local),
                "buy_booked_cost_hkd": money(buy.cash.hkd),
                "sell_settlement_usd": money(sell.cash.local),
                "sell_cash_in_hkd": money(sell.cash.hkd),
                "allocated_cost_hkd": money(closed.allocated_cost_hkd),
                "realized_hkd": money(closed.realized_hkd),
                "quantity": quantity(closed.position.quantity),
                "remaining_cost_hkd": money(closed.position.cost_hkd),
            }
        case "FIN-002":
            source = case["initial"]["account_A"]
            target = case["initial"]["account_B"]
            moved = transfer(
                Position(D(source["qty"]), D(source["cost_hkd"])),
                Position(D(target["qty"]), D(target["cost_hkd"])),
                D(case["action"]["qty"]),
            )
            actual = {
                "moved_cost_hkd": money(moved.moved_cost_hkd),
                "account_A_qty": quantity(moved.source.quantity),
                "account_A_cost_hkd": money(moved.source.cost_hkd),
                "account_B_qty": quantity(moved.target.quantity),
                "account_B_cost_hkd": money(moved.target.cost_hkd),
                "realized_hkd": money(moved.source.realized_hkd + moved.target.realized_hkd),
            }
        case "FIN-003":
            initial, action = case["initial"], case["action"]
            position = split(
                Position(D(initial["qty"]), D(initial["cost_hkd"])),
                action["numerator"],
                action["denominator"],
            )
            assert position.average_cost_hkd is not None
            actual = {
                "qty": quantity(position.quantity),
                "cost_hkd": money(position.cost_hkd),
                "avg_cost_hkd_per_share": money(position.average_cost_hkd),
            }
        case "FIN-004":
            # Explicit zero-cost synthetic facts isolate the fixture's quantity-only eligibility.
            events = [
                Event(
                    str(index),
                    date.fromisoformat(row["date"]),
                    index,
                    Trade("A", "security", row["type"], D(row["qty"]), ZERO),
                )
                for index, row in enumerate(case["trades"], 1)
            ]
            eligible = eligible_quantity(
                events, "A", "security", date.fromisoformat(case["event"]["ex_date"])
            )
            actual = {
                "eligible_qty": quantity(eligible),
                "gross_currency": money(eligible * D(case["event"]["dps"])),
            }
        case "FIN-005":
            event = case["event"]
            dividend = accrue(
                "event",
                "A",
                "USD",
                D(event["eligible_qty"]),
                D(event["dps_usd"]),
                D(event["withholding_rate"]),
                D(event["fee_usd"]),
                D(event["fx_hkd_per_usd"]),
            )
            reconciled = reconcile([dividend], [])[0]
            assert dividend.booked_hkd is not None
            actual = {
                "gross_usd": money(dividend.gross_local),
                "tax_usd": money(dividend.tax_local),
                "fee_usd": money(dividend.fee_local),
                "expected_net_usd": money(dividend.net_local),
                "expected_hkd": money(dividend.booked_hkd),
                "receipt_state": reconciled.receipt_state,
            }
        case "FIN-006":
            old = case["old"]
            posted = lock_money(D(old["usd_amount"]), "USD", D(old["booked_fx"]))
            assert money(posted.hkd) == old["booked_hkd"]
            new_posting = lock_money(D(old["usd_amount"]), "USD", D(case["new_current_fx"]))
            assert new_posting.hkd != posted.hkd
            actual = {"booked_hkd_after_refresh": money(posted.hkd)}
        case "FIN-007":
            initial = case["position"]
            valued = value_position(
                Position(D(initial["qty"]), D(initial["cost_hkd"])), initial["quote"], "HKD"
            )
            actual = {
                "market_value_hkd": valued.market_value_hkd,
                "unrealized_hkd": valued.unrealized_hkd,
                "total_pnl_hkd": valued.total_pnl_hkd,
                "quality": valued.quality,
            }
        case "FIN-008":
            initial = Position(D(case["initial"]["qty"]), D(case["initial"]["cost_hkd"]))
            with pytest.raises(FinanceError) as raised:
                apply_trade(initial, "SELL", D(case["action"]["qty"]), ZERO)
            assert raised.value.code == expected["error"]
            assert quantity(initial.quantity) == expected["remaining_qty"]
            assert money(initial.cost_hkd) == case["initial"]["cost_hkd"]
            # SQL mutated_rows remains an integration gate; a pure function cannot prove rollback.
            return
        case "FIN-009":
            portfolio = case["portfolio"]
            nav = net_asset_value(
                D(portfolio["securities_market_value_hkd"]),
                D(portfolio["cash_balance_hkd"]),
                portfolio["cash_completeness"],
            )
            twr = time_weighted_return(None, portfolio["cash_completeness"])
            assert nav.quality == twr.quality
            actual = {"nav_hkd": nav.value, "twr": twr.value, "quality": nav.quality}
        case "FIN-010":
            holdings = [
                Event("buy", date(2026, 9, 1), 1, Trade("A", "security", "BUY", D("10"), ZERO))
            ]
            accruals = [
                accrue(
                    row["identity"],
                    "A",
                    "USD",
                    eligible_quantity(
                        holdings, "A", "security", date.fromisoformat(row["ex_date"])
                    ),
                    D(row["dps_usd"]),
                    ZERO,
                    ZERO,
                )
                for row in case["events"]
            ]
            reconciled_events = reconcile(accruals, [])
            eligible = eligible_quantity(
                holdings, "A", "security", date.fromisoformat(case["events"][0]["ex_date"])
            )
            actual = {
                "events_count": len(reconciled_events),
                "eligible_qty_each": quantity(eligible),
                "gross_total_usd": money(
                    sum((item.accrual.gross_local for item in reconciled_events), ZERO)
                ),
            }
        case _:
            pytest.fail("Unimplemented authoritative financial golden")
    assert actual == expected
