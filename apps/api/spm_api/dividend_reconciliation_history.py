"""Compare recorded projections and receipt links without granting financial authority."""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_projection_history as projections
import spm_api.dividend_receipt_audit as receipts
from spm_api.dividend_math import Receipt, reconcile
from spm_api.dividend_projection import ProjectionState
from spm_api.finance import FinanceError, Quality
from spm_api.master_models import Currency


class ReconciliationHistoryError(ValueError):
    """Fixed error codes only; retained amounts and identities remain private."""


RecordedState = Literal[
    "NO_RECORDED_PROJECTION",
    "STALE_RECORDED_PROJECTION",
    "PROJECTION_NOT_COMPARABLE",
    "CASH_LINK_UNAVAILABLE",
    "NO_RECORDED_ACTIVE_RECEIPTS",
    "CURRENCY_REVIEW",
    "RECORDED_PARTIAL",
    "RECORDED_RECEIVED",
    "RECORDED_OVER_RECEIVED",
    "RECORDED_NEEDS_REVIEW",
]


@dataclass(frozen=True, slots=True)
class RecordedReconciliation:
    """Recorded comparison only, not a current entitlement or payment assertion."""

    event_id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    account_id: UUID = field(repr=False)
    projection_id: UUID | None = field(repr=False)
    projection_no: int | None
    projection_as_of: date | None = field(repr=False)
    projection_state: ProjectionState | None
    projection_quality: Quality | None
    comparison_quality: Quality | None
    currency: Currency | None
    recorded_expected: Decimal | None = field(repr=False)
    recorded_received: Decimal | None = field(repr=False)
    recorded_outstanding: Decimal | None = field(repr=False)
    active_receipts: int
    void_receipts: int
    unverified_origins: int
    unavailable_cash_links: int
    state: RecordedState
    scope: Literal["UNVERIFIED_RECORDED_RECONCILIATION_ONLY"] = (
        "UNVERIFIED_RECORDED_RECONCILIATION_ONLY"
    )
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"] = "OWNER_RECORDED_NOT_VERIFIED"


def read(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID
) -> RecordedReconciliation | None:
    """Read a single retained snapshot, not current authority or confirmed payment."""
    if not con.in_transaction() or not all(
        isinstance(value, UUID) for value in (owner, event_id, account_id)
    ):
        raise ReconciliationHistoryError("INVALID_DIVIDEND_RECONCILIATION_SCOPE")
    try:
        event = events.get(con, owner, event_id)
        if event is None:
            return None
        versions = projections.history(con, owner, event_id, account_id)
        if versions is None:
            return None
        history = receipts.history(con, owner, event_id, account_id)
        if history is None:
            raise ValueError("Inconsistent retained scope")

        facts: list[Receipt] = []
        identifiers: set[UUID] = set()
        missing_cash = unverified = void_count = 0
        for item in history:
            value = item.receipt
            if (
                not isinstance(value.id, UUID)
                or value.id in identifiers
                or value.status not in ("ACTIVE", "VOID")
                or item.audit_scope
                != ("RECORDED_AUDIT_MATCHED_ONLY" if item.origin else "RECEIPT_AUDIT_NOT_VERIFIED")
            ):
                raise ValueError("Inconsistent retained receipt")
            identifiers.add(value.id)
            active = value.status == "ACTIVE"
            void_count += not active
            unverified += item.origin is None
            expected_link = (
                "MATCHING_CASH_RECORDED_UNVERIFIED"
                if active
                else "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
            )
            missing_cash += value.cash_scope != expected_link
            facts.append(
                Receipt(
                    str(value.id),
                    str(event_id),
                    str(account_id),
                    value.currency,
                    value.received,
                    active,
                )
            )

        latest = versions[-1] if versions else None
        version = latest.version if latest else None
        result = RecordedReconciliation(
            event_id=event_id,
            revision_id=event.record.revision_id,
            revision_no=event.record.revision_no,
            account_id=account_id,
            projection_id=version.id if version else None,
            projection_no=version.projection_no if version else None,
            projection_as_of=latest.as_of if latest else None,
            projection_state=version.state if version else None,
            projection_quality=version.accrual.quality if version and version.accrual else None,
            comparison_quality=None,
            currency=None,
            recorded_expected=None,
            recorded_received=None,
            recorded_outstanding=None,
            active_receipts=len(facts) - void_count,
            void_receipts=void_count,
            unverified_origins=unverified,
            unavailable_cash_links=missing_cash,
            state="NO_RECORDED_PROJECTION",
        )
        if version is None:
            return result
        if (version.revision_id, version.revision_no) != (
            event.record.revision_id,
            event.record.revision_no,
        ):
            return replace(result, state="STALE_RECORDED_PROJECTION")
        if version.state != "EXPECTED" or version.accrual is None:
            return replace(result, state="PROJECTION_NOT_COMPARABLE")
        accrual = version.accrual
        result = replace(result, currency=accrual.currency, recorded_expected=accrual.net_local)
        if missing_cash:
            return replace(result, state="CASH_LINK_UNAVAILABLE")
        if not result.active_receipts:
            return replace(result, state="NO_RECORDED_ACTIVE_RECEIPTS")
        comparison = reconcile([accrual], facts)[0]
        result = replace(result, comparison_quality=comparison.quality)
        if comparison.received_local is None or comparison.outstanding_local is None:
            return replace(result, state="CURRENCY_REVIEW")
        states: dict[str, RecordedState] = {
            "PARTIAL": "RECORDED_PARTIAL",
            "RECEIVED": "RECORDED_RECEIVED",
            "OVER_RECEIVED": "RECORDED_OVER_RECEIVED",
            "NEEDS_REVIEW": "RECORDED_NEEDS_REVIEW",
        }
        return replace(
            result,
            recorded_received=comparison.received_local,
            recorded_outstanding=comparison.outstanding_local,
            state=states[comparison.receipt_state],
        )
    except (
        events.EventError,
        projections.ProjectionHistoryError,
        receipts.ReceiptAuditError,
        FinanceError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
    ):
        raise ReconciliationHistoryError("DIVIDEND_RECONCILIATION_UNAVAILABLE") from None
