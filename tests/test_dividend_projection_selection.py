"""Select explicit current projection versions without double-counting retained receipts."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from itertools import permutations
from typing import Any
from uuid import UUID

import pytest

from spm_api.dividend_math import Receipt, accrue, reconcile
from spm_api.dividend_projection import (
    ProjectionError,
    ProjectionScope,
    ProjectionState,
    ProjectionVersion,
    select_current,
)
from spm_api.finance import FinanceError

D = Decimal
EVENT = UUID(int=1)
EVENT_2 = UUID(int=2)
ACCOUNT = UUID(int=3)
ACCOUNT_2 = UUID(int=4)
REV_1 = UUID(int=5)
REV_2 = UUID(int=6)
OTHER_REV = UUID(int=7)
FIRST = UUID(int=8)
SECOND = UUID(int=9)
THIRD = UUID(int=10)


def scope(
    event: UUID = EVENT,
    account: UUID = ACCOUNT,
    revision_id: UUID = REV_2,
    revision_no: int = 2,
) -> ProjectionScope:
    return ProjectionScope(event, revision_id, revision_no, account)


def version(
    identifier: UUID = FIRST,
    *,
    event: UUID = EVENT,
    account: UUID = ACCOUNT,
    revision_id: UUID = REV_1,
    revision_no: int = 1,
    number: int = 1,
    previous: UUID | None = None,
    amount: str = "10",
) -> ProjectionVersion:
    accrual = accrue(str(event), str(account), "USD", D(amount), D("1"), D("0"), D("0"), D("7.8"))
    return ProjectionVersion(
        identifier, event, revision_id, revision_no, account, number, previous, "EXPECTED", accrual
    )


def selected_amount(snapshot: ProjectionVersion) -> Decimal:
    assert snapshot.accrual is not None
    return snapshot.accrual.net_local


def test_revision_retains_receipts_and_selects_exactly_one_current_accrual() -> None:
    first = version()
    second = version(
        SECOND, revision_id=REV_2, revision_no=2, number=2, previous=FIRST, amount="11"
    )
    receipt = Receipt("synthetic-receipt", str(EVENT), str(ACCOUNT), "USD", D("6"))
    selected = select_current([scope()], [second, first])
    assert selected == (second,)
    assert selected[0].accrual is not None
    result = reconcile([selected[0].accrual], [receipt])[0]
    assert (result.accrual.net_local, result.received_local, result.outstanding_local) == (
        D("11.000000"),
        D("6.000000"),
        D("5.000000"),
    )
    assert result.receipt_state == "PARTIAL"
    assert selected_amount(first) == D("10.000000")


def test_same_revision_rebuild_selects_only_the_latest_projection() -> None:
    first = version(revision_id=REV_2, revision_no=2)
    second = version(
        SECOND, revision_id=REV_2, revision_no=2, number=2, previous=FIRST, amount="11"
    )
    assert select_current([scope()], [first, second]) == (second,)


def test_separate_events_accounts_and_permutations_do_not_merge() -> None:
    first = version(revision_id=REV_2, revision_no=2)
    second = version(SECOND, account=ACCOUNT_2, revision_id=REV_2, revision_no=2, amount="12")
    third = version(THIRD, event=EVENT_2, revision_id=OTHER_REV, amount="5")
    scopes = [scope(), scope(account=ACCOUNT_2), scope(EVENT_2, ACCOUNT, OTHER_REV, 1)]
    for ordering in permutations([first, second, third]):
        assert select_current(scopes, ordering) == (first, second, third)


def test_derived_rounding_delta_and_locked_hkd_are_not_recomputed_from_rounded_net() -> None:
    calculated = accrue(
        str(EVENT), str(ACCOUNT), "USD", D("0.000001"), D("0.5"), D("0.5"), D("0"), D("7.8")
    )
    snapshot = replace(version(revision_id=REV_2, revision_no=2), accrual=calculated)
    assert select_current([scope()], [snapshot]) == (snapshot,)
    assert calculated.net_local == D("0.000000")
    assert calculated.rounding_delta_local == D("-0.000001")
    assert calculated.booked_hkd == D("0.000002")


def test_negative_expected_net_is_retained_with_review_quality() -> None:
    calculated = accrue(str(EVENT), str(ACCOUNT), "USD", D("1"), D("1"), D("0"), D("2"))
    snapshot = replace(version(revision_id=REV_2, revision_no=2), accrual=calculated)
    assert select_current([scope()], [snapshot]) == (snapshot,)
    assert calculated.net_local == D("-1.000000") and calculated.quality == "NEEDS_REVIEW"
    assert calculated.booked_hkd is None


def test_missing_scope_and_stale_revision_are_not_treated_as_zero() -> None:
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_MISSING$"):
        select_current([scope()], [])
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_STALE$"):
        select_current([scope()], [version()])
    assert select_current([], []) == ()


@pytest.mark.parametrize("state", ["NO_POSITION", "NEEDS_REVIEW", "CANCELLED"])
def test_non_amount_states_are_explicit_and_do_not_turn_receipts_into_zero(
    state: ProjectionState,
) -> None:
    snapshot = replace(version(revision_id=REV_2, revision_no=2), state=state, accrual=None)
    assert select_current([scope()], [snapshot]) == (snapshot,)
    receipt = Receipt("synthetic-receipt", str(EVENT), str(ACCOUNT), "USD", D("6"))
    with pytest.raises(FinanceError) as failure:
        reconcile([], [receipt])
    assert failure.value.code == "NEEDS_REVIEW"


def test_cross_currency_historical_receipt_stays_under_review() -> None:
    snapshot = version(revision_id=REV_2, revision_no=2)
    receipt = Receipt("synthetic-receipt", str(EVENT), str(ACCOUNT), "HKD", D("46.8"))
    selected = select_current([scope()], [snapshot])[0]
    assert selected.accrual is not None
    result = reconcile([selected.accrual], [receipt])[0]
    assert result.receipt_state == "NEEDS_REVIEW"
    assert result.received_local is None and result.outstanding_local is None


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate-scope",
        "foreign-projection",
        "duplicate-id",
        "duplicate-version",
        "missing-first",
        "wrong-predecessor",
        "revision-regression",
        "future-revision",
        "wrong-current-id",
        "revision-number-reused",
        "revision-id-reused",
        "inconsistent-event-head",
    ],
)
def test_inconsistent_history_never_selects_a_current_projection(problem: str) -> None:
    first = version()
    second = version(SECOND, revision_id=REV_2, revision_no=2, number=2, previous=FIRST)
    scopes, versions = [scope()], [first, second]
    if problem == "duplicate-scope":
        scopes.append(scope())
    elif problem == "foreign-projection":
        versions.append(version(THIRD, account=ACCOUNT_2))
    elif problem == "duplicate-id":
        versions[1] = replace(second, id=FIRST)
    elif problem == "duplicate-version":
        versions.append(replace(first, id=THIRD))
    elif problem == "missing-first":
        versions = [second]
    elif problem == "wrong-predecessor":
        versions[1] = replace(second, previous_id=THIRD)
    elif problem == "revision-regression":
        versions = [
            replace(first, revision_id=REV_2, revision_no=2),
            replace(second, revision_id=REV_1, revision_no=1),
        ]
    elif problem == "future-revision":
        versions[1] = replace(second, revision_no=3)
    elif problem == "wrong-current-id":
        versions[1] = replace(second, revision_id=OTHER_REV)
    elif problem == "revision-number-reused":
        versions = [first, replace(second, revision_no=1)]
    elif problem == "revision-id-reused":
        versions[1] = replace(second, revision_id=REV_1)
    elif problem == "inconsistent-event-head":
        scopes.append(scope(account=ACCOUNT_2, revision_id=OTHER_REV))
    with pytest.raises(
        ProjectionError,
        match="^(INVALID_DIVIDEND_PROJECTION_SCOPE|DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE)$",
    ):
        select_current(scopes, versions)


@pytest.mark.parametrize("earlier", ["missing", "stale"])
def test_missing_or_stale_scope_cannot_hide_corrupt_later_history(earlier: str) -> None:
    corrupt = version(
        SECOND,
        account=ACCOUNT_2,
        revision_id=REV_2,
        revision_no=2,
        number=2,
        previous=THIRD,
    )
    versions = [corrupt] if earlier == "missing" else [version(), corrupt]
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        select_current([scope(), scope(account=ACCOUNT_2)], versions)


def test_a_physical_revision_cannot_belong_to_two_events() -> None:
    first = version(revision_id=REV_2, revision_no=2)
    other = version(SECOND, event=EVENT_2, revision_id=REV_2, revision_no=2)
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        select_current([scope(), scope(EVENT_2, ACCOUNT, REV_2, 2)], [first, other])


@pytest.mark.parametrize(
    "problem", ["net", "quality", "missing-hkd", "false-missing-fx", "negative"]
)
def test_invalid_accrual_consistency_is_not_selected(problem: str) -> None:
    snapshot = version(revision_id=REV_2, revision_no=2)
    assert snapshot.accrual is not None
    altered = replace(snapshot.accrual)
    if problem == "net":
        object.__setattr__(altered, "net_local", D("99.000000"))
    elif problem == "quality":
        object.__setattr__(altered, "quality", "VERIFIED")
    elif problem == "missing-hkd":
        object.__setattr__(altered, "booked_hkd", None)
    elif problem == "false-missing-fx":
        object.__setattr__(altered, "quality", "MISSING_FX")
    else:
        object.__setattr__(altered, "fee_local", D("11.000000"))
        object.__setattr__(altered, "net_local", D("-1.000000"))
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        replace(snapshot, accrual=altered)


def test_nested_accrual_is_revalidated_at_selection() -> None:
    snapshot = version(revision_id=REV_2, revision_no=2)
    assert snapshot.accrual is not None
    object.__setattr__(snapshot.accrual, "net_local", D("99.000000"))
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        select_current([scope()], [snapshot])


def test_a_review_state_cannot_carry_a_financial_amount() -> None:
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        replace(version(), state="NEEDS_REVIEW")
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        replace(version(), state="EXPECTED", accrual=None)


@pytest.mark.parametrize("field", ["id", "event_id", "revision_id", "account_id", "projection_no"])
def test_forged_or_mutated_snapshot_is_revalidated_without_private_output(field: str) -> None:
    snapshot = version(revision_id=REV_2, revision_no=2)
    object.__setattr__(snapshot, field, True)
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$") as failure:
        select_current([scope()], [snapshot])
    assert str(EVENT) not in str(failure.value) and str(ACCOUNT) not in repr(snapshot)


@pytest.mark.parametrize("field", ["event_id", "revision_id", "account_id", "revision_no"])
def test_forged_scope_is_revalidated(field: str) -> None:
    current = scope()
    object.__setattr__(current, field, False)
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        select_current([current], [])


def test_a_forged_accrual_with_invalid_precision_fails_closed() -> None:
    snapshot = version(revision_id=REV_2, revision_no=2)
    assert snapshot.accrual is not None
    altered = replace(snapshot.accrual)
    object.__setattr__(altered, "gross_local", D("0.0000001"))
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        replace(snapshot, accrual=altered)


@pytest.mark.parametrize("problem", ["scopes", "versions", "scope-item", "version-item"])
def test_untyped_collections_and_items_fail_closed(problem: str) -> None:
    scopes: Any = [scope()]
    versions: Any = [version(revision_id=REV_2, revision_no=2)]
    if problem == "scopes":
        scopes = "not a sequence of scopes"
    elif problem == "versions":
        versions = iter(versions)
    elif problem == "scope-item":
        scopes = ["not a scope"]
    else:
        versions = ["not a version"]
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        select_current(scopes, versions)


def test_inputs_are_immutable_and_large_versions_are_bounded() -> None:
    snapshot = version()
    with pytest.raises(FrozenInstanceError):
        snapshot.projection_no = 2  # type: ignore[misc]
    assert (
        replace(snapshot, projection_no=9223372036854775807, previous_id=SECOND).projection_no
        == 9223372036854775807
    )
    with pytest.raises(ProjectionError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
        replace(snapshot, projection_no=9223372036854775808, previous_id=SECOND)
