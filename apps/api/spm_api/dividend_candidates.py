"""Produce bounded, review-only candidate seeds without automatically merging observations."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field
from pydantic_core import PydanticSerializationError

from spm_api.dividend_evidence import DividendTerms
from spm_api.errors import Model
from spm_api.master_models import ProviderId
from spm_api.providers.base import DividendObservation, OriginGroup
from spm_api.providers.manual import DividendEvidence

HintReason = Literal[
    "SAME_EX_DATE",
    "POSSIBLE_DATE_MISMATCH",
    "POSSIBLE_SOURCE_REVISION",
    "DIFFERENT_EVENT_TYPES",
    "DIFFERENT_REPORTED_DPS",
    "DIFFERENT_CURRENCIES",
    "AMBIGUOUS_MULTIPLE",
]


class CandidateObservation(Model):
    """A caller-supplied source reference, not an owned or validated database association."""

    model_config = ConfigDict(frozen=True)

    observation_id: UUID
    observation: DividendObservation = Field(repr=False)


@dataclass(frozen=True, slots=True)
class CandidateSeed:
    observation_id: UUID
    source_sha256: str
    provider_id: ProviderId
    origin_group: OriginGroup
    terms: DividendTerms


@dataclass(frozen=True, slots=True)
class CandidateHint:
    left_observation_id: UUID
    right_observation_id: UUID
    reasons: frozenset[HintReason]


@dataclass(frozen=True, slots=True)
class CandidateSuggestions:
    seeds: tuple[CandidateSeed, ...]
    hints: tuple[CandidateHint, ...]
    scope: Literal["SUGGESTIONS_ONLY"] = "SUGGESTIONS_ONLY"


def _hint(left: CandidateObservation, right: CandidateObservation) -> CandidateHint | None:
    first, second = left.observation, right.observation
    if first.security_id != second.security_id:
        return None
    reasons: set[HintReason] = set()
    if (
        first.provider_id == second.provider_id
        and first.provider_event_id is not None
        and first.provider_event_id == second.provider_event_id
    ):
        reasons.add("POSSIBLE_SOURCE_REVISION")
    same_day = False
    if first.ex_date is not None and second.ex_date is not None:
        distance = abs(
            (date.fromisoformat(first.ex_date) - date.fromisoformat(second.ex_date)).days
        )
        if distance == 0:
            same_day = True
            reasons.add("SAME_EX_DATE")
        elif distance <= 3:
            reasons.add("POSSIBLE_DATE_MISMATCH")
    if not reasons:
        return None
    if (
        first.event_type != "UNKNOWN"
        and second.event_type != "UNKNOWN"
        and first.event_type != second.event_type
    ):
        reasons.add("DIFFERENT_EVENT_TYPES")
    if (
        first.currency is not None
        and second.currency is not None
        and first.currency != second.currency
    ):
        reasons.add("DIFFERENT_CURRENCIES")
    if first.dps is not None and second.dps is not None and first.dps != second.dps:
        reasons.add("DIFFERENT_REPORTED_DPS")
        if same_day and "UNKNOWN" in {first.event_type, second.event_type}:
            reasons.add("AMBIGUOUS_MULTIPLE")
    return CandidateHint(left.observation_id, right.observation_id, frozenset(reasons))


def suggest(observations: Sequence[CandidateObservation]) -> CandidateSuggestions:
    """Return one seed per source and pairwise hints, never evidence matches or event IDs."""
    try:
        if len(observations) > 100 or any(
            not isinstance(item, CandidateObservation) for item in observations
        ):
            raise ValueError
        checked = []
        for item in observations:
            ref = CandidateObservation.model_validate(item.model_dump(warnings="error"))
            model: type[DividendObservation] = (
                DividendEvidence
                if ref.observation.provider_id in {"manual", "hkex_evidence", "sec_evidence"}
                else DividendObservation
            )
            source = model.model_validate(ref.observation.model_dump(warnings="error"))
            checked.append(
                CandidateObservation(observation_id=ref.observation_id, observation=source)
            )
        if len({item.observation_id for item in checked}) != len(checked) or len(
            {
                (
                    item.observation.provider_id,
                    item.observation.security_id,
                    item.observation.normalized_sha256,
                )
                for item in checked
            }
        ) != len(checked):
            raise ValueError
        checked.sort(key=lambda item: str(item.observation_id))
        seeds = tuple(
            CandidateSeed(
                observation_id=item.observation_id,
                source_sha256=item.observation.normalized_sha256,
                provider_id=item.observation.provider_id,
                origin_group=item.observation.origin_group,
                terms=DividendTerms.model_validate(
                    {name: getattr(item.observation, name) for name in DividendTerms.model_fields}
                ),
            )
            for item in checked
        )
        # ponytail: at most 100 sources; index pair selection if the bound grows.
        hints = tuple(
            hint
            for left, right in combinations(checked, 2)
            if (hint := _hint(left, right)) is not None
        )
        return CandidateSuggestions(seeds, hints)
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise ValueError("INVALID_DIVIDEND_CANDIDATE_INPUT") from None
