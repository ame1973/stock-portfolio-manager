"""Authenticated rejection decisions preserve event identity and financial history."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import spm_api.dividend_event_routes as event_routes
import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_rejection_writer as rejection_writer
import spm_api.mutations as mutations
import spm_api.security as security
import tests.test_dividend_projection_migration as projections
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_write_api import revision
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
REASON = "Synthetic rejection decision, not a financial grant."


def endpoint(event_id: object) -> str:
    return f"/api/v1/dividends/{event_id}/verify"


def body(revision_id: object, **changes: Any) -> dict[str, Any]:
    return {
        "revision_id": str(revision_id),
        "decision": "REJECT_REVISION",
        "citation": "Synthetic owner rejection reference",
        "evidence_url": "https://example.test/notice?private=synthetic#part",
        "reason": REASON,
        **changes,
    }


async def test_rejection_records_a_separate_decision_without_changing_the_event_or_etag(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A rejection must not contact a provider")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    event = book["event"]
    event_url = f"/api/v1/dividends/events/{event.event_id}"
    original = await client.get(event_url)
    before = database_snapshot(engine)
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-protected-reject")
    saved = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json() == original.json()
    assert saved.headers["etag"] == original.headers["etag"] == '"1"'
    assert saved.headers["cache-control"] == "no-store"
    state = await client.get(event_url + "/rejection")
    assert state.status_code == 200 and state.json()["state"] == "REJECTED"
    rejected = state.json()["rejection"]
    assert rejected["decision"] == "REJECT_REVISION"
    assert rejected["revision_id"] == str(event.revision_id)
    assert rejected["reference"] == {
        "citation": intent["citation"],
        "url": None,
        "url_withheld": True,
    }
    assert rejected["eligibility_scope"] == "NOT_ESTABLISHED"
    assert "private=synthetic" not in state.text
    after = database_snapshot(engine)
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 2
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    for name in (
        "dividend_events",
        "dividend_event_revisions",
        "dividend_receipts",
        "cash_ledger_entries",
    ):
        assert after[name] == before[name]
    assert (await client.get(event_url)).json() == original.json()
    retry = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert retry.status_code == 200 and retry.json() == saved.json()
    assert database_snapshot(engine) == after
    conflict = await client.post(
        endpoint(event.event_id), json={**intent, "reason": REASON + " Changed."}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == after


async def test_rejection_replay_remains_historical_after_exact_pinned_resolution(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    url = f"/api/v1/dividends/events/{event.event_id}"
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key="synthetic-rejection-historical")
    first = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert first.status_code == 200, first.text
    rejected = (await client.get(url + "/rejection")).json()["rejection"]
    before_resolution = database_snapshot(engine)
    missing_pin = await client.patch(url, json=revision(book), headers=version_headers(client, 1))
    assert missing_pin.status_code == 409
    assert missing_pin.json()["code"] == "DIVIDEND_REJECTION_CONFIRMATION_REQUIRED"
    assert database_snapshot(engine) == before_resolution
    successor = await client.patch(
        url,
        json={**revision(book), "resolve_rejection_id": rejected["id"]},
        headers=version_headers(client, 1),
    )
    assert successor.status_code == 200, successor.text
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": successor.json()["current_revision_id"], "currency": "USD"},
            book["account"]["id"],
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["etag"] == '"1"'
    current = await client.get(url + "/rejection")
    assert current.json()["state"] == "NOT_REJECTED" and current.json()["revision_no"] == 2
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_rejection_rechecks_security_inside_writer_even_on_replay(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    failure: str,
    replay: bool,
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key=f"synthetic-rejection-auth-{failure}")
    intent = body(event.revision_id)
    if replay:
        assert (
            await client.post(endpoint(event.event_id), json=intent, headers=headers)
        ).status_code == 200
    before = database_snapshot(engine)
    name, status, code = {
        "revoked": ("read_auth", 401, "UNAUTHORIZED"),
        "csrf": ("validate_csrf", 403, "CSRF_REJECTED"),
        "reauth": ("require_reauth", 403, "REAUTH_REQUIRED"),
    }[failure]
    with patch.object(mutations, name, side_effect=ApiError(status, code, "Synthetic denial")):
        denied = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert denied.status_code == status and denied.json()["code"] == code
    assert denied.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_rejection_with_existing_entitlement_and_receipt_is_fenced(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id), json=body(event.revision_id), headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
            ).scalar_one()
            == 7_800_000_000_000
        )


async def test_rejection_with_existing_projection_is_fenced(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(projections, "OWNER", UUID(book["owner"]["id"])),
    ):
        projection = projections.snapshot_values(
            con,
            {"id": str(event.event_id)},
            {"id": str(event.revision_id), "currency": "USD"},
            book["account"]["id"],
        )
        insert(con, "dividend_projection_versions", projection)
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id), json=body(event.revision_id), headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("same_key", [False, True])
async def test_competing_rejections_commit_at_most_one_decision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], same_key: bool
) -> None:
    event = book["event"]
    intent = body(event.revision_id)
    first_key = "synthetic-concurrent-reject-a"
    other_key = first_key if same_key else "synthetic-concurrent-reject-b"
    before = database_snapshot(engine)
    first, second = await asyncio.gather(
        client.post(
            endpoint(event.event_id), json=intent, headers=version_headers(client, 1, key=first_key)
        ),
        client.post(
            endpoint(event.event_id), json=intent, headers=version_headers(client, 1, key=other_key)
        ),
    )
    assert sorted([first.status_code, second.status_code]) == (
        [200, 200] if same_key else [200, 409]
    )
    after = database_snapshot(engine)
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + 1
    if same_key:
        assert first.json() == second.json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_id", "not-a-uuid"),
        ("citation", ""),
        ("reason", "short"),
        ("evidence_url", "https://user:password@example.test/private"),
        ("resolve_rejection_id", str(uuid4())),
    ],
)
async def test_invalid_rejection_does_not_write_or_echo_private_input(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: Any
) -> None:
    event = book["event"]
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id),
        json={**body(event.revision_id), field: value},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 422
    assert "private=synthetic" not in response.text and "user:password" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("stale", ["physical", "version"])
async def test_rejection_requires_exact_event_and_physical_pins(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], stale: str
) -> None:
    event = book["event"]
    intent = body(uuid4() if stale == "physical" else event.revision_id)
    headers = version_headers(client, 2 if stale == "version" else 1)
    before = database_snapshot(engine)
    response = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert response.status_code == 412 and response.json()["code"] == "PRECONDITION_FAILED"
    assert database_snapshot(engine) == before


async def test_competing_new_key_cannot_replace_an_existing_rejection(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    intent = body(event.revision_id)
    first = await client.post(
        endpoint(event.event_id),
        json=intent,
        headers=version_headers(client, 1, key="synthetic-original-rejection"),
    )
    assert first.status_code == 200, first.text
    before = database_snapshot(engine)
    second = await client.post(
        endpoint(event.event_id),
        json={**intent, "reason": REASON + " Another decision."},
        headers=version_headers(client, 1, key="synthetic-another-rejection"),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "DIVIDEND_REVISION_ALREADY_REJECTED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(("failure", "status"), [("session", 401), ("csrf", 403), ("origin", 403)])
async def test_rejection_requires_session_csrf_and_same_origin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str, status: int
) -> None:
    event = book["event"]
    headers = version_headers(client, 1)
    if failure == "session":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "synthetic-invalid-csrf-token"
    else:
        headers["Origin"] = "https://untrusted.example.test"
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id), json=body(event.revision_id), headers=headers
    )
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_reject_another_owners_event(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
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
    response = await client.post(
        endpoint(event.event_id), json=body(event.revision_id), headers=version_headers(client, 1)
    )
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    assert database_snapshot(engine) == before


async def test_changed_unredacted_intent_cannot_replay_even_if_request_hash_is_forged(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    key = "synthetic-rejection-fake-hash"
    headers = version_headers(client, 1, key=key)
    intent = body(event.revision_id)
    saved = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert saved.status_code == 200, saved.text
    changed = {**intent, "evidence_url": "https://example.test/notice?private=changed"}
    with unit_of_work(engine, write=True) as con:
        result = con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=? "
            "WHERE owner_id=? AND route=? AND idem_key=?",
            (
                digest(canonical({"event_version": 1, **changed})),
                book["owner"]["id"],
                "POST " + endpoint(event.event_id),
                key,
            ),
        )
        assert result.rowcount == 1
    before = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=changed, headers=headers)
    assert replay.status_code == 503
    assert "private=changed" not in replay.text and "private=synthetic" not in replay.text
    assert database_snapshot(engine) == before


async def test_duplicate_late_http_intent_audit_blocks_replay_without_repairs(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-rejection-late-audit")
    intent = body(event.revision_id)
    result = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert result.status_code == 200, result.text
    status = await client.get(f"/api/v1/dividends/events/{event.event_id}/rejection")
    rejection_id = status.json()["rejection"]["id"]
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_rejection_http_intent",
            rejection_id,
            "DIVIDEND_REJECTION_HTTP_INTENT",
            request_id=str(uuid4()),
            after={"synthetic": "late duplicate"},
        )
    before = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 503 and "private=synthetic" not in replay.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["intent-audit", "intent-check", "response", "release"])
async def test_failure_after_rejection_rolls_back_every_write_and_allows_same_key_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    event = book["event"]
    intent = body(event.revision_id)
    headers = version_headers(client, 1, key=f"synthetic-reject-rollback-{failure}")
    target, name, error = {
        "intent-audit": (event_routes, "audit", ValueError("private=synthetic")),
        "intent-check": (event_routes, "_check_rejection_intent", ValueError("private=synthetic")),
        "response": (event_routes, "_current", ValueError("private=synthetic")),
        "release": (Connection, "_release_savepoint_impl", SQLAlchemyError("private=synthetic")),
    }[failure]
    before = database_snapshot(engine)
    with patch.object(target, name, side_effect=error):
        response = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert response.status_code == 503 and "private=synthetic" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    retried = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert retried.status_code == 200, retried.text
    after = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == retried.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    "corruption", ["valid-substitution", "missing-scope", "event-id", "version"]
)
async def test_forged_cached_response_cannot_replace_the_audited_historical_event(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    event = book["event"]
    key = "synthetic-reject-forged-response"
    headers = version_headers(client, 1, key=key)
    intent = body(event.revision_id)
    result = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert result.status_code == 200, result.text
    changed = result.json()
    if corruption == "valid-substitution":
        changed["dps"] = "1.90000000"
    elif corruption == "event-id":
        changed["id"] = str(uuid4())
    elif corruption == "version":
        changed["version"] = 2
    else:
        changed.pop("eligibility_scope")
    with unit_of_work(engine, write=True) as con:
        updated = con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? "
            "WHERE owner_id=? AND route=? AND idem_key=?",
            (json.dumps(changed), book["owner"]["id"], "POST " + endpoint(event.event_id), key),
        )
        assert updated.rowcount == 1
    before = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 503 and "private=synthetic" not in replay.text
    assert database_snapshot(engine) == before


async def test_historical_replay_requires_recent_password_confirmation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-reject-reauth-replay")
    intent = body(event.revision_id)
    saved = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert saved.status_code == 200, saved.text
    before = database_snapshot(engine)
    with patch.object(security, "REAUTH_AGE", timedelta(0)):
        denied = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    after = database_snapshot(engine)
    replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == saved.json()
    assert database_snapshot(engine) == after


async def test_rejection_contract_requires_physical_pin_citation_and_security(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    spec = (await client.get("/openapi.json")).json()
    operation = spec["paths"]["/api/v1/dividends/{event_id}/verify"]["post"]
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {entry["name"]: entry for entry in operation["parameters"] if entry["in"] == "header"}
    assert all(
        headers[name]["required"] for name in ("X-CSRF-Token", "Idempotency-Key", "If-Match")
    )
    union = operation["requestBody"]["content"]["application/json"]["schema"]
    assert union["discriminator"]["mapping"]["REJECT_REVISION"].endswith("/RejectDividendRevision")
    schema = spec["components"]["schemas"]["RejectDividendRevision"]
    assert set(schema["required"]) == {"revision_id", "decision", "citation", "reason"}
    assert schema["properties"]["decision"]["const"] == "REJECT_REVISION"
    assert schema["additionalProperties"] is False
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("code", "status", "public_code"),
    [
        ("DIVIDEND_REJECTION_NOT_FOUND", 404, "NOT_FOUND"),
        ("DIVIDEND_REJECTION_OWNER_UNAVAILABLE", 401, "UNAUTHORIZED"),
        ("DIVIDEND_REJECTION_VERSION_CONFLICT", 412, "PRECONDITION_FAILED"),
        ("DIVIDEND_REJECTION_CONFLICT", 409, "DIVIDEND_REJECTION_CONFLICT"),
        ("DIVIDEND_REJECTION_TIME_CONFLICT", 409, "DIVIDEND_REJECTION_TIME_CONFLICT"),
        ("INVALID_DIVIDEND_REJECTION_SCOPE", 422, "INVALID_DIVIDEND_REJECTION_SCOPE"),
        ("UNKNOWN_PRIVATE_FAILURE", 503, "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"),
    ],
)
async def test_internal_rejection_failures_map_to_fixed_private_errors(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    code: str,
    status: int,
    public_code: str,
) -> None:
    event = book["event"]
    before = database_snapshot(engine)
    with patch.object(rejection_writer, "save", side_effect=rejections.RejectionError(code)):
        response = await client.post(
            endpoint(event.event_id),
            json=body(event.revision_id),
            headers=version_headers(client, 1),
        )
    assert response.status_code == status and response.json()["code"] == public_code
    assert "private=synthetic" not in response.text
    assert database_snapshot(engine) == before


async def test_mismatched_http_intent_binding_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-rejection-binding-substitution")
    intent = body(event.revision_id)
    assert (
        await client.post(endpoint(event.event_id), json=intent, headers=headers)
    ).status_code == 200
    before = database_snapshot(engine)
    with patch.object(event_routes, "_rejection_binding", return_value={"synthetic": "mismatch"}):
        replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 503 and "private=synthetic" not in replay.text
    assert database_snapshot(engine) == before


async def test_http_intent_audit_cannot_predate_the_recorded_decision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-rejection-early-http-audit")
    intent = body(event.revision_id)
    assert (
        await client.post(endpoint(event.event_id), json=intent, headers=headers)
    ).status_code == 200
    with unit_of_work(engine) as con:
        created = con.exec_driver_sql(
            "SELECT created_at FROM audit_events "
            "WHERE entity_type='dividend_rejection_http_intent' "
            "AND action='DIVIDEND_REJECTION_HTTP_INTENT'"
        ).scalar_one()
    original_time = events._time

    def shifted(value: object) -> Any:
        result = original_time(value)
        return result - timedelta(days=1) if value == created else result

    before = database_snapshot(engine)
    with patch.object(events, "_time", side_effect=shifted):
        replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 503
    assert database_snapshot(engine) == before


async def test_missing_historical_rejection_blocks_cached_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-rejection-missing-history")
    intent = body(event.revision_id)
    assert (
        await client.post(endpoint(event.event_id), json=intent, headers=headers)
    ).status_code == 200
    before = database_snapshot(engine)
    with patch.object(rejections, "history", return_value=()):
        replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 503
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_no", 2),
        ("citation", "Synthetic substituted citation"),
        ("source_url", "https://example.test/changed"),
        ("reason", "Synthetic substituted reason."),
    ],
)
async def test_substituted_retained_rejection_cannot_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: object
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-rejection-substituted-record")
    intent = body(event.revision_id)
    assert (
        await client.post(endpoint(event.event_id), json=intent, headers=headers)
    ).status_code == 200
    with unit_of_work(engine) as con:
        retained = rejections.history(con, UUID(book["owner"]["id"]), event.event_id)
    assert retained is not None and len(retained) == 1
    altered = (retained[0].model_copy(update={field: value}),)
    before = database_snapshot(engine)
    with patch.object(rejections, "history", return_value=altered):
        replay = await client.post(endpoint(event.event_id), json=intent, headers=headers)
    assert replay.status_code == 503 and "private=synthetic" not in replay.text
    assert database_snapshot(engine) == before


async def test_competing_entitlement_and_rejection_cannot_commit_together(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    claim = entitlement({"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"])
    claim["created_at"] = stamp()

    def post_entitlement() -> bool:
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, "dividend_entitlements", claim)
            return True
        except IntegrityError:
            return False

    result, posted = await asyncio.gather(
        client.post(
            endpoint(event.event_id),
            json=body(event.revision_id),
            headers=version_headers(client, 1, key="synthetic-reject-entitlement-race"),
        ),
        asyncio.to_thread(post_entitlement),
    )
    assert (result.status_code, posted) in {(200, False), (409, True)}
    after = database_snapshot(engine)
    assert (len(after["dividend_event_rejections"]), len(after["dividend_entitlements"])) == (
        (0, 1) if posted else (1, 0)
    )
    if posted:
        assert result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"


async def test_unadvanceable_event_version_cannot_be_rejected(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    with unit_of_work(engine) as con:
        current = events.get(con, UUID(book["owner"]["id"]), event.event_id)
    assert current is not None
    exhausted = replace(
        current, record=current.record.model_copy(update={"revision_no": 2_147_483_647})
    )
    before = database_snapshot(engine)
    with patch.object(event_routes, "_owned", return_value=exhausted):
        response = await client.post(
            endpoint(event.event_id),
            json=body(event.revision_id),
            headers=version_headers(client, 2_147_483_647),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_VERSION_EXHAUSTED"
    assert database_snapshot(engine) == before


async def test_rejection_fences_receipts_of_earlier_physical_revisions(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    event_url = f"/api/v1/dividends/events/{event.event_id}"
    successor = await client.patch(
        event_url, json=revision(book), headers=version_headers(client, 1)
    )
    assert successor.status_code == 200, successor.text
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(event.event_id),
        json=body(successor.json()["current_revision_id"]),
        headers=version_headers(client, 2),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
            ).scalar_one()
            == 7_800_000_000_000
        )
