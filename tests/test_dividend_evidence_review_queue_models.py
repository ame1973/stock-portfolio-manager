"""Cross-event evidence review wire models preserve per-event history boundaries."""

from copy import deepcopy
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

import spm_api.dividend_evidence_review_models as models
from tests.test_dividend_evidence_review_models import page as page


@pytest.fixture
def queue(page: dict[str, object]) -> dict[str, Any]:
    items = deepcopy(page["items"])
    assert isinstance(items, list)
    items[0]["current_revision_id"] = page["revision_id"]
    items[0]["current_revision_no"] = page["revision_no"]
    return {
        "items": items,
        "next_cursor": None,
        "scope": "RETAINED_EVIDENCE_REVIEW_QUEUE_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "document_verification": "NOT_ESTABLISHED",
    }


def test_queue_preserves_current_head_and_explicit_unverified_scope(queue: dict[str, Any]) -> None:
    result = models.EvidenceReviewQueuePage.model_validate(queue)
    assert result.items[0].current_revision_no == 2
    assert result.items[0].acknowledgement_scope == "NOT_ESTABLISHED"
    assert result.history_completeness == "UNVERIFIED"
    empty = models.EvidenceReviewQueuePage.model_validate({**queue, "items": []})
    assert not empty.items and empty.eligibility_scope == "NOT_ESTABLISHED"
    assert str(result.items[0].event_id) not in repr(result)


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "COMPLETE"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "AUTHORIZED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"document_verification": "VERIFIED"},
        {"next_cursor": "x" * 1501},
        {"private_field": "synthetic"},
    ],
)
def test_queue_rejects_unverified_scope_substitutions(
    queue: dict[str, Any], change: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate({**queue, **change})


def test_queue_rejects_duplicate_ids_bad_order_and_empty_continuation(
    queue: dict[str, Any],
) -> None:
    item = queue["items"][0]
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate({**queue, "items": [item, item]})
    later = {**item, "id": str(uuid4()), "created_at": "2026-11-01T00:00:01Z"}
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate({**queue, "items": [item, later]})
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate(
            {**queue, "items": [], "next_cursor": "cursor"}
        )


def test_queue_rejects_mixed_event_heads_and_bad_review_lineage(queue: dict[str, Any]) -> None:
    item = queue["items"][0]
    other = {
        **item,
        "id": str(uuid4()),
        "current_revision_id": str(uuid4()),
        "created_at": "2026-10-31T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate({**queue, "items": [item, other]})
    other = {
        **item,
        "id": str(uuid4()),
        "event_id": str(uuid4()),
        "created_at": "2026-10-31T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate({**queue, "items": [item, other]})
    with pytest.raises(ValidationError):
        models.EvidenceReviewQueuePage.model_validate(
            {**queue, "items": [{**item, "current_revision_no": 1}]}
        )


def test_queue_fails_closed_on_record_serialization_error(queue: dict[str, Any]) -> None:
    item = models.EvidenceReviewQueueRecord.model_validate(queue["items"][0])
    with (
        patch.object(
            models.EvidenceReviewQueueRecord,
            "model_dump",
            side_effect=PydanticSerializationError("synthetic private detail"),
        ),
        pytest.raises(ValidationError),
    ):
        models.EvidenceReviewQueuePage.model_validate({**queue, "items": [item]})
