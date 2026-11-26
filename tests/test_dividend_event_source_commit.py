"""A protected event decision must recheck the candidate it used before commit."""

from contextlib import nullcontext
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
from spm_api.audit import now
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload as candidate_payload
from tests.test_dividend_verification_api import body as accept_body
from tests.test_dividend_verification_api import endpoint as accept_endpoint
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("operation", ["adopt", "accept"])
async def test_late_candidate_revision_cannot_be_adopted_or_accepted(
    client: httpx.AsyncClient,
    engine: Engine,
    candidate_book: dict[str, Any],
    operation: str,
) -> None:
    candidate_response = await client.post(
        "/api/v1/dividends/candidates",
        json=candidate_payload(candidate_book),
        headers=write_headers(client),
    )
    assert candidate_response.status_code == 201
    selected = candidate_response.json()
    adopted_body = {
        "candidate_id": selected["id"],
        "candidate_revision_id": selected["revision_id"],
        "candidate_version": selected["version"],
        "confirmation": "CREATE_DISTINCT_EVENT",
        "reason": "Synthetic candidate adoption without financial authorization.",
    }
    path = "/api/v1/dividends/events/from-candidate"
    payload = adopted_body
    headers = write_headers(client, key=f"synthetic-candidate-commit-{operation}")
    if operation == "accept":
        response = await client.post(path, json=adopted_body, headers=write_headers(client))
        assert response.status_code == 201
        event = response.json()
        path = accept_endpoint(event["id"])
        payload = accept_body(event["current_revision_id"])
        headers = version_headers(client, event["version"], key="synthetic-accept-source-commit")
    before = database_snapshot(engine)
    injected: list[str] = []

    def dismiss(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(operation)
        owner = UUID(candidate_book["owner"]["id"])
        current = candidates.get(connection, owner, UUID(selected["id"]))
        assert current is not None
        updated = current.record.decision.model_copy(
            update={
                "id": uuid4(),
                "revision_no": current.record.decision.revision_no + 1,
                "state": "DISMISSED",
                "change_reason": "Synthetic late candidate dismissal.",
            }
        )
        candidates.save(connection, owner, updated, at=now(), request_id=uuid4())

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", dismiss)
    try:
        result = await client.post(path, json=payload, headers=headers)
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", dismiss)
    assert injected == [operation], (result.status_code, result.json().get("code"))
    assert (result.status_code, result.json().get("code")) == (412, "PRECONDITION_FAILED")
    assert result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    retry = await client.post(path, json=payload, headers=headers)
    assert retry.status_code == (201 if operation == "adopt" else 200), retry.text
    after = database_snapshot(engine)
    replay = await client.post(path, json=payload, headers=headers)
    assert replay.status_code == retry.status_code and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("phase", ["before", "after_invalidation"])
@pytest.mark.parametrize("catch_inside", [False, True])
async def test_internal_acceptance_rechecks_predecessor_candidate_after_invalidation(
    client: httpx.AsyncClient,
    engine: Engine,
    candidate_book: dict[str, Any],
    catch_inside: bool,
    phase: str,
) -> None:
    candidate_response = await client.post(
        "/api/v1/dividends/candidates",
        json=candidate_payload(candidate_book),
        headers=write_headers(client),
    )
    assert candidate_response.status_code == 201
    selected = candidate_response.json()
    adopted = await client.post(
        "/api/v1/dividends/events/from-candidate",
        json={
            "candidate_id": selected["id"],
            "candidate_revision_id": selected["revision_id"],
            "candidate_version": selected["version"],
            "confirmation": "CREATE_DISTINCT_EVENT",
            "reason": "Synthetic candidate adoption without financial authorization.",
        },
        headers=write_headers(client),
    )
    assert adopted.status_code == 201, adopted.text
    event = adopted.json()
    owner = UUID(candidate_book["owner"]["id"])
    intent = writer.EventIntent(
        event_id=UUID(event["id"]),
        revision_id=uuid4(),
        revision_no=2,
        kind="VERIFY",
        citation="Synthetic acceptance reference",
        reason="Synthetic acceptance without financial authorization.",
    )
    request, at = uuid4(), now()
    original_invalidate = invalidate_portfolio
    injected: list[str] = []

    def dismiss(con: Any) -> None:
        current = candidates.get(con, owner, UUID(selected["id"]))
        assert current is not None
        changed = current.record.decision.model_copy(
            update={
                "id": uuid4(),
                "revision_no": current.record.decision.revision_no + 1,
                "state": "DISMISSED",
                "change_reason": "Synthetic late candidate dismissal.",
            }
        )
        candidates.save(con, owner, changed, at=now(), request_id=uuid4())

    def invalidate_and_dismiss(
        con: Any, portfolio: str, source: str, reason: str, from_date: str | None = None
    ) -> Any:
        marker = original_invalidate(con, portfolio, source, reason, from_date)
        assert not injected
        dismiss(con)
        injected.append("dismissed")
        return marker

    if phase == "before":
        with unit_of_work(engine, write=True) as con:
            dismiss(con)
    before = database_snapshot(engine)
    injected_scope = (
        patch.object(writer, "invalidate_portfolio", side_effect=invalidate_and_dismiss)
        if phase == "after_invalidation"
        else nullcontext()
    )
    outer = nullcontext() if catch_inside else pytest.raises(events.EventError)
    inner = pytest.raises(events.EventError) if catch_inside else nullcontext()
    with injected_scope, outer, unit_of_work(engine, write=True) as con, inner:
        writer.save(con, owner, intent, at=at, request_id=request)
    assert injected == (["dismissed"] if phase == "after_invalidation" else [])
    assert database_snapshot(engine) == before
    if phase == "before":
        withdrawal = intent.model_copy(update={"kind": "WITHDRAW", "revision_id": uuid4()})
        with unit_of_work(engine, write=True) as con:
            withdrawn = writer.save(con, owner, withdrawal, at=now(), request_id=uuid4())
        assert withdrawn.record.review_status == "WITHDRAWN"
        return
    with unit_of_work(engine, write=True) as con:
        saved = writer.save(con, owner, intent, at=at, request_id=request)
    with unit_of_work(engine, write=True) as con:
        dismiss(con)
    after = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert writer.save(con, owner, intent, at=at, request_id=request) == saved
    assert database_snapshot(engine) == after
