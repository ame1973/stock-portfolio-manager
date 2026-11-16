"""Audit and select explicit fee assertions without granting economic authority."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import canonical, digest, stamp
from spm_api.decimal_types import INT64_MAX, IsoDate, NonNegativeMoney, from_scaled, to_scaled
from spm_api.dividend_account_scope import account, positive_version
from spm_api.dividend_event_service import Reason, Version
from spm_api.errors import Model
from spm_api.master_models import Currency

Number = Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
FeeStatus = Literal["ESTIMATED", "UNKNOWN"]


class FeeSettingError(ValueError):
    """Fixed codes only; do not expose financial values or private rationale."""


class FeeSetting(Model):
    """A supplied owner assertion, not authentication or proof of a broker charge."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    owner_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    portfolio_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: Number
    broker_version: Number
    setting_no: Number
    previous_id: UUID | None = Field(repr=False)
    effective_from: IsoDate
    fee_status: FeeStatus
    fee: NonNegativeMoney | None = Field(repr=False)
    fee_currency: Currency | None
    request_id: UUID = Field(repr=False)
    audit_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True, repr=False)]
    reason: Reason = Field(repr=False)
    scope: Literal["OWNER_ESTIMATE_ONLY"] = "OWNER_ESTIMATE_ONLY"

    @model_validator(mode="after")
    def complete_decision(self) -> Self:
        if (self.setting_no == 1) != (self.previous_id is None):
            raise ValueError("A setting must identify its predecessor")
        if self.fee_status == "ESTIMATED":
            if self.fee is None or self.fee_currency is None:
                raise ValueError("An estimated fee requires an explicit amount and currency")
        elif self.fee is not None:
            raise ValueError("An unknown fee cannot supply an amount")
        return self


@dataclass(frozen=True, slots=True)
class FeeSelection:
    """Selection over supplied assertions, not a verified or current-use grant."""

    effective_on: date
    head_no: int
    head_id: UUID | None = field(repr=False)
    selected: FeeSetting | None = field(repr=False)
    status: Literal["MISSING", "UNKNOWN", "ESTIMATED"]
    scope: Literal["UNVERIFIED_FEE_SELECTION_ONLY"] = "UNVERIFIED_FEE_SELECTION_ONLY"


def select(settings: Sequence[FeeSetting], effective_on: date) -> FeeSelection:
    """Select a complete supplied chain or explicit prefix for one effective calendar day."""
    if (
        not isinstance(settings, Sequence)
        or isinstance(settings, (str, bytes))
        or type(effective_on) is not date
    ):
        raise FeeSettingError("INVALID_DIVIDEND_FEE_SCOPE")
    try:
        records: list[FeeSetting] = []
        identities: set[UUID] = set()
        requests: set[UUID] = set()
        audits_seen: set[UUID] = set()
        for number, item in enumerate(settings, 1):
            if not isinstance(item, FeeSetting):
                raise ValueError
            record = FeeSetting.model_validate(item.model_dump(warnings="error"))
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
        return FeeSelection(
            effective_on=effective_on,
            head_no=last.setting_no if last else 0,
            head_id=last.id if last else None,
            selected=chosen,
            status=chosen.fee_status if chosen else "MISSING",
        )
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None


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
    "fee_status",
    "fee_micro",
    "fee_currency",
    "request_id",
    "created_at",
)


def _storage(record: FeeSetting) -> dict[str, Any]:
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
        "fee_status": record.fee_status,
        "fee_micro": to_scaled(record.fee, 6) if record.fee is not None else None,
        "fee_currency": record.fee_currency,
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


def history(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    revision_id: UUID,
    revision_no: int,
    account_id: UUID,
) -> tuple[FeeSetting, ...] | None:
    """Read complete audited history in one snapshot, not authentication or current authority."""
    if (
        not con.in_transaction()
        or not all(isinstance(value, UUID) for value in (owner, event_id, revision_id, account_id))
        or type(revision_no) is not int
        or not 1 <= revision_no <= 2_147_483_647
    ):
        raise FeeSettingError("INVALID_DIVIDEND_FEE_SCOPE")
    try:
        head = events.get(con, owner, event_id)
        owned_account = account(con, owner, account_id)
        if head is None or owned_account is None:
            return None
        if rejections.history(con, owner, event_id) is None:
            raise ValueError("Missing rejection history")
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
        source_time = events._time(source_audits[0].created_at)
        rows = con.execute(
            text("""
            SELECT f.*,p.owner_id AS retained_portfolio_owner,
                   b.owner_id AS retained_broker_owner,b.version AS retained_broker_version
            FROM dividend_fee_settings f
            LEFT JOIN portfolios p ON p.id=f.portfolio_id
            LEFT JOIN brokers b ON b.id=f.broker_id
            WHERE f.account_id=:account AND (f.revision_id=:revision OR
                  (f.event_id=:event AND f.revision_no=:number))
            ORDER BY f.setting_no
        """),
            {
                "account": str(account_id),
                "revision": str(revision_id),
                "event": str(event_id),
                "number": revision_no,
            },
        ).mappings()
        result: list[FeeSetting] = []
        # ponytail: verify each retained audit; batch verified history if chains become large.
        for row in rows:
            identifier = events._uuid(row["id"])
            audits = (
                con.execute(
                    text(
                        "SELECT * FROM audit_events WHERE entity_type='dividend_fee_setting' "
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
            amount = row["fee_micro"]
            record = FeeSetting.model_validate(
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
                    "fee_status": row["fee_status"],
                    "fee": from_scaled(amount, 6) if amount is not None else None,
                    "fee_currency": row["fee_currency"],
                    "request_id": events._uuid(row["request_id"]),
                    "audit_id": events._uuid(row["audit_id"]),
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
                or row["retained_portfolio_owner"] != str(owner)
                or row["retained_broker_owner"] != str(owner)
                or record.account_version > positive_version(owned_account["account_version"])
                or record.broker_version > positive_version(row["retained_broker_version"])
                or record.created_at < event.record.created_at
                or record.created_at < source_time
                or entry["id"] != str(record.audit_id)
                or entry["owner_id"] != str(owner)
                or entry["action"] != "DIVIDEND_FEE_SETTING_RECORDED"
                or entry["request_id"] != str(record.request_id)
                or entry["reason"] != record.reason
                or events._time(entry["created_at"]) < record.created_at
                or entry["before_sha256"]
                != (
                    digest(canonical(_payload(_storage(previous), previous.reason)))
                    if previous
                    else None
                )
                or entry["after_sha256"] != digest(canonical(_payload(values, record.reason)))
            ):
                raise ValueError("Inconsistent fee setting history")
            result.append(record)
        select(result, date.min)
        return tuple(result)
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise FeeSettingError("DIVIDEND_FEE_HISTORY_UNAVAILABLE") from None
