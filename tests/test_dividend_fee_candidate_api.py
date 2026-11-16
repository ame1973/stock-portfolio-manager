"""Fee HTTP writes revalidate candidate source and basis, while replay stays historical."""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from tests.helpers import database_snapshot, version_headers, write_headers
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload as candidate_payload
from tests.test_dividend_candidate_adoption_api import create_body
from tests.test_dividend_candidate_adoption_api import selected as selected
from tests.test_dividend_fee_api import body, safe
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


async def make_event_and_scope(
    client: httpx.AsyncClient, candidate_book: dict[str, Any], candidate: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    result = await client.post(
        "/api/v1/dividends/events/from-candidate",
        json=create_body(candidate),
        headers=write_headers(client),
    )
    assert result.status_code == 201, result.text
    event = result.json()
    path = (
        f"/api/v1/dividends/events/{event['id']}"
        f"/accounts/{candidate_book['account']['id']}/fee-settings"
    )
    selected_scope = await client.get(path, params={"effective_on": "2026-01-08"})
    assert selected_scope.status_code == 200
    return path, dict(selected_scope.json())


async def test_candidate_dismissal_blocks_new_fee_but_not_original_replay(
    client: httpx.AsyncClient,
    engine: Engine,
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
) -> None:
    path, scope = await make_event_and_scope(client, candidate_book, selected)
    headers = version_headers(client, 1, key="synthetic-fee-candidate-replay")
    first = await client.post(path, json=body(scope, fee="1"), headers=headers)
    assert first.status_code == 201, first.text
    dismissed = await client.patch(
        f"/api/v1/dividends/candidates/{selected['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200
    current = await client.get(path, params={"effective_on": "2026-01-08"})
    assert (
        current.status_code == 200
        and current.json()["current_use_scope"] == "REVALIDATION_REQUIRED"
    )
    before = database_snapshot(engine)
    replay = await client.post(path, json=body(scope, fee="1"), headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    denied = await client.post(path, json=body(current.json()), headers=version_headers(client, 1))
    assert denied.status_code == 409 and denied.json()["code"] == "CANDIDATE_VERSION_CONFLICT"
    safe(denied)
    assert database_snapshot(engine) == before


async def test_candidate_basis_change_blocks_new_fee_without_touching_history(
    client: httpx.AsyncClient,
    engine: Engine,
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
) -> None:
    path, scope = await make_event_and_scope(client, candidate_book, selected)
    action = candidate_book["action"]
    corrected = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}",
        json={**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"},
        headers=version_headers(client, 1),
    )
    assert corrected.status_code == 200
    before = database_snapshot(engine)
    denied = await client.post(path, json=body(scope), headers=version_headers(client, 1))
    assert denied.status_code == 409 and denied.json()["code"] == "CANDIDATE_BASIS_CHANGED"
    safe(denied)
    assert database_snapshot(engine) == before
