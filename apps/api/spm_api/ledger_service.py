"""Shared exact cash posting and validation of authoritative financial timelines."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, now, stamp
from spm_api.decimal_types import INT64_MAX, from_scaled, to_scaled
from spm_api.errors import ApiError
from spm_api.finance import FinanceError, Position, lock_money, round_money
from spm_api.master_models import Currency
from spm_api.master_service import owned_account
from spm_api.replay import Event, Split, Trade, Transfer, replay


def decoded(value: int, scale: int) -> Decimal:
    return Decimal(from_scaled(value, scale))


def reversible_integer(value: int) -> int:
    if type(value) is not int or not -INT64_MAX <= value <= INT64_MAX:
        raise FinanceError("FINANCIAL_OVERFLOW", "The cash amount and inverse must fit INT64")
    return value


def money_integer(value: Decimal) -> int:
    return reversible_integer(to_scaled(str(round_money(value)), 6))


def allocate_sequence(
    con: Connection, kind: Literal["TRADE", "TRANSFER", "ACTION"], root: str
) -> int:
    value = con.execute(
        text("INSERT INTO ledger_sequences(kind,fact_id) VALUES (:kind,:root)"),
        {"kind": kind, "root": root},
    ).lastrowid
    assert value is not None
    return value


def post_cash(
    con: Connection,
    *,
    account: str,
    day: str,
    currency: Currency,
    amount: int,
    fx: int | None,
    booked: int | None,
    kind: str,
    source_type: str,
    source_id: str,
    note: str = "",
    external: bool = False,
) -> str:
    reversible_integer(amount)
    if booked is not None:
        reversible_integer(booked)
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,
            fx_rate_hkd_e12,booked_hkd_micro,kind,source_type,source_id,note,external_flow,created_at)
        VALUES (:id,:account,:day,:currency,:amount,:fx,:booked,:kind,:type,:source,
                :note,:external,:now)
    """),
        {
            "id": identifier,
            "account": account,
            "day": day,
            "currency": currency,
            "amount": amount,
            "fx": fx,
            "booked": booked,
            "kind": kind,
            "type": source_type,
            "source": source_id,
            "note": note,
            "external": int(external),
            "now": stamp(),
        },
    )
    return identifier


def reverse_cash(con: Connection, original: RowMapping, *, reason: str) -> str:
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,
            fx_rate_hkd_e12,booked_hkd_micro,kind,source_type,source_id,entry_role,
            external_flow,note,created_at,reverses_entry_id)
        VALUES (:id,:account,:day,:currency,:amount,:fx,:booked,'REVERSAL',:type,:source,
                'REVERSAL',:external,:note,:now,:original)
    """),
        {
            "id": identifier,
            "account": original["account_id"],
            "day": original["effective_date"],
            "currency": original["currency"],
            "amount": -original["amount_micro"],
            "fx": original["fx_rate_hkd_e12"],
            "booked": -original["booked_hkd_micro"]
            if original["booked_hkd_micro"] is not None
            else None,
            "type": original["source_type"],
            "source": original["source_id"],
            "external": original["external_flow"],
            "note": reason,
            "now": stamp(),
            "original": original["id"],
        },
    )
    con.execute(
        text("UPDATE cash_ledger_entries SET version=version+1 WHERE id=:id"),
        {"id": original["id"]},
    )
    return identifier


def rebase_opening(
    con: Connection, owner: str, account_id: str, operation_id: str, request_id: str
) -> None:
    """Keep the owner's start-of-day balance assertion true after an earlier cash correction."""
    account = owned_account(con, owner, account_id)
    if account["opening_cash_micro"] is None:
        return
    day = account["opening_cash_date"]
    if day is None:
        raise ApiError(409, "NEEDS_REVIEW", "The opening balance needs a dated anchor")
    opening = (
        con.execute(
            text("""
        SELECT * FROM cash_ledger_entries WHERE account_id=:account
        AND source_type='OPENING_BALANCE' AND entry_role='PRIMARY'
    """),
            {"account": account_id},
        )
        .mappings()
        .first()
    )
    if (
        opening is None
        or con.execute(
            text("SELECT 1 FROM cash_ledger_entries WHERE reverses_entry_id=:id"),
            {"id": opening["id"]},
        ).first()
    ):
        raise ApiError(409, "NEEDS_REVIEW", "The opening balance has no current cash provenance")
    values = con.execute(
        text("""
        SELECT amount_micro FROM cash_ledger_entries
        WHERE account_id=:account AND currency=:currency
        AND (effective_date<:day OR (effective_date=:day
             AND source_type IN ('OPENING_BALANCE','OPENING_REBASE')))
    """),
        {"account": account_id, "currency": account["default_currency"], "day": day},
    ).scalars()
    # Python integers avoid SQLite SUM overflow while preserving every recorded micro-unit.
    current = sum(values, 0)
    delta = account["opening_cash_micro"] - current
    if delta == 0:
        return
    reversible_integer(delta)
    amount = decoded(delta, 6)
    fx = opening["fx_rate_hkd_e12"]
    locked = lock_money(
        amount, account["default_currency"], decoded(fx, 12) if fx is not None else None
    )
    identifier = post_cash(
        con,
        account=account_id,
        day=day,
        currency=account["default_currency"],
        amount=delta,
        fx=fx,
        booked=money_integer(locked.hkd),
        kind="ADJUSTMENT",
        source_type="OPENING_REBASE",
        source_id=operation_id,
        note="Preserve the declared opening balance after a pre-anchor cash change",
    )
    audit(
        con,
        owner,
        "cash_entry",
        identifier,
        "REBASE_OPENING",
        request_id=request_id,
        before={"anchor_balance_micro": current},
        after={"anchor_balance_micro": account["opening_cash_micro"], "delta_micro": delta},
        reason="Derived from the existing owner-declared opening balance; not an external flow",
    )


def rebase_cash_changes(
    con: Connection,
    owner: str,
    entries: Sequence[str],
    operation_id: str,
    request_id: str,
) -> None:
    """Rebase only a nonzero native change strictly before that wallet's opening assertion."""
    changes: dict[str, int] = {}
    for identifier in entries:
        row = (
            con.execute(
                text("""
            SELECT c.*,a.default_currency,a.opening_cash_date FROM cash_ledger_entries c
            JOIN accounts a ON a.id=c.account_id JOIN portfolios p ON p.id=a.portfolio_id
            WHERE c.id=:id AND p.owner_id=:owner
        """),
                {"id": identifier, "owner": owner},
            )
            .mappings()
            .one()
        )
        if (
            row["opening_cash_date"] is not None
            and row["currency"] == row["default_currency"]
            and row["effective_date"] < row["opening_cash_date"]
        ):
            account = row["account_id"]
            changes[account] = changes.get(account, 0) + row["amount_micro"]
    for account, change in changes.items():
        if change:
            rebase_opening(con, owner, account, operation_id, request_id)


def history_events(con: Connection, portfolio: str) -> tuple[list[Event], list[RowMapping]]:
    events: list[Event] = []
    for row in con.execute(
        text("SELECT * FROM trades WHERE portfolio_id=:p AND status='ACTIVE'"), {"p": portfolio}
    ).mappings():
        if row["booked_hkd_micro"] is None:
            raise FinanceError("MISSING_FX", "An active trade has no locked HKD amount")
        events.append(
            Event(
                f"trade:{row['id']}",
                date.fromisoformat(row["trade_date"]),
                row["created_sequence"],
                Trade(
                    row["account_id"],
                    row["security_id"],
                    row["side"],
                    decoded(row["quantity_e8"], 8),
                    decoded(row["booked_hkd_micro"], 6),
                ),
                datetime.fromisoformat(row["executed_at_utc"].replace("Z", "+00:00"))
                if row["executed_at_utc"]
                else None,
            )
        )
    for row in con.execute(
        text(
            "SELECT * FROM security_transfers WHERE portfolio_id=:p "
            "AND status='ACTIVE' AND is_current=1"
        ),
        {"p": portfolio},
    ).mappings():
        events.append(
            Event(
                f"transfer:{row['id']}",
                date.fromisoformat(row["transfer_date"]),
                row["created_sequence"],
                Transfer(
                    row["source_account_id"],
                    row["target_account_id"],
                    row["security_id"],
                    decoded(row["quantity_e8"], 8),
                ),
            )
        )
    securities = {event.operation.security_id for event in events}
    unresolved: list[RowMapping] = []
    for row in con.execute(
        text("""
        SELECT c.* FROM corporate_actions c WHERE c.is_current=1 AND c.status!='VOID'
        AND (EXISTS(SELECT 1 FROM trades t WHERE t.security_id=c.security_id AND t.portfolio_id=:p)
             OR EXISTS(SELECT 1 FROM security_transfers s
                       WHERE s.security_id=c.security_id AND s.portfolio_id=:p))
    """),
        {"p": portfolio},
    ).mappings():
        if row["security_id"] not in securities or row["action_type"] == "SYMBOL_CHANGE":
            continue
        if row["status"] == "CONFIRMED" and row["action_type"] in {"SPLIT", "REVERSE_SPLIT"}:
            events.append(
                Event(
                    f"action:{row['id']}",
                    date.fromisoformat(row["effective_date"]),
                    row["created_sequence"],
                    Split(row["security_id"], row["ratio_num"], row["ratio_den"]),
                )
            )
        else:
            unresolved.append(row)
    return events, unresolved


def validate_history(con: Connection, portfolio: str) -> dict[tuple[str, str], Position]:
    events, unresolved = history_events(con, portfolio)
    clock = now()
    horizons: dict[str, date] = {
        row["id"]: clock.astimezone(ZoneInfo(row["timezone"])).date()
        for row in con.execute(
            text("""
            SELECT s.id,e.timezone FROM securities s JOIN exchanges e ON e.code=s.exchange_code
            WHERE s.id IN (SELECT security_id FROM trades WHERE portfolio_id=:p
                           UNION SELECT security_id FROM security_transfers WHERE portfolio_id=:p)
        """),
            {"p": portfolio},
        ).mappings()
    }
    # Validate committed future facts too, but never apply another market's tomorrow early.
    for event in events:
        if not isinstance(event.operation, Split):
            security = event.operation.security_id
            horizons[security] = max(horizons[security], event.day)
    events = [
        event
        for event in events
        if not isinstance(event.operation, Split)
        or event.day <= horizons[event.operation.security_id]
    ]
    for action in unresolved:
        day = date.fromisoformat(action["effective_date"])
        if day <= horizons[action["security_id"]]:
            before = replay(events, as_of=day, exclusive=True)
            if any(
                key[1] == action["security_id"] and position.quantity > 0
                for key, position in before.items()
            ):
                raise FinanceError(
                    "NEEDS_REVIEW", "An unallocated corporate action affects held shares"
                )
    return replay(events, as_of=max(horizons.values(), default=clock.date()))
