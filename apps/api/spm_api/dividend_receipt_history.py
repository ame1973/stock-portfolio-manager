"""Resolve retained receipt links without authenticating or granting financial use."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
from spm_api.decimal_types import INT64_MAX, INT64_MIN, from_scaled
from spm_api.dividend_account_scope import account
from spm_api.master_models import Currency


class ReceiptHistoryError(ValueError):
    """Fixed error codes only; do not expose retained amounts or owner data."""


CashScope = Literal[
    "CASH_NOT_RECORDED",
    "CASH_REVERSAL_NOT_RECORDED",
    "MATCHING_CASH_RECORDED_UNVERIFIED",
    "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED",
]


@dataclass(frozen=True, slots=True)
class RetainedReceipt:
    id: UUID = field(repr=False)
    received_date: date = field(repr=False)
    received: Decimal = field(repr=False)
    currency: Currency = field(repr=False)
    tax: Decimal | None = field(repr=False)
    fee: Decimal | None = field(repr=False)
    fx_rate_hkd: Decimal | None = field(repr=False)
    received_hkd: Decimal | None = field(repr=False)
    source: Literal["MANUAL", "IMPORT"]
    status: Literal["ACTIVE", "VOID"]
    version: int
    created_at: datetime = field(repr=False)
    voided_at: datetime | None = field(repr=False)
    cash_scope: CashScope
    audit_scope: Literal["RECEIPT_AUDIT_NOT_VERIFIED"] = "RECEIPT_AUDIT_NOT_VERIFIED"


@dataclass(frozen=True, slots=True)
class RetainedEntitlement:
    id: UUID = field(repr=False)
    revision_id: UUID = field(repr=False)
    revision_no: int
    eligible_shares: Decimal = field(repr=False)
    gross: Decimal = field(repr=False)
    tax: Decimal = field(repr=False)
    fee: Decimal = field(repr=False)
    expected_net: Decimal = field(repr=False)
    currency: Currency
    fx_rate_hkd: Decimal | None = field(repr=False)
    expected_hkd: Decimal | None = field(repr=False)
    recorded_quality: str
    tax_rate: Decimal | None = field(repr=False)
    projection_version: int
    created_at: datetime = field(repr=False)
    receipts: tuple[RetainedReceipt, ...] = field(repr=False)
    scope: Literal["LEGACY_ENTITLEMENT_NOT_VERIFIED"] = "LEGACY_ENTITLEMENT_NOT_VERIFIED"


@dataclass(frozen=True, slots=True)
class ReceiptHistory:
    event_id: UUID = field(repr=False)
    current_revision_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    entitlements: tuple[RetainedEntitlement, ...] = field(repr=False)
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    scope: Literal["UNVERIFIED_RECEIPT_HISTORY_ONLY"] = "UNVERIFIED_RECEIPT_HISTORY_ONLY"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


def _integer(value: object, minimum: int = INT64_MIN, maximum: int = INT64_MAX) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid retained integer")
    return value


def _decimal(value: object, scale: int, minimum: int = INT64_MIN) -> Decimal:
    return Decimal(from_scaled(_integer(value, minimum), scale))


def _optional_decimal(value: object, scale: int, minimum: int = INT64_MIN) -> Decimal | None:
    return None if value is None else _decimal(value, scale, minimum)


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("Invalid retained date")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("Invalid retained date")
    return result


def _currency(value: object) -> Currency:
    if value not in ("HKD", "USD"):
        raise ValueError("Invalid retained currency")
    return value


def _cash_numbers(row: RowMapping) -> None:
    _integer(row["amount_micro"])
    _integer(row["external_flow"], 0, 1)
    if row["fx_rate_hkd_e12"] is not None:
        _integer(row["fx_rate_hkd_e12"], 1)
    if row["booked_hkd_micro"] is not None:
        _integer(row["booked_hkd_micro"])


def _cash_scope(
    con: Connection,
    receipt: RowMapping,
    *,
    source_type: Literal["DIVIDEND_RECEIPT", "DIVIDEND_DIRECT_RECEIPT"] = "DIVIDEND_RECEIPT",
) -> CashScope:
    rows = (
        con.execute(
            text("""
                SELECT * FROM cash_ledger_entries
                WHERE source_type=:source_type AND source_id=:id
                ORDER BY entry_role,id
            """),
            {"id": receipt["id"], "source_type": source_type},
        )
        .mappings()
        .all()
    )
    if not rows:
        return "CASH_NOT_RECORDED"
    primaries = [row for row in rows if row["entry_role"] == "PRIMARY"]
    reversals = [row for row in rows if row["entry_role"] == "REVERSAL"]
    if len(primaries) != 1 or len(reversals) > 1 or len(rows) != len(primaries) + len(reversals):
        raise ValueError("Inconsistent retained cash links")
    primary = primaries[0]
    primary_id = events._uuid(primary["id"])
    events._time(primary["created_at"])
    _cash_numbers(primary)
    linked = (
        con.execute(
            text("SELECT * FROM cash_ledger_entries WHERE reverses_entry_id=:id ORDER BY id"),
            {"id": str(primary_id)},
        )
        .mappings()
        .all()
    )
    if len(linked) != len(reversals) or {row["id"] for row in linked} != {
        row["id"] for row in reversals
    }:
        raise ValueError("Inconsistent retained reversal links")
    if (
        primary["source_type"] != source_type
        or primary["source_id"] != receipt["id"]
        or primary["account_id"] != receipt["account_id"]
        or primary["effective_date"] != receipt["received_date"]
        or primary["currency"] != receipt["currency"]
        or primary["amount_micro"] != receipt["received_micro"]
        or primary["fx_rate_hkd_e12"] != receipt["fx_rate_hkd_e12"]
        or primary["booked_hkd_micro"] != receipt["received_hkd_micro"]
        or primary["kind"] != "DIVIDEND_RECEIPT"
        or primary["external_flow"] != 0
        or primary["reverses_entry_id"] is not None
    ):
        raise ValueError("Inconsistent retained cash entry")
    if receipt["status"] == "ACTIVE":
        if reversals:
            raise ValueError("Active receipt has a reversal")
        return "MATCHING_CASH_RECORDED_UNVERIFIED"
    if not reversals:
        return "CASH_REVERSAL_NOT_RECORDED"
    reversal = reversals[0]
    events._uuid(reversal["id"])
    events._time(reversal["created_at"])
    _cash_numbers(reversal)
    booked = receipt["received_hkd_micro"]
    if (
        reversal["reverses_entry_id"] != str(primary_id)
        or reversal["source_type"] != source_type
        or reversal["source_id"] != receipt["id"]
        or reversal["account_id"] != primary["account_id"]
        or reversal["effective_date"] != primary["effective_date"]
        or reversal["currency"] != primary["currency"]
        or reversal["amount_micro"] != -receipt["received_micro"]
        or reversal["fx_rate_hkd_e12"] != primary["fx_rate_hkd_e12"]
        or reversal["booked_hkd_micro"] != (-booked if booked is not None else None)
        or reversal["kind"] != "REVERSAL"
        or reversal["external_flow"] != 0
    ):
        raise ValueError("Inconsistent retained cash reversal")
    return "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"


def _receipt(
    con: Connection,
    row: RowMapping,
    account_id: UUID,
    *,
    source_type: Literal["DIVIDEND_RECEIPT", "DIVIDEND_DIRECT_RECEIPT"] = "DIVIDEND_RECEIPT",
) -> RetainedReceipt:
    identifier = events._uuid(row["id"])
    if row["account_id"] != str(account_id) or row["source"] not in ("MANUAL", "IMPORT"):
        raise ValueError("Invalid retained receipt scope")
    status = row["status"]
    if status not in ("ACTIVE", "VOID"):
        raise ValueError("Invalid retained receipt state")
    version = _integer(row["version"], 1)
    created_at = events._time(row["created_at"])
    voided_at = events._time(row["voided_at"]) if row["voided_at"] is not None else None
    if (status == "ACTIVE" and voided_at is not None) or (
        status == "VOID" and (voided_at is None or voided_at < created_at or version < 2)
    ):
        raise ValueError("Invalid retained receipt lifecycle")
    return RetainedReceipt(
        id=identifier,
        received_date=_day(row["received_date"]),
        received=_decimal(row["received_micro"], 6),
        currency=_currency(row["currency"]),
        tax=_optional_decimal(row["tax_micro"], 6, 0),
        fee=_optional_decimal(row["fee_micro"], 6, 0),
        fx_rate_hkd=_optional_decimal(row["fx_rate_hkd_e12"], 12, 1),
        received_hkd=_optional_decimal(row["received_hkd_micro"], 6),
        source=cast(Literal["MANUAL", "IMPORT"], row["source"]),
        status=cast(Literal["ACTIVE", "VOID"], status),
        version=version,
        created_at=created_at,
        voided_at=voided_at,
        cash_scope=_cash_scope(con, row, source_type=source_type),
    )


def history(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID
) -> ReceiptHistory | None:
    """Resolve every linked physical entitlement in one snapshot, without granting use.

    A matching cash row is structural evidence only. Legacy receipt and entitlement
    audit content, external ownership and the completeness of the input are not verified.
    """
    if (
        not con.in_transaction()
        or not isinstance(owner, UUID)
        or not isinstance(event_id, UUID)
        or not isinstance(account_id, UUID)
    ):
        raise ReceiptHistoryError("INVALID_DIVIDEND_RECEIPT_SCOPE")
    try:
        current = events.get(con, owner, event_id)
        if current is None or account(con, owner, account_id) is None:
            return None
        # Do not return a partial history before the direct-receipt reader is installed.
        direct = con.execute(
            text("""
                SELECT 1 FROM dividend_direct_receipts
                WHERE event_id=:event AND account_id=:account
                UNION ALL
                SELECT 1 FROM dividend_direct_receipt_origins
                WHERE event_id=:event AND account_id=:account LIMIT 1
            """),
            {"event": str(event_id), "account": str(account_id)},
        ).first()
        if direct is not None:
            raise ValueError("Direct receipt history is not yet available")
        mismatch = con.execute(
            text("""
                SELECT 1 FROM dividend_receipts receipt
                JOIN dividend_entitlements ent ON ent.id=receipt.entitlement_id
                JOIN dividend_event_revisions revision ON revision.id=ent.revision_id
                WHERE revision.event_id=:event
                  AND (receipt.account_id=:account OR ent.account_id=:account)
                  AND receipt.account_id IS NOT ent.account_id LIMIT 1
            """),
            {"event": str(event_id), "account": str(account_id)},
        ).first()
        if mismatch is not None:
            raise ValueError("Inconsistent historical account link")
        rows = (
            con.execute(
                text("""
                    SELECT ent.*,revision.revision_no FROM dividend_entitlements ent
                    JOIN dividend_event_revisions revision ON revision.id=ent.revision_id
                    WHERE revision.event_id=:event AND ent.account_id=:account
                    ORDER BY revision.revision_no,ent.id
                """),
                {"event": str(event_id), "account": str(account_id)},
            )
            .mappings()
            .all()
        )
        retained: list[RetainedEntitlement] = []
        for row in rows:
            revision_no = _integer(row["revision_no"], 1, 2_147_483_647)
            revision_id, identifier = events._uuid(row["revision_id"]), events._uuid(row["id"])
            revision = events.get(con, owner, event_id, version=revision_no)
            if revision is None or revision.record.revision_id != revision_id:
                raise ValueError("Inconsistent historical event revision")
            currency = _currency(row["currency"])
            if revision.record.terms.currency != currency or row["quality"] not in (
                "ESTIMATED",
                "ACCRUED",
                "NEEDS_REVIEW",
                "CONFIRMED",
                "MISSING_FX",
            ):
                raise ValueError("Invalid retained entitlement")
            receipt_rows = (
                con.execute(
                    text("""
                        SELECT * FROM dividend_receipts WHERE entitlement_id=:id
                        ORDER BY received_date,created_at,id
                    """),
                    {"id": str(identifier)},
                )
                .mappings()
                .all()
            )
            tax_rate = _optional_decimal(row["tax_rate_snapshot_ppm"], 6, 0)
            if tax_rate is not None and tax_rate > 1:
                raise ValueError("Invalid retained tax rate")
            retained.append(
                RetainedEntitlement(
                    id=identifier,
                    revision_id=revision_id,
                    revision_no=revision_no,
                    eligible_shares=_decimal(row["eligible_shares_e8"], 8, 1),
                    gross=_decimal(row["gross_micro"], 6, 0),
                    tax=_decimal(row["tax_micro"], 6, 0),
                    fee=_decimal(row["fee_micro"], 6, 0),
                    expected_net=_decimal(row["expected_net_micro"], 6),
                    currency=currency,
                    fx_rate_hkd=_optional_decimal(row["fx_rate_hkd_e12"], 12, 1),
                    expected_hkd=_optional_decimal(row["expected_hkd_micro"], 6),
                    recorded_quality=cast(str, row["quality"]),
                    tax_rate=tax_rate,
                    projection_version=_integer(row["projection_version"], 0),
                    created_at=events._time(row["created_at"]),
                    receipts=tuple(_receipt(con, receipt, account_id) for receipt in receipt_rows),
                )
            )
        return ReceiptHistory(event_id, current.record.revision_id, account_id, tuple(retained))
    except (events.EventError, ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise ReceiptHistoryError("DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE") from None
