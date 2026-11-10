"""Atomic trade creation, immutable revision/void, and exact cash/projection effects."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

from spm_api import trade_audit, trade_history_review
from spm_api.audit import audit, now, stamp
from spm_api.decimal_types import to_scaled
from spm_api.errors import ApiError, Success
from spm_api.finance import FinanceError, conversion_rate, lock_money, native_settlement
from spm_api.ledger_service import (
    allocate_sequence,
    money_integer,
    post_cash,
    rebase_cash_changes,
    reverse_cash,
    validate_history,
)
from spm_api.master_service import (
    invalidate_portfolio,
    owned_account,
    owned_broker,
    owned_portfolio,
    security_row,
)
from spm_api.mutations import check_version
from spm_api.security import Auth
from spm_api.trade_codec import as_trade as as_trade
from spm_api.trade_models import TradeCreate, TradeRecord, TradeRevision


def owned_trade(con: Connection, owner: str, identifier: str) -> RowMapping:
    row = (
        con.execute(
            text("""
        SELECT t.* FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
        WHERE t.id=:id AND p.owner_id=:owner
    """),
            {"id": identifier, "owner": owner},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Trade not found")
    return row


def _remember_trade_origin(con: Connection, auth: Auth, identifier: str, request_id: str) -> None:
    try:
        trade_audit.remember_created(con, auth.owner.id, UUID(identifier), UUID(request_id))
    except ValueError:
        raise ApiError(
            503, "TRADE_AUDIT_UNAVAILABLE", "Trade audit history is unavailable"
        ) from None


def _capture_trade_origin(con: Connection, auth: Auth, row: RowMapping, request_id: str) -> None:
    if row["status"] in {"SUPERSEDED", "VOID"}:
        raise ApiError(409, "INACTIVE_FACT", "Use the current trade revision")
    identifier = row["id"]
    try:
        reviewed_ids = con.execute(
            text("""
            SELECT DISTINCT t.id FROM trades t
            JOIN trade_history_reviews r ON r.trade_id=t.id
            WHERE t.root_id=:root
        """),
            {"root": row["root_id"]},
        ).scalars()
        missing_creation = False
        for reviewed_id in reviewed_ids:
            reviews = trade_history_review.history(con, auth.owner.id, UUID(reviewed_id))
            if not reviews:
                raise ValueError("Invalid reviewed trade history")
            missing_creation |= any(item.gap == "MISSING_CREATION_AUDIT" for item in reviews)
    except (ValueError, SQLAlchemyError):
        raise ApiError(
            503, "TRADE_AUDIT_UNAVAILABLE", "Trade audit history is unavailable"
        ) from None
    if missing_creation:
        raise ApiError(
            409,
            "NEEDS_REVIEW",
            "A previously missing trade audit requires review before correction",
        )
    origin = con.execute(
        text("SELECT 1 FROM trade_audit_origins WHERE trade_id=:id"), {"id": identifier}
    ).first()
    if (
        origin is None
        and con.execute(
            text("""
            SELECT 1 FROM audit_events WHERE entity_type='trade' AND entity_id=:id
            AND action=:action LIMIT 1
        """),
            {"id": identifier, "action": "CREATE" if row["revision"] == 1 else "REVISE"},
        ).first()
        is None
    ):
        raise ApiError(
            409,
            "NEEDS_REVIEW",
            "The original trade audit is missing; review is required before correction",
        )
    try:
        trade_audit.capture_before_retire(con, auth.owner.id, UUID(identifier), UUID(request_id))
    except ValueError:
        raise ApiError(
            503, "TRADE_AUDIT_UNAVAILABLE", "Trade audit history is unavailable"
        ) from None


def _prepare(con: Connection, auth: Auth, body: TradeCreate) -> dict[str, Any]:
    owner, portfolio, account_id = str(auth.owner.id), str(body.portfolio_id), str(body.account_id)
    owned_portfolio(con, owner, portfolio)
    account = owned_account(con, owner, account_id)
    owned_broker(con, owner, account["broker_id"])
    if account["portfolio_id"] != portfolio:
        raise ApiError(409, "PORTFOLIO_MISMATCH", "The trade account belongs to another portfolio")
    if account["status"] != "ACTIVE":
        raise ApiError(409, "ARCHIVED", "Reopen the account before adding a trade")
    security = security_row(con, str(body.security_id))
    if body.currency != security["quote_currency"]:
        raise ApiError(
            422, "CURRENCY_MISMATCH", "Price, fee and tax must use the security quote currency"
        )
    timezone = con.execute(
        text("SELECT timezone FROM exchanges WHERE code=:code"), {"code": security["exchange_code"]}
    ).scalar_one()
    zone = ZoneInfo(timezone)
    if body.executed_at_utc is not None:
        try:
            local_day = (
                datetime.fromisoformat(body.executed_at_utc.replace("Z", "+00:00"))
                .astimezone(zone)
                .date()
                .isoformat()
            )
        except (ValueError, OverflowError) as exc:
            raise ApiError(
                422, "VALIDATION_ERROR", "Execution time is outside the supported calendar"
            ) from exc
        if local_day != body.trade_date:
            raise ApiError(
                422,
                "EXECUTION_DATE_MISMATCH",
                "Execution time does not match the market-local trade date",
            )
    scheduled = body.trade_date > now().astimezone(zone).date().isoformat()
    native = native_settlement(
        body.side,
        Decimal(body.quantity),
        Decimal(body.price),
        Decimal(body.fee),
        Decimal(body.tax),
        body.currency,
        actual_local=Decimal(body.actual_settlement)
        if body.actual_settlement is not None
        else None,
        reason=body.override_reason,
    )
    rate = conversion_rate(
        body.currency, Decimal(body.fx_rate_hkd) if body.fx_rate_hkd is not None else None
    )
    booked = None
    if rate is None:
        if not scheduled or body.actual_hkd is not None:
            raise FinanceError(
                "MISSING_FX", "USD needs an explicit historical FX snapshot before posting"
            )
    else:
        locked = lock_money(
            native.amount,
            body.currency,
            rate,
            actual_hkd=Decimal(body.actual_hkd) if body.actual_hkd is not None else None,
            reason=body.override_reason,
        )
        booked = money_integer(locked.hkd)
    gross, settlement = money_integer(native.gross_local), money_integer(native.amount)
    fee, tax = to_scaled(body.fee, 6), to_scaled(body.tax, 6)
    delta = money_integer(native.rounding_delta_local)
    return {
        "portfolio_id": portfolio,
        "account_id": account_id,
        "security_id": str(body.security_id),
        "side": body.side,
        "trade_date": body.trade_date,
        "executed_at_utc": body.executed_at_utc,
        "quantity_e8": to_scaled(body.quantity, 8),
        "price_e8": to_scaled(body.price, 8),
        "fee_micro": fee,
        "tax_micro": tax,
        "currency": body.currency,
        "fx_rate_hkd_e12": to_scaled(format(rate, "f"), 12) if rate is not None else None,
        "fx_source": "IDENTITY"
        if body.currency == "HKD"
        else "MANUAL"
        if body.fx_source == "MANUAL"
        else f"MANUAL:{body.fx_source}",
        "gross_micro": gross,
        "settlement_micro": settlement,
        "booked_hkd_micro": booked,
        "rounding_delta_micro": delta,
        "settlement_override": int(body.actual_settlement is not None),
        "hkd_override": int(body.actual_hkd is not None),
        "override_reason": body.override_reason,
        "note": body.note,
        "status": "DRAFT_SCHEDULED" if scheduled else "ACTIVE",
    }


def _insert(con: Connection, values: dict[str, Any]) -> None:
    # All keys come from this service's fixed mappings, never user-supplied SQL identifiers.
    con.execute(
        text(
            f"INSERT INTO trades({','.join(values)}) "
            f"VALUES ({','.join(':' + key for key in values)})"
        ),
        values,
    )


def _post_settlement(con: Connection, row: RowMapping) -> str | None:
    if row["status"] != "ACTIVE":
        return None
    sign = -1 if row["side"] == "BUY" else 1
    return post_cash(
        con,
        account=row["account_id"],
        day=row["trade_date"],
        currency=row["currency"],
        amount=sign * row["settlement_micro"],
        fx=row["fx_rate_hkd_e12"],
        booked=sign * row["booked_hkd_micro"],
        kind="TRADE_SETTLEMENT",
        source_type="TRADE",
        source_id=row["id"],
        note=row["note"],
    )


def _retire(con: Connection, row: RowMapping, status: str) -> None:
    if row["status"] in {"SUPERSEDED", "VOID"}:
        raise ApiError(409, "INACTIVE_FACT", "Use the current trade revision")
    result = con.execute(
        text("""
        UPDATE trades SET status=:status,version=version+1,updated_at=:now
        WHERE id=:id AND version=:version
    """),
        {"status": status, "now": stamp(), "id": row["id"], "version": row["version"]},
    )
    if result.rowcount != 1:
        raise ApiError(412, "PRECONDITION_FAILED", "This trade changed; reload before editing")


def _reverse_settlement(con: Connection, row: RowMapping, reason: str) -> list[str]:
    entries = list(
        con.execute(
            text("""
        SELECT * FROM cash_ledger_entries WHERE source_type='TRADE' AND source_id=:id
        AND entry_role='PRIMARY'
    """),
            {"id": row["id"]},
        ).mappings()
    )
    if len(entries) != (1 if row["status"] == "ACTIVE" else 0):
        raise ApiError(409, "NEEDS_REVIEW", "The trade's settlement provenance is inconsistent")
    return [reverse_cash(con, entry, reason=reason) for entry in entries]


def _finalize(
    con: Connection,
    auth: Auth,
    rows: list[RowMapping],
    cash_entries: list[str],
    request_id: str,
) -> None:
    economic = [row for row in rows if row["status"] == "ACTIVE"]
    for portfolio in {row["portfolio_id"] for row in economic}:
        validate_history(con, portfolio)
    rebase_cash_changes(con, str(auth.owner.id), cash_entries, str(uuid4()), request_id)
    day = min(row["trade_date"] for row in rows)
    for portfolio in {row["portfolio_id"] for row in rows}:
        invalidate_portfolio(con, portfolio, rows[-1]["id"], "TRADE_FACT_CHANGED", day)


def create_trade(con: Connection, auth: Auth, body: TradeCreate, request_id: str) -> TradeRecord:
    values = _prepare(con, auth, body)
    identifier = str(uuid4())
    values.update(
        id=identifier,
        root_id=identifier,
        created_sequence=allocate_sequence(con, "TRADE", identifier),
        created_at=stamp(),
        updated_at=stamp(),
    )
    _insert(con, values)
    row = owned_trade(con, str(auth.owner.id), identifier)
    settlement = _post_settlement(con, row)
    _finalize(con, auth, [row], [settlement] if settlement is not None else [], request_id)
    result = as_trade(row)
    audit(
        con,
        str(auth.owner.id),
        "trade",
        identifier,
        "CREATE",
        request_id=request_id,
        after=result.model_dump(mode="json"),
        reason=body.override_reason or body.note,
    )
    _remember_trade_origin(con, auth, identifier, request_id)
    return result


def revise_trade(
    con: Connection, auth: Auth, identifier: str, version: int, body: TradeRevision, request_id: str
) -> TradeRecord:
    before = owned_trade(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    values = _prepare(con, auth, body)
    _capture_trade_origin(con, auth, before, request_id)
    _retire(con, before, "SUPERSEDED")
    cash_entries = _reverse_settlement(con, before, body.reason)
    revised = str(uuid4())
    values.update(
        id=revised,
        root_id=before["root_id"],
        created_sequence=before["created_sequence"],
        revision=before["revision"] + 1,
        supersedes_id=identifier,
        created_at=stamp(),
        updated_at=stamp(),
    )
    _insert(con, values)
    row = owned_trade(con, str(auth.owner.id), revised)
    settlement = _post_settlement(con, row)
    if settlement is not None:
        cash_entries.append(settlement)
    _finalize(con, auth, [before, row], cash_entries, request_id)
    result = as_trade(row)
    audit(
        con,
        str(auth.owner.id),
        "trade",
        revised,
        "REVISE",
        request_id=request_id,
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    _remember_trade_origin(con, auth, revised, request_id)
    return result


def void_trade(
    con: Connection, auth: Auth, identifier: str, version: int, reason: str, request_id: str
) -> Success:
    before = owned_trade(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    _capture_trade_origin(con, auth, before, request_id)
    _retire(con, before, "VOID")
    cash_entries = _reverse_settlement(con, before, reason)
    _finalize(con, auth, [before], cash_entries, request_id)
    audit(
        con,
        str(auth.owner.id),
        "trade",
        identifier,
        "VOID",
        request_id=request_id,
        before=dict(before),
        after={"status": "VOID", "version": version + 1},
        reason=reason,
    )
    return Success(message="Trade voided; original facts and cash reversal retained")
