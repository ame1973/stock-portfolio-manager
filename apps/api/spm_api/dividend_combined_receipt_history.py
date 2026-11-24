"""Combine recorded legacy and direct receipt histories without granting financial use."""

from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_direct_receipt_history as direct
import spm_api.dividend_receipt_audit as legacy


class CombinedReceiptHistoryError(ValueError):
    """Fixed codes only; never include private receipt values or identities."""


RecordedReceipt = legacy.AuditedReceipt | direct.RecordedDirectReceipt


def history(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID
) -> tuple[RecordedReceipt, ...] | None:
    """Read both complete retained stores in one snapshot, not payment or eligibility."""
    if not con.in_transaction() or not all(
        isinstance(value, UUID) for value in (owner, event_id, account_id)
    ):
        raise CombinedReceiptHistoryError("INVALID_COMBINED_RECEIPT_SCOPE")
    try:
        direct_items = direct.history(con, owner, event_id, account_id)
        if direct_items is None:
            return None
        legacy_items = legacy._history(con, owner, event_id, account_id, legacy_only=True)
        if legacy_items is None:
            raise ValueError("Inconsistent combined receipt scope")
        if any(not isinstance(item, direct.RecordedDirectReceipt) for item in direct_items) or any(
            not isinstance(item, legacy.AuditedReceipt) for item in legacy_items
        ):
            raise ValueError("Inconsistent combined receipt history")
        items: tuple[RecordedReceipt, ...] = (*legacy_items, *direct_items)
        identifiers = {item.receipt.id for item in items}
        if len(identifiers) != len(items) or any(
            not isinstance(value, UUID) for value in identifiers
        ):
            raise ValueError("Duplicate or invalid recorded receipt identity")
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    item.receipt.received_date,
                    item.receipt.created_at,
                    str(item.receipt.id),
                ),
            )
        )
    except (
        direct.DirectReceiptHistoryError,
        legacy.ReceiptAuditError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
    ):
        raise CombinedReceiptHistoryError("COMBINED_RECEIPT_HISTORY_UNAVAILABLE") from None
