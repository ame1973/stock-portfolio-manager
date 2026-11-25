"""Append fee estimates and revalidate their current use without granting income."""

from datetime import UTC, date, datetime
from typing import Annotated, Any, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_fee_service as fees
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import audit, now, stamp
from spm_api.db import savepoint
from spm_api.decimal_types import INT64_MAX, IsoDate, NonNegativeMoney, to_scaled
from spm_api.dividend_account_scope import account
from spm_api.errors import Model
from spm_api.master_models import Currency
from spm_api.master_service import invalidate_portfolio

ExpectedNumber = Annotated[int, Field(strict=True, ge=0, lt=INT64_MAX)]


class FeeIntent(Model):
    """An explicit estimate and observed chain head; the caller must authenticate."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    portfolio_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: fees.Number
    broker_version: fees.Number
    expected_setting_no: ExpectedNumber
    expected_head_id: UUID | None = Field(repr=False)
    effective_from: IsoDate
    fee_status: fees.FeeStatus
    fee: NonNegativeMoney | None = Field(repr=False)
    fee_currency: Currency | None
    reason: events.Reason = Field(repr=False)

    @model_validator(mode="after")
    def complete_decision(self) -> Self:
        if (self.expected_setting_no == 0) != (self.expected_head_id is None):
            raise ValueError("An observed head must match its setting number")
        if self.fee_status == "ESTIMATED":
            if self.fee is None or self.fee_currency is None:
                raise ValueError("An estimated fee requires an explicit amount and currency")
        elif self.fee is not None:
            raise ValueError("An unknown fee cannot supply an amount")
        return self


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise fees.FeeSettingError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise fees.FeeSettingError("INVALID_DIVIDEND_FEE_SCOPE")
    try:
        active = con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
    except SQLAlchemyError:
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None
    if active is None:
        raise fees.FeeSettingError("DIVIDEND_FEE_OWNER_UNAVAILABLE")


def _current_source(
    con: Connection, owner: UUID, event_id: UUID, revision_id: UUID, revision_no: int
) -> events.ReviewedEvent:
    try:
        return event_writer.require_current_source(
            con, owner, event_id, revision_id=revision_id, version=revision_no
        )
    except events.EventError as error:
        code = str(error)
        if code not in {
            "DIVIDEND_EVENT_NOT_FOUND",
            "DIVIDEND_EVENT_VERSION_CONFLICT",
            "DIVIDEND_EVENT_SOURCE_CHANGED",
            "CANDIDATE_NOT_FOUND",
            "CANDIDATE_VERSION_CONFLICT",
            "CANDIDATE_DISMISSED",
            "CANDIDATE_SOURCE_UNAVAILABLE",
            "CANDIDATE_SOURCE_CHANGED",
            "CANDIDATE_BASIS_UNAVAILABLE",
            "CANDIDATE_BASIS_CHANGED",
        }:
            code = "DIVIDEND_FEE_HISTORY_UNAVAILABLE"
        raise fees.FeeSettingError(code) from None


def _clear(
    con: Connection, owner: UUID, event_id: UUID, revision_id: UUID, revision_no: int
) -> None:
    try:
        rejections.require_clear(con, owner, event_id, revision_id=revision_id, version=revision_no)
    except rejections.RejectionError as error:
        code = str(error)
        if code not in {"DIVIDEND_REVISION_REJECTED", "DIVIDEND_REJECTION_VERSION_CONFLICT"}:
            code = "DIVIDEND_FEE_HISTORY_UNAVAILABLE"
        raise fees.FeeSettingError(code) from None


def _matches_account(record: fees.FeeSetting | FeeIntent, current: RowMapping) -> bool:
    return bool(
        str(record.account_id) == current["id"]
        and str(record.portfolio_id) == current["portfolio_id"]
        and str(record.broker_id) == current["broker_id"]
        and record.account_version == current["account_version"]
        and record.broker_version == current["broker_version"]
    )


def require_current(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    revision_id: UUID,
    revision_no: int,
    account_id: UUID,
    *,
    effective_on: date,
    expected_setting_no: int,
    expected_head_id: UUID | None,
    expected_portfolio_id: UUID,
    expected_broker_id: UUID,
    expected_account_version: int,
    expected_broker_version: int,
) -> fees.FeeSelection:
    """Revalidate source, rejection, account and fee head; this is not authorization."""
    _writer(con, owner)
    if (
        not all(
            isinstance(value, UUID)
            for value in (
                event_id,
                revision_id,
                account_id,
                expected_portfolio_id,
                expected_broker_id,
            )
        )
        or type(expected_account_version) is not int
        or not 1 <= expected_account_version <= INT64_MAX
        or type(expected_broker_version) is not int
        or not 1 <= expected_broker_version <= INT64_MAX
        or type(revision_no) is not int
        or not 1 <= revision_no <= 2_147_483_647
        or type(effective_on) is not date
        or type(expected_setting_no) is not int
        or not 0 <= expected_setting_no <= INT64_MAX
        or (expected_setting_no == 0 and expected_head_id is not None)
        or (expected_setting_no > 0 and not isinstance(expected_head_id, UUID))
    ):
        raise fees.FeeSettingError("INVALID_DIVIDEND_FEE_SCOPE")
    try:
        _current_source(con, owner, event_id, revision_id, revision_no)
        _clear(con, owner, event_id, revision_id, revision_no)
        retained = fees.history(con, owner, event_id, revision_id, revision_no, account_id)
        current = account(con, owner, account_id)
        if retained is None or current is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_NOT_FOUND")
        selected = fees.select(retained, effective_on)
        if (selected.head_no, selected.head_id) != (expected_setting_no, expected_head_id):
            raise fees.FeeSettingError("DIVIDEND_FEE_SETTING_CHANGED")
        if (
            current["portfolio_id"] != str(expected_portfolio_id)
            or current["broker_id"] != str(expected_broker_id)
            or current["account_version"] != expected_account_version
            or current["broker_version"] != expected_broker_version
            or (selected.selected is not None and not _matches_account(selected.selected, current))
        ):
            raise fees.FeeSettingError("DIVIDEND_FEE_SCOPE_CHANGED")
        return selected
    except fees.FeeSettingError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None


def _replay_matches(record: fees.FeeSetting, intent: FeeIntent, request_id: UUID) -> bool:
    return bool(
        record.id == intent.id
        and record.request_id == request_id
        and record.setting_no == intent.expected_setting_no + 1
        and record.previous_id == intent.expected_head_id
        and record.portfolio_id == intent.portfolio_id
        and record.broker_id == intent.broker_id
        and record.account_version == intent.account_version
        and record.broker_version == intent.broker_version
        and record.effective_from == intent.effective_from
        and record.fee_status == intent.fee_status
        and record.fee == intent.fee
        and record.fee_currency == intent.fee_currency
        and record.reason == intent.reason
    )


def _portfolio_scope(con: Connection, owner: UUID, portfolio_id: UUID) -> tuple[int, int]:
    row = con.execute(
        text("SELECT version,data_revision FROM portfolios WHERE id=:id AND owner_id=:owner"),
        {"id": str(portfolio_id), "owner": str(owner)},
    ).one_or_none()
    if row is None:
        raise fees.FeeSettingError("DIVIDEND_FEE_SCOPE_CHANGED")
    if (
        type(row.version) is not int
        or not 1 <= row.version <= INT64_MAX
        or type(row.data_revision) is not int
        or not 0 <= row.data_revision <= INT64_MAX
    ):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
    return row.version, row.data_revision


def _invalidation(
    con: Connection, intent: FeeIntent, from_date: str
) -> tuple[str, str, str, str, str, str, None]:
    rows = con.execute(
        text("""
            SELECT id,portfolio_id,from_date,reason,source_id,created_at,resolved_at
            FROM projection_invalidations WHERE source_id=:id
        """),
        {"id": str(intent.id)},
    ).all()
    if len(rows) != 1:
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
    row = rows[0]
    try:
        events._uuid(row.id)
        events._time(row.created_at)
        if (
            row.portfolio_id != str(intent.portfolio_id)
            or row.from_date != from_date
            or row.reason != "DIVIDEND_FEE_SETTING_CHANGED"
            or row.source_id != str(intent.id)
            or row.resolved_at is not None
        ):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None
    return (
        row.id,
        row.portfolio_id,
        row.from_date,
        row.reason,
        row.source_id,
        row.created_at,
        None,
    )


def _require_saved(
    con: Connection, owner: UUID, intent: FeeIntent, request_id: UUID, saved: fees.FeeSetting
) -> fees.FeeSetting:
    selected = require_current(
        con,
        owner,
        intent.event_id,
        intent.revision_id,
        intent.revision_no,
        intent.account_id,
        effective_on=date.fromisoformat(intent.effective_from),
        expected_setting_no=intent.expected_setting_no + 1,
        expected_head_id=intent.id,
        expected_portfolio_id=intent.portfolio_id,
        expected_broker_id=intent.broker_id,
        expected_account_version=intent.account_version,
        expected_broker_version=intent.broker_version,
    )
    actual = selected.selected
    if type(actual) is not fees.FeeSetting or type(saved) is not fees.FeeSetting:
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
    try:
        validated = fees.FeeSetting.model_validate(saved.model_dump(warnings="error"))
    except (ValueError, TypeError, PydanticSerializationError):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None
    fields = fees.FeeSetting.model_fields
    if (
        any(record.__dict__.keys() != fields.keys() for record in (saved, actual))
        or any(
            record != validated
            or any(
                type(getattr(record, name)) is not type(getattr(validated, name)) for name in fields
            )
            for record in (saved, actual)
        )
        or not _replay_matches(actual, intent, request_id)
        or (saved.owner_id, saved.event_id, saved.revision_id, saved.revision_no, saved.account_id)
        != (owner, intent.event_id, intent.revision_id, intent.revision_no, intent.account_id)
    ):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
    return actual


def save(con: Connection, owner: UUID, intent: FeeIntent, *, request_id: UUID) -> fees.FeeSetting:
    """Append an audited estimate and invalidate atomically; authenticate separately."""
    try:
        return _save(con, owner, intent, request_id=request_id)
    except fees.FeeSettingError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None


def _save(con: Connection, owner: UUID, intent: FeeIntent, *, request_id: UUID) -> fees.FeeSetting:
    _writer(con, owner)
    try:
        if not isinstance(intent, FeeIntent) or not isinstance(request_id, UUID):
            raise ValueError
        validated = FeeIntent.model_validate(intent.model_dump(warnings="error"))
        if validated != intent:
            raise ValueError
        intent = validated
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise fees.FeeSettingError("INVALID_DIVIDEND_FEE_SCOPE") from None

    with savepoint(con):
        retained = fees.history(
            con, owner, intent.event_id, intent.revision_id, intent.revision_no, intent.account_id
        )
        if retained is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_NOT_FOUND")
        for record in retained:
            if record.id == intent.id:
                if not _replay_matches(record, intent, request_id):
                    raise fees.FeeSettingError("DIVIDEND_FEE_CONFLICT")
                return record
        if any(record.request_id == request_id for record in retained):
            raise fees.FeeSettingError("DIVIDEND_FEE_CONFLICT")
        if con.execute(
            text("SELECT 1 FROM dividend_fee_settings WHERE id=:id"), {"id": str(intent.id)}
        ).first():
            raise fees.FeeSettingError("DIVIDEND_FEE_CONFLICT")
        if con.execute(
            text(
                "SELECT 1 FROM audit_events WHERE entity_type='dividend_fee_setting' "
                "AND entity_id=:id LIMIT 1"
            ),
            {"id": str(intent.id)},
        ).first():
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        previous = retained[-1] if retained else None
        if (len(retained), previous.id if previous else None) != (
            intent.expected_setting_no,
            intent.expected_head_id,
        ):
            raise fees.FeeSettingError("DIVIDEND_FEE_VERSION_CONFLICT")
        event = _current_source(con, owner, intent.event_id, intent.revision_id, intent.revision_no)
        _clear(con, owner, intent.event_id, intent.revision_id, intent.revision_no)
        current = account(con, owner, intent.account_id)
        if current is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_NOT_FOUND")
        if not _matches_account(intent, current):
            raise fees.FeeSettingError("DIVIDEND_FEE_SCOPE_CHANGED")
        portfolio_version, data_revision = _portfolio_scope(con, owner, intent.portfolio_id)
        source = con.execute(
            text("""
            SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'
              AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'
        """),
            {"id": str(intent.revision_id)},
        ).all()
        if len(source) != 1:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        previous_audit_time = (
            events._time(
                con.execute(
                    text("SELECT created_at FROM audit_events WHERE id=:id"),
                    {"id": str(previous.audit_id)},
                ).scalar_one()
            )
            if previous is not None
            else None
        )
        at = now()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        at = at.astimezone(UTC)
        if (
            at < event.record.created_at
            or at < events._time(source[0].created_at)
            or (previous is not None and at < previous.created_at)
            or (previous_audit_time is not None and at < previous_audit_time)
        ):
            raise fees.FeeSettingError("DIVIDEND_FEE_TIME_CONFLICT")
        values: dict[str, Any] = {
            "id": str(intent.id),
            "owner_id": str(owner),
            "event_id": str(intent.event_id),
            "revision_id": str(intent.revision_id),
            "revision_no": intent.revision_no,
            "portfolio_id": str(intent.portfolio_id),
            "account_id": str(intent.account_id),
            "broker_id": str(intent.broker_id),
            "account_version": intent.account_version,
            "broker_version": intent.broker_version,
            "setting_no": intent.expected_setting_no + 1,
            "previous_id": str(previous.id) if previous else None,
            "effective_from": intent.effective_from,
            "fee_status": intent.fee_status,
            "fee_micro": to_scaled(intent.fee, 6) if intent.fee is not None else None,
            "fee_currency": intent.fee_currency,
            "request_id": str(request_id),
            "created_at": stamp(at),
        }
        audit(
            con,
            str(owner),
            "dividend_fee_setting",
            str(intent.id),
            "DIVIDEND_FEE_SETTING_RECORDED",
            request_id=str(request_id),
            before=fees._payload(fees._storage(previous), previous.reason) if previous else None,
            after=fees._payload(values, intent.reason),
            reason=intent.reason,
        )
        entries = con.execute(
            text(
                "SELECT id,created_at FROM audit_events WHERE entity_type='dividend_fee_setting' "
                "AND entity_id=:id"
            ),
            {"id": str(intent.id)},
        ).all()
        if len(entries) != 1:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        if events._time(entries[0].created_at) < at:
            raise fees.FeeSettingError("DIVIDEND_FEE_TIME_CONFLICT")
        values["audit_id"] = entries[0].id
        columns = ",".join(values)
        parameters = ",".join(":" + key for key in values)
        con.execute(
            text(f"INSERT INTO dividend_fee_settings({columns}) VALUES ({parameters})"), values
        )
        updated = fees.history(
            con, owner, intent.event_id, intent.revision_id, intent.revision_no, intent.account_id
        )
        if updated is None or len(updated) != len(retained) + 1 or updated[-1].id != intent.id:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        day = event.record.terms.ex_date
        if day is None:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        from_date = min(intent.effective_from, day)
        marker = invalidate_portfolio(
            con,
            str(intent.portfolio_id),
            str(intent.id),
            "DIVIDEND_FEE_SETTING_CHANGED",
            from_date,
        )
        saved = _require_saved(con, owner, intent, request_id, updated[-1])
        if _portfolio_scope(con, owner, intent.portfolio_id) != (
            portfolio_version,
            data_revision + 1,
        ):
            raise fees.FeeSettingError("DIVIDEND_FEE_SCOPE_CHANGED")
        if _invalidation(con, intent, from_date) != marker:
            raise fees.FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE")
        return saved
