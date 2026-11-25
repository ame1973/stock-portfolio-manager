"""Preview proposed dividend terms against retained cash without publishing a revision."""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_event_financial_scope as financial
import spm_api.dividend_event_service as events
import spm_api.dividend_revision_scope as revision_scope
from spm_api.decimal_types import Dps, FxRate, IsoDate, NonNegativeMoney, Rate
from spm_api.dividend_eligibility import AccountEligibility, EligibilityRule, EligibilityState
from spm_api.dividend_math import Accrual, Receipt, accrue, reconcile
from spm_api.dividend_receipt_terms import Source
from spm_api.errors import Model
from spm_api.finance import Quality, round_money
from spm_api.master_models import Currency
from spm_api.providers.base import AdjustmentBasis


class RevisionPreviewError(ValueError):
    """Fixed errors only; do not disclose proposed amounts or retained history."""


class AccountEstimate(Model):
    """Explicit hypothetical values, not persisted or confirmed tax, fees, or FX."""

    model_config = ConfigDict(frozen=True)

    account_id: UUID = Field(repr=False)
    tax_rate: Rate | None = Field(repr=False)
    fee: NonNegativeMoney | None = Field(repr=False)
    fee_currency: Currency | None
    fee_rate_event: FxRate | None = Field(repr=False)
    fee_rate_source: Source | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    fx_source: Source | None = Field(repr=False)

    @field_validator("fee_rate_source", "fx_source")
    @classmethod
    def canonical_source(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("A rate source must not have surrounding whitespace")
        return value

    @model_validator(mode="after")
    def explicit_values(self) -> Self:
        if self.fee is not None and self.fee_currency is None:
            raise ValueError("An estimated fee requires an explicit currency")
        if (self.fee_rate_event is None) != (self.fee_rate_source is None):
            raise ValueError("A fee rate and source must be supplied together")
        if (self.fx_rate_hkd is None) != (self.fx_source is None):
            raise ValueError("An HKD rate and source must be supplied together")
        if self.fee_currency is None and self.fee_rate_event is not None:
            raise ValueError("An unknown fee currency cannot specify a conversion")
        return self


class RevisionProposal(Model):
    """Explicit proposed terms and estimates, not a source decision or grant."""

    model_config = ConfigDict(frozen=True)

    ex_date: IsoDate = Field(repr=False)
    as_of: IsoDate = Field(repr=False)
    eligibility_rule: EligibilityRule
    dps: Dps = Field(repr=False)
    currency: Currency
    adjustment_basis: AdjustmentBasis
    accounts: tuple[AccountEstimate, ...] = Field(repr=False)

    @model_validator(mode="after")
    def explicit_scope(self) -> Self:
        ids = [item.account_id for item in self.accounts]
        if len(set(ids)) != len(ids):
            raise ValueError("Each account needs one explicit estimate")
        for item in self.accounts:
            if self.currency == "HKD":
                if item.fx_rate_hkd is not None and (
                    Decimal(item.fx_rate_hkd) != 1 or item.fx_source != "IDENTITY"
                ):
                    raise ValueError("HKD requires identity valuation")
            elif item.fx_source == "IDENTITY":
                raise ValueError("Foreign HKD valuation requires an explicit source")
            if item.fee_currency == self.currency:
                if item.fee_rate_event is not None and (
                    Decimal(item.fee_rate_event) != 1 or item.fee_rate_source != "IDENTITY"
                ):
                    raise ValueError("Same-currency fees require identity conversion")
            elif item.fee_rate_source == "IDENTITY":
                raise ValueError("Foreign fees require an explicit conversion source")
        return self


PreviewState = Literal[
    "EVENT_BASIS_REVIEW",
    "LEDGER_AUDIT_REVIEW",
    "ELIGIBILITY_REVIEW",
    "NO_RECORDED_POSITION",
    "NO_RECORDED_POSITION_CASH_REVIEW",
    "ESTIMATES_UNAVAILABLE",
    "FEE_FX_UNAVAILABLE",
    "CASH_LINK_UNAVAILABLE",
    "NO_RECORDED_ACTIVE_RECEIPTS",
    "CURRENCY_REVIEW",
    "RECORDED_PARTIAL",
    "RECORDED_RECEIVED",
    "RECORDED_OVER_RECEIVED",
    "RECORDED_NEEDS_REVIEW",
]


@dataclass(frozen=True, slots=True)
class AccountPreview:
    account_id: UUID = field(repr=False)
    previous_projection_id: UUID | None = field(repr=False)
    previous_revision_id: UUID | None = field(repr=False)
    previous_as_of: date | None = field(repr=False)
    previous_currency: Currency | None
    previous_expected: Decimal | None = field(repr=False)
    proposed_accrual: Accrual | None = field(repr=False)
    expected_delta: Decimal | None = field(repr=False)
    recorded_received: Decimal | None = field(repr=False)
    recorded_outstanding: Decimal | None = field(repr=False)
    active_receipts: int
    void_receipts: int
    unverified_origins: int
    unavailable_cash_links: int
    eligibility_state: EligibilityState
    comparison_quality: Quality | None
    state: PreviewState


@dataclass(frozen=True, slots=True)
class RevisionPreview:
    snapshot: revision_scope.RevisionScope = field(repr=False)
    proposal: RevisionProposal = field(repr=False)
    accounts: tuple[AccountPreview, ...] = field(repr=False)
    scope: Literal["HYPOTHETICAL_RECORDED_COMPARISON_ONLY"] = (
        "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"
    )
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"] = "OWNER_RECORDED_NOT_VERIFIED"


def _account(
    snapshot: revision_scope.RevisionScope,
    portfolio: revision_scope.PortfolioLedger,
    observation: AccountEligibility,
    history: financial.AccountHistory | None,
    estimate: AccountEstimate,
    proposal: RevisionProposal,
) -> AccountPreview:
    old = history.projections[-1] if history and history.projections else None
    old_accrual = old.version.accrual if old and old.version.state == "EXPECTED" else None
    recorded = history.receipts if history else ()
    active = sum(item.receipt.status == "ACTIVE" for item in recorded)
    void = sum(item.receipt.status == "VOID" for item in recorded)
    missing = sum(
        item.receipt.cash_scope
        != (
            "MATCHING_CASH_RECORDED_UNVERIFIED"
            if item.receipt.status == "ACTIVE"
            else "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
        )
        for item in recorded
    )
    base = AccountPreview(
        account_id=estimate.account_id,
        previous_projection_id=old.version.id if old else None,
        previous_revision_id=old.version.revision_id if old else None,
        previous_as_of=old.as_of if old else None,
        previous_currency=old_accrual.currency if old_accrual else None,
        previous_expected=old_accrual.net_local if old_accrual else None,
        proposed_accrual=None,
        expected_delta=None,
        recorded_received=None,
        recorded_outstanding=None,
        active_receipts=active,
        void_receipts=void,
        unverified_origins=sum(item.origin is None for item in recorded),
        unavailable_cash_links=missing,
        eligibility_state=observation.state,
        comparison_quality=None,
        state="ELIGIBILITY_REVIEW",
    )

    def unavailable(state: PreviewState) -> AccountPreview:
        return replace(base, state=state)

    if proposal.adjustment_basis != "AS_DECLARED":
        return unavailable("EVENT_BASIS_REVIEW")
    if (
        portfolio.book.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
        or portfolio.book.trade_creation_audit_scope == "CREATION_GAP_REVIEWED_UNVERIFIED"
    ):
        return unavailable("LEDGER_AUDIT_REVIEW")
    if observation.state == "NEEDS_REVIEW" or observation.recorded_quantity is None:
        return unavailable("ELIGIBILITY_REVIEW")
    quantity = Decimal(observation.recorded_quantity)
    if quantity == 0:
        return unavailable(
            "NO_RECORDED_POSITION_CASH_REVIEW" if active or missing else "NO_RECORDED_POSITION"
        )
    if estimate.tax_rate is None or estimate.fee is None or estimate.fee_currency is None:
        return unavailable("ESTIMATES_UNAVAILABLE")
    if (
        estimate.fee_currency != proposal.currency
        and Decimal(estimate.fee) != 0
        and estimate.fee_rate_event is None
    ):
        return unavailable("FEE_FX_UNAVAILABLE")
    calculated = accrue(
        str(snapshot.retained.event_id),
        str(estimate.account_id),
        proposal.currency,
        quantity,
        Decimal(proposal.dps),
        Decimal(estimate.tax_rate),
        Decimal(estimate.fee),
        Decimal(estimate.fx_rate_hkd) if estimate.fx_rate_hkd is not None else None,
        fee_currency=estimate.fee_currency,
        fee_rate_event=Decimal(estimate.fee_rate_event)
        if estimate.fee_rate_event is not None
        else None,
    )
    delta = None
    if (
        old is not None
        and old_accrual is not None
        and old.version.revision_id == snapshot.retained.revision_id
        and old.as_of == date.fromisoformat(proposal.as_of)
        and old_accrual.currency == calculated.currency
    ):
        with localcontext(prec=64):
            delta = round_money(calculated.net_local - old_accrual.net_local)
    comparison_quality: Quality | None = None
    if missing:
        state: PreviewState = "CASH_LINK_UNAVAILABLE"
        received = outstanding = None
    elif not active:
        state = "NO_RECORDED_ACTIVE_RECEIPTS"
        received = outstanding = None
    else:
        facts = [
            Receipt(
                str(item.receipt.id),
                str(snapshot.retained.event_id),
                str(estimate.account_id),
                item.receipt.currency,
                item.receipt.received,
                item.receipt.status == "ACTIVE",
            )
            for item in recorded
        ]
        comparison = reconcile([calculated], facts)[0]
        comparison_quality = comparison.quality
        received, outstanding = comparison.received_local, comparison.outstanding_local
        if received is None or outstanding is None:
            state = "CURRENCY_REVIEW"
        else:
            states: dict[str, PreviewState] = {
                "PARTIAL": "RECORDED_PARTIAL",
                "RECEIVED": "RECORDED_RECEIVED",
                "OVER_RECEIVED": "RECORDED_OVER_RECEIVED",
                "NEEDS_REVIEW": "RECORDED_NEEDS_REVIEW",
            }
            state = states[comparison.receipt_state]
    return replace(
        base,
        proposed_accrual=calculated,
        expected_delta=delta,
        recorded_received=received,
        recorded_outstanding=outstanding,
        comparison_quality=comparison_quality,
        state=state,
    )


def read(
    con: Connection, owner: UUID, event_id: UUID, proposal: RevisionProposal
) -> RevisionPreview | None:
    """Calculate hypothetical recorded differences in one snapshot, not financial authority."""
    if not con.in_transaction() or not all(isinstance(value, UUID) for value in (owner, event_id)):
        raise RevisionPreviewError("INVALID_DIVIDEND_REVISION_PREVIEW")
    try:
        if not isinstance(proposal, RevisionProposal):
            raise ValueError("Invalid proposal")
        proposal = RevisionProposal.model_validate(proposal.model_dump(warnings="error"))
        current = events.get(con, owner, event_id)
        if current is None:
            return None
        snapshot = revision_scope.read(
            con,
            owner,
            event_id,
            ex_date=date.fromisoformat(proposal.ex_date),
            as_of=date.fromisoformat(proposal.as_of),
            rule=proposal.eligibility_rule,
        )
        if (
            not isinstance(snapshot, revision_scope.RevisionScope)
            or not isinstance(snapshot.retained, financial.EventFinancialScope)
            or (
                snapshot.retained.event_id,
                snapshot.retained.revision_id,
                snapshot.retained.revision_no,
                snapshot.security_id,
                snapshot.proposed_ex_date,
                snapshot.as_of,
                snapshot.rule,
            )
            != (
                event_id,
                current.record.revision_id,
                current.record.revision_no,
                current.record.terms.security_id,
                date.fromisoformat(proposal.ex_date),
                date.fromisoformat(proposal.as_of),
                proposal.eligibility_rule,
            )
        ):
            raise ValueError("Inconsistent proposed revision scope")
        if (
            not isinstance(snapshot.portfolios, tuple)
            or any(
                not isinstance(item, revision_scope.PortfolioLedger) for item in snapshot.portfolios
            )
            or not isinstance(snapshot.retained.accounts, tuple)
            or any(
                not isinstance(item, financial.AccountHistory)
                for item in snapshot.retained.accounts
            )
        ):
            raise ValueError("Invalid retained scope")
        if any(
            not isinstance(item.portfolio, financial.PortfolioScope)
            or not isinstance(item.book, eligibility.RecordedEligibilityBook)
            or not isinstance(item.accounts, tuple)
            or not isinstance(item.book.accounts, tuple)
            or any(not isinstance(pin, revision_scope.AccountPins) for pin in item.accounts)
            or any(not isinstance(value, AccountEligibility) for value in item.book.accounts)
            for item in snapshot.portfolios
        ):
            raise ValueError("Invalid recorded ledger scope")
        portfolio_ids = [item.portfolio.id for item in snapshot.portfolios]
        database_portfolios = {
            events._uuid(row.id)
            for row in con.execute(
                text("SELECT id FROM portfolios WHERE owner_id=:owner"), {"owner": str(owner)}
            )
        }
        if (
            len(set(portfolio_ids)) != len(portfolio_ids)
            or set(portfolio_ids) != database_portfolios
        ):
            raise ValueError("Inconsistent portfolio scope")
        for portfolio in snapshot.portfolios:
            if (
                type(portfolio.portfolio.version) is not int
                or type(portfolio.portfolio.data_revision) is not int
                or type(portfolio.book.data_revision) is not int
                or portfolio.portfolio != financial._portfolio(con, owner, portfolio.portfolio.id)
                or (
                    portfolio.book.owner_id,
                    portfolio.book.portfolio_id,
                    portfolio.book.security_id,
                    portfolio.book.data_revision,
                    portfolio.book.ex_date,
                    portfolio.book.as_of,
                    portfolio.book.rule,
                )
                != (
                    owner,
                    portfolio.portfolio.id,
                    snapshot.security_id,
                    portfolio.portfolio.data_revision,
                    snapshot.proposed_ex_date,
                    snapshot.as_of,
                    snapshot.rule,
                )
                or [str(item.account_id) for item in portfolio.accounts]
                != [item.account_id for item in portfolio.book.accounts]
            ):
                raise ValueError("Inconsistent ledger scope")
        estimates = {item.account_id: item for item in proposal.accounts}
        account_ids = [
            item.account_id for portfolio in snapshot.portfolios for item in portfolio.accounts
        ]
        database_accounts = {
            events._uuid(row.id)
            for row in con.execute(
                text("""
                    SELECT account.id FROM accounts account
                    JOIN portfolios portfolio ON portfolio.id=account.portfolio_id
                    WHERE portfolio.owner_id=:owner
                """),
                {"owner": str(owner)},
            )
        }
        if (
            len(estimates) != len(proposal.accounts)
            or len(set(account_ids)) != len(account_ids)
            or set(estimates) != set(account_ids)
            or set(account_ids) != database_accounts
        ):
            raise ValueError("Every account requires an explicit estimate")
        histories = {item.account_id: item for item in snapshot.retained.accounts}
        if (
            len(histories) != len(snapshot.retained.accounts)
            or not set(histories) <= database_accounts
        ):
            raise ValueError("Inconsistent retained account scope")
        result = tuple(
            _account(
                snapshot,
                portfolio,
                observation,
                histories.get(pin.account_id),
                estimates[pin.account_id],
                proposal,
            )
            for portfolio in snapshot.portfolios
            for pin, observation in zip(portfolio.accounts, portfolio.book.accounts, strict=True)
        )
        return RevisionPreview(snapshot, proposal, result)
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise RevisionPreviewError("DIVIDEND_REVISION_PREVIEW_UNAVAILABLE") from None
