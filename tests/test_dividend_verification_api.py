"""Synthetic HTTP proofs for owner acceptance of dividend revisions."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_models as models
import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_writer as writer
import tests.test_dividend_projection_migration as projections
import tests.test_dividend_tax_api as tax_fixtures
from spm_api.audit import audit, canonical, digest, now, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_evidence import DividendTerms
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_basis_api import basis_book as basis_book
from tests.test_candidate_api import candidate_book as candidate_book
from tests.test_candidate_api import payload as candidate_payload
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_read_api import append
from tests.test_dividend_event_write_api import only_event_writes, payload, safe
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio
tax_book = tax_fixtures.book
REASON = "Synthetic owner acceptance, not independent document verification."


def endpoint(event_id: object) -> str:
    return f"/api/v1/dividends/{event_id}/verify"


def body(observed_revision: object, **changes: Any) -> dict[str, Any]:
    return {
        "revision_id": str(observed_revision),
        "decision": "ACCEPT_REVISION",
        "citation": "Synthetic owner verification reference",
        "evidence_url": "https://example.test/notice?private-value=synthetic#fragment",
        "reason": REASON,
        **changes,
    }


@pytest.fixture
async def adopted(candidate_book: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    candidate = await client.post(
        "/api/v1/dividends/candidates",
        json=candidate_payload(candidate_book),
        headers=write_headers(client),
    )
    assert candidate.status_code == 201, candidate.text
    selection = candidate.json()
    event = await client.post(
        "/api/v1/dividends/events/from-candidate",
        json={
            "candidate_id": selection["id"],
            "candidate_revision_id": selection["revision_id"],
            "candidate_version": selection["version"],
            "confirmation": "CREATE_DISTINCT_EVENT",
            "reason": REASON,
        },
        headers=write_headers(client),
    )
    assert event.status_code == 201, event.text
    return {"candidate": selection, "event": event.json()}


async def test_acceptance_is_owner_decision_not_document_or_economic_proof(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Owner acceptance must not access a provider")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    event = tax_book["event"]
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-accept-event")
    before = database_snapshot(engine)
    response = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert response.status_code == 200, response.text
    safe(response)
    result = response.json()
    assert response.headers["etag"] == '"2"'
    assert result["id"] == str(event.event_id) and result["version"] == 2
    assert result["current_revision_id"] != str(event.revision_id)
    assert result["decision_kind"] == "VERIFY" and result["workflow_status"] == "VERIFIED"
    assert result["manual_locked"] is True and result["dps"] == "0.90000000"
    assert result["reference"] == {
        "citation": intent["citation"],
        "url": None,
        "url_withheld": True,
    }
    assert result["scope"] == "OWNER_EVENT_DECISION_ONLY"
    assert result["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert result["document_verification"] == result["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    only_event_writes(before, after)
    with unit_of_work(engine) as con:
        entries = con.exec_driver_sql(
            "SELECT action,request_id,reason FROM audit_events "
            "WHERE entity_type='dividend_event_revision' AND entity_id=? ORDER BY action",
            (result["current_revision_id"],),
        ).all()
    assert [entry.action for entry in entries] == [
        "DIVIDEND_EVENT_HTTP_INTENT",
        "DIVIDEND_EVENT_REVISED",
    ]
    assert all(entry.request_id == response.headers["x-request-id"] for entry in entries)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == result
    assert database_snapshot(engine) == after


async def test_acceptance_does_not_inherit_prior_revision_tax_setting(
    tax_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = tax_book["event"]
    original = await tax_fixtures.selection(client, tax_book)
    setting = await client.post(
        tax_fixtures.path(tax_book),
        json=tax_fixtures.decision(original, rate="0"),
        headers=version_headers(client, 1),
    )
    assert setting.status_code == 201 and setting.json()["tax_status"] == "ESTIMATED"
    before = database_snapshot(engine)
    accepted = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 1),
    )
    assert accepted.status_code == 200, accepted.text
    assert database_snapshot(engine)["dividend_tax_settings"] == before["dividend_tax_settings"]
    selected = await tax_fixtures.selection(client, tax_book)
    assert selected["revision_id"] == accepted.json()["current_revision_id"]
    assert selected["tax_status"] == "MISSING" and selected["selected"] is None
    assert selected["head_no"] == "0" and selected["head_id"] is None
    history = await client.get(
        tax_fixtures.path(tax_book) + "/history",
        params={"revision_id": str(event.revision_id), "revision_no": "1"},
    )
    assert history.status_code == 200 and history.json()["items"] == [setting.json()]


async def test_replay_is_historical_after_an_explicit_unlock_and_revision(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = tax_book["event"]
    headers = version_headers(client, 1, key="synthetic-accept-replay")
    intent = body(event.revision_id)
    first = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert first.status_code == 200
    successor = await client.patch(
        f"/api/v1/dividends/events/{event.event_id}",
        json=payload(
            tax_book, revision_id=first.json()["current_revision_id"], confirm_unlock=True
        ),
        headers=version_headers(client, 2),
    )
    assert successor.status_code == 200, successor.text
    before = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["etag"] == '"2"'
    assert (
        await client.get(f"/api/v1/dividends/events/{event.event_id}")
    ).json() == successor.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_id", "not-a-uuid"),
        ("decision", "UNRECOGNIZED"),
        ("decision", True),
        ("citation", ""),
        ("evidence_url", "https://user:password@example.test/notice"),
        ("reason", ""),
        ("reason", "bad\u0000reason"),
        ("terms", {"dps": "0"}),
    ],
)
async def test_invalid_acceptance_is_rejected_without_writes(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    field: str,
    value: Any,
) -> None:
    event = tax_book["event"]
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id, **{field: value}),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["revision_id", "decision", "citation", "reason"])
async def test_acceptance_requires_every_explicit_decision_field(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    field: str,
) -> None:
    event = tax_book["event"]
    intent = body(event.revision_id)
    del intent[field]
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id), json=intent, headers=version_headers(client, 1)
    )
    assert response.status_code == 422
    assert database_snapshot(engine) == before


async def test_stale_event_and_unresolved_basis_do_not_become_verified(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = tax_book["event"]
    before = database_snapshot(engine)
    stale = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 2),
    )
    physical = await client.post(
        endpoint(event.event_id),
        json=body(uuid4()),
        headers=version_headers(client, 1),
    )
    assert stale.status_code == physical.status_code == 412
    assert database_snapshot(engine) == before
    incomplete = await client.post(
        "/api/v1/dividends/events",
        json=payload(tax_book, adjustment_basis="UNKNOWN"),
        headers=write_headers(client),
    )
    assert incomplete.status_code == 201 and incomplete.json()["workflow_status"] == "INCOMPLETE"
    before = database_snapshot(engine)
    denied = await client.post(
        endpoint(incomplete.json()["id"]),
        json=body(incomplete.json()["current_revision_id"]),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == 409 and denied.json()["code"] == "DIVIDEND_EVENT_UNRESOLVED_BASIS"
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("with_receipt", [False, True])
async def test_acceptance_respects_existing_entitlements_and_preserves_receipts_and_fx(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    with_receipt: bool,
) -> None:
    event = tax_book["event"]
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(event.revision_id), "currency": "USD"}, tax_book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        if with_receipt:
            add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 1),
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    assert database_snapshot(engine) == before


async def test_acceptance_fences_existing_projection_history(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tax_book["event"]
    monkeypatch.setattr(projections, "OWNER", UUID(tax_book["owner"]["id"]))
    with unit_of_work(engine, write=True) as con:
        snapshot = projections.snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            tax_book["account"]["id"],
        )
        insert(con, "dividend_projection_versions", snapshot)
    before = database_snapshot(engine)
    denied = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == before


async def test_receipt_race_is_rechecked_inside_acceptance_writer(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tax_book["event"]
    checkpoint: dict[str, Any] = {}

    def post_before_writer(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            claim = entitlement(
                {"id": str(event.revision_id), "currency": "USD"}, tax_book["account"]["id"]
            )
            claim["created_at"] = stamp()
            insert(con, "dividend_entitlements", claim)
            add_posted_receipt(con, claim, fx=7_800_000_000_000)
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", post_before_writer)
    denied = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == checkpoint["state"]


async def test_withdrawn_event_cannot_be_accepted(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = tax_book["event"]
    withdrawn = append(engine, tax_book, kind="WITHDRAW", number=2, event_id=event.event_id)
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id),
        json=body(withdrawn.revision_id),
        headers=version_headers(client, 2),
    )
    assert response.status_code == 409 and response.json()["code"] == "DIVIDEND_EVENT_WITHDRAWN"
    assert database_snapshot(engine) == before


async def test_owner_acceptance_resolves_conflict_review_without_verifying_document(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    candidate_body = candidate_payload(candidate_book)
    candidate_body["terms"]["dps"] = "1.10000000"
    candidate_response = await client.post(
        "/api/v1/dividends/candidates", json=candidate_body, headers=write_headers(client)
    )
    assert candidate_response.status_code == 201
    candidate = candidate_response.json()
    assert candidate["assessment"]["status"] == "CONFLICT"
    adopted_response = await client.post(
        "/api/v1/dividends/events/from-candidate",
        json={
            "candidate_id": candidate["id"],
            "candidate_revision_id": candidate["revision_id"],
            "candidate_version": candidate["version"],
            "confirmation": "CREATE_DISTINCT_EVENT",
            "reason": REASON,
        },
        headers=write_headers(client),
    )
    assert adopted_response.status_code == 201
    event = adopted_response.json()
    assert event["workflow_status"] == "CONFLICT"
    with unit_of_work(engine) as con:
        open_reviews = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=? AND kind='DIVIDEND_EVIDENCE' "
            "AND status='OPEN'",
            (event["id"],),
        ).all()
    assert open_reviews
    before = database_snapshot(engine)
    accepted = await client.post(
        endpoint(event["id"]),
        json=body(event["current_revision_id"]),
        headers=version_headers(client, 1),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["workflow_status"] == "VERIFIED"
    assert accepted.json()["dps"] == "1.10000000"
    assert accepted.json()["document_verification"] == "NOT_ESTABLISHED"
    assert accepted.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    only_event_writes(before, database_snapshot(engine))
    with unit_of_work(engine) as con:
        resolved = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=? AND kind='DIVIDEND_EVIDENCE' "
            "AND status='RESOLVED'",
            (event["id"],),
        ).all()
    assert resolved == open_reviews
    history = await client.get(f"/api/v1/dividends/events/{event['id']}/revisions")
    assert history.status_code == 200
    assert history.json()["items"][1]["assessment"]["status"] == "CONFLICT"


@pytest.mark.parametrize("change", ["dismissed", "basis"])
async def test_new_acceptance_rechecks_candidate_and_basis(
    candidate_book: dict[str, Any],
    adopted: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    change: str,
) -> None:
    candidate, event = adopted["candidate"], adopted["event"]
    if change == "dismissed":
        changed = await client.patch(
            f"/api/v1/dividends/candidates/{candidate['id']}",
            json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
            headers=version_headers(client, 1),
        )
    else:
        changed = await client.patch(
            f"/api/v1/corporate-actions/{candidate_book['action']['id']}",
            json={
                **candidate_book["action_body"],
                "ratio_numerator": "2",
                "ratio_denominator": "1",
            },
            headers=version_headers(client, 1),
        )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    denied = await client.post(
        endpoint(event["id"]),
        json=body(event["current_revision_id"]),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == (412 if change == "dismissed" else 409), denied.text
    assert database_snapshot(engine) == before


async def test_historical_acceptance_replay_survives_later_candidate_dismissal(
    candidate_book: dict[str, Any],
    adopted: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    candidate, event = adopted["candidate"], adopted["event"]
    headers = version_headers(client, 1, key="synthetic-accept-candidate-replay")
    intent = body(event["current_revision_id"])
    accepted = await client.post(endpoint(event["id"]), json=intent, headers=headers)
    assert accepted.status_code == 200, accepted.text
    dismissed = await client.patch(
        f"/api/v1/dividends/candidates/{candidate['id']}",
        json={**candidate_payload(candidate_book), "state": "DISMISSED", "matches": []},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(endpoint(event["id"]), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == accepted.json()
    assert database_snapshot(engine) == before


async def test_event_revision_race_is_rechecked_inside_writer(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tax_book["event"]
    checkpoint: dict[str, Any] = {}

    def revise_before_writer(*args: Any, **kwargs: Any) -> Any:
        decision = writer.EventIntent(
            event_id=event.event_id,
            revision_id=uuid4(),
            revision_no=2,
            kind="MANUAL",
            terms=event.terms,
            citation="Synthetic competing owner revision",
            reason=REASON,
        )
        with unit_of_work(engine, write=True) as con:
            writer.save(con, UUID(tax_book["owner"]["id"]), decision, at=now(), request_id=uuid4())
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", revise_before_writer)
    denied = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == 412
    assert database_snapshot(engine) == checkpoint["state"]


async def test_late_audit_failure_rolls_back_acceptance_and_can_retry(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = tax_book["event"]
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-accept-failure")
    before = database_snapshot(engine)
    with monkeypatch.context() as patcher:

        def fail_after_audit(*args: Any, **kwargs: Any) -> None:
            audit(*args, **kwargs)
            raise SQLAlchemyError("synthetic-private-value")

        patcher.setattr(routes, "audit", fail_after_audit)
        failed = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert failed.status_code == 503 and "synthetic-private-value" not in failed.text
    assert database_snapshot(engine) == before
    retry = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert retry.status_code == 200, retry.text


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("citation", "A different synthetic acceptance reference"),
        ("evidence_url", "https://example.test/notice?different=secret"),
        ("reason", "A different synthetic acceptance reason."),
    ],
)
async def test_original_unredacted_acceptance_intent_is_verified_on_replay(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    field: str,
    changed_value: str,
) -> None:
    event = tax_book["event"]
    headers = version_headers(client, 1, key="synthetic-accept-private-replay")
    first = await client.post(
        endpoint(event.event_id), json=body(event.revision_id), headers=headers
    )
    assert first.status_code == 200 and first.json()["reference"]["url"] is None
    changed = body(event.revision_id, **{field: changed_value})
    normalized = {
        "event_version": 1,
        **models.AcceptDividendRevision.model_validate(changed).model_dump(mode="json"),
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
            (digest(canonical(normalized)), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    denied = await client.post(endpoint(event.event_id), json=changed, headers=headers)
    assert denied.status_code == 503
    safe(denied)
    assert changed_value not in denied.text and database_snapshot(engine) == before


async def test_manual_revision_cannot_be_replayed_as_acceptance(
    tax_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = tax_book["event"]
    headers = version_headers(client, 1, key="synthetic-manual-as-acceptance")
    manual = await client.patch(
        f"/api/v1/dividends/events/{event.event_id}",
        json=payload(tax_book, revision_id=str(event.revision_id)),
        headers=headers,
    )
    assert manual.status_code == 200 and manual.json()["decision_kind"] == "MANUAL"
    intent = body(event.revision_id)
    normalized = {
        "event_version": 1,
        **models.AcceptDividendRevision.model_validate(intent).model_dump(mode="json"),
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET route=?,request_sha256=? WHERE idem_key=?",
            (
                f"POST {endpoint(event.event_id)}",
                digest(canonical(normalized)),
                headers["Idempotency-Key"],
            ),
        )
    before = database_snapshot(engine)
    denied = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert denied.status_code == 503
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["response", "binding"])
async def test_corrupt_replay_fails_closed(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    corruption: str,
) -> None:
    event = tax_book["event"]
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-accept-corrupt-replay")
    first = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        if corruption == "response":
            altered = first.json()
            altered["document_verification"] = "ESTABLISHED"
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                (json.dumps(altered), headers["Idempotency-Key"]),
            )
        else:
            audit(
                con,
                tax_book["owner"]["id"],
                "dividend_event_revision",
                first.json()["current_revision_id"],
                "DIVIDEND_EVENT_HTTP_INTENT",
                request_id=str(uuid4()),
                after={"synthetic": "duplicate"},
            )
    before = database_snapshot(engine)
    denied = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert denied.status_code == 503
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key", "reauth"])
async def test_acceptance_requires_current_security_dependencies(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    failure: str,
) -> None:
    event = tax_book["event"]
    headers = version_headers(client, 1)
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
    denied = await client.post(
        endpoint(event.event_id), json=body(event.revision_id), headers=headers
    )
    assert (
        denied.status_code
        == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422, "reauth": 403}[failure]
    )
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_writer_rechecks_security_before_acceptance_and_replay(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    event = tax_book["event"]
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-accept-writer-security")
    if replay:
        assert (
            await client.post(endpoint(event.event_id), json=intent, headers=headers)
        ).status_code == 200
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
    denied = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert denied.status_code == (401 if failure == "revoked" else 403)
    safe(denied)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_foreign_event_is_not_disclosed(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = tax_book["event"]
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
    denied = await client.post(
        endpoint(event.event_id),
        json=body(event.revision_id),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == 404
    safe(denied)
    assert database_snapshot(engine) == before


async def test_candidate_dismissal_race_is_rechecked_inside_writer(
    candidate_book: dict[str, Any],
    adopted: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, event = adopted["candidate"], adopted["event"]
    checkpoint: dict[str, Any] = {}

    def dismiss_before_writer(*args: Any, **kwargs: Any) -> Any:
        decision = candidates.CandidateDecision(
            id=uuid4(),
            candidate_id=UUID(candidate["id"]),
            revision_no=2,
            state="DISMISSED",
            terms=DividendTerms.model_validate(candidate["terms"]),
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
    denied = await client.post(
        endpoint(event["id"]),
        json=body(event["current_revision_id"]),
        headers=version_headers(client, 1),
    )
    assert denied.status_code == 412
    assert database_snapshot(engine) == checkpoint["state"]


async def test_same_key_retries_and_competing_acceptance_serialize(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    event = tax_book["event"]
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-accept-concurrent-retry")
    first, retry = await asyncio.gather(
        *(client.post(endpoint(event.event_id), json=intent, headers=headers) for _ in range(2))
    )
    assert first.status_code == retry.status_code == 200 and first.json() == retry.json()
    before = database_snapshot(engine)
    conflict = await client.post(
        endpoint(event.event_id),
        json={**intent, "reason": "A changed synthetic acceptance."},
        headers=headers,
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before
    another = await client.post(
        "/api/v1/dividends/events",
        json=payload(tax_book),
        headers=write_headers(client),
    )
    assert another.status_code == 201
    other = another.json()
    outcomes = await asyncio.gather(
        *(
            client.post(
                endpoint(other["id"]),
                json=body(other["current_revision_id"]),
                headers=version_headers(client, 1),
            )
            for _ in range(2)
        )
    )
    assert sorted(item.status_code for item in outcomes) == [200, 412]


async def test_acceptance_contract_requires_physical_version_and_owner_decision(
    tax_book: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    spec = (await client.get("/openapi.json")).json()
    operation = spec["paths"]["/api/v1/dividends/{event_id}/verify"]["post"]
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
    assert all(
        headers[name]["required"] for name in ("X-CSRF-Token", "Idempotency-Key", "If-Match")
    )
    schema = spec["components"]["schemas"]["AcceptDividendRevision"]
    assert set(schema["required"]) == {"revision_id", "decision", "citation", "reason"}
    assert schema["properties"]["decision"]["const"] == "ACCEPT_REVISION"
    assert schema["additionalProperties"] is False
