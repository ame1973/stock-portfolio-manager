"""Validate audited decisions about unverified trade-history gaps."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

from spm_api import trade_audit
from spm_api.audit import audit, canonical, digest
from spm_api.decimal_types import INT64_MAX
from spm_api.trade_codec import as_trade

Decision = Literal["ACKNOWLEDGE_UNVERIFIED", "WITHDRAW"]
Gap = Literal["MISSING_CREATION_AUDIT", "MISSING_RETIRED_ORIGIN"]
Status = Literal["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID"]


class TradeReviewError(ValueError):
    """Fixed error codes, without trade data or private rationale."""


@dataclass(frozen=True, slots=True)
class Review:
    id: UUID = field(repr=False)
    trade_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    revision: int
    previous_review_id: UUID | None = field(repr=False)
    decision: Decision
    gap: Gap
    observed_status: Status
    observed_trade_version: int
    observed_updated_at: str = field(repr=False)
    snapshot_sha256: str = field(repr=False)
    request_id: UUID = field(repr=False)
    audit_id: UUID = field(repr=False)
    created_at: str = field(repr=False)
    reason: str = field(repr=False)
    scope: Literal["UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"] = "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"


def _writer(con: Connection) -> None:
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise TradeReviewError("TRADE_REVIEW_WRITE_REQUIRED")


def _payload(record: Review | Mapping[str, Any], reason: str | None = None) -> dict[str, Any]:
    if isinstance(record, Review):
        values: Mapping[str, Any] = {
            "id": str(record.id),
            "trade_id": str(record.trade_id),
            "owner_id": str(record.owner_id),
            "revision": record.revision,
            "previous_review_id": str(record.previous_review_id)
            if record.previous_review_id
            else None,
            "decision": record.decision,
            "gap": record.gap,
            "observed_status": record.observed_status,
            "observed_trade_version": record.observed_trade_version,
            "observed_updated_at": record.observed_updated_at,
            "snapshot_sha256": record.snapshot_sha256,
            "request_id": str(record.request_id),
        }
        reason = record.reason
    else:
        values = record
    if not trade_audit._reason(reason):
        raise ValueError("Invalid reason")
    return {
        **{
            key: values[key]
            for key in (
                "id",
                "trade_id",
                "owner_id",
                "revision",
                "previous_review_id",
                "decision",
                "gap",
                "observed_status",
                "observed_trade_version",
                "observed_updated_at",
                "snapshot_sha256",
                "request_id",
            )
        },
        "reason_sha256": digest(canonical(reason)),
        "scope": "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY",
    }


def _creation_if_present(con: Connection, owner: UUID, trade: RowMapping) -> RowMapping | None:
    action = "CREATE" if trade["revision"] == 1 else "REVISE"
    count = con.execute(
        text(
            "SELECT count(*) FROM audit_events WHERE entity_type='trade' "
            "AND entity_id=:id AND action=:action"
        ),
        {"id": trade["id"], "action": action},
    ).scalar_one()
    if count > 1:
        raise ValueError("Duplicate creation audit")
    if not count:
        return None
    creation = trade_audit._creation(con, owner, trade)
    if (
        trade["status"] not in ("SUPERSEDED", "VOID")
        and digest(canonical(as_trade(trade).model_dump(mode="json"))) != creation["after_sha256"]
    ):
        raise ValueError("Inconsistent current trade audit")
    return creation


def _retired_structure(con: Connection, owner: UUID, trade: RowMapping) -> None:
    successors = (
        con.execute(text("SELECT id FROM trades WHERE supersedes_id=:id"), {"id": trade["id"]})
        .scalars()
        .all()
    )
    if trade["status"] == "SUPERSEDED":
        if len(successors) != 1:
            raise ValueError("Invalid successor")
        successor = trade_audit._trade(con, owner, trade_audit._identity(successors[0]))
        if (
            successor is None
            or successor["root_id"] != trade["root_id"]
            or successor["revision"] != trade["revision"] + 1
            or successor["created_sequence"] != trade["created_sequence"]
        ):
            raise ValueError("Invalid successor scope")
        # An absent successor audit is a separate unresolved gap, not a matching fingerprint.
        _creation_if_present(con, owner, successor)
        trade_audit.get_origin(con, owner, UUID(successor["id"]))
    elif trade["status"] == "VOID":
        entries = (
            con.execute(
                text(
                    "SELECT before_sha256 FROM audit_events "
                    "WHERE entity_type='trade' AND entity_id=:id AND action='VOID'"
                ),
                {"id": trade["id"]},
            )
            .scalars()
            .all()
        )
        if len(entries) != 1 or not trade_audit._hash(entries[0]):
            raise ValueError("Invalid void audit")
        trade_audit._retirement(con, owner, trade, entries[0])
    else:
        raise ValueError("Not retired")


def _gap(con: Connection, owner: UUID, trade: RowMapping) -> Gap:
    origin = trade_audit.get_origin(con, owner, UUID(trade["id"]))
    if origin is not None:
        raise TradeReviewError("TRADE_REVIEW_NOT_REQUIRED")
    creation = _creation_if_present(con, owner, trade)
    retired = trade["status"] in ("SUPERSEDED", "VOID")
    if retired:
        _retired_structure(con, owner, trade)
    if creation is None:
        return "MISSING_CREATION_AUDIT"
    if retired:
        return "MISSING_RETIRED_ORIGIN"
    raise TradeReviewError("TRADE_REVIEW_NOT_REQUIRED")


def _snapshot(con: Connection, owner: UUID, trade: RowMapping, record: Review) -> None:
    old = dict(trade)
    current = (record.observed_status, record.observed_trade_version, record.observed_updated_at)
    now = (trade["status"], trade["version"], trade["updated_at"])
    if current != now:
        if record.observed_trade_version != 1 or trade["status"] not in ("SUPERSEDED", "VOID"):
            raise ValueError("Observed physical state changed")
        old.update(status=record.observed_status, version=1, updated_at=record.observed_updated_at)
        trade_audit._retirement(con, owner, trade, record.snapshot_sha256)
    elif trade["status"] in ("SUPERSEDED", "VOID"):
        _retired_structure(con, owner, trade)
    if digest(canonical(old)) != record.snapshot_sha256:
        raise ValueError("Observed snapshot differs")


def history(con: Connection, owner: UUID, identifier: UUID) -> tuple[Review, ...] | None:
    """Validate decision history, not HTTP authority or the entire trade chain."""
    try:
        trade = trade_audit._trade(con, owner, identifier)
        if trade is None:
            return None
        trade_audit.get_origin(con, owner, identifier)
        creation = _creation_if_present(con, owner, trade)
        rows = (
            con.execute(
                text("SELECT * FROM trade_history_reviews WHERE trade_id=:id ORDER BY revision"),
                {"id": str(identifier)},
            )
            .mappings()
            .all()
        )
        result: list[Review] = []
        for number, row in enumerate(rows, 1):
            previous = result[-1] if result else None
            if (
                type(row["revision"]) is not int
                or row["revision"] != number
                or row["owner_id"] != str(owner)
                or row["previous_review_id"] != (str(previous.id) if previous else None)
                or row["decision"] not in ("ACKNOWLEDGE_UNVERIFIED", "WITHDRAW")
                or row["gap"] not in ("MISSING_CREATION_AUDIT", "MISSING_RETIRED_ORIGIN")
                or (previous is None and row["decision"] != "ACKNOWLEDGE_UNVERIFIED")
                or (
                    previous is not None
                    and (
                        row["decision"] == previous.decision
                        or (row["decision"] == "WITHDRAW" and row["gap"] != previous.gap)
                    )
                )
            ):
                raise ValueError("Invalid review sequence")
            audits = (
                con.execute(
                    text(
                        "SELECT * FROM audit_events WHERE entity_type='trade_history_review' "
                        "AND entity_id=:id"
                    ),
                    {"id": row["id"]},
                )
                .mappings()
                .all()
            )
            if len(audits) != 1:
                raise ValueError("Invalid review audit")
            entry = audits[0]
            reason = entry["reason"]
            status = row["observed_status"]
            version = row["observed_trade_version"]
            if (
                not trade_audit._reason(reason)
                or status not in ("ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID")
                or type(version) is not int
                or not (
                    (version == 1 and status in ("ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX"))
                    or (version == 2 and status in ("SUPERSEDED", "VOID"))
                )
                or not trade_audit._hash(row["snapshot_sha256"])
            ):
                raise ValueError("Invalid review metadata")
            record = Review(
                trade_audit._identity(row["id"]),
                identifier,
                owner,
                number,
                trade_audit._identity(row["previous_review_id"]) if previous else None,
                cast(Decision, row["decision"]),
                cast(Gap, row["gap"]),
                cast(Status, status),
                version,
                trade_audit._time(row["observed_updated_at"]),
                row["snapshot_sha256"],
                trade_audit._identity(row["request_id"]),
                trade_audit._identity(row["audit_id"]),
                trade_audit._time(row["created_at"]),
                reason,
            )
            action = (
                "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED"
                if record.decision == "ACKNOWLEDGE_UNVERIFIED"
                else "TRADE_HISTORY_UNVERIFIED_WITHDRAWN"
            )
            if (
                entry["id"] != str(record.audit_id)
                or entry["owner_id"] != str(owner)
                or entry["request_id"] != str(record.request_id)
                or entry["action"] != action
                or entry["created_at"] != record.created_at
                or (previous is not None and previous.created_at > record.created_at)
                or entry["before_sha256"]
                != (digest(canonical(_payload(previous))) if previous else None)
                or entry["after_sha256"] != digest(canonical(_payload(record)))
            ):
                raise ValueError("Invalid correlated review audit")
            _snapshot(con, owner, trade, record)
            if creation is not None and record.observed_trade_version == 1:
                observed = as_trade(trade).model_copy(
                    update={
                        "status": record.observed_status,
                        "version": record.observed_trade_version,
                        "updated_at": record.observed_updated_at,
                    }
                )
                if digest(canonical(observed.model_dump(mode="json"))) != creation["after_sha256"]:
                    raise ValueError("Observed trade conflicts with creation audit")
            if record.gap == "MISSING_RETIRED_ORIGIN" and creation is None:
                raise ValueError("Missing historical creation audit")
            result.append(record)
        return tuple(result)
    except TradeReviewError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise TradeReviewError("TRADE_REVIEW_UNAVAILABLE") from None


def correction_requires_review(con: Connection, owner: UUID, identifier: UUID) -> bool:
    """Check the retained trade root; this does not grant a correction or verify ownership."""
    try:
        selected = trade_audit._trade(con, owner, identifier)
        if selected is None:
            raise ValueError("Missing trade")
        root = selected["root_id"]
        # ponytail: scan retained root history; add indexed checkpoints if measured histories grow.
        identifiers = con.execute(
            text("SELECT id FROM trades WHERE root_id=:root"), {"root": root}
        ).scalars()
        trades = []
        for item in identifiers:
            trade = trade_audit._trade(con, owner, trade_audit._identity(item))
            if trade is None:
                raise ValueError("Invalid trade-root owner")
            trades.append(trade)
        trades.sort(key=lambda item: item["revision"])
        if not trades or trades[-1]["id"] != str(identifier):
            raise ValueError("Invalid current trade")
        requires_review = False
        for index, trade in enumerate(trades):
            previous = trades[index - 1] if index else None
            latest = index == len(trades) - 1
            if (
                trade["root_id"] != root
                or trade["revision"] != index + 1
                or (
                    previous is None and (trade["id"] != root or trade["supersedes_id"] is not None)
                )
                or (
                    previous is not None
                    and (
                        trade["supersedes_id"] != previous["id"]
                        or trade["created_sequence"] != previous["created_sequence"]
                    )
                )
                or (not latest and trade["status"] != "SUPERSEDED")
                or (latest and trade["status"] == "SUPERSEDED")
            ):
                raise ValueError("Invalid trade-root history")
            creation = _creation_if_present(con, owner, trade)
            origin = trade_audit.get_origin(con, owner, trade_audit._identity(trade["id"]))
            reviews = history(con, owner, trade_audit._identity(trade["id"]))
            if reviews is None:
                raise ValueError("Invalid reviewed trade")
            requires_review |= creation is None or any(
                item.gap == "MISSING_CREATION_AUDIT" for item in reviews
            )
            if trade["status"] in ("SUPERSEDED", "VOID"):
                _retired_structure(con, owner, trade)
                successor_creation = (
                    _creation_if_present(con, owner, trades[index + 1]) if not latest else None
                )
                if origin is not None and (latest or successor_creation is not None):
                    trade_audit._retirement(con, owner, trade, origin.before_sha256)
        return requires_review
    except TradeReviewError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise TradeReviewError("TRADE_REVIEW_UNAVAILABLE") from None


def save(
    con: Connection,
    owner: UUID,
    identifier: UUID,
    *,
    review_id: UUID,
    request_id: UUID,
    expected_trade_version: int,
    expected_revision: int,
    decision: Decision,
    reason: str,
) -> Review:
    """Append one decision in a writer; the caller must authenticate and commit."""
    _writer(con)
    if (
        not isinstance(owner, UUID)
        or not isinstance(identifier, UUID)
        or not isinstance(review_id, UUID)
        or not isinstance(request_id, UUID)
        or type(expected_trade_version) is not int
        or expected_trade_version not in (1, 2)
        or type(expected_revision) is not int
        or not 0 <= expected_revision < INT64_MAX
        or decision not in ("ACKNOWLEDGE_UNVERIFIED", "WITHDRAW")
        or not trade_audit._reason(reason)
    ):
        raise TradeReviewError("INVALID_TRADE_REVIEW_SCOPE")
    with con.begin_nested():
        try:
            trade = trade_audit._trade(con, owner, identifier)
            if trade is None:
                raise TradeReviewError("TRADE_REVIEW_NOT_FOUND")
            reviews = history(con, owner, identifier)
            assert reviews is not None
            for item in reviews:
                if item.id == review_id:
                    if (
                        item.request_id != request_id
                        or item.revision != expected_revision + 1
                        or item.observed_trade_version != expected_trade_version
                        or item.decision != decision
                        or item.reason != reason
                    ):
                        raise TradeReviewError("TRADE_REVIEW_CONFLICT")
                    return item
            if any(item.request_id == request_id for item in reviews):
                raise TradeReviewError("TRADE_REVIEW_CONFLICT")
            if con.execute(
                text("SELECT 1 FROM trade_history_reviews WHERE id=:id"), {"id": str(review_id)}
            ).first():
                raise TradeReviewError("TRADE_REVIEW_CONFLICT")
            if con.execute(
                text(
                    "SELECT 1 FROM audit_events WHERE entity_type='trade_history_review' "
                    "AND entity_id=:id LIMIT 1"
                ),
                {"id": str(review_id)},
            ).first():
                raise TradeReviewError("TRADE_REVIEW_UNAVAILABLE")
            if len(reviews) != expected_revision or trade["version"] != expected_trade_version:
                raise TradeReviewError("TRADE_REVIEW_VERSION_CONFLICT")
            previous = reviews[-1] if reviews else None
            if (previous is None and decision != "ACKNOWLEDGE_UNVERIFIED") or (
                previous is not None and previous.decision == decision
            ):
                raise TradeReviewError("TRADE_REVIEW_DECISION_CONFLICT")
            gap = (
                _gap(con, owner, trade)
                if decision == "ACKNOWLEDGE_UNVERIFIED"
                else cast(Review, previous).gap
            )
            values = {
                "id": str(review_id),
                "trade_id": str(identifier),
                "owner_id": str(owner),
                "revision": expected_revision + 1,
                "previous_review_id": str(previous.id) if previous else None,
                "decision": decision,
                "gap": gap,
                "observed_status": trade["status"],
                "observed_trade_version": trade["version"],
                "observed_updated_at": trade["updated_at"],
                "snapshot_sha256": digest(canonical(dict(trade))),
                "request_id": str(request_id),
            }
            action = (
                "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED"
                if decision == "ACKNOWLEDGE_UNVERIFIED"
                else "TRADE_HISTORY_UNVERIFIED_WITHDRAWN"
            )
            audit(
                con,
                str(owner),
                "trade_history_review",
                str(review_id),
                action,
                request_id=str(request_id),
                before=_payload(previous) if previous else None,
                after=_payload(values, reason),
                reason=reason,
            )
            event = con.execute(
                text(
                    "SELECT id,created_at FROM audit_events "
                    "WHERE entity_type='trade_history_review' AND entity_id=:id AND action=:action"
                ),
                {"id": str(review_id), "action": action},
            ).one()
            if previous is not None and event.created_at < previous.created_at:
                raise TradeReviewError("TRADE_REVIEW_TIME_CONFLICT")
            values.update(audit_id=event.id, created_at=event.created_at)
            columns = ",".join(values)
            parameters = ",".join(":" + key for key in values)
            con.execute(
                text(f"INSERT INTO trade_history_reviews({columns}) VALUES ({parameters})"),
                values,
            )
            updated = history(con, owner, identifier)
            assert updated is not None and len(updated) == expected_revision + 1
            trade_audit._invalidate_root(con, owner, trade, "TRADE_HISTORY_REVIEW_CHANGED")
            return updated[-1]
        except TradeReviewError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
            raise TradeReviewError("TRADE_REVIEW_UNAVAILABLE") from None


def require_current(
    con: Connection, owner: UUID, identifier: UUID, *, trade_version: int
) -> Review:
    """Recheck one acknowledgment; callers must authenticate and validate full economic scope."""
    _writer(con)
    try:
        trade = trade_audit._trade(con, owner, identifier)
        if trade is None:
            raise TradeReviewError("TRADE_REVIEW_NOT_FOUND")
        reviews = history(con, owner, identifier)
        if type(trade_version) is not int or trade["version"] != trade_version:
            raise TradeReviewError("TRADE_REVIEW_VERSION_CONFLICT")
        if not reviews or reviews[-1].decision != "ACKNOWLEDGE_UNVERIFIED":
            raise TradeReviewError("TRADE_REVIEW_REQUIRED")
        try:
            current_gap = _gap(con, owner, trade)
        except TradeReviewError as error:
            if str(error) == "TRADE_REVIEW_NOT_REQUIRED":
                raise TradeReviewError("TRADE_REVIEW_CHANGED") from None
            raise
        if current_gap != reviews[-1].gap:
            raise TradeReviewError("TRADE_REVIEW_CHANGED")
        return reviews[-1]
    except TradeReviewError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise TradeReviewError("TRADE_REVIEW_UNAVAILABLE") from None
