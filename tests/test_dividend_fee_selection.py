"""Pure fee selection preserves explicit unknowns and validated historical prefixes."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.dividend_fee_service import FeeSetting, FeeSettingError, select


@pytest.fixture
def scope() -> dict[str, Any]:
    return {
        "owner_id": uuid4(),
        "event_id": uuid4(),
        "revision_id": uuid4(),
        "revision_no": 1,
        "portfolio_id": uuid4(),
        "account_id": uuid4(),
        "broker_id": uuid4(),
        "account_version": 1,
        "broker_version": 1,
    }


def setting(
    scope: dict[str, Any],
    number: int,
    previous: FeeSetting | None = None,
    **changes: Any,
) -> FeeSetting:
    return FeeSetting.model_validate(
        {
            **scope,
            "id": uuid4(),
            "setting_no": number,
            "previous_id": previous.id if previous else None,
            "effective_from": "2026-01-01",
            "fee_status": "ESTIMATED",
            "fee": "0.000000",
            "fee_currency": "HKD",
            "request_id": uuid4(),
            "audit_id": uuid4(),
            "created_at": datetime(2026, 1, 2, tzinfo=UTC) + timedelta(seconds=number),
            "reason": "Synthetic explicit fee estimate only.",
            **changes,
        }
    )


def test_missing_unknown_and_explicit_zero_are_never_conflated(scope: dict[str, Any]) -> None:
    day = date(2026, 1, 8)
    missing = select([], day)
    assert missing.status == "MISSING" and missing.selected is None
    assert missing.head_no == 0 and missing.head_id is None
    zero = setting(scope, 1)
    explicit = select([zero], day)
    assert explicit.status == "ESTIMATED" and explicit.selected == zero
    assert explicit.selected.fee == "0.000000" and explicit.selected.fee_currency == "HKD"
    unknown = setting(scope, 2, zero, fee_status="UNKNOWN", fee=None, fee_currency="USD")
    selected = select([zero, unknown], day)
    assert selected.status == "UNKNOWN" and selected.selected == unknown
    assert selected.selected.fee is None and selected.selected.fee_currency == "USD"
    assert selected.head_no == 2 and selected.head_id == unknown.id
    assert selected.scope == "UNVERIFIED_FEE_SELECTION_ONLY"


def test_effective_day_and_observed_history_prefix_are_explicit(scope: dict[str, Any]) -> None:
    old = setting(scope, 1, effective_from="2026-01-01", fee="1.000000")
    future = setting(scope, 2, old, effective_from="2026-03-01", fee="2.000000")
    backdated = setting(scope, 3, future, effective_from="2026-01-01", fee="3.000000")
    early = select([old, future], date(2025, 12, 31))
    assert early.status == "MISSING" and early.head_no == 2 and early.head_id == future.id
    historical = select([old, future], date(2026, 1, 15))
    assert historical.selected == old and historical.head_id == future.id
    current = select([old, future, backdated], date(2026, 1, 15))
    assert current.selected == backdated and current.head_id == backdated.id
    assert select([old, future, backdated], date(2026, 3, 1)).selected == future
    assert historical.selected == old and historical.head_id == future.id


def test_scope_changes_remain_historical_and_do_not_authorize_current_use(
    scope: dict[str, Any],
) -> None:
    original = setting(scope, 1, effective_from="2026-01-01", fee="1.000000")
    moved = setting(
        scope,
        2,
        original,
        effective_from="2026-02-01",
        portfolio_id=uuid4(),
        broker_id=uuid4(),
        account_version=2,
        broker_version=2,
        fee_status="UNKNOWN",
        fee=None,
        fee_currency=None,
    )
    old_day = select([original, moved], date(2026, 1, 15))
    assert old_day.head_id == moved.id and old_day.selected == original
    assert old_day.scope == "UNVERIFIED_FEE_SELECTION_ONLY"
    assert select([original, moved], date(2026, 2, 1)).selected == moved


def test_noncanonical_constructed_history_is_not_silently_normalized(scope: dict[str, Any]) -> None:
    first = setting(scope, 1).model_copy(update={"fee": "1"})
    with pytest.raises(FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        select([first], date(2026, 1, 1))


def test_private_values_do_not_appear_in_selection_representations(scope: dict[str, Any]) -> None:
    value = setting(scope, 1, fee="123456.789012", reason="Synthetic private fee rationale.")
    for rendered in (repr(value), repr(select([value], date(2026, 1, 1)))):
        assert "123456.789012" not in rendered and "Synthetic private" not in rendered
        assert str(value.account_id) not in rendered and str(value.request_id) not in rendered


@pytest.mark.parametrize(
    "changes",
    [
        {"fee_status": "ESTIMATED", "fee": None},
        {"fee_status": "ESTIMATED", "fee_currency": None},
        {"fee_status": "UNKNOWN", "fee": "0"},
        {"fee_status": "UNKNOWN", "fee": None, "fee_currency": "CNY"},
        {"fee_status": "CONFIRMED"},
        {"fee": "-0.000001"},
        {"fee": "0.0000001"},
        {"fee": 0.0},
        {"fee": "9223372036854.775808"},
        {"account_version": 0},
        {"account_version": 9223372036854775808},
        {"broker_version": True},
        {"effective_from": "2026-02-30"},
        {"setting_no": 2, "previous_id": None},
        {"setting_no": 1, "previous_id": uuid4()},
    ],
)
def test_invalid_fee_assertion_is_not_rounded_or_filled(
    scope: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        setting(scope, 1, **changes)


def test_exact_int64_amount_is_preserved_as_decimal_string(scope: dict[str, Any]) -> None:
    maximum = setting(
        scope,
        1,
        fee="9223372036854.775807",
        fee_currency="USD",
        account_version=9223372036854775807,
        broker_version=9007199254740993,
    )
    assert select([maximum], date(2026, 1, 1)).selected == maximum
    assert maximum.fee == "9223372036854.775807"
    assert maximum.account_version == 9223372036854775807
    assert maximum.broker_version == 9007199254740993


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("setting_no", 3),
        ("previous_id", uuid4()),
        ("owner_id", uuid4()),
        ("event_id", uuid4()),
        ("revision_id", uuid4()),
        ("revision_no", 2),
        ("account_id", uuid4()),
        ("created_at", datetime(2025, 1, 1, tzinfo=UTC)),
        ("fee", 0.0),
        ("fee_currency", "CNY"),
        ("fee_status", "CONFIRMED"),
    ],
)
def test_corrupt_later_history_cannot_be_hidden_by_an_earlier_effective_day(
    scope: dict[str, Any], field: str, value: Any
) -> None:
    first = setting(scope, 1)
    second = setting(scope, 2, first, effective_from="2027-01-01")
    corrupt = second.model_copy(update={field: value})
    with pytest.raises(FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        select([first, corrupt], date(2026, 1, 1))


@pytest.mark.parametrize("field", ["id", "request_id", "audit_id"])
def test_reused_identity_request_or_audit_fails_closed(scope: dict[str, Any], field: str) -> None:
    first = setting(scope, 1)
    second = setting(scope, 2, first).model_copy(update={field: getattr(first, field)})
    with pytest.raises(FeeSettingError, match="^DIVIDEND_FEE_HISTORY_UNAVAILABLE$"):
        select([first, second], date(2026, 1, 1))


@pytest.mark.parametrize("bad", [None, "not-a-sequence", b"no", {}, [object()]])
def test_bad_history_input_is_rejected_without_private_values(bad: Any) -> None:
    with pytest.raises(FeeSettingError):
        select(bad, date(2026, 1, 1))


def test_datetime_is_not_silently_truncated_to_an_effective_day() -> None:
    with pytest.raises(FeeSettingError, match="^INVALID_DIVIDEND_FEE_SCOPE$"):
        select([], datetime(2026, 1, 1, tzinfo=UTC))
