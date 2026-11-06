"""Pure market-local source timing; never read secrets, enqueue work or infer market coverage."""

from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from spm_api.providers.symbols import FetchWindow
from spm_api.schedule_models import SourceSchedulePolicy


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_SCHEDULE_TIME_REQUIRED")
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("SCHEDULE_TIME_OUT_OF_RANGE") from None


def _local_date(value: datetime, zone: ZoneInfo) -> date:
    try:
        return value.astimezone(zone).date()
    except OverflowError:
        # UTC's finite endpoints can fall outside a market's representable civil date.
        return date.min if value.year == 1 else date.max


def _align(policy: SourceSchedulePolicy, day: date, *, forward: bool) -> date:
    if policy.cadence == "DAILY":
        return day
    if policy.cadence == "WEEKDAYS":
        offset = (7 - day.weekday()) if forward else (day.weekday() - 4)
        return day + timedelta(days=offset if forward else -offset) if day.weekday() > 4 else day
    weekday = cast(int, policy.weekday)  # Validated WEEKLY policies require an explicit weekday.
    offset = ((weekday - day.weekday()) if forward else (day.weekday() - weekday)) % 7
    return day + timedelta(days=offset if forward else -offset)


def _instant(policy: SourceSchedulePolicy, day: date, zone: ZoneInfo) -> datetime:
    # Late local hours avoid DST gaps/folds in these zones; rebuild each date's historical offset.
    local = datetime.combine(day, time.fromisoformat(policy.local_run_time), tzinfo=zone)
    return local.astimezone(UTC)


def next_due(
    policy: SourceSchedulePolicy, after: datetime, *, inclusive: bool = False
) -> datetime | None:
    """Return a strictly later UTC occurrence by default, or None at calendar exhaustion."""
    if type(inclusive) is not bool:
        raise ValueError("INVALID_INCLUSIVE_FLAG")
    current = _utc(after)
    zone = ZoneInfo(policy.exchange_timezone)
    try:
        day = _align(
            policy,
            max(date.fromisoformat(policy.first_date), _local_date(current, zone)),
            forward=True,
        )
        candidate = _instant(policy, day, zone)
        if candidate > current or (inclusive and candidate == current):
            return candidate
        return _instant(policy, _align(policy, day + timedelta(days=1), forward=True), zone)
    except OverflowError:
        return None


def latest_due(policy: SourceSchedulePolicy, at: datetime) -> datetime | None:
    """Find the latest elapsed slot in constant work, so recovery need not emit a backlog."""
    current = _utc(at)
    zone = ZoneInfo(policy.exchange_timezone)
    first = date.fromisoformat(policy.first_date)
    try:
        day = _align(policy, _local_date(current, zone), forward=False)
        if day < first:
            return None
        try:
            candidate = _instant(policy, day, zone)
        except OverflowError:
            candidate = None
        if candidate is not None and candidate <= current:
            return candidate
        day = _align(policy, day - timedelta(days=1), forward=False)
        return _instant(policy, day, zone) if day >= first else None
    except OverflowError:
        return None


def occurrence_window(
    policy: SourceSchedulePolicy, due_at: datetime, *, initial: bool = False
) -> FetchWindow:
    """Use the occurrence's market date; an explicit initial backfill is never automatic."""
    if type(initial) is not bool:
        raise ValueError("INVALID_INITIAL_FLAG")
    due = _utc(due_at)
    zone = ZoneInfo(policy.exchange_timezone)
    try:
        end = due.astimezone(zone).date()
        valid = (
            end >= date.fromisoformat(policy.first_date)
            and _align(policy, end, forward=True) == end
            and _instant(policy, end, zone) == due
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("INVALID_SCHEDULE_OCCURRENCE")
    try:
        if initial and policy.backfill_from_date is not None:
            start = date.fromisoformat(policy.backfill_from_date)
        elif policy.lookback_days is not None:
            start = end - timedelta(days=policy.lookback_days - 1)
        else:
            # Calendar-month windows include the clipped anniversary through the due date.
            offset = (end.year - 1) * 12 + end.month - 1 - cast(int, policy.lookback_months)
            if offset < 0:
                raise ValueError("Calendar month underflow")
            year, month = divmod(offset, 12)
            start = date(year + 1, month + 1, min(end.day, monthrange(year + 1, month + 1)[1]))
    except (ValueError, OverflowError):
        raise ValueError("SCHEDULE_WINDOW_OUT_OF_RANGE") from None
    return FetchWindow(start=start.isoformat(), end=end.isoformat())
