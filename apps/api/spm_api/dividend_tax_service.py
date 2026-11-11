"""Audit and select explicit dividend tax estimates without granting financial use."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import savepoint
from spm_api.decimal_types import INT64_MAX, IsoDate, Rate, from_scaled, to_scaled
from spm_api.errors import Model
from spm_api.master_service import invalidate_portfolio

Number = Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
ExpectedNumber = Annotated[int, Field(strict=True, ge=0, lt=INT64_MAX)]
TaxStatus = Literal["ESTIMATED", "UNKNOWN"]


class TaxSettingError(ValueError):
    """Fixed codes only; do not expose account, rate or private rationale."""


class SettingIntent(Model):
    """A proposed decision with explicitly observed event and account scope."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: Number
    broker_version: Number
    expected_setting_no: ExpectedNumber
    effective_from: IsoDate
    rate: Rate | None = Field(repr=False)
    reason: events.Reason = Field(repr=False)


class TaxSetting(Model):
    """Retained owner assertion; a matching audit does not authenticate or confirm tax."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    owner_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: Number
    broker_version: Number
    setting_no: Number
    previous_id: UUID | None = Field(repr=False)
    effective_from: IsoDate
    tax_status: TaxStatus
    rate: Rate | None = Field(repr=False)
    request_id: UUID = Field(repr=False)
    audit_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True, repr=False)]
    reason: events.Reason = Field(repr=False)
    scope: Literal["OWNER_ESTIMATE_ONLY"] = "OWNER_ESTIMATE_ONLY"

    @model_validator(mode="after")
    def complete_decision(self) -> Self:
        if (self.setting_no == 1) != (self.previous_id is None):
            raise ValueError("A setting must identify its predecessor")
        if (self.tax_status == "ESTIMATED") != (self.rate is not None):
            raise ValueError("An estimated setting requires an explicit rate")
        return self


@dataclass(frozen=True, slots=True)
class TaxSelection:
    """One selected assertion and observed history head, not a withholding confirmation."""

    effective_on: date
    head_no: int
    head_id: UUID | None = field(repr=False)
    selected: TaxSetting | None = field(repr=False)
    status: Literal["MISSING", "UNKNOWN", "ESTIMATED"]
    scope: Literal["OWNER_ESTIMATE_ONLY"] = "OWNER_ESTIMATE_ONLY"


_FIELDS = (
    "id",
    "owner_id",
    "event_id",
    "revision_id",
    "revision_no",
    "portfolio_id",
    "account_id",
    "broker_id",
    "account_version",
    "broker_version",
    "setting_no",
    "previous_id",
    "effective_from",
    "tax_status",
    "rate_ppm",
    "request_id",
)


def _storage(record: TaxSetting) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "owner_id": str(record.owner_id),
        "event_id": str(record.event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "portfolio_id": str(record.portfolio_id),
        "account_id": str(record.account_id),
        "broker_id": str(record.broker_id),
        "account_version": record.account_version,
        "broker_version": record.broker_version,
        "setting_no": record.setting_no,
        "previous_id": str(record.previous_id) if record.previous_id is not None else None,
        "effective_from": record.effective_from,
        "tax_status": record.tax_status,
        "rate_ppm": to_scaled(record.rate, 6) if record.rate is not None else None,
        "request_id": str(record.request_id),
        "audit_id": str(record.audit_id),
        "created_at": stamp(record.created_at.astimezone(UTC)),
    }


def _payload(values: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        **{key: values[key] for key in _FIELDS},
        "reason_sha256": digest(canonical(reason)),
        "scope": "OWNER_ESTIMATE_ONLY",
    }


def _scope(
    con: Connection, owner: UUID, event_id: UUID, revision_id: UUID, revision_no: int, account: UUID
) -> None:
    if (
        not con.in_transaction()
        or not all(isinstance(value, UUID) for value in (owner, event_id, revision_id, account))
        or type(revision_no) is not int
        or not 1 <= revision_no <= 2_147_483_647
    ):
        raise TaxSettingError("INVALID_DIVIDEND_TAX_SCOPE")


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise TaxSettingError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise TaxSettingError("INVALID_DIVIDEND_TAX_SCOPE")
    try:
        active = con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
    except SQLAlchemyError:
        raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE") from None
    if active is None:
        raise TaxSettingError("DIVIDEND_TAX_OWNER_UNAVAILABLE")


def _integer(value: object) -> int:
    if type(value) is not int or not 1 <= value <= INT64_MAX:
        raise ValueError("Expected a positive version")
    return value


def _account(con: Connection, owner: UUID, identifier: UUID) -> RowMapping | None:
    row = (
        con.execute(
            text("""
            SELECT a.id,a.portfolio_id,a.broker_id,a.version AS account_version,
                   p.owner_id AS portfolio_owner,b.owner_id AS broker_owner,
                   b.version AS broker_version
            FROM accounts a LEFT JOIN portfolios p ON p.id=a.portfolio_id
              LEFT JOIN brokers b ON b.id=a.broker_id WHERE a.id=:account
        """),
            {"account": str(identifier)},
        )
        .mappings()
        .first()
    )
    if row is None or row["portfolio_owner"] != str(owner):
        return None
    if row["broker_owner"] != str(owner):
        raise ValueError("Inconsistent owned account")
    for key in ("id", "portfolio_id", "broker_id"):
        events._uuid(row[key])
    _integer(row["account_version"])
    _integer(row["broker_version"])
    return row


def _matches_account(record: TaxSetting | SettingIntent, account: RowMapping) -> bool:
    return bool(
        str(record.account_id) == account["id"]
        and str(record.portfolio_id) == account["portfolio_id"]
        and str(record.broker_id) == account["broker_id"]
        and record.account_version == account["account_version"]
        and record.broker_version == account["broker_version"]
    )


def history(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    revision_id: UUID,
    revision_no: int,
    account_id: UUID,
) -> tuple[TaxSetting, ...] | None:
    """Validate one complete retained chain in a snapshot, not authentication or eligibility."""
    _scope(con, owner, event_id, revision_id, revision_no, account_id)
    try:
        head = events.get(con, owner, event_id)
        account = _account(con, owner, account_id)
        if head is None or account is None:
            return None
        event = (
            head
            if head.record.revision_no == revision_no
            else events.get(con, owner, event_id, version=revision_no)
        )
        if event is None or event.record.revision_id != revision_id:
            return None
        source_audits = con.execute(
            text("""
            SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'
              AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'
        """),
            {"id": str(revision_id)},
        ).all()
        if len(source_audits) != 1:
            raise ValueError("Missing source audit")
        source_audit_time = events._time(source_audits[0].created_at)
        rows = con.execute(
            text("""
            SELECT t.*,p.owner_id AS retained_portfolio_owner,
                   b.owner_id AS retained_broker_owner,b.version AS retained_broker_version
            FROM dividend_tax_settings t
            LEFT JOIN portfolios p ON p.id=t.portfolio_id
            LEFT JOIN brokers b ON b.id=t.broker_id
            WHERE t.event_id=:event AND t.revision_id=:revision AND t.account_id=:account
            ORDER BY t.setting_no
        """),
            {"event": str(event_id), "revision": str(revision_id), "account": str(account_id)},
        ).mappings()
        result: list[TaxSetting] = []
        identities: set[UUID] = set()
        requests: set[UUID] = set()
        audits_seen: set[UUID] = set()
        # ponytail: verify the whole chain; add verified checkpoints if histories become large.
        for number, row in enumerate(rows, 1):
            identifier, request, audit_id = (
                events._uuid(row["id"]),
                events._uuid(row["request_id"]),
                events._uuid(row["audit_id"]),
            )
            audits = (
                con.execute(
                    text(
                        "SELECT * FROM audit_events WHERE entity_type='dividend_tax_setting' "
                        "AND entity_id=:id"
                    ),
                    {"id": str(identifier)},
                )
                .mappings()
                .all()
            )
            if len(audits) != 1:
                raise ValueError("Missing or duplicate setting audit")
            entry = audits[0]
            rate = row["rate_ppm"]
            record = TaxSetting.model_validate(
                {
                    "id": identifier,
                    "owner_id": events._uuid(row["owner_id"]),
                    "event_id": events._uuid(row["event_id"]),
                    "revision_id": events._uuid(row["revision_id"]),
                    "revision_no": row["revision_no"],
                    "portfolio_id": events._uuid(row["portfolio_id"]),
                    "account_id": events._uuid(row["account_id"]),
                    "broker_id": events._uuid(row["broker_id"]),
                    "account_version": row["account_version"],
                    "broker_version": row["broker_version"],
                    "setting_no": row["setting_no"],
                    "previous_id": (
                        events._uuid(row["previous_id"]) if row["previous_id"] is not None else None
                    ),
                    "effective_from": row["effective_from"],
                    "tax_status": row["tax_status"],
                    "rate": from_scaled(rate, 6) if rate is not None else None,
                    "request_id": request,
                    "audit_id": audit_id,
                    "created_at": events._time(row["created_at"]),
                    "reason": entry["reason"],
                }
            )
            previous = result[-1] if result else None
            values = _storage(record)
            if (
                any(row[key] != value for key, value in values.items())
                or record.owner_id != owner
                or record.event_id != event_id
                or record.revision_id != revision_id
                or record.revision_no != revision_no
                or record.account_id != account_id
                or record.setting_no != number
                or record.previous_id != (previous.id if previous else None)
                or row["retained_portfolio_owner"] != str(owner)
                or row["retained_broker_owner"] != str(owner)
                or record.account_version > _integer(account["account_version"])
                or record.broker_version > _integer(row["retained_broker_version"])
                or record.created_at < event.record.created_at
                or record.created_at < source_audit_time
                or (previous is not None and record.created_at < previous.created_at)
                or identifier in identities
                or request in requests
                or audit_id in audits_seen
                or entry["id"] != str(audit_id)
                or entry["owner_id"] != str(owner)
                or entry["action"] != "DIVIDEND_TAX_SETTING_RECORDED"
                or entry["request_id"] != str(request)
                or entry["reason"] != record.reason
                or events._time(entry["created_at"]) != record.created_at
                or entry["before_sha256"]
                != (
                    digest(canonical(_payload(_storage(previous), previous.reason)))
                    if previous
                    else None
                )
                or entry["after_sha256"] != digest(canonical(_payload(values, record.reason)))
            ):
                raise ValueError("Inconsistent setting history")
            identities.add(identifier)
            requests.add(request)
            audits_seen.add(audit_id)
            result.append(record)
        return tuple(result)
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE") from None


def select(settings: Sequence[TaxSetting], effective_on: date) -> TaxSelection:
    """Select a complete supplied history or explicit historical prefix, without DB authority."""
    if (
        not isinstance(settings, Sequence)
        or isinstance(settings, (str, bytes))
        or type(effective_on) is not date
    ):
        raise TaxSettingError("INVALID_DIVIDEND_TAX_SCOPE")
    try:
        records: list[TaxSetting] = []
        identities: set[UUID] = set()
        requests: set[UUID] = set()
        audits_seen: set[UUID] = set()
        for number, item in enumerate(settings, 1):
            if not isinstance(item, TaxSetting):
                raise ValueError
            record = TaxSetting.model_validate(item.model_dump(warnings="error"))
            previous = records[-1] if records else None
            if (
                record != item
                or record.setting_no != number
                or record.previous_id != (previous.id if previous else None)
                or record.id in identities
                or record.request_id in requests
                or record.audit_id in audits_seen
                or (
                    previous is not None
                    and (
                        record.created_at < previous.created_at
                        or (
                            record.owner_id,
                            record.event_id,
                            record.revision_id,
                            record.revision_no,
                            record.account_id,
                        )
                        != (
                            previous.owner_id,
                            previous.event_id,
                            previous.revision_id,
                            previous.revision_no,
                            previous.account_id,
                        )
                    )
                )
            ):
                raise ValueError
            identities.add(record.id)
            requests.add(record.request_id)
            audits_seen.add(record.audit_id)
            records.append(record)
        applicable = (
            item for item in records if date.fromisoformat(item.effective_from) <= effective_on
        )
        chosen = max(
            applicable, key=lambda item: (item.effective_from, item.setting_no), default=None
        )
        last = records[-1] if records else None
        return TaxSelection(
            effective_on=effective_on,
            head_no=last.setting_no if last else 0,
            head_id=last.id if last else None,
            selected=chosen,
            status=chosen.tax_status if chosen else "MISSING",
        )
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE") from None


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
            "DIVIDEND_EVENT_HISTORY_UNAVAILABLE",
            "CANDIDATE_NOT_FOUND",
            "CANDIDATE_VERSION_CONFLICT",
            "CANDIDATE_DISMISSED",
            "CANDIDATE_HISTORY_UNAVAILABLE",
            "CANDIDATE_SOURCE_UNAVAILABLE",
            "CANDIDATE_SOURCE_CHANGED",
            "CANDIDATE_BASIS_UNAVAILABLE",
            "CANDIDATE_BASIS_CHANGED",
        }:
            code = "DIVIDEND_TAX_HISTORY_UNAVAILABLE"
        raise TaxSettingError(code) from None


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
) -> TaxSelection:
    """Recheck source, exact setting head and account now; callers still owe authorization."""
    _writer(con, owner)
    _scope(con, owner, event_id, revision_id, revision_no, account_id)
    if (
        type(effective_on) is not date
        or type(expected_setting_no) is not int
        or not 0 <= expected_setting_no <= INT64_MAX
        or (expected_setting_no == 0 and expected_head_id is not None)
        or (expected_setting_no > 0 and not isinstance(expected_head_id, UUID))
    ):
        raise TaxSettingError("INVALID_DIVIDEND_TAX_SCOPE")
    try:
        _current_source(con, owner, event_id, revision_id, revision_no)
        settings = history(con, owner, event_id, revision_id, revision_no, account_id)
        account = _account(con, owner, account_id)
        if settings is None or account is None:
            raise TaxSettingError("DIVIDEND_TAX_NOT_FOUND")
        selected = select(settings, effective_on)
        if (selected.head_no, selected.head_id) != (expected_setting_no, expected_head_id):
            raise TaxSettingError("DIVIDEND_TAX_SETTING_CHANGED")
        if selected.selected is not None and not _matches_account(selected.selected, account):
            raise TaxSettingError("DIVIDEND_TAX_SCOPE_CHANGED")
        return selected
    except TaxSettingError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE") from None


def save(con: Connection, owner: UUID, intent: SettingIntent, *, request_id: UUID) -> TaxSetting:
    """Append an audited estimate and invalidate its portfolio; caller must authenticate."""
    try:
        return _save(con, owner, intent, request_id=request_id)
    except SQLAlchemyError:
        raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE") from None


def _save(con: Connection, owner: UUID, intent: SettingIntent, *, request_id: UUID) -> TaxSetting:
    _writer(con, owner)
    try:
        if not isinstance(intent, SettingIntent) or not isinstance(request_id, UUID):
            raise ValueError
        intent = SettingIntent.model_validate(intent.model_dump(warnings="error"))
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise TaxSettingError("INVALID_DIVIDEND_TAX_SCOPE") from None
    with savepoint(con):
        try:
            settings = history(
                con,
                owner,
                intent.event_id,
                intent.revision_id,
                intent.revision_no,
                intent.account_id,
            )
            if settings is None:
                raise TaxSettingError("DIVIDEND_TAX_NOT_FOUND")
            for item in settings:
                if item.id == intent.id:
                    if (
                        item.request_id != request_id
                        or item.setting_no != intent.expected_setting_no + 1
                        or item.portfolio_id != intent.portfolio_id
                        or item.broker_id != intent.broker_id
                        or item.account_version != intent.account_version
                        or item.broker_version != intent.broker_version
                        or item.effective_from != intent.effective_from
                        or item.rate != intent.rate
                        or item.reason != intent.reason
                    ):
                        raise TaxSettingError("DIVIDEND_TAX_CONFLICT")
                    return item
            if any(item.request_id == request_id for item in settings):
                raise TaxSettingError("DIVIDEND_TAX_CONFLICT")
            if con.execute(
                text("SELECT 1 FROM dividend_tax_settings WHERE id=:id"), {"id": str(intent.id)}
            ).first():
                raise TaxSettingError("DIVIDEND_TAX_CONFLICT")
            if con.execute(
                text(
                    "SELECT 1 FROM audit_events WHERE entity_type='dividend_tax_setting' "
                    "AND entity_id=:id LIMIT 1"
                ),
                {"id": str(intent.id)},
            ).first():
                raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE")
            if len(settings) != intent.expected_setting_no:
                raise TaxSettingError("DIVIDEND_TAX_VERSION_CONFLICT")
            event = _current_source(
                con, owner, intent.event_id, intent.revision_id, intent.revision_no
            )
            account = _account(con, owner, intent.account_id)
            if account is None:
                raise TaxSettingError("DIVIDEND_TAX_NOT_FOUND")
            if not _matches_account(intent, account):
                raise TaxSettingError("DIVIDEND_TAX_SCOPE_CHANGED")
            previous = settings[-1] if settings else None
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
                "tax_status": "ESTIMATED" if intent.rate is not None else "UNKNOWN",
                "rate_ppm": to_scaled(intent.rate, 6) if intent.rate is not None else None,
                "request_id": str(request_id),
            }
            audit(
                con,
                str(owner),
                "dividend_tax_setting",
                str(intent.id),
                "DIVIDEND_TAX_SETTING_RECORDED",
                request_id=str(request_id),
                before=_payload(_storage(previous), previous.reason) if previous else None,
                after=_payload(values, intent.reason),
                reason=intent.reason,
            )
            entry = con.execute(
                text(
                    "SELECT id,created_at FROM audit_events "
                    "WHERE entity_type='dividend_tax_setting' AND entity_id=:id"
                ),
                {"id": str(intent.id)},
            ).one()
            values.update(audit_id=entry.id, created_at=entry.created_at)
            columns = ",".join(values)
            parameters = ",".join(":" + key for key in values)
            con.execute(
                text(f"INSERT INTO dividend_tax_settings({columns}) VALUES ({parameters})"), values
            )
            updated = history(
                con,
                owner,
                intent.event_id,
                intent.revision_id,
                intent.revision_no,
                intent.account_id,
            )
            if updated is None or len(updated) != intent.expected_setting_no + 1:
                raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE")
            ex_date = event.record.terms.ex_date
            if ex_date is None:
                raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE")
            invalidate_portfolio(
                con,
                str(intent.portfolio_id),
                str(intent.id),
                "DIVIDEND_TAX_SETTING_CHANGED",
                min(intent.effective_from, ex_date),
            )
            return updated[-1]
        except TaxSettingError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
            raise TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE") from None
