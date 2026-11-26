"""Observe an explicit proposed event decision and its recorded financial scope."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_integrity as review_integrity
import spm_api.dividend_rejection_service as rejection_service
import spm_api.dividend_revision_ledger_scope as ledger_scope
import spm_api.dividend_revision_preview as revision_preview
import spm_api.dividend_revision_projection_scope as projection_scope
import spm_api.dividend_revision_receipt_scope as receipt_scope
import spm_api.dividend_revision_scope as revision_scope
from spm_api.dividend_account_scope import positive_version
from spm_api.dividend_evidence import DividendTerms
from spm_api.master_models import Currency, Security, SecurityStatus
from spm_api.master_service import as_security


class RebuildError(ValueError):
    """Use fixed errors without exposing recorded financial facts or owner decisions."""


@dataclass(frozen=True, slots=True)
class ActionReviewScope:
    """Retained action-review rows and their audits, without verifying either."""

    reviews: tuple[tuple[str | None, ...], ...] = field(repr=False)
    audits: tuple[tuple[str | None, ...], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SecurityScope:
    """Current catalogue values, not verified market identity or ownership."""

    id: UUID = field(repr=False)
    exchange_code: str
    display_ticker: str = field(repr=False)
    name: str = field(repr=False)
    security_type: str
    quote_currency: Currency
    isin: str | None = field(repr=False)
    status: SecurityStatus
    verified: bool
    version: int
    created_at: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RebuildObservation:
    """One recorded snapshot, not authorization, source verification or a write grant."""

    owner_id: UUID = field(repr=False)
    event: events.ReviewedEvent = field(repr=False)
    decision: event_writer.EventIntent = field(repr=False)
    preview: revision_preview.RevisionPreview = field(repr=False)
    candidate: candidates.ReviewedCandidate | None = field(repr=False)
    rejections: tuple[rejection_service.RejectionRecord, ...] = field(repr=False)
    evidence_reviews: reviews.EvidenceReviewHistory = field(repr=False)
    action_reviews: ActionReviewScope = field(repr=False)
    security: SecurityScope = field(repr=False)
    ledger: ledger_scope.RecordedLedgerScope = field(repr=False)
    receipts: receipt_scope.RecordedReceiptScope = field(repr=False)
    projections: projection_scope.RecordedProjectionScope = field(repr=False)
    scope: Literal["RECORDED_REBUILD_OBSERVATION_ONLY"] = "RECORDED_REBUILD_OBSERVATION_ONLY"
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    current_use_scope: Literal["REVALIDATION_REQUIRED"] = "REVALIDATION_REQUIRED"
    source_scope: Literal["CURRENT_SOURCE_NOT_REVALIDATED"] = "CURRENT_SOURCE_NOT_REVALIDATED"
    eligibility_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    payment_scope: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"


def _security_scope(con: Connection, security_id: UUID) -> SecurityScope:
    """Bind the current catalogue row without verifying an external security identity."""
    row = (
        con.execute(text("SELECT * FROM securities WHERE id=:id"), {"id": str(security_id)})
        .mappings()
        .first()
    )
    if (
        row is None
        or row["id"] != str(security_id)
        or type(row["verified"]) is not int
        or row["verified"] not in (0, 1)
        or not isinstance(row["created_at"], str)
    ):
        raise ValueError("Invalid current security scope")
    positive_version(row["version"])
    record = as_security(row)
    expected = {name: row[name] for name in Security.model_fields}
    expected.update(id=security_id, verified=bool(row["verified"]))
    if (
        not isinstance(record, Security)
        or record.__dict__.keys() != Security.model_fields.keys()
        or not isinstance(record.id, UUID)
        or type(record.version) is not int
        or type(record.verified) is not bool
        or record.model_dump(warnings="error") != expected
    ):
        raise ValueError("Inconsistent current security scope")
    return SecurityScope(**record.model_dump(warnings="error"), created_at=row["created_at"])


def _action_review_scope(con: Connection, security_id: UUID) -> ActionReviewScope:
    """Freeze only review rows linked to the security's recorded action roots.

    Action reviews affect action audit reconstruction even when recorded shares do not
    change. This is a raw comparison, not validation of the reviews or their audits.
    """
    linked = """
        EXISTS(SELECT 1 FROM corporate_actions c WHERE c.security_id=:security
            AND c.root_id=json_extract(r.detail_json,'$.action_root_id'))
    """
    parameters = {"security": str(security_id)}
    # ponytail: scan linked reviews; add a root index only if measured histories grow.
    review_rows = con.execute(
        text(
            "SELECT r.id,r.portfolio_id,r.event_id,r.account_id,r.kind,r.severity,r.status,"
            "r.reason_code,r.detail_json,r.created_at,r.resolved_at FROM review_items r "
            f"WHERE {linked} ORDER BY r.id"
        ),
        parameters,
    )
    audit_rows = con.execute(
        text(
            "SELECT a.id,a.owner_id,a.entity_type,a.entity_id,a.action,a.before_sha256,"
            "a.after_sha256,a.reason,a.request_id,a.created_at FROM audit_events a "
            "WHERE a.entity_type='review_item' AND EXISTS(SELECT 1 FROM review_items r "
            f"WHERE r.id=a.entity_id AND {linked}) ORDER BY a.id"
        ),
        parameters,
    )
    reviews = tuple(tuple(row) for row in review_rows)
    audits = tuple(tuple(row) for row in audit_rows)
    for rows in (reviews, audits):
        if (
            any(not isinstance(row[0], str) or not row[0] for row in rows)
            or len({row[0] for row in rows}) != len(rows)
            or any(
                value is not None and not isinstance(value, str) for row in rows for value in row
            )
        ):
            raise ValueError("Invalid action review rows")
    return ActionReviewScope(reviews, audits)


def _same_terms(
    terms: DividendTerms, security_id: UUID, proposal: revision_preview.RevisionProposal
) -> bool:
    return bool(
        terms.security_id == security_id
        and terms.ex_date == proposal.ex_date
        and terms.currency == proposal.currency
        and terms.adjustment_basis == proposal.adjustment_basis
        and terms.dps is not None
        and Decimal(terms.dps) == Decimal(proposal.dps)
    )


def _verify_preview(
    con: Connection,
    owner: UUID,
    event: events.EventRevision,
    proposal: revision_preview.RevisionProposal,
    result: revision_preview.RevisionPreview,
) -> None:
    snapshot = revision_scope.read(
        con,
        owner,
        event.event_id,
        ex_date=date.fromisoformat(proposal.ex_date),
        as_of=date.fromisoformat(proposal.as_of),
        rule=proposal.eligibility_rule,
    )
    if (
        not isinstance(result, revision_preview.RevisionPreview)
        or not isinstance(snapshot, revision_scope.RevisionScope)
        or result.snapshot != snapshot
        or result.proposal != proposal
        or result.scope != "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"
        or result.history_completeness != "UNVERIFIED"
        or result.current_use_scope != "REVALIDATION_REQUIRED"
        or result.eligibility_scope != "NOT_ESTABLISHED"
        or result.payment_scope != "OWNER_RECORDED_NOT_VERIFIED"
        or (
            snapshot.retained.event_id,
            snapshot.retained.revision_id,
            snapshot.retained.revision_no,
        )
        != (event.event_id, event.revision_id, event.revision_no)
        or snapshot.security_id != event.terms.security_id
    ):
        raise ValueError("Inconsistent recorded revision scope")
    estimates = {item.account_id: item for item in proposal.accounts}
    histories = {item.account_id: item for item in snapshot.retained.accounts}
    if len(estimates) != len(proposal.accounts) or len(histories) != len(
        snapshot.retained.accounts
    ):
        raise ValueError("Inconsistent recorded accounts")
    calculated = tuple(
        revision_preview._account(
            snapshot,
            portfolio,
            observation,
            histories.get(pin.account_id),
            estimates[pin.account_id],
            proposal,
        )
        for portfolio in snapshot.portfolios
        for pin, observation in zip(portfolio.accounts, portfolio.book.accounts, strict=True)
    )
    if result.accounts != calculated:
        raise ValueError("Inconsistent recorded comparisons")


def observe(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    decision: event_writer.EventIntent,
    proposal: revision_preview.RevisionProposal,
) -> RebuildObservation | None:
    """Bind explicit inputs in a read snapshot, without validating current-use authority.

    The proposed calculations are hypothetical, including for a withdrawal. No revision,
    projection, review task or financial fact is written, and a caller must authenticate
    and revalidate the complete scope inside its serialized transaction before any use.
    """
    if (
        not con.in_transaction()
        or not isinstance(owner, UUID)
        or not isinstance(event_id, UUID)
        or not isinstance(decision, event_writer.EventIntent)
        or not isinstance(proposal, revision_preview.RevisionProposal)
    ):
        raise RebuildError("INVALID_DIVIDEND_REBUILD_SCOPE")
    try:
        decision = event_writer.EventIntent.model_validate(decision.model_dump(warnings="error"))
        proposal = revision_preview.RevisionProposal.model_validate(
            proposal.model_dump(warnings="error")
        )
        if decision.event_id != event_id:
            raise RebuildError("INVALID_DIVIDEND_REBUILD_SCOPE")
        current = events.get(con, owner, event_id)
        parent = events._owned_parent(con, owner, event_id)
        if current is None and parent is None:
            return None
        if (
            not isinstance(current, events.ReviewedEvent)
            or not isinstance(current.record, events.EventRevision)
            or parent is None
            or current.record.owner_id != owner
            or current.record.event_id != event_id
            or current.record.revision_no != parent["version"]
            or current.record != events._record(con, parent, parent["version"])
            or current.assessment != events._lineage(con, current.record)
            or type(current.manual_override) is not bool
            or (
                current.resolution is not None
                and not isinstance(current.resolution, events.ResolutionLink)
            )
        ):
            raise ValueError("Inconsistent retained event")
        previous = (
            events._record(con, parent, parent["version"] - 1) if parent["version"] > 1 else None
        )
        events._audited(
            con,
            current.record,
            previous,
            manual_override=current.manual_override,
            resolution=current.resolution,
        )
        if decision.revision_no != current.record.revision_no + 1:
            raise RebuildError("DIVIDEND_REBUILD_SCOPE_CHANGED")
        if con.execute(
            text("SELECT 1 FROM dividend_event_revisions WHERE id=:id"),
            {"id": str(decision.revision_id)},
        ).first():
            raise RebuildError("DIVIDEND_EVENT_REVISION_CONFLICT")
        candidate = None
        if decision.kind == "CANDIDATE":
            assert decision.candidate_id is not None and decision.candidate_version is not None
            candidate = candidates.get(con, owner, decision.candidate_id)
            row = (
                con.execute(
                    text("""
                        SELECT * FROM dividend_candidate_revisions
                        WHERE owner_id=:owner AND candidate_id=:candidate
                        ORDER BY revision_no DESC LIMIT 1
                    """),
                    {"owner": str(owner), "candidate": str(decision.candidate_id)},
                )
                .mappings()
                .first()
            )
            if (candidate is None) != (row is None):
                raise ValueError("Inconsistent retained candidate")
            if candidate is None:
                raise RebuildError("CANDIDATE_NOT_FOUND")
            if (
                not isinstance(candidate, candidates.ReviewedCandidate)
                or not isinstance(candidate.record, candidates.StoredCandidate)
                or not isinstance(candidate.record.decision, candidates.CandidateDecision)
                or candidate.record.owner_id != owner
                or candidate.record.decision.candidate_id != decision.candidate_id
                or row is None
                or candidate != candidates._historical(con, row)
            ):
                raise ValueError("Inconsistent retained candidate")
            if candidate.record.decision.revision_no != decision.candidate_version:
                raise RebuildError("CANDIDATE_VERSION_CONFLICT")
            if candidate.record.decision.state != "OPEN":
                raise RebuildError("CANDIDATE_DISMISSED")
            terms = candidate.record.decision.terms
        elif decision.kind == "MANUAL":
            assert decision.terms is not None
            terms = decision.terms
        else:
            terms = current.record.terms
        if not _same_terms(terms, current.record.terms.security_id, proposal):
            raise RebuildError("DIVIDEND_REBUILD_PROPOSAL_MISMATCH")
        rejection_history = rejection_service.history(con, owner, event_id)
        evidence = reviews.history(con, owner, event_id)
        if rejection_history is None or evidence is None:
            raise ValueError("Missing retained decision history")
        rejection_rows = (
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
        if not isinstance(rejection_history, tuple) or rejection_history != tuple(
            rejection_service._record(row) for row in rejection_rows
        ):
            raise ValueError("Inconsistent retained rejection history")
        rejected = next(
            (item for item in rejection_history if item.revision_id == current.record.revision_id),
            None,
        )
        if rejected is not None and decision.resolve_rejection_id is None:
            raise RebuildError("DIVIDEND_REJECTION_CONFIRMATION_REQUIRED")
        if (rejected.id if rejected is not None else None) != decision.resolve_rejection_id:
            raise RebuildError("DIVIDEND_REJECTION_VERSION_CONFLICT")
        if (
            not isinstance(evidence, reviews.EvidenceReviewHistory)
            or (evidence.event_id, evidence.revision_id, evidence.revision_no)
            != (event_id, current.record.revision_id, current.record.revision_no)
            or evidence.scope != "RETAINED_EVIDENCE_REVIEWS_ONLY"
            or evidence.history_completeness != "UNVERIFIED"
            or evidence.current_use_scope != "REVALIDATION_REQUIRED"
            or evidence.eligibility_scope != "NOT_ESTABLISHED"
        ):
            raise ValueError("Inconsistent evidence review scope")
        review_integrity.checked_rows(con, evidence)
        proposed = revision_preview.read(con, owner, event_id, proposal)
        if proposed is None:
            raise ValueError("Missing recorded comparison")
        _verify_preview(con, owner, current.record, proposal, proposed)
        return RebuildObservation(
            owner,
            current,
            decision,
            proposed,
            candidate,
            rejection_history,
            evidence,
            _action_review_scope(con, current.record.terms.security_id),
            _security_scope(con, current.record.terms.security_id),
            ledger_scope.read(
                con,
                owner,
                current.record.terms.security_id,
                tuple(item.portfolio.id for item in proposed.snapshot.portfolios),
            ),
            receipt_scope.read(con, owner, event_id),
            projection_scope.read(
                con, owner, event_id, current.record.revision_id, current.record.revision_no
            ),
        )
    except RebuildError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise RebuildError("DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE") from None
