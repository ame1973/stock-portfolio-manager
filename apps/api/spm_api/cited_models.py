"""Cited source inputs exclude trusted actor, citation identity and financial adoption claims."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from spm_api.action_models import EvidenceUrl, Ratio
from spm_api.decimal_types import Dps, IsoDate
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Currency, ProviderCode
from spm_api.providers.base import (
    AdjustmentBasis,
    CitationReference,
    EventType,
    EvidenceCitation,
    SourcePeriod,
)
from spm_api.providers.manual import DividendEvidence, EvidenceProvider, SplitEvidence


class CitationInput(Model):
    model_config = ConfigDict(frozen=True)

    citation: CitationReference = Field(repr=False)
    reason: Annotated[ChangeReason, Field(pattern=r"^[^\x00]*$", repr=False)]
    url: EvidenceUrl | None = Field(default=None, repr=False)

    def source(self, identifier: UUID) -> EvidenceCitation:
        return EvidenceCitation(id=identifier, **self.model_dump())


class _CitedInput(Model):
    model_config = ConfigDict(frozen=True)

    provider_id: EvidenceProvider
    security_id: UUID
    provider_symbol: ProviderCode
    citation: CitationInput = Field(repr=False)


class CitedDividendCreate(_CitedInput):
    operation: Literal["DIVIDENDS"]
    ex_date: IsoDate
    announce_date: IsoDate | None = None
    record_date: IsoDate | None = None
    pay_date: IsoDate | None = None
    dps: Dps | None = None
    currency: Currency | None = None
    event_type: EventType = "UNKNOWN"
    adjustment_basis: AdjustmentBasis = "UNKNOWN"
    source_currency: Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")] | None = None
    source_period: SourcePeriod | None = None
    source_frequency: int | None = Field(default=None, strict=True, ge=0, le=365)

    def source(self, identifier: UUID) -> DividendEvidence:
        return DividendEvidence(
            **self.model_dump(exclude={"operation", "citation"}),
            evidence=self.citation.source(identifier),
        )


class CitedSplitCreate(_CitedInput):
    operation: Literal["SPLITS"]
    effective_date: IsoDate
    ratio_numerator: Ratio
    ratio_denominator: Ratio
    source_adjustment_type: Literal["FORWARD_SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND"] | None = (
        None
    )

    def source(self, identifier: UUID) -> SplitEvidence:
        return SplitEvidence(
            **self.model_dump(exclude={"operation", "citation"}),
            evidence=self.citation.source(identifier),
        )


CitedSourceInput = Annotated[
    CitedDividendCreate | CitedSplitCreate, Field(discriminator="operation")
]


class CitedSourceCreated(Model):
    provider_id: EvidenceProvider
    security_id: UUID
    operation: Literal["DIVIDENDS", "SPLITS"]
    observation_id: UUID
    provider_run_id: UUID
    citation_id: UUID
    scope: Literal["SOURCE_ONLY"] = "SOURCE_ONLY"
    canonical_adoption: Literal["NOT_ADOPTED"] = "NOT_ADOPTED"
    canonical_adoption_scope: Literal["RESPONSE_ONLY_NOT_LINKAGE"] = Field(
        default="RESPONSE_ONLY_NOT_LINKAGE",
        description="The marker describes this creation response, not current or historical links.",
    )
    document_verification: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
