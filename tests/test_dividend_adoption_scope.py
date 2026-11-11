"""Source-only labels do not claim whether an event ever used the evidence."""

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
from spm_api.audit import now
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot, write_headers
from tests.test_basis_api import payload as basis_payload
from tests.test_candidate_api import basis_book as basis_book
from tests.test_candidate_api import book as book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload

pytestmark = pytest.mark.anyio
SCOPE = "RESPONSE_ONLY_NOT_LINKAGE"


def adopt(engine: Engine, owner: UUID, candidate: UUID) -> tuple[UUID, UUID]:
    event_id, revision_id = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        writer.save(
            con,
            owner,
            writer.EventIntent(
                event_id=event_id,
                revision_id=revision_id,
                revision_no=1,
                kind="CANDIDATE",
                candidate_id=candidate,
                candidate_version=1,
                reason="Explicit synthetic candidate association for scope testing.",
            ),
            at=now(),
            request_id=uuid4(),
        )
    return event_id, revision_id


async def test_linked_source_and_candidate_never_claim_that_linkage_was_checked(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body, headers = payload(candidate_book), write_headers(client)
    created = await client.post("/api/v1/dividends/candidates", json=body, headers=headers)
    assert created.status_code == 201
    candidate = created.json()
    event_id, revision_id = adopt(
        engine, UUID(candidate_book["owner"]["id"]), UUID(candidate["id"])
    )
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT observation_id FROM dividend_evidence_links WHERE revision_id=?",
                (str(revision_id),),
            ).scalar_one()
            == candidate_book["observation_id"]
        )
        assert (
            con.exec_driver_sql(
                "SELECT candidate_revision_id FROM dividend_revision_decisions WHERE revision_id=?",
                (str(revision_id),),
            ).scalar_one()
            == candidate["revision_id"]
        )

    async def check() -> None:
        before = database_snapshot(engine)
        observation = await client.get("/api/v1/dividends/observations")
        basis = await client.get(
            f"/api/v1/dividends/basis-assertions/{candidate_book['basis']['id']}"
        )
        read = await client.get(created.headers["location"])
        replay = await client.post("/api/v1/dividends/candidates", json=body, headers=headers)
        assert all(
            response.status_code in (200, 201) for response in (observation, basis, read, replay)
        )
        source = next(
            row
            for row in observation.json()["items"]
            if row["id"] == candidate_book["observation_id"]
        )
        for item in (source, basis.json(), read.json(), replay.json()):
            assert item["canonical_adoption"] == "NOT_ADOPTED"
            assert item["canonical_adoption_scope"] == SCOPE
        assert read.json() == replay.json() == candidate
        assert database_snapshot(engine) == before

    await check()
    with unit_of_work(engine, write=True) as con:
        current = events.get(con, UUID(candidate_book["owner"]["id"]), event_id)
        assert current is not None
        writer.save(
            con,
            UUID(candidate_book["owner"]["id"]),
            writer.EventIntent(
                event_id=event_id,
                revision_id=uuid4(),
                revision_no=2,
                kind="MANUAL",
                terms=current.record.terms,
                citation="Synthetic later manual reference",
                reason="Explicit synthetic later revision preserving historical links.",
            ),
            at=now(),
            request_id=uuid4(),
        )
    await check()


async def test_legacy_candidate_and_basis_replays_add_only_the_scope_label(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body, headers = payload(candidate_book), write_headers(client)
    created = await client.post("/api/v1/dividends/candidates", json=body, headers=headers)
    assert created.status_code == 201

    paths = (
        ("/api/v1/dividends/candidates", body, headers),
        ("/api/v1/dividends/basis-assertions", basis_payload(candidate_book), None),
    )
    for path, original_body, original_headers in paths:
        with unit_of_work(engine, write=True) as con:
            stored = con.exec_driver_sql(
                "SELECT idem_key,response_json FROM idempotency_keys WHERE route=?",
                (f"POST {path}",),
            ).one()
            response = json.loads(stored.response_json)
            assert response.pop("canonical_adoption_scope") == SCOPE
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE route=? AND idem_key=?",
                (json.dumps(response), f"POST {path}", stored.idem_key),
            )
        before = database_snapshot(engine)
        replay_headers = original_headers or write_headers(client, key=stored.idem_key)
        replay = await client.post(path, json=original_body, headers=replay_headers)
        assert replay.status_code == 201
        assert replay.json() == {**response, "canonical_adoption_scope": SCOPE}
        assert database_snapshot(engine) == before
