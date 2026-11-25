"""Strict private contracts for hypothetical dividend-revision comparisons."""

from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from pydantic_core import PydanticSerializationError

from spm_api.decimal_types import Dps, IsoDate, Money, Quantity, to_scaled
from spm_api.dividend_eligibility import EligibilityRule, EligibilityState
from spm_api.dividend_eligibility_service import TradeCreationAuditScope, TradeHistoryAuditScope
from spm_api.dividend_event_service import Version
from spm_api.dividend_math import accrue
from spm_api.dividend_projection_models import ProjectionAmounts
from spm_api.dividend_revision_preview import AccountEstimate, PreviewState, RevisionProposal
from spm_api.dividend_tax_models import Ordinal, PositiveOrdinal
from spm_api.errors import Model
from spm_api.finance import Quality
from spm_api.master_models import Currency
from spm_api.providers.base import AdjustmentBasis

SnapshotToken = Annotated[
    str, StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]
ReviewReason = Literal[
    "ELIGIBILITY_RULE_REQUIRES_REVIEW", "UNALLOCATED_ACTION", "SPLIT_FRACTIONAL_RESIDUE"
]


class PreviewRequest(Model):
    """A cursor compares snapshots; it does not authorize a revision or a financial write."""

    model_config = ConfigDict(revalidate_instances="always")

    revision_id: UUID = Field(repr=False)
    proposal: RevisionProposal = Field(repr=False)
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 25
    after_account_id: UUID | None = Field(repr=False)
    snapshot_token: SnapshotToken | None = Field(repr=False)

    @model_validator(mode="after")
    def paired_cursor(self) -> Self:
        if (self.after_account_id is None) != (self.snapshot_token is None):
            raise ValueError("The snapshot cursor is incomplete")
        try:
            self.proposal = RevisionProposal.model_validate(
                self.proposal.model_dump(warnings="error")
            )
        except PydanticSerializationError:
            raise ValueError("The hypothetical proposal is invalid") from None
        return self


class PreviewTerms(Model):
    model_config = ConfigDict(revalidate_instances="always")

    ex_date: IsoDate = Field(repr=False)
    as_of: IsoDate = Field(repr=False)
    eligibility_rule: EligibilityRule
    dps: Dps = Field(repr=False)
    currency: Currency
    adjustment_basis: AdjustmentBasis


class PreviewAccount(Model):
    model_config = ConfigDict(revalidate_instances="always")

    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_status: Literal["ACTIVE", "ARCHIVED"]
    broker_status: Literal["ACTIVE", "ARCHIVED"]
    account_version: PositiveOrdinal
    broker_version: PositiveOrdinal
    portfolio_version: PositiveOrdinal
    portfolio_data_revision: Ordinal
    retired_trade_scope: TradeHistoryAuditScope
    creation_audit_scope: TradeCreationAuditScope
    eligibility_state: EligibilityState
    recorded_quantity: Quantity | None = Field(repr=False)
    eligibility_reasons: list[ReviewReason]
    pending_action_ids: list[UUID] = Field(repr=False)
    estimate: AccountEstimate = Field(repr=False)
    previous_projection_id: UUID | None = Field(repr=False)
    previous_revision_id: UUID | None = Field(repr=False)
    previous_as_of: IsoDate | None = Field(repr=False)
    previous_currency: Currency | None
    previous_expected: Money | None = Field(repr=False)
    proposed_amounts: ProjectionAmounts | None = Field(repr=False)
    expected_delta: Money | None = Field(repr=False)
    recorded_received: Money | None = Field(repr=False)
    recorded_outstanding: Money | None = Field(repr=False)
    active_receipts: Ordinal
    void_receipts: Ordinal
    unverified_origins: Ordinal
    unavailable_cash_links: Ordinal
    comparison_quality: Quality | None
    state: PreviewState

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        try:
            self.estimate = AccountEstimate.model_validate(
                self.estimate.model_dump(warnings="error")
            )
        except PydanticSerializationError:
            raise ValueError("The hypothetical estimate is invalid") from None
        projection = self.previous_projection_id is not None
        active, void, unverified, missing = (
            int(value)
            for value in (
                self.active_receipts,
                self.void_receipts,
                self.unverified_origins,
                self.unavailable_cash_links,
            )
        )
        if (
            self.estimate.account_id != self.account_id
            or projection != (self.previous_revision_id is not None)
            or projection != (self.previous_as_of is not None)
            or (self.previous_currency is None) != (self.previous_expected is None)
            or (not projection and self.previous_expected is not None)
            or unverified > active + void
            or missing > active + void
            or len(set(self.pending_action_ids)) != len(self.pending_action_ids)
        ):
            raise ValueError("The hypothetical account scope is inconsistent")
        if self.eligibility_state == "NEEDS_REVIEW":
            if (
                self.recorded_quantity is not None
                or len(self.eligibility_reasons) != 1
                or (self.eligibility_reasons == ["UNALLOCATED_ACTION"])
                != bool(self.pending_action_ids)
            ):
                raise ValueError("The recorded eligibility review is inconsistent")
        elif self.recorded_quantity is None or self.eligibility_reasons or self.pending_action_ids:
            raise ValueError("The recorded eligibility observation is inconsistent")
        return self


class PreviewPage(Model):
    """A bounded display of one snapshot, never a source decision or a payment grant."""

    model_config = ConfigDict(revalidate_instances="always")

    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: Version
    security_id: UUID = Field(repr=False)
    terms: PreviewTerms = Field(repr=False)
    items: Annotated[list[PreviewAccount], Field(max_length=100)] = Field(repr=False)
    snapshot_token: SnapshotToken = Field(repr=False)
    next_after_account_id: UUID | None = Field(repr=False)
    scope: Literal["HYPOTHETICAL_RECORDED_COMPARISON_ONLY"]
    history_completeness: Literal["UNVERIFIED"]
    current_use_scope: Literal["REVALIDATION_REQUIRED"]
    eligibility_scope: Literal["NOT_ESTABLISHED"]
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"]

    @model_validator(mode="after")
    def consistent_page(self) -> Self:
        ids = [str(item.account_id) for item in self.items]
        if (
            ids != sorted(ids)
            or len(ids) != len(set(ids))
            or (
                self.next_after_account_id is not None
                and (not ids or str(self.next_after_account_id) != ids[-1])
            )
        ):
            raise ValueError("The hypothetical preview page is inconsistent")
        for item in self.items:
            self._account(item)
        return self

    def _account(self, item: PreviewAccount) -> None:
        terms, estimate = self.terms, item.estimate
        RevisionProposal.model_validate({**terms.model_dump(), "accounts": [estimate.model_dump()]})
        if item.eligibility_state != "NEEDS_REVIEW":
            expected = (
                "RECORDED_PRE_EX_DATE"
                if terms.ex_date <= terms.as_of
                else "UPCOMING_RECORDED_HOLDINGS"
            )
            if (
                terms.eligibility_rule != "ORDINARY_PRE_EX_DATE"
                or item.eligibility_state != expected
            ):
                raise ValueError("The recorded eligibility timing is inconsistent")

        active, missing = int(item.active_receipts), int(item.unavailable_cash_links)
        unavailable: PreviewState | None = None
        if terms.adjustment_basis != "AS_DECLARED":
            unavailable = "EVENT_BASIS_REVIEW"
        elif (
            item.retired_trade_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
            or item.creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
        ):
            unavailable = "LEDGER_AUDIT_REVIEW"
        elif item.eligibility_state == "NEEDS_REVIEW" or item.recorded_quantity is None:
            unavailable = "ELIGIBILITY_REVIEW"
        elif Decimal(item.recorded_quantity) == 0:
            unavailable = (
                "NO_RECORDED_POSITION_CASH_REVIEW" if active or missing else "NO_RECORDED_POSITION"
            )
        elif estimate.tax_rate is None or estimate.fee is None or estimate.fee_currency is None:
            unavailable = "ESTIMATES_UNAVAILABLE"
        elif (
            estimate.fee_currency != terms.currency
            and Decimal(estimate.fee) != 0
            and estimate.fee_rate_event is None
        ):
            unavailable = "FEE_FX_UNAVAILABLE"
        if unavailable is not None:
            if item.state != unavailable or any(
                value is not None
                for value in (
                    item.proposed_amounts,
                    item.expected_delta,
                    item.recorded_received,
                    item.recorded_outstanding,
                    item.comparison_quality,
                )
            ):
                raise ValueError("The unavailable hypothetical comparison is inconsistent")
            return

        if (
            item.recorded_quantity is None
            or estimate.tax_rate is None
            or estimate.fee is None
            or estimate.fee_currency is None
            or item.proposed_amounts is None
        ):
            raise ValueError("The hypothetical inputs are unavailable")
        amount = accrue(
            str(self.event_id),
            str(item.account_id),
            terms.currency,
            Decimal(item.recorded_quantity),
            Decimal(terms.dps),
            Decimal(estimate.tax_rate),
            Decimal(estimate.fee),
            Decimal(estimate.fx_rate_hkd) if estimate.fx_rate_hkd is not None else None,
            fee_currency=estimate.fee_currency,
            fee_rate_event=Decimal(estimate.fee_rate_event)
            if estimate.fee_rate_event is not None
            else None,
        )
        calculated = ProjectionAmounts.model_validate(
            {
                "currency": amount.currency,
                "gross_local": format(amount.gross_local, "f"),
                "tax_local": format(amount.tax_local, "f"),
                "fee_local": format(amount.fee_local, "f"),
                "net_local": format(amount.net_local, "f"),
                "rounding_delta_local": format(amount.rounding_delta_local, "f"),
                "estimated_hkd": format(amount.booked_hkd, "f")
                if amount.booked_hkd is not None
                else None,
                "quality": amount.quality,
            }
        )
        if item.proposed_amounts != calculated:
            raise ValueError("The hypothetical accrual is inconsistent")
        comparable = (
            item.previous_expected is not None
            and item.previous_revision_id == self.revision_id
            and item.previous_as_of == terms.as_of
            and item.previous_currency == terms.currency
        )
        if comparable:
            assert item.previous_expected is not None
            if item.expected_delta is None or to_scaled(item.expected_delta, 6) != (
                to_scaled(calculated.net_local, 6) - to_scaled(item.previous_expected, 6)
            ):
                raise ValueError("The hypothetical revision delta is inconsistent")
        elif item.expected_delta is not None:
            raise ValueError("The historical projection is not comparable")

        if missing or not active or item.state == "CURRENCY_REVIEW":
            expected_state = (
                "CASH_LINK_UNAVAILABLE"
                if missing
                else "NO_RECORDED_ACTIVE_RECEIPTS"
                if not active
                else "CURRENCY_REVIEW"
            )
            if (
                item.state != expected_state
                or item.recorded_received is not None
                or item.recorded_outstanding is not None
                or item.comparison_quality
                != ("NEEDS_REVIEW" if expected_state == "CURRENCY_REVIEW" else None)
            ):
                raise ValueError("The recorded cash availability is inconsistent")
            return
        if (
            item.recorded_received is None
            or item.recorded_outstanding is None
            or item.state
            not in {
                "RECORDED_PARTIAL",
                "RECORDED_RECEIVED",
                "RECORDED_OVER_RECEIVED",
                "RECORDED_NEEDS_REVIEW",
            }
        ):
            raise ValueError("The recorded comparison is unavailable")
        expected_money, received, outstanding = (
            to_scaled(value, 6)
            for value in (
                calculated.net_local,
                item.recorded_received,
                item.recorded_outstanding,
            )
        )
        if (
            expected_money - received != outstanding
            or (
                item.state == "RECORDED_PARTIAL"
                and (expected_money < 0 or received < 0 or outstanding <= 0)
            )
            or (
                item.state == "RECORDED_RECEIVED"
                and (expected_money < 0 or received < 0 or outstanding != 0)
            )
            or (
                item.state == "RECORDED_OVER_RECEIVED"
                and (expected_money < 0 or received < 0 or outstanding >= 0)
            )
            or item.comparison_quality
            != (
                "NEEDS_REVIEW"
                if item.state in {"RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"}
                else calculated.quality
            )
        ):
            raise ValueError("The recorded comparison is inconsistent")
