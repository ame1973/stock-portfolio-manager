"""Synthetic HTTP proofs for explicit candidate-to-event decisions."""

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.action_service as action_service
import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
import tests.test_dividend_projection_migration as projections
from spm_api.action_models import CorporateActionCreate
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_evidence import DividendTerms
from spm_api.master_service import invalidate_portfolio
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session, read_auth
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload as candidate_payload
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/events"
CANDIDATES = "/api/v1/dividends/candidates"
REASON = "Synthetic explicit candidate association, not external market proof."


@pytest.fixture
async def selected(candidate_book: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        CANDIDATES, json=candidate_payload(candidate_book), headers=write_headers(client)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def create_body(candidate: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "candidate_id": candidate["id"],
        "candidate_revision_id": candidate["revision_id"],
        "candidate_version": candidate["version"],
        "confirmation": "CREATE_DISTINCT_EVENT",
        "reason": REASON,
        **changes,
    }


def revision_body(
    candidate: dict[str, Any], event: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    return create_body(
        candidate,
        revision_id=event["current_revision_id"],
        confirmation="MATCH_EXISTING_EVENT",
        **changes,
    )


async def manual_event(client: httpx.AsyncClient, candidate: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        PATH,
        json={
            **candidate["terms"],
            "citation": "Synthetic original event reference",
            "reason": REASON,
        },
        headers=write_headers(client),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert '"owner_id"' not in response.text and "private=fixture" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert REASON not in response.text


def only_event_writes(before: Any, after: Any) -> None:
    allowed = {
        "dividend_events",
        "dividend_event_controls",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "dividend_evidence_links",
        "audit_events",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
        "review_items",
    }
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }


async def test_candidate_requires_explicit_owner_adoption(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    candidate_response = await client.post(
        "/api/v1/dividends/candidates",
        json=candidate_payload(candidate_book),
        headers=write_headers(client),
    )
    assert candidate_response.status_code == 201
    candidate = candidate_response.json()
    body = {
        "candidate_id": candidate["id"],
        "candidate_revision_id": candidate["revision_id"],
        "candidate_version": candidate["version"],
        "confirmation": "CREATE_DISTINCT_EVENT",
        "reason": "Synthetic explicit candidate association, not external market proof.",
    }
    before = database_snapshot(engine)
    result = await client.post(f"{PATH}/from-candidate", json=body, headers=write_headers(client))
    assert result.status_code == 201, result.text
    assert result.json()["candidate_revision_id"] == candidate["revision_id"]
    assert result.json()["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert result.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    for table in ("dividend_entitlements", "dividend_receipts", "cash_ledger_entries"):
        assert after[table] == before[table]


async def test_candidate_creation_links_exact_evidence_and_replays_without_economic_writes(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Candidate adoption must not access providers")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-create-event")
    before = database_snapshot(engine)
    result = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert result.status_code == 201, result.text
    safe(result)
    item = result.json()
    assert result.headers["location"] == f"{PATH}/{item['id']}"
    assert result.headers["etag"] == '"1"'
    assert UUID(item["id"]).version == UUID(item["current_revision_id"]).version == 4
    assert item["decision_kind"] == "CANDIDATE" and not item["manual_locked"]
    assert item["candidate_revision_id"] == selected["revision_id"]
    assert item["dps"] == "0.90000000" and item["currency"] == "USD"
    assert item["workflow_status"] == selected["assessment"]["status"]
    assert item["assessment"] == selected["assessment"]
    assert item["evidence"][0]["observation_id"] == str(candidate_book["observation_id"])
    assert item["reference"] is None
    assert item["scope"] == "OWNER_EVENT_DECISION_ONLY"
    assert item["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert item["document_verification"] == item["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    only_event_writes(before, after)
    assert len(after["dividend_evidence_links"]) == len(before["dividend_evidence_links"]) + 1
    assert len(after["dividend_projection_versions"]) == len(before["dividend_projection_versions"])
    with unit_of_work(engine) as con:
        entries = con.exec_driver_sql(
            "SELECT action,request_id,reason FROM audit_events "
            "WHERE entity_type='dividend_event_revision' AND entity_id=? ORDER BY action",
            (item["current_revision_id"],),
        ).all()
        assert [row.action for row in entries] == [
            "DIVIDEND_EVENT_HTTP_INTENT",
            "DIVIDEND_EVENT_REVISED",
        ]
        assert all(row.request_id == result.headers["x-request-id"] for row in entries)
        assert entries[0].reason == "" and entries[1].reason == REASON
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == item
    safe(replay)
    assert database_snapshot(engine) == after


async def test_explicit_match_revises_only_the_selected_stable_event(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await manual_event(client, selected)
    headers = version_headers(client, 1, key="synthetic-candidate-event-revision")
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, original),
        headers=headers,
    )
    assert result.status_code == 200, result.text
    safe(result)
    item = result.json()
    assert item["id"] == original["id"] and item["version"] == 2
    assert item["current_revision_id"] != original["current_revision_id"]
    assert item["candidate_revision_id"] == selected["revision_id"]
    assert item["assessment"] == selected["assessment"] and result.headers["etag"] == '"2"'
    after = database_snapshot(engine)
    only_event_writes(before, after)
    assert len(after["dividend_events"]) == len(before["dividend_events"])
    assert len(after["dividend_event_revisions"]) == len(before["dividend_event_revisions"]) + 1
    history = await client.get(f"{PATH}/{original['id']}/revisions")
    assert [row["decision_kind"] for row in history.json()["items"]] == ["CANDIDATE", "MANUAL"]
    replay = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, original),
        headers=headers,
    )
    assert replay.status_code == 200 and replay.json() == item
    assert database_snapshot(engine) == after


async def test_same_day_explicit_candidates_remain_distinct_and_one_cannot_be_reused(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    other = await client.post(
        CANDIDATES, json=candidate_payload(candidate_book), headers=write_headers(client)
    )
    assert other.status_code == 201 and other.json()["id"] != selected["id"]
    first = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    second = await client.post(
        f"{PATH}/from-candidate", json=create_body(other.json()), headers=write_headers(client)
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["ex_date"] == second.json()["ex_date"]
    before = database_snapshot(engine)
    reused = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    assert reused.status_code == 409
    assert reused.json()["code"] == "DIVIDEND_EVENT_CANDIDATE_ALREADY_USED"
    safe(reused)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", "not-a-uuid"),
        ("candidate_revision_id", "not-a-uuid"),
        ("candidate_version", 0),
        ("candidate_version", 2_147_483_648),
        ("candidate_version", "1"),
        ("candidate_version", True),
        ("confirmation", "MATCH_EXISTING_EVENT"),
        ("confirmation", True),
        ("reason", ""),
        ("reason", "private\u0000reason"),
        ("terms", {"dps": "0"}),
    ],
)
async def test_invalid_candidate_intent_is_rejected_without_writes(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str, value: Any
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/from-candidate",
        json=create_body(selected, **{field: value}),
        headers=write_headers(client),
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field",
    ["candidate_id", "candidate_revision_id", "candidate_version", "confirmation", "reason"],
)
async def test_candidate_adoption_requires_every_explicit_pin(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str
) -> None:
    body = create_body(selected)
    del body[field]
    before = database_snapshot(engine)
    result = await client.post(f"{PATH}/from-candidate", json=body, headers=write_headers(client))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"candidate_id": str(uuid4())}, 404),
        ({"candidate_revision_id": str(uuid4())}, 412),
        ({"candidate_version": 2}, 412),
    ],
)
async def test_candidate_identity_and_current_version_are_pinned(
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    change: dict[str, Any],
    status: int,
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/from-candidate",
        json=create_body(selected, **change),
        headers=write_headers(client),
    )
    assert result.status_code == status
    safe(result)
    assert database_snapshot(engine) == before


async def test_revision_pins_event_version_physical_id_and_identity_confirmation(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await manual_event(client, selected)
    path = f"{PATH}/{original['id']}/from-candidate"
    body = revision_body(selected, original)
    before = database_snapshot(engine)
    cases = [
        (body, version_headers(client, 2), 412),
        ({**body, "revision_id": str(uuid4())}, version_headers(client, 1), 412),
        ({**body, "confirmation": "CREATE_DISTINCT_EVENT"}, version_headers(client, 1), 422),
        (
            {key: value for key, value in body.items() if key != "revision_id"},
            version_headers(client, 1),
            422,
        ),
        (body, write_headers(client), 422),
        (body, {**write_headers(client), "If-Match": '"2147483648"'}, 422),
    ]
    for proposed, headers, status in cases:
        result = await client.post(path, json=proposed, headers=headers)
        assert result.status_code == status, result.text
        safe(result)
    assert database_snapshot(engine) == before


async def test_candidate_basis_changes_and_dismissal_fence_new_use(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    changed = await client.patch(
        f"/api/v1/corporate-actions/{candidate_book['action']['id']}",
        headers=version_headers(client, 1),
        json={**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"},
    )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    blocked = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    assert blocked.status_code == 409 and blocked.json()["code"] == "CANDIDATE_BASIS_CHANGED"
    safe(blocked)
    assert database_snapshot(engine) == before
    dismissed = await client.patch(
        f"{CANDIDATES}/{selected['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200
    before = database_snapshot(engine)
    stale = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    denied = await client.post(
        f"{PATH}/from-candidate", json=create_body(dismissed.json()), headers=write_headers(client)
    )
    assert stale.status_code == 412
    assert denied.status_code == 409 and denied.json()["code"] == "CANDIDATE_DISMISSED"
    safe(stale)
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["locked", "withdrawn"])
async def test_candidate_cannot_replace_a_locked_or_withdrawn_event(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    state: str,
) -> None:
    original = await manual_event(client, selected)
    kind = "VERIFY" if state == "locked" else "WITHDRAW"
    followup = writer.EventIntent(
        event_id=UUID(original["id"]),
        revision_id=uuid4(),
        revision_no=2,
        kind=kind,
        citation="Synthetic owner-only state change",
        reason=REASON,
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(
            con, UUID(candidate_book["owner"]["id"]), followup, at=now(), request_id=uuid4()
        )
    observed = {**original, "current_revision_id": str(followup.revision_id)}
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, observed),
        headers=version_headers(client, 2),
    )
    assert result.status_code == 409
    assert result.json()["code"] == (
        "DIVIDEND_EVENT_LOCKED" if state == "locked" else "DIVIDEND_EVENT_WITHDRAWN"
    )
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("missing", ["ex_date", "dps", "currency"])
async def test_incomplete_candidate_terms_are_not_promoted_or_zero_filled(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, missing: str
) -> None:
    body = candidate_payload(candidate_book)
    body["terms"][missing] = None
    response = await client.post(CANDIDATES, json=body, headers=write_headers(client))
    assert response.status_code == 201
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/from-candidate", json=create_body(response.json()), headers=write_headers(client)
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("assessment", ["conflict", "no-evidence"])
async def test_conflict_or_absent_evidence_remains_visible_and_creates_review(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, assessment: str
) -> None:
    body = candidate_payload(candidate_book)
    if assessment == "conflict":
        body["terms"]["dps"] = "1.2"
    else:
        body["matches"] = []
    candidate = await client.post(CANDIDATES, json=body, headers=write_headers(client))
    assert candidate.status_code == 201
    expected = "CONFLICT" if assessment == "conflict" else "INCOMPLETE"
    assert candidate.json()["assessment"]["status"] == expected
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/from-candidate", json=create_body(candidate.json()), headers=write_headers(client)
    )
    assert result.status_code == 201, result.text
    assert result.json()["workflow_status"] == expected
    assert result.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    only_event_writes(before, after)
    assert len(after["review_items"]) == len(before["review_items"]) + 1


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key", "reauth"])
async def test_candidate_adoption_requires_current_security_dependencies(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
    headers = write_headers(client)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "synthetic-invalid-csrf"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    elif failure == "key":
        headers["Idempotency-Key"] = "short"
    else:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'"
            )
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=headers
    )
    assert (
        result.status_code
        == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422, "reauth": 403}[failure]
    )
    safe(result)
    assert database_snapshot(engine) == before


async def test_foreign_candidate_and_foreign_event_are_not_disclosed(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    original = await manual_event(client, selected)
    with unit_of_work(engine, write=True) as con:
        owner = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (owner, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, owner, "synthetic-other-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    foreign_candidate = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    foreign_event = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, original),
        headers=version_headers(client, 1),
    )
    assert foreign_candidate.status_code == foreign_event.status_code == 404
    safe(foreign_candidate)
    safe(foreign_event)
    assert database_snapshot(engine) == before


async def test_historical_replay_survives_candidate_and_event_successors(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-historical-replay")
    first = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert first.status_code == 201
    original = first.json()
    dismissed = await client.patch(
        f"{CANDIDATES}/{selected['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200
    action = await client.patch(
        f"/api/v1/corporate-actions/{candidate_book['action']['id']}",
        headers=version_headers(client, 1),
        json={**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"},
    )
    assert action.status_code == 200
    successor = await client.patch(
        f"{PATH}/{original['id']}",
        json={
            **selected["terms"],
            "revision_id": original["current_revision_id"],
            "dps": "1.1",
            "citation": "Synthetic later owner reference",
            "reason": REASON,
        },
        headers=version_headers(client, 1),
    )
    assert successor.status_code == 200, successor.text
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == original
    assert replay.headers["etag"] == '"1"'
    assert (await client.get(f"{PATH}/{original['id']}")).json() == successor.json()
    assert database_snapshot(engine) == before


async def test_revision_replay_remains_bound_to_its_original_predecessor(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = await manual_event(client, selected)
    body = revision_body(selected, original)
    headers = version_headers(client, 1, key="synthetic-candidate-revision-replay")
    path = f"{PATH}/{original['id']}/from-candidate"
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    successor = await client.patch(
        f"{PATH}/{original['id']}",
        json={
            **selected["terms"],
            "revision_id": first.json()["current_revision_id"],
            "citation": "Synthetic later owner reference",
            "reason": REASON,
        },
        headers=version_headers(client, 2),
    )
    assert successor.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["etag"] == '"2"'
    assert database_snapshot(engine) == before
    actual = events.get

    def missing_prior(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("version") == 1:
            return None
        return actual(*args, **kwargs)

    monkeypatch.setattr(events, "get", missing_prior)
    failed = await client.post(path, json=body, headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before


async def test_replay_rechecks_recent_reauthentication(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-reauth-replay")
    first = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_writer_rechecks_security_for_new_and_cached_intents(
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-writer-security")
    if replay:
        assert (
            await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
        ).status_code == 201
    checkpoint: dict[str, Any] = {}

    def changed(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            elif failure == "csrf":
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
            else:
                con.exec_driver_sql(
                    "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'"
                )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed)
    result = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_candidate_is_rechecked_after_a_concurrent_dismissal(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint: dict[str, Any] = {}

    def dismiss_before_writer(*args: Any, **kwargs: Any) -> Any:
        decision = candidates.CandidateDecision(
            id=uuid4(),
            candidate_id=UUID(selected["id"]),
            revision_no=2,
            state="DISMISSED",
            terms=DividendTerms.model_validate(selected["terms"]),
            matches=(),
            change_reason=REASON,
        )
        with unit_of_work(engine, write=True) as con:
            candidates.save(
                con, UUID(candidate_book["owner"]["id"]), decision, at=now(), request_id=uuid4()
            )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", dismiss_before_writer)
    result = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    assert result.status_code == 412
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize("existing_event", [False, True])
async def test_candidate_basis_is_rechecked_after_a_concurrent_action_revision(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    existing_event: bool,
) -> None:
    if existing_event:
        original = await manual_event(client, selected)
        path = f"{PATH}/{original['id']}/from-candidate"
        body = revision_body(selected, original)
        headers = version_headers(client, 1)
    else:
        path = f"{PATH}/from-candidate"
        body = create_body(selected)
        headers = write_headers(client)
    checkpoint: dict[str, Any] = {}

    def revise_action_before_writer(*args: Any, **kwargs: Any) -> Any:
        request = args[0]
        revised = CorporateActionCreate.model_validate(
            {**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"}
        )
        with unit_of_work(engine, write=True) as con:
            auth = read_auth(con, request.cookies.get(COOKIE))
            action_service.revise_action(
                con, auth, candidate_book["action"]["id"], 1, revised, str(uuid4())
            )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", revise_action_before_writer)
    result = await client.post(path, json=body, headers=headers)
    assert result.status_code == 409 and result.json()["code"] == "CANDIDATE_BASIS_CHANGED"
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_existing_event_is_rechecked_after_a_concurrent_revision(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = await manual_event(client, selected)
    checkpoint: dict[str, Any] = {}

    def revise_before_writer(*args: Any, **kwargs: Any) -> Any:
        decision = writer.EventIntent(
            event_id=UUID(original["id"]),
            revision_id=uuid4(),
            revision_no=2,
            kind="MANUAL",
            terms=DividendTerms.model_validate(selected["terms"]),
            citation="Synthetic competing owner revision",
            reason=REASON,
        )
        with unit_of_work(engine, write=True) as con:
            writer.save(
                con, UUID(candidate_book["owner"]["id"]), decision, at=now(), request_id=uuid4()
            )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", revise_before_writer)
    result = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, original),
        headers=version_headers(client, 1),
    )
    assert result.status_code == 412
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_concurrent_retries_and_competing_candidate_creations_serialize(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-concurrent-retry")
    first, retry = await asyncio.gather(
        *(client.post(f"{PATH}/from-candidate", json=body, headers=headers) for _ in range(2))
    )
    assert first.status_code == retry.status_code == 201 and first.json() == retry.json()
    before = database_snapshot(engine)
    conflict = await client.post(
        f"{PATH}/from-candidate",
        json={**body, "reason": "A changed synthetic decision."},
        headers=headers,
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before
    another = await client.post(
        CANDIDATES, json=candidate_payload(candidate_book), headers=write_headers(client)
    )
    assert another.status_code == 201
    other_body = create_body(another.json())
    results = await asyncio.gather(
        *(
            client.post(f"{PATH}/from-candidate", json=other_body, headers=write_headers(client))
            for _ in range(2)
        )
    )
    assert sorted(item.status_code for item in results) == [201, 409]
    assert (
        next(item for item in results if item.status_code == 409).json()["code"]
        == "DIVIDEND_EVENT_CANDIDATE_ALREADY_USED"
    )


@pytest.mark.parametrize("with_receipt", [False, True])
async def test_entitlement_or_receipt_fences_candidate_revision_and_preserves_locked_fx(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    with_receipt: bool,
) -> None:
    original = await manual_event(client, selected)
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": original["current_revision_id"], "currency": "USD"},
            candidate_book["account"]["id"],
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        if with_receipt:
            add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, original),
        headers=version_headers(client, 1),
    )
    assert (
        result.status_code == 409 and result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    safe(result)
    assert database_snapshot(engine) == before


async def test_projection_history_fences_candidate_revision(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = await manual_event(client, selected)
    monkeypatch.setattr(projections, "OWNER", UUID(candidate_book["owner"]["id"]))
    with unit_of_work(engine, write=True) as con:
        values = projections.snapshot_values(
            con,
            {"id": original["id"]},
            {"id": original["current_revision_id"], "currency": "USD"},
            candidate_book["account"]["id"],
        )
        insert(con, "dividend_projection_versions", values)
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{original['id']}/from-candidate",
        json=revision_body(selected, original),
        headers=version_headers(client, 1),
    )
    assert (
        result.status_code == 409 and result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["binding-audit", "serialization", "invalidation"])
async def test_failed_candidate_adoption_rolls_back_all_effects_and_can_retry(
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    headers = write_headers(client, key="synthetic-candidate-adoption-failure")
    body = create_body(selected)
    before = database_snapshot(engine)
    with monkeypatch.context() as patcher:
        if failure == "binding-audit":

            def fail_audit(*args: Any, **kwargs: Any) -> None:
                audit(*args, **kwargs)
                raise SQLAlchemyError("synthetic-private-value")

            patcher.setattr(routes, "audit", fail_audit)
        elif failure == "serialization":

            def fail_public(*args: Any, **kwargs: Any) -> Any:
                raise ValueError("synthetic-private-value")

            patcher.setattr(routes, "_current", fail_public)
        else:

            def fail_invalidation(*args: Any, **kwargs: Any) -> None:
                invalidate_portfolio(*args, **kwargs)
                raise SQLAlchemyError("synthetic-private-value")

            patcher.setattr(writer, "invalidate_portfolio", fail_invalidation)
        failed = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert failed.status_code == 503
    assert "synthetic-private-value" not in failed.text
    safe(failed)
    assert database_snapshot(engine) == before
    retry = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert retry.status_code == 201, retry.text


@pytest.mark.parametrize("corruption", ["response", "version", "value", "candidate", "binding"])
async def test_corrupt_cached_response_or_late_binding_fails_closed(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    corruption: str,
) -> None:
    headers = write_headers(client, key="synthetic-candidate-corrupt-replay")
    body = create_body(selected)
    first = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert first.status_code == 201
    if corruption in {"response", "version", "value"}:
        altered = first.json()
        if corruption == "response":
            altered["eligibility_scope"] = "ESTABLISHED"
        elif corruption == "version":
            altered["version"] = 2
        else:
            altered["dps"] = "1.00000000"
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                (json.dumps(altered), headers["Idempotency-Key"]),
            )
    elif corruption == "candidate":
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                candidate_book["owner"]["id"],
                "dividend_candidate_revision",
                selected["revision_id"],
                "DIVIDEND_CANDIDATE_REVISED",
                request_id=str(uuid4()),
                after={"synthetic": "duplicate"},
                reason=REASON,
            )
    else:
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                candidate_book["owner"]["id"],
                "dividend_event_revision",
                first.json()["current_revision_id"],
                "DIVIDEND_EVENT_HTTP_INTENT",
                request_id=str(uuid4()),
                after={"synthetic": "duplicate"},
            )
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before


async def test_valid_cached_event_from_another_candidate_cannot_be_substituted(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    other = await client.post(
        CANDIDATES, json=candidate_payload(candidate_book), headers=write_headers(client)
    )
    assert other.status_code == 201
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-substituted-response")
    first = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    second = await client.post(
        f"{PATH}/from-candidate", json=create_body(other.json()), headers=write_headers(client)
    )
    assert first.status_code == second.status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(second.json()), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before


async def test_cached_revision_cannot_be_replayed_for_another_event(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await manual_event(client, selected)
    path = f"{PATH}/{original['id']}/from-candidate"
    body = revision_body(selected, original)
    headers = version_headers(client, 1, key="synthetic-candidate-revision-wrong-event")
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    altered = {**first.json(), "id": str(uuid4())}
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(altered), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before


async def test_candidate_replay_requires_its_retained_event(
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-missing-event")
    first = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert first.status_code == 201
    monkeypatch.setattr(events, "get", lambda *args, **kwargs: None)
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["unavailable", "missing", "wrong-revision"])
async def test_candidate_replay_rechecks_original_candidate_identity(
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    body = create_body(selected)
    headers = write_headers(client, key="synthetic-candidate-original-identity")
    first = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert first.status_code == 201
    actual = candidates.get
    calls = 0

    def changed(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls != 2:
            return actual(*args, **kwargs)
        if failure == "unavailable":
            raise candidates.CandidateError("synthetic-private-value")
        if failure == "missing":
            return None
        candidate = actual(*args, **kwargs)
        assert candidate is not None
        decision = candidate.record.decision.model_copy(update={"id": uuid4()})
        record = candidate.record.model_copy(update={"decision": decision})
        return replace(candidate, record=record)

    monkeypatch.setattr(candidates, "get", changed)
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/from-candidate", json=body, headers=headers)
    assert calls == 2 and replay.status_code == 503
    safe(replay)
    assert "synthetic-private-value" not in replay.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("code", "status", "public_code"),
    [
        ("CANDIDATE_NOT_FOUND", 404, "NOT_FOUND"),
        ("CANDIDATE_OWNER_UNAVAILABLE", 401, "UNAUTHORIZED"),
        ("CANDIDATE_VERSION_CONFLICT", 412, "PRECONDITION_FAILED"),
        ("CANDIDATE_DISMISSED", 409, "CANDIDATE_DISMISSED"),
        ("CANDIDATE_BASIS_CHANGED", 409, "CANDIDATE_BASIS_CHANGED"),
        ("CANDIDATE_SOURCE_CHANGED", 409, "CANDIDATE_SOURCE_CHANGED"),
        ("DIVIDEND_EVENT_CANDIDATE_ALREADY_USED", 409, "DIVIDEND_EVENT_CANDIDATE_ALREADY_USED"),
        ("DIVIDEND_EVENT_LOCKED", 409, "DIVIDEND_EVENT_LOCKED"),
        ("DIVIDEND_EVENT_WITHDRAWN", 409, "DIVIDEND_EVENT_WITHDRAWN"),
        ("DIVIDEND_EVENT_PROJECTION_REQUIRED", 409, "DIVIDEND_EVENT_PROJECTION_REQUIRED"),
        ("CANDIDATE_HISTORY_UNAVAILABLE", 503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"),
        ("CANDIDATE_SOURCE_UNAVAILABLE", 503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"),
        ("synthetic-private-value", 503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"),
    ],
)
async def test_candidate_failures_have_fixed_private_error_mappings(
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
    public_code: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise events.EventError(code)

    monkeypatch.setattr(writer, "save", fail)
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    assert result.status_code == status and result.json()["code"] == public_code
    safe(result)
    assert "synthetic-private-value" not in result.text
    assert database_snapshot(engine) == before


async def test_openapi_requires_security_version_pins_and_explicit_identity_choices(
    selected: dict[str, Any], client: httpx.AsyncClient
) -> None:
    spec = (await client.get("/openapi.json")).json()
    for path, needs_version in (
        (f"{PATH}/from-candidate", False),
        (f"{PATH}/{{event_id}}/from-candidate", True),
    ):
        operation = spec["paths"][path]["post"]
        assert operation["security"] == [{"cookieAuth": []}]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        assert headers["X-CSRF-Token"]["required"] and headers["Idempotency-Key"]["required"]
        if needs_version:
            assert headers["If-Match"]["required"]
    schemas = spec["components"]["schemas"]
    for name, confirmation in (
        ("CandidateEventCreate", "CREATE_DISTINCT_EVENT"),
        ("CandidateEventRevision", "MATCH_EXISTING_EVENT"),
    ):
        schema = schemas[name]
        assert {
            "candidate_id",
            "candidate_revision_id",
            "candidate_version",
            "confirmation",
            "reason",
        } <= set(schema["required"])
        assert schema["properties"]["confirmation"]["const"] == confirmation
    assert "revision_id" in schemas["CandidateEventRevision"]["required"]


async def test_owner_can_withdraw_after_candidate_and_basis_change(
    candidate_book: dict[str, Any],
    selected: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    adopted = await client.post(
        f"{PATH}/from-candidate", json=create_body(selected), headers=write_headers(client)
    )
    assert adopted.status_code == 201, adopted.text
    event = adopted.json()
    dismissed = await client.patch(
        f"{CANDIDATES}/{selected['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200, dismissed.text
    action = await client.patch(
        f"/api/v1/corporate-actions/{candidate_book['action']['id']}",
        json={**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"},
        headers=version_headers(client, 1),
    )
    assert action.status_code == 200, action.text
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{event['id']}/withdraw",
        json={
            "revision_id": event["current_revision_id"],
            "confirmation": "WITHDRAW_EVENT",
            "citation": "Synthetic owner withdrawal reference",
            "reason": REASON,
        },
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200, result.text
    assert result.json()["workflow_status"] == "WITHDRAWN"
    assert result.json()["dps"] == event["dps"]
    assert result.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    only_event_writes(before, database_snapshot(engine))
    safe(result)
