"""Strict owner-scoped wire models for retained dividend evidence reviews."""

from datetime import timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints, model_validator
from pydantic_core import PydanticSerializationError

from spm_api.dividend_event_service import Version
from spm_api.dividend_evidence import TermField
from spm_api.errors import Model

ReasonCode = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")
]
Cursor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1500)]


class EvidenceReviewRecord(Model):
    """A retained review, not a verified acknowledgment or financial authorization."""

    model_config = ConfigDict(revalidate_instances="always")

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    portfolio_id: UUID | None = Field(repr=False)
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"]
    severity: Literal["WARNING"]
    reason_code: Literal["DIVIDEND_SOURCE_CONFLICT", "DIVIDEND_INCOMPLETE"]
    conflicting_fields: Annotated[list[TermField], Field(max_length=8)]
    incomplete_reasons: Annotated[list[ReasonCode], Field(max_length=32)]
    created_at: AwareDatetime = Field(repr=False)
    resolved_at: AwareDatetime | None = Field(repr=False)
    resolved_by_revision_id: UUID | None = Field(repr=False)
    acknowledgement_scope: Literal["NOT_ESTABLISHED"]
    scope: Literal["RETAINED_EVIDENCE_REVIEW_ONLY"]

    @model_validator(mode="after")
    def consistent_review(self) -> Self:
        if (
            self.created_at.utcoffset() != timedelta(0)
            or (self.resolved_at is not None and self.resolved_at.utcoffset() != timedelta(0))
            or self.conflicting_fields != sorted(set(self.conflicting_fields))
            or self.incomplete_reasons != sorted(set(self.incomplete_reasons))
            or (self.reason_code == "DIVIDEND_SOURCE_CONFLICT") != bool(self.conflicting_fields)
            or (self.reason_code == "DIVIDEND_INCOMPLETE" and not self.incomplete_reasons)
        ):
            raise ValueError("Inconsistent retained review detail")
        if self.status == "RESOLVED":
            if (
                self.resolved_at is None
                or self.resolved_by_revision_id is None
                or self.resolved_by_revision_id == self.revision_id
                or self.resolved_at < self.created_at
            ):
                raise ValueError("Inconsistent retained review resolution")
        elif self.resolved_at is not None or self.resolved_by_revision_id is not None:
            raise ValueError("Active reviews cannot have a resolution")
        return self


class EvidenceReviewPage(Model):
    """A bounded retained history page; absence is not completeness or a grant."""

    model_config = ConfigDict(revalidate_instances="always")

    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    items: Annotated[list[EvidenceReviewRecord], Field(max_length=100)] = Field(repr=False)
    next_cursor: Cursor | None = Field(repr=False)
    scope: Literal["RETAINED_EVIDENCE_REVIEWS_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    document_verification: Literal["NOT_ESTABLISHED"]

    @model_validator(mode="after")
    def consistent_page(self) -> Self:
        try:
            self.items = [
                EvidenceReviewRecord.model_validate(item.model_dump(warnings="error"))
                for item in self.items
            ]
        except PydanticSerializationError:
            raise ValueError("Invalid retained review") from None
        ids = [item.id for item in self.items]
        order = [(item.created_at, item.id.int) for item in self.items]
        if (
            len(ids) != len(set(ids))
            or order != sorted(order, reverse=True)
            or (self.next_cursor is not None and not self.items)
        ):
            raise ValueError("Inconsistent retained review page")
        for item in self.items:
            if item.event_id != self.event_id:
                raise ValueError("Inconsistent retained review event")
            if item.status == "RESOLVED":
                if (
                    item.revision_no >= self.revision_no
                    or item.revision_id == self.revision_id
                    or (item.revision_no + 1 == self.revision_no)
                    != (item.resolved_by_revision_id == self.revision_id)
                ):
                    raise ValueError("Inconsistent retained review revision")
            elif (item.revision_id, item.revision_no) != (self.revision_id, self.revision_no):
                raise ValueError("Inconsistent active review revision")
        return self
