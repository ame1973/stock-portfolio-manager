"""Candidate adoption preserves current-source checks while resolving a rejection."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_models as models
import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import canonical, digest
from spm_api.db import unit_of_work
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot, version_headers
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload as candidate_payload
from tests.test_dividend_candidate_adoption_api import manual_event, revision_body
from tests.test_dividend_candidate_adoption_api import selected as selected
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


def reject(engine: Engine, candidate_book: dict[str, Any], event: dict[str, Any]) -> Any:
    observed = SimpleNamespace(
        event_id=UUID(event["id"]),
        revision_id=UUID(event["current_revision_id"]),
        revision_no=event["version"],
    )
    with unit_of_work(engine, write=True) as con:
        return rejection_writer.save(
            con, UUID(candidate_book["owner"]["id"]), rejection_intent(observed), request_id=uuid4()
        )


async def test_candidate_resolution_rechecks_source_and_historical_replay(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = await manual_event(client, selected)
    rejected = reject(engine, candidate_book, event)
    path = f"/api/v1/dividends/events/{event['id']}/from-candidate"
    body = revision_body(selected, event)
    before = database_snapshot(engine)
    missing = await client.post(path, json=body, headers=version_headers(client, 1))
    assert (
        missing.status_code == 409
        and missing.json()["code"] == "DIVIDEND_REJECTION_CONFIRMATION_REQUIRED"
    )
    stale = await client.post(
        path,
        json={**body, "resolve_rejection_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert stale.status_code == 412
    assert database_snapshot(engine) == before
    pinned = {**body, "resolve_rejection_id": str(rejected.id)}
    headers = version_headers(client, 1, key="synthetic-candidate-resolution")
    saved = await client.post(path, json=pinned, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.headers["etag"] == '"2"' and saved.headers["cache-control"] == "no-store"
    assert saved.json()["candidate_revision_id"] == selected["revision_id"]
    with unit_of_work(engine) as con:
        current = events.get(con, UUID(candidate_book["owner"]["id"]), UUID(event["id"]))
        assert current is not None and current.resolution is not None
        assert current.resolution.rejection_id == rejected.id
    dismissed = await client.patch(
        f"/api/v1/dividends/candidates/{selected['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200, dismissed.text
    before_replay = database_snapshot(engine)
    replay = await client.post(path, json=pinned, headers=headers)
    assert replay.status_code == 200 and replay.json() == saved.json()
    assert database_snapshot(engine) == before_replay


async def test_candidate_absent_pin_preserves_the_legacy_request_fingerprint(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = await manual_event(client, selected)
    path = f"/api/v1/dividends/events/{event['id']}/from-candidate"
    body = revision_body(selected, event)
    key = "synthetic-legacy-candidate-resolution"
    headers = version_headers(client, 1, key=key)
    saved = await client.post(path, json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    normalized = models.CandidateEventRevision.model_validate(body).model_dump(
        mode="json", exclude={"resolve_rejection_id"}
    )
    with unit_of_work(engine) as con:
        fingerprint = con.exec_driver_sql(
            "SELECT request_sha256 FROM idempotency_keys WHERE idem_key=?", (key,)
        ).scalar_one()
    assert fingerprint == digest(canonical({"event_version": 1, **normalized}))
    before = database_snapshot(engine)
    replay = await client.post(path, json={**body, "resolve_rejection_id": None}, headers=headers)
    assert replay.status_code == 200 and replay.json() == saved.json()
    assert database_snapshot(engine) == before


async def test_stale_or_dismissed_candidate_cannot_resolve_a_rejection(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = await manual_event(client, selected)
    rejected = reject(engine, candidate_book, event)
    dismissed = await client.patch(
        f"/api/v1/dividends/candidates/{selected['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200, dismissed.text
    path = f"/api/v1/dividends/events/{event['id']}/from-candidate"
    before = database_snapshot(engine)
    stale = await client.post(
        path,
        json=revision_body(selected, event, resolve_rejection_id=str(rejected.id)),
        headers=version_headers(client, 1),
    )
    assert stale.status_code == 412
    refused = await client.post(
        path,
        json=revision_body(dismissed.json(), event, resolve_rejection_id=str(rejected.id)),
        headers=version_headers(client, 1),
    )
    assert refused.status_code == 409 and refused.json()["code"] == "CANDIDATE_DISMISSED"
    assert database_snapshot(engine) == before
