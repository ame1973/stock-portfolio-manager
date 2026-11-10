"""Select explicitly scoped immutable dividend projections without adopting financial facts."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import localcontext
from typing import Literal
from uuid import UUID

from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_math import Accrual

ProjectionState = Literal["EXPECTED", "NO_POSITION", "NEEDS_REVIEW", "CANCELLED"]


class ProjectionError(ValueError):
    """Fixed codes only; never include account, event or financial values."""


def _number(value: object, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


@dataclass(frozen=True, slots=True)
class ProjectionScope:
    """An explicitly supplied current event revision and one account, not an authorization."""

    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    account_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID) for value in (self.event_id, self.revision_id, self.account_id)
        ) or not _number(self.revision_no, 2_147_483_647):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")


@dataclass(frozen=True, slots=True)
class ProjectionVersion:
    """One supplied historical projection, not an audited or current financial record."""

    id: UUID = field(repr=False)
    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    account_id: UUID = field(repr=False)
    projection_no: int
    previous_id: UUID | None = field(repr=False)
    state: ProjectionState
    accrual: Accrual | None = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(value, UUID)
                for value in (self.id, self.event_id, self.revision_id, self.account_id)
            )
            or not _number(self.revision_no, 2_147_483_647)
            or not _number(self.projection_no, INT64_MAX)
            or (self.projection_no == 1 and self.previous_id is not None)
            or (self.projection_no > 1 and not isinstance(self.previous_id, UUID))
            or self.state not in ("EXPECTED", "NO_POSITION", "NEEDS_REVIEW", "CANCELLED")
        ):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
        if self.state != "EXPECTED":
            if self.accrual is not None:
                raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
            return
        if not isinstance(self.accrual, Accrual):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
        try:
            self.accrual.__post_init__()
        except (ValueError, TypeError, OverflowError):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE") from None
        with localcontext(prec=64):
            net = (
                self.accrual.gross_local
                - self.accrual.tax_local
                - self.accrual.fee_local
                + self.accrual.rounding_delta_local
            )
        if (
            self.accrual.event_id != str(self.event_id)
            or self.accrual.account_id != str(self.account_id)
            or self.accrual.net_local != net
            or self.accrual.quality
            not in ("OK", "MISSING_QUOTE", "MISSING_FX", "NOT_COMPUTABLE", "NEEDS_REVIEW")
            or (self.accrual.quality == "OK" and self.accrual.booked_hkd is None)
            or (self.accrual.quality == "MISSING_FX" and self.accrual.booked_hkd is not None)
            or (net < 0 and self.accrual.quality != "NEEDS_REVIEW")
        ):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")


def select_current(
    scopes: Sequence[ProjectionScope], versions: Sequence[ProjectionVersion]
) -> tuple[ProjectionVersion, ...]:
    """Select one supplied latest version per scope after validating all supplied history.

    The caller must authenticate, provide every relevant account and retained version,
    verify current event/ledger evidence, and commit economic changes atomically.
    Selection is not proof of completeness, a source decision or financial authority.
    """
    if (
        not isinstance(scopes, Sequence)
        or isinstance(scopes, (str, bytes))
        or not isinstance(versions, Sequence)
        or isinstance(versions, (str, bytes))
    ):
        raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
    frozen_scopes, frozen_versions = tuple(scopes), tuple(versions)
    groups: dict[tuple[UUID, UUID], list[ProjectionVersion]] = {}
    event_heads: dict[UUID, tuple[UUID, int]] = {}
    by_number: dict[tuple[UUID, int], UUID] = {}
    by_id: dict[UUID, tuple[UUID, int]] = {}

    def revision(event: UUID, identifier: UUID, number: int) -> None:
        number_key = event, number
        if (number_key in by_number and by_number[number_key] != identifier) or (
            identifier in by_id and by_id[identifier] != (event, number)
        ):
            raise ProjectionError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        by_number[number_key], by_id[identifier] = identifier, (event, number)

    for scope in frozen_scopes:
        if not isinstance(scope, ProjectionScope):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
        scope.__post_init__()
        key = scope.event_id, scope.account_id
        head = scope.revision_id, scope.revision_no
        if key in groups or (scope.event_id in event_heads and event_heads[scope.event_id] != head):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
        groups[key], event_heads[scope.event_id] = [], head
        revision(scope.event_id, scope.revision_id, scope.revision_no)

    identities: set[UUID] = set()
    for item in frozen_versions:
        if not isinstance(item, ProjectionVersion):
            raise ProjectionError("INVALID_DIVIDEND_PROJECTION_SCOPE")
        item.__post_init__()
        key = item.event_id, item.account_id
        if (
            key not in groups
            or item.id in identities
            or item.revision_no > event_heads[item.event_id][1]
        ):
            raise ProjectionError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        identities.add(item.id)
        revision(item.event_id, item.revision_id, item.revision_no)
        groups[key].append(item)

    selected = []
    missing = False
    stale = False
    for scope in frozen_scopes:
        history = sorted(
            groups[scope.event_id, scope.account_id], key=lambda item: item.projection_no
        )
        if not history:
            missing = True
            continue
        for index, item in enumerate(history, 1):
            previous = history[index - 2] if index > 1 else None
            if item.projection_no != index or (
                previous is not None
                and (item.previous_id != previous.id or item.revision_no < previous.revision_no)
            ):
                raise ProjectionError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        current = history[-1]
        if (current.revision_id, current.revision_no) != (scope.revision_id, scope.revision_no):
            stale = True
        selected.append(current)
    if missing:
        raise ProjectionError("DIVIDEND_PROJECTION_MISSING")
    if stale:
        raise ProjectionError("DIVIDEND_PROJECTION_STALE")
    return tuple(selected)
