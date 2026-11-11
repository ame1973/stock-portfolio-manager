"""Candidate-linked event read proofs using isolated synthetic source evidence."""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as writer
from spm_api.audit import now
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/events"


async def test_linked_event_reports_retained_source_assessment_not_current_adoption(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    decision = await client.post(
        "/api/v1/dividends/candidates", json=payload(candidate_book), headers=write_headers(client)
    )
    assert decision.status_code == 201, decision.text
    candidate = decision.json()
    intent = writer.EventIntent.model_validate(
        {
            "event_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "revision_no": 1,
            "kind": "CANDIDATE",
            "candidate_id": candidate["id"],
            "candidate_version": candidate["version"],
            "reason": "Synthetic candidate association, not independent market proof.",
        }
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(con, UUID(candidate_book["owner"]["id"]), intent, at=now(), request_id=uuid4())
    changed = await client.patch(
        f"/api/v1/corporate-actions/{candidate_book['action']['id']}",
        headers=version_headers(client, 1),
        json={**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"},
    )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    response = await client.get(f"{PATH}/{intent.event_id}")
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["candidate_revision_id"] == candidate["revision_id"]
    assert record["assessment"] == candidate["assessment"]
    assert record["independent_origin_groups"] == 1
    assert record["evidence"][0]["observation_id"] == str(candidate_book["observation_id"])
    assert record["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert record["document_verification"] == record["eligibility_scope"] == "NOT_ESTABLISHED"
    assert "private=fixture" not in response.text
    listed = await client.get(PATH)
    history = await client.get(f"{PATH}/{intent.event_id}/revisions")
    assert listed.status_code == history.status_code == 200
    assert listed.json()["items"][0] == record
    assert history.json()["items"][0]["assessment"] == candidate["assessment"]
    assert database_snapshot(engine) == before
