"""Pure Decimal accounting; inputs are exact, only persisted derived money is rounded."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Literal

from spm_api.decimal_types import INT64_MAX, from_scaled, to_scaled
from spm_api.master_models import Completeness, Currency

ZERO = Decimal("0")
ONE = Decimal("1")
Side = Literal["BUY", "SELL"]
Quality = Literal["OK", "MISSING_QUOTE", "MISSING_FX", "NOT_COMPUTABLE", "NEEDS_REVIEW"]


class FinanceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def finite_decimal(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise FinanceError("INVALID_FINANCIAL_VALUE", "A finite Decimal is required")
    return value


def exact_decimal(
    value: Decimal, scale: int, *, nonnegative: bool = False, positive: bool = False
) -> Decimal:
    finite_decimal(value)
    try:
        encoded = to_scaled(format(value, "f"), scale)
    except ValueError as exc:
        raise FinanceError("INVALID_FINANCIAL_VALUE", "Invalid precision or INT64 range") from exc
    if (positive and encoded <= 0) or (nonnegative and encoded < 0):
        raise FinanceError("INVALID_FINANCIAL_VALUE", "Invalid financial sign")
    return Decimal(from_scaled(encoded, scale))


def round_money(value: Decimal) -> Decimal:
    """Quantize a computed result at its persistence boundary, never an unchecked input."""
    finite_decimal(value)
    try:
        with localcontext(prec=64, rounding=ROUND_HALF_UP):
            rounded = value.quantize(Decimal("0.000001"))
            return Decimal(from_scaled(to_scaled(format(rounded, "f"), 6), 6))
    except (ValueError, InvalidOperation) as exc:
        raise FinanceError("FINANCIAL_OVERFLOW", "Computed money exceeds INT64 storage") from exc


def conversion_rate(currency: Currency, rate: Decimal | None) -> Decimal | None:
    if currency not in ("HKD", "USD"):
        raise FinanceError("UNSUPPORTED_CURRENCY", "Only HKD and USD are supported")
    if rate is not None:
        rate = exact_decimal(rate, 12, positive=True)
    if currency == "HKD":
        if rate is not None and rate != ONE:
            raise FinanceError("CURRENCY_MISMATCH", "HKD/HKD must equal identity 1")
        return ONE
    return rate


@dataclass(frozen=True, slots=True)
class LockedMoney:
    local: Decimal
    currency: Currency
    fx_rate_hkd: Decimal
    hkd: Decimal
    override_reason: str | None


def lock_money(
    amount: Decimal,
    currency: Currency,
    rate: Decimal | None = None,
    *,
    actual_hkd: Decimal | None = None,
    reason: str | None = None,
) -> LockedMoney:
    amount = exact_decimal(amount, 6)
    fx = conversion_rate(currency, rate)
    if fx is None:
        raise FinanceError("MISSING_FX", "USD requires an explicit locked historical FX rate")
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        hkd = round_money(amount * fx)
        if actual_hkd is not None:
            if reason is None or len(reason.strip()) < 10:
                raise FinanceError("REASON_REQUIRED", "An actual HKD override needs evidence")
            hkd = exact_decimal(actual_hkd, 6)
            if currency == "HKD" and hkd != amount:
                raise FinanceError("CURRENCY_MISMATCH", "HKD overrides cannot change identity FX")
            if (amount == ZERO and hkd != ZERO) or amount * hkd < ZERO:
                raise FinanceError("CURRENCY_MISMATCH", "A conversion cannot reverse the cash sign")
        return LockedMoney(amount, currency, fx, hkd, reason if actual_hkd is not None else None)


@dataclass(frozen=True, slots=True)
class Settlement:
    gross_local: Decimal
    theoretical_local: Decimal
    cash: LockedMoney
    rounding_delta_local: Decimal
    override_reason: str | None


@dataclass(frozen=True, slots=True)
class NativeSettlement:
    gross_local: Decimal
    theoretical_local: Decimal
    amount: Decimal
    rounding_delta_local: Decimal
    override_reason: str | None


def native_settlement(
    side: Side,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    tax: Decimal,
    currency: Currency,
    *,
    actual_local: Decimal | None = None,
    reason: str | None = None,
) -> NativeSettlement:
    """Calculate native cash without inventing FX for an unposted scheduled draft."""
    if side not in ("BUY", "SELL"):
        raise FinanceError("INVALID_SIDE", "Select BUY or SELL")
    conversion_rate(currency, None)
    quantity = exact_decimal(quantity, 8, positive=True)
    price = exact_decimal(price, 8, nonnegative=True)
    fee = exact_decimal(fee, 6, nonnegative=True)
    tax = exact_decimal(tax, 6, nonnegative=True)
    if actual_local is not None and (reason is None or len(reason.strip()) < 10):
        raise FinanceError("REASON_REQUIRED", "An actual settlement override needs evidence")
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        gross = quantity * price
        theoretical = gross + fee + tax if side == "BUY" else gross - fee - tax
        # HKD/USD cash settles to cents by default; explicit broker actuals retain their precision.
        local = (
            theoretical.quantize(Decimal("0.01"))
            if actual_local is None
            else exact_decimal(actual_local, 6)
        )
        if side == "BUY" and local < ZERO:
            raise FinanceError("INVALID_FINANCIAL_VALUE", "Buy settlement cost cannot be negative")
        return NativeSettlement(gross, theoretical, round_money(local), local - theoretical, reason)


def settle_trade(
    side: Side,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    tax: Decimal,
    currency: Currency,
    rate: Decimal | None = None,
    *,
    actual_local: Decimal | None = None,
    actual_hkd: Decimal | None = None,
    reason: str | None = None,
) -> Settlement:
    native = native_settlement(
        side, quantity, price, fee, tax, currency, actual_local=actual_local, reason=reason
    )
    cash = lock_money(native.amount, currency, rate, actual_hkd=actual_hkd, reason=reason)
    return Settlement(
        native.gross_local,
        native.theoretical_local,
        cash,
        native.rounding_delta_local,
        native.override_reason,
    )


@dataclass(frozen=True, slots=True)
class Position:
    quantity: Decimal = ZERO
    cost_hkd: Decimal = ZERO
    realized_hkd: Decimal = ZERO

    def __post_init__(self) -> None:
        exact_decimal(self.quantity, 8, nonnegative=True)
        round_money(self.cost_hkd)
        round_money(self.realized_hkd)
        if self.cost_hkd < ZERO or (self.quantity == ZERO and self.cost_hkd != ZERO):
            raise FinanceError("INVALID_POSITION", "A flat position cannot retain book cost")

    @property
    def average_cost_hkd(self) -> Decimal | None:
        with localcontext(prec=64, rounding=ROUND_HALF_UP):
            return self.cost_hkd / self.quantity if self.quantity else None


@dataclass(frozen=True, slots=True)
class TradeEffect:
    position: Position
    allocated_cost_hkd: Decimal
    realized_hkd: Decimal


def apply_trade(
    position: Position, side: Side, quantity: Decimal, booked_hkd: Decimal
) -> TradeEffect:
    quantity = exact_decimal(quantity, 8, positive=True)
    booked_hkd = exact_decimal(booked_hkd, 6)
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        if side == "BUY":
            if booked_hkd < ZERO:
                raise FinanceError("INVALID_FINANCIAL_VALUE", "Buy cost cannot be negative")
            return TradeEffect(
                Position(
                    position.quantity + quantity,
                    position.cost_hkd + booked_hkd,
                    position.realized_hkd,
                ),
                ZERO,
                ZERO,
            )
        if side != "SELL":
            raise FinanceError("INVALID_SIDE", "Select BUY or SELL")
        if quantity > position.quantity:
            raise FinanceError(
                "INSUFFICIENT_SHARES", "Selling more than account holdings is forbidden"
            )
        allocated = (
            position.cost_hkd
            if quantity == position.quantity
            else position.cost_hkd * quantity / position.quantity
        )
        realized = booked_hkd - allocated
        remaining = position.quantity - quantity
        return TradeEffect(
            Position(
                remaining,
                position.cost_hkd - allocated if remaining else ZERO,
                position.realized_hkd + realized,
            ),
            allocated,
            realized,
        )


@dataclass(frozen=True, slots=True)
class TransferEffect:
    source: Position
    target: Position
    moved_cost_hkd: Decimal


def transfer(source: Position, target: Position, quantity: Decimal) -> TransferEffect:
    quantity = exact_decimal(quantity, 8, positive=True)
    if quantity > source.quantity:
        raise FinanceError("INSUFFICIENT_SHARES", "Transfer exceeds source account holdings")
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        moved = (
            source.cost_hkd
            if quantity == source.quantity
            else source.cost_hkd * quantity / source.quantity
        )
        remaining = source.quantity - quantity
        return TransferEffect(
            Position(
                remaining, source.cost_hkd - moved if remaining else ZERO, source.realized_hkd
            ),
            Position(target.quantity + quantity, target.cost_hkd + moved, target.realized_hkd),
            moved,
        )


def split(position: Position, numerator: int, denominator: int) -> Position:
    if any(
        type(value) is not int or not 1 <= value <= INT64_MAX for value in (numerator, denominator)
    ):
        raise FinanceError("INVALID_RATIO", "Split ratios require positive INT64 integers")
    encoded = to_scaled(format(position.quantity, "f"), 8)
    quantity, remainder = divmod(encoded * numerator, denominator)
    if remainder:
        raise FinanceError(
            "NEEDS_REVIEW", "Fractional residue requires confirmed cash-in-lieu terms"
        )
    try:
        shares = Decimal(from_scaled(quantity, 8))
    except ValueError as exc:
        raise FinanceError("FINANCIAL_OVERFLOW", "Split quantity exceeds INT64 storage") from exc
    return Position(shares, position.cost_hkd, position.realized_hkd)


@dataclass(frozen=True, slots=True)
class Metric:
    value: Decimal | None
    quality: Quality


@dataclass(frozen=True, slots=True)
class Valuation:
    market_value_hkd: Decimal | None
    unrealized_hkd: Decimal | None
    total_pnl_hkd: Decimal | None
    quality: Quality


def value_position(
    position: Position,
    price: Decimal | None,
    currency: Currency,
    rate: Decimal | None = None,
    *,
    recognized_dividend_hkd: Decimal = ZERO,
    operating_expenses_hkd: Decimal = ZERO,
) -> Valuation:
    finite_decimal(recognized_dividend_hkd)
    finite_decimal(operating_expenses_hkd)
    fx = conversion_rate(currency, rate)
    if price is not None:
        price = exact_decimal(price, 8, nonnegative=True)
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        if position.quantity == ZERO:
            market = ZERO
        else:
            if price is None:
                return Valuation(None, None, None, "MISSING_QUOTE")
            if fx is None:
                return Valuation(None, None, None, "MISSING_FX")
            market = position.quantity * price * fx
        unrealized = market - position.cost_hkd
        # Receipts settle receivables, not an additional component of economic profit.
        pnl = position.realized_hkd + unrealized + recognized_dividend_hkd - operating_expenses_hkd
        return Valuation(round_money(market), round_money(unrealized), round_money(pnl), "OK")


def net_asset_value(
    securities_hkd: Decimal | None,
    cash_hkd: Decimal | None,
    completeness: Completeness,
    *,
    receivables_hkd: Decimal = ZERO,
    liabilities_hkd: Decimal = ZERO,
) -> Metric:
    if completeness not in ("UNKNOWN", "PARTIAL", "COMPLETE"):
        raise FinanceError("INVALID_COMPLETENESS", "Unknown cash completeness assertion")
    if completeness != "COMPLETE" or cash_hkd is None:
        return Metric(None, "NOT_COMPUTABLE")
    if securities_hkd is None:
        return Metric(None, "MISSING_QUOTE")
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        for value in (securities_hkd, cash_hkd, receivables_hkd, liabilities_hkd):
            finite_decimal(value)
        return Metric(
            round_money(securities_hkd + cash_hkd + receivables_hkd - liabilities_hkd), "OK"
        )


def time_weighted_return(
    subperiods: Sequence[tuple[Decimal, Decimal]] | None, completeness: Completeness
) -> Metric:
    """Chain complete valuations split immediately around every external cash flow."""
    if completeness not in ("UNKNOWN", "PARTIAL", "COMPLETE"):
        raise FinanceError("INVALID_COMPLETENESS", "Unknown cash completeness assertion")
    if completeness != "COMPLETE" or not subperiods:
        return Metric(None, "NOT_COMPUTABLE")
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        factor = ONE
        for opening, closing in subperiods:
            finite_decimal(opening)
            finite_decimal(closing)
            if opening <= ZERO or closing < ZERO:
                return Metric(None, "NOT_COMPUTABLE")
            factor *= closing / opening
        return Metric(factor - ONE, "OK")
