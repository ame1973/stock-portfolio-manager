"""Bind normalized rebuild intent without authenticating or publishing a revision."""

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from pydantic_core import PydanticSerializationError

import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api.audit import canonical, digest, stamp
from spm_api.decimal_types import IsoDate
from spm_api.errors import Model


class SettingDecision(Model):
    """Choose whether to record the supplied estimate or leave the new setting missing."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["RECORD_SUPPLIED", "LEAVE_MISSING"]
    effective_from: IsoDate | None = Field(repr=False)
    select_on: IsoDate = Field(repr=False)

    @model_validator(mode="after")
    def explicit_dates(self) -> Self:
        if self.mode == "RECORD_SUPPLIED":
            if self.effective_from is None or self.effective_from > self.select_on:
                raise ValueError("The effective date must be on or before selection")
        elif self.effective_from is not None:
            raise ValueError("A missing setting cannot have an effective date")
        return self


class AccountSettings(Model):
    """Independent tax and fee decisions for one explicitly supplied account."""

    model_config = ConfigDict(frozen=True)

    account_id: UUID = Field(repr=False)
    tax: SettingDecision = Field(repr=False)
    fee: SettingDecision = Field(repr=False)


class RebuildSettings(Model):
    """Explicit successor-setting intent; this model does not authorize any write."""

    model_config = ConfigDict(frozen=True)

    confirmation: Literal["EXPLICIT_SUCCESSOR_SETTINGS_ONLY"]
    accounts: tuple[AccountSettings, ...] = Field(repr=False)

    @model_validator(mode="after")
    def unique_accounts(self) -> Self:
        ids = [account.account_id for account in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("Each account requires one setting decision")
        return self


def fingerprint(
    owner: UUID,
    previous_revision_id: UUID,
    previous_revision_no: int,
    decision: events.EventIntent,
    proposal: preview.RevisionProposal,
    *,
    settings: RebuildSettings,
    request_id: UUID,
    at: datetime,
) -> str:
    """Return a domain-separated hash, not a source, scope or financial grant.

    The caller must separately validate the recorded observation, current sources,
    authorization and complete financial scope inside the intended writer transaction.
    """
    try:
        if (
            not all(isinstance(value, UUID) for value in (owner, previous_revision_id, request_id))
            or type(previous_revision_no) is not int
            or not 1 <= previous_revision_no < 2_147_483_647
            or not isinstance(decision, events.EventIntent)
            or not isinstance(proposal, preview.RevisionProposal)
            or not isinstance(settings, RebuildSettings)
            or not isinstance(at, datetime)
            or at.utcoffset() is None
        ):
            raise ValueError
        valid_decision = events.EventIntent.model_validate(decision.model_dump(warnings="error"))
        valid_proposal = preview.RevisionProposal.model_validate(
            proposal.model_dump(warnings="error")
        )
        valid_settings = RebuildSettings.model_validate(settings.model_dump(warnings="error"))
        if (
            not current._exact(valid_decision, decision)
            or not current._exact(valid_proposal, proposal)
            or not current._exact(valid_settings, settings)
            or {account.account_id for account in valid_proposal.accounts}
            != {account.account_id for account in valid_settings.accounts}
            or valid_decision.revision_id == previous_revision_id
            or valid_decision.revision_no != previous_revision_no + 1
        ):
            raise ValueError
        if valid_decision.kind == "MANUAL":
            terms = valid_decision.terms
            if terms is None or (
                terms.ex_date,
                terms.dps,
                terms.currency,
                terms.adjustment_basis,
            ) != (
                valid_proposal.ex_date,
                valid_proposal.dps,
                valid_proposal.currency,
                valid_proposal.adjustment_basis,
            ):
                raise ValueError
        normalized = valid_proposal.model_dump(mode="json", warnings="error")
        normalized["accounts"] = sorted(normalized["accounts"], key=lambda row: row["account_id"])
        normalized_settings = valid_settings.model_dump(mode="json", warnings="error")
        normalized_settings["accounts"] = sorted(
            normalized_settings["accounts"], key=lambda row: row["account_id"]
        )
        return digest(
            canonical(
                {
                    "scope": "DIVIDEND_REVISION_REBUILD_INTENT_ONLY",
                    "owner_id": str(owner),
                    "previous_revision_id": str(previous_revision_id),
                    "previous_revision_no": previous_revision_no,
                    "decision": valid_decision.model_dump(mode="json", warnings="error"),
                    "proposal": normalized,
                    "settings": normalized_settings,
                    "request_id": str(request_id),
                    "at": stamp(at.astimezone(UTC)),
                }
            )
        )
    except (ValueError, TypeError, AttributeError, OverflowError, PydanticSerializationError):
        raise rebuild.RebuildError("INVALID_DIVIDEND_REBUILD_INTENT") from None
