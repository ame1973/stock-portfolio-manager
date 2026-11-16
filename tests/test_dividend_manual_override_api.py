"""Protected manual override decisions preserve explicit provenance and financial history."""

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_models as models
import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
import tests.test_dividend_projection_migration as projections
from spm_api.audit import audit, canonical, digest, now, stamp
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_read_api import append
from tests.test_dividend_event_write_api import only_event_writes, payload, safe
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
REASON = "Synthetic owner override of explicitly observed dividend terms."


def endpoint(book: dict[str, Any]) -> str:
    return f"/api/v1/dividends/{book['event'].event_id}/verify"


def override_body(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    values = payload(book, revision_id=str(book["event"].revision_id), reason=REASON)
    values["evidence_url"] = values.pop("source_url")
    return {**values, "decision": "MANUAL_OVERRIDE", **changes}


def internal_override(book: dict[str, Any]) -> writer.EventIntent:
    return writer.EventIntent(
        event_id=book["event"].event_id,
        revision_id=uuid4(),
        revision_no=2,
        kind="MANUAL",
        manual_override=True,
        terms=book["event"].terms,
        citation="Synthetic explicit override reference",
        reason=REASON,
    )


async def test_a_late_override_marker_cannot_relabel_an_ordinary_manual_revision(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    manual = await client.patch(
        f"/api/v1/dividends/events/{event.event_id}",
        json=payload(book, revision_id=str(event.revision_id)),
        headers=version_headers(client, 1),
    )
    assert manual.status_code == 200 and manual.json()["manual_override"] is False
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        previous = events.get(con, owner, event.event_id, version=1)
        current = events.get(con, owner, event.event_id, version=2)
        assert previous is not None and current is not None
        audit(
            con,
            str(owner),
            "dividend_event_revision",
            str(current.record.revision_id),
            "DIVIDEND_EVENT_MANUAL_OVERRIDE",
            request_id=str(current.record.request_id),
            before=events._audit_values(previous.record),
            after=events._override_audit_values(previous.record, current.record),
            reason=current.record.reason,
        )
    before = database_snapshot(engine)
    denied = await client.get(f"/api/v1/dividends/events/{event.event_id}")
    assert denied.status_code == 503
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("origin", ["initial", "verification"])
async def test_override_marker_on_an_invalid_origin_fails_closed(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, origin: str
) -> None:
    event_id, owner = book["event"].event_id, UUID(book["owner"]["id"])
    number = 1
    if origin == "verification":
        append(engine, book, kind="VERIFY", number=2, event_id=event_id)
        number = 2
    with unit_of_work(engine, write=True) as con:
        reviewed = events.get(con, owner, event_id, version=number)
        assert reviewed is not None
        audit(
            con,
            str(owner),
            "dividend_event_revision",
            str(reviewed.record.revision_id),
            "DIVIDEND_EVENT_MANUAL_OVERRIDE",
            request_id=str(reviewed.record.request_id),
            after={"synthetic": "invalid override origin"},
            reason=reviewed.record.reason,
        )
    before = database_snapshot(engine)
    response = await client.get(f"/api/v1/dividends/events/{event_id}")
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_override_requires_explicit_terms_and_retains_audited_origin(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = append(engine, book, kind="VERIFY", number=2, event_id=book["event"].event_id)
    intent = override_body(book, revision_id=str(previous.revision_id), confirm_unlock=True)
    headers = version_headers(client, 2, key="synthetic-manual-override")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Owner override must not access a provider")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    response = await client.post(endpoint(book), json=intent, headers=headers)
    assert response.status_code == 200, response.text
    safe(response)
    result = response.json()
    assert response.headers["etag"] == '"3"'
    assert result["id"] == str(book["event"].event_id) and result["version"] == 3
    assert result["current_revision_id"] != str(previous.revision_id)
    assert result["decision_kind"] == "MANUAL" and result["manual_override"] is True
    assert result["workflow_status"] == "PROVISIONAL" and result["manual_locked"] is False
    assert result["dps"] == "1.23456789" and result["currency"] == "USD"
    assert result["document_verification"] == result["eligibility_scope"] == "NOT_ESTABLISHED"
    assert result["reference"] == {
        "citation": intent["citation"],
        "url": None,
        "url_withheld": True,
    }
    after = database_snapshot(engine)
    only_event_writes(before, after)
    with unit_of_work(engine) as con:
        marker = con.exec_driver_sql(
            "SELECT owner_id,request_id,reason FROM audit_events "
            "WHERE entity_type='dividend_event_revision' AND entity_id=? "
            "AND action='DIVIDEND_EVENT_MANUAL_OVERRIDE'",
            (result["current_revision_id"],),
        ).one()
    assert tuple(marker) == (book["owner"]["id"], response.headers["x-request-id"], REASON)
    replay = await client.post(endpoint(book), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == result
    assert database_snapshot(engine) == after

    successor = await client.patch(
        f"/api/v1/dividends/events/{book['event'].event_id}",
        json=payload(book, revision_id=result["current_revision_id"]),
        headers=version_headers(client, 3),
    )
    assert successor.status_code == 200 and successor.json()["manual_override"] is False
    before_replay = database_snapshot(engine)
    replay = await client.post(endpoint(book), json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == result
    assert database_snapshot(engine) == before_replay
    history = await client.get(f"/api/v1/dividends/events/{book['event'].event_id}/revisions")
    assert history.status_code == 200
    assert [item["manual_override"] for item in history.json()["items"]] == [
        False,
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    "field",
    [
        "decision",
        "revision_id",
        "security_id",
        "event_type",
        "ex_date",
        "dps",
        "currency",
        "adjustment_basis",
        "citation",
        "reason",
    ],
)
async def test_override_requires_complete_explicit_terms_and_reference(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str
) -> None:
    intent = override_body(book)
    del intent[field]
    before = database_snapshot(engine)
    response = await client.post(endpoint(book), json=intent, headers=version_headers(client, 1))
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision", "UNRECOGNIZED"),
        ("revision_id", "not-a-uuid"),
        ("dps", "0.123456789"),
        ("dps", "-0.01"),
        ("dps", 0.9),
        ("currency", "CNY"),
        ("adjustment_basis", "AS_GUESSED"),
        ("evidence_url", "https://user:password@example.test/notice"),
        ("source_url", "https://example.test/another"),
        ("reason", ""),
        ("confirm_unlock", "true"),
        ("confirm_reopen", 1),
    ],
)
async def test_override_rejects_invalid_or_ambiguous_inputs_without_writes(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str, value: Any
) -> None:
    before = database_snapshot(engine)
    response = await client.post(
        endpoint(book),
        json=override_body(book, **{field: value}),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


async def test_override_requires_separate_reopen_and_unlock_confirmation(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    append(engine, book, kind="VERIFY", number=2, event_id=event.event_id)
    withdrawn = append(engine, book, kind="WITHDRAW", number=3, event_id=event.event_id)
    base = override_body(book, revision_id=str(withdrawn.revision_id))
    before = database_snapshot(engine)
    for extra, code in (
        ({}, "DIVIDEND_EVENT_REOPEN_CONFIRMATION_REQUIRED"),
        ({"confirm_unlock": True}, "DIVIDEND_EVENT_REOPEN_CONFIRMATION_REQUIRED"),
        ({"confirm_reopen": True}, "DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED"),
    ):
        response = await client.post(
            endpoint(book), json={**base, **extra}, headers=version_headers(client, 3)
        )
        assert response.status_code == 409 and response.json()["code"] == code
        assert database_snapshot(engine) == before
    accepted = await client.post(
        endpoint(book),
        json={**base, "confirm_reopen": True, "confirm_unlock": True},
        headers=version_headers(client, 3),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["version"] == 4 and accepted.json()["manual_override"] is True
    assert accepted.json()["manual_locked"] is False


async def test_override_checks_physical_version_and_cannot_change_event_security(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    stale = await client.post(
        endpoint(book), json=override_body(book), headers=version_headers(client, 2)
    )
    wrong_revision = await client.post(
        endpoint(book),
        json=override_body(book, revision_id=str(uuid4())),
        headers=version_headers(client, 1),
    )
    other_security = await client.post(
        endpoint(book),
        json=override_body(book, security_id=str(uuid4())),
        headers=version_headers(client, 1),
    )
    assert stale.status_code == wrong_revision.status_code == 412
    assert other_security.status_code == 409
    assert other_security.json()["code"] == "DIVIDEND_EVENT_SECURITY_CHANGED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("basis", ["UNKNOWN", "SPLIT_ADJUSTED"])
async def test_override_keeps_unresolved_basis_and_zero_explicit(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, basis: str
) -> None:
    response = await client.post(
        endpoint(book),
        json=override_body(book, adjustment_basis=basis, dps="0"),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200, response.text
    assert response.json()["dps"] == "0.00000000"
    assert response.json()["workflow_status"] == "INCOMPLETE"
    assert response.json()["manual_override"] is True
    assert response.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    with unit_of_work(engine) as con:
        reviews = con.exec_driver_sql(
            "SELECT status,reason_code FROM review_items "
            "WHERE event_id=? AND kind='DIVIDEND_EVIDENCE'",
            (str(book["event"].event_id),),
        ).all()
    assert reviews and all(
        row.status == "OPEN" and row.reason_code == "DIVIDEND_INCOMPLETE" for row in reviews
    )


async def test_foreign_override_does_not_disclose_event(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
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
        endpoint(book), json=override_body(book), headers=version_headers(client, 1)
    )
    assert response.status_code == 404
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key", "reauth"])
async def test_override_requires_security_dependencies(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
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
    denied = await client.post(endpoint(book), json=override_body(book), headers=headers)
    assert (
        denied.status_code
        == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422, "reauth": 403}[failure]
    )
    safe(denied)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_override_writer_rechecks_security_before_new_or_cached_decision(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    replay: bool,
) -> None:
    intent = override_body(book)
    headers = version_headers(client, 1, key="synthetic-override-security")
    if replay:
        assert (await client.post(endpoint(book), json=intent, headers=headers)).status_code == 200
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
    denied = await client.post(endpoint(book), json=intent, headers=headers)
    assert denied.status_code == (401 if failure == "revoked" else 403)
    safe(denied)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_concurrent_override_retries_and_competing_revisions_serialize(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    intent = override_body(book)
    headers = version_headers(client, 1, key="synthetic-override-concurrent")
    one, replay = await asyncio.gather(
        *(client.post(endpoint(book), json=intent, headers=headers) for _ in range(2))
    )
    assert one.status_code == replay.status_code == 200 and one.json() == replay.json()
    assert one.json()["manual_override"] is True
    before = database_snapshot(engine)
    changed = await client.post(
        endpoint(book),
        json={**intent, "reason": "A different synthetic override reason."},
        headers=headers,
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before
    next_intent = override_body(book, revision_id=one.json()["current_revision_id"])
    outcomes = await asyncio.gather(
        *(
            client.post(endpoint(book), json=next_intent, headers=version_headers(client, 2))
            for _ in range(2)
        )
    )
    assert sorted(result.status_code for result in outcomes) == [200, 412]


@pytest.mark.parametrize("history", ["entitlement", "receipt", "projection"])
async def test_override_preserves_existing_financial_history_and_publication_fence(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    history: str,
) -> None:
    event = book["event"]
    with unit_of_work(engine, write=True) as con:
        if history == "projection":
            monkeypatch.setattr(projections, "OWNER", UUID(book["owner"]["id"]))
            values = projections.snapshot_values(
                con,
                {"id": str(event.event_id)},
                {"id": str(event.revision_id), "currency": "USD"},
                book["account"]["id"],
            )
            insert(con, "dividend_projection_versions", values)
        else:
            claim = entitlement(
                {"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"]
            )
            claim["created_at"] = stamp()
            insert(con, "dividend_entitlements", claim)
            if history == "receipt":
                add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    denied = await client.post(
        endpoint(book), json=override_body(book), headers=version_headers(client, 1)
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == before


async def test_receipt_arriving_before_writer_blocks_override_without_losing_fx(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = book["event"]
    checkpoint: dict[str, Any] = {}

    def post_before_writer(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            claim = entitlement(
                {"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"]
            )
            claim["created_at"] = stamp()
            insert(con, "dividend_entitlements", claim)
            add_posted_receipt(con, claim, fx=7_800_000_000_000)
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", post_before_writer)
    denied = await client.post(
        endpoint(book), json=override_body(book), headers=version_headers(client, 1)
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize("failure", ["override-audit", "intent-audit", "serialization"])
async def test_override_failure_rolls_back_all_effects_and_allows_original_key_retry(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    intent = override_body(book)
    headers = version_headers(client, 1, key="synthetic-override-rollback")
    before = database_snapshot(engine)
    with monkeypatch.context() as patcher:
        if failure == "override-audit":

            def fail_override_audit(*args: Any, **kwargs: Any) -> None:
                audit(*args, **kwargs)
                if args[4] == "DIVIDEND_EVENT_MANUAL_OVERRIDE":
                    raise SQLAlchemyError("synthetic-private-override")

            patcher.setattr(writer, "audit", fail_override_audit)
        elif failure == "intent-audit":

            def fail_intent_audit(*args: Any, **kwargs: Any) -> None:
                audit(*args, **kwargs)
                raise SQLAlchemyError("synthetic-private-override")

            patcher.setattr(routes, "audit", fail_intent_audit)
        else:

            def fail_serialization(*args: Any, **kwargs: Any) -> Any:
                raise ValueError("synthetic-private-override")

            patcher.setattr(routes, "_current", fail_serialization)
        failed = await client.post(endpoint(book), json=intent, headers=headers)
    assert failed.status_code == 503 and "synthetic-private-override" not in failed.text
    assert database_snapshot(engine) == before
    retry = await client.post(endpoint(book), json=intent, headers=headers)
    assert retry.status_code == 200 and retry.json()["manual_override"] is True


async def test_missing_override_marker_cannot_be_reclassified_as_manual(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = override_body(book)
    headers = version_headers(client, 1, key="synthetic-override-missing-marker")
    first = await client.post(endpoint(book), json=intent, headers=headers)
    assert first.status_code == 200 and first.json()["manual_override"] is True
    before = database_snapshot(engine)
    monkeypatch.setattr(events, "_manual_override", lambda *args, **kwargs: False)
    denied = await client.get(f"/api/v1/dividends/events/{book['event'].event_id}")
    replay = await client.post(endpoint(book), json=intent, headers=headers)
    assert denied.status_code == replay.status_code == 503
    assert database_snapshot(engine) == before


async def test_duplicate_override_audit_fails_closed_for_reads_and_replay(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    intent = override_body(book)
    headers = version_headers(client, 1, key="synthetic-override-duplicate-marker")
    first = await client.post(endpoint(book), json=intent, headers=headers)
    assert first.status_code == 200
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine, write=True) as con:
        previous = events.get(con, owner, event.event_id, version=1)
        current = events.get(con, owner, event.event_id, version=2)
        assert previous is not None and current is not None
        audit(
            con,
            str(owner),
            "dividend_event_revision",
            str(current.record.revision_id),
            "DIVIDEND_EVENT_MANUAL_OVERRIDE",
            request_id=str(current.record.request_id),
            before=events._audit_values(previous.record),
            after=events._override_audit_values(previous.record, current.record),
            reason=current.record.reason,
        )
    before = database_snapshot(engine)
    for path in (
        f"/api/v1/dividends/events/{event.event_id}",
        f"/api/v1/dividends/events/{event.event_id}/revisions",
    ):
        assert (await client.get(path)).status_code == 503
    assert (await client.post(endpoint(book), json=intent, headers=headers)).status_code == 503
    assert database_snapshot(engine) == before


async def test_override_cached_response_cannot_hide_its_origin(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    intent = override_body(book)
    headers = version_headers(client, 1, key="synthetic-override-corrupt-cache")
    first = await client.post(endpoint(book), json=intent, headers=headers)
    assert first.status_code == 200 and first.json()["manual_override"] is True
    forged = first.json()
    forged["manual_override"] = False
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(forged), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(endpoint(book), json=intent, headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("citation", "A different synthetic override reference"),
        ("evidence_url", "https://example.test/dividend?different=secret"),
        ("reason", "A different synthetic override reason."),
    ],
)
async def test_override_replay_validates_original_unredacted_intent(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str, value: str
) -> None:
    headers = version_headers(client, 1, key="synthetic-override-intent")
    first = await client.post(endpoint(book), json=override_body(book), headers=headers)
    assert first.status_code == 200 and first.json()["reference"]["url"] is None
    changed = override_body(book, **{field: value})
    normalized = {
        "event_version": 1,
        **models.ManualDividendOverride.model_validate(changed).model_dump(
            mode="json", exclude={"resolve_rejection_id"}
        ),
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
            (digest(canonical(normalized)), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(endpoint(book), json=changed, headers=headers)
    assert replay.status_code == 503 and value not in replay.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["manual", "accept", "withdrawal"])
async def test_old_cached_event_responses_gain_only_the_false_override_label(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, operation: str
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key=f"synthetic-old-{operation}-response")
    if operation == "manual":
        path = f"/api/v1/dividends/events/{event.event_id}"
        method = client.patch
        intent = payload(book, revision_id=str(event.revision_id))
    elif operation == "accept":
        path = endpoint(book)
        method = client.post
        intent = {
            "revision_id": str(event.revision_id),
            "decision": "ACCEPT_REVISION",
            "citation": "Synthetic acceptance reference",
            "reason": REASON,
        }
    else:
        path = f"/api/v1/dividends/events/{event.event_id}/withdraw"
        method = client.post
        intent = {
            "revision_id": str(event.revision_id),
            "confirmation": "WITHDRAW_EVENT",
            "citation": "Synthetic withdrawal reference",
            "reason": REASON,
        }
    first = await method(path, json=intent, headers=headers)
    assert first.status_code == 200 and first.json()["manual_override"] is False
    legacy = first.json()
    del legacy["manual_override"]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(legacy), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await method(path, json=intent, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == before


async def test_override_contract_exposes_discriminated_decisions_and_full_terms(
    book: dict[str, Any], client: httpx.AsyncClient
) -> None:
    spec = (await client.get("/openapi.json")).json()
    operation = spec["paths"]["/api/v1/dividends/{event_id}/verify"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["discriminator"]["propertyName"] == "decision"
    assert set(request_schema["discriminator"]["mapping"]) == {
        "ACCEPT_REVISION",
        "REJECT_REVISION",
        "MANUAL_OVERRIDE",
    }
    schema = spec["components"]["schemas"]["ManualDividendOverride"]
    assert {
        "decision",
        "revision_id",
        "security_id",
        "event_type",
        "ex_date",
        "dps",
        "currency",
        "adjustment_basis",
        "citation",
        "reason",
    } <= set(schema["required"])
    assert "evidence_url" in schema["properties"] and "source_url" not in schema["properties"]
    assert schema["properties"]["decision"]["const"] == "MANUAL_OVERRIDE"
    assert schema["additionalProperties"] is False
    assert (
        spec["components"]["schemas"]["DividendEventRecord"]["properties"]["manual_override"][
            "default"
        ]
        is False
    )


@pytest.mark.parametrize(
    ("kind", "number", "flag"),
    [
        ("MANUAL", 1, True),
        ("VERIFY", 2, True),
        ("WITHDRAW", 2, True),
        ("CANDIDATE", 2, True),
        ("MANUAL", 2, 1),
        ("MANUAL", 2, "true"),
    ],
)
async def test_internal_override_flag_is_strict_and_only_for_manual_successors(
    book: dict[str, Any], kind: str, number: int, flag: Any
) -> None:
    with pytest.raises(ValidationError):
        writer.EventIntent.model_validate(
            {
                "event_id": book["event"].event_id,
                "revision_id": uuid4(),
                "revision_no": number,
                "kind": kind,
                "manual_override": flag,
                "terms": book["event"].terms,
                "citation": "Synthetic override reference",
                "reason": REASON,
            }
        )


async def test_internal_override_replay_requires_original_origin_flag(
    book: dict[str, Any], engine: Engine
) -> None:
    owner = UUID(book["owner"]["id"])
    intent = internal_override(book)
    created_at, request_id = now(), uuid4()
    with unit_of_work(engine, write=True) as con:
        first = writer.save(con, owner, intent, at=created_at, request_id=request_id)
    assert first.manual_override is True
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        replay = writer.save(con, owner, intent, at=created_at, request_id=request_id)
    assert replay == first and database_snapshot(engine) == before
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_REVISION_CONFLICT$"),
    ):
        writer.save(
            con,
            owner,
            intent.model_copy(update={"manual_override": False}),
            at=created_at,
            request_id=request_id,
        )
    assert database_snapshot(engine) == before


async def test_caught_internal_override_failure_cannot_commit_partial_history(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = internal_override(book)
    before = database_snapshot(engine)

    def fail_after_audit(*args: Any, **kwargs: Any) -> None:
        audit(*args, **kwargs)
        if args[4] == "DIVIDEND_EVENT_MANUAL_OVERRIDE":
            raise SQLAlchemyError("Synthetic audit failure")

    monkeypatch.setattr(writer, "audit", fail_after_audit)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(SQLAlchemyError, match="Synthetic audit failure"),
    ):
        writer.save(con, UUID(book["owner"]["id"]), intent, at=now(), request_id=uuid4())
    assert database_snapshot(engine) == before


async def test_caught_override_failure_preserves_unrelated_outer_work(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, before = UUID(book["owner"]["id"]), database_snapshot(engine)

    def fail_after_audit(*args: Any, **kwargs: Any) -> None:
        audit(*args, **kwargs)
        if args[4] == "DIVIDEND_EVENT_MANUAL_OVERRIDE":
            raise SQLAlchemyError("Synthetic audit failure")

    monkeypatch.setattr(writer, "audit", fail_after_audit)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "synthetic_outer_work",
            str(uuid4()),
            "SYNTHETIC_OUTER_WORK",
            request_id=str(uuid4()),
            after={"synthetic": True},
            reason=REASON,
        )
        with pytest.raises(SQLAlchemyError, match="Synthetic audit failure"):
            writer.save(con, owner, internal_override(book), at=now(), request_id=uuid4())
        assert con.in_transaction()
    after = database_snapshot(engine)
    assert {key: value for key, value in after.items() if key != "audit_events"} == {
        key: value for key, value in before.items() if key != "audit_events"
    }
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1


async def test_outer_rollback_removes_a_successful_override_savepoint(
    book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(RuntimeError, match="Synthetic outer rollback"),
        unit_of_work(engine, write=True) as con,
    ):
        writer.save(
            con, UUID(book["owner"]["id"]), internal_override(book), at=now(), request_id=uuid4()
        )
        raise RuntimeError("Synthetic outer rollback")
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["begin", "release"])
async def test_uncertain_override_savepoint_aborts_the_outer_transaction(
    book: dict[str, Any], engine: Engine, phase: str
) -> None:
    owner, before = UUID(book["owner"]["id"]), database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "synthetic_outer_work",
            str(uuid4()),
            "SYNTHETIC_OUTER_WORK",
            request_id=str(uuid4()),
            after={"synthetic": True},
            reason=REASON,
        )
        target = con if phase == "begin" else engine.dialect
        method = "begin_nested" if phase == "begin" else "do_release_savepoint"
        with (
            patch.object(
                target, method, side_effect=SQLAlchemyError("Synthetic savepoint failure")
            ),
            pytest.raises(SQLAlchemyError, match="Synthetic savepoint failure"),
        ):
            writer.save(con, owner, internal_override(book), at=now(), request_id=uuid4())
        assert not con.in_transaction()
    assert database_snapshot(engine) == before


async def test_override_release_failure_keeps_key_available_for_retry(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    headers = version_headers(client, 1, key="synthetic-override-release")
    before = database_snapshot(engine)
    with patch.object(
        Connection,
        "_release_savepoint_impl",
        side_effect=SQLAlchemyError("private-synthetic savepoint failure"),
    ):
        response = await client.post(endpoint(book), json=override_body(book), headers=headers)
    assert response.status_code == 503 and "private-synthetic" not in response.text
    assert database_snapshot(engine) == before
    retry = await client.post(endpoint(book), json=override_body(book), headers=headers)
    assert retry.status_code == 200 and retry.json()["manual_override"] is True
