"""Reverse a retained receipt cash entry without deleting its original financial facts."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as history
import spm_api.dividend_receipt_writer as creation
from spm_api.audit import audit, canonical, digest, now, stamp
from spm_api.db import savepoint
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account, positive_version
from spm_api.errors import ApiError, Model
from spm_api.ledger_service import rebase_cash_changes, reverse_cash
from spm_api.master_service import invalidate_portfolio


class ReceiptVoidError(ValueError):
    """Fixed codes only; do not disclose recorded cash or private intent."""


class ReceiptVoidIntent(Model):
    """Explicit recorded scope; callers must authenticate the correction separately."""

    model_config = ConfigDict(frozen=True)

    receipt_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    observed_revision_id: UUID = Field(repr=False)
    observed_revision_no: events.Version
    entitlement_id: UUID = Field(repr=False)
    original_revision_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: creation.PositiveVersion
    portfolio_version: creation.PositiveVersion
    broker_version: creation.PositiveVersion
    expected_data_revision: creation.AdvanceableRevision
    cash_entry_id: UUID = Field(repr=False)
    expected_receipt_version: creation.PositiveVersion
    reason: events.Reason = Field(repr=False)
    confirmation: Literal["VOID_RECORDED_CASH"]


def _validated(value: object, request_id: object) -> ReceiptVoidIntent:
    if not isinstance(value, ReceiptVoidIntent) or not isinstance(request_id, UUID):
        raise ReceiptVoidError("INVALID_DIVIDEND_RECEIPT_VOID_SCOPE")
    try:
        valid = ReceiptVoidIntent.model_validate(
            {name: getattr(value, name) for name in ReceiptVoidIntent.model_fields}
        )
        if valid != value:
            raise ValueError
        return valid
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ReceiptVoidError("INVALID_DIVIDEND_RECEIPT_VOID_SCOPE") from None


def _payload(intent: ReceiptVoidIntent, request_id: UUID) -> dict[str, str]:
    return {
        "receipt_id": str(intent.receipt_id),
        "event_id": str(intent.event_id),
        "account_id": str(intent.account_id),
        "request_id": str(request_id),
        "intent_sha256": digest(canonical(intent.model_dump(mode="json", warnings="error"))),
        "scope": "RECORDED_RECEIPT_VOID_INTENT_ONLY",
    }


def _scope(
    con: Connection,
    owner: UUID,
    intent: ReceiptVoidIntent,
    origin: history.ReceiptOrigin,
    *,
    invalidated: bool = False,
) -> tuple[tuple[str, int, int], ...]:
    event = events.get(con, owner, intent.event_id)
    if event is None:
        raise ReceiptVoidError("DIVIDEND_RECEIPT_NOT_FOUND")
    if (event.record.revision_id, event.record.revision_no) != (
        intent.observed_revision_id,
        intent.observed_revision_no,
    ):
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED")
    current = account(con, owner, intent.account_id)
    if current is None:
        raise ReceiptVoidError("DIVIDEND_RECEIPT_NOT_FOUND")
    portfolio_ids = {intent.portfolio_id, origin.portfolio_id}
    portfolios: dict[UUID, tuple[int, int]] = {}
    for identifier in portfolio_ids:
        row = con.execute(
            text("SELECT version,data_revision FROM portfolios WHERE id=:id AND owner_id=:owner"),
            {"id": str(identifier), "owner": str(owner)},
        ).one_or_none()
        if row is None:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        version = positive_version(row.version)
        maximum = INT64_MAX if invalidated else INT64_MAX - 1
        if type(row.data_revision) is not int or not 0 <= row.data_revision <= maximum:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        portfolios[identifier] = version, row.data_revision
    if (
        current["portfolio_id"] != str(intent.portfolio_id)
        or current["broker_id"] != str(intent.broker_id)
        or current["account_version"] != intent.account_version
        or current["broker_version"] != intent.broker_version
        or portfolios[intent.portfolio_id]
        != (intent.portfolio_version, intent.expected_data_revision + int(invalidated))
    ):
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED")
    return tuple(
        sorted(
            (str(identifier), version, revision)
            for identifier, (version, revision) in portfolios.items()
        )
    )


def _verify_void(
    con: Connection,
    owner: UUID,
    intent: ReceiptVoidIntent,
    request_id: UUID,
    result: history.AuditedReceipt,
) -> None:
    origin = result.origin
    if origin is None or result.receipt.status != "VOID" or result.receipt.voided_at is None:
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    void_rows = (
        con.execute(
            text(
                "SELECT * FROM audit_events WHERE entity_type='dividend_receipt' "
                "AND entity_id=:id AND action='DIVIDEND_RECEIPT_VOIDED'"
            ),
            {"id": str(intent.receipt_id)},
        )
        .mappings()
        .all()
    )
    intent_rows = (
        con.execute(
            text(
                "SELECT * FROM audit_events WHERE entity_type='dividend_receipt_void_intent' "
                "AND entity_id=:id"
            ),
            {"id": str(intent.receipt_id)},
        )
        .mappings()
        .all()
    )
    if len(void_rows) != 1 or len(intent_rows) != 1:
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
    void_audit, audit_intent = void_rows[0], intent_rows[0]
    events._uuid(audit_intent["id"])
    if (
        void_audit["request_id"] != str(request_id)
        or audit_intent["request_id"] != str(request_id)
        or void_audit["reason"] != intent.reason
        or audit_intent["reason"] != intent.reason
        or audit_intent["after_sha256"] != digest(canonical(_payload(intent, request_id)))
    ):
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_CONFLICT")
    if (
        audit_intent["owner_id"] != str(owner)
        or audit_intent["action"] != "DIVIDEND_RECEIPT_VOID_INTENT_RECORDED"
        or audit_intent["before_sha256"] is not None
        or events._time(audit_intent["created_at"]) != result.receipt.voided_at
        or result.receipt.id != intent.receipt_id
        or result.receipt.version != intent.expected_receipt_version + 1
        or result.entitlement_id != intent.entitlement_id
        or result.original_revision_id != intent.original_revision_id
        or origin.cash_entry_id != intent.cash_entry_id
    ):
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")


def void(
    con: Connection, owner: UUID, intent: ReceiptVoidIntent, *, request_id: UUID
) -> history.AuditedReceipt:
    """Append an exact reversal and audit atomically; authentication remains external."""
    try:
        return _void(con, owner, intent, request_id=request_id)
    except ReceiptVoidError:
        raise
    except creation.ReceiptWriteError as error:
        raise ReceiptVoidError(str(error)) from None
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError, ApiError):
        raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE") from None


def _void(
    con: Connection, owner: UUID, intent: ReceiptVoidIntent, *, request_id: UUID
) -> history.AuditedReceipt:
    creation._writer(con, owner)
    intent = _validated(intent, request_id)
    with savepoint(con):
        records = history.history(con, owner, intent.event_id, intent.account_id)
        if records is None:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_NOT_FOUND")
        found = [item for item in records if item.receipt.id == intent.receipt_id]
        if len(found) != 1:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_NOT_FOUND")
        record = found[0]
        origin = record.origin
        if origin is None or record.audit_scope != "RECORDED_AUDIT_MATCHED_ONLY":
            raise ReceiptVoidError("DIVIDEND_RECEIPT_UNVERIFIED")
        if record.receipt.status == "VOID":
            _verify_void(con, owner, intent, request_id, record)
            return record
        if record.receipt.version != intent.expected_receipt_version:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_VERSION_CHANGED")
        if (
            record.entitlement_id != intent.entitlement_id
            or record.original_revision_id != intent.original_revision_id
            or origin.cash_entry_id != intent.cash_entry_id
        ):
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED")
        if (
            con.execute(
                text(
                    "SELECT 1 FROM audit_events WHERE entity_type='dividend_receipt_void_intent' "
                    "AND (entity_id=:id OR (owner_id=:owner AND request_id=:request))"
                ),
                {"id": str(intent.receipt_id), "owner": str(owner), "request": str(request_id)},
            ).first()
            is not None
        ):
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_CONFLICT")
        portfolio_revisions = _scope(con, owner, intent, origin)
        receipt = (
            con.execute(
                text("SELECT * FROM dividend_receipts WHERE id=:id"), {"id": str(intent.receipt_id)}
            )
            .mappings()
            .one()
        )
        origin_row = (
            con.execute(
                text("SELECT * FROM dividend_receipt_origins WHERE receipt_id=:id"),
                {"id": str(intent.receipt_id)},
            )
            .mappings()
            .one()
        )
        primary = (
            con.execute(
                text("SELECT * FROM cash_ledger_entries WHERE id=:id"),
                {"id": str(origin.cash_entry_id)},
            )
            .mappings()
            .one()
        )
        reversal_id = reverse_cash(con, primary, reason=intent.reason)
        reversal = (
            con.execute(text("SELECT * FROM cash_ledger_entries WHERE id=:id"), {"id": reversal_id})
            .mappings()
            .one()
        )
        at = now()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        at = at.astimezone(UTC)
        at_text = stamp(at)
        audit(
            con,
            str(owner),
            "dividend_receipt",
            str(intent.receipt_id),
            "DIVIDEND_RECEIPT_VOIDED",
            request_id=str(request_id),
            before=history._creation_payload(receipt, origin_row, primary, origin.creation_reason),
            after=history._void_payload(
                receipt, origin_row, reversal, str(request_id), intent.reason, at_text
            ),
            reason=intent.reason,
            at=at,
        )
        updated = con.execute(
            text(
                "UPDATE dividend_receipts SET status='VOID',version=version+1,voided_at=:at "
                "WHERE id=:id AND status='ACTIVE' AND version=:version"
            ),
            {
                "id": str(intent.receipt_id),
                "version": intent.expected_receipt_version,
                "at": at_text,
            },
        )
        if updated.rowcount != 1:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_VERSION_CHANGED")
        audit(
            con,
            str(owner),
            "dividend_receipt_void_intent",
            str(intent.receipt_id),
            "DIVIDEND_RECEIPT_VOID_INTENT_RECORDED",
            request_id=str(request_id),
            after=_payload(intent, request_id),
            reason=intent.reason,
            at=at,
        )
        rebase_cash_changes(con, str(owner), [reversal_id], str(uuid4()), str(request_id))
        if _scope(con, owner, intent, origin) != portfolio_revisions:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED")
        for portfolio, _, _ in portfolio_revisions:
            invalidate_portfolio(
                con,
                portfolio,
                str(intent.receipt_id),
                "DIVIDEND_RECEIPT_VOIDED",
                record.receipt.received_date.isoformat(),
            )
        expected_revisions = tuple(
            (identifier, version, revision + 1)
            for identifier, version, revision in portfolio_revisions
        )
        if _scope(con, owner, intent, origin, invalidated=True) != expected_revisions:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED")
        records = history.history(con, owner, intent.event_id, intent.account_id)
        if records is None:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        results = [item for item in records if item.receipt.id == intent.receipt_id]
        if len(results) != 1:
            raise ReceiptVoidError("DIVIDEND_RECEIPT_VOID_UNAVAILABLE")
        _verify_void(con, owner, intent, request_id, results[0])
        return results[0]
