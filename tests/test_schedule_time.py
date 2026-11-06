"""Market-local source schedules are explicit calendar rules, never financial or coverage facts."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from spm_api.schedule_models import SourceSchedulePolicy
from spm_api.schedule_time import latest_due, next_due, occurrence_window

SECURITY = UUID("11111111-1111-4111-8111-111111111111")


def policy(**changes: Any) -> SourceSchedulePolicy:
    return SourceSchedulePolicy.model_validate(
        {
            "security_ids": [str(SECURITY)],
            "provider_ids": ["eodhd"],
            "market": "HK",
            "first_date": "2026-01-01",
            "local_run_time": "17:15",
            "cadence": "WEEKDAYS",
            "lookback_days": 30,
            **changes,
        }
    )


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_policy_requires_explicit_scope_and_does_not_enable_or_adopt_anything() -> None:
    value = policy()
    assert value.scope == "SOURCE_ONLY"
    assert value.calendar_basis == "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR"
    assert value.operations == ("DIVIDENDS",)
    assert value.security_ids == (SECURITY,)
    assert value.exchange_timezone == "Asia/Hong_Kong"
    assert "enabled" not in type(value).model_fields
    assert "credential" not in value.model_dump_json()
    with pytest.raises(ValidationError):
        value.market = "US"  # type: ignore[misc]


def test_policy_canonicalizes_sets_without_accepting_raw_overlimit_duplicates() -> None:
    value = policy(
        security_ids=[str(SECURITY), str(SECURITY)],
        provider_ids=["yfinance", "eodhd", "yahoo", "eodhd"],
        operations=["SPLITS", "DIVIDENDS"],
    )
    assert value.security_ids == (SECURITY,)
    assert value.provider_ids == ("eodhd", "yahoo", "yfinance")
    assert value.operations == ("DIVIDENDS", "SPLITS")
    assert SourceSchedulePolicy.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        policy(security_ids=[str(SECURITY)] * 101)
    with pytest.raises(ValidationError):
        policy(provider_ids=["eodhd"] * 9)


@pytest.mark.parametrize(
    "changes",
    [
        {"security_ids": []},
        {"provider_ids": []},
        {"provider_ids": ["unknown"]},
        {"provider_ids": ["manual"]},
        {"provider_ids": ["hkex_evidence"]},
        {"provider_ids": ["sec_evidence"]},
        {"operations": []},
        {"operations": ["RECEIPTS"]},
        {"market": "EU"},
        {"first_date": "0000-01-01"},
        {"first_date": "2026-02-30"},
        {"first_date": "2026-1-1"},
        {"local_run_time": "09:00"},
        {"local_run_time": "17:15:00"},
        {"local_run_time": "17:15Z"},
        {"local_run_time": "24:00"},
        {"local_run_time": "１７:１５"},
        {"local_run_time": 1715},
        {"cadence": "CRON"},
        {"weekday": 2},
        {"cadence": "WEEKLY"},
        {"cadence": "WEEKLY", "weekday": True},
        {"cadence": "WEEKLY", "weekday": 7},
        {"lookback_days": None},
        {"lookback_days": 0},
        {"lookback_days": True},
        {"lookback_days": 30.0},
        {"lookback_days": "30"},
        {"lookback_days": 3661},
        {"lookback_months": 24},
        {"lookback_days": None, "lookback_months": 0},
        {"lookback_days": None, "lookback_months": 121},
        {"lookback_days": None, "lookback_months": 1.0},
        {"backfill_from_date": "2026-01-02"},
        {"schema_version": True},
        {"schema_version": 2},
        {"scope": "FINANCIAL_SYNC"},
        {"calendar_basis": "CONFIRMED_TRADING_SESSIONS"},
        {"timezone": "Etc/GMT-8"},
        {"force": True},
        {"api_key": "synthetic-never-a-real-key"},
        {"account_id": str(SECURITY)},
        {"fx_rate_hkd": "7.8"},
    ],
)
def test_policy_rejects_implicit_unsafe_or_noncanonical_inputs(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        policy(**changes)


@pytest.mark.parametrize(
    ("reference", "expected_next", "expected_last"),
    [
        ("2026-11-06T09:14:59Z", "2026-11-06T09:15:00Z", "2026-11-05T09:15:00Z"),
        ("2026-11-06T09:15:00Z", "2026-11-09T09:15:00Z", "2026-11-06T09:15:00Z"),
        ("2026-11-07T09:15:00Z", "2026-11-09T09:15:00Z", "2026-11-06T09:15:00Z"),
        ("2026-11-08T23:59:00Z", "2026-11-09T09:15:00Z", "2026-11-06T09:15:00Z"),
    ],
)
def test_hk_week_pattern_has_exact_utc_boundaries(
    reference: str, expected_next: str, expected_last: str
) -> None:
    value = policy()
    assert next_due(value, utc(reference)) == utc(expected_next)
    assert latest_due(value, utc(reference)) == utc(expected_last)
    assert next_due(value, utc(expected_next), inclusive=True) == utc(expected_next)


@pytest.mark.parametrize(
    ("reference", "expected_next", "expected_last"),
    [
        ("2026-03-06T22:15:00Z", "2026-03-09T21:15:00Z", "2026-03-06T22:15:00Z"),
        ("2026-03-09T21:14:59Z", "2026-03-09T21:15:00Z", "2026-03-06T22:15:00Z"),
        ("2026-10-30T21:15:00Z", "2026-11-02T22:15:00Z", "2026-10-30T21:15:00Z"),
        ("2026-11-02T22:14:59Z", "2026-11-02T22:15:00Z", "2026-10-30T21:15:00Z"),
    ],
)
def test_us_schedule_reconstructs_local_time_instead_of_adding_utc_days(
    reference: str, expected_next: str, expected_last: str
) -> None:
    value = policy(market="US")
    assert value.exchange_timezone == "America/New_York"
    assert next_due(value, utc(reference)) == utc(expected_next)
    assert latest_due(value, utc(reference)) == utc(expected_last)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("2026-03-07T22:15:00Z", "2026-03-08T21:15:00Z"),
        ("2026-10-31T21:15:00Z", "2026-11-01T22:15:00Z"),
        ("2026-11-06T22:15:00Z", "2026-11-07T22:15:00Z"),
    ],
)
def test_daily_corrections_include_weekends_and_rebuild_each_dst_local_time(
    reference: str, expected: str
) -> None:
    value = policy(market="US", cadence="DAILY")
    due = utc(expected)
    assert next_due(value, utc(reference)) == due
    assert latest_due(value, due) == due
    assert occurrence_window(value, due).end == (
        due.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    )


def test_weekly_sunday_run_moves_with_dst_and_never_claims_a_trading_session() -> None:
    value = policy(market="US", cadence="WEEKLY", weekday=6)
    assert next_due(value, utc("2026-03-01T22:15:00Z")) == utc("2026-03-08T21:15:00Z")
    assert latest_due(value, utc("2026-03-08T21:15:00Z")) == utc("2026-03-08T21:15:00Z")
    assert value.calendar_basis == "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR"


def test_first_date_and_equal_reference_are_explicit_not_guessed_from_machine_today() -> None:
    value = policy(first_date="2026-11-07")
    assert latest_due(value, utc("2026-11-06T23:00:00Z")) is None
    assert next_due(value, utc("2020-01-01T00:00:00Z")) == utc("2026-11-09T09:15:00Z")
    assert next_due(value, utc("2026-11-09T09:15:00Z"), inclusive=True) == utc(
        "2026-11-09T09:15:00Z"
    )
    assert next_due(value, utc("2026-11-09T17:15:00+08:00")) == utc("2026-11-10T09:15:00Z")


def test_a_week_pattern_does_not_silently_filter_an_unverified_exchange_holiday() -> None:
    value = policy(market="US")
    assert next_due(value, utc("2026-07-02T22:00:00Z")) == utc("2026-07-03T21:15:00Z")
    assert value.model_dump(mode="json")["calendar_basis"] == (
        "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR"
    )


def test_day_window_is_inclusive_and_uses_market_date_not_the_utc_or_nas_date() -> None:
    value = policy(market="US", local_run_time="23:45")
    due = utc("2026-01-03T04:45:00Z")
    result = occurrence_window(value, due)
    assert result.start == "2025-12-04" and result.end == "2026-01-02"
    assert result.end != due.date().isoformat()


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        ("2024-02-29", 12, "2023-02-28"),
        ("2024-03-31", 1, "2024-02-29"),
        ("2025-03-31", 1, "2025-02-28"),
        ("2026-11-06", 24, "2024-11-06"),
    ],
)
def test_month_windows_use_exact_calendar_month_offsets_with_explicit_month_end_clipping(
    day: str, months: int, expected: str
) -> None:
    local_day = datetime.fromisoformat(day)
    value = policy(
        first_date="2024-01-01",
        cadence="WEEKLY",
        weekday=local_day.weekday(),
        lookback_days=None,
        lookback_months=months,
    )
    due = utc(f"{day}T09:15:00Z")
    assert occurrence_window(value, due).model_dump() == {"start": expected, "end": day}


def test_initial_backfill_is_explicit_and_never_replaces_regular_windows() -> None:
    value = policy(backfill_from_date="2000-01-01")
    due = utc("2026-11-06T09:15:00Z")
    assert occurrence_window(value, due, initial=True).start == "2000-01-01"
    assert occurrence_window(value, due).start == "2026-10-08"
    assert occurrence_window(policy(), due, initial=True) == occurrence_window(policy(), due)
    with pytest.raises(ValueError, match="INVALID_INITIAL_FLAG"):
        occurrence_window(value, due, initial="true")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "due",
    [
        "2026-11-06T09:15:00.000001Z",
        "2026-11-06T09:16:00Z",
        "2026-11-07T09:15:00Z",
        "2025-12-31T09:15:00Z",
    ],
)
def test_unaligned_or_pre_policy_occurrences_are_not_valid_windows(due: str) -> None:
    with pytest.raises(ValueError, match="INVALID_SCHEDULE_OCCURRENCE"):
        occurrence_window(policy(), utc(due))


@pytest.mark.parametrize("function", [next_due, latest_due, occurrence_window])
@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 1, 1),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=12))),
    ],
)
def test_naive_or_unrepresentable_clocks_fail_without_a_synthetic_date(
    function: Any, at: datetime
) -> None:
    with pytest.raises(ValueError, match="SCHEDULE_TIME"):
        function(policy(), at)


def test_no_future_slot_is_distinct_from_unavailable_source_or_zero_income() -> None:
    value = policy(first_date="9999-12-31")
    assert next_due(value, datetime.max.replace(tzinfo=UTC)) is None
    assert latest_due(value, datetime.max.replace(tzinfo=UTC)) == utc("9999-12-31T09:15:00Z")
    late_us = policy(market="US", first_date="9999-12-30", local_run_time="23:45")
    assert next_due(late_us, datetime.max.replace(tzinfo=UTC)) is None
    assert latest_due(late_us, datetime.max.replace(tzinfo=UTC)) == utc("9999-12-31T04:45:00Z")


@pytest.mark.parametrize("inclusive", [0, 1, None, "true"])
def test_only_a_real_boolean_can_include_the_equal_instant(inclusive: Any) -> None:
    with pytest.raises(ValueError, match="INVALID_INCLUSIVE_FLAG"):
        next_due(policy(), utc("2026-11-06T09:15:00Z"), inclusive=inclusive)


def test_lower_utc_endpoint_does_not_invent_a_previous_us_civil_day() -> None:
    value = policy(market="US", first_date="0001-01-01")
    reference = datetime.min.replace(tzinfo=UTC)
    assert latest_due(value, reference) is None
    due = next_due(value, reference)
    assert due is not None and due > reference and due.year == 1


def test_an_unrepresentable_market_occurrence_cannot_become_a_window() -> None:
    value = policy(first_date="9999-12-31")
    with pytest.raises(ValueError, match="INVALID_SCHEDULE_OCCURRENCE"):
        occurrence_window(value, datetime.max.replace(tzinfo=UTC))


def test_day_underflow_does_not_clip_to_an_invented_history_start() -> None:
    value = policy(first_date="0001-01-01")
    with pytest.raises(ValueError, match="SCHEDULE_WINDOW_OUT_OF_RANGE"):
        occurrence_window(value, utc("0001-01-01T09:38:18Z"))


def test_month_underflow_never_invents_an_arbitrary_history_start() -> None:
    value = policy(first_date="0001-01-01", lookback_days=None, lookback_months=24)
    with pytest.raises(ValueError, match="SCHEDULE_WINDOW_OUT_OF_RANGE"):
        occurrence_window(value, utc("0001-01-01T09:38:18Z"))
