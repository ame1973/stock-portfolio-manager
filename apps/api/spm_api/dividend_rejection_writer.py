"""Append explicit rejection assertions without authenticating or granting income."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_service as rejection
from spm_api.action_models import EvidenceUrl
from spm_api.audit import audit, now, stamp
from spm_api.db import savepoint
from spm_api.dividend_rejection_service import history
from spm_api.errors import Model
from spm_api.master_service import invalidate_portfolio
from spm_api.providers.base import CitationReference


class RejectionIntent(Model):
    """An explicit physical event pin; the caller is responsible for authorization."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    citation: CitationReference = Field(repr=False)
    source_url: EvidenceUrl | None = Field(default=None, repr=False)
    reason: events.Reason = Field(repr=False)


def _replay_matches(
    record: rejection.RejectionRecord, intent: RejectionIntent, request_id: UUID
) -> bool:
    return bool(
        record.id == intent.id
        and record.event_id == intent.event_id
        and record.revision_id == intent.revision_id
        and record.revision_no == intent.revision_no
        and record.citation == intent.citation
        and record.source_url == intent.source_url
        and record.reason == intent.reason
        and record.request_id == request_id
    )


def save(
    con: Connection, owner: UUID, intent: RejectionIntent, *, request_id: UUID
) -> rejection.RejectionRecord:
    """Append one audited assertion atomically; the caller must authenticate separately."""
    try:
        return _save(con, owner, intent, request_id=request_id)
    except rejection.RejectionError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE") from None


def _save(
    con: Connection, owner: UUID, intent: RejectionIntent, *, request_id: UUID
) -> rejection.RejectionRecord:
    rejection._writer(con, owner)
    try:
        if not isinstance(intent, RejectionIntent) or not isinstance(request_id, UUID):
            raise ValueError
        intent = RejectionIntent.model_validate(intent.model_dump(warnings="error"))
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise rejection.RejectionError("INVALID_DIVIDEND_REJECTION_SCOPE") from None

    with savepoint(con):
        retained = history(con, owner, intent.event_id)
        if retained is None:
            raise rejection.RejectionError("DIVIDEND_REJECTION_NOT_FOUND")
        for record in retained:
            if record.id == intent.id:
                if not _replay_matches(record, intent, request_id):
                    raise rejection.RejectionError("DIVIDEND_REJECTION_CONFLICT")
                return record
        if any(item.revision_id == intent.revision_id for item in retained):
            raise rejection.RejectionError("DIVIDEND_REVISION_ALREADY_REJECTED")
        if con.execute(
            text("SELECT 1 FROM dividend_event_rejections WHERE id=:id"), {"id": str(intent.id)}
        ).first():
            raise rejection.RejectionError("DIVIDEND_REJECTION_CONFLICT")
        if con.execute(
            text("""
            SELECT 1 FROM audit_events WHERE entity_type='dividend_event_rejection'
              AND entity_id=:id LIMIT 1
        """),
            {"id": str(intent.id)},
        ).first():
            raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        reviewed = events.get(con, owner, intent.event_id)
        if reviewed is None:
            raise rejection.RejectionError("DIVIDEND_REJECTION_NOT_FOUND")
        if (
            reviewed.record.revision_id != intent.revision_id
            or reviewed.record.revision_no != intent.revision_no
        ):
            raise rejection.RejectionError("DIVIDEND_REJECTION_VERSION_CONFLICT")
        source_audits = con.execute(
            text("""
            SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision'
              AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'
        """),
            {"id": str(intent.revision_id)},
        ).all()
        if len(source_audits) != 1:
            raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        at = now()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        at = at.astimezone(UTC)
        if at < reviewed.record.created_at or at < events._time(source_audits[0].created_at):
            raise rejection.RejectionError("DIVIDEND_REJECTION_TIME_CONFLICT")
        values: dict[str, Any] = {
            "id": str(intent.id),
            "owner_id": str(owner),
            "event_id": str(intent.event_id),
            "revision_id": str(intent.revision_id),
            "revision_no": intent.revision_no,
            "citation": intent.citation,
            "source_url": intent.source_url,
            "reason": intent.reason,
            "request_id": str(request_id),
            "created_at": stamp(at),
        }
        audit(
            con,
            str(owner),
            "dividend_event_rejection",
            str(intent.id),
            "DIVIDEND_EVENT_REVISION_REJECTED",
            request_id=str(request_id),
            before=rejection._source_values(reviewed),
            after=rejection._payload(values),
            reason=intent.reason,
        )
        entries = con.execute(
            text("""
            SELECT id FROM audit_events WHERE entity_type='dividend_event_rejection'
              AND entity_id=:id
        """),
            {"id": str(intent.id)},
        ).all()
        if len(entries) != 1:
            raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        values["audit_id"] = entries[0].id
        columns = ",".join(values)
        parameters = ",".join(":" + key for key in values)
        con.execute(
            text(f"INSERT INTO dividend_event_rejections({columns}) VALUES ({parameters})"), values
        )
        day = reviewed.record.terms.ex_date
        if day is None:
            raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        for portfolio in event_writer._portfolios(con, owner, reviewed.record.terms.security_id):
            invalidate_portfolio(con, portfolio, str(intent.id), "DIVIDEND_REVISION_REJECTED", day)
        result = history(con, owner, intent.event_id)
        if result is None or len(result) != len(retained) + 1 or result[-1].id != intent.id:
            raise rejection.RejectionError("DIVIDEND_REJECTION_HISTORY_UNAVAILABLE")
        return result[-1]
