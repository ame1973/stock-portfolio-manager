"""Append explicitly recorded receipt cash, without authenticating or granting eligibility."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_receipt_audit as history
from spm_api.audit import audit, canonical, digest, now, stamp
from spm_api.db import savepoint
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.dividend_receipt_terms import (
    ReceiptComponents,
    ReceiptTerms,
    calculate_components,
    calculate_receipt,
)
from spm_api.errors import ApiError, Model
from spm_api.finance import FinanceError
from spm_api.ledger_service import post_cash, rebase_cash_changes
from spm_api.master_service import invalidate_portfolio

PositiveVersion = Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
AdvanceableRevision = Annotated[int, Field(strict=True, ge=0, lt=INT64_MAX)]
ReceiptNote = Annotated[str, Field(strict=True, max_length=1000, pattern=r"^[^\x00]*$")]


class ReceiptWriteError(ValueError):
    """Fixed codes only; never disclose private receipt values or intent."""


class ReceiptIntent(Model):
    """Explicit scope and recorded cash; callers must authenticate separately."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    entitlement_id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: PositiveVersion
    portfolio_version: PositiveVersion
    broker_version: PositiveVersion
    expected_data_revision: AdvanceableRevision
    received_date: IsoDate = Field(repr=False)
    components: ReceiptComponents = Field(repr=False)
    note: ReceiptNote = Field(repr=False)
    reason: events.Reason = Field(repr=False)
    confirmation: Literal["RECORDED_CASH_ONLY"]


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise ReceiptWriteError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise ReceiptWriteError("INVALID_DIVIDEND_RECEIPT_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
        is None
    ):
        raise ReceiptWriteError("DIVIDEND_RECEIPT_OWNER_UNAVAILABLE")


def _validated(value: object, request_id: object) -> ReceiptIntent:
    if not isinstance(value, ReceiptIntent) or not isinstance(request_id, UUID):
        raise ReceiptWriteError("INVALID_DIVIDEND_RECEIPT_SCOPE")
    try:
        fields = {name: getattr(value, name) for name in ReceiptIntent.model_fields}
        components = value.components
        if not isinstance(components, ReceiptComponents):
            raise ValueError
        fields["components"] = {
            name: getattr(components, name) for name in ReceiptComponents.model_fields
        }
        valid = ReceiptIntent.model_validate(fields)
        if valid != value:
            raise ValueError
        return valid
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ReceiptWriteError("INVALID_DIVIDEND_RECEIPT_SCOPE") from None


def _intent_payload(intent: ReceiptIntent, request_id: UUID) -> dict[str, str]:
    return {
        "receipt_id": str(intent.id),
        "event_id": str(intent.event_id),
        "account_id": str(intent.account_id),
        "request_id": str(request_id),
        "intent_sha256": digest(canonical(intent.model_dump(mode="json", warnings="error"))),
        "scope": "RECORDED_RECEIPT_WRITE_INTENT_ONLY",
    }


def _verify_intent(
    con: Connection,
    owner: UUID,
    intent: ReceiptIntent,
    request_id: UUID,
    result: history.AuditedReceipt,
) -> None:
    origin = result.origin
    if origin is None or result.audit_scope != "RECORDED_AUDIT_MATCHED_ONLY":
        raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    rows = (
        con.execute(
            text(
                "SELECT * FROM audit_events WHERE entity_type='dividend_receipt_intent' "
                "AND entity_id=:id"
            ),
            {"id": str(intent.id)},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    row = rows[0]
    events._uuid(row["id"])
    if (
        row["owner_id"] != str(owner)
        or row["action"] != "DIVIDEND_RECEIPT_INTENT_RECORDED"
        or row["request_id"] != str(request_id)
        or row["reason"] != intent.reason
        or events._time(row["created_at"]) != origin.created_at
        or row["before_sha256"] is not None
        or row["after_sha256"] != digest(canonical(_intent_payload(intent, request_id)))
    ):
        raise ReceiptWriteError("DIVIDEND_RECEIPT_CONFLICT")
    receipt = (
        con.execute(text("SELECT * FROM dividend_receipts WHERE id=:id"), {"id": str(intent.id)})
        .mappings()
        .one()
    )
    booking = calculate_components(intent.components)
    if (
        result.receipt.id != intent.id
        or result.entitlement_id != intent.entitlement_id
        or receipt["account_id"] != str(intent.account_id)
        or receipt["received_date"] != intent.received_date
        or receipt["currency"] != intent.components.currency
        or receipt["received_micro"] != booking.received_micro
        or receipt["tax_micro"] != booking.tax_micro
        or receipt["fee_micro"] != booking.fee_micro
        or receipt["fx_rate_hkd_e12"] != booking.fx_rate_hkd_e12
        or receipt["received_hkd_micro"] != booking.received_hkd_micro
        or receipt["note"] != intent.note
        or receipt["source"] != "MANUAL"
        or origin.observed_revision_id != intent.revision_id
        or origin.observed_revision_no != intent.revision_no
        or origin.portfolio_id != intent.portfolio_id
        or origin.broker_id != intent.broker_id
        or origin.account_version != intent.account_version
        or origin.portfolio_version != intent.portfolio_version
        or origin.broker_version != intent.broker_version
        or origin.portfolio_data_revision != intent.expected_data_revision
        or origin.cash_note != intent.note
        or origin.fx_source != booking.fx_source
        or origin.actual_hkd_reason != intent.components.actual_hkd_reason
        or origin.creation_reason != intent.reason
        or origin.request_id != request_id
    ):
        raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")


def _current_scope(
    con: Connection, owner: UUID, intent: ReceiptIntent, *, invalidated: bool = False
) -> None:
    event = events.get(con, owner, intent.event_id)
    if event is None:
        raise ReceiptWriteError("DIVIDEND_RECEIPT_NOT_FOUND")
    if (event.record.revision_id, event.record.revision_no) != (
        intent.revision_id,
        intent.revision_no,
    ):
        raise ReceiptWriteError("DIVIDEND_RECEIPT_SCOPE_CHANGED")
    account = (
        con.execute(
            text("""
                SELECT a.portfolio_id,a.broker_id,a.version AS account_version,a.status,
                       p.version AS portfolio_version,p.data_revision,b.version AS broker_version
                FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
                JOIN brokers b ON b.id=a.broker_id
                WHERE a.id=:account AND p.owner_id=:owner AND b.owner_id=:owner
            """),
            {"account": str(intent.account_id), "owner": str(owner)},
        )
        .mappings()
        .first()
    )
    if account is None:
        raise ReceiptWriteError("DIVIDEND_RECEIPT_NOT_FOUND")
    for name in ("account_version", "portfolio_version", "broker_version"):
        if type(account[name]) is not int or not 1 <= account[name] <= INT64_MAX:
            raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    if type(account["data_revision"]) is not int or not 0 <= account["data_revision"] <= INT64_MAX:
        raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
    if account["status"] != "ACTIVE":
        raise ReceiptWriteError("DIVIDEND_RECEIPT_ACCOUNT_ARCHIVED")
    if (
        account["portfolio_id"] != str(intent.portfolio_id)
        or account["broker_id"] != str(intent.broker_id)
        or account["account_version"] != intent.account_version
        or account["portfolio_version"] != intent.portfolio_version
        or account["broker_version"] != intent.broker_version
        or account["data_revision"] != intent.expected_data_revision + int(invalidated)
    ):
        raise ReceiptWriteError("DIVIDEND_RECEIPT_SCOPE_CHANGED")
    entitlement = con.execute(
        text("""
            SELECT 1 FROM dividend_entitlements ent
            JOIN dividend_event_revisions revision ON revision.id=ent.revision_id
            WHERE ent.id=:entitlement AND ent.account_id=:account AND revision.event_id=:event
        """),
        {
            "entitlement": str(intent.entitlement_id),
            "account": str(intent.account_id),
            "event": str(intent.event_id),
        },
    ).first()
    if entitlement is None:
        raise ReceiptWriteError("DIVIDEND_RECEIPT_NOT_FOUND")


def save(
    con: Connection, owner: UUID, intent: ReceiptIntent, *, request_id: UUID
) -> history.AuditedReceipt:
    """Record one cash fact and provenance atomically; HTTP authorization remains external."""
    try:
        return _save(con, owner, intent, request_id=request_id)
    except ReceiptWriteError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError, ApiError):
        raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE") from None


def _save(
    con: Connection, owner: UUID, intent: ReceiptIntent, *, request_id: UUID
) -> history.AuditedReceipt:
    _writer(con, owner)
    intent = _validated(intent, request_id)
    with savepoint(con):
        previous = history.history(con, owner, intent.event_id, intent.account_id)
        if previous is None:
            raise ReceiptWriteError("DIVIDEND_RECEIPT_NOT_FOUND")
        for item in previous:
            if item.receipt.id == intent.id:
                if item.origin is None or item.origin.request_id != request_id:
                    raise ReceiptWriteError("DIVIDEND_RECEIPT_CONFLICT")
                _verify_intent(con, owner, intent, request_id, item)
                return item
        if (
            con.execute(
                text("SELECT 1 FROM dividend_receipts WHERE id=:id"), {"id": str(intent.id)}
            ).first()
            is not None
            or con.execute(
                text(
                    "SELECT 1 FROM dividend_receipt_origins "
                    "WHERE owner_id=:owner AND request_id=:id"
                ),
                {"owner": str(owner), "id": str(request_id)},
            ).first()
            is not None
        ):
            raise ReceiptWriteError("DIVIDEND_RECEIPT_CONFLICT")
        if (
            con.execute(
                text(
                    "SELECT 1 FROM audit_events WHERE entity_type IN "
                    "('dividend_receipt','dividend_receipt_intent') AND entity_id=:id"
                ),
                {"id": str(intent.id)},
            ).first()
            is not None
        ):
            raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        _current_scope(con, owner, intent)
        try:
            terms = ReceiptTerms.model_validate(
                {
                    **{
                        name: getattr(intent.components, name)
                        for name in ReceiptComponents.model_fields
                    },
                    "received_date": intent.received_date,
                }
            )
        except ValidationError:
            raise ReceiptWriteError("DIVIDEND_RECEIPT_DATE_INVALID") from None
        try:
            booking = calculate_receipt(terms)
        except FinanceError:
            raise ReceiptWriteError("INVALID_DIVIDEND_RECEIPT_TERMS") from None
        receipt: dict[str, Any] = {
            "id": str(intent.id),
            "entitlement_id": str(intent.entitlement_id),
            "account_id": str(intent.account_id),
            "received_date": intent.received_date,
            "received_micro": booking.received_micro,
            "currency": intent.components.currency,
            "tax_micro": booking.tax_micro,
            "fee_micro": booking.fee_micro,
            "fx_rate_hkd_e12": booking.fx_rate_hkd_e12,
            "received_hkd_micro": booking.received_hkd_micro,
            "note": intent.note,
            "source": "MANUAL",
            "created_at": stamp(),
        }
        columns = ",".join(receipt)
        values = ",".join(":" + name for name in receipt)
        con.execute(text(f"INSERT INTO dividend_receipts({columns}) VALUES ({values})"), receipt)
        cash_id = post_cash(
            con,
            account=str(intent.account_id),
            day=intent.received_date,
            currency=intent.components.currency,
            amount=booking.received_micro,
            fx=booking.fx_rate_hkd_e12,
            booked=booking.received_hkd_micro,
            kind="DIVIDEND_RECEIPT",
            source_type="DIVIDEND_RECEIPT",
            source_id=str(intent.id),
            note=intent.note,
        )
        cash = (
            con.execute(text("SELECT * FROM cash_ledger_entries WHERE id=:id"), {"id": cash_id})
            .mappings()
            .one()
        )
        at = now()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        at = at.astimezone(UTC)
        origin: dict[str, Any] = {
            "receipt_id": str(intent.id),
            "owner_id": str(owner),
            "event_id": str(intent.event_id),
            "observed_revision_id": str(intent.revision_id),
            "observed_revision_no": intent.revision_no,
            "account_id": str(intent.account_id),
            "portfolio_id": str(intent.portfolio_id),
            "broker_id": str(intent.broker_id),
            "account_version": intent.account_version,
            "portfolio_version": intent.portfolio_version,
            "broker_version": intent.broker_version,
            "portfolio_data_revision": intent.expected_data_revision,
            "cash_entry_id": cash_id,
            "fx_source": booking.fx_source,
            "actual_hkd_reason": intent.components.actual_hkd_reason,
            "request_id": str(request_id),
            "created_at": stamp(at),
        }
        audit(
            con,
            str(owner),
            "dividend_receipt",
            str(intent.id),
            "DIVIDEND_RECEIPT_RECORDED",
            request_id=str(request_id),
            after=history._creation_payload(receipt, origin, cash, intent.reason),
            reason=intent.reason,
            at=at,
        )
        origin["audit_id"] = con.execute(
            text(
                "SELECT id FROM audit_events WHERE entity_type='dividend_receipt' AND entity_id=:id"
            ),
            {"id": str(intent.id)},
        ).scalar_one()
        columns = ",".join(origin)
        values = ",".join(":" + name for name in origin)
        con.execute(
            text(f"INSERT INTO dividend_receipt_origins({columns}) VALUES ({values})"), origin
        )
        audit(
            con,
            str(owner),
            "dividend_receipt_intent",
            str(intent.id),
            "DIVIDEND_RECEIPT_INTENT_RECORDED",
            request_id=str(request_id),
            after=_intent_payload(intent, request_id),
            reason=intent.reason,
            at=at,
        )
        rebase_cash_changes(con, str(owner), [cash_id], str(intent.id), str(request_id))
        _current_scope(con, owner, intent)
        invalidate_portfolio(
            con,
            str(intent.portfolio_id),
            str(intent.id),
            "DIVIDEND_RECEIPT_RECORDED",
            intent.received_date,
        )
        _current_scope(con, owner, intent, invalidated=True)
        updated = history.history(con, owner, intent.event_id, intent.account_id)
        if updated is None or len(updated) != len(previous) + 1:
            raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        matches = [item for item in updated if item.receipt.id == intent.id]
        if len(matches) != 1:
            raise ReceiptWriteError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE")
        _verify_intent(con, owner, intent, request_id, matches[0])
        return matches[0]
