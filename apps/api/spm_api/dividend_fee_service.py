"""Select explicit fee assertions without authentication or economic authority."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator
from pydantic_core import PydanticSerializationError

from spm_api.decimal_types import INT64_MAX, IsoDate, NonNegativeMoney
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
