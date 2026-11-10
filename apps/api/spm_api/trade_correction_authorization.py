"""Validate retained unverified trade-correction evidence without granting new use."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from spm_api import trade_audit, trade_history_review
from spm_api.audit import canonical, digest
from spm_api.decimal_types import INT64_MAX


class CorrectionAuthorizationError(ValueError):
    """Fixed codes only; never include trade data or private reasons."""


@dataclass(frozen=True, slots=True)
class AuthorizationPin:
    trade_id: UUID = field(repr=False)
    trade_version: int
    review_id: UUID = field(repr=False)
    review_revision: int
    gap: Literal["MISSING_CREATION_AUDIT"] = "MISSING_CREATION_AUDIT"


@dataclass(frozen=True, slots=True)
class CorrectionAuthorization:
    id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    root_id: UUID = field(repr=False)
    trade_id: UUID = field(repr=False)
    trade_version: int
    operation: Literal["REVISE", "VOID"]
    result_trade_id: UUID = field(repr=False)
    pins: tuple[AuthorizationPin, ...] = field(repr=False)
    intent_sha256: str = field(repr=False)
    request_id: UUID = field(repr=False)
    audit_id: UUID = field(repr=False)
    created_at: str = field(repr=False)
    reason: str = field(repr=False)
    scope: Literal["UNVERIFIED_TRADE_CORRECTION_ONLY"] = "UNVERIFIED_TRADE_CORRECTION_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"


def _payload(row: Any, scope: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    if not trade_audit._reason(reason):
        raise ValueError("Invalid correction reason")
    return {
        **{
            key: row[key]
            for key in (
                "id",
                "owner_id",
                "root_id",
                "trade_id",
                "trade_version",
                "operation",
                "result_trade_id",
                "intent_sha256",
                "request_id",
            )
        },
        "scope": scope,
        "reason_sha256": digest(canonical(reason)),
        "scope_type": "UNVERIFIED_TRADE_CORRECTION_ONLY",
    }


def _pins(value: object) -> tuple[list[dict[str, Any]], tuple[AuthorizationPin, ...]]:
    if not isinstance(value, str):
        raise ValueError("Invalid scope")
    raw = json.loads(value)
    if not isinstance(raw, list) or not raw or canonical(raw) != value:
        raise ValueError("Invalid scope")
    pins = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"trade_id", "trade_version", "review_id", "review_revision", "gap"}
            or type(item["trade_version"]) is not int
            or item["trade_version"] not in (1, 2)
            or not isinstance(item["review_revision"], str)
            or item["gap"] != "MISSING_CREATION_AUDIT"
        ):
            raise ValueError("Invalid scope pin")
        revision = int(item["review_revision"])
        if str(revision) != item["review_revision"] or not 1 <= revision <= INT64_MAX:
            raise ValueError("Invalid scope revision")
        pins.append(
            AuthorizationPin(
                trade_audit._identity(item["trade_id"]),
                item["trade_version"],
                trade_audit._identity(item["review_id"]),
                revision,
            )
        )
    if len({pin.trade_id for pin in pins}) != len(pins):
        raise ValueError("Duplicate scope trade")
    return raw, tuple(pins)


def get(con: Connection, owner: UUID, identifier: UUID) -> CorrectionAuthorization | None:
    """Read one owned historical record; the caller must authenticate and validate current use."""
    if not con.in_transaction() or not isinstance(owner, UUID) or not isinstance(identifier, UUID):
        raise CorrectionAuthorizationError("INVALID_TRADE_CORRECTION_SCOPE")
    try:
        row = (
            con.execute(
                text(
                    "SELECT * FROM trade_correction_authorizations WHERE id=:id AND owner_id=:owner"
                ),
                {"id": str(identifier), "owner": str(owner)},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        root = trade_audit._identity(row["root_id"])
        target_id = trade_audit._identity(row["trade_id"])
        result_id = trade_audit._identity(row["result_trade_id"])
        request_id = trade_audit._identity(row["request_id"])
        audit_id = trade_audit._identity(row["audit_id"])
        created = trade_audit._time(row["created_at"])
        if (
            row["owner_id"] != str(owner)
            or type(row["trade_version"]) is not int
            or row["trade_version"] != 1
            or row["operation"] not in ("REVISE", "VOID")
            or not trade_audit._hash(row["intent_sha256"])
        ):
            raise ValueError("Invalid authorization metadata")
        raw_scope, pins = _pins(row["scope_json"])
        events = (
            con.execute(
                text("SELECT * FROM audit_events WHERE entity_type=:type AND entity_id=:id"),
                {"type": "trade_correction_authorization", "id": str(identifier)},
            )
            .mappings()
            .all()
        )
        if len(events) != 1:
            raise ValueError("Invalid authorization audit")
        event = events[0]
        reason = event["reason"]
        if (
            event["id"] != str(audit_id)
            or event["owner_id"] != str(owner)
            or event["request_id"] != str(request_id)
            or event["action"] != "TRADE_CORRECTION_UNVERIFIED"
            or event["created_at"] != created
            or event["before_sha256"] is not None
            or event["after_sha256"] != digest(canonical(_payload(row, raw_scope, reason)))
        ):
            raise ValueError("Invalid authorization audit")
        target = trade_audit._trade(con, owner, target_id)
        result = trade_audit._trade(con, owner, result_id)
        if (
            target is None
            or result is None
            or target["root_id"] != str(root)
            or result["root_id"] != str(root)
            or target["version"] != 2
        ):
            raise ValueError("Invalid authorization trade")
        gaps = trade_history_review._correction_gaps(con, owner, target_id, historical=True)
        expected = {
            trade_audit._identity(trade["id"])
            for trade, _ in gaps
            if trade["revision"] <= target["revision"]
        }
        if expected != {pin.trade_id for pin in pins}:
            raise ValueError("Incomplete historical scope")
        target_review: trade_history_review.Review | None = None
        previous_revision = 0
        for pin in pins:
            trade = trade_audit._trade(con, owner, pin.trade_id)
            if (
                trade is None
                or trade["root_id"] != str(root)
                or trade["revision"] <= previous_revision
                or trade["revision"] > target["revision"]
            ):
                raise ValueError("Invalid authorization scope")
            previous_revision = trade["revision"]
            reviews = trade_history_review.history(con, owner, pin.trade_id)
            if reviews is None:
                raise ValueError("Missing authorization review")
            selected = next(
                (item for item in reviews if item.revision == pin.review_revision), None
            )
            if (
                selected is None
                or selected.id != pin.review_id
                or selected.decision != "ACKNOWLEDGE_UNVERIFIED"
                or selected.gap != "MISSING_CREATION_AUDIT"
                or selected.created_at > created
            ):
                raise ValueError("Invalid authorization review")
            if pin.trade_id == target_id:
                if pin.trade_version != 1 or selected.observed_trade_version != 1:
                    raise ValueError("Invalid target scope")
                target_review = selected
            elif pin.trade_version != 2 or trade["status"] != "SUPERSEDED":
                raise ValueError("Invalid historical scope")
        origin = trade_audit.get_origin(con, owner, target_id)
        if target_review is not None:
            before = target_review.snapshot_sha256
        elif origin is not None:
            before = origin.before_sha256
        else:
            raise ValueError("Missing target provenance")
        if origin is not None and origin.before_sha256 != before:
            raise ValueError("Conflicting target provenance")
        trade_audit._retirement(con, owner, target, before)
        if row["operation"] == "REVISE":
            if (
                target["status"] != "SUPERSEDED"
                or result["supersedes_id"] != str(target_id)
                or result["revision"] != target["revision"] + 1
                or result["created_sequence"] != target["created_sequence"]
            ):
                raise ValueError("Invalid revised trade")
            trade_event = trade_audit._creation(con, owner, result)
            result_origin = trade_audit.get_origin(con, owner, result_id)
            if result_origin is None:
                raise ValueError("Missing revised trade origin")
            if result["status"] in ("SUPERSEDED", "VOID"):
                trade_audit._retirement(con, owner, result, result_origin.before_sha256)
        else:
            if target["status"] != "VOID" or result_id != target_id:
                raise ValueError("Invalid void trade")
            trade_event = (
                con.execute(
                    text(
                        "SELECT * FROM audit_events WHERE entity_type='trade' AND entity_id=:id "
                        "AND action='VOID'"
                    ),
                    {"id": str(target_id)},
                )
                .mappings()
                .one()
            )
        if (
            trade_event["request_id"] != str(request_id)
            or trade_event["reason"] != reason
            or trade_event["created_at"] > created
        ):
            raise ValueError("Invalid correction audit link")
        return CorrectionAuthorization(
            identifier,
            owner,
            root,
            target_id,
            row["trade_version"],
            row["operation"],
            result_id,
            pins,
            row["intent_sha256"],
            request_id,
            audit_id,
            created,
            reason,
        )
    except CorrectionAuthorizationError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise CorrectionAuthorizationError("TRADE_CORRECTION_UNAVAILABLE") from None
