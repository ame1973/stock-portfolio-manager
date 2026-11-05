"""Atomic paired cash and cost-preserving security transfers with immutable revision history."""

from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, now, stamp
from spm_api.cash_models import BookingInput
from spm_api.cash_service import finish_financial_change, native_booking
from spm_api.decimal_types import from_scaled, to_scaled
from spm_api.errors import ApiError, Success
from spm_api.finance import Position, conversion_rate, transfer
from spm_api.ledger_service import (
    allocate_sequence,
    history_events,
    money_integer,
    post_cash,
    reverse_cash,
    validate_history,
)
from spm_api.master_service import owned_account, owned_portfolio, security_row
from spm_api.mutations import check_version
from spm_api.replay import Event, Transfer, replay_before
from spm_api.security import Auth
from spm_api.transfer_models import (
    CashTransfer,
    CashTransferCreate,
    SecurityTransfer,
    SecurityTransferCreate,
    SecurityTransferRevision,
    TransferAccounts,
)

CASH_SQL = """
    SELECT t.* FROM cash_transfers t JOIN portfolios p ON p.id=t.portfolio_id
    WHERE p.owner_id=:owner
"""
SECURITY_SQL = """
    SELECT t.* FROM security_transfers t JOIN portfolios p ON p.id=t.portfolio_id
    WHERE p.owner_id=:owner
"""


def owned_transfer(
    con: Connection, owner: str, identifier: str, kind: Literal["cash", "security"]
) -> RowMapping:
    sql = CASH_SQL if kind == "cash" else SECURITY_SQL
    row = (
        con.execute(text(sql + " AND t.id=:id"), {"owner": owner, "id": identifier})
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Transfer not found")
    return row


def as_cash(row: RowMapping) -> CashTransfer:
    values = {key: row[key] for key in CashTransfer.model_fields if key in row}
    for field, column, scale in (
        ("source_amount", "source_amount_micro", 6),
        ("target_amount", "target_amount_micro", 6),
        ("fx_rate_hkd", "fx_rate_hkd_e12", 12),
        ("actual_hkd", "actual_hkd_micro", 6),
        ("source_booked_hkd", "source_booked_hkd_micro", 6),
        ("target_booked_hkd", "target_booked_hkd_micro", 6),
    ):
        values[field] = from_scaled(row[column], scale) if row[column] is not None else None
    delta = (
        row["target_booked_hkd_micro"] - row["source_booked_hkd_micro"]
        if row["source_booked_hkd_micro"] is not None and row["target_booked_hkd_micro"] is not None
        else None
    )
    values["booking_delta_hkd"] = from_scaled(delta, 6) if delta is not None else None
    values["quality"] = "MISSING_FX" if delta is None else "NEEDS_REVIEW" if delta else "OK"
    return CashTransfer.model_validate(values)


def as_security(row: RowMapping) -> SecurityTransfer:
    values = {key: row[key] for key in SecurityTransfer.model_fields if key in row}
    values["quantity"] = from_scaled(row["quantity_e8"], 8)
    values["cost_moved_hkd"] = from_scaled(row["transferred_cost_hkd_micro"], 6)
    values["is_current"] = bool(row["is_current"])
    values["status"] = row["status"] if row["is_current"] else "SUPERSEDED"
    return SecurityTransfer.model_validate(values)


def _accounts(con: Connection, auth: Auth, body: TransferAccounts) -> None:
    owner = str(auth.owner.id)
    portfolio = str(body.portfolio_id)
    owned_portfolio(con, owner, portfolio)
    for identifier in (body.source_account_id, body.target_account_id):
        account = owned_account(con, owner, str(identifier))
        if account["portfolio_id"] != portfolio:
            raise ApiError(
                409,
                "PORTFOLIO_MISMATCH",
                "Both transfer accounts must belong to the selected portfolio",
            )
        if account["status"] != "ACTIVE":
            raise ApiError(409, "ARCHIVED", "Reopen both accounts before recording a transfer")


def _insert(
    con: Connection, table: Literal["cash_transfers", "security_transfers"], values: dict[str, Any]
) -> None:
    # Identifiers come from fixed service mappings, never request-supplied SQL names.
    con.execute(
        text(
            f"INSERT INTO {table}({','.join(values)}) "
            f"VALUES ({','.join(':' + key for key in values)})"
        ),
        values,
    )


def _cash_values(con: Connection, auth: Auth, body: CashTransferCreate) -> dict[str, Any]:
    _accounts(con, auth, body)
    if body.source_currency == body.target_currency == "HKD":
        conversion_rate("HKD", Decimal(body.fx_rate_hkd) if body.fx_rate_hkd is not None else None)
    bookings = []
    for account, currency, amount in (
        (body.source_account_id, body.source_currency, body.source_amount),
        (body.target_account_id, body.target_currency, body.target_amount),
    ):
        bookings.append(
            native_booking(
                BookingInput(
                    account_id=account,
                    currency=currency,
                    amount=amount,
                    fx_rate_hkd=body.fx_rate_hkd if currency == "USD" else None,
                    actual_hkd=body.actual_hkd if currency == "USD" else None,
                    reason=body.reason,
                    note=body.note,
                )
            )
        )
    source, target = bookings
    common_fx = source[1] if body.source_currency == "USD" else target[1]
    return {
        "portfolio_id": str(body.portfolio_id),
        "source_account_id": str(body.source_account_id),
        "target_account_id": str(body.target_account_id),
        "transfer_date": body.transfer_date,
        "source_currency": body.source_currency,
        "target_currency": body.target_currency,
        "source_amount_micro": source[0],
        "target_amount_micro": target[0],
        "fx_rate_hkd_e12": common_fx,
        "source_booked_hkd_micro": source[2],
        "target_booked_hkd_micro": target[2],
        "actual_hkd_micro": to_scaled(body.actual_hkd, 6) if body.actual_hkd is not None else None,
        "reason": body.reason,
        "note": body.note,
    }


def _cash_legs(con: Connection, row: RowMapping) -> list[str]:
    entries = []
    for leg, suffix, sign in (("source", "OUT", -1), ("target", "IN", 1)):
        entries.append(
            post_cash(
                con,
                account=row[f"{leg}_account_id"],
                day=row["transfer_date"],
                currency=row[f"{leg}_currency"],
                amount=sign * row[f"{leg}_amount_micro"],
                fx=10**12 if row[f"{leg}_currency"] == "HKD" else row["fx_rate_hkd_e12"],
                booked=sign * row[f"{leg}_booked_hkd_micro"]
                if row[f"{leg}_booked_hkd_micro"] is not None
                else None,
                kind=f"TRANSFER_{suffix}",
                source_type=f"CASH_TRANSFER_{suffix}",
                source_id=row["id"],
                note=row["note"],
            )
        )
    return entries


def _reverse_legs(con: Connection, row: RowMapping, reason: str) -> list[str]:
    entries = (
        con.execute(
            text("""
        SELECT * FROM cash_ledger_entries WHERE source_id=:id AND entry_role='PRIMARY'
        AND source_type IN ('CASH_TRANSFER_OUT','CASH_TRANSFER_IN')
    """),
            {"id": row["id"]},
        )
        .mappings()
        .all()
    )
    if len(entries) != 2 or {entry["source_type"] for entry in entries} != {
        "CASH_TRANSFER_OUT",
        "CASH_TRANSFER_IN",
    }:
        raise ApiError(409, "NEEDS_REVIEW", "The transfer has inconsistent paired cash provenance")
    return [reverse_cash(con, entry, reason=reason) for entry in entries]


def _retire_cash(con: Connection, row: RowMapping, status: str) -> None:
    if row["status"] != "ACTIVE":
        raise ApiError(409, "INACTIVE_FACT", "Use the current active cash transfer")
    result = con.execute(
        text("""
        UPDATE cash_transfers SET status=:status,version=version+1,updated_at=:now
        WHERE id=:id AND version=:version
    """),
        {"status": status, "now": stamp(), "id": row["id"], "version": row["version"]},
    )
    if result.rowcount != 1:
        raise ApiError(412, "PRECONDITION_FAILED", "This transfer changed; reload before editing")


def create_cash(
    con: Connection, auth: Auth, body: CashTransferCreate, request_id: str
) -> CashTransfer:
    values = _cash_values(con, auth, body)
    identifier = str(uuid4())
    values.update(id=identifier, root_id=identifier, created_at=stamp(), updated_at=stamp())
    _insert(con, "cash_transfers", values)
    row = owned_transfer(con, str(auth.owner.id), identifier, "cash")
    entries = _cash_legs(con, row)
    finish_financial_change(
        con,
        auth,
        [row],
        entries,
        identifier,
        "CASH_TRANSFER_CHANGED",
        body.transfer_date,
        request_id,
    )
    result = as_cash(row)
    audit(
        con,
        str(auth.owner.id),
        "cash_transfer",
        identifier,
        "CREATE",
        request_id=request_id,
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def revise_cash(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: CashTransferCreate,
    request_id: str,
) -> CashTransfer:
    before = owned_transfer(con, str(auth.owner.id), identifier, "cash")
    check_version(before["version"], version)
    values = _cash_values(con, auth, body)
    _retire_cash(con, before, "SUPERSEDED")
    entries = _reverse_legs(con, before, body.reason)
    revised = str(uuid4())
    values.update(
        id=revised,
        root_id=before["root_id"],
        supersedes_id=identifier,
        revision=before["revision"] + 1,
        created_at=stamp(),
        updated_at=stamp(),
    )
    _insert(con, "cash_transfers", values)
    row = owned_transfer(con, str(auth.owner.id), revised, "cash")
    entries.extend(_cash_legs(con, row))
    finish_financial_change(
        con,
        auth,
        [before, row],
        entries,
        revised,
        "CASH_TRANSFER_CHANGED",
        min(before["transfer_date"], body.transfer_date),
        request_id,
    )
    result = as_cash(row)
    audit(
        con,
        str(auth.owner.id),
        "cash_transfer",
        revised,
        "REVISE",
        request_id=request_id,
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def void_cash(
    con: Connection, auth: Auth, identifier: str, version: int, reason: str, request_id: str
) -> Success:
    before = owned_transfer(con, str(auth.owner.id), identifier, "cash")
    check_version(before["version"], version)
    _retire_cash(con, before, "VOID")
    entries = _reverse_legs(con, before, reason)
    finish_financial_change(
        con,
        auth,
        [before],
        entries,
        identifier,
        "CASH_TRANSFER_CHANGED",
        before["transfer_date"],
        request_id,
    )
    audit(
        con,
        str(auth.owner.id),
        "cash_transfer",
        identifier,
        "VOID",
        request_id=request_id,
        before=dict(before),
        after={"status": "VOID", "version": version + 1},
        reason=reason,
    )
    return Success(message="Cash transfer voided; both exact reversals retained")


def _security_values(
    con: Connection,
    auth: Auth,
    body: SecurityTransferCreate,
    identifier: str,
    sequence: int,
) -> dict[str, Any]:
    _accounts(con, auth, body)
    security = security_row(con, str(body.security_id))
    timezone = con.execute(
        text("SELECT timezone FROM exchanges WHERE code=:code"), {"code": security["exchange_code"]}
    ).scalar_one()
    if body.transfer_date > now().astimezone(ZoneInfo(timezone)).date().isoformat():
        raise ApiError(
            422, "FUTURE_TRANSFER", "A security transfer records actual non-future movement"
        )
    event = Event(
        f"transfer:{identifier}",
        date.fromisoformat(body.transfer_date),
        sequence,
        Transfer(
            str(body.source_account_id),
            str(body.target_account_id),
            str(body.security_id),
            Decimal(body.quantity),
        ),
    )
    events, _ = history_events(con, str(body.portfolio_id))
    before = replay_before(events, event)
    moved = transfer(
        before.get((str(body.source_account_id), str(body.security_id)), Position()),
        before.get((str(body.target_account_id), str(body.security_id)), Position()),
        Decimal(body.quantity),
    ).moved_cost_hkd
    return {
        "portfolio_id": str(body.portfolio_id),
        "source_account_id": str(body.source_account_id),
        "target_account_id": str(body.target_account_id),
        "security_id": str(body.security_id),
        "transfer_date": body.transfer_date,
        "quantity_e8": to_scaled(body.quantity, 8),
        "transferred_cost_hkd_micro": money_integer(moved),
        "note": body.note,
    }


def _retire_security(
    con: Connection, row: RowMapping, status: Literal["SUPERSEDED", "VOID"]
) -> None:
    if row["status"] != "ACTIVE" or not row["is_current"]:
        raise ApiError(409, "INACTIVE_FACT", "Use the current active security transfer")
    result = con.execute(
        text("""
        UPDATE security_transfers SET status=:status,is_current=:current,version=version+1
        WHERE id=:id AND version=:version
    """),
        {
            "status": "ACTIVE" if status == "SUPERSEDED" else "VOID",
            "current": int(status != "SUPERSEDED"),
            "id": row["id"],
            "version": row["version"],
        },
    )
    if result.rowcount != 1:
        raise ApiError(412, "PRECONDITION_FAILED", "This transfer changed; reload before editing")


def _finish_security(con: Connection, auth: Auth, rows: list[RowMapping], request_id: str) -> None:
    for portfolio in {row["portfolio_id"] for row in rows}:
        validate_history(con, portfolio)
    finish_financial_change(
        con,
        auth,
        rows,
        [],
        rows[-1]["id"],
        "SECURITY_TRANSFER_CHANGED",
        min(row["transfer_date"] for row in rows),
        request_id,
    )


def create_security(
    con: Connection,
    auth: Auth,
    body: SecurityTransferCreate,
    request_id: str,
) -> SecurityTransfer:
    identifier = str(uuid4())
    sequence = allocate_sequence(con, "TRANSFER", identifier)
    values = _security_values(con, auth, body, identifier, sequence)
    values.update(id=identifier, root_id=identifier, created_sequence=sequence, created_at=stamp())
    _insert(con, "security_transfers", values)
    row = owned_transfer(con, str(auth.owner.id), identifier, "security")
    _finish_security(con, auth, [row], request_id)
    result = as_security(row)
    audit(
        con,
        str(auth.owner.id),
        "security_transfer",
        identifier,
        "CREATE",
        request_id=request_id,
        after=result.model_dump(mode="json"),
        reason=body.note,
    )
    return result


def revise_security(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: SecurityTransferRevision,
    request_id: str,
) -> SecurityTransfer:
    before = owned_transfer(con, str(auth.owner.id), identifier, "security")
    check_version(before["version"], version)
    _retire_security(con, before, "SUPERSEDED")
    revised = str(uuid4())
    values = _security_values(con, auth, body, revised, before["created_sequence"])
    values.update(
        id=revised,
        root_id=before["root_id"],
        supersedes_id=identifier,
        revision=before["revision"] + 1,
        created_sequence=before["created_sequence"],
        created_at=stamp(),
    )
    _insert(con, "security_transfers", values)
    row = owned_transfer(con, str(auth.owner.id), revised, "security")
    _finish_security(con, auth, [before, row], request_id)
    result = as_security(row)
    audit(
        con,
        str(auth.owner.id),
        "security_transfer",
        revised,
        "REVISE",
        request_id=request_id,
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def void_security(
    con: Connection, auth: Auth, identifier: str, version: int, reason: str, request_id: str
) -> Success:
    before = owned_transfer(con, str(auth.owner.id), identifier, "security")
    check_version(before["version"], version)
    _retire_security(con, before, "VOID")
    _finish_security(con, auth, [before], request_id)
    audit(
        con,
        str(auth.owner.id),
        "security_transfer",
        identifier,
        "VOID",
        request_id=request_id,
        before=dict(before),
        after={"status": "VOID", "version": version + 1},
        reason=reason,
    )
    return Success(message="Security transfer voided; original fact and cost snapshot retained")
