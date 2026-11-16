"""Bind a rejected revision to its explicitly audited immediate successor."""

from collections.abc import Mapping
from datetime import UTC
from typing import Annotated, Any
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field
from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.errors import Model


class ResolutionRecord(Model):
    """A retained linkage, not an authenticated owner or economic grant."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    owner_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    rejection_id: UUID = Field(repr=False)
    rejected_revision_id: UUID = Field(repr=False)
    successor_revision_id: UUID = Field(repr=False)
    successor_revision_no: events.Version
    reason: events.Reason = Field(repr=False)
    request_id: UUID = Field(repr=False)
    audit_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True)]


def _storage(record: ResolutionRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "owner_id": str(record.owner_id),
        "event_id": str(record.event_id),
        "rejection_id": str(record.rejection_id),
        "rejected_revision_id": str(record.rejected_revision_id),
        "successor_revision_id": str(record.successor_revision_id),
        "successor_revision_no": record.successor_revision_no,
        "reason": record.reason,
        "request_id": str(record.request_id),
        "audit_id": str(record.audit_id),
        "created_at": stamp(record.created_at.astimezone(UTC)),
    }


def _payload(
    values: Mapping[str, Any],
    successor: events.EventRevision,
    *,
    manual_override: bool,
    link: events.ResolutionLink,
) -> dict[str, object]:
    return {
        "resolution_id": values["id"],
        "decision": "RESOLVE_BY_SUCCESSOR",
        "owner_id": values["owner_id"],
        "event_id": values["event_id"],
        "rejection_id": values["rejection_id"],
        "rejected_revision_id": values["rejected_revision_id"],
        "successor_revision_id": values["successor_revision_id"],
        "successor_revision_no": values["successor_revision_no"],
        "successor_sha256": digest(
            canonical(events._revision_audit_values(successor, manual_override, link))
        ),
        "reason_sha256": digest(canonical(values["reason"])),
        "created_at": values["created_at"],
        "scope": "OWNER_REJECTION_RESOLUTION_ONLY",
    }


def _record(row: RowMapping) -> ResolutionRecord:
    return ResolutionRecord(
        id=events._uuid(row["id"]),
        owner_id=events._uuid(row["owner_id"]),
        event_id=events._uuid(row["event_id"]),
        rejection_id=events._uuid(row["rejection_id"]),
        rejected_revision_id=events._uuid(row["rejected_revision_id"]),
        successor_revision_id=events._uuid(row["successor_revision_id"]),
        successor_revision_no=row["successor_revision_no"],
        reason=row["reason"],
        request_id=events._uuid(row["request_id"]),
        audit_id=events._uuid(row["audit_id"]),
        created_at=events._time(row["created_at"]),
    )


def checked_link(
    con: Connection,
    previous: events.ReviewedEvent,
    successor: events.EventRevision,
    *,
    manual_override: bool,
    marker: RowMapping,
) -> events.ResolutionLink:
    """Validate one retained linkage without recursively reading the event chain."""
    rows = (
        con.execute(
            text("""
            SELECT * FROM dividend_rejection_resolutions
            WHERE successor_revision_id=:successor OR rejected_revision_id=:previous
        """),
            {"successor": str(successor.revision_id), "previous": str(previous.record.revision_id)},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise ValueError("Missing or ambiguous rejection resolution")
    row = rows[0]
    record = _record(row)
    link = events.ResolutionLink(record.id, record.rejection_id)
    rejected_rows = (
        con.execute(
            text("SELECT * FROM dividend_event_rejections WHERE id=:id"),
            {"id": str(record.rejection_id)},
        )
        .mappings()
        .all()
    )
    if len(rejected_rows) != 1:
        raise ValueError("Missing resolved rejection")
    rejected = rejections._checked(con, rejected_rows[0], previous)
    rejection_audits = con.execute(
        text("SELECT created_at FROM audit_events WHERE id=:id"), {"id": str(rejected.audit_id)}
    ).all()
    successor_audits = con.execute(
        text("""
        SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'
          AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'
    """),
        {"id": str(successor.revision_id)},
    ).all()
    if len(rejection_audits) != 1 or len(successor_audits) != 1:
        raise ValueError("Missing resolution predecessor or successor audit")
    if (
        any(row[key] != value for key, value in _storage(record).items())
        or record.owner_id != successor.owner_id
        or record.owner_id != previous.record.owner_id
        or record.event_id != successor.event_id
        or record.event_id != previous.record.event_id
        or record.rejection_id != rejected.id
        or record.rejected_revision_id != previous.record.revision_id
        or record.successor_revision_id != successor.revision_id
        or record.successor_revision_no != successor.revision_no
        or successor.revision_no != previous.record.revision_no + 1
        or record.request_id != successor.request_id
        or record.reason != successor.reason
        or record.created_at != successor.created_at
        or record.created_at < rejected.created_at
        or record.created_at < events._time(rejection_audits[0].created_at)
        or marker["id"] != str(record.audit_id)
        or marker["owner_id"] != str(record.owner_id)
        or marker["entity_type"] != "dividend_event_revision"
        or marker["entity_id"] != str(successor.revision_id)
        or marker["action"] != "DIVIDEND_EVENT_REJECTION_RESOLUTION"
        or marker["request_id"] != str(record.request_id)
        or marker["reason"] != record.reason
        or events._time(marker["created_at"]) < record.created_at
        or events._time(marker["created_at"]) < events._time(successor_audits[0].created_at)
        or marker["before_sha256"]
        != digest(canonical(rejections._payload(rejections._storage(rejected))))
        or marker["after_sha256"]
        != digest(
            canonical(
                _payload(_storage(record), successor, manual_override=manual_override, link=link)
            )
        )
    ):
        raise ValueError("Inconsistent rejection resolution")
    return link


def append(
    con: Connection,
    previous: events.ReviewedEvent,
    rejected: rejections.RejectionRecord,
    successor: events.EventRevision,
    *,
    manual_override: bool,
    link: events.ResolutionLink,
) -> None:
    """Append inside the caller's savepoint, before publishing its successor."""
    if (
        rejected.id != link.rejection_id
        or rejected.owner_id != previous.record.owner_id
        or rejected.event_id != previous.record.event_id
        or rejected.revision_id != previous.record.revision_id
        or successor.event_id != previous.record.event_id
        or successor.owner_id != previous.record.owner_id
        or successor.revision_no != previous.record.revision_no + 1
    ):
        raise ValueError("Invalid rejection resolution scope")
    values: dict[str, Any] = {
        "id": str(link.id),
        "owner_id": str(successor.owner_id),
        "event_id": str(successor.event_id),
        "rejection_id": str(rejected.id),
        "rejected_revision_id": str(previous.record.revision_id),
        "successor_revision_id": str(successor.revision_id),
        "successor_revision_no": successor.revision_no,
        "reason": successor.reason,
        "request_id": str(successor.request_id),
        "created_at": stamp(successor.created_at.astimezone(UTC)),
    }
    audit(
        con,
        str(successor.owner_id),
        "dividend_event_revision",
        str(successor.revision_id),
        "DIVIDEND_EVENT_REJECTION_RESOLUTION",
        request_id=str(successor.request_id),
        before=rejections._payload(rejections._storage(rejected)),
        after=_payload(values, successor, manual_override=manual_override, link=link),
        reason=successor.reason,
    )
    markers = con.execute(
        text("""
        SELECT id FROM audit_events WHERE entity_type='dividend_event_revision'
          AND entity_id=:id AND action='DIVIDEND_EVENT_REJECTION_RESOLUTION'
    """),
        {"id": str(successor.revision_id)},
    ).all()
    if len(markers) != 1:
        raise ValueError("Missing or duplicate rejection-resolution audit")
    values["audit_id"] = markers[0].id
    columns = ",".join(values)
    parameters = ",".join(":" + key for key in values)
    con.execute(
        text(f"INSERT INTO dividend_rejection_resolutions({columns}) VALUES ({parameters})"), values
    )
