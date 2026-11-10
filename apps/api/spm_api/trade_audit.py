"""Validate and retain original trade audit inputs inside caller-owned transactions."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, canonical, digest, stamp
from spm_api.decimal_types import INT64_MAX, INT64_MIN
from spm_api.master_service import invalidate_portfolio
from spm_api.trade_codec import as_trade
from spm_api.trade_models import TradeRecord

OriginalStatus = Literal["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX"]
CaptureMode = Literal["AT_CREATION", "BEFORE_RETIRE", "RETAINED_RESPONSE"]
CAPTURE_REASON = "Preserve the fingerprint-matched trade state before retirement."


class TradeAuditError(ValueError):
    """Fixed codes only; never include trade details or audit contents."""


@dataclass(frozen=True, slots=True)
class TradeAuditOrigin:
    """Reconstructible audit inputs, not HTTP authority or a financial adoption grant."""

    owner_id: UUID = field(repr=False)
    trade_id: UUID = field(repr=False)
    creation_audit_id: UUID = field(repr=False)
    original_status: OriginalStatus = field(repr=False)
    original_updated_at: str = field(repr=False)
    capture_mode: CaptureMode
    capture_request_id: UUID = field(repr=False)
    capture_audit_id: UUID | None = field(repr=False)
    captured_at: str = field(repr=False)
    before_sha256: str = field(repr=False)


def _identity(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("Invalid identity")
    result = UUID(value)
    if str(result) != value:
        raise ValueError("Invalid identity")
    return result


def _time(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or stamp(parsed.astimezone(UTC)) != value:
        raise ValueError("Invalid timestamp")
    return value


def _hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _writer(con: Connection) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise TradeAuditError("TRADE_AUDIT_WRITE_REQUIRED")


def _trade(con: Connection, owner: UUID, identifier: UUID) -> RowMapping | None:
    if not con.in_transaction() or not isinstance(owner, UUID) or not isinstance(identifier, UUID):
        raise TradeAuditError("INVALID_TRADE_AUDIT_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:owner AND is_active=1"), {"owner": str(owner)}
        ).first()
        is None
    ):
        raise TradeAuditError("TRADE_AUDIT_OWNER_UNAVAILABLE")
    row = (
        con.execute(
            text("""
            SELECT t.* FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
            WHERE t.id=:id AND p.owner_id=:owner
        """),
            {"id": str(identifier), "owner": str(owner)},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    for name in ("id", "root_id", "portfolio_id", "account_id", "security_id"):
        _identity(row[name])
    if row["supersedes_id"] is not None:
        _identity(row["supersedes_id"])
    if (
        type(row["revision"]) is not int
        or not 1 <= row["revision"] <= INT64_MAX
        or type(row["created_sequence"]) is not int
        or not 1 <= row["created_sequence"] <= INT64_MAX
        or type(row["version"]) is not int
        or (
            row["revision"] == 1
            and (row["id"] != row["root_id"] or row["supersedes_id"] is not None)
        )
        or (row["revision"] > 1 and (row["id"] == row["root_id"] or row["supersedes_id"] is None))
    ):
        raise ValueError("Invalid trade identity")
    original = row["status"] in ("ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX")
    retired = row["status"] in ("SUPERSEDED", "VOID")
    if not ((original and row["version"] == 1) or (retired and row["version"] == 2)):
        raise ValueError("Unreconstructible trade control")
    _time(row["created_at"])
    _time(row["updated_at"])
    if row["executed_at_utc"] is not None:
        _time(row["executed_at_utc"])
    for column in (
        "quantity_e8",
        "price_e8",
        "fee_micro",
        "tax_micro",
        "fx_rate_hkd_e12",
        "gross_micro",
        "settlement_micro",
        "booked_hkd_micro",
        "rounding_delta_micro",
    ):
        value = row[column]
        if value is not None and (type(value) is not int or not INT64_MIN <= value <= INT64_MAX):
            raise ValueError("Invalid scaled trade value")
    if any(
        type(row[name]) is not int or row[name] not in (0, 1)
        for name in ("settlement_override", "hkd_override")
    ):
        raise ValueError("Invalid trade flags")
    if (
        con.execute(
            text("""
        SELECT 1 FROM accounts a JOIN brokers b ON b.id=a.broker_id
        JOIN ledger_sequences q ON q.sequence=:sequence AND q.kind='TRADE' AND q.fact_id=:root
        WHERE a.id=:account AND a.portfolio_id=:portfolio AND b.owner_id=:owner
    """),
            {
                "account": row["account_id"],
                "portfolio": row["portfolio_id"],
                "owner": str(owner),
                "sequence": row["created_sequence"],
                "root": row["root_id"],
            },
        ).first()
        is None
    ):
        raise ValueError("Invalid trade scope")
    return row


def _creation(con: Connection, owner: UUID, trade: RowMapping) -> RowMapping:
    action = "CREATE" if trade["revision"] == 1 else "REVISE"
    rows = (
        con.execute(
            text("""
            SELECT * FROM audit_events WHERE entity_type='trade' AND entity_id=:id
            AND action=:action
        """),
            {"id": trade["id"], "action": action},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise ValueError("Invalid trade audit")
    entry = rows[0]
    _identity(entry["id"])
    _identity(entry["request_id"])
    _time(entry["created_at"])
    reason = entry["reason"]
    if (
        entry["owner_id"] != str(owner)
        or not isinstance(reason, str)
        or len(reason) > 1000
        or (
            action == "CREATE"
            and (
                entry["before_sha256"] is not None
                or reason != (trade["override_reason"] or trade["note"])
            )
        )
        or (
            action == "REVISE"
            and (not _hash(entry["before_sha256"]) or len(reason) < 10 or reason != reason.strip())
        )
        or not _hash(entry["after_sha256"])
    ):
        raise ValueError("Invalid trade audit metadata")
    return entry


def _payload(
    trade: RowMapping,
    creation: RowMapping,
    owner: UUID,
    status: str,
    updated: str,
    mode: Literal["BEFORE_RETIRE", "RETAINED_RESPONSE"] = "BEFORE_RETIRE",
    reason: str | None = None,
) -> dict[str, str]:
    payload = {
        "trade_id": trade["id"],
        "owner_id": str(owner),
        "creation_audit_id": creation["id"],
        "original_status": status,
        "original_updated_at": updated,
        "capture_mode": mode,
    }
    if mode == "RETAINED_RESPONSE":
        if not _reason(reason):
            raise ValueError("Invalid recovery reason")
        payload["retained_response_sha256"] = creation["after_sha256"]
        payload["reason_sha256"] = digest(canonical(reason))
    return payload


def _reason(value: object) -> bool:
    return (
        isinstance(value, str)
        and 10 <= len(value) <= 1000
        and value == value.strip()
        and "\0" not in value
    )


def _retirement(con: Connection, owner: UUID, trade: RowMapping, before_hash: str) -> None:
    successors = (
        con.execute(text("SELECT id FROM trades WHERE supersedes_id=:id"), {"id": trade["id"]})
        .scalars()
        .all()
    )
    if trade["status"] == "SUPERSEDED":
        if len(successors) != 1:
            raise ValueError("Invalid trade successor")
        successor = _trade(con, owner, _identity(successors[0]))
        if (
            successor is None
            or successor["root_id"] != trade["root_id"]
            or successor["created_sequence"] != trade["created_sequence"]
            or successor["revision"] != trade["revision"] + 1
            or _creation(con, owner, successor)["before_sha256"] != before_hash
        ):
            raise ValueError("Inconsistent trade successor")
    elif trade["status"] == "VOID":
        entries = (
            con.execute(
                text(
                    "SELECT * FROM audit_events WHERE entity_type='trade' "
                    "AND entity_id=:id AND action='VOID'"
                ),
                {"id": trade["id"]},
            )
            .mappings()
            .all()
        )
        if successors or len(entries) != 1:
            raise ValueError("Invalid trade void")
        entry = entries[0]
        _identity(entry["id"])
        _identity(entry["request_id"])
        _time(entry["created_at"])
        if (
            entry["owner_id"] != str(owner)
            or not _reason(entry["reason"])
            or entry["before_sha256"] != before_hash
            or entry["after_sha256"]
            != digest(canonical({"status": "VOID", "version": trade["version"]}))
        ):
            raise ValueError("Inconsistent trade void")
    else:
        raise ValueError("Trade is not retired")


def _invalidate_root(
    con: Connection,
    owner: UUID,
    trade: RowMapping,
    reason: Literal["TRADE_AUDIT_ORIGIN_RECOVERED", "TRADE_HISTORY_REVIEW_CHANGED"] = (
        "TRADE_AUDIT_ORIGIN_RECOVERED"
    ),
) -> None:
    rows = (
        con.execute(
            text("""
        SELECT t.portfolio_id,t.trade_date,p.owner_id,p.data_revision
        FROM trades t LEFT JOIN portfolios p ON p.id=t.portfolio_id WHERE t.root_id=:root
    """),
            {"root": trade["root_id"]},
        )
        .mappings()
        .all()
    )
    portfolios: set[str] = set()
    days: list[str] = []
    for row in rows:
        _identity(row["portfolio_id"])
        day = row["trade_date"]
        if (
            row["owner_id"] != str(owner)
            or type(row["data_revision"]) is not int
            or not 0 <= row["data_revision"] < INT64_MAX
            or not isinstance(day, str)
            or date.fromisoformat(day).isoformat() != day
        ):
            raise ValueError("Invalid trade-root scope")
        portfolios.add(row["portfolio_id"])
        days.append(day)
    if not portfolios:
        raise ValueError("Missing trade-root scope")
    for portfolio in sorted(portfolios):
        invalidate_portfolio(con, portfolio, trade["id"], reason, min(days))


def _validate(
    con: Connection, owner: UUID, trade: RowMapping, stored: RowMapping
) -> TradeAuditOrigin:
    creation = _creation(con, owner, trade)
    status, updated = stored["original_status"], _time(stored["original_updated_at"])
    if (
        stored["trade_id"] != trade["id"]
        or stored["owner_id"] != str(owner)
        or stored["creation_audit_id"] != creation["id"]
        or status not in ("ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX")
        or (
            trade["status"] not in ("SUPERSEDED", "VOID")
            and (trade["status"] != status or trade["updated_at"] != updated)
        )
    ):
        raise ValueError("Inconsistent original trade state")
    original = as_trade(trade).model_copy(
        update={"status": status, "updated_at": updated, "version": 1}
    )
    if digest(canonical(original.model_dump(mode="json"))) != creation["after_sha256"]:
        raise ValueError("Original trade audit does not match")
    before = dict(trade)
    before.update(status=status, updated_at=updated, version=1)
    request, captured = _identity(stored["capture_request_id"]), _time(stored["captured_at"])
    mode = stored["capture_mode"]
    capture_id: UUID | None = None
    if mode == "AT_CREATION":
        if (
            stored["capture_audit_id"] is not None
            or str(request) != creation["request_id"]
            or captured != creation["created_at"]
        ):
            raise ValueError("Inconsistent original capture")
    elif mode in ("BEFORE_RETIRE", "RETAINED_RESPONSE"):
        capture_id = _identity(stored["capture_audit_id"])
        action = (
            "TRADE_AUDIT_ORIGIN_CAPTURED"
            if mode == "BEFORE_RETIRE"
            else "TRADE_AUDIT_ORIGIN_RECOVERED"
        )
        captures = (
            con.execute(
                text("""
            SELECT * FROM audit_events WHERE entity_type='trade_audit_origin' AND entity_id=:id
            AND action=:action
        """),
                {"id": trade["id"], "action": action},
            )
            .mappings()
            .all()
        )
        if len(captures) != 1:
            raise ValueError("Missing capture audit")
        entry = captures[0]
        if (
            entry["id"] != str(capture_id)
            or entry["owner_id"] != str(owner)
            or entry["request_id"] != str(request)
            or entry["created_at"] != captured
            or (
                entry["reason"] != CAPTURE_REASON
                if mode == "BEFORE_RETIRE"
                else not _reason(entry["reason"])
            )
            or entry["before_sha256"] is not None
            or entry["after_sha256"]
            != digest(
                canonical(_payload(trade, creation, owner, status, updated, mode, entry["reason"]))
            )
        ):
            raise ValueError("Inconsistent capture audit")
        if mode == "RETAINED_RESPONSE":
            _retirement(con, owner, trade, digest(canonical(before)))
    else:
        raise ValueError("Unsupported capture mode")
    return TradeAuditOrigin(
        owner,
        UUID(trade["id"]),
        UUID(creation["id"]),
        status,
        updated,
        mode,
        request,
        capture_id,
        captured,
        digest(canonical(before)),
    )


def get_origin(con: Connection, owner: UUID, identifier: UUID) -> TradeAuditOrigin | None:
    """Read one owned origin; callers separately authenticate and recheck current-use scope."""
    try:
        trade = _trade(con, owner, identifier)
        if trade is None:
            return None
        stored = (
            con.execute(
                text("SELECT * FROM trade_audit_origins WHERE trade_id=:id"),
                {"id": str(identifier)},
            )
            .mappings()
            .first()
        )
        return _validate(con, owner, trade, stored) if stored is not None else None
    except TradeAuditError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise TradeAuditError("TRADE_AUDIT_INCONSISTENT") from None


def recover_from_response(
    con: Connection,
    owner: UUID,
    identifier: UUID,
    response: TradeRecord,
    request_id: UUID,
    reason: str,
) -> TradeAuditOrigin:
    """Retain a matched retired origin; the caller must authenticate and commit atomically."""
    _writer(con)
    if (
        not isinstance(request_id, UUID)
        or not isinstance(response, TradeRecord)
        or not _reason(reason)
    ):
        raise TradeAuditError("INVALID_TRADE_AUDIT_SCOPE")
    with con.begin_nested():
        return _recover_response(con, owner, identifier, response, request_id, reason)


def _recover_response(
    con: Connection,
    owner: UUID,
    identifier: UUID,
    response: TradeRecord,
    request_id: UUID,
    reason: str,
) -> TradeAuditOrigin:
    try:
        trade = _trade(con, owner, identifier)
        if trade is None:
            raise TradeAuditError("TRADE_AUDIT_NOT_FOUND")
        claimed = TradeRecord.model_validate(dict(response))
        if (
            trade["status"] not in ("SUPERSEDED", "VOID")
            or claimed.id != identifier
            or claimed.status not in ("ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX")
            or claimed.version != 1
        ):
            raise ValueError("Invalid retained response")
        updated = _time(claimed.updated_at)
        creation = _creation(con, owner, trade)
        expected = as_trade(trade).model_copy(
            update={"status": claimed.status, "updated_at": updated, "version": 1}
        )
        if (
            claimed != expected
            or digest(canonical(expected.model_dump(mode="json"))) != creation["after_sha256"]
        ):
            raise ValueError("Retained response does not match")
        before = dict(trade)
        before.update(status=claimed.status, updated_at=updated, version=1)
        _retirement(con, owner, trade, digest(canonical(before)))
        existing = get_origin(con, owner, identifier)
        if existing is not None:
            if (
                existing.capture_mode != "RETAINED_RESPONSE"
                or existing.capture_request_id != request_id
                or con.execute(
                    text("SELECT reason FROM audit_events WHERE id=:id"),
                    {"id": str(existing.capture_audit_id)},
                ).scalar_one()
                != reason
            ):
                raise TradeAuditError("TRADE_AUDIT_ORIGIN_EXISTS")
            return existing
        if (
            con.execute(
                text(
                    "SELECT 1 FROM audit_events WHERE entity_type='trade_audit_origin' "
                    "AND entity_id=:id AND action='TRADE_AUDIT_ORIGIN_RECOVERED'"
                ),
                {"id": str(identifier)},
            ).first()
            is not None
        ):
            raise ValueError("Unassociated recovery audit")
        audit(
            con,
            str(owner),
            "trade_audit_origin",
            str(identifier),
            "TRADE_AUDIT_ORIGIN_RECOVERED",
            request_id=str(request_id),
            after=_payload(
                trade, creation, owner, claimed.status, updated, "RETAINED_RESPONSE", reason
            ),
            reason=reason,
        )
        entry = (
            con.execute(
                text(
                    "SELECT id,created_at FROM audit_events WHERE entity_type='trade_audit_origin' "
                    "AND entity_id=:id AND action='TRADE_AUDIT_ORIGIN_RECOVERED'"
                ),
                {"id": str(identifier)},
            )
            .mappings()
            .one()
        )
        con.execute(
            text("""
            INSERT INTO trade_audit_origins(trade_id,owner_id,creation_audit_id,original_status,
                original_updated_at,capture_mode,capture_request_id,capture_audit_id,captured_at)
            VALUES (:trade,:owner,:creation,:status,:updated,'RETAINED_RESPONSE',
                    :request,:audit,:captured)
        """),
            {
                "trade": str(identifier),
                "owner": str(owner),
                "creation": creation["id"],
                "status": claimed.status,
                "updated": updated,
                "request": str(request_id),
                "audit": entry["id"],
                "captured": entry["created_at"],
            },
        )
        result = get_origin(con, owner, identifier)
        assert result is not None
        _invalidate_root(con, owner, trade)
        return result
    except TradeAuditError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise TradeAuditError("TRADE_AUDIT_INCONSISTENT") from None


def remember_created(
    con: Connection, owner: UUID, identifier: UUID, request_id: UUID
) -> TradeAuditOrigin:
    """Retain the just-created audited state in the same writer; not an authentication API."""
    _writer(con)
    if not isinstance(request_id, UUID):
        raise TradeAuditError("INVALID_TRADE_AUDIT_SCOPE")
    try:
        trade = _trade(con, owner, identifier)
        if trade is None:
            raise TradeAuditError("TRADE_AUDIT_NOT_FOUND")
        if trade["status"] in ("SUPERSEDED", "VOID"):
            raise ValueError("The original state is no longer current")
        creation = _creation(con, owner, trade)
        if creation["request_id"] != str(request_id):
            raise ValueError("Uncorrelated request")
        if digest(canonical(as_trade(trade).model_dump(mode="json"))) != creation["after_sha256"]:
            raise ValueError("Original audit mismatch")
        existing = get_origin(con, owner, identifier)
        if existing is not None:
            if existing.capture_mode != "AT_CREATION" or existing.capture_request_id != request_id:
                raise ValueError("Conflicting origin")
            return existing
        con.execute(
            text("""
            INSERT INTO trade_audit_origins(trade_id,owner_id,creation_audit_id,original_status,
                original_updated_at,capture_mode,capture_request_id,capture_audit_id,captured_at)
            VALUES (:trade,:owner,:creation,:status,:updated,'AT_CREATION',:request,NULL,:captured)
        """),
            {
                "trade": str(identifier),
                "owner": str(owner),
                "creation": creation["id"],
                "status": trade["status"],
                "updated": trade["updated_at"],
                "request": str(request_id),
                "captured": creation["created_at"],
            },
        )
        result = get_origin(con, owner, identifier)
        assert result is not None
        return result
    except TradeAuditError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise TradeAuditError("TRADE_AUDIT_INCONSISTENT") from None


def capture_before_retire(
    con: Connection, owner: UUID, identifier: UUID, request_id: UUID
) -> TradeAuditOrigin:
    """Audit and capture an existing verified current state before it is overwritten."""
    _writer(con)
    if not isinstance(request_id, UUID):
        raise TradeAuditError("INVALID_TRADE_AUDIT_SCOPE")
    try:
        trade = _trade(con, owner, identifier)
        if trade is None:
            raise TradeAuditError("TRADE_AUDIT_NOT_FOUND")
        if trade["status"] in ("SUPERSEDED", "VOID"):
            raise ValueError("The original state is no longer current")
        existing = get_origin(con, owner, identifier)
        if existing is not None:
            return existing
        creation = _creation(con, owner, trade)
        if digest(canonical(as_trade(trade).model_dump(mode="json"))) != creation["after_sha256"]:
            raise ValueError("Original audit mismatch")
        if (
            con.execute(
                text("""
            SELECT 1 FROM audit_events WHERE entity_type='trade_audit_origin' AND entity_id=:id
            AND action='TRADE_AUDIT_ORIGIN_CAPTURED'
        """),
                {"id": str(identifier)},
            ).first()
            is not None
        ):
            raise ValueError("Unassociated capture audit")
        audit(
            con,
            str(owner),
            "trade_audit_origin",
            str(identifier),
            "TRADE_AUDIT_ORIGIN_CAPTURED",
            request_id=str(request_id),
            after=_payload(trade, creation, owner, trade["status"], trade["updated_at"]),
            reason=CAPTURE_REASON,
        )
        entry = (
            con.execute(
                text("""
            SELECT id,created_at FROM audit_events
            WHERE entity_type='trade_audit_origin' AND entity_id=:id
            AND action='TRADE_AUDIT_ORIGIN_CAPTURED'
        """),
                {"id": str(identifier)},
            )
            .mappings()
            .one()
        )
        con.execute(
            text("""
            INSERT INTO trade_audit_origins(trade_id,owner_id,creation_audit_id,original_status,
                original_updated_at,capture_mode,capture_request_id,capture_audit_id,captured_at)
            VALUES (:trade,:owner,:creation,:status,:updated,'BEFORE_RETIRE',
                    :request,:audit,:captured)
        """),
            {
                "trade": str(identifier),
                "owner": str(owner),
                "creation": creation["id"],
                "status": trade["status"],
                "updated": trade["updated_at"],
                "request": str(request_id),
                "audit": entry["id"],
                "captured": entry["created_at"],
            },
        )
        result = get_origin(con, owner, identifier)
        assert result is not None
        return result
    except TradeAuditError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise TradeAuditError("TRADE_AUDIT_INCONSISTENT") from None
