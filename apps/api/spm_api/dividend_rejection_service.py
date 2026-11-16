"""Read owned rejection history without authenticating or granting financial use."""

from collections.abc import Mapping
from datetime import UTC
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
from spm_api.action_models import EvidenceUrl
from spm_api.audit import canonical, digest, stamp
from spm_api.errors import Model
from spm_api.providers.base import CitationReference


class RejectionError(ValueError):
    """Fixed codes only; never expose private references or retained decisions."""


class RejectionRecord(Model):
    """A retained assertion, not an authenticated owner or economic decision."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    owner_id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    citation: CitationReference = Field(repr=False)
    source_url: EvidenceUrl | None = Field(repr=False)
    reason: events.Reason = Field(repr=False)
    request_id: UUID = Field(repr=False)
    audit_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True)]
    decision: Literal["REJECT_REVISION"] = "REJECT_REVISION"
    scope: Literal["OWNER_REVISION_REJECTION_ONLY"] = "OWNER_REVISION_REJECTION_ONLY"


def _scope(con: Connection, owner: UUID, event_id: UUID) -> None:
    if not con.in_transaction() or not isinstance(owner, UUID) or not isinstance(event_id, UUID):
        raise RejectionError("INVALID_DIVIDEND_REJECTION_SCOPE")


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise RejectionError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise RejectionError("INVALID_DIVIDEND_REJECTION_SCOPE")
    try:
        active = con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
    except SQLAlchemyError:
        raise RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE") from None
    if active is None:
        raise RejectionError("DIVIDEND_REJECTION_OWNER_UNAVAILABLE")


def _source_values(reviewed: events.ReviewedEvent) -> dict[str, object]:
    return events._revision_audit_values(
        reviewed.record, reviewed.manual_override, reviewed.resolution
    )


def _storage(record: RejectionRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "owner_id": str(record.owner_id),
        "event_id": str(record.event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "citation": record.citation,
        "source_url": record.source_url,
        "reason": record.reason,
        "request_id": str(record.request_id),
        "audit_id": str(record.audit_id),
        "created_at": stamp(record.created_at.astimezone(UTC)),
    }


def _payload(values: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rejection_id": values["id"],
        "decision": "REJECT_REVISION",
        "owner_id": values["owner_id"],
        "event_id": values["event_id"],
        "revision_id": values["revision_id"],
        "revision_no": values["revision_no"],
        "reference_sha256": digest(
            canonical({"citation": values["citation"], "source_url": values["source_url"]})
        ),
        "reason_sha256": digest(canonical(values["reason"])),
        "created_at": values["created_at"],
        "scope": "OWNER_REVISION_REJECTION_ONLY",
    }


def _record(row: RowMapping) -> RejectionRecord:
    return RejectionRecord(
        id=events._uuid(row["id"]),
        owner_id=events._uuid(row["owner_id"]),
        event_id=events._uuid(row["event_id"]),
        revision_id=events._uuid(row["revision_id"]),
        revision_no=row["revision_no"],
        citation=row["citation"],
        source_url=row["source_url"],
        reason=row["reason"],
        request_id=events._uuid(row["request_id"]),
        audit_id=events._uuid(row["audit_id"]),
        created_at=events._time(row["created_at"]),
    )


def _checked(con: Connection, row: RowMapping, reviewed: events.ReviewedEvent) -> RejectionRecord:
    record = _record(row)
    source_audits = con.execute(
        text("""
        SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'
          AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'
    """),
        {"id": str(reviewed.record.revision_id)},
    ).all()
    audits = (
        con.execute(
            text("""
            SELECT * FROM audit_events WHERE entity_type='dividend_event_rejection'
              AND entity_id=:id
        """),
            {"id": str(record.id)},
        )
        .mappings()
        .all()
    )
    if len(source_audits) != 1 or len(audits) != 1:
        raise ValueError("Missing or duplicate rejection audit")
    entry = audits[0]
    if (
        any(row[key] != value for key, value in _storage(record).items())
        or record.owner_id != reviewed.record.owner_id
        or record.event_id != reviewed.record.event_id
        or record.revision_id != reviewed.record.revision_id
        or record.revision_no != reviewed.record.revision_no
        or record.created_at < reviewed.record.created_at
        or record.created_at < events._time(source_audits[0].created_at)
        or entry["id"] != str(record.audit_id)
        or entry["owner_id"] != str(record.owner_id)
        or entry["action"] != "DIVIDEND_EVENT_REVISION_REJECTED"
        or entry["request_id"] != str(record.request_id)
        or entry["reason"] != record.reason
        or events._time(entry["created_at"]) < record.created_at
        or entry["before_sha256"] != digest(canonical(_source_values(reviewed)))
        or entry["after_sha256"] != digest(canonical(_payload(_storage(record))))
    ):
        raise ValueError("Inconsistent rejection history")
    return record


def history(con: Connection, owner: UUID, event_id: UUID) -> tuple[RejectionRecord, ...] | None:
    """Read complete rejection and successor history in one snapshot, not an HTTP grant."""
    _scope(con, owner, event_id)
    try:
        reviewed = events.get(con, owner, event_id)
        if reviewed is None:
            return None
        rows = (
            con.execute(
                text("""
                SELECT rejected.* FROM dividend_event_rejections rejected
                LEFT JOIN dividend_event_revisions revision ON revision.id=rejected.revision_id
                WHERE rejected.event_id=:event OR revision.event_id=:event
                ORDER BY rejected.revision_no,rejected.id
            """),
                {"event": str(event_id)},
            )
            .mappings()
            .all()
        )
        result: list[RejectionRecord] = []
        identities: set[UUID] = set()
        versions: set[int] = set()
        # ponytail: quadratic full-chain reads; batch verified history if it grows.
        for row in rows:
            number = row["revision_no"]
            if type(number) is not int or not 1 <= number <= reviewed.record.revision_no:
                raise ValueError("Invalid rejected revision")
            prior = events.get(con, owner, event_id, version=number)
            if prior is None:
                raise ValueError("Missing rejected revision")
            record = _checked(con, row, prior)
            if record.id in identities or number in versions:
                raise ValueError("Duplicate rejection identity or revision")
            identities.add(record.id)
            versions.add(number)
            if number < reviewed.record.revision_no:
                successor = events.get(con, owner, event_id, version=number + 1)
                if (
                    successor is None
                    or successor.resolution is None
                    or successor.resolution.rejection_id != record.id
                ):
                    raise ValueError("Missing immediate rejection resolution")
            result.append(record)
        return tuple(result)
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE") from None


def require_clear(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    *,
    revision_id: UUID,
    version: int,
) -> None:
    """Recheck only rejection state now; callers still owe all other current-use checks."""
    _writer(con, owner)
    _scope(con, owner, event_id)
    if (
        not isinstance(revision_id, UUID)
        or type(version) is not int
        or not 1 <= version <= 2_147_483_647
    ):
        raise RejectionError("INVALID_DIVIDEND_REJECTION_SCOPE")
    try:
        reviewed = events.get(con, owner, event_id)
        if reviewed is None:
            raise RejectionError("DIVIDEND_REJECTION_NOT_FOUND")
        if reviewed.record.revision_id != revision_id or reviewed.record.revision_no != version:
            raise RejectionError("DIVIDEND_REJECTION_VERSION_CONFLICT")
        retained = history(con, owner, event_id)
        if retained is None:
            raise RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        if any(item.revision_id == revision_id for item in retained):
            raise RejectionError("DIVIDEND_REVISION_REJECTED")
    except RejectionError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE") from None
