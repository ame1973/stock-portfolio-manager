"""Synthetic HTTP proofs for explicit owner-confirmed dividend withdrawal."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_models as models
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
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import PASSWORD, create, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_read_api import append
from tests.test_dividend_event_write_api import only_event_writes, payload, safe
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/events"
REASON = "Synthetic owner-confirmed withdrawal, not independent proof."


def withdrawal_body(book: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "revision_id": str(book["event"].revision_id),
        "confirmation": "WITHDRAW_EVENT",
        "citation": "Synthetic owner withdrawal reference",
        "source_url": "https://example.test/withdrawal?private-value=synthetic#fragment",
        "reason": REASON,
        **changes,
    }


def safe_withdrawal(response: httpx.Response) -> None:
    safe(response)
    if response.is_error:
        assert REASON not in response.text


async def test_withdrawal_requires_an_explicit_owner_decision(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    body = {
        "revision_id": str(event.revision_id),
        "confirmation": "WITHDRAW_EVENT",
        "citation": "Synthetic owner withdrawal reference",
        "source_url": None,
        "reason": "Synthetic owner-confirmed withdrawal, not independent proof.",
    }
    before = database_snapshot(engine)
    result = await client.post(
        f"/api/v1/dividends/events/{event.event_id}/withdraw",
        json=body,
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200, result.text
    assert result.json()["workflow_status"] == "WITHDRAWN"
    assert result.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    for table in ("dividend_entitlements", "dividend_receipts", "cash_ledger_entries"):
        assert after[table] == before[table]


async def test_withdrawal_preserves_terms_lock_and_audited_owner_reference(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = book["event"]
    verified = append(engine, book, kind="VERIFY", number=2, event_id=event.event_id)
    body = withdrawal_body(book, revision_id=str(verified.revision_id))
    headers = version_headers(client, 2, key="synthetic-withdraw-locked-event")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Owner withdrawal must not access providers")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    result = await client.post(f"{PATH}/{event.event_id}/withdraw", json=body, headers=headers)
    assert result.status_code == 200, result.text
    safe_withdrawal(result)
    item = result.json()
    assert result.headers["etag"] == '"3"' and item["version"] == 3
    assert item["id"] == str(event.event_id) and item["current_revision_id"] != str(
        verified.revision_id
    )
    assert item["workflow_status"] == "WITHDRAWN" and item["manual_locked"] is True
    assert item["decision_kind"] == "WITHDRAW" and item["dps"] == "0.90000000"
    assert item["reference"] == {"citation": body["citation"], "url": None, "url_withheld": True}
    assert item["document_verification"] == item["eligibility_scope"] == "NOT_ESTABLISHED"
    assert (
        item["scope"] == "OWNER_EVENT_DECISION_ONLY"
        and item["current_use_scope"] == "REVALIDATION_REQUIRED"
    )
    after = database_snapshot(engine)
    only_event_writes(before, after)
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
    replay = await client.post(f"{PATH}/{event.event_id}/withdraw", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == item
    assert database_snapshot(engine) == after


async def test_withdrawn_event_can_only_reopen_through_explicit_manual_confirmation(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-withdraw-historical-replay")
    body = withdrawal_body(book)
    first = await client.post(f"{PATH}/{event.event_id}/withdraw", json=body, headers=headers)
    assert first.status_code == 200
    removed = first.json()
    before = database_snapshot(engine)
    repeated = await client.post(
        f"{PATH}/{event.event_id}/withdraw",
        json=withdrawal_body(book, revision_id=removed["current_revision_id"]),
        headers=version_headers(client, 2),
    )
    assert repeated.status_code == 409 and repeated.json()["code"] == "DIVIDEND_EVENT_WITHDRAWN"
    no_confirm = await client.patch(
        f"{PATH}/{event.event_id}",
        json=payload(book, revision_id=removed["current_revision_id"]),
        headers=version_headers(client, 2),
    )
    assert no_confirm.status_code == 409
    assert no_confirm.json()["code"] == "DIVIDEND_EVENT_REOPEN_CONFIRMATION_REQUIRED"
    assert database_snapshot(engine) == before
    reopened = await client.patch(
        f"{PATH}/{event.event_id}",
        json=payload(book, revision_id=removed["current_revision_id"], confirm_reopen=True),
        headers=version_headers(client, 2),
    )
    assert reopened.status_code == 200 and reopened.json()["workflow_status"] == "PROVISIONAL"
    before = database_snapshot(engine)
    replay = await client.post(f"{PATH}/{event.event_id}/withdraw", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == removed
    assert replay.headers["etag"] == '"2"'
    assert (await client.get(f"{PATH}/{event.event_id}")).json() == reopened.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_id", "not-a-uuid"),
        ("confirmation", "REJECT_REVISION"),
        ("confirmation", True),
        ("citation", ""),
        ("source_url", "https://user:password@example.test/private-value"),
        ("reason", ""),
        ("reason", "bad\u0000reason"),
        ("terms", {"dps": "0"}),
    ],
)
async def test_invalid_withdrawal_is_not_disclosed_or_written(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str, value: Any
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw",
        json=withdrawal_body(book, **{field: value}),
        headers=version_headers(client, 1),
    )
    assert result.status_code == 422
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["revision_id", "confirmation", "citation", "reason"])
async def test_withdrawal_requires_explicit_identity_confirmation_and_reference(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str
) -> None:
    body = withdrawal_body(book)
    del body[field]
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw", json=body, headers=version_headers(client, 1)
    )
    assert result.status_code == 422
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


async def test_observed_physical_revision_and_event_version_are_fenced(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    before = database_snapshot(engine)
    requests = [
        (withdrawal_body(book), version_headers(client, 2), 412),
        (withdrawal_body(book, revision_id=str(uuid4())), version_headers(client, 1), 412),
        (withdrawal_body(book), write_headers(client), 422),
        (withdrawal_body(book), {**write_headers(client), "If-Match": '"2147483648"'}, 422),
    ]
    for body, headers, status in requests:
        result = await client.post(path, json=body, headers=headers)
        assert result.status_code == status
        safe_withdrawal(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key", "reauth"])
async def test_withdrawal_requires_current_security_dependencies(
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
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw", json=withdrawal_body(book), headers=headers
    )
    assert (
        result.status_code
        == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422, "reauth": 403}[failure]
    )
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_withdraw_another_event(
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
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw",
        json=withdrawal_body(book),
        headers=version_headers(client, 1),
    )
    assert result.status_code == 404 and result.json()["code"] == "NOT_FOUND"
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_withdrawal_rechecks_authentication_inside_writer(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-writer-security")
    if replay:
        assert (await client.post(path, json=body, headers=headers)).status_code == 200
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
    result = await client.post(path, json=body, headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe_withdrawal(result)
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize("competing_change", ["event", "receipt"])
async def test_withdrawal_rechecks_event_and_receipt_fence_inside_writer(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    competing_change: str,
) -> None:
    checkpoint: dict[str, Any] = {}

    def changed(*args: Any, **kwargs: Any) -> Any:
        if competing_change == "event":
            append(engine, book, kind="VERIFY", number=2, event_id=book["event"].event_id)
        else:
            with unit_of_work(engine, write=True) as con:
                claim = entitlement(
                    {"id": str(book["event"].revision_id), "currency": "USD"},
                    book["account"]["id"],
                )
                claim["created_at"] = stamp()
                insert(con, "dividend_entitlements", claim)
                add_posted_receipt(con, claim, fx=7_800_000_000_000)
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed)
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw",
        json=withdrawal_body(book),
        headers=version_headers(client, 1),
    )
    if competing_change == "event":
        assert result.status_code == 412
    else:
        assert result.status_code == 409
        assert result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    safe_withdrawal(result)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_withdrawal_replay_requires_fresh_reauthentication(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-reauth")
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path, json=body, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == before


async def test_competing_withdrawals_and_idempotent_retries_serialize(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-concurrent")
    first, retry = await asyncio.gather(
        *(client.post(path, json=body, headers=headers) for _ in range(2))
    )
    assert first.status_code == retry.status_code == 200 and first.json() == retry.json()
    before = database_snapshot(engine)
    conflict = await client.post(
        path, json={**body, "reason": "A different synthetic withdrawal reason."}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before
    reopened = await client.patch(
        f"{PATH}/{book['event'].event_id}",
        json=payload(book, revision_id=first.json()["current_revision_id"], confirm_reopen=True),
        headers=version_headers(client, 2),
    )
    assert reopened.status_code == 200
    next_body = withdrawal_body(book, revision_id=reopened.json()["current_revision_id"])
    results = await asyncio.gather(
        *(client.post(path, json=next_body, headers=version_headers(client, 3)) for _ in range(2))
    )
    assert sorted(item.status_code for item in results) == [200, 412]


@pytest.mark.parametrize("with_receipt", [False, True])
async def test_withdrawal_fences_existing_entitlement_or_receipt_and_preserves_fx(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, with_receipt: bool
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
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw",
        json=withdrawal_body(book),
        headers=version_headers(client, 1),
    )
    assert (
        result.status_code == 409 and result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


async def test_withdrawal_fences_existing_projection_history(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
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
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw",
        json=withdrawal_body(book),
        headers=version_headers(client, 1),
    )
    assert (
        result.status_code == 409 and result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
    )
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["binding-audit", "serialization", "invalidation"])
async def test_failed_withdrawal_rolls_back_every_effect_and_same_key_can_retry(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
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
    headers = version_headers(client, 1, key="synthetic-withdraw-rollback")
    path = f"{PATH}/{book['event'].event_id}/withdraw"
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
        failed = await client.post(path, json=withdrawal_body(book), headers=headers)
    assert failed.status_code == 503 and "synthetic-private-value" not in failed.text
    safe_withdrawal(failed)
    assert database_snapshot(engine) == before
    retry = await client.post(path, json=withdrawal_body(book), headers=headers)
    assert retry.status_code == 200, retry.text


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("citation", "A different synthetic withdrawal reference"),
        ("source_url", "https://example.test/withdrawal?different=secret"),
        ("reason", "A different synthetic withdrawal reason."),
    ],
)
async def test_original_withdrawal_intent_is_verified_on_replay(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    field: str,
    changed_value: str,
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-private-replay")
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200 and first.json()["reference"]["url"] is None
    changed = {**body, field: changed_value}
    changed_payload = {
        "event_version": 1,
        **models.DividendWithdrawal.model_validate(changed).model_dump(mode="json"),
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
            (digest(canonical(changed_payload)), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(path, json=changed, headers=headers)
    assert replay.status_code == 503
    safe_withdrawal(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "corruption", ["label", "reference", "version", "event-id", "binding", "event-audit"]
)
async def test_corrupt_cached_withdrawal_or_late_audit_fails_closed(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, corruption: str
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-corrupt-replay")
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    if corruption in {"label", "reference", "version", "event-id"}:
        altered = first.json()
        if corruption == "label":
            del altered["eligibility_scope"]
        elif corruption == "reference":
            altered["reference"]["url_withheld"] = False
        elif corruption == "event-id":
            altered["id"] = str(uuid4())
        else:
            altered["version"] = 3
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                (json.dumps(altered), headers["Idempotency-Key"]),
            )
    else:
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                book["owner"]["id"],
                "dividend_event_revision",
                first.json()["current_revision_id"],
                "DIVIDEND_EVENT_HTTP_INTENT"
                if corruption == "binding"
                else "DIVIDEND_EVENT_REVISED",
                request_id=str(uuid4()),
                after={"synthetic": "duplicate"},
                reason="" if corruption == "binding" else REASON,
            )
    before = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 503
    safe_withdrawal(replay)
    assert database_snapshot(engine) == before


async def test_manual_revision_cannot_be_replayed_as_a_withdrawal(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    headers = version_headers(client, 1, key="synthetic-manual-as-withdrawal")
    manual = await client.patch(
        f"{PATH}/{event.event_id}",
        json=payload(book, revision_id=str(event.revision_id)),
        headers=headers,
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["decision_kind"] == "MANUAL"
    body = withdrawal_body(book)
    normalized = {
        "event_version": 1,
        **models.DividendWithdrawal.model_validate(body).model_dump(mode="json"),
    }
    path = f"{PATH}/{event.event_id}/withdraw"
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET route=?,request_sha256=? WHERE idem_key=?",
            (f"POST {path}", digest(canonical(normalized)), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 503
    safe_withdrawal(replay)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["missing", "different"])
async def test_withdrawal_replay_requires_the_original_physical_predecessor(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-predecessor")
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    before = database_snapshot(engine)
    actual = events.get

    def changed_prior(*args: Any, **kwargs: Any) -> Any:
        result = actual(*args, **kwargs)
        if kwargs.get("version") == 1:
            if failure == "missing":
                return None
            assert result is not None
            return events.ReviewedEvent(
                result.record.model_copy(update={"revision_id": uuid4()}), result.assessment
            )
        return result

    monkeypatch.setattr(events, "get", changed_prior)
    failed = await client.post(path, json=body, headers=headers)
    assert failed.status_code == 503
    safe_withdrawal(failed)
    assert database_snapshot(engine) == before


async def test_withdrawal_replay_requires_the_retained_withdrawal_revision(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = f"{PATH}/{book['event'].event_id}/withdraw"
    body = withdrawal_body(book)
    headers = version_headers(client, 1, key="synthetic-withdraw-missing-revision")
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    before = database_snapshot(engine)
    monkeypatch.setattr(events, "get", lambda *args, **kwargs: None)
    failed = await client.post(path, json=body, headers=headers)
    assert failed.status_code == 503
    safe_withdrawal(failed)
    assert database_snapshot(engine) == before


async def test_maximum_event_version_cannot_be_withdrawn(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with unit_of_work(engine) as con:
        original = events.get(con, UUID(book["owner"]["id"]), book["event"].event_id)
    assert original is not None
    maximum = events.ReviewedEvent(
        original.record.model_copy(update={"revision_no": 2_147_483_647}), original.assessment
    )
    monkeypatch.setattr(routes, "_owned", lambda *args, **kwargs: maximum)
    before = database_snapshot(engine)
    result = await client.post(
        f"{PATH}/{book['event'].event_id}/withdraw",
        json=withdrawal_body(book),
        headers=version_headers(client, 2_147_483_647),
    )
    assert result.status_code == 409 and result.json()["code"] == "DIVIDEND_EVENT_VERSION_EXHAUSTED"
    safe_withdrawal(result)
    assert database_snapshot(engine) == before


async def test_executable_contract_requires_withdrawal_confirmation_and_pins(
    book: dict[str, Any], client: httpx.AsyncClient
) -> None:
    spec = (await client.get("/openapi.json")).json()
    operation = spec["paths"][f"{PATH}/{{event_id}}/withdraw"]["post"]
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
    assert all(
        headers[name]["required"] for name in ("X-CSRF-Token", "Idempotency-Key", "If-Match")
    )
    schema = spec["components"]["schemas"]["DividendWithdrawal"]
    assert {"revision_id", "confirmation", "citation", "reason"} <= set(schema["required"])
    assert schema["properties"]["confirmation"]["const"] == "WITHDRAW_EVENT"
