"""Deterministic, account-specific replay of selected financial fact versions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from spm_api.decimal_types import INT64_MAX
from spm_api.finance import FinanceError, Position, Side, apply_trade, split, transfer


@dataclass(frozen=True, slots=True)
class Trade:
    account_id: str
    security_id: str
    side: Side
    quantity: Decimal
    booked_hkd: Decimal


@dataclass(frozen=True, slots=True)
class Transfer:
    source_account_id: str
    target_account_id: str
    security_id: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class Split:
    security_id: str
    numerator: int
    denominator: int


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    day: date
    sequence: int
    operation: Trade | Transfer | Split
    executed_at: datetime | None = None
    active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise FinanceError("INVALID_EVENT", "A stable fact identity is required")
        if type(self.day) is not date or type(self.sequence) is not int:
            raise FinanceError(
                "INVALID_EVENT", "An effective date and integer sequence are required"
            )
        if not 1 <= self.sequence <= INT64_MAX or type(self.active) is not bool:
            raise FinanceError("INVALID_EVENT", "Invalid replay sequence or active flag")
        if self.executed_at is not None and (
            not isinstance(self.executed_at, datetime) or self.executed_at.utcoffset() is None
        ):
            raise FinanceError("INVALID_EVENT", "Execution timestamps must include a timezone")


def _order(event: Event) -> tuple[date, int, datetime, int, str]:
    # Splits apply at opening; unknown intraday times sort before known timestamps.
    opening_action = 0 if isinstance(event.operation, Split) else 1
    execution = (
        event.executed_at.astimezone(UTC)
        if event.executed_at is not None
        else datetime.min.replace(tzinfo=UTC)
    )
    return event.day, opening_action, execution, event.sequence, event.id


def replay(
    events: Sequence[Event], *, as_of: date, exclusive: bool = False
) -> dict[tuple[str, str], Position]:
    """Replay active inputs into new immutable values; an error publishes no partial result."""
    if type(as_of) is not date:
        raise FinanceError("INVALID_EVENT", "Replay requires a calendar date")
    positions: dict[tuple[str, str], Position] = {}
    seen: set[str] = set()
    for event in sorted(events, key=_order):
        if event.id in seen:
            raise FinanceError("DUPLICATE_FACT", "A fact cannot be replayed twice")
        seen.add(event.id)
        if not event.active or event.day > as_of or (exclusive and event.day == as_of):
            continue
        operation = event.operation
        if isinstance(operation, Trade):
            key = operation.account_id, operation.security_id
            position = positions.get(key, Position())
            positions[key] = apply_trade(
                position, operation.side, operation.quantity, operation.booked_hkd
            ).position
        elif isinstance(operation, Transfer):
            if operation.source_account_id == operation.target_account_id:
                raise FinanceError("INVALID_TRANSFER", "Source and target accounts must differ")
            source_key = operation.source_account_id, operation.security_id
            target_key = operation.target_account_id, operation.security_id
            effect = transfer(
                positions.get(source_key, Position()),
                positions.get(target_key, Position()),
                operation.quantity,
            )
            positions[source_key], positions[target_key] = effect.source, effect.target
        elif isinstance(operation, Split):
            # Compute every affected account first; one residue prevents any published split effect.
            updates = {
                key: split(position, operation.numerator, operation.denominator)
                for key, position in positions.items()
                if key[1] == operation.security_id
            }
            positions.update(updates)
        else:
            raise FinanceError("NEEDS_REVIEW", "Unsupported financial operation")
    return positions


def eligible_quantity(
    events: Sequence[Event], account_id: str, security_id: str, ex_date: date
) -> Decimal:
    """Ex-date eligibility uses strictly earlier effective dates, never a pay-date guess."""
    return (
        replay(events, as_of=ex_date, exclusive=True)
        .get((account_id, security_id), Position())
        .quantity
    )
