"""Read recorded ledger and retained history for a possible event revision."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_event_financial_scope as financial_scope
import spm_api.dividend_event_service as events
from spm_api.decimal_types import from_scaled, to_scaled
from spm_api.dividend_account_scope import account, positive_version
from spm_api.dividend_eligibility import AccountEligibility, EligibilityRule


class RevisionScopeError(ValueError):
    """Fixed errors only; do not disclose private financial data."""


@dataclass(frozen=True, slots=True)
class AccountPins:
    account_id: UUID = field(repr=False)
    broker_id: UUID = field(repr=False)
    account_version: int
    broker_version: int
    account_status: Literal["ACTIVE", "ARCHIVED"]
    broker_status: Literal["ACTIVE", "ARCHIVED"]


@dataclass(frozen=True, slots=True)
class PortfolioLedger:
    portfolio: financial_scope.PortfolioScope = field(repr=False)
    book: eligibility.RecordedEligibilityBook = field(repr=False)
    accounts: tuple[AccountPins, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class RevisionScope:
    retained: financial_scope.EventFinancialScope = field(repr=False)
    security_id: UUID = field(repr=False)
    proposed_ex_date: date = field(repr=False)
    as_of: date = field(repr=False)
    rule: EligibilityRule
    portfolios: tuple[PortfolioLedger, ...] = field(repr=False)
    scope: Literal["RECORDED_REVISION_SCOPE_ONLY"] = "RECORDED_REVISION_SCOPE_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


def _observation(
    item: AccountEligibility, *, ex_date: date, as_of: date, rule: EligibilityRule
) -> None:
    if (
        item.scope != "RECORDED_SHARES_ONLY"
        or item.history_completeness != "UNVERIFIED"
        or not isinstance(item.reasons, tuple)
        or not isinstance(item.pending_action_ids, tuple)
        or any(not isinstance(value, str) or not value for value in item.reasons)
        or any(not isinstance(value, str) or not value for value in item.pending_action_ids)
        or len(set(item.pending_action_ids)) != len(item.pending_action_ids)
    ):
        raise ValueError("Invalid recorded observation")
    if item.state == "NEEDS_REVIEW":
        if item.recorded_quantity is not None or not item.reasons:
            raise ValueError("Review quantity cannot be inferred")
        return
    if item.reasons or item.pending_action_ids:
        raise ValueError("Invalid recorded observation")
    expected = "RECORDED_PRE_EX_DATE" if ex_date <= as_of else "UPCOMING_RECORDED_HOLDINGS"
    quantity = item.recorded_quantity
    if rule != "ORDINARY_PRE_EX_DATE" or item.state != expected or not isinstance(quantity, str):
        raise ValueError("Invalid recorded observation")
    amount = to_scaled(quantity, 8)
    if amount < 0 or from_scaled(amount, 8) != quantity:
        raise ValueError("Invalid recorded quantity")


def read(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    *,
    ex_date: date,
    as_of: date,
    rule: EligibilityRule,
) -> RevisionScope | None:
    """Read all currently owned portfolios in one snapshot; this is not a rebuild grant.

    The caller supplies a proposed date and rule. Historical ownership and completeness remain
    unverified, including for accounts whose recorded quantity is zero. No future decision,
    settings, amounts, payments, event revision or reconciliation task are created here.
    """
    if (
        not con.in_transaction()
        or not all(isinstance(item, UUID) for item in (owner, event_id))
        or type(ex_date) is not date
        or type(as_of) is not date
        or not isinstance(rule, str)
        or rule not in ("ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW")
    ):
        raise RevisionScopeError("INVALID_DIVIDEND_REVISION_SCOPE")
    try:
        event = events.get(con, owner, event_id)
        if event is None:
            return None
        if (
            con.execute(
                text("SELECT 1 FROM owners WHERE id=:owner AND is_active=1"),
                {"owner": str(owner)},
            ).first()
            is None
        ):
            raise ValueError("Inactive owner")
        retained = financial_scope.history(con, owner, event_id)
        if not isinstance(retained, financial_scope.EventFinancialScope) or (
            retained.event_id,
            retained.revision_id,
            retained.revision_no,
        ) != (event_id, event.record.revision_id, event.record.revision_no):
            raise ValueError("Inconsistent event history")
        security = event.record.terms.security_id
        # ponytail: replay each owned portfolio; add verified checkpoints if histories grow large.
        ids = [
            events._uuid(row.id)
            for row in con.execute(
                text("SELECT id FROM portfolios WHERE owner_id=:owner ORDER BY id"),
                {"owner": str(owner)},
            )
        ]
        if not {item.id for item in retained.portfolios} <= set(ids):
            raise ValueError("A retained portfolio is unavailable")
        portfolios: list[PortfolioLedger] = []
        for portfolio_id in ids:
            portfolio = financial_scope._portfolio(con, owner, portfolio_id)
            book = eligibility.get(
                con, owner, portfolio_id, security, ex_date=ex_date, as_of=as_of, rule=rule
            )
            if (
                not isinstance(book, eligibility.RecordedEligibilityBook)
                or type(book.data_revision) is not int
                or book.trade_history_audit_scope
                not in (
                    "NO_RETIRED_TRADES",
                    "RETIRED_TRADE_HISTORY_UNVERIFIED",
                    "RETIRED_TRADE_HASHES_MATCHED",
                )
                or book.trade_creation_audit_scope
                not in (
                    "NO_RECORDED_CREATION_GAP_REVIEW",
                    "CREATION_GAP_REVIEWED_UNVERIFIED",
                )
                or (
                    book.latest_input_at is not None
                    and (
                        not isinstance(book.latest_input_at, datetime)
                        or book.latest_input_at.utcoffset() is None
                    )
                )
                or (
                    book.owner_id,
                    book.portfolio_id,
                    book.security_id,
                    book.data_revision,
                    book.ex_date,
                    book.as_of,
                    book.rule,
                )
                != (owner, portfolio_id, security, portfolio.data_revision, ex_date, as_of, rule)
            ):
                raise ValueError("Inconsistent ledger book")
            rows = (
                con.execute(
                    text("""
                    SELECT a.id,a.status,b.status AS broker_status FROM accounts a
                    LEFT JOIN brokers b ON b.id=a.broker_id
                    WHERE a.portfolio_id=:portfolio ORDER BY a.id
                """),
                    {"portfolio": str(portfolio_id)},
                )
                .mappings()
                .all()
            )
            if (
                not isinstance(book.accounts, tuple)
                or any(not isinstance(item, AccountEligibility) for item in book.accounts)
                or [item.account_id for item in book.accounts] != [row["id"] for row in rows]
            ):
                raise ValueError("Inconsistent ledger accounts")
            pins: list[AccountPins] = []
            for row, observation in zip(rows, book.accounts, strict=True):
                account_id = events._uuid(row["id"])
                current = account(con, owner, account_id)
                if current is None or current["portfolio_id"] != str(portfolio_id):
                    raise ValueError("Inconsistent current account")
                status, broker_status = row["status"], row["broker_status"]
                if status not in ("ACTIVE", "ARCHIVED") or broker_status not in (
                    "ACTIVE",
                    "ARCHIVED",
                ):
                    raise ValueError("Invalid current account status")
                _observation(observation, ex_date=ex_date, as_of=as_of, rule=rule)
                pins.append(
                    AccountPins(
                        account_id,
                        events._uuid(current["broker_id"]),
                        positive_version(current["account_version"]),
                        positive_version(current["broker_version"]),
                        cast(Literal["ACTIVE", "ARCHIVED"], status),
                        cast(Literal["ACTIVE", "ARCHIVED"], broker_status),
                    )
                )
            portfolios.append(PortfolioLedger(portfolio, book, tuple(pins)))
        retained_accounts = [item.account_id for item in retained.accounts]
        ledger_accounts = {
            item.account_id for portfolio in portfolios for item in portfolio.accounts
        }
        if (
            len(set(retained_accounts)) != len(retained_accounts)
            or not set(retained_accounts) <= ledger_accounts
        ):
            raise ValueError("A retained account is unavailable")
        return RevisionScope(retained, security, ex_date, as_of, rule, tuple(portfolios))
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise RevisionScopeError("DIVIDEND_REVISION_SCOPE_UNAVAILABLE") from None
