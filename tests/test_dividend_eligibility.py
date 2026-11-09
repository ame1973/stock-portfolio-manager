"""Recorded dividend eligibility uses exact pre-ex-date account histories."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from spm_api.dividend_eligibility import (
    AccountEligibility,
    EligibilityError,
    PendingAction,
    recorded_eligibility,
)
from spm_api.replay import Event, Split, Trade, Transfer

D = Decimal
SECURITY = "synthetic-stock"
EX_DATE = date(2026, 1, 5)


def trade(identifier: str, day: int, account: str, side: str, quantity: str) -> Event:
    return Event(
        identifier,
        date(2026, 1, day),
        int(identifier.rsplit("-", 1)[-1]),
        Trade(account, SECURITY, side, D(quantity), D("0")),  # type: ignore[arg-type]
    )


def recorded(
    entries: list[Event],
    *,
    accounts: tuple[str, ...] = ("A", "B", "C"),
    as_of: date = EX_DATE,
    rule: Any = "ORDINARY_PRE_EX_DATE",
    pending: tuple[PendingAction, ...] = (),
) -> tuple[AccountEligibility, ...]:
    return recorded_eligibility(
        entries,
        pending,
        accounts,
        security_id=SECURITY,
        ex_date=EX_DATE,
        as_of=as_of,
        rule=rule,
    )


def test_pre_ex_date_replay_separates_accounts_transfers_and_exact_split() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "10"),
        trade("trade-2", 2, "B", "BUY", "5"),
        Event("transfer-3", date(2026, 1, 3), 3, Transfer("A", "B", SECURITY, D("4"))),
        Event("split-4", date(2026, 1, 4), 4, Split(SECURITY, 3, 2)),
        trade("trade-5", 5, "A", "SELL", "1"),
        trade("trade-6", 5, "B", "BUY", "2"),
    ]
    result = recorded(entries)
    assert [(row.account_id, row.recorded_quantity, row.state) for row in result] == [
        ("A", "9.00000000", "RECORDED_PRE_EX_DATE"),
        ("B", "13.50000000", "RECORDED_PRE_EX_DATE"),
        ("C", "0.00000000", "RECORDED_PRE_EX_DATE"),
    ]
    assert all(row.history_completeness == "UNVERIFIED" for row in result)
    assert all(row.scope == "RECORDED_SHARES_ONLY" for row in result)
    assert all(row.reasons == () and row.pending_action_ids == () for row in result)


def test_future_ex_date_shows_only_recorded_as_of_holdings_not_forecast_eligibility() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "10"),
        trade("trade-2", 2, "B", "BUY", "5"),
        Event("transfer-3", date(2026, 1, 3), 3, Transfer("A", "B", SECURITY, D("4"))),
        Event("split-4", date(2026, 1, 4), 4, Split(SECURITY, 3, 2)),
    ]
    result = recorded(entries, as_of=date(2026, 1, 3))
    assert [(row.account_id, row.recorded_quantity, row.state) for row in result] == [
        ("A", "6.00000000", "UPCOMING_RECORDED_HOLDINGS"),
        ("B", "9.00000000", "UPCOMING_RECORDED_HOLDINGS"),
        ("C", "0.00000000", "UPCOMING_RECORDED_HOLDINGS"),
    ]


def test_due_bill_or_unknown_eligibility_convention_requires_explicit_review() -> None:
    result = recorded([trade("trade-1", 1, "A", "BUY", "10")], rule="REQUIRES_REVIEW")
    assert all(row.recorded_quantity is None and row.state == "NEEDS_REVIEW" for row in result)
    assert all(row.reasons == ("ELIGIBILITY_RULE_REQUIRES_REVIEW",) for row in result)


def test_unallocated_action_only_blocks_affected_account_components() -> None:
    entries = [trade("trade-1", 1, "A", "BUY", "10")]
    pending = (
        PendingAction("action-2", SECURITY, date(2026, 1, 2)),
        PendingAction("action-3", SECURITY, date(2026, 1, 4)),
    )
    result = recorded(entries, pending=pending)
    assert result[0].recorded_quantity is None and result[0].state == "NEEDS_REVIEW"
    assert result[0].reasons == ("UNALLOCATED_ACTION",)
    assert result[0].pending_action_ids == ("action-2", "action-3")
    assert [(row.recorded_quantity, row.state) for row in result[1:]] == [
        ("0.00000000", "RECORDED_PRE_EX_DATE"),
        ("0.00000000", "RECORDED_PRE_EX_DATE"),
    ]


def test_unallocated_action_taints_transfer_linked_accounts_but_not_independent_accounts() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "10"),
        Event("transfer-3", date(2026, 1, 3), 3, Transfer("A", "B", SECURITY, D("4"))),
        trade("trade-4", 4, "C", "BUY", "7"),
    ]
    result = recorded(entries, pending=(PendingAction("action-2", SECURITY, date(2026, 1, 2)),))
    assert [(row.recorded_quantity, row.reasons) for row in result] == [
        (None, ("UNALLOCATED_ACTION",)),
        (None, ("UNALLOCATED_ACTION",)),
        ("7.00000000", ()),
    ]


def test_unallocated_action_before_holding_or_on_ex_date_does_not_change_prior_eligibility() -> (
    None
):
    entries = [trade("trade-2", 2, "A", "BUY", "10")]
    pending = (
        PendingAction("action-1", SECURITY, date(2026, 1, 1)),
        PendingAction("action-5", SECURITY, EX_DATE),
        PendingAction("other", "different-stock", date(2026, 1, 3)),
    )
    result = recorded(entries, pending=pending)
    assert result[0].recorded_quantity == "10.00000000"
    assert result[0].state == "RECORDED_PRE_EX_DATE" and result[0].reasons == ()


def test_upcoming_same_day_unallocated_action_makes_recorded_holdings_unknown() -> None:
    entries = [trade("trade-1", 1, "A", "BUY", "10")]
    result = recorded(
        entries,
        as_of=date(2026, 1, 3),
        pending=(PendingAction("action-3", SECURITY, date(2026, 1, 3)),),
    )
    assert result[0].recorded_quantity is None and result[0].reasons == ("UNALLOCATED_ACTION",)
    assert [row.recorded_quantity for row in result[1:]] == ["0.00000000", "0.00000000"]


def test_fractional_split_residue_is_review_not_rounded_or_treated_as_zero() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "0.00000001"),
        Event("split-2", date(2026, 1, 2), 2, Split(SECURITY, 1, 3)),
    ]
    result = recorded(entries)
    assert result[0].recorded_quantity is None
    assert result[0].reasons == ("SPLIT_FRACTIONAL_RESIDUE",)
    assert [row.recorded_quantity for row in result[1:]] == ["0.00000000", "0.00000000"]


def test_oversell_or_duplicate_fact_never_returns_partial_eligibility() -> None:
    entries = [trade("trade-1", 1, "A", "BUY", "1"), trade("trade-2", 2, "A", "SELL", "2")]
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded(entries)
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([entries[0], entries[0]])


@pytest.mark.parametrize("accounts", [("A", "A"), ("",), ("bad\x00id",)])
def test_duplicate_or_invalid_accounts_are_not_silently_collapsed(
    accounts: tuple[str, ...],
) -> None:
    with pytest.raises(EligibilityError, match="^INVALID_ELIGIBILITY_SCOPE$"):
        recorded([], accounts=accounts)


@pytest.mark.parametrize("invalid", [None, True, 123, [], {}])
def test_eligibility_rule_must_be_explicit_and_well_formed(invalid: Any) -> None:
    with pytest.raises(EligibilityError, match="^INVALID_ELIGIBILITY_SCOPE$"):
        recorded([], rule=invalid)


@pytest.mark.parametrize("account", [None, "", "bad\x00id"])
def test_bad_account_on_recorded_trade_cannot_turn_into_a_false_zero(account: Any) -> None:
    entry = Event("trade-1", date(2026, 1, 1), 1, Trade(account, SECURITY, "BUY", D("10"), D("0")))
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([entry])


def test_confirmed_ex_date_split_is_excluded_but_as_of_day_split_is_included() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "10"),
        Event("split-5", EX_DATE, 5, Split(SECURITY, 2, 1)),
    ]
    assert recorded(entries)[0].recorded_quantity == "10.00000000"
    upcoming = recorded(entries, as_of=date(2026, 1, 5), rule="ORDINARY_PRE_EX_DATE")
    assert upcoming[0].recorded_quantity == "10.00000000"
    # A later ex-date makes the same split part of the explicitly requested as-of day.
    later = recorded_eligibility(
        entries,
        (),
        ("A",),
        security_id=SECURITY,
        ex_date=date(2026, 1, 6),
        as_of=EX_DATE,
        rule="ORDINARY_PRE_EX_DATE",
    )
    assert later[0].state == "UPCOMING_RECORDED_HOLDINGS"
    assert later[0].recorded_quantity == "20.00000000"


@pytest.mark.parametrize("invalid", [None, "2026-01-05", True, 123])
def test_explicit_exchange_local_date_must_be_a_real_calendar_date(invalid: Any) -> None:
    with pytest.raises(EligibilityError, match="^INVALID_ELIGIBILITY_SCOPE$"):
        recorded([], as_of=invalid)


def test_internal_result_repr_does_not_disclose_account_quantity_or_action_ids() -> None:
    secret_account = "private-account-marker"
    secret_action = "private-action-marker"
    row = recorded(
        [trade("trade-1", 1, secret_account, "BUY", "7.01020304")],
        accounts=(secret_account,),
    )[0]
    reviewed = recorded(
        [trade("trade-1", 1, secret_account, "BUY", "7.01020304")],
        accounts=(secret_account,),
        pending=(PendingAction(secret_action, SECURITY, date(2026, 1, 2)),),
    )[0]
    assert row.recorded_quantity == "7.01020304" and reviewed.pending_action_ids == (secret_action,)
    assert all(
        value not in repr(item)
        for item in (row, reviewed)
        for value in (secret_account, secret_action, "7.01020304")
    )
    action = PendingAction(secret_action, SECURITY, date(2026, 1, 2))
    assert secret_action not in repr(action) and SECURITY not in repr(action)


def test_connected_review_propagates_through_unselected_intermediary_accounts() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "10"),
        Event("transfer-3", date(2026, 1, 3), 3, Transfer("A", "hidden", SECURITY, D("4"))),
        Event("transfer-4", date(2026, 1, 4), 4, Transfer("hidden", "B", SECURITY, D("2"))),
    ]
    result = recorded(
        entries,
        accounts=("B", "C", "A"),
        pending=(PendingAction("action-2", SECURITY, date(2026, 1, 2)),),
    )
    assert [row.account_id for row in result] == ["B", "C", "A"]
    assert [(row.recorded_quantity, row.state) for row in result] == [
        (None, "NEEDS_REVIEW"),
        ("0.00000000", "RECORDED_PRE_EX_DATE"),
        (None, "NEEDS_REVIEW"),
    ]


def test_inactive_and_future_transfers_do_not_connect_current_account_components() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "10"),
        Event(
            "inactive-3", date(2026, 1, 3), 3, Transfer("A", "B", SECURITY, D("1")), active=False
        ),
        Event("future-5", EX_DATE, 5, Transfer("A", "C", SECURITY, D("1"))),
        trade("trade-4", 4, "B", "BUY", "2"),
    ]
    result = recorded(entries, pending=(PendingAction("action-2", SECURITY, date(2026, 1, 2)),))
    assert [row.recorded_quantity for row in result] == [None, "2.00000000", "0.00000000"]
    assert all(row.history_completeness == "UNVERIFIED" for row in result)


def test_fractional_split_reviews_transfer_component_not_unrelated_positions() -> None:
    entries = [
        trade("trade-1", 1, "A", "BUY", "0.00000001"),
        Event("split-2", date(2026, 1, 2), 2, Split(SECURITY, 1, 3)),
        Event("transfer-3", date(2026, 1, 3), 3, Transfer("A", "B", SECURITY, D("0.00000001"))),
        trade("trade-4", 4, "C", "BUY", "2"),
    ]
    result = recorded(entries)
    assert [(row.recorded_quantity, row.reasons) for row in result] == [
        (None, ("SPLIT_FRACTIONAL_RESIDUE",)),
        (None, ("SPLIT_FRACTIONAL_RESIDUE",)),
        ("2.00000000", ()),
    ]


def test_calendar_extremes_do_not_subtract_days_or_forecast_future_shares() -> None:
    entries = [
        Event("first", date.min, 1, Trade("A", SECURITY, "BUY", D("1"), D("0"))),
        Event("last", date.max, 2, Trade("A", SECURITY, "BUY", D("2"), D("0"))),
    ]

    def at(ex_date: date, as_of: date) -> AccountEligibility:
        return recorded_eligibility(
            entries,
            (),
            ("A",),
            security_id=SECURITY,
            ex_date=ex_date,
            as_of=as_of,
            rule="ORDINARY_PRE_EX_DATE",
        )[0]

    assert at(date.min, date.min).recorded_quantity == "0.00000000"
    assert at(date.max, date.min).recorded_quantity == "1.00000000"
    assert at(date.max, date.min).state == "UPCOMING_RECORDED_HOLDINGS"
    assert at(date.max, date.max).recorded_quantity == "1.00000000"
    assert at(date.max, date.max).state == "RECORDED_PRE_EX_DATE"


def test_int64_maximum_quantity_is_exact_and_accumulated_overflow_fails_closed() -> None:
    maximum = "92233720368.54775807"
    assert recorded([trade("trade-1", 1, "A", "BUY", maximum)])[0].recorded_quantity == maximum
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded(
            [
                trade("trade-1", 1, "A", "BUY", maximum),
                trade("trade-2", 2, "A", "BUY", "0.00000001"),
            ]
        )


@pytest.mark.parametrize(
    "quantity",
    [0.1, D("NaN"), D("Infinity"), D("0"), D("-1"), D("0.000000001"), D("92233720368.54775808")],
)
def test_invalid_trade_quantity_cannot_be_converted_to_recorded_zero(quantity: Any) -> None:
    entry = Event("trade-1", date(2026, 1, 1), 1, Trade("A", SECURITY, "BUY", quantity, D("0")))
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([entry])


@pytest.mark.parametrize(
    "operation",
    [
        Trade("A", SECURITY, "BUY", D("1"), D("-1")),
        Trade("A", SECURITY, "SELL", D("1"), D("0")),
        Trade("A", SECURITY, "WRONG", D("1"), D("0")),  # type: ignore[arg-type]
        Transfer("A", "A", SECURITY, D("1")),
        Transfer("A", "B", SECURITY, D("0.000000001")),
        Split(SECURITY, 0, 1),
        Split(SECURITY, 1, 2**63),
        Split(SECURITY, True, 1),
        None,
    ],
)
def test_invalid_history_fails_closed_even_with_valid_unrelated_accounts(operation: Any) -> None:
    entry = Event("bad", date(2026, 1, 1), 1, operation)
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([entry])


def test_pending_action_identity_must_be_unique_and_not_overlap_an_event() -> None:
    action = PendingAction("action-2", SECURITY, date(2026, 1, 2))
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([], pending=(action, action))
    event = Event("action-2", date(2026, 1, 1), 1, Split(SECURITY, 2, 1))
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([event], pending=(action,))


@pytest.mark.parametrize("identifier", ["", " A", "A\nB", "A\x7fB", "A" * 129, None])
def test_pending_action_rejects_invalid_identity_at_construction(identifier: Any) -> None:
    with pytest.raises(EligibilityError, match="^INVALID_ELIGIBILITY_SCOPE$"):
        PendingAction(identifier, SECURITY, date(2026, 1, 1))


@pytest.mark.parametrize(
    "field, value",
    [
        ("id", "bad\x00id"),
        ("day", "2026-01-01"),
        ("sequence", True),
        ("sequence", 2**63),
        ("active", 1),
        ("executed_at", datetime(2026, 1, 1)),
    ],
)
def test_corrupt_frozen_event_metadata_is_rejected(field: str, value: Any) -> None:
    entry = trade("trade-1", 1, "A", "BUY", "1")
    object.__setattr__(entry, field, value)
    with pytest.raises(EligibilityError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        recorded([entry])


def test_timezone_aware_execution_is_accepted_and_other_securities_are_excluded() -> None:
    entries = [
        Event(
            "one",
            date(2026, 1, 1),
            1,
            Trade("A", SECURITY, "BUY", D("2"), D("0")),
            datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Event("other", date(2026, 1, 1), 2, Trade("A", "other", "BUY", D("4"), D("0"))),
    ]
    assert recorded(entries)[0].recorded_quantity == "2.00000000"


@pytest.mark.parametrize(
    "field, value",
    [
        ("events", None),
        ("events", "bad"),
        ("events", [None]),
        ("pending_actions", None),
        ("pending_actions", "bad"),
        ("pending_actions", [None]),
        ("accounts", None),
        ("accounts", "bad"),
        ("accounts", [None]),
        ("security_id", ""),
        ("security_id", "bad\x00id"),
        ("security_id", "x" * 129),
        ("ex_date", datetime(2026, 1, 5)),
    ],
)
def test_invalid_scope_inputs_have_a_fixed_error(field: str, value: Any) -> None:
    parameters: dict[str, Any] = {
        "events": [],
        "pending_actions": [],
        "accounts": ["A"],
        "security_id": SECURITY,
        "ex_date": EX_DATE,
        "as_of": EX_DATE,
        "rule": "ORDINARY_PRE_EX_DATE",
    }
    parameters[field] = value
    with pytest.raises(EligibilityError, match="^INVALID_ELIGIBILITY_SCOPE$"):
        recorded_eligibility(**parameters)
