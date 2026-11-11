"""Protected manual event writes retain identity, history and economic safety fences."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
import tests.test_dividend_projection_migration as projections
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import PASSWORD, create, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_read_api import append
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/events"
REASON = "Synthetic owner decision, not external document verification."


def payload(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "security_id": str(book["event"].terms.security_id),
        "event_type": "REGULAR",
        "ex_date": "2026-01-08",
        "announce_date": "2025-12-01",
        "record_date": "2026-01-09",
        "pay_date": "2026-02-01",
        "dps": "1.23456789",
        "currency": "USD",
        "adjustment_basis": "AS_DECLARED",
        "citation": "Synthetic owner reference",
        "source_url": "https://example.test/dividend?private-value=synthetic#detail",
        "reason": REASON,
        **changes,
    }


def revision(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return payload(book, revision_id=str(book["event"].revision_id), **changes)


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "private-value" not in response.text
    assert '"owner_id"' not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert REASON not in response.text


def only_event_writes(before: Any, after: Any) -> None:
    allowed = {
        "dividend_events",
        "dividend_event_controls",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "audit_events",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
        "review_items",
    }
    assert {key: value for key, value in before.items() if key not in allowed} == {
        key: value for key, value in after.items() if key not in allowed
    }


async def test_manual_creation_is_audited_private_and_has_no_economic_effect(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Manual event decisions must not access providers")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    body = payload(book)
    headers = write_headers(client, key="synthetic-manual-event-create")
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=headers)
    assert response.status_code == 201, response.text
    safe(response)
    result = response.json()
    assert response.headers["location"] == f"{PATH}/{result['id']}"
    assert response.headers["etag"] == '"1"'
    assert UUID(result["id"]).version == UUID(result["current_revision_id"]).version == 4
    assert result["version"] == 1 and result["dps"] == "1.23456789"
    assert result["decision_kind"] == "MANUAL" and result["workflow_status"] == "PROVISIONAL"
    assert result["manual_locked"] is False and result["assessment"] is None
    assert result["independent_origin_groups"] is None and result["evidence"] == []
    assert result["reference"] == {
        "citation": body["citation"],
        "url": None,
        "url_withheld": True,
    }
    assert result["scope"] == "OWNER_EVENT_DECISION_ONLY"
    assert result["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert result["document_verification"] == result["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    only_event_writes(before, after)
    for name in (
        "dividend_events",
        "dividend_event_controls",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "idempotency_keys",
    ):
        assert len(after[name]) == len(before[name]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 2
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT action,request_id,reason FROM audit_events "
            "WHERE entity_type='dividend_event_revision' AND entity_id=? ORDER BY action",
            (result["current_revision_id"],),
        ).all()
        assert [row.action for row in rows] == [
            "DIVIDEND_EVENT_HTTP_INTENT",
            "DIVIDEND_EVENT_REVISED",
        ]
        assert all(row.request_id == response.headers["x-request-id"] for row in rows)
        assert rows[0].reason == "" and rows[1].reason == REASON
    replay = await client.post(PATH, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == result
    safe(replay)
    assert database_snapshot(engine) == after


async def test_manual_creation_accepts_unknown_optional_dates_and_exact_int64_dps(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    body = payload(book, dps="92233720368.54775807")
    for field in ("announce_date", "record_date", "pay_date", "source_url"):
        del body[field]
    result = await client.post(PATH, json=body, headers=write_headers(client))
    assert result.status_code == 201, result.text
    saved = result.json()
    assert saved["dps"] == "92233720368.54775807"
    assert saved["announce_date"] is saved["record_date"] is saved["pay_date"] is None
    assert saved["reference"]["url"] is None and saved["reference"]["url_withheld"] is False
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT dps_e8 FROM dividend_event_revisions WHERE id=?",
                (saved["current_revision_id"],),
            ).scalar_one()
            == 9_223_372_036_854_775_807
        )


async def test_missing_security_and_missing_physical_revision_do_not_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    missing_security = await client.post(
        PATH, json=payload(book, security_id=str(uuid4())), headers=write_headers(client)
    )
    body = revision(book)
    del body["revision_id"]
    missing_revision = await client.patch(
        f"{PATH}/{book['event'].event_id}", json=body, headers=version_headers(client, 1)
    )
    assert missing_security.status_code == 404 and missing_revision.status_code == 422
    safe(missing_security)
    safe(missing_revision)
    assert database_snapshot(engine) == before


async def test_manual_revision_keeps_stable_identity_and_historical_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    body = payload(book)
    headers = write_headers(client, key="synthetic-manual-event-historical-replay")
    first = await client.post(PATH, json=body, headers=headers)
    assert first.status_code == 201
    initial = first.json()
    change = {**body, "revision_id": initial["current_revision_id"], "dps": "0"}
    revised = await client.patch(
        f"{PATH}/{initial['id']}", json=change, headers=version_headers(client, 1)
    )
    assert revised.status_code == 200, revised.text
    safe(revised)
    current = revised.json()
    assert current["id"] == initial["id"] and current["version"] == 2
    assert current["current_revision_id"] != initial["current_revision_id"]
    assert current["dps"] == "0.00000000" and revised.headers["etag"] == '"2"'
    history = await client.get(f"{PATH}/{initial['id']}/revisions")
    assert [row["dps"] for row in history.json()["items"]] == ["0.00000000", "1.23456789"]
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == initial
    assert replay.headers["etag"] == '"1"'
    assert (await client.get(f"{PATH}/{initial['id']}")).json() == current
    assert database_snapshot(engine) == before


async def test_same_day_identical_intents_are_distinct_and_cache_cannot_swap_them(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    body = payload(book)
    first_headers = write_headers(client, key="synthetic-event-first-identical-intent")
    second_headers = write_headers(client, key="synthetic-event-second-identical-intent")
    first = await client.post(PATH, json=body, headers=first_headers)
    second = await client.post(PATH, json=body, headers=second_headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(second.json()), first_headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    swapped = await client.post(PATH, json=body, headers=first_headers)
    assert swapped.status_code == 503
    assert swapped.json()["code"] == "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
    safe(swapped)
    assert database_snapshot(engine) == before


async def test_raw_withheld_reference_is_checked_on_a_forged_cached_intent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    body = payload(book)
    headers = write_headers(client, key="synthetic-event-cached-private-reference")
    first = await client.post(PATH, json=body, headers=headers)
    assert first.status_code == 201 and first.json()["reference"]["url"] is None
    changed = {**body, "source_url": "https://example.test/dividend?different=secret"}
    from spm_api.dividend_event_models import ManualEventCreate

    changed_payload = ManualEventCreate.model_validate(changed).model_dump(mode="json")
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
            (digest(canonical(changed_payload)), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=changed, headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["label", "revision", "dps", "reason", "reference"])
async def test_cached_response_must_match_the_audited_original(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    headers = write_headers(client, key="synthetic-event-corrupt-cache")
    first = await client.post(PATH, json=payload(book), headers=headers)
    assert first.status_code == 201
    altered = first.json()
    if corruption == "label":
        del altered["eligibility_scope"]
    elif corruption == "revision":
        altered["current_revision_id"] = str(uuid4())
    elif corruption == "dps":
        altered["dps"] = "1.00000000"
    elif corruption == "reason":
        altered["reason"] = "An altered synthetic private reason"
    else:
        altered["reference"]["url_withheld"] = False
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(altered), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(book), headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_duplicate_late_binding_audit_or_revision_audit_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    headers = write_headers(client, key="synthetic-event-extra-binding-audit")
    first = await client.post(PATH, json=payload(book), headers=headers)
    assert first.status_code == 201
    saved = first.json()
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_event_revision",
            saved["current_revision_id"],
            "DIVIDEND_EVENT_HTTP_INTENT",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
        )
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=payload(book), headers=headers)
    assert replay.status_code == 503
    safe(replay)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_event_revision",
            saved["current_revision_id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{saved['id']}",
        json={**payload(book), "revision_id": saved["current_revision_id"]},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_authentication_csrf_origin_and_key_are_required(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    headers = write_headers(client)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "synthetic-invalid-csrf"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        headers["Idempotency-Key"] = "short"
    before = database_snapshot(engine)
    result = await client.post(PATH, json=payload(book), headers=headers)
    assert result.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(result)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_revise_an_owned_event(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        foreign = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (foreign, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, foreign, "synthetic-other-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{book['event'].event_id}", json=revision(book), headers=version_headers(client, 1)
    )
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    safe(response)
    assert database_snapshot(engine) == before


async def test_recent_reauthentication_is_required_even_for_historical_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    headers = write_headers(client, key="synthetic-event-reauth-replay")
    first = await client.post(PATH, json=payload(book), headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(PATH, json=payload(book), headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    safe(denied)
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=payload(book), headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_security_is_rechecked_inside_the_writer_transaction(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    headers = write_headers(client, key="synthetic-event-writer-security-race")
    if replay:
        assert (await client.post(PATH, json=payload(book), headers=headers)).status_code == 201
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
    result = await client.post(PATH, json=payload(book), headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize(
    "header", [None, "1", '"0"', '"01"', '"2147483648"', '"' + "9" * 4500 + '"']
)
async def test_revision_if_match_is_bounded_before_integer_conversion(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], header: str | None
) -> None:
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{book['event'].event_id}", json=revision(book), headers=headers
    )
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dps", 1.2),
        ("dps", "1.000000001"),
        ("dps", "-0.01"),
        ("dps", "92233720368.54775808"),
        ("currency", "CNY"),
        ("ex_date", "2026-02-30"),
        ("event_type", "unexpected-private-value"),
        ("source_url", "https://user:password@example.test/private-value"),
        ("citation", ""),
        ("reason", "bad\0reason"),
    ],
)
async def test_invalid_manual_terms_are_not_rounded_disclosed_or_written(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: object
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        PATH, json=payload(book, **{field: value}), headers=write_headers(client)
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "missing",
    [
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
async def test_manual_creation_requires_explicit_terms_and_reference(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], missing: str
) -> None:
    body = payload(book)
    del body[missing]
    before = database_snapshot(engine)
    result = await client.post(PATH, json=body, headers=write_headers(client))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


async def test_physical_revision_version_security_and_confirmation_are_pinned(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event_id = str(book["event"].event_id)
    before = database_snapshot(engine)
    wrong_id = {**revision(book), "revision_id": str(uuid4())}
    wrong_version = await client.patch(
        f"{PATH}/{event_id}", json=revision(book), headers=version_headers(client, 2)
    )
    wrong_physical = await client.patch(
        f"{PATH}/{event_id}", json=wrong_id, headers=version_headers(client, 1)
    )
    assert wrong_version.status_code == wrong_physical.status_code == 412
    changed_security = await client.patch(
        f"{PATH}/{event_id}",
        json={**revision(book), "security_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert changed_security.status_code == 409
    assert changed_security.json()["code"] == "DIVIDEND_EVENT_SECURITY_CHANGED"
    invalid_boolean = await client.patch(
        f"{PATH}/{event_id}",
        json={**revision(book), "confirm_unlock": "true"},
        headers=version_headers(client, 1),
    )
    assert invalid_boolean.status_code == 422
    for response in (wrong_version, wrong_physical, changed_security, invalid_boolean):
        safe(response)
    assert database_snapshot(engine) == before


async def test_locked_and_withdrawn_events_require_explicit_reopen_and_unlock(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event_id = book["event"].event_id
    verified = append(engine, book, kind="VERIFY", number=2, event_id=event_id)
    body = payload(book, revision_id=str(verified.revision_id))
    before = database_snapshot(engine)
    locked = await client.patch(f"{PATH}/{event_id}", json=body, headers=version_headers(client, 2))
    assert locked.status_code == 409
    assert locked.json()["code"] == "DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED"
    assert database_snapshot(engine) == before
    changed = await client.patch(
        f"{PATH}/{event_id}",
        json={**body, "confirm_unlock": True},
        headers=version_headers(client, 2),
    )
    assert changed.status_code == 200 and changed.json()["manual_locked"] is False
    append(engine, book, kind="VERIFY", number=4, event_id=event_id)
    withdrawn = append(engine, book, kind="WITHDRAW", number=5, event_id=event_id)
    body = payload(book, revision_id=str(withdrawn.revision_id))
    before = database_snapshot(engine)
    no_reopen = await client.patch(
        f"{PATH}/{event_id}", json=body, headers=version_headers(client, 5)
    )
    assert no_reopen.status_code == 409
    assert no_reopen.json()["code"] == "DIVIDEND_EVENT_REOPEN_CONFIRMATION_REQUIRED"
    no_unlock = await client.patch(
        f"{PATH}/{event_id}",
        json={**body, "confirm_reopen": True},
        headers=version_headers(client, 5),
    )
    assert no_unlock.status_code == 409
    assert no_unlock.json()["code"] == "DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED"
    assert database_snapshot(engine) == before
    headers = version_headers(client, 5, key="synthetic-event-reopen-locked")
    reopened = await client.patch(
        f"{PATH}/{event_id}",
        json={**body, "confirm_reopen": True, "confirm_unlock": True},
        headers=headers,
    )
    assert reopened.status_code == 200 and reopened.json()["version"] == 6
    assert reopened.json()["workflow_status"] == "PROVISIONAL"
    assert reopened.json()["manual_locked"] is False
    before = database_snapshot(engine)
    replay = await client.patch(
        f"{PATH}/{event_id}",
        json={**body, "confirm_reopen": True, "confirm_unlock": True},
        headers=headers,
    )
    assert replay.status_code == 200 and replay.json() == reopened.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("basis", ["UNKNOWN", "SPLIT_ADJUSTED"])
async def test_unresolved_manual_basis_remains_incomplete_and_opens_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], basis: str
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        PATH, json=payload(book, adjustment_basis=basis), headers=write_headers(client)
    )
    assert result.status_code == 201
    assert result.json()["workflow_status"] == "INCOMPLETE"
    assert result.json()["adjustment_basis"] == basis
    assert result.json()["document_verification"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    only_event_writes(before, after)
    assert len(after["review_items"]) == len(before["review_items"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3


async def test_competing_revisions_and_identical_key_retries_are_serialized(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event_id = str(book["event"].event_id)
    body = revision(book)
    headers = version_headers(client, 1, key="synthetic-event-concurrent-same-intent")
    first, replay = await asyncio.gather(
        *(client.patch(f"{PATH}/{event_id}", json=body, headers=headers) for _ in range(2))
    )
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    before = database_snapshot(engine)
    changed = await client.patch(f"{PATH}/{event_id}", json={**body, "dps": "1"}, headers=headers)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before
    current = first.json()
    next_body = payload(book, revision_id=current["current_revision_id"])
    results = await asyncio.gather(
        *(
            client.patch(f"{PATH}/{event_id}", json=next_body, headers=version_headers(client, 2))
            for _ in range(2)
        )
    )
    assert sorted(result.status_code for result in results) == [200, 412]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_event_revisions WHERE event_id=?", (event_id,)
            ).scalar_one()
            == 3
        )


@pytest.mark.parametrize("with_receipt", [False, True])
async def test_existing_entitlement_and_receipt_fence_revision_without_rewriting_fx(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], with_receipt: bool
) -> None:
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(book["event"].revision_id), "currency": "USD"}, book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        if with_receipt:
            add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{book['event'].event_id}", json=revision(book), headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_existing_projection_version_fences_publication(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(projections, "OWNER", UUID(book["owner"]["id"]))
    with unit_of_work(engine, write=True) as con:
        values = projections.snapshot_values(
            con,
            {"id": str(book["event"].event_id)},
            {"id": str(book["event"].revision_id), "currency": "USD"},
            book["account"]["id"],
        )
        insert(con, "dividend_projection_versions", values)
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{book['event'].event_id}", json=revision(book), headers=version_headers(client, 1)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["binding-audit", "serialization", "invalidation"])
async def test_failure_rolls_back_every_effect_and_same_key_can_retry(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "invalidation":
        await create(
            client,
            "trades",
            {
                "portfolio_id": book["portfolio"]["id"],
                "account_id": book["account"]["id"],
                "security_id": str(book["event"].terms.security_id),
                "side": "BUY",
                "trade_date": "2026-01-02",
                "quantity": "10",
                "price": "100",
                "currency": "HKD",
            },
        )
    headers = write_headers(client, key="synthetic-event-rollback-retry")
    before = database_snapshot(engine)
    with monkeypatch.context() as patcher:
        if failure == "binding-audit":
            original = audit

            def fail_audit(*args: Any, **kwargs: Any) -> None:
                original(*args, **kwargs)
                raise SQLAlchemyError("private-value")

            patcher.setattr(routes, "audit", fail_audit)
        elif failure == "serialization":

            def fail_public(*args: Any, **kwargs: Any) -> Any:
                raise ValueError("private-value")

            patcher.setattr(routes, "_current", fail_public)
        else:
            original_invalidate = invalidate_portfolio

            def fail_invalidation(*args: Any, **kwargs: Any) -> None:
                original_invalidate(*args, **kwargs)
                raise SQLAlchemyError("private-value")

            patcher.setattr(writer, "invalidate_portfolio", fail_invalidation)
        failed = await client.post(PATH, json=payload(book), headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before
    retry = await client.post(PATH, json=payload(book), headers=headers)
    assert retry.status_code == 201, retry.text
    if failure == "invalidation":
        after = database_snapshot(engine)
        assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
        assert after["cash_ledger_entries"] == before["cash_ledger_entries"]


async def test_executable_contract_requires_write_security_and_physical_revision(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    schema = (await client.get("/openapi.json")).json()
    for path, method in ((PATH, "post"), (PATH + "/{event_id}", "patch")):
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"cookieAuth": []}]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        for name in ("X-CSRF-Token", "Idempotency-Key"):
            assert headers[name]["required"]
        if method == "patch":
            assert headers["If-Match"]["required"]
    create_schema = schema["components"]["schemas"]["ManualEventCreate"]
    revision_schema = schema["components"]["schemas"]["ManualEventRevision"]
    assert {
        "security_id",
        "event_type",
        "ex_date",
        "dps",
        "currency",
        "adjustment_basis",
        "citation",
        "reason",
    } <= set(create_schema["required"])
    assert "revision_id" in revision_schema["required"]
    assert create_schema["properties"]["dps"]["type"] == "string"


@pytest.mark.parametrize(
    ("code", "status", "public_code"),
    [
        ("DIVIDEND_EVENT_NOT_FOUND", 404, "NOT_FOUND"),
        ("DIVIDEND_EVENT_SECURITY_UNAVAILABLE", 404, "NOT_FOUND"),
        ("DIVIDEND_EVENT_OWNER_UNAVAILABLE", 401, "UNAUTHORIZED"),
        ("INVALID_DIVIDEND_EVENT_SCOPE", 422, "INVALID_DIVIDEND_EVENT_SCOPE"),
        ("INVALID_DIVIDEND_EVENT_DECISION", 422, "INVALID_DIVIDEND_EVENT_DECISION"),
        ("DIVIDEND_EVENT_VERSION_CONFLICT", 412, "PRECONDITION_FAILED"),
        ("DIVIDEND_EVENT_TIME_CONFLICT", 409, "DIVIDEND_EVENT_TIME_CONFLICT"),
        ("unexpected-private-value", 503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"),
    ],
)
async def test_internal_write_failures_have_fixed_private_mappings(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
    public_code: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise events.EventError(code)

    monkeypatch.setattr(writer, "save", fail)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert response.status_code == status and response.json()["code"] == public_code
    safe(response)
    assert database_snapshot(engine) == before


async def test_maximum_event_version_cannot_be_advanced(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    with unit_of_work(engine) as con:
        original = events.get(con, UUID(book["owner"]["id"]), book["event"].event_id)
    assert original is not None
    maximum = events.ReviewedEvent(
        original.record.model_copy(update={"revision_no": 2_147_483_647}), original.assessment
    )
    monkeypatch.setattr(routes, "_owned", lambda *args, **kwargs: maximum)
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{book['event'].event_id}",
        json=revision(book),
        headers=version_headers(client, 2_147_483_647),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DIVIDEND_EVENT_VERSION_EXHAUSTED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_revision_replay_checks_physical_predecessor_after_later_changes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = str(book["event"].event_id)
    body = revision(book)
    headers = version_headers(client, 1, key="synthetic-event-prior-revision-replay")
    first = await client.patch(f"{PATH}/{event_id}", json=body, headers=headers)
    assert first.status_code == 200
    successor = await client.patch(
        f"{PATH}/{event_id}",
        json=payload(book, revision_id=first.json()["current_revision_id"], dps="2"),
        headers=version_headers(client, 2),
    )
    assert successor.status_code == 200 and successor.json()["version"] == 3
    before = database_snapshot(engine)
    replay = await client.patch(f"{PATH}/{event_id}", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["etag"] == '"2"'
    assert database_snapshot(engine) == before
    actual = events.get

    def absent_predecessor(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("version") == 1:
            return None
        return actual(*args, **kwargs)

    monkeypatch.setattr(events, "get", absent_predecessor)
    failed = await client.patch(f"{PATH}/{event_id}", json=body, headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before


async def test_cached_revision_version_cannot_be_relabelled(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event_id = str(book["event"].event_id)
    body = revision(book)
    headers = version_headers(client, 1, key="synthetic-event-version-cache")
    first = await client.patch(f"{PATH}/{event_id}", json=body, headers=headers)
    assert first.status_code == 200
    cached = first.json()
    cached["version"] = 3
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(cached), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    failed = await client.patch(f"{PATH}/{event_id}", json=body, headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before
