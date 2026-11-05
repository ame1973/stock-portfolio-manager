"""Explicit corporate-action terms; unsupported allocation is review, never guessed math."""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Note, Ticker

ActionType = Literal[
    "SPLIT", "REVERSE_SPLIT", "SYMBOL_CHANGE", "CASH_IN_LIEU", "MERGER", "SPIN_OFF"
]


def _ratio_input(value: object) -> object:
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]{1,19}", value) is None:
            raise ValueError(
                "Use an exact positive INT64 integer string without exponent or decimal"
            )
        return int(value)
    return value


Ratio = Annotated[
    int,
    Field(strict=True, ge=1, le=INT64_MAX),
    BeforeValidator(_ratio_input, json_schema_input_type=int | str),
]
# Input models retain integer fingerprints; only responses need browser-safe JSON strings.
JsonRatio = Annotated[Ratio, PlainSerializer(str, return_type=str, when_used="json")]


def _evidence(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError("Invalid evidence URL")
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Use an HTTP(S) evidence URL without credentials or whitespace") from exc
    return value


EvidenceUrl = Annotated[
    str, Field(strict=True, min_length=1, max_length=2048), AfterValidator(_evidence)
]


class CorporateActionCreate(Model):
    security_id: UUID
    action_type: ActionType
    effective_date: IsoDate
    ratio_numerator: Ratio | None = None
    ratio_denominator: Ratio | None = None
    new_symbol: Ticker | None = None
    evidence_url: EvidenceUrl | None = None
    status: Literal["CONFIRMED", "NEEDS_REVIEW"] = "NEEDS_REVIEW"
    reason: ChangeReason
    note: Note = ""

    @field_validator("new_symbol")
    @classmethod
    def uppercase(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def explicit_terms(self) -> "CorporateActionCreate":
        numerator, denominator = self.ratio_numerator, self.ratio_denominator
        if (numerator is None) != (denominator is None):
            raise ValueError("Provide both rational terms or neither")
        if self.action_type in {"SPLIT", "REVERSE_SPLIT"}:
            if numerator is None or denominator is None:
                raise ValueError("A split requires an explicit integer rational ratio")
            if (self.action_type == "SPLIT" and numerator <= denominator) or (
                self.action_type == "REVERSE_SPLIT" and numerator >= denominator
            ):
                raise ValueError("The rational ratio must match split/reverse-split direction")
        if self.action_type == "SYMBOL_CHANGE":
            if self.new_symbol is None or numerator is not None:
                raise ValueError("A symbol change needs an explicit ticker and no share ratio")
        elif self.new_symbol is not None:
            raise ValueError("Only a symbol change supplies a new ticker")
        if (
            self.action_type in {"CASH_IN_LIEU", "MERGER", "SPIN_OFF"}
            and self.status == "CONFIRMED"
        ):
            raise ValueError("Unallocated corporate actions must remain NEEDS_REVIEW")
        return self


class CorporateAction(Model):
    id: UUID
    root_id: UUID
    supersedes_id: UUID | None
    security_id: UUID
    action_type: ActionType
    effective_date: IsoDate
    ratio_numerator: JsonRatio | None
    ratio_denominator: JsonRatio | None
    new_symbol: str | None
    previous_symbol: str | None
    evidence_url: str | None
    status: Literal["CONFIRMED", "NEEDS_REVIEW", "VOID"]
    lifecycle_status: Literal["CURRENT", "SUPERSEDED", "VOID"]
    is_current: bool
    revision: int
    version: int
    created_sequence: int
    effect_scope: Literal["QUANTITY_TERMS", "DISPLAY_ONLY", "REVIEW_ONLY", "NONE"]
    allocation_scope: Literal["TERMS_NOT_A_VALUATION"] = "TERMS_NOT_A_VALUATION"
    review_required: bool
    reason: str
    note: str
    created_at: str
