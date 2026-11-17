"""Reconstruct recorded receipt audit inputs without authenticating or granting income."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_history as retained
from spm_api.audit import canonical, digest
from spm_api.decimal_types import INT64_MAX, from_scaled
from spm_api.dividend_account_scope import account, positive_version
from spm_api.dividend_receipt_terms import ReceiptComponents, calculate_components


class ReceiptAuditError(ValueError):
    """Fixed codes only; never disclose receipt amounts, source details or reasons."""


@dataclass(frozen=True, slots=True)
class ReceiptOrigin:
    observed_revision_id: UUID = field(repr=False)
    observed_revision_no: int
    portfolio_id: UUID = field(repr=False)
    broker_id: UUID = field(repr=False)
    account_version: int
    portfolio_version: int
    broker_version: int
    portfolio_data_revision: int
    cash_entry_id: UUID = field(repr=False)
    cash_note: str = field(repr=False)
    fx_source: str | None = field(repr=False)
    actual_hkd_reason: str | None = field(repr=False)
    request_id: UUID = field(repr=False)
    audit_id: UUID = field(repr=False)
    creation_reason: str = field(repr=False)
    created_at: datetime = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuditedReceipt:
    receipt: retained.RetainedReceipt = field(repr=False)
    entitlement_id: UUID = field(repr=False)
    original_revision_id: UUID = field(repr=False)
    origin: ReceiptOrigin | None = field(repr=False)
    audit_scope: Literal["RECEIPT_AUDIT_NOT_VERIFIED", "RECORDED_AUDIT_MATCHED_ONLY"]
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


_RECEIPT_FIELDS = (
    "id",
    "entitlement_id",
    "account_id",
    "received_date",
    "received_micro",
    "currency",
    "tax_micro",
    "fee_micro",
    "fx_rate_hkd_e12",
    "received_hkd_micro",
    "note",
    "source",
    "created_at",
)
_ORIGIN_FIELDS = (
    "receipt_id",
    "owner_id",
    "event_id",
    "observed_revision_id",
    "observed_revision_no",
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
_CASH_FIELDS = (
    "id",
    "account_id",
    "effective_date",
    "currency",
    "amount_micro",
    "fx_rate_hkd_e12",
    "booked_hkd_micro",
    "kind",
    "source_type",
    "source_id",
    "entry_role",
    "external_flow",
    "note",
    "created_at",
    "reverses_entry_id",
)


def _reason(value: object, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Invalid receipt audit reason")
    if (allow_empty and value == "") or (10 <= len(value) <= 1000 and value == value.strip()):
        return value
    raise ValueError("Invalid receipt audit reason")


def _creation_payload(
    receipt: Mapping[Any, Any], origin: Mapping[Any, Any], cash: Mapping[Any, Any], reason: str
) -> dict[str, Any]:
    """Canonical original inputs; callers still validate data and authorization separately."""
    _reason(reason, allow_empty=True)
    return {
        "receipt": {
            **{name: receipt[name] for name in _RECEIPT_FIELDS},
            "status": "ACTIVE",
            "version": 1,
            "voided_at": None,
        },
        "origin": {name: origin[name] for name in _ORIGIN_FIELDS},
        "cash": {**{name: cash[name] for name in _CASH_FIELDS}, "version": 1},
        "reason_sha256": digest(canonical(reason)),
        "scope": "RECORDED_RECEIPT_AUDIT_ONLY",
    }


def _void_payload(
    receipt: Mapping[Any, Any],
    origin: Mapping[Any, Any],
    reversal: Mapping[Any, Any],
    request_id: str,
    reason: str,
    voided_at: str,
) -> dict[str, Any]:
    _reason(reason, allow_empty=False)
    return {
        "receipt_id": receipt["id"],
        "event_id": origin["event_id"],
        "account_id": origin["account_id"],
        "status": "VOID",
        "version": 2,
        "voided_at": voided_at,
        "reversal": {**{name: reversal[name] for name in _CASH_FIELDS}, "version": 1},
        "request_id": request_id,
        "reason_sha256": digest(canonical(reason)),
        "scope": "RECORDED_RECEIPT_VOID_AUDIT_ONLY",
    }


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= INT64_MAX:
        raise ValueError("Invalid receipt audit ordinal")
    return value


def _optional_scaled(value: object, scale: int) -> str | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("Invalid scaled receipt value")
    return from_scaled(value, scale)


def _booking(receipt: RowMapping, origin: RowMapping) -> None:
    reason = origin["actual_hkd_reason"]
    rate = _optional_scaled(receipt["fx_rate_hkd_e12"], 12)
    booked = _optional_scaled(receipt["received_hkd_micro"], 6)
    terms = ReceiptComponents.model_validate(
        {
            "received_amount": from_scaled(receipt["received_micro"], 6),
            "currency": receipt["currency"],
            "tax": _optional_scaled(receipt["tax_micro"], 6),
            "fee": _optional_scaled(receipt["fee_micro"], 6),
            "fx_rate_hkd": rate,
            "fx_source": origin["fx_source"],
            "actual_hkd": booked if reason is not None else None,
            "actual_hkd_reason": reason,
        }
    )
    calculated = calculate_components(terms)
    if (
        terms.fx_source != origin["fx_source"]
        or terms.actual_hkd_reason != reason
        or calculated.received_micro != receipt["received_micro"]
        or calculated.tax_micro != receipt["tax_micro"]
        or calculated.fee_micro != receipt["fee_micro"]
        or calculated.fx_rate_hkd_e12 != receipt["fx_rate_hkd_e12"]
        or calculated.fx_source != origin["fx_source"]
        or calculated.received_hkd_micro != receipt["received_hkd_micro"]
        or calculated.actual_hkd_override != (reason is not None)
    ):
        raise ValueError("Inconsistent receipt arithmetic")


def _revision(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    number: int,
    cache: dict[int, tuple[events.ReviewedEvent, datetime]],
) -> tuple[events.ReviewedEvent, datetime]:
    if type(number) is not int or not 1 <= number <= 2_147_483_647:
        raise ValueError("Invalid receipt event revision")
    if number not in cache:
        reviewed = events.get(con, owner, event_id, version=number)
        if reviewed is None:
            raise ValueError("Missing receipt event revision")
        audits = con.execute(
            text(
                "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision' "
                "AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'"
            ),
            {"id": str(reviewed.record.revision_id)},
        ).all()
        if len(audits) != 1:
            raise ValueError("Missing receipt event audit")
        audit_time = events._time(audits[0].created_at)
        if audit_time < reviewed.record.created_at:
            raise ValueError("Receipt event audit predates its revision")
        cache[number] = reviewed, audit_time
    return cache[number]


def _origin(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    account_id: UUID,
    account_row: RowMapping,
    entitlement: retained.RetainedEntitlement,
    item: retained.RetainedReceipt,
    row: RowMapping,
    observed_cache: dict[int, tuple[events.ReviewedEvent, datetime]],
) -> ReceiptOrigin:
    identifier = events._uuid(row["receipt_id"])
    observed_id = events._uuid(row["observed_revision_id"])
    portfolio_id = events._uuid(row["portfolio_id"])
    broker_id = events._uuid(row["broker_id"])
    cash_id, audit_id, request_id = (
        events._uuid(row[name]) for name in ("cash_entry_id", "audit_id", "request_id")
    )
    created = events._time(row["created_at"])
    observed_no = _integer(row["observed_revision_no"], 1)
    if observed_no > 2_147_483_647:
        raise ValueError("Invalid observed event revision")
    account_version = positive_version(row["account_version"])
    portfolio_version = positive_version(row["portfolio_version"])
    broker_version = positive_version(row["broker_version"])
    data_revision = _integer(row["portfolio_data_revision"])
    observed, source_time = _revision(con, owner, event_id, observed_no, observed_cache)
    original, original_source_time = _revision(
        con, owner, event_id, entitlement.revision_no, observed_cache
    )
    if (
        identifier != item.id
        or row["owner_id"] != str(owner)
        or row["event_id"] != str(event_id)
        or row["account_id"] != str(account_id)
        or row["retained_portfolio_owner"] != str(owner)
        or row["retained_broker_owner"] != str(owner)
        or account_version > positive_version(account_row["account_version"])
        or portfolio_version > positive_version(row["retained_portfolio_version"])
        or broker_version > positive_version(row["retained_broker_version"])
        or data_revision > _integer(row["retained_data_revision"])
        or observed.record.revision_id != observed_id
        or original.record.revision_id != entitlement.revision_id
        or observed_no < entitlement.revision_no
        or entitlement.created_at < original.record.created_at
        or original_source_time > item.created_at
        or source_time > item.created_at
        or item.created_at < entitlement.created_at
        or created < observed.record.created_at
        or created < entitlement.created_at
        or created < item.created_at
    ):
        raise ValueError("Inconsistent receipt origin scope")
    receipt = (
        con.execute(text("SELECT * FROM dividend_receipts WHERE id=:id"), {"id": str(identifier)})
        .mappings()
        .one()
    )
    cash = (
        con.execute(text("SELECT * FROM cash_ledger_entries WHERE id=:id"), {"id": str(cash_id)})
        .mappings()
        .one()
    )
    cash_time = events._time(cash["created_at"])
    if (
        receipt["entitlement_id"] != str(entitlement.id)
        or receipt["account_id"] != str(account_id)
        or cash["source_type"] != "DIVIDEND_RECEIPT"
        or cash["source_id"] != str(identifier)
        or cash["entry_role"] != "PRIMARY"
        or cash_time < item.created_at
        or cash_time > created
        or item.cash_scope
        not in {
            "MATCHING_CASH_RECORDED_UNVERIFIED",
            "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED",
        }
        or not isinstance(receipt["note"], str)
        or "\x00" in receipt["note"]
        or len(receipt["note"]) > 1000
        or not isinstance(cash["note"], str)
        or "\x00" in cash["note"]
        or len(cash["note"]) > 1000
    ):
        raise ValueError("Inconsistent receipt cash scope")
    _booking(receipt, row)
    audits = (
        con.execute(
            text(
                "SELECT * FROM audit_events WHERE entity_type='dividend_receipt' AND entity_id=:id"
            ),
            {"id": str(identifier)},
        )
        .mappings()
        .all()
    )
    creation = [audit for audit in audits if audit["action"] == "DIVIDEND_RECEIPT_RECORDED"]
    voids = [audit for audit in audits if audit["action"] == "DIVIDEND_RECEIPT_VOIDED"]
    if len(creation) != 1 or len(audits) != len(creation) + len(voids):
        raise ValueError("Inconsistent receipt audits")
    audit = creation[0]
    reason = _reason(audit["reason"], allow_empty=True)
    payload = _creation_payload(receipt, row, cash, reason)
    creation_hash = digest(canonical(payload))
    if (
        audit["id"] != str(audit_id)
        or audit["owner_id"] != str(owner)
        or audit["request_id"] != str(request_id)
        or events._time(audit["created_at"]) != created
        or audit["before_sha256"] is not None
        or audit["after_sha256"] != creation_hash
    ):
        raise ValueError("Inconsistent receipt creation audit")
    if item.status == "ACTIVE":
        if item.version != 1 or _integer(cash["version"], 1) != 1 or voids:
            raise ValueError("Inconsistent active receipt")
    else:
        if item.version != 2 or _integer(cash["version"], 1) != 2 or len(voids) != 1:
            raise ValueError("Inconsistent void receipt")
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
        void_reason = _reason(void_audit["reason"], allow_empty=False)
        reversal_time = events._time(reversal["created_at"])
        void_time = events._time(void_audit["created_at"])
        if (
            void_audit["owner_id"] != str(owner)
            or item.voided_at != void_time
            or _integer(reversal["version"], 1) != 1
            or reversal["note"] != void_reason
            or reversal_time < created
            or reversal_time > void_time
            or void_audit["before_sha256"] != creation_hash
            or void_audit["after_sha256"]
            != digest(
                canonical(
                    _void_payload(
                        receipt, row, reversal, str(void_request), void_reason, receipt["voided_at"]
                    )
                )
            )
        ):
            raise ValueError("Inconsistent receipt void audit")
    return ReceiptOrigin(
        observed_id,
        observed_no,
        portfolio_id,
        broker_id,
        account_version,
        portfolio_version,
        broker_version,
        data_revision,
        cash_id,
        cash["note"],
        row["fx_source"],
        row["actual_hkd_reason"],
        request_id,
        audit_id,
        reason,
        created,
    )


def history(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID
) -> tuple[AuditedReceipt, ...] | None:
    """Read complete retained links in one snapshot, not HTTP authority or payment proof."""
    if not con.in_transaction() or not all(
        isinstance(value, UUID) for value in (owner, event_id, account_id)
    ):
        raise ReceiptAuditError("INVALID_DIVIDEND_RECEIPT_AUDIT_SCOPE")
    try:
        base = retained.history(con, owner, event_id, account_id)
        if base is None:
            return None
        current_account = account(con, owner, account_id)
        if current_account is None:
            raise ValueError("Inconsistent retained account")
        rows = (
            con.execute(
                text("""
            SELECT o.*,p.owner_id AS retained_portfolio_owner,
                p.version AS retained_portfolio_version,p.data_revision AS retained_data_revision,
                b.owner_id AS retained_broker_owner,b.version AS retained_broker_version
            FROM dividend_receipt_origins o
            LEFT JOIN portfolios p ON p.id=o.portfolio_id
            LEFT JOIN brokers b ON b.id=o.broker_id
            LEFT JOIN dividend_receipts r ON r.id=o.receipt_id
            LEFT JOIN dividend_entitlements entitlement ON entitlement.id=r.entitlement_id
            LEFT JOIN dividend_event_revisions revision ON revision.id=entitlement.revision_id
            WHERE (o.event_id=:event AND o.account_id=:account)
               OR (revision.event_id=:event AND
                   (r.account_id=:account OR entitlement.account_id=:account))
        """),
                {"event": str(event_id), "account": str(account_id)},
            )
            .mappings()
            .all()
        )
        origins: dict[UUID, RowMapping] = {}
        for row in rows:
            identifier = events._uuid(row["receipt_id"])
            if identifier in origins:
                raise ValueError("Duplicate receipt origin")
            origins[identifier] = row
        result: list[AuditedReceipt] = []
        observed_cache: dict[int, tuple[events.ReviewedEvent, datetime]] = {}
        for entitlement in base.entitlements:
            for item in entitlement.receipts:
                origin_row = origins.pop(item.id) if item.id in origins else None
                origin = (
                    _origin(
                        con,
                        owner,
                        event_id,
                        account_id,
                        current_account,
                        entitlement,
                        item,
                        origin_row,
                        observed_cache,
                    )
                    if origin_row is not None
                    else None
                )
                result.append(
                    AuditedReceipt(
                        item,
                        entitlement.id,
                        entitlement.revision_id,
                        origin,
                        "RECORDED_AUDIT_MATCHED_ONLY" if origin else "RECEIPT_AUDIT_NOT_VERIFIED",
                    )
                )
        if origins:
            raise ValueError("Inconsistent receipt origin linkage")
        return tuple(result)
    except (
        retained.ReceiptHistoryError,
        events.EventError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
    ):
        raise ReceiptAuditError("DIVIDEND_RECEIPT_AUDIT_UNAVAILABLE") from None
