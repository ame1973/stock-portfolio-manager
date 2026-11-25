"""Bind normalized rebuild intent without authenticating or publishing a revision."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic_core import PydanticSerializationError

import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api.audit import canonical, digest, stamp


def fingerprint(
    owner: UUID,
    previous_revision_id: UUID,
    previous_revision_no: int,
    decision: events.EventIntent,
    proposal: preview.RevisionProposal,
    *,
    request_id: UUID,
    at: datetime,
) -> str:
    """Return a domain-separated hash, not a source, scope or financial grant.

    The caller must separately validate the recorded observation, current sources,
    authorization and complete financial scope inside the intended writer transaction.
    """
    try:
        if (
            not all(isinstance(value, UUID) for value in (owner, previous_revision_id, request_id))
            or type(previous_revision_no) is not int
            or not 1 <= previous_revision_no < 2_147_483_647
            or not isinstance(decision, events.EventIntent)
            or not isinstance(proposal, preview.RevisionProposal)
            or not isinstance(at, datetime)
            or at.utcoffset() is None
        ):
            raise ValueError
        valid_decision = events.EventIntent.model_validate(decision.model_dump(warnings="error"))
        valid_proposal = preview.RevisionProposal.model_validate(
            proposal.model_dump(warnings="error")
        )
        if (
            not current._exact(valid_decision, decision)
            or not current._exact(valid_proposal, proposal)
            or valid_decision.revision_id == previous_revision_id
            or valid_decision.revision_no != previous_revision_no + 1
        ):
            raise ValueError
        if valid_decision.kind == "MANUAL":
            terms = valid_decision.terms
            if terms is None or (
                terms.ex_date,
                terms.dps,
                terms.currency,
                terms.adjustment_basis,
            ) != (
                valid_proposal.ex_date,
                valid_proposal.dps,
                valid_proposal.currency,
                valid_proposal.adjustment_basis,
            ):
                raise ValueError
        normalized = valid_proposal.model_dump(mode="json", warnings="error")
        normalized["accounts"] = sorted(normalized["accounts"], key=lambda row: row["account_id"])
        return digest(
            canonical(
                {
                    "scope": "DIVIDEND_REVISION_REBUILD_INTENT_ONLY",
                    "owner_id": str(owner),
                    "previous_revision_id": str(previous_revision_id),
                    "previous_revision_no": previous_revision_no,
                    "decision": valid_decision.model_dump(mode="json", warnings="error"),
                    "proposal": normalized,
                    "request_id": str(request_id),
                    "at": stamp(at.astimezone(UTC)),
                }
            )
        )
    except (ValueError, TypeError, AttributeError, OverflowError, PydanticSerializationError):
        raise rebuild.RebuildError("INVALID_DIVIDEND_REBUILD_INTENT") from None
