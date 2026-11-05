"""Atomic native cash/expense posting, exact corrections, and truthful wallet balances."""

from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, stamp
from spm_api.cash_models import (
    AccountCashBalances,
    BalanceQuality,
    BookingInput,
    CashEntry,
    CashEntryCreate,
    CashEntryRevision,
    CurrencyCashBalance,
    Expense,
    ExpenseCreate,
    ExpenseRevision,
)
from spm_api.decimal_types import from_scaled, to_scaled
from spm_api.errors import ApiError, Success
from spm_api.finance import FinanceError, conversion_rate, lock_money
from spm_api.ledger_service import (
    money_integer,
    post_cash,
    rebase_cash_changes,
    reverse_cash,
    reversible_integer,
)
from spm_api.master_models import Currency
from spm_api.master_service import invalidate_portfolio, owned_account
from spm_api.mutations import check_version
from spm_api.security import Auth

MANUAL_SOURCES = frozenset({"MANUAL", "MANUAL_CASH", "CASH_CORRECTION"})
MANUAL_KINDS = frozenset({"DEPOSIT", "WITHDRAW", "ADJUSTMENT"})
CASH_SQL = """
    SELECT c.*,a.portfolio_id,
        (SELECT r.id FROM cash_ledger_entries r WHERE r.reverses_entry_id=c.id) AS reversed_by_id,
        (SELECT n.id FROM cash_ledger_entries n JOIN accounts b ON b.id=n.account_id
         JOIN portfolios q ON q.id=b.portfolio_id WHERE n.source_type='CASH_CORRECTION'
         AND n.source_id=c.id AND n.entry_role='PRIMARY' AND q.owner_id=:owner LIMIT 1)
            AS superseded_by_id
    FROM cash_ledger_entries c JOIN accounts a ON a.id=c.account_id
    JOIN portfolios p ON p.id=a.portfolio_id WHERE p.owner_id=:owner
"""
EXPENSE_SQL = """
    SELECT e.*,a.portfolio_id FROM expenses e JOIN accounts a ON a.id=e.account_id
    JOIN portfolios p ON p.id=a.portfolio_id WHERE p.owner_id=:owner
"""


def owned_cash(con: Connection, owner: str, identifier: str) -> RowMapping:
    row = (
        con.execute(text(CASH_SQL + " AND c.id=:id"), {"owner": owner, "id": identifier})
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Cash entry not found")
    return row


def owned_expense(con: Connection, owner: str, identifier: str) -> RowMapping:
    row = (
        con.execute(text(EXPENSE_SQL + " AND e.id=:id"), {"owner": owner, "id": identifier})
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Expense not found")
    return row


def as_cash(row: RowMapping) -> CashEntry:
    values = {key: row[key] for key in CashEntry.model_fields if key in row}
    for field, column, scale in (
        ("amount", "amount_micro", 6),
        ("fx_rate_hkd", "fx_rate_hkd_e12", 12),
        ("booked_hkd", "booked_hkd_micro", 6),
    ):
        values[field] = from_scaled(row[column], scale) if row[column] is not None else None
    values["status"] = (
        "REVERSAL"
        if row["entry_role"] == "REVERSAL"
        else "SUPERSEDED"
        if row["superseded_by_id"] is not None
        else "VOID"
        if row["reversed_by_id"] is not None
        else "ACTIVE"
    )
    values["editable"] = (
        values["status"] == "ACTIVE"
        and row["source_type"] in MANUAL_SOURCES
        and row["kind"] in MANUAL_KINDS
    )
    values["supersedes_id"] = (
        row["source_id"]
        if (row["source_type"] == "CASH_CORRECTION" and row["entry_role"] == "PRIMARY")
        else None
    )
    values["external_flow"] = bool(row["external_flow"])
    values["quality"] = "MISSING_FX" if row["booked_hkd_micro"] is None else "OK"
    return CashEntry.model_validate(values)


def as_expense(row: RowMapping) -> Expense:
    values = {key: row[key] for key in Expense.model_fields if key in row}
    values["amount"] = from_scaled(row["amount_micro"], 6)
    values["fx_rate_hkd"] = (
        from_scaled(row["fx_rate_hkd_e12"], 12) if row["fx_rate_hkd_e12"] is not None else None
    )
    values["hkd_amount"] = (
        from_scaled(row["hkd_micro"], 6) if row["hkd_micro"] is not None else None
    )
    values["quality"] = "MISSING_FX" if row["hkd_micro"] is None else "OK"
    return Expense.model_validate(values)


def _active_account(con: Connection, auth: Auth, identifier: str) -> RowMapping:
    account = owned_account(con, str(auth.owner.id), identifier)
    if account["status"] != "ACTIVE":
        raise ApiError(409, "ARCHIVED", "Reopen the account before recording a new movement")
    return account


def _booking(body: BookingInput) -> tuple[int, int | None, int | None]:
    amount = reversible_integer(to_scaled(body.amount, 6))
    rate = conversion_rate(
        body.currency, Decimal(body.fx_rate_hkd) if body.fx_rate_hkd is not None else None
    )
    if rate is None:
        if body.actual_hkd is not None:
            raise FinanceError(
                "MISSING_FX", "An actual HKD booking still needs its historical FX snapshot"
            )
        return amount, None, None
    locked = lock_money(
        Decimal(body.amount),
        body.currency,
        rate,
        actual_hkd=Decimal(body.actual_hkd) if body.actual_hkd is not None else None,
        reason=body.reason,
    )
    return amount, to_scaled(format(rate, "f"), 12), money_integer(locked.hkd)


def _finish(
    con: Connection,
    auth: Auth,
    rows: Sequence[RowMapping],
    entries: list[str],
    source: str,
    reason: str,
    day: str,
    request_id: str,
) -> None:
    rebase_cash_changes(con, str(auth.owner.id), entries, str(uuid4()), request_id)
    for portfolio in {row["portfolio_id"] for row in rows}:
        invalidate_portfolio(con, portfolio, source, reason, day)


def _manual_current(row: RowMapping) -> None:
    if row["source_type"] not in MANUAL_SOURCES or row["kind"] not in MANUAL_KINDS:
        raise ApiError(
            409,
            "SOURCE_MANAGED",
            "Use the owning trade, expense, receipt, transfer or opening workflow",
        )
    if row["entry_role"] != "PRIMARY" or row["reversed_by_id"] is not None:
        raise ApiError(409, "INACTIVE_FACT", "Use the current unreversed manual cash entry")


def _post_manual(con: Connection, body: CashEntryCreate, *, predecessor: str | None = None) -> str:
    amount, fx, booked = _booking(body)
    return post_cash(
        con,
        account=str(body.account_id),
        day=body.effective_date,
        currency=body.currency,
        amount=amount,
        fx=fx,
        booked=booked,
        kind=body.kind,
        source_type="CASH_CORRECTION" if predecessor is not None else "MANUAL_CASH",
        source_id=predecessor or str(uuid4()),
        note=body.note,
        external=body.kind != "ADJUSTMENT",
    )


def create_cash(con: Connection, auth: Auth, body: CashEntryCreate, request_id: str) -> CashEntry:
    _active_account(con, auth, str(body.account_id))
    identifier = _post_manual(con, body)
    row = owned_cash(con, str(auth.owner.id), identifier)
    _finish(
        con,
        auth,
        [row],
        [identifier],
        identifier,
        "CASH_FACT_CHANGED",
        body.effective_date,
        request_id,
    )
    result = as_cash(row)
    audit(
        con,
        str(auth.owner.id),
        "cash_entry",
        identifier,
        "CREATE",
        request_id=request_id,
        after=result.model_dump(mode="json"),
        reason=body.reason or body.note,
    )
    return result


def revise_cash(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: CashEntryRevision,
    request_id: str,
) -> CashEntry:
    before = owned_cash(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    _manual_current(before)
    _active_account(con, auth, str(body.account_id))
    reversal = reverse_cash(con, before, reason=body.reason)
    revised = _post_manual(con, body, predecessor=identifier)
    row = owned_cash(con, str(auth.owner.id), revised)
    _finish(
        con,
        auth,
        [before, row],
        [reversal, revised],
        revised,
        "CASH_FACT_CHANGED",
        min(before["effective_date"], body.effective_date),
        request_id,
    )
    result = as_cash(row)
    audit(
        con,
        str(auth.owner.id),
        "cash_entry",
        revised,
        "REVISE",
        request_id=request_id,
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def void_cash(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    reason: str,
    request_id: str,
) -> Success:
    before = owned_cash(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    _manual_current(before)
    reversal = reverse_cash(con, before, reason=reason)
    _finish(
        con,
        auth,
        [before],
        [reversal],
        identifier,
        "CASH_FACT_CHANGED",
        before["effective_date"],
        request_id,
    )
    audit(
        con,
        str(auth.owner.id),
        "cash_entry",
        identifier,
        "VOID",
        request_id=request_id,
        before=dict(before),
        after={"status": "VOID", "version": version + 1, "reversal_id": reversal},
        reason=reason,
    )
    return Success(message="Cash entry voided by an exact reversal; original facts retained")


def _insert_expense(
    con: Connection,
    body: ExpenseCreate,
    *,
    predecessor: str | None = None,
) -> str:
    amount, fx, booked = _booking(body)
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO expenses(id,account_id,date,category,amount_micro,currency,fx_rate_hkd_e12,
            hkd_micro,supersedes_id,note,created_at)
        VALUES (:id,:account,:day,:category,:amount,:currency,:fx,:booked,:previous,:note,:now)
    """),
        {
            "id": identifier,
            "account": str(body.account_id),
            "day": body.date,
            "category": body.category,
            "amount": amount,
            "currency": body.currency,
            "fx": fx,
            "booked": booked,
            "previous": predecessor,
            "note": body.note,
            "now": stamp(),
        },
    )
    return identifier


def _post_expense(con: Connection, row: RowMapping) -> str:
    return post_cash(
        con,
        account=row["account_id"],
        day=row["date"],
        currency=row["currency"],
        amount=-row["amount_micro"],
        fx=row["fx_rate_hkd_e12"],
        booked=-row["hkd_micro"] if row["hkd_micro"] is not None else None,
        kind="EXPENSE",
        source_type="EXPENSE",
        source_id=row["id"],
        note=row["note"],
    )


def _retire_expense(con: Connection, row: RowMapping, status: str) -> None:
    if row["status"] != "ACTIVE":
        raise ApiError(409, "INACTIVE_FACT", "Use the current active expense")
    result = con.execute(
        text(
            "UPDATE expenses SET status=:status,version=version+1 WHERE id=:id AND version=:version"
        ),
        {"status": status, "id": row["id"], "version": row["version"]},
    )
    if result.rowcount != 1:
        raise ApiError(412, "PRECONDITION_FAILED", "This expense changed; reload before editing")


def _reverse_expense(con: Connection, row: RowMapping, reason: str) -> str:
    entries = (
        con.execute(
            text("""
        SELECT * FROM cash_ledger_entries WHERE source_type='EXPENSE' AND source_id=:id
        AND entry_role='PRIMARY'
    """),
            {"id": row["id"]},
        )
        .mappings()
        .all()
    )
    if len(entries) != 1:
        raise ApiError(409, "NEEDS_REVIEW", "The expense has inconsistent cash provenance")
    return reverse_cash(con, entries[0], reason=reason)


def create_expense(con: Connection, auth: Auth, body: ExpenseCreate, request_id: str) -> Expense:
    _active_account(con, auth, str(body.account_id))
    identifier = _insert_expense(con, body)
    row = owned_expense(con, str(auth.owner.id), identifier)
    cash = _post_expense(con, row)
    _finish(con, auth, [row], [cash], identifier, "EXPENSE_FACT_CHANGED", body.date, request_id)
    result = as_expense(row)
    audit(
        con,
        str(auth.owner.id),
        "expense",
        identifier,
        "CREATE",
        request_id=request_id,
        after=result.model_dump(mode="json"),
        reason=body.reason or body.note,
    )
    return result


def revise_expense(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: ExpenseRevision,
    request_id: str,
) -> Expense:
    before = owned_expense(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    _active_account(con, auth, str(body.account_id))
    _retire_expense(con, before, "SUPERSEDED")
    reversal = _reverse_expense(con, before, body.reason)
    revised = _insert_expense(con, body, predecessor=identifier)
    row = owned_expense(con, str(auth.owner.id), revised)
    cash = _post_expense(con, row)
    _finish(
        con,
        auth,
        [before, row],
        [reversal, cash],
        revised,
        "EXPENSE_FACT_CHANGED",
        min(before["date"], body.date),
        request_id,
    )
    result = as_expense(row)
    audit(
        con,
        str(auth.owner.id),
        "expense",
        revised,
        "REVISE",
        request_id=request_id,
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
    )
    return result


def void_expense(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    reason: str,
    request_id: str,
) -> Success:
    before = owned_expense(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    _retire_expense(con, before, "VOID")
    reversal = _reverse_expense(con, before, reason)
    _finish(
        con,
        auth,
        [before],
        [reversal],
        identifier,
        "EXPENSE_FACT_CHANGED",
        before["date"],
        request_id,
    )
    audit(
        con,
        str(auth.owner.id),
        "expense",
        identifier,
        "VOID",
        request_id=request_id,
        before=dict(before),
        after={"status": "VOID", "version": version + 1},
        reason=reason,
    )
    return Success(message="Expense voided; original facts and exact cash reversal retained")


def _total(value: int) -> str:
    try:
        return from_scaled(value, 6)
    except ValueError as exc:
        raise FinanceError("FINANCIAL_OVERFLOW", "The recorded cash total exceeds INT64") from exc


def cash_balances(con: Connection, owner: str, identifier: str, day: str) -> AccountCashBalances:
    account = owned_account(con, owner, identifier)
    rows = (
        con.execute(
            text("SELECT * FROM cash_ledger_entries WHERE account_id=:id AND effective_date<=:day"),
            {"id": identifier, "day": day},
        )
        .mappings()
        .all()
    )
    reversed_ids = {
        row["reverses_entry_id"] for row in rows if row["reverses_entry_id"] is not None
    }
    currencies: set[Currency] = {account["default_currency"], *(row["currency"] for row in rows)}
    quality: BalanceQuality = account["cash_completeness"]
    opening_day = account["opening_cash_date"]
    if opening_day is not None:
        if day < opening_day:
            quality = "BEFORE_OPENING"
        else:
            originals = [
                row
                for row in rows
                if row["source_type"] == "OPENING_BALANCE" and row["entry_role"] == "PRIMARY"
            ]
            anchored = sum(
                row["amount_micro"]
                for row in rows
                if row["currency"] == account["default_currency"]
                and (
                    row["effective_date"] < opening_day
                    or (
                        row["effective_date"] == opening_day
                        and row["source_type"] in {"OPENING_BALANCE", "OPENING_REBASE"}
                    )
                )
            )
            if (
                len(originals) != 1
                or originals[0]["id"] in reversed_ids
                or anchored != account["opening_cash_micro"]
            ):
                quality = "NEEDS_REVIEW"
    balances: list[CurrencyCashBalance] = []
    for currency in sorted(currencies):
        wallet = [row for row in rows if row["currency"] == currency]
        current = [
            row
            for row in wallet
            if row["entry_role"] == "PRIMARY" and row["id"] not in reversed_ids
        ]
        unvalued = sum(row["booked_hkd_micro"] is None for row in current)
        recorded = _total(sum(row["amount_micro"] for row in wallet))
        booked = _total(sum(row["booked_hkd_micro"] for row in current)) if unvalued == 0 else None
        balances.append(
            CurrencyCashBalance(
                currency=currency,
                recorded_balance=recorded,
                balance=recorded if quality == "COMPLETE" else None,
                balance_quality=quality,
                historical_booked_hkd_net=booked,
                historical_booking_quality="MISSING_FX" if unvalued else "OK",
                unvalued_entries=unvalued,
            )
        )
    return AccountCashBalances(
        account_id=identifier,
        as_of=day,
        cash_completeness=account["cash_completeness"],
        balances=balances,
    )
