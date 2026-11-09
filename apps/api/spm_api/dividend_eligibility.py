"""Recorded ex-date share eligibility without inferring income or completeness."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from spm_api.decimal_types import INT64_MAX, from_scaled, to_scaled
from spm_api.finance import FinanceError, exact_decimal
from spm_api.replay import Event, Split, Trade, Transfer, replay

EligibilityRule = Literal["ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"]
EligibilityState = Literal["RECORDED_PRE_EX_DATE", "UPCOMING_RECORDED_HOLDINGS", "NEEDS_REVIEW"]


class EligibilityError(ValueError):
    """Fixed codes only; never include account identifiers or historical facts."""


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value == value.strip()
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


@dataclass(frozen=True, slots=True)
class PendingAction:
    """An explicitly unresolved current action, not an adopted split or inferred allocation."""

    id: str = field(repr=False)
    security_id: str = field(repr=False)
    day: date

    def __post_init__(self) -> None:
        if (
            not _identifier(self.id)
            or not _identifier(self.security_id)
            or type(self.day) is not date
        ):
            raise EligibilityError("INVALID_ELIGIBILITY_SCOPE")


@dataclass(frozen=True, slots=True)
class AccountEligibility:
    """A recorded-history observation, never a confirmed entitlement or complete-position claim."""

    account_id: str = field(repr=False)
    recorded_quantity: str | None = field(repr=False)
    state: EligibilityState
    reasons: tuple[str, ...] = ()
    pending_action_ids: tuple[str, ...] = field(default=(), repr=False)
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    scope: Literal["RECORDED_SHARES_ONLY"] = "RECORDED_SHARES_ONLY"


def _review(
    accounts: Sequence[str], reason: str, actions: tuple[str, ...] = ()
) -> tuple[AccountEligibility, ...]:
    return tuple(
        AccountEligibility(account, None, "NEEDS_REVIEW", (reason,), actions)
        for account in accounts
    )


def _valid_event(event: Event) -> bool:
    try:
        if (
            not _identifier(event.id)
            or type(event.day) is not date
            or type(event.sequence) is not int
            or not 1 <= event.sequence <= INT64_MAX
            or type(event.active) is not bool
            or (
                event.executed_at is not None
                and (
                    not isinstance(event.executed_at, datetime)
                    or event.executed_at.utcoffset() is None
                )
            )
        ):
            return False
        operation = event.operation
        if not isinstance(operation, (Trade, Transfer, Split)) or not _identifier(
            operation.security_id
        ):
            return False
        if isinstance(operation, Trade):
            if not _identifier(operation.account_id) or operation.side not in ("BUY", "SELL"):
                return False
            exact_decimal(operation.quantity, 8, positive=True)
            exact_decimal(operation.booked_hkd, 6)
        elif isinstance(operation, Transfer):
            if (
                not _identifier(operation.source_account_id)
                or not _identifier(operation.target_account_id)
                or operation.source_account_id == operation.target_account_id
            ):
                return False
            exact_decimal(operation.quantity, 8, positive=True)
        elif any(
            type(value) is not int or not 1 <= value <= INT64_MAX
            for value in (operation.numerator, operation.denominator)
        ):
            return False
        return True
    except (ValueError, TypeError, OverflowError):
        return False


def recorded_eligibility(
    events: Sequence[Event],
    pending_actions: Sequence[PendingAction],
    accounts: Sequence[str],
    *,
    security_id: str,
    ex_date: date,
    as_of: date,
    rule: EligibilityRule,
) -> tuple[AccountEligibility, ...]:
    """Replay one authorized portfolio; the caller must validate accounts and current actions.

    Transfer-linked accounts are reviewed together when an action or split is unresolved.
    Results describe recorded shares only and do not establish complete ownership history.
    """
    if (
        not _identifier(security_id)
        or type(ex_date) is not date
        or type(as_of) is not date
        or not isinstance(rule, str)
        or rule not in {"ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"}
        or isinstance(events, (str, bytes))
        or isinstance(pending_actions, (str, bytes))
        or isinstance(accounts, (str, bytes))
        or not isinstance(events, Sequence)
        or not isinstance(pending_actions, Sequence)
        or not isinstance(accounts, Sequence)
        or any(not isinstance(item, Event) for item in events)
        or any(not isinstance(item, PendingAction) for item in pending_actions)
        or any(not _identifier(account) for account in accounts)
        or len(set(accounts)) != len(accounts)
    ):
        raise EligibilityError("INVALID_ELIGIBILITY_SCOPE")
    if any(not _valid_event(event) for event in events) or any(
        not _identifier(action.id)
        or not _identifier(action.security_id)
        or type(action.day) is not date
        for action in pending_actions
    ):
        raise EligibilityError("ELIGIBILITY_HISTORY_UNAVAILABLE")
    if len({event.id for event in events}) != len(events) or len(
        {action.id for action in pending_actions}
    ) != len(pending_actions):
        raise EligibilityError("ELIGIBILITY_HISTORY_UNAVAILABLE")
    if {event.id for event in events} & {action.id for action in pending_actions}:
        raise EligibilityError("ELIGIBILITY_HISTORY_UNAVAILABLE")
    if rule == "REQUIRES_REVIEW":
        return _review(accounts, "ELIGIBILITY_RULE_REQUIRES_REVIEW")

    ex_date_reached = ex_date <= as_of
    cutoff = ex_date if ex_date_reached else as_of
    effective = [
        event
        for event in events
        if event.operation.security_id == security_id
        and event.active
        and (event.day < cutoff if ex_date_reached else event.day <= cutoff)
    ]
    pending = sorted(
        (
            action
            for action in pending_actions
            if action.security_id == security_id
            and (action.day < cutoff if ex_date_reached else action.day <= cutoff)
        ),
        key=lambda action: (action.day, action.id),
    )

    # ponytail: whole transfer-connected components are conservative; use dated edges if needed.
    neighbors: dict[str, set[str]] = {account: set() for account in accounts}
    for event in effective:
        operation = event.operation
        if isinstance(operation, Trade):
            neighbors.setdefault(operation.account_id, set())
        elif isinstance(operation, Transfer):
            neighbors.setdefault(operation.source_account_id, set()).add(
                operation.target_account_id
            )
            neighbors.setdefault(operation.target_account_id, set()).add(
                operation.source_account_id
            )

    groups: list[set[str]] = []
    remaining = set(neighbors)
    while remaining:
        group: set[str] = set()
        stack = [remaining.pop()]
        while stack:
            account = stack.pop()
            group.add(account)
            linked = neighbors[account] & remaining
            remaining.difference_update(linked)
            stack.extend(linked)
        groups.append(group)

    state: EligibilityState = (
        "RECORDED_PRE_EX_DATE" if ex_date_reached else "UPCOMING_RECORDED_HOLDINGS"
    )
    results: dict[str, AccountEligibility] = {}
    try:
        for group in groups:
            group_events = [
                event
                for event in effective
                if isinstance(event.operation, Split)
                or (isinstance(event.operation, Trade) and event.operation.account_id in group)
                or (
                    isinstance(event.operation, Transfer)
                    and event.operation.source_account_id in group
                )
            ]
            try:
                # ponytail: replay unresolved prefixes; use incremental replay if histories grow.
                affected = any(
                    any(
                        position.quantity > 0
                        for position in replay(
                            group_events, as_of=action.day, exclusive=True
                        ).values()
                    )
                    for action in pending
                )
                if affected:
                    for result in _review(
                        tuple(group), "UNALLOCATED_ACTION", tuple(action.id for action in pending)
                    ):
                        results[result.account_id] = result
                    continue
                positions = replay(group_events, as_of=cutoff, exclusive=ex_date_reached)
                for account in group:
                    position = positions.get((account, security_id))
                    quantity = (
                        from_scaled(to_scaled(format(position.quantity, "f"), 8), 8)
                        if position is not None
                        else "0.00000000"
                    )
                    results[account] = AccountEligibility(account, quantity, state)
            except FinanceError as error:
                if error.code != "NEEDS_REVIEW":
                    raise
                for result in _review(tuple(group), "SPLIT_FRACTIONAL_RESIDUE"):
                    results[result.account_id] = result
        return tuple(results[account] for account in accounts)
    except (FinanceError, ValueError, TypeError, KeyError, OverflowError):
        raise EligibilityError("ELIGIBILITY_HISTORY_UNAVAILABLE") from None
