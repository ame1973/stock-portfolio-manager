"""A candidate resolution cannot discard an existing receipt and locked FX."""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.audit import stamp
from spm_api.db import unit_of_work
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_dividend_candidate_adoption_api import manual_event, revision_body
from tests.test_dividend_candidate_adoption_api import selected as selected
from tests.test_dividend_candidate_resolution_api import reject
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


async def test_candidate_resolution_preserves_existing_receipt_cash_and_locked_fx(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = await manual_event(client, selected)
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": event["current_revision_id"], "currency": "USD"}, candidate_book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
    rejected = reject(engine, candidate_book, event)
    before = database_snapshot(engine)
    response = await client.post(
        f"/api/v1/dividends/events/{event['id']}/from-candidate",
        json=revision_body(selected, event, resolve_rejection_id=str(rejected.id)),
        headers=version_headers(client, 1),
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
