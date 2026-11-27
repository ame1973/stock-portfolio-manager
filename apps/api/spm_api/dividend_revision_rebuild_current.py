"""Revalidate a recorded revision observation inside a serialized writer."""

import sqlite3
from dataclasses import fields, is_dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as event_service
import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_ledger_scope as ledger_scope
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_projection_scope as projection_scope
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_receipt_scope as receipt_scope


def _exact(left: Any, right: Any) -> bool:
    """Compare snapshot values without equating floats, booleans and integer pins."""
    if type(left) is not type(right) or isinstance(left, float):
        return False
    if isinstance(left, BaseModel):
        declared = type(left).model_fields
        return (
            left == right
            and left.__dict__.keys() == declared.keys()
            and right.__dict__.keys() == declared.keys()
            and all(_exact(getattr(left, name), getattr(right, name)) for name in declared)
        )
    if is_dataclass(left) and not isinstance(left, type):
        return all(
            _exact(getattr(left, item.name), getattr(right, item.name)) for item in fields(left)
        )
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(
            _exact(a, b) for a, b in zip(left, right, strict=True)
        )
    # ponytail: map/set matching is quadratic; index typed keys if these bounds grow.
    if isinstance(left, dict):
        return len(left) == len(right) and all(
            any(_exact(key, other) and _exact(value, right[other]) for other in right)
            for key, value in left.items()
        )
    if isinstance(left, (set, frozenset)):
        return len(left) == len(right) and all(any(_exact(a, b) for b in right) for a in left)
    return bool(left == right)


def require_current(
    con: Connection,
    owner: UUID,
    decision: events.EventIntent,
    proposal: preview.RevisionProposal,
    observed: rebuild.RebuildObservation,
) -> rebuild.RebuildObservation:
    """Recheck a prospective operation; this does not authenticate or write a revision.

    Call again after any same-transaction change. Historical replay must be handled
    separately, before this current-scope check, without adopting an unrelated operation.
    """
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise rebuild.RebuildError("WRITE_UNIT_OF_WORK_REQUIRED")
    if (
        not isinstance(owner, UUID)
        or not isinstance(decision, events.EventIntent)
        or not isinstance(proposal, preview.RevisionProposal)
    ):
        raise rebuild.RebuildError("INVALID_DIVIDEND_REBUILD_SCOPE")
    if (
        not isinstance(observed, rebuild.RebuildObservation)
        or not isinstance(observed.preview, preview.RevisionPreview)
        or observed.owner_id != owner
        or observed.decision != decision
        or observed.preview.proposal != proposal
    ):
        raise rebuild.RebuildError("DIVIDEND_REBUILD_SCOPE_CHANGED")
    fresh = rebuild.observe(con, owner, decision.event_id, decision, proposal)
    if fresh is None or not _exact(fresh, observed):
        raise rebuild.RebuildError("DIVIDEND_REBUILD_SCOPE_CHANGED")
    return fresh


def require_preserved(con: Connection, owner: UUID, observed: rebuild.RebuildObservation) -> None:
    """Compare retained raw inputs after a possible head change, not successor authority.

    This read-only check does not validate the new head, account set, source, rejection,
    replacement results or actor. It must not be used for historical replay or as a grant.
    """
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise rebuild.RebuildError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID) or not isinstance(observed, rebuild.RebuildObservation):
        raise rebuild.RebuildError("INVALID_DIVIDEND_REBUILD_SCOPE")
    if (
        not isinstance(observed.event, event_service.ReviewedEvent)
        or not isinstance(observed.event.record, event_service.EventRevision)
        or observed.owner_id != owner
        or observed.event.record.owner_id != owner
    ):
        raise rebuild.RebuildError("DIVIDEND_REBUILD_SCOPE_CHANGED")
    try:
        raw = con.connection.driver_connection
        if type(raw) is not sqlite3.Connection:
            raise ValueError("Unsupported recorded scope connection")
        # A reentrant callback can write after an earlier row was read in this transaction.
        changes = raw.total_changes
        record = observed.event.record
        security = record.terms.security_id
        actual = (
            rebuild._security_scope(con, security),
            rebuild._action_review_scope(con, security),
            ledger_scope.read(con, owner, security, observed.ledger.portfolio_ids),
            receipt_scope.read(con, owner, record.event_id),
            projection_scope.read(
                con, owner, record.event_id, record.revision_id, record.revision_no
            ),
        )
        expected = (
            observed.security,
            observed.action_reviews,
            observed.ledger,
            observed.receipts,
            observed.projections,
        )
        if not _exact(actual, expected) or raw.total_changes != changes:
            raise rebuild.RebuildError("DIVIDEND_REBUILD_SCOPE_CHANGED")
    except rebuild.RebuildError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, SQLAlchemyError):
        raise rebuild.RebuildError("DIVIDEND_REBUILD_SCOPE_CHANGED") from None
