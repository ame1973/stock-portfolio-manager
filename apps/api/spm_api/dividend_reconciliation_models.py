"""Private recorded-reconciliation output, without current financial authority."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from spm_api.decimal_types import IsoDate, Money, to_scaled
from spm_api.dividend_event_service import Version
from spm_api.dividend_projection import ProjectionState
from spm_api.dividend_reconciliation_history import RecordedState
from spm_api.dividend_tax_models import Ordinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.finance import Quality
from spm_api.master_models import Currency


class RecordedReconciliation(Model):
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    account_id: UUID = Field(repr=False)
    projection_id: UUID | None = Field(repr=False)
    projection_no: PositiveOrdinal | None
    projection_as_of: IsoDate | None = Field(repr=False)
    projection_state: ProjectionState | None
    projection_quality: Quality | None
    comparison_quality: Quality | None
    currency: Currency | None
    recorded_expected: Money | None = Field(repr=False)
    recorded_received: Money | None = Field(repr=False)
    recorded_outstanding: Money | None = Field(repr=False)
    active_receipts: Ordinal
    void_receipts: Ordinal
    unverified_origins: Ordinal
    unavailable_cash_links: Ordinal
    state: RecordedState
    scope: Literal["UNVERIFIED_RECORDED_RECONCILIATION_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_record(self) -> Self:
        active, void, unverified, missing = (
            int(value)
            for value in (
                self.active_receipts,
                self.void_receipts,
                self.unverified_origins,
                self.unavailable_cash_links,
            )
        )
        projection = self.projection_id is not None
        if (
            projection != (self.projection_no is not None)
            or projection != (self.projection_as_of is not None)
            or projection != (self.projection_state is not None)
            or (self.projection_state == "EXPECTED") != (self.projection_quality is not None)
            or (self.state == "NO_RECORDED_PROJECTION") == projection
            or unverified > active + void
            or missing > active + void
        ):
            raise ValueError("The recorded reconciliation scope is inconsistent")
        no_comparison = self.state in {
            "NO_RECORDED_PROJECTION",
            "STALE_RECORDED_PROJECTION",
            "PROJECTION_NOT_COMPARABLE",
        }
        if no_comparison:
            if any(
                value is not None
                for value in (
                    self.currency,
                    self.recorded_expected,
                    self.recorded_received,
                    self.recorded_outstanding,
                    self.comparison_quality,
                )
            ) or (
                self.state == "PROJECTION_NOT_COMPARABLE" and self.projection_state == "EXPECTED"
            ):
                raise ValueError("The recorded reconciliation projection is inconsistent")
            return self
        if (
            self.projection_state != "EXPECTED"
            or self.currency is None
            or self.recorded_expected is None
        ):
            raise ValueError("The recorded reconciliation projection is unavailable")
        if self.state in {
            "CASH_LINK_UNAVAILABLE",
            "NO_RECORDED_ACTIVE_RECEIPTS",
            "CURRENCY_REVIEW",
        }:
            if (
                self.recorded_received is not None
                or self.recorded_outstanding is not None
                or (self.state == "CASH_LINK_UNAVAILABLE") != (missing > 0)
                or (self.state == "NO_RECORDED_ACTIVE_RECEIPTS" and active != 0)
                or (self.state == "CURRENCY_REVIEW" and active == 0)
                or self.comparison_quality
                != ("NEEDS_REVIEW" if self.state == "CURRENCY_REVIEW" else None)
            ):
                raise ValueError("The recorded reconciliation availability is inconsistent")
            return self
        if (
            missing
            or not active
            or self.recorded_received is None
            or self.recorded_outstanding is None
            or self.comparison_quality is None
        ):
            raise ValueError("The recorded reconciliation amounts are unavailable")
        expected, received, outstanding = (
            to_scaled(value, 6)
            for value in (
                self.recorded_expected,
                self.recorded_received,
                self.recorded_outstanding,
            )
        )
        if (
            expected - received != outstanding
            or (
                self.state == "RECORDED_PARTIAL"
                and (expected < 0 or received < 0 or outstanding <= 0)
            )
            or (
                self.state == "RECORDED_RECEIVED"
                and (expected < 0 or received < 0 or outstanding != 0)
            )
            or (
                self.state == "RECORDED_OVER_RECEIVED"
                and (expected < 0 or received < 0 or outstanding >= 0)
            )
            or self.comparison_quality
            != (
                "NEEDS_REVIEW"
                if self.state in {"RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"}
                else self.projection_quality
            )
        ):
            raise ValueError("The recorded reconciliation amounts are inconsistent")
        return self
