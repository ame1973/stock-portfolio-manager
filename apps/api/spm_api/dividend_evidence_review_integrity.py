"""Cross-check retained evidence reviews against the current database snapshot."""

from datetime import datetime

from sqlalchemy import Connection, text

import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_models as models
from spm_api.audit import canonical, stamp


def checked_rows(
    con: Connection, retained: reviews.EvidenceReviewHistory
) -> dict[str, models.EvidenceReviewRecord]:
    """Validate every retained row before a caller uses or filters the history."""
    rows = (
        con.execute(
            text("""
                SELECT r.*,rev.revision_no AS observed_revision_no,next.id AS successor_id
                FROM review_items r
                LEFT JOIN dividend_event_revisions rev
                  ON rev.id=json_extract(r.detail_json,'$.revision_id') AND rev.event_id=r.event_id
                LEFT JOIN dividend_event_revisions next
                  ON next.event_id=r.event_id AND next.revision_no=rev.revision_no+1
                WHERE r.event_id=:event AND r.kind='DIVIDEND_EVIDENCE'
                ORDER BY r.created_at,r.id
            """),
            {"event": str(retained.event_id)},
        )
        .mappings()
        .all()
    )
    if not isinstance(retained.items, tuple) or len(rows) != len(retained.items):
        raise ValueError("Inconsistent retained review count")
    records: dict[str, models.EvidenceReviewRecord] = {}
    for row, item in zip(rows, retained.items, strict=True):
        if (
            not isinstance(item, reviews.EvidenceReview)
            or not isinstance(item.created_at, datetime)
            or (item.resolved_at is not None and not isinstance(item.resolved_at, datetime))
        ):
            raise ValueError("Invalid retained review")
        identifier = str(item.id)
        detail = {
            "scope": "EVENT_EVIDENCE_REVIEW",
            "revision_id": str(item.revision_id),
            "conflicting_fields": list(item.conflicting_fields),
            "incomplete_reasons": list(item.incomplete_reasons),
        }
        successor = str(item.resolved_by_revision_id) if item.resolved_by_revision_id else None
        if (
            identifier in records
            or row["id"] != identifier
            or row["event_id"] != str(retained.event_id)
            or item.event_id != retained.event_id
            or row["account_id"] is not None
            or item.account_id is not None
            or row["portfolio_id"] != (str(item.portfolio_id) if item.portfolio_id else None)
            or row["observed_revision_no"] != item.revision_no
            or row["severity"] != item.severity
            or row["status"] != item.status
            or row["reason_code"] != item.reason_code
            or row["detail_json"] != canonical(detail)
            or row["created_at"] != stamp(item.created_at)
            or row["resolved_at"] != (stamp(item.resolved_at) if item.resolved_at else None)
            or (row["successor_id"] if item.status == "RESOLVED" else None) != successor
            or item.acknowledgement_scope != "NOT_ESTABLISHED"
        ):
            raise ValueError("Inconsistent retained review result")
        if item.status == "RESOLVED":
            if (
                item.revision_no >= retained.revision_no
                or item.revision_id == retained.revision_id
                or (item.revision_no + 1 == retained.revision_no)
                != (item.resolved_by_revision_id == retained.revision_id)
            ):
                raise ValueError("Inconsistent retained review revision")
        elif (item.revision_id, item.revision_no) != (
            retained.revision_id,
            retained.revision_no,
        ) or row["successor_id"] is not None:
            raise ValueError("Inconsistent active review revision")
        records[identifier] = models.EvidenceReviewRecord(
            id=item.id,
            event_id=item.event_id,
            revision_id=item.revision_id,
            revision_no=item.revision_no,
            portfolio_id=item.portfolio_id,
            status=item.status,
            severity=item.severity,
            reason_code=item.reason_code,
            conflicting_fields=list(item.conflicting_fields),
            incomplete_reasons=list(item.incomplete_reasons),
            created_at=item.created_at,
            resolved_at=item.resolved_at,
            resolved_by_revision_id=item.resolved_by_revision_id,
            acknowledgement_scope=item.acknowledgement_scope,
            scope="RETAINED_EVIDENCE_REVIEW_ONLY",
        )
    return records
