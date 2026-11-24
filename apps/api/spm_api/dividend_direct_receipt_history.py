"""Read audited direct receipt facts without authenticating or granting financial use."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as audit_history
import spm_api.dividend_receipt_history as retained
from spm_api.audit import canonical, digest
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account, positive_version


class DirectReceiptHistoryError(ValueError):
    """Fixed codes only; never disclose private receipt data or reasons."""


@dataclass(frozen=True, slots=True)
class RecordedDirectReceipt:
    """A matched recorded fact, not an authenticated payment or entitlement."""

    receipt: retained.RetainedReceipt = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    origin: audit_history.ReceiptOrigin = field(repr=False)
    audit_scope: Literal["RECORDED_AUDIT_MATCHED_ONLY"] = "RECORDED_AUDIT_MATCHED_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    payment_scope: Literal["OWNER_RECORDED_NOT_VERIFIED"] = "OWNER_RECORDED_NOT_VERIFIED"


_RECEIPT_FIELDS = (
    "id",
    "event_id",
    "revision_id",
    "account_id",
    "received_date",
    "received_micro",
    "currency",
    "tax_micro",
    "fee_micro",
    "fx_rate_hkd_e12",
    "received_hkd_micro",
    "note",
    "created_at",
)
_ORIGIN_FIELDS = (
    "receipt_id",
    "owner_id",
    "event_id",
    "revision_id",
    "revision_no",
    "account_id",
    "portfolio_id",
    "broker_id",
    "account_version",
    "portfolio_version",
    "broker_version",
    "portfolio_data_revision",
    "cash_entry_id",
    "fx_source",
    "actual_hkd_reason",
    "request_id",
    "created_at",
)


def _creation_payload(
    receipt: Mapping[Any, Any], origin: Mapping[Any, Any], cash: Mapping[Any, Any], reason: str
) -> dict[str, Any]:
    audit_history._reason(reason, allow_empty=False)
    return {
        "receipt": {
            **{name: receipt[name] for name in _RECEIPT_FIELDS},
            "status": "ACTIVE",
            "version": 1,
            "voided_at": None,
        },
        "origin": {name: origin[name] for name in _ORIGIN_FIELDS},
        "cash": {**{name: cash[name] for name in audit_history._CASH_FIELDS}, "version": 1},
        "reason_sha256": digest(canonical(reason)),
        "scope": "DIRECT_RECORDED_RECEIPT_AUDIT_ONLY",
    }


def _void_payload(
    receipt: Mapping[Any, Any],
    reversal: Mapping[Any, Any],
    request_id: str,
    reason: str,
    voided_at: str,
) -> dict[str, Any]:
    audit_history._reason(reason, allow_empty=False)
    return {
        "receipt_id": receipt["id"],
        "event_id": receipt["event_id"],
        "revision_id": receipt["revision_id"],
        "account_id": receipt["account_id"],
        "status": "VOID",
        "version": 2,
        "voided_at": voided_at,
        "reversal": {**{name: reversal[name] for name in audit_history._CASH_FIELDS}, "version": 1},
        "request_id": request_id,
        "reason_sha256": digest(canonical(reason)),
        "scope": "DIRECT_RECORDED_RECEIPT_VOID_AUDIT_ONLY",
    }


def _read_one(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    account_id: UUID,
    current_account: RowMapping,
    row: RowMapping,
    cache: dict[int, tuple[events.ReviewedEvent, datetime]],
) -> RecordedDirectReceipt:
    item = retained._receipt(con, row, account_id, source_type="DIVIDEND_DIRECT_RECEIPT")
    if (
        row["event_id"] != str(event_id)
        or con.execute(
            text("SELECT 1 FROM dividend_receipts WHERE id=:id"), {"id": str(item.id)}
        ).first()
        is not None
    ):
        raise ValueError("Inconsistent direct receipt identity")
    retained._integer(row["received_micro"], -INT64_MAX)
    if row["received_hkd_micro"] is not None:
        retained._integer(row["received_hkd_micro"], -INT64_MAX)
    revision_no = audit_history._integer(row["revision_no"], 1)
    revision, source_time = audit_history._revision(con, owner, event_id, revision_no, cache)
    revision_id = events._uuid(row["revision_id"])
    if revision.record.revision_id != revision_id or item.created_at < source_time:
        raise ValueError("Inconsistent direct receipt revision")
    origin = (
        con.execute(
            text("""
                SELECT o.*,p.owner_id AS retained_portfolio_owner,
                       p.version AS retained_portfolio_version,
                       p.data_revision AS retained_data_revision,
                       b.owner_id AS retained_broker_owner,b.version AS retained_broker_version
                FROM dividend_direct_receipt_origins o
                LEFT JOIN portfolios p ON p.id=o.portfolio_id
                LEFT JOIN brokers b ON b.id=o.broker_id
                WHERE o.receipt_id=:id
            """),
            {"id": str(item.id)},
        )
        .mappings()
        .one()
    )
    portfolio_id, broker_id = (events._uuid(origin[name]) for name in ("portfolio_id", "broker_id"))
    cash_id, audit_id, request_id = (
        events._uuid(origin[name]) for name in ("cash_entry_id", "audit_id", "request_id")
    )
    account_version = positive_version(origin["account_version"])
    portfolio_version = positive_version(origin["portfolio_version"])
    broker_version = positive_version(origin["broker_version"])
    data_revision = audit_history._integer(origin["portfolio_data_revision"])
    created = events._time(origin["created_at"])
    if (
        origin["receipt_id"] != str(item.id)
        or origin["owner_id"] != str(owner)
        or origin["event_id"] != str(event_id)
        or origin["revision_id"] != str(revision_id)
        or type(origin["revision_no"]) is not int
        or origin["revision_no"] != revision_no
        or origin["account_id"] != str(account_id)
        or origin["retained_portfolio_owner"] != str(owner)
        or origin["retained_broker_owner"] != str(owner)
        or account_version > positive_version(current_account["account_version"])
        or portfolio_version > positive_version(origin["retained_portfolio_version"])
        or broker_version > positive_version(origin["retained_broker_version"])
        or data_revision > audit_history._integer(origin["retained_data_revision"])
        or created < item.created_at
        or created < source_time
    ):
        raise ValueError("Inconsistent direct receipt origin")
    cash = (
        con.execute(text("SELECT * FROM cash_ledger_entries WHERE id=:id"), {"id": str(cash_id)})
        .mappings()
        .one()
    )
    cash_time = events._time(cash["created_at"])
    if (
        cash["source_id"] != str(item.id)
        or cash["source_type"] != "DIVIDEND_DIRECT_RECEIPT"
        or cash["entry_role"] != "PRIMARY"
        or cash["note"] != row["note"]
        or not isinstance(row["note"], str)
        or "\x00" in row["note"]
        or len(row["note"]) > 1000
        or cash_time < item.created_at
        or cash_time > created
        or item.cash_scope
        not in {
            "MATCHING_CASH_RECORDED_UNVERIFIED",
            "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED",
        }
    ):
        raise ValueError("Inconsistent direct receipt cash")
    audit_history._booking(row, origin)
    audits = (
        con.execute(
            text(
                "SELECT * FROM audit_events WHERE entity_type='dividend_direct_receipt' "
                "AND entity_id=:id"
            ),
            {"id": str(item.id)},
        )
        .mappings()
        .all()
    )
    creation = [audit for audit in audits if audit["action"] == "DIVIDEND_DIRECT_RECEIPT_RECORDED"]
    voids = [audit for audit in audits if audit["action"] == "DIVIDEND_DIRECT_RECEIPT_VOIDED"]
    if len(creation) != 1 or len(audits) != len(creation) + len(voids):
        raise ValueError("Inconsistent direct receipt audits")
    created_audit = creation[0]
    reason = audit_history._reason(created_audit["reason"], allow_empty=False)
    original_hash = digest(canonical(_creation_payload(row, origin, cash, reason)))
    if (
        created_audit["id"] != str(audit_id)
        or created_audit["owner_id"] != str(owner)
        or created_audit["request_id"] != str(request_id)
        or events._time(created_audit["created_at"]) != created
        or created_audit["before_sha256"] is not None
        or created_audit["after_sha256"] != original_hash
    ):
        raise ValueError("Inconsistent direct receipt creation audit")
    if item.status == "ACTIVE":
        if item.version != 1 or audit_history._integer(cash["version"], 1) != 1 or voids:
            raise ValueError("Inconsistent active direct receipt")
    else:
        if item.version != 2 or audit_history._integer(cash["version"], 1) != 2 or len(voids) != 1:
            raise ValueError("Inconsistent direct receipt void")
        reversal = (
            con.execute(
                text("SELECT * FROM cash_ledger_entries WHERE reverses_entry_id=:id"),
                {"id": str(cash_id)},
            )
            .mappings()
            .one()
        )
        void_audit = voids[0]
        void_request = events._uuid(void_audit["request_id"])
        events._uuid(void_audit["id"])
        void_reason = audit_history._reason(void_audit["reason"], allow_empty=False)
        reversed_at = events._time(reversal["created_at"])
        voided_at = events._time(void_audit["created_at"])
        if (
            void_audit["owner_id"] != str(owner)
            or item.voided_at != voided_at
            or audit_history._integer(reversal["version"], 1) != 1
            or reversal["note"] != void_reason
            or reversed_at < created
            or reversed_at > voided_at
            or void_audit["before_sha256"] != original_hash
            or void_audit["after_sha256"]
            != digest(
                canonical(
                    _void_payload(row, reversal, str(void_request), void_reason, row["voided_at"])
                )
            )
        ):
            raise ValueError("Inconsistent direct receipt void audit")
    return RecordedDirectReceipt(
        item,
        revision_id,
        revision_no,
        audit_history.ReceiptOrigin(
            revision_id,
            revision_no,
            portfolio_id,
            broker_id,
            account_version,
            portfolio_version,
            broker_version,
            data_revision,
            cash_id,
            cash["note"],
            origin["fx_source"],
            origin["actual_hkd_reason"],
            request_id,
            audit_id,
            reason,
            created,
        ),
    )


def history(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID
) -> tuple[RecordedDirectReceipt, ...] | None:
    """Read all direct receipts in one snapshot, not current authority or confirmed payment."""
    if not con.in_transaction() or not all(
        isinstance(value, UUID) for value in (owner, event_id, account_id)
    ):
        raise DirectReceiptHistoryError("INVALID_DIRECT_RECEIPT_SCOPE")
    try:
        event = events.get(con, owner, event_id)
        if event is None:
            return None
        current = account(con, owner, account_id)
        if current is None:
            return None
        mismatched = con.execute(
            text("""
                SELECT 1 FROM dividend_direct_receipt_origins o
                LEFT JOIN dividend_direct_receipts receipt ON receipt.id=o.receipt_id
                LEFT JOIN dividend_event_revisions rev ON rev.id=receipt.revision_id
                WHERE o.event_id=:event AND o.account_id=:account
                  AND (receipt.id IS NULL OR receipt.event_id IS NOT o.event_id
                    OR receipt.account_id IS NOT o.account_id
                    OR receipt.revision_id IS NOT o.revision_id
                    OR rev.event_id IS NOT o.event_id OR rev.revision_no IS NOT o.revision_no)
                LIMIT 1
            """),
            {"event": str(event_id), "account": str(account_id)},
        ).first()
        if mismatched is not None:
            raise ValueError("Inconsistent direct receipt scope")
        rows = (
            con.execute(
                text("""
                    SELECT receipt.*,revision.revision_no,'MANUAL' AS source
                    FROM dividend_direct_receipts receipt
                    LEFT JOIN dividend_event_revisions revision ON revision.id=receipt.revision_id
                    WHERE receipt.event_id=:event AND receipt.account_id=:account
                    ORDER BY receipt.received_date,receipt.created_at,receipt.id
                """),
                {"event": str(event_id), "account": str(account_id)},
            )
            .mappings()
            .all()
        )
        cache: dict[int, tuple[events.ReviewedEvent, datetime]] = {}
        return tuple(
            _read_one(con, owner, event_id, account_id, current, row, cache) for row in rows
        )
    except (
        events.EventError,
        retained.ReceiptHistoryError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
    ):
        raise DirectReceiptHistoryError("DIRECT_RECEIPT_HISTORY_UNAVAILABLE") from None
