"""Read owned recorded dividend-share observations from a consistent ledger snapshot."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from spm_api import trade_audit
from spm_api.action_service import action_row, as_action
from spm_api.audit import canonical, digest, stamp
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_eligibility import (
    AccountEligibility,
    EligibilityRule,
    PendingAction,
    recorded_eligibility,
)
from spm_api.ledger_service import history_events
from spm_api.replay import Trade, Transfer
from spm_api.trade_service import as_trade
from spm_api.transfer_service import as_security

TradeHistoryAuditScope = Literal[
    "NO_RETIRED_TRADES", "RETIRED_TRADE_HISTORY_UNVERIFIED", "RETIRED_TRADE_HASHES_MATCHED"
]


class EligibilityHistoryError(ValueError):
    """Fixed codes only; do not expose portfolio or financial facts."""


@dataclass(frozen=True, slots=True)
class RecordedEligibilityBook:
    """A private snapshot, not an HTTP authorization or current-use grant."""

    owner_id: UUID = field(repr=False)
    portfolio_id: UUID = field(repr=False)
    security_id: UUID = field(repr=False)
    data_revision: int
    ex_date: date
    as_of: date
    rule: EligibilityRule
    trade_history_audit_scope: TradeHistoryAuditScope
    accounts: tuple[AccountEligibility, ...] = field(repr=False)


def _canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("Expected a canonical identity")
    identifier = UUID(value)
    if str(identifier) != value:
        raise ValueError("Expected a canonical identity")
    return identifier


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("Expected a calendar date")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("Expected a canonical calendar date")
    return result


def _sequence(row: RowMapping, kind: str) -> None:
    _canonical_uuid(row["id"])
    _canonical_uuid(row["root_id"])
    if (
        type(row["created_sequence"]) is not int
        or not 1 <= row["created_sequence"] <= INT64_MAX
        or row["sequence_kind"] != kind
        or row["sequence_root"] != row["root_id"]
    ):
        raise ValueError("Inconsistent ledger sequence")


def _audit_entry(
    con: Connection,
    row: RowMapping,
    kind: Literal["TRADE", "TRANSFER", "ACTION"],
    action: str,
    owner: str | None,
) -> RowMapping:
    entity = {"TRADE": "trade", "TRANSFER": "security_transfer", "ACTION": "corporate_action"}[kind]
    entries = (
        con.execute(
            text("""
        SELECT a.*,o.id AS actor_id FROM audit_events a LEFT JOIN owners o ON o.id=a.owner_id
        WHERE a.entity_type=:entity AND a.entity_id=:id AND a.action=:action
    """),
            {"entity": entity, "id": row["id"], "action": action},
        )
        .mappings()
        .all()
    )
    if len(entries) != 1:
        raise ValueError("Missing fact audit")
    entry = entries[0]
    _canonical_uuid(entry["id"])
    _canonical_uuid(entry["owner_id"])
    _canonical_uuid(entry["request_id"])
    if entry["actor_id"] != entry["owner_id"] or (owner is not None and entry["owner_id"] != owner):
        raise ValueError("Inconsistent audit actor")
    reason, timestamp = entry["reason"], entry["created_at"]
    if (
        not isinstance(reason, str)
        or len(reason) > 1000
        or (action != "CREATE" and (len(reason) < 10 or reason != reason.strip()))
        or not isinstance(timestamp, str)
    ):
        raise ValueError("Invalid audit metadata")
    recorded = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if recorded.utcoffset() is None or stamp(recorded.astimezone(UTC)) != timestamp:
        raise ValueError("Invalid audit timestamp")
    before, after = entry["before_sha256"], entry["after_sha256"]

    def valid_hash(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(digit in "0123456789abcdef" for digit in value)
        )

    if (
        not valid_hash(after)
        or (action == "CREATE" and before is not None)
        or (action != "CREATE" and not valid_hash(before))
    ):
        raise ValueError("Invalid audit fingerprint")
    return entry


def _predecessor_hashes(
    con: Connection,
    previous: RowMapping,
    kind: Literal["TRANSFER", "ACTION"],
    *,
    statuses: Sequence[str] | None = None,
) -> set[str]:
    if kind == "TRANSFER":
        row = (
            con.execute(
                text("SELECT * FROM security_transfers WHERE id=:id"), {"id": previous["id"]}
            )
            .mappings()
            .one()
        )
        values = dict(row)
        values.update(status="ACTIVE", is_current=1, version=1)
        return {digest(canonical(values))}
    values = dict(action_row(con, previous["id"]))
    values.update(is_current=1, version=1)
    return {
        digest(canonical({**values, "status": status, "review_required": reviewed}))
        for status in statuses or (previous["status"],)
        for reviewed in (0, 1)
    }


def _audited(
    con: Connection,
    row: RowMapping,
    kind: Literal["TRADE", "TRANSFER", "ACTION"],
    owner: str | None,
    *,
    latest: bool,
    previous: RowMapping | None,
) -> None:
    creation = _audit_entry(con, row, kind, "CREATE" if row["revision"] == 1 else "REVISE", owner)
    if kind == "ACTION":
        expected_reason = row["reason"]
    elif row["revision"] == 1 and kind == "TRANSFER":
        expected_reason = row["note"]
    elif row["revision"] == 1:
        expected_reason = row["override_reason"] or row["note"]
    else:
        expected_reason = None
    if expected_reason is not None and creation["reason"] != expected_reason:
        raise ValueError("Inconsistent audit reason")
    if (
        previous is not None
        and kind != "TRADE"
        and creation["before_sha256"] not in _predecessor_hashes(con, previous, kind)
    ):
        raise ValueError("Inconsistent predecessor audit")
    matching_statuses: set[str] = set()
    if kind == "TRADE" and (not latest or row["status"] == "VOID"):
        # A retained origin, when available, is verified separately with the retirement audit.
        hashes: set[str] | None = None
    elif kind == "TRADE":
        hashes = {digest(canonical(as_trade(row).model_dump(mode="json")))}
    elif kind == "TRANSFER":
        transfer_record = as_security(row).model_copy(
            update={
                "status": "ACTIVE",
                "is_current": True,
                "version": 1,
            }
        )
        hashes = {digest(canonical(transfer_record.model_dump(mode="json")))}
    else:
        action = as_action(action_row(con, row["id"]))
        statuses = ("CONFIRMED", "NEEDS_REVIEW") if row["status"] == "VOID" else (row["status"],)
        hashes = set()
        for status in statuses:
            if status == "CONFIRMED" and row["action_type"] in {
                "CASH_IN_LIEU",
                "MERGER",
                "SPIN_OFF",
            }:
                continue
            scope = "REVIEW_ONLY"
            if status == "CONFIRMED" and row["action_type"] in {"SPLIT", "REVERSE_SPLIT"}:
                scope = "QUANTITY_TERMS"
            elif (
                status == "CONFIRMED"
                and row["action_type"] == "SYMBOL_CHANGE"
                and row["previous_symbol"] is not None
            ):
                scope = "DISPLAY_ONLY"
            # Review items can resolve later, so either original flag may be valid.
            for reviewed in (False, True):
                record = action.model_copy(
                    update={
                        "status": status,
                        "lifecycle_status": "CURRENT",
                        "is_current": True,
                        "version": 1,
                        "effect_scope": scope,
                        "review_required": reviewed,
                    }
                )
                fingerprint = digest(canonical(record.model_dump(mode="json")))
                hashes.add(fingerprint)
                if fingerprint == creation["after_sha256"]:
                    matching_statuses.add(status)
    if hashes is not None and creation["after_sha256"] not in hashes:
        raise ValueError("Inconsistent fact audit")
    if latest and row["status"] == "VOID":
        void = _audit_entry(con, row, kind, "VOID", owner)
        if kind != "TRADE" and void["before_sha256"] not in _predecessor_hashes(
            con, row, kind, statuses=tuple(matching_statuses) if kind == "ACTION" else None
        ):
            raise ValueError("Inconsistent void predecessor audit")
        after = {"status": "VOID", "version": row["version"]}
        if void["after_sha256"] != digest(canonical(after)):
            raise ValueError("Inconsistent void audit")


def _chains(
    con: Connection,
    rows: Sequence[RowMapping],
    kind: Literal["TRADE", "TRANSFER", "ACTION"],
    owner: str | None,
) -> bool:
    """Reject incomplete histories and report retired trades without reconstructible origins."""
    groups: dict[str, list[RowMapping]] = {}
    unverified = False
    for row in rows:
        _sequence(row, kind)
        if (
            type(row["revision"]) is not int
            or not 1 <= row["revision"] <= INT64_MAX
            or type(row["version"]) is not int
            or not 1 <= row["version"] <= INT64_MAX
        ):
            raise ValueError("Invalid fact revision")
        groups.setdefault(row["root_id"], []).append(row)

    for root, history in groups.items():
        history.sort(key=lambda row: row["revision"])
        for number, row in enumerate(history, 1):
            previous = history[number - 2] if number > 1 else None
            latest = number == len(history)
            if (
                row["revision"] != number
                or (previous is None and (row["id"] != root or row["supersedes_id"] is not None))
                or (
                    previous is not None
                    and (
                        row["supersedes_id"] != previous["id"]
                        or row["created_sequence"] != previous["created_sequence"]
                    )
                )
            ):
                raise ValueError("Incomplete fact history")
            if kind == "TRADE":
                if (not latest and row["status"] != "SUPERSEDED") or (
                    latest
                    and row["status"] not in {"ACTIVE", "VOID", "DRAFT_SCHEDULED", "PENDING_FX"}
                ):
                    raise ValueError("Inconsistent trade lifecycle")
            else:
                if type(row["is_current"]) is not int or row["is_current"] != int(latest):
                    raise ValueError("Inconsistent current fact")
                statuses = (
                    {"ACTIVE", "VOID"}
                    if kind == "TRANSFER"
                    else {"CONFIRMED", "NEEDS_REVIEW", "VOID"}
                )
                if row["status"] not in statuses or (not latest and row["status"] == "VOID"):
                    raise ValueError("Inconsistent fact lifecycle")
            _audited(con, row, kind, owner, latest=latest, previous=previous)
            if kind == "TRADE":
                if owner is None:
                    raise ValueError("Missing trade owner")
                exists = con.execute(
                    text("SELECT 1 FROM trade_audit_origins WHERE trade_id=:id"),
                    {"id": row["id"]},
                ).first()
                retired = not latest or row["status"] == "VOID"
                if exists is None:
                    unverified = unverified or retired
                    continue
                origin = trade_audit.get_origin(con, UUID(owner), UUID(row["id"]))
                if origin is None:
                    raise ValueError("Missing retained trade origin")
                if retired:
                    retirement_row = row if latest else history[number]
                    retirement_action = "VOID" if latest else "REVISE"
                    retirement = _audit_entry(
                        con, retirement_row, "TRADE", retirement_action, owner
                    )
                    if retirement["before_sha256"] != origin.before_sha256:
                        raise ValueError("Inconsistent trade retirement audit")
    return unverified


def _accounts(con: Connection, owner: str, portfolio: str) -> list[str]:
    rows = con.execute(
        text("""
        SELECT a.id,a.portfolio_id,b.owner_id AS broker_owner
        FROM accounts a LEFT JOIN brokers b ON b.id=a.broker_id
        WHERE a.portfolio_id=:portfolio ORDER BY a.id
    """),
        {"portfolio": portfolio},
    ).mappings()
    result: list[str] = []
    for row in rows:
        _canonical_uuid(row["id"])
        if row["portfolio_id"] != portfolio or row["broker_owner"] != owner:
            raise ValueError("Inconsistent account ownership")
        result.append(row["id"])
    return result


def _fact_scope(
    con: Connection, owner: str, portfolio: str, security: str, accounts: set[str]
) -> tuple[bool, bool]:
    # ponytail: scan relevant retained roots; add indexed checkpoints if measured histories grow.
    params = {"portfolio": portfolio, "security": security}
    trades = (
        con.execute(
            text("""
        SELECT t.*,p.owner_id AS portfolio_owner,a.portfolio_id AS account_portfolio,
               q.kind AS sequence_kind,q.fact_id AS sequence_root
        FROM trades t LEFT JOIN portfolios p ON p.id=t.portfolio_id
        LEFT JOIN accounts a ON a.id=t.account_id
        LEFT JOIN ledger_sequences q ON q.sequence=t.created_sequence
        WHERE t.root_id IN (
            SELECT s.root_id FROM trades s LEFT JOIN accounts sa ON sa.id=s.account_id
            WHERE s.security_id=:security
              AND (s.portfolio_id=:portfolio OR sa.portfolio_id=:portfolio)
        ) OR (t.security_id=:security
              AND (t.portfolio_id=:portfolio OR a.portfolio_id=:portfolio))
    """),
            params,
        )
        .mappings()
        .all()
    )
    for row in trades:
        _canonical_uuid(row["portfolio_id"])
        _canonical_uuid(row["account_id"])
        _canonical_uuid(row["security_id"])
        if (
            row["portfolio_owner"] != owner
            or row["account_portfolio"] != row["portfolio_id"]
            or (row["portfolio_id"] == portfolio and row["account_id"] not in accounts)
        ):
            raise ValueError("Inconsistent trade scope")
        _day(row["trade_date"])
    unverified_trades = _chains(con, trades, "TRADE", owner)

    transfers = (
        con.execute(
            text("""
        SELECT t.*,p.owner_id AS portfolio_owner,a.portfolio_id AS source_portfolio,
               b.portfolio_id AS target_portfolio,q.kind AS sequence_kind,q.fact_id AS sequence_root
        FROM security_transfers t LEFT JOIN portfolios p ON p.id=t.portfolio_id
        LEFT JOIN accounts a ON a.id=t.source_account_id
        LEFT JOIN accounts b ON b.id=t.target_account_id
        LEFT JOIN ledger_sequences q ON q.sequence=t.created_sequence
        WHERE t.root_id IN (
            SELECT s.root_id FROM security_transfers s
            LEFT JOIN accounts sa ON sa.id=s.source_account_id
            LEFT JOIN accounts sb ON sb.id=s.target_account_id
            WHERE s.security_id=:security AND (s.portfolio_id=:portfolio
                  OR sa.portfolio_id=:portfolio OR sb.portfolio_id=:portfolio)
        ) OR (t.security_id=:security AND (t.portfolio_id=:portfolio
              OR a.portfolio_id=:portfolio OR b.portfolio_id=:portfolio))
    """),
            params,
        )
        .mappings()
        .all()
    )
    for row in transfers:
        _canonical_uuid(row["portfolio_id"])
        _canonical_uuid(row["source_account_id"])
        _canonical_uuid(row["target_account_id"])
        _canonical_uuid(row["security_id"])
        if (
            row["portfolio_owner"] != owner
            or row["source_portfolio"] != row["portfolio_id"]
            or row["target_portfolio"] != row["portfolio_id"]
            or row["source_account_id"] == row["target_account_id"]
            or (
                row["portfolio_id"] == portfolio
                and (
                    row["source_account_id"] not in accounts
                    or row["target_account_id"] not in accounts
                )
            )
        ):
            raise ValueError("Inconsistent transfer scope")
        _day(row["transfer_date"])
    _chains(con, transfers, "TRANSFER", owner)
    return any(row["status"] in {"SUPERSEDED", "VOID"} for row in trades), unverified_trades


def _actions(con: Connection, security: str) -> None:
    rows = (
        con.execute(
            text("""
        SELECT c.*,q.kind AS sequence_kind,q.fact_id AS sequence_root
        FROM corporate_actions c LEFT JOIN ledger_sequences q ON q.sequence=c.created_sequence
        WHERE c.root_id IN (SELECT root_id FROM corporate_actions WHERE security_id=:security)
           OR c.security_id=:security
    """),
            {"security": security},
        )
        .mappings()
        .all()
    )
    for row in rows:
        if row["security_id"] != security:
            raise ValueError("Inconsistent action identity")
        _day(row["effective_date"])
        if row["action_type"] == "SYMBOL_CHANGE":
            continue
        if row["action_type"] not in {
            "SPLIT",
            "REVERSE_SPLIT",
            "CASH_IN_LIEU",
            "MERGER",
            "SPIN_OFF",
        }:
            raise ValueError("Invalid action type")
        numerator, denominator = row["ratio_num"], row["ratio_den"]
        if (numerator is None) != (denominator is None) or any(
            value is not None and (type(value) is not int or not 1 <= value <= INT64_MAX)
            for value in (numerator, denominator)
        ):
            raise ValueError("Invalid action ratio")
        if row["action_type"] in {"SPLIT", "REVERSE_SPLIT"}:
            if (
                numerator is None
                or denominator is None
                or (row["action_type"] == "SPLIT" and numerator <= denominator)
                or (row["action_type"] == "REVERSE_SPLIT" and numerator >= denominator)
            ):
                raise ValueError("Invalid action direction")
        elif row["status"] not in {"NEEDS_REVIEW", "VOID"}:
            raise ValueError("Unallocated action cannot be confirmed")
    _chains(con, rows, "ACTION", None)


def get(
    con: Connection,
    owner_id: UUID,
    portfolio_id: UUID,
    security_id: UUID,
    *,
    ex_date: date,
    as_of: date,
    rule: EligibilityRule,
) -> RecordedEligibilityBook | None:
    """Read one active owner's portfolio in the caller's transaction, without writes.

    Callers authenticate separately. A later economic writer must re-read current
    facts in its writer transaction rather than treating this snapshot as a grant.
    """
    if (
        not con.in_transaction()
        or not isinstance(owner_id, UUID)
        or not isinstance(portfolio_id, UUID)
        or not isinstance(security_id, UUID)
        or type(ex_date) is not date
        or type(as_of) is not date
        or not isinstance(rule, str)
        or rule not in ("ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW")
    ):
        raise EligibilityHistoryError("INVALID_ELIGIBILITY_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner_id)}
        ).first()
        is None
    ):
        raise EligibilityHistoryError("ELIGIBILITY_OWNER_UNAVAILABLE")
    portfolio = (
        con.execute(
            text("SELECT * FROM portfolios WHERE id=:id AND owner_id=:owner"),
            {"id": str(portfolio_id), "owner": str(owner_id)},
        )
        .mappings()
        .first()
    )
    if portfolio is None:
        return None
    if (
        con.execute(text("SELECT 1 FROM securities WHERE id=:id"), {"id": str(security_id)}).first()
        is None
    ):
        raise EligibilityHistoryError("ELIGIBILITY_SECURITY_UNAVAILABLE")
    try:
        revision = portfolio["data_revision"]
        if type(revision) is not int or not 0 <= revision <= INT64_MAX:
            raise ValueError("Invalid portfolio data revision")
        account_ids = _accounts(con, str(owner_id), str(portfolio_id))
        account_set = set(account_ids)
        retired_trades, unverified_trades = _fact_scope(
            con, str(owner_id), str(portfolio_id), str(security_id), account_set
        )
        _actions(con, str(security_id))
        events, unresolved = history_events(con, str(portfolio_id))
        for event in events:
            operation = event.operation
            if isinstance(operation, Trade) and operation.account_id not in account_set:
                raise ValueError("Inconsistent trade account")
            if isinstance(operation, Transfer) and (
                operation.source_account_id not in account_set
                or operation.target_account_id not in account_set
            ):
                raise ValueError("Inconsistent transfer accounts")
        selected = [event for event in events if event.operation.security_id == str(security_id)]
        pending = [
            PendingAction(f"action:{row['id']}", row["security_id"], _day(row["effective_date"]))
            for row in unresolved
            if row["security_id"] == str(security_id)
        ]
        accounts = recorded_eligibility(
            selected,
            pending,
            account_ids,
            security_id=str(security_id),
            ex_date=ex_date,
            as_of=as_of,
            rule=rule,
        )
        trade_scope: TradeHistoryAuditScope = (
            "NO_RETIRED_TRADES"
            if not retired_trades
            else "RETIRED_TRADE_HISTORY_UNVERIFIED"
            if unverified_trades
            else "RETIRED_TRADE_HASHES_MATCHED"
        )
        return RecordedEligibilityBook(
            owner_id,
            portfolio_id,
            security_id,
            revision,
            ex_date,
            as_of,
            rule,
            trade_scope,
            accounts,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise EligibilityHistoryError("ELIGIBILITY_HISTORY_UNAVAILABLE") from None
