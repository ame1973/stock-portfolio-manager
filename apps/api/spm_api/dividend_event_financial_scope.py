"""Discover retained event financial links without authorizing a rebuild."""

from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_event_service as events
import spm_api.dividend_projection_history as projection_history
import spm_api.dividend_receipt_audit as receipt_audit
import spm_api.dividend_receipt_history as legacy
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account, positive_version


class EventFinancialScopeError(ValueError):
    """Use fixed codes without disclosing owner data or financial records."""


@dataclass(frozen=True, slots=True)
class PortfolioScope:
    id: UUID = field(repr=False)
    version: int
    data_revision: int


@dataclass(frozen=True, slots=True)
class AccountHistory:
    account_id: UUID = field(repr=False)
    portfolio_id: UUID = field(repr=False)
    broker_id: UUID = field(repr=False)
    account_status: Literal["ACTIVE", "ARCHIVED"]
    account_version: int
    portfolio_version: int
    portfolio_data_revision: int
    broker_version: int
    entitlements: tuple[legacy.RetainedEntitlement, ...] = field(repr=False)
    projections: tuple[projection_history.RetainedProjection, ...] = field(repr=False)
    receipts: tuple[combined.RecordedReceipt, ...] = field(repr=False)
    unverified_origins: int


@dataclass(frozen=True, slots=True)
class EventFinancialScope:
    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    accounts: tuple[AccountHistory, ...] = field(repr=False)
    portfolios: tuple[PortfolioScope, ...] = field(repr=False)
    scope: Literal["RETAINED_FINANCIAL_SCOPE_ONLY"] = "RETAINED_FINANCIAL_SCOPE_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


def _portfolio(con: Connection, owner: UUID, identifier: UUID) -> PortfolioScope:
    row = con.execute(
        text("SELECT owner_id,version,data_revision FROM portfolios WHERE id=:id"),
        {"id": str(identifier)},
    ).first()
    if row is None or row.owner_id != str(owner):
        raise ValueError("Invalid retained portfolio")
    revision = row.data_revision
    if type(revision) is not int or not 0 <= revision <= INT64_MAX:
        raise ValueError("Invalid portfolio data revision")
    return PortfolioScope(identifier, positive_version(row.version), revision)


def history(con: Connection, owner: UUID, event_id: UUID) -> EventFinancialScope | None:
    """Read linked histories in one snapshot, not all affected accounts or a financial grant.

    Accounts with holdings but no linked event history are intentionally not discovered here.
    Legacy entitlements and receipts without origins cannot establish historical portfolios.
    """
    if not con.in_transaction() or not all(isinstance(value, UUID) for value in (owner, event_id)):
        raise EventFinancialScopeError("INVALID_DIVIDEND_FINANCIAL_SCOPE")
    try:
        event = events.get(con, owner, event_id)
        if event is None:
            return None
        # ponytail: scan all linked accounts; page in one snapshot if histories grow large.
        rows = con.execute(
            text("""
                SELECT ent.account_id FROM dividend_entitlements ent
                JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                WHERE rev.event_id=:event
                UNION
                SELECT receipt.account_id FROM dividend_receipts receipt
                JOIN dividend_entitlements ent ON ent.id=receipt.entitlement_id
                JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                WHERE rev.event_id=:event
                UNION SELECT account_id FROM dividend_projection_versions WHERE event_id=:event
                UNION SELECT account_id FROM dividend_direct_receipts WHERE event_id=:event
                UNION SELECT account_id FROM dividend_direct_receipt_origins WHERE event_id=:event
                UNION SELECT account_id FROM dividend_receipt_origins WHERE event_id=:event
                ORDER BY account_id
            """),
            {"event": str(event_id)},
        ).all()
        accounts: list[AccountHistory] = []
        portfolio_ids: set[UUID] = set()
        for row in rows:
            identifier = events._uuid(row.account_id)
            current = account(con, owner, identifier)
            if current is None:
                raise ValueError("Invalid current account")
            portfolio_id = events._uuid(current["portfolio_id"])
            broker_id = events._uuid(current["broker_id"])
            status = con.execute(
                text("SELECT status FROM accounts WHERE id=:id"), {"id": str(identifier)}
            ).scalar_one()
            if status not in ("ACTIVE", "ARCHIVED"):
                raise ValueError("Invalid account status")
            portfolio = _portfolio(con, owner, portfolio_id)
            retained = legacy._legacy_history(con, owner, event_id, identifier)
            versions = projection_history.history(con, owner, event_id, identifier)
            recorded = combined.history(con, owner, event_id, identifier)
            if retained is None or versions is None or recorded is None:
                raise ValueError("Inconsistent financial histories")
            legacy_ids = {item.id for ent in retained.entitlements for item in ent.receipts}
            audited_ids = {
                item.receipt.id
                for item in recorded
                if isinstance(item, receipt_audit.AuditedReceipt)
            }
            direct_ids = {
                events._uuid(item.id)
                for item in con.execute(
                    text(
                        "SELECT id FROM dividend_direct_receipts "
                        "WHERE event_id=:event AND account_id=:account"
                    ),
                    {"event": str(event_id), "account": str(identifier)},
                )
            }
            recorded_direct_ids = {
                item.receipt.id
                for item in recorded
                if isinstance(item, direct.RecordedDirectReceipt)
            }
            if (
                legacy_ids != audited_ids
                or direct_ids != recorded_direct_ids
                or len(recorded) != len(audited_ids) + len(recorded_direct_ids)
                or not (retained.entitlements or versions or recorded)
            ):
                raise ValueError("Inconsistent recorded links")
            portfolio_ids.add(portfolio_id)
            portfolio_ids.update(item.portfolio_id for item in versions)
            portfolio_ids.update(item.origin.portfolio_id for item in recorded if item.origin)
            accounts.append(
                AccountHistory(
                    account_id=identifier,
                    portfolio_id=portfolio_id,
                    broker_id=broker_id,
                    account_status=cast(Literal["ACTIVE", "ARCHIVED"], status),
                    account_version=positive_version(current["account_version"]),
                    portfolio_version=portfolio.version,
                    portfolio_data_revision=portfolio.data_revision,
                    broker_version=positive_version(current["broker_version"]),
                    entitlements=retained.entitlements,
                    projections=versions,
                    receipts=recorded,
                    unverified_origins=sum(item.origin is None for item in recorded),
                )
            )
        portfolios = tuple(
            _portfolio(con, owner, identifier) for identifier in sorted(portfolio_ids)
        )
        return EventFinancialScope(
            event_id,
            event.record.revision_id,
            event.record.revision_no,
            tuple(accounts),
            portfolios,
        )
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise EventFinancialScopeError("DIVIDEND_FINANCIAL_SCOPE_UNAVAILABLE") from None
