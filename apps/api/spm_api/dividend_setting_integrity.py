"""Shared exact checks for recorded dividend-setting transaction boundaries."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text

import spm_api.dividend_event_service as events
from spm_api.decimal_types import INT64_MAX
from spm_api.master_service import InvalidationMarker


class SettingIntegrityError(ValueError):
    """Fixed internal codes only, without account or financial details."""


def portfolio_scope(con: Connection, owner: UUID, portfolio_id: UUID) -> tuple[int, int]:
    row = con.execute(
        text("SELECT version,data_revision FROM portfolios WHERE id=:id AND owner_id=:owner"),
        {"id": str(portfolio_id), "owner": str(owner)},
    ).one_or_none()
    if row is None:
        raise SettingIntegrityError("SCOPE_CHANGED")
    if (
        type(row.version) is not int
        or not 1 <= row.version <= INT64_MAX
        or type(row.data_revision) is not int
        or not 0 <= row.data_revision <= INT64_MAX
    ):
        raise SettingIntegrityError("HISTORY_UNAVAILABLE")
    return row.version, row.data_revision


def invalidation(
    con: Connection, setting_id: UUID, portfolio_id: UUID, from_date: str, reason: str
) -> InvalidationMarker:
    rows = con.execute(
        text("""
            SELECT id,portfolio_id,from_date,reason,source_id,created_at,resolved_at
            FROM projection_invalidations WHERE source_id=:id
        """),
        {"id": str(setting_id)},
    ).all()
    if len(rows) != 1:
        raise SettingIntegrityError("HISTORY_UNAVAILABLE")
    row = rows[0]
    try:
        events._uuid(row.id)
        events._time(row.created_at)
        if (
            row.portfolio_id != str(portfolio_id)
            or row.from_date != from_date
            or row.reason != reason
            or row.source_id != str(setting_id)
            or row.resolved_at is not None
        ):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise SettingIntegrityError("HISTORY_UNAVAILABLE") from None
    return (
        row.id,
        row.portfolio_id,
        row.from_date,
        row.reason,
        row.source_id,
        row.created_at,
        None,
    )


def exact_record[M: BaseModel](model: type[M], saved: M, actual: M | None) -> M:
    """Reject substituted or non-scalar setting values, including equal numeric types."""
    if type(actual) is not model or type(saved) is not model:
        raise SettingIntegrityError("HISTORY_UNAVAILABLE")
    try:
        validated = model.model_validate(saved.model_dump(warnings="error"))
    except (ValueError, TypeError, PydanticSerializationError):
        raise SettingIntegrityError("HISTORY_UNAVAILABLE") from None
    fields = model.model_fields
    if any(record.__dict__.keys() != fields.keys() for record in (saved, actual)) or any(
        record != validated
        or any(
            type(getattr(record, name)) not in (str, int, bool, UUID, datetime, type(None))
            or type(getattr(record, name)) is not type(getattr(validated, name))
            for name in fields
        )
        for record in (saved, actual)
    ):
        raise SettingIntegrityError("HISTORY_UNAVAILABLE")
    return actual
