"""Owner rejection views; retained decisions do not grant economic use."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from spm_api.dividend_event_models import EventReference
from spm_api.dividend_event_service import Reason, Version
from spm_api.errors import Model


class RejectionDecision(Model):
    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    decision: Literal["REJECT_REVISION"]
    reference: EventReference = Field(repr=False)
    reason: Reason = Field(repr=False)
    created_at: AwareDatetime
    scope: Literal["OWNER_REVISION_REJECTION_ONLY"]
    document_verification: Literal["NOT_ESTABLISHED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]


class RejectionResolution(Model):
    id: UUID = Field(repr=False)
    successor_revision_id: UUID = Field(repr=False)
    successor_revision_no: Version


class RejectionHistoryItem(Model):
    decision: RejectionDecision
    resolution: RejectionResolution | None


class RejectionState(Model):
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    state: Literal["NOT_REJECTED", "REJECTED"]
    rejection: RejectionDecision | None
    scope: Literal["OWNER_REJECTION_STATE_ONLY"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    document_verification: Literal["NOT_ESTABLISHED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]

    @model_validator(mode="after")
    def matches_current_revision(self) -> Self:
        if self.rejection is None:
            if self.state != "NOT_REJECTED":
                raise ValueError("A rejected state requires a current rejection")
        elif (
            self.state != "REJECTED"
            or self.rejection.event_id != self.event_id
            or self.rejection.revision_id != self.revision_id
            or self.rejection.revision_no != self.revision_no
        ):
            raise ValueError("The rejection must match the current event revision")
        return self
