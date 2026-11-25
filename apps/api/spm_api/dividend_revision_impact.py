"""Classify recorded revision impact without authorizing or writing a rebuild."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_financial_scope as financial
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current


@dataclass(frozen=True, slots=True)
class AccountImpact:
    """A recorded account disposition, never proof of historical ownership or payment."""

    account_id: UUID = field(repr=False)
    portfolio_id: UUID = field(repr=False)
    broker_id: UUID = field(repr=False)
    account_version: int
    broker_version: int
    account_status: Literal["ACTIVE", "ARCHIVED"]
    broker_status: Literal["ACTIVE", "ARCHIVED"]
    disposition: Literal["REBUILD_REQUIRED", "NO_RECORDED_IMPACT"]
    recorded_position: str | None = field(repr=False)
    has_recorded_history: bool
    entitlements: int
    projections: int
    active_receipts: int
    void_receipts: int
    unverified_origins: int
    retained_portfolios: tuple[UUID, ...] = field(repr=False)
    historical_portfolio_scope: Literal["UNVERIFIED"] = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class RevisionImpact:
    """A writer-scoped recorded plan, not a financial, session or source grant."""

    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    accounts: tuple[AccountImpact, ...] = field(repr=False)
    invalidation_portfolios: tuple[financial.PortfolioScope, ...] = field(repr=False)
    scope: Literal["RECORDED_REVISION_IMPACT_ONLY"] = "RECORDED_REVISION_IMPACT_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    source_scope: Literal["CURRENT_SOURCE_NOT_REVALIDATED"] = "CURRENT_SOURCE_NOT_REVALIDATED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    payment_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


def inspect(
    con: Connection,
    owner: UUID,
    decision: event_writer.EventIntent,
    proposal: preview.RevisionProposal,
    observed: rebuild.RebuildObservation,
) -> RevisionImpact:
    """Include recorded history, current owned accounts and known old portfolios.

    A no-impact disposition is only recorded past-ex-date zero without retained history; it is
    not complete ownership or eligibility. Repeat the entire check after any transaction change.
    """
    fresh = current.require_current(con, owner, decision, proposal, observed)
    try:
        if not isinstance(fresh, rebuild.RebuildObservation) or not current._exact(fresh, observed):
            raise ValueError("Inconsistent current observation")
        snapshot = fresh.preview.snapshot
        portfolios = {item.portfolio.id: item.portfolio for item in snapshot.portfolios}
        histories = {item.account_id: item for item in snapshot.retained.accounts}
        comparisons = {item.account_id: item for item in fresh.preview.accounts}
        if (
            len(portfolios) != len(snapshot.portfolios)
            or len(histories) != len(snapshot.retained.accounts)
            or len(comparisons) != len(fresh.preview.accounts)
        ):
            raise ValueError("Duplicate recorded scope")
        retained_portfolios = {item.id for item in snapshot.retained.portfolios}
        if len(retained_portfolios) != len(snapshot.retained.portfolios):
            raise ValueError("Duplicate retained portfolio")
        # ponytail: independent scans; add verified security indexes if histories grow.
        linked = [
            events._uuid(value)
            for value in event_writer._portfolios(con, owner, snapshot.security_id)
        ]
        rows = con.execute(
            text("""
                SELECT p.id FROM portfolios p WHERE p.owner_id=:owner AND (
                    EXISTS(SELECT 1 FROM trades t
                           WHERE t.portfolio_id=p.id AND t.security_id=:security)
                    OR EXISTS(SELECT 1 FROM security_transfers t
                              WHERE t.portfolio_id=p.id AND t.security_id=:security)
                    OR EXISTS(SELECT 1 FROM positions s
                              WHERE s.portfolio_id=p.id AND s.security_id=:security)
                ) ORDER BY p.id
            """),
            {"owner": str(owner), "security": str(snapshot.security_id)},
        ).scalars()
        if linked != [events._uuid(value) for value in rows]:
            raise ValueError("Inconsistent security-linked portfolios")
        affected = set(linked)
        affected.update(retained_portfolios)
        accounts: list[AccountImpact] = []
        for portfolio in snapshot.portfolios:
            for pin, position in zip(portfolio.accounts, portfolio.book.accounts, strict=True):
                compared = comparisons[pin.account_id]
                history = histories.get(pin.account_id)
                if position.account_id != str(pin.account_id):
                    raise ValueError("Inconsistent recorded account")
                known_portfolios: set[UUID] = set()
                if history is not None:
                    if (history.portfolio_id, history.broker_id) != (
                        portfolio.portfolio.id,
                        pin.broker_id,
                    ):
                        raise ValueError("Inconsistent retained account")
                    known_portfolios.update(item.portfolio_id for item in history.projections)
                    known_portfolios.update(
                        item.origin.portfolio_id for item in history.receipts if item.origin
                    )
                    if not known_portfolios <= retained_portfolios:
                        raise ValueError("A retained portfolio is unavailable")
                no_impact = (
                    history is None
                    and position.state == "RECORDED_PRE_EX_DATE"
                    and position.recorded_quantity == "0.00000000"
                    and compared.state == "NO_RECORDED_POSITION"
                )
                if not no_impact:
                    affected.add(portfolio.portfolio.id)
                accounts.append(
                    AccountImpact(
                        pin.account_id,
                        portfolio.portfolio.id,
                        pin.broker_id,
                        pin.account_version,
                        pin.broker_version,
                        pin.account_status,
                        pin.broker_status,
                        "NO_RECORDED_IMPACT" if no_impact else "REBUILD_REQUIRED",
                        position.recorded_quantity,
                        history is not None,
                        len(history.entitlements) if history else 0,
                        len(history.projections) if history else 0,
                        compared.active_receipts,
                        compared.void_receipts,
                        history.unverified_origins if history else 0,
                        tuple(sorted(known_portfolios)),
                    )
                )
        if (
            len({item.account_id for item in accounts}) != len(accounts)
            or {item.account_id for item in accounts} != set(comparisons)
            or not set(histories) <= set(comparisons)
            or not affected <= set(portfolios)
        ):
            raise ValueError("Incomplete recorded impact")
        return RevisionImpact(
            snapshot.retained.event_id,
            snapshot.retained.revision_id,
            snapshot.retained.revision_no,
            tuple(accounts),
            tuple(portfolios[identifier] for identifier in sorted(affected)),
        )
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise rebuild.RebuildError("DIVIDEND_REBUILD_IMPACT_UNAVAILABLE") from None
