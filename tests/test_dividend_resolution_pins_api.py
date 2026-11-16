"""Protected event decisions require an explicit rejection pin for a successor."""

from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_models as models
import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_writer as rejection_writer
import spm_api.mutations as mutations
from spm_api.audit import canonical, digest
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_event_write_api import revision
from tests.test_dividend_manual_override_api import override_body
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_tax_api import book as book
from tests.test_dividend_verification_api import body as acceptance_body
from tests.test_dividend_withdrawal_api import withdrawal_body

pytestmark = pytest.mark.anyio


def operation(book: dict[str, Any], kind: str) -> tuple[str, str, dict[str, Any], type[Any]]:
    event = book["event"]
    path = f"/api/v1/dividends/events/{event.event_id}"
    if kind == "manual":
        return "PATCH", path, revision(book), models.ManualEventRevision
    if kind == "accept":
        return (
            "POST",
            f"/api/v1/dividends/{event.event_id}/verify",
            acceptance_body(event.revision_id),
            models.AcceptDividendRevision,
        )
    if kind == "override":
        return (
            "POST",
            f"/api/v1/dividends/{event.event_id}/verify",
            override_body(book),
            models.ManualDividendOverride,
        )
    return "POST", path + "/withdraw", withdrawal_body(book), models.DividendWithdrawal


def reject(engine: Engine, book: dict[str, Any], event: Any) -> Any:
    with unit_of_work(engine, write=True) as con:
        return rejection_writer.save(
            con, UUID(book["owner"]["id"]), rejection_intent(event), request_id=uuid4()
        )


@pytest.mark.parametrize("kind", ["manual", "accept", "override", "withdraw"])
async def test_explicit_pin_publishes_one_successor_and_replays_original_decision(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    event = book["event"]
    rejected = reject(engine, book, event)
    method, url, body, _ = operation(book, kind)
    before = database_snapshot(engine)
    missing = await client.request(method, url, json=body, headers=version_headers(client, 1))
    assert missing.status_code == 409
    assert missing.json()["code"] == "DIVIDEND_REJECTION_CONFIRMATION_REQUIRED"
    wrong = await client.request(
        method,
        url,
        json={**body, "resolve_rejection_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert wrong.status_code == 412 and wrong.json()["code"] == "PRECONDITION_FAILED"
    assert database_snapshot(engine) == before
    pinned = {**body, "resolve_rejection_id": str(rejected.id)}
    headers = version_headers(client, 1, key=f"synthetic-resolution-{kind}")
    result = await client.request(method, url, json=pinned, headers=headers)
    assert result.status_code == 200, result.text
    assert result.headers["etag"] == '"2"' and result.headers["cache-control"] == "no-store"
    assert result.json()["version"] == 2
    after = database_snapshot(engine)
    with unit_of_work(engine) as con:
        reviewed = events.get(con, UUID(book["owner"]["id"]), event.event_id)
        assert reviewed is not None and reviewed.resolution is not None
        assert reviewed.resolution.rejection_id == rejected.id
    assert (
        len(after["dividend_rejection_resolutions"])
        == len(before["dividend_rejection_resolutions"]) + 1
    )
    for table in ("dividend_entitlements", "dividend_receipts", "cash_ledger_entries"):
        assert after[table] == before[table]
    assert (await client.request(method, url, json=pinned, headers=headers)).json() == result.json()
    assert database_snapshot(engine) == after
    changed = await client.request(
        method, url, json={**pinned, "resolve_rejection_id": str(uuid4())}, headers=headers
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == after
    reject(engine, book, reviewed.record)
    later = database_snapshot(engine)
    replay = await client.request(method, url, json=pinned, headers=headers)
    assert replay.status_code == 200 and replay.json() == result.json()
    assert database_snapshot(engine) == later


@pytest.mark.parametrize("kind", ["manual", "accept", "override", "withdraw"])
async def test_absent_or_null_pin_preserves_legacy_idempotency_fingerprint(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    method, url, body, model = operation(book, kind)
    key = f"synthetic-legacy-resolution-{kind}"
    headers = version_headers(client, 1, key=key)
    result = await client.request(method, url, json=body, headers=headers)
    assert result.status_code == 200, result.text
    normalized = model.model_validate(body).model_dump(
        mode="json", exclude={"resolve_rejection_id"}
    )
    with unit_of_work(engine) as con:
        stored = con.exec_driver_sql(
            "SELECT request_sha256 FROM idempotency_keys WHERE idem_key=?", (key,)
        ).scalar_one()
    assert stored == digest(canonical({"event_version": 1, **normalized}))
    before = database_snapshot(engine)
    replay = await client.request(
        method, url, json={**body, "resolve_rejection_id": None}, headers=headers
    )
    assert replay.status_code == 200 and replay.json() == result.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["manual", "accept", "override", "withdraw"])
async def test_replay_cannot_hide_a_mismatched_retained_resolution(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    rejected = reject(engine, book, book["event"])
    method, url, body, _ = operation(book, kind)
    pinned = {**body, "resolve_rejection_id": str(rejected.id)}
    headers = version_headers(client, 1, key=f"synthetic-bad-resolution-replay-{kind}")
    saved = await client.request(method, url, json=pinned, headers=headers)
    assert saved.status_code == 200, saved.text
    before = database_snapshot(engine)
    original_get = events.get

    def substituted(*args: Any, **kwargs: Any) -> Any:
        reviewed = original_get(*args, **kwargs)
        if kwargs.get("version") == 2 and reviewed is not None:
            return replace(reviewed, resolution=events.ResolutionLink(uuid4(), uuid4()))
        return reviewed

    with patch.object(events, "get", side_effect=substituted):
        replay = await client.request(method, url, json=pinned, headers=headers)
    assert (
        replay.status_code == 503 and replay.json()["code"] == "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
    )
    assert replay.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["manual", "accept", "override", "withdraw"])
async def test_failed_response_serialization_rolls_back_resolution_and_retries_same_key(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    rejected = reject(engine, book, book["event"])
    method, url, body, _ = operation(book, kind)
    pinned = {**body, "resolve_rejection_id": str(rejected.id)}
    headers = version_headers(client, 1, key=f"synthetic-resolution-rollback-{kind}")
    before = database_snapshot(engine)
    with patch.object(routes, "_current", side_effect=ValueError("synthetic private failure")):
        failed = await client.request(method, url, json=pinned, headers=headers)
    assert failed.status_code == 503 and "synthetic private failure" not in failed.text
    assert failed.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    retried = await client.request(method, url, json=pinned, headers=headers)
    assert retried.status_code == 200, retried.text
    after = database_snapshot(engine)
    assert (
        len(after["dividend_rejection_resolutions"])
        == len(before["dividend_rejection_resolutions"]) + 1
    )
    assert (
        await client.request(method, url, json=pinned, headers=headers)
    ).json() == retried.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_pinned_resolution_rechecks_session_csrf_and_reauthentication_in_writer(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    failure: str,
    replay: bool,
) -> None:
    rejected = reject(engine, book, book["event"])
    method, url, body, _ = operation(book, "accept")
    pinned = {**body, "resolve_rejection_id": str(rejected.id)}
    headers = version_headers(client, 1, key=f"synthetic-resolution-auth-{failure}")
    if replay:
        assert (await client.request(method, url, json=pinned, headers=headers)).status_code == 200
    before = database_snapshot(engine)
    name, status, code = {
        "revoked": ("read_auth", 401, "UNAUTHORIZED"),
        "csrf": ("validate_csrf", 403, "CSRF_REJECTED"),
        "reauth": ("require_reauth", 403, "REAUTH_REQUIRED"),
    }[failure]
    with patch.object(mutations, name, side_effect=ApiError(status, code, "Synthetic denial")):
        denied = await client.request(method, url, json=pinned, headers=headers)
    assert denied.status_code == status and denied.json()["code"] == code
    assert denied.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
