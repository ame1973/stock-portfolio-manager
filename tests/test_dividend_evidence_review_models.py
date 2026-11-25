"""Strict wire contracts for retained dividend evidence-review history."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy import Engine

import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_models as models
from spm_api.db import unit_of_work
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT, next_intent, submit, terms
from tests.test_dividend_evidence_review_history import incomplete


@pytest.fixture
def page() -> dict[str, object]:
    event_id, current, prior, review_id = (str(uuid4()) for _ in range(4))
    return {
        "event_id": event_id,
        "revision_id": current,
        "revision_no": 2,
        "items": [
            {
                "id": review_id,
                "event_id": event_id,
                "revision_id": prior,
                "revision_no": 1,
                "portfolio_id": None,
                "status": "RESOLVED",
                "severity": "WARNING",
                "reason_code": "DIVIDEND_INCOMPLETE",
                "conflicting_fields": [],
                "incomplete_reasons": ["UNRESOLVED_ADJUSTMENT"],
                "created_at": "2026-11-01T00:00:00Z",
                "resolved_at": "2026-11-02T00:00:00Z",
                "resolved_by_revision_id": current,
                "acknowledgement_scope": "NOT_ESTABLISHED",
                "scope": "RETAINED_EVIDENCE_REVIEW_ONLY",
            }
        ],
        "next_cursor": None,
        "scope": "RETAINED_EVIDENCE_REVIEWS_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "document_verification": "NOT_ESTABLISHED",
    }


def test_models_accept_actual_recorded_review_and_resolution(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    successor = next_intent(intent, "MANUAL", terms=terms())
    submit(engine, successor, at=AT + timedelta(seconds=1))
    with unit_of_work(engine) as con:
        retained = reviews.history(con, OWNER, intent.event_id)
        assert retained is not None and len(retained.items) == 1
        item = asdict(retained.items[0])
        del item["account_id"]
        item["scope"] = "RETAINED_EVIDENCE_REVIEW_ONLY"
        result = models.EvidenceReviewPage.model_validate(
            {
                "event_id": retained.event_id,
                "revision_id": retained.revision_id,
                "revision_no": retained.revision_no,
                "items": [item],
                "next_cursor": None,
                "scope": retained.scope,
                "history_completeness": retained.history_completeness,
                "current_use_scope": retained.current_use_scope,
                "eligibility_scope": retained.eligibility_scope,
                "document_verification": "NOT_ESTABLISHED",
            }
        )
    assert result.items[0].revision_id == intent.revision_id
    assert result.items[0].resolved_by_revision_id == successor.revision_id
    assert result.items[0].acknowledgement_scope == "NOT_ESTABLISHED"


def test_valid_and_empty_page_make_no_completeness_or_acknowledgement_claim(
    page: dict[str, object],
) -> None:
    result = models.EvidenceReviewPage.model_validate(page)
    assert result.history_completeness == "UNVERIFIED"
    assert result.items[0].acknowledgement_scope == "NOT_ESTABLISHED"
    assert "private" not in repr(result).lower()
    empty = models.EvidenceReviewPage.model_validate({**page, "items": []})
    assert not empty.items and empty.eligibility_scope == "NOT_ESTABLISHED"


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "VERIFIED"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "AUTHORIZED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"document_verification": "VERIFIED"},
        {"revision_no": 0},
        {"revision_no": "2"},
        {"next_cursor": "x" * 1501},
        {"extra_private_field": "synthetic"},
    ],
)
def test_page_rejects_unsupported_claims_or_wire_types(
    page: dict[str, object], change: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate({**page, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": str(uuid4())},
        {"revision_no": 2},
        {"status": "OPEN"},
        {"status": "DISMISSED"},
        {"severity": "INFO"},
        {"reason_code": "UNKNOWN"},
        {"reason_code": "DIVIDEND_SOURCE_CONFLICT"},
        {"incomplete_reasons": []},
        {"incomplete_reasons": ["UNRESOLVED_ADJUSTMENT", "UNRESOLVED_ADJUSTMENT"]},
        {"conflicting_fields": ["dps"]},
        {"created_at": "2026-11-01T08:00:00+08:00"},
        {"acknowledgement_scope": "AUTHENTICATED"},
        {"scope": "FINANCIAL_GRANT"},
        {"resolved_at": None},
        {"resolved_by_revision_id": None},
        {"resolved_by_revision_id": str(uuid4())},
        {"created_at": "2026-11-03T00:00:00Z"},
        {"conflicting_fields": ["private-field"]},
        {"incomplete_reasons": ["private reason"]},
        {"account_id": str(uuid4())},
    ],
)
def test_rejects_mismatched_or_forged_review_rows(
    page: dict[str, object], change: dict[str, object]
) -> None:
    data = deepcopy(page)
    items = data["items"]
    assert isinstance(items, list)
    items[0].update(change)
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)


def test_historical_row_cannot_reuse_the_current_physical_revision(page: dict[str, object]) -> None:
    data = deepcopy(page)
    items = data["items"]
    assert isinstance(items, list)
    items[0]["revision_id"] = data["revision_id"]
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)


def test_active_row_must_match_the_current_physical_revision(page: dict[str, object]) -> None:
    data = deepcopy(page)
    item = data["items"][0]  # type: ignore[index]
    item.update(
        revision_id=data["revision_id"],
        revision_no=2,
        status="ACKNOWLEDGED",
        resolved_at=None,
        resolved_by_revision_id=None,
    )
    result = models.EvidenceReviewPage.model_validate(data)
    assert result.items[0].status == "ACKNOWLEDGED"
    assert result.items[0].acknowledgement_scope == "NOT_ESTABLISHED"
    item["revision_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)


def test_rejects_duplicate_ids_and_inconsistent_page_order(page: dict[str, object]) -> None:
    data = deepcopy(page)
    items = data["items"]
    assert isinstance(items, list)
    items.append(deepcopy(items[0]))
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)
    items[1]["id"] = str(uuid4())
    items[1]["created_at"] = "2026-11-01T00:00:01Z"
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "event_id",
        "revision_id",
        "revision_no",
        "items",
        "next_cursor",
        "scope",
        "history_completeness",
        "current_use_scope",
        "eligibility_scope",
        "document_verification",
    ],
)
def test_page_requires_every_scope_field(page: dict[str, object], field: str) -> None:
    data = deepcopy(page)
    del data[field]
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "event_id",
        "revision_id",
        "revision_no",
        "portfolio_id",
        "status",
        "severity",
        "reason_code",
        "conflicting_fields",
        "incomplete_reasons",
        "created_at",
        "resolved_at",
        "resolved_by_revision_id",
        "acknowledgement_scope",
        "scope",
    ],
)
def test_review_requires_every_field(page: dict[str, object], field: str) -> None:
    data = deepcopy(page)
    items = data["items"]
    assert isinstance(items, list)
    del items[0][field]
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate(data)


def test_conflict_keeps_explicit_fields_without_granting_source_verification(
    page: dict[str, object],
) -> None:
    data = deepcopy(page)
    items = data["items"]
    assert isinstance(items, list)
    items[0].update(
        reason_code="DIVIDEND_SOURCE_CONFLICT",
        conflicting_fields=["dps", "pay_date"],
        incomplete_reasons=[],
    )
    result = models.EvidenceReviewPage.model_validate(data)
    assert result.items[0].conflicting_fields == ["dps", "pay_date"]
    assert result.document_verification == "NOT_ESTABLISHED"


def test_unserializable_nested_review_uses_a_fixed_private_error(page: dict[str, object]) -> None:
    with (
        patch.object(
            models.EvidenceReviewRecord,
            "model_dump",
            side_effect=PydanticSerializationError("PRIVATE-SYNTHETIC-DETAIL"),
        ),
        pytest.raises(ValidationError) as caught,
    ):
        models.EvidenceReviewPage.model_validate(page)
    assert "PRIVATE-SYNTHETIC-DETAIL" not in str(caught.value)


def test_forged_nested_model_is_revalidated(page: dict[str, object]) -> None:
    valid = models.EvidenceReviewPage.model_validate(page)
    forged = valid.items[0].model_copy(update={"acknowledgement_scope": "AUTHENTICATED"})
    with pytest.raises(ValidationError):
        models.EvidenceReviewPage.model_validate({**page, "items": [forged]})
