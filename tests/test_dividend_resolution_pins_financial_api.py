"""Protected rejection resolution preserves receipts, FX and serialized decisions."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_write_api import payload
from tests.test_dividend_resolution_pins_api import operation, reject
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("kind", ["manual", "accept", "override", "withdraw"])
async def test_pinned_resolution_never_detaches_an_existing_receipt_or_locked_fx(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
    rejected = reject(engine, book, event)
    method, url, body, _ = operation(book, kind)
    before = database_snapshot(engine)
    response = await client.request(
        method,
        url,
        json={**body, "resolve_rejection_id": str(rejected.id)},
        headers=version_headers(client, 1),
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["manual", "accept", "override", "withdraw"])
async def test_a_pin_without_a_current_rejection_is_not_silently_ignored(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    method, url, body, _ = operation(book, kind)
    before = database_snapshot(engine)
    response = await client.request(
        method,
        url,
        json={**body, "resolve_rejection_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 412 and response.json()["code"] == "PRECONDITION_FAILED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", ["", "not-a-uuid", True, []])
async def test_malformed_resolution_pins_fail_before_writes(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, invalid: Any
) -> None:
    method, url, body, _ = operation(book, "accept")
    before = database_snapshot(engine)
    response = await client.request(
        method,
        url,
        json={**body, "resolve_rejection_id": invalid},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_pinned_resolutions_commit_at_most_one_revision(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, same_key: bool
) -> None:
    event = book["event"]
    rejected = reject(engine, book, event)
    method, url, body, _ = operation(book, "accept")
    pinned = {**body, "resolve_rejection_id": str(rejected.id)}
    first = version_headers(client, 1, key="synthetic-resolution-race-first")
    second = (
        first if same_key else version_headers(client, 1, key="synthetic-resolution-race-second")
    )
    before = database_snapshot(engine)
    responses = await asyncio.gather(
        client.request(method, url, json=pinned, headers=first),
        client.request(method, url, json=pinned, headers=second),
    )
    assert sorted(response.status_code for response in responses) == (
        [200, 200] if same_key else [200, 412]
    )
    if same_key:
        assert responses[0].json() == responses[1].json()
    after = database_snapshot(engine)
    assert len(after["dividend_event_revisions"]) == len(before["dividend_event_revisions"]) + 1
    assert (
        len(after["dividend_rejection_resolutions"])
        == len(before["dividend_rejection_resolutions"]) + 1
    )
    for table in ("dividend_entitlements", "dividend_receipts", "cash_ledger_entries"):
        assert after[table] == before[table]


async def test_pinned_manual_resolution_still_requires_explicit_unlock(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    method, url, body, _ = operation(book, "accept")
    verified = await client.request(method, url, json=body, headers=version_headers(client, 1))
    assert verified.status_code == 200 and verified.json()["manual_locked"] is True
    with unit_of_work(engine) as con:
        current = events.get(con, UUID(book["owner"]["id"]), event.event_id)
        assert current is not None
    rejected = reject(engine, book, current.record)
    path = f"/api/v1/dividends/events/{event.event_id}"
    replacement = payload(
        book, revision_id=str(current.record.revision_id), resolve_rejection_id=str(rejected.id)
    )
    before = database_snapshot(engine)
    denied = await client.patch(path, json=replacement, headers=version_headers(client, 2))
    assert (
        denied.status_code == 409
        and denied.json()["code"] == "DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED"
    )
    assert database_snapshot(engine) == before
    allowed = await client.patch(
        path, json={**replacement, "confirm_unlock": True}, headers=version_headers(client, 2)
    )
    assert allowed.status_code == 200 and allowed.json()["manual_locked"] is False
    assert allowed.json()["version"] == 3
