"""Protected unverified review decisions are private, versioned, and never a financial grant."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.trade_routes as routes
from spm_api import trade_audit, trade_service
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from spm_api.trade_codec import as_trade
from spm_api.trade_models import TradeHistoryReviewRecord
from tests.helpers import PASSWORD, create, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trade_history_review_migration import retired_draft, review_values
from tests.test_trade_history_review_service import REASON
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def path(identifier: str) -> str:
    return f"/api/v1/trades/{identifier}/history-review"


def body(
    decision: str = "ACKNOWLEDGE_UNVERIFIED",
    revision: str = "0",
    gap: str = "MISSING_CREATION_AUDIT",
) -> dict[str, str]:
    return {
        "decision": decision,
        "expected_revision": revision,
        "expected_gap": gap,
        "reason": REASON,
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "private-untrusted" not in response.text


async def test_owned_status_reports_the_actual_missing_gap_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    current = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    retired, _ = retired_draft(engine, book, has_creation_audit=True)
    before = database_snapshot(engine)
    first = await client.get(path(current))
    second = await client.get(path(retired))
    assert first.status_code == second.status_code == 200
    safe(first)
    safe(second)
    assert first.json() == {
        "trade_id": current,
        "trade_version": 1,
        "trade_status": "DRAFT_SCHEDULED",
        "review_revision": "0",
        "current_gap": "MISSING_CREATION_AUDIT",
        "state": "NOT_REVIEWED",
        "latest": None,
        "scope": "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY",
        "history_completeness": "UNVERIFIED",
    }
    assert second.json()["current_gap"] == "MISSING_RETIRED_ORIGIN"
    assert second.json()["trade_version"] == 2
    assert database_snapshot(engine) == before


async def test_release_failure_returns_private_error_and_keeps_original_retry_available(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1, key="synthetic-review-release-failure")
    before = database_snapshot(engine)
    with patch.object(
        Connection,
        "_release_savepoint_impl",
        side_effect=SQLAlchemyError("private-untrusted release failure"),
    ):
        response = await client.post(path(trade), json=body(), headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "DATABASE_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(path(trade), json=body(), headers=headers)
    assert retry.status_code == 200
    safe(retry)


async def test_acknowledge_withdraw_and_original_retry_are_atomic(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1, key="synthetic-review-acknowledgment")
    before = database_snapshot(engine)
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    safe(first)
    assert "etag" not in first.headers
    decision = first.json()
    assert decision["revision"] == "1" and decision["decision"] == "ACKNOWLEDGE_UNVERIFIED"
    assert decision["gap"] == "MISSING_CREATION_AUDIT"
    assert decision["scope"] == "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY"
    assert decision["history_completeness"] == "UNVERIFIED"
    assert all(field not in first.text for field in ("snapshot_sha256", "owner_id", "request_id"))
    after = database_snapshot(engine)
    changed = {
        "audit_events",
        "trade_history_reviews",
        "portfolios",
        "projection_invalidations",
        "idempotency_keys",
    }
    assert {key: value for key, value in before.items() if key not in changed} == {
        key: value for key, value in after.items() if key not in changed
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(engine) as con:
        audit = con.exec_driver_sql(
            "SELECT owner_id,request_id FROM audit_events "
            "WHERE entity_type='trade_history_review' AND entity_id=?",
            (decision["id"],),
        ).one()
        assert tuple(audit) == (book["owner"]["id"], first.headers["x-request-id"])
    status = await client.get(path(trade))
    assert status.status_code == 200 and status.json()["state"] == "ACKNOWLEDGED_UNVERIFIED"
    assert "etag" not in status.headers
    assert status.json()["latest"] == decision and status.json()["review_revision"] == "1"
    withdrawn = await client.post(
        path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 1)
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["revision"] == "2"
    status = await client.get(path(trade))
    assert (
        status.json()["state"] == "WITHDRAWN"
        and status.json()["current_gap"] == "MISSING_CREATION_AUDIT"
    )
    complete = database_snapshot(engine)
    retry = await client.post(path(trade), json=body(), headers=headers)
    assert retry.status_code == 200 and retry.json() == decision
    safe(retry)
    assert database_snapshot(engine) == complete
    revisions = await client.get(path(trade) + "/revisions", params={"limit": 1})
    assert revisions.status_code == 200 and revisions.json()["items"] == [withdrawn.json()]
    cursor = revisions.json()["next_cursor"]
    next_page = await client.get(path(trade) + "/revisions", params={"limit": 1, "cursor": cursor})
    assert next_page.status_code == 200 and next_page.json()["items"] == [decision]
    assert next_page.json()["next_cursor"] is None
    assert database_snapshot(engine) == complete
    assert UUID(decision["id"])


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_security_dependencies_reject_review_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "invalid-synthetic-csrf"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        headers["Idempotency-Key"] = "short"
    before = database_snapshot(engine)
    result = await client.post(path(trade), json=body(), headers=headers)
    assert result.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(result)
    if failure == "cookie":
        for suffix in ("", "/revisions"):
            read = await client.get(path(trade) + suffix)
            assert read.status_code == 401
            safe(read)
    assert database_snapshot(engine) == before


async def test_current_password_is_required_for_cached_review_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1, key="synthetic-review-reauth-intent")
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path(trade), json=body(), headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    safe(denied)
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    after_confirmation = database_snapshot(engine)
    replay = await client.post(path(trade), json=body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == after_confirmation


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_writer_rechecks_current_security_before_new_or_cached_review(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    if replay:
        assert (await client.post(path(trade), json=body(), headers=headers)).status_code == 200
    checkpoint: dict[str, Any] = {}

    def changed_after_dependency(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            elif failure == "csrf":
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
            else:
                con.exec_driver_sql(
                    "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'"
                )
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed_after_dependency)
    result = await client.post(path(trade), json=body(), headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["before"]


@pytest.mark.parametrize(
    "header", [None, "1", '"0"', '"01"', '"9223372036854775808"', '"' + "9" * 4500 + '"']
)
async def test_physical_if_match_is_bounded_before_integer_conversion(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], header: str | None
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    result = await client.post(path(trade), json=body(), headers=headers)
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "revision",
    [0, 1.0, True, None, "", "01", "-1", "1.0", "1e0", "1\n", "9223372036854775807", "9" * 4500],
)
async def test_review_revision_requires_bounded_canonical_decimal_string(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], revision: object
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    before = database_snapshot(engine)
    result = await client.post(
        path(trade),
        json={**body(), "expected_revision": revision},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("revision", ["9007199254740993", "9223372036854775806"])
async def test_exact_large_review_revision_is_fenced_without_number_rounding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], revision: str
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    before = database_snapshot(engine)
    result = await client.post(
        path(trade), json=body(revision=revision), headers=version_headers(client, 1)
    )
    assert result.status_code == 412
    assert database_snapshot(engine) == before
    cursor = base64.urlsafe_b64encode(json.dumps([revision, str(uuid4())]).encode()).decode()
    history = await client.get(path(trade) + "/revisions", params={"cursor": cursor})
    assert history.status_code == 200 and history.json()["items"] == []
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["reason", "decision", "gap", "missing-gap", "extra"])
async def test_invalid_review_input_is_not_echoed_or_written(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    data = body()
    if failure == "reason":
        data["reason"] = "private-untrusted\0reason"
    elif failure == "decision":
        data["decision"] = "VERIFIED"
    elif failure == "gap":
        data["expected_gap"] = "VERIFIED"
    elif failure == "missing-gap":
        del data["expected_gap"]
    else:
        data["private-untrusted"] = "private-untrusted"
    before = database_snapshot(engine)
    result = await client.post(path(trade), json=data, headers=version_headers(client, 1))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_read_or_write_a_trade_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    foreign = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (foreign,),
        )
        token, csrf, _ = issue_session(con, foreign, "synthetic-other-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    for suffix in ("", "/revisions"):
        result = await client.get(path(trade) + suffix)
        assert result.status_code == 404
        safe(result)
    result = await client.post(path(trade), json=body(), headers=version_headers(client, 1))
    assert result.status_code == 404
    safe(result)
    assert database_snapshot(engine) == before


async def test_same_key_replays_and_competing_keys_fence_review_revision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1, key="synthetic-concurrent-review-intent")
    first, replay = await asyncio.gather(
        *(client.post(path(trade), json=body(), headers=headers) for _ in range(2))
    )
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    before = database_snapshot(engine)
    for changed, changed_headers in (
        ({**body(), "reason": "A different synthetic review reason"}, headers),
        (body(), {**headers, "If-Match": '"2"'}),
        (body("WITHDRAW", "1"), headers),
    ):
        result = await client.post(path(trade), json=changed, headers=changed_headers)
        assert result.status_code == 409 and result.json()["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await client.post(path(trade), json=body(), headers=version_headers(client, 1))
    assert stale.status_code == 412
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_history_reviews").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations "
                "WHERE reason='TRADE_HISTORY_REVIEW_CHANGED'"
            ).scalar_one()
            == 1
        )


async def test_simultaneous_distinct_review_decisions_have_one_winner(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    results = await asyncio.gather(
        *(
            client.post(path(trade), json=body(), headers=version_headers(client, 1))
            for _ in range(2)
        )
    )
    assert sorted(result.status_code for result in results) == [200, 412]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_history_reviews").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='trade_history_review'"
            ).scalar_one()
            == 1
        )


async def test_review_does_not_unblock_trade_patch_or_delete(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    assert (
        await client.post(path(trade), json=body(), headers=version_headers(client, 1))
    ).status_code == 200
    before = database_snapshot(engine)
    revision = payload(book, trade_date="2999-01-02", fx_rate_hkd=None, reason=REASON)
    for method, request_body in (("PATCH", revision), ("DELETE", {"reason": REASON})):
        result = await client.request(
            method, f"/api/v1/trades/{trade}", json=request_body, headers=version_headers(client, 1)
        )
        assert result.status_code == 409 and result.json()["code"] == "NEEDS_REVIEW"
        safe(result)
    assert database_snapshot(engine) == before


async def test_audit_origin_makes_a_new_unverified_review_unnecessary(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = (await create(client, "trades", payload(book)))["id"]
    before = database_snapshot(engine)
    status = await client.get(path(trade))
    assert status.status_code == 200 and status.json()["state"] == "NOT_REQUIRED"
    assert status.json()["current_gap"] is None and status.json()["latest"] is None
    result = await client.post(path(trade), json=body(), headers=version_headers(client, 1))
    assert result.status_code == 409 and result.json()["code"] == "TRADE_REVIEW_NOT_REQUIRED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "tamper", ["trade", "id", "revision", "gap", "reason", "extra", "intent", "intent-gap"]
)
async def test_cached_review_is_revalidated_against_audited_history_and_original_intent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], tamper: str
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    altered, requested = first.json(), body()
    if tamper == "trade":
        altered["trade_id"] = str(uuid4())
    elif tamper == "id":
        altered["id"] = str(uuid4())
    elif tamper == "revision":
        altered["revision"] = "2"
    elif tamper == "gap":
        altered["gap"] = "MISSING_RETIRED_ORIGIN"
    elif tamper == "reason":
        altered["reason"] = "Different synthetic cached reason"
    elif tamper == "extra":
        altered["private-untrusted"] = "private-untrusted"
    elif tamper == "intent-gap":
        requested = {**requested, "expected_gap": "MISSING_RETIRED_ORIGIN"}
    else:
        requested = {**requested, "reason": "Different synthetic requested reason"}
    with unit_of_work(engine, write=True) as con:
        if tamper in ("intent", "intent-gap"):
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
                (digest(canonical({"version": 1, **requested})), headers["Idempotency-Key"]),
            )
        else:
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                (json.dumps(altered), headers["Idempotency-Key"]),
            )
    before = database_snapshot(engine)
    result = await client.post(path(trade), json=requested, headers=headers)
    assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_duplicate_review_audit_blocks_read_replay_and_new_decisions(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "trade_history_review",
            first.json()["id"],
            "TRADE_HISTORY_UNVERIFIED_ACKNOWLEDGED",
            request_id=str(uuid4()),
            after={"synthetic": "unrelated duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    for suffix in ("", "/revisions"):
        result = await client.get(path(trade) + suffix)
        assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
        safe(result)
    for data, request_headers in (
        (body(), headers),
        (body("WITHDRAW", "1"), version_headers(client, 1)),
    ):
        result = await client.post(path(trade), json=data, headers=request_headers)
        assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
        safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("malformed", ["hash", "reason"])
async def test_malformed_creation_audit_is_not_reported_as_a_reviewable_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], malformed: Any
) -> None:
    trade, _ = retired_draft(engine, book, has_creation_audit=True, malformed=malformed)
    before = database_snapshot(engine)
    for result in (
        await client.get(path(trade)),
        await client.get(path(trade) + "/revisions"),
        await client.post(
            path(trade), json=body(gap="MISSING_RETIRED_ORIGIN"), headers=version_headers(client, 2)
        ),
    ):
        assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
        safe(result)
    assert database_snapshot(engine) == before


async def test_missing_successor_cannot_be_acknowledged_as_an_evidence_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade,)).mappings().one()
        trade_service._retire(con, row, "SUPERSEDED")
    before = database_snapshot(engine)
    for result in (
        await client.get(path(trade)),
        await client.post(path(trade), json=body(), headers=version_headers(client, 2)),
    ):
        assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
        safe(result)
    assert database_snapshot(engine) == before


async def test_well_shaped_but_false_sql_review_never_becomes_an_http_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        values = review_values(con, book["owner"]["id"], trade)
        values["snapshot_sha256"] = "0" * 64
        insert(con, "trade_history_reviews", values)
    before = database_snapshot(engine)
    for suffix in ("", "/revisions"):
        result = await client.get(path(trade) + suffix)
        assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
    result = await client.post(
        path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 1)
    )
    assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_later_conflicting_audit_blocks_review_read_and_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "trade",
            trade,
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "unrelated later audit"},
        )
    before = database_snapshot(engine)
    for result in (
        await client.get(path(trade)),
        await client.get(path(trade) + "/revisions"),
        await client.post(path(trade), json=body(), headers=headers),
    ):
        assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
        safe(result)
    assert database_snapshot(engine) == before


async def test_historical_review_replay_survives_a_matching_retirement(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1, key="synthetic-retired-history-review")
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade,)).mappings().one()
        trade_service._retire(con, row, "SUPERSEDED")
        successor_id, at = str(uuid4()), stamp()
        insert(
            con,
            "trades",
            {
                **dict(row),
                "id": successor_id,
                "revision": 2,
                "supersedes_id": trade,
                "created_at": at,
                "updated_at": at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (successor_id,)).mappings().one()
        )
        audit(
            con,
            book["owner"]["id"],
            "trade",
            successor_id,
            "REVISE",
            request_id=str(uuid4()),
            before=dict(row),
            after=as_trade(successor).model_dump(mode="json"),
            reason=REASON,
        )
    before = database_snapshot(engine)
    status = await client.get(path(trade))
    assert status.status_code == 200 and status.json()["trade_version"] == 2
    assert status.json()["state"] == "ACKNOWLEDGED_UNVERIFIED"
    replay = await client.post(path(trade), json=body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    stale = await client.post(
        path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 1)
    )
    assert stale.status_code == 412 and database_snapshot(engine) == before
    withdrawn = await client.post(
        path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 2)
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["observed_trade_version"] == 2


async def test_acknowledgment_cannot_silently_change_the_observed_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade, original = retired_draft(engine, book, has_creation_audit=False)
    initial = await client.get(path(trade))
    assert initial.status_code == 200 and initial.json()["current_gap"] == "MISSING_CREATION_AUDIT"
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "trade",
            trade,
            "CREATE",
            request_id=str(uuid4()),
            after=original.model_dump(mode="json"),
        )
    before = database_snapshot(engine)
    changed = await client.get(path(trade))
    assert changed.status_code == 200 and changed.json()["current_gap"] == "MISSING_RETIRED_ORIGIN"
    result = await client.post(
        path(trade),
        json={**body(), "expected_gap": "MISSING_CREATION_AUDIT"},
        headers=version_headers(client, 2),
    )
    assert result.status_code == 409 and result.json()["code"] == "TRADE_REVIEW_CHANGED"
    assert database_snapshot(engine) == before
    accepted = await client.post(
        path(trade), json=body(gap="MISSING_RETIRED_ORIGIN"), headers=version_headers(client, 2)
    )
    assert accepted.status_code == 200 and accepted.json()["gap"] == "MISSING_RETIRED_ORIGIN"


async def test_retained_origin_changes_review_state_but_preserves_original_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade, original = retired_draft(engine, book, has_creation_audit=True)
    headers = version_headers(client, 2, key="synthetic-retired-review-intent")
    first = await client.post(path(trade), json=body(gap="MISSING_RETIRED_ORIGIN"), headers=headers)
    assert first.status_code == 200 and first.json()["gap"] == "MISSING_RETIRED_ORIGIN"
    with unit_of_work(engine, write=True) as con:
        trade_audit.recover_from_response(
            con, UUID(book["owner"]["id"]), UUID(trade), original, uuid4(), REASON
        )
    status = await client.get(path(trade))
    assert status.status_code == 200 and status.json()["state"] == "EVIDENCE_CHANGED"
    assert status.json()["current_gap"] is None and status.json()["latest"] == first.json()
    before = database_snapshot(engine)
    wrong_gap = await client.post(
        path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 2)
    )
    assert wrong_gap.status_code == 409 and wrong_gap.json()["code"] == "TRADE_REVIEW_CHANGED"
    assert database_snapshot(engine) == before
    withdrawn = await client.post(
        path(trade),
        json=body("WITHDRAW", "1", "MISSING_RETIRED_ORIGIN"),
        headers=version_headers(client, 2),
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["gap"] == "MISSING_RETIRED_ORIGIN"
    before = database_snapshot(engine)
    replay = await client.post(
        path(trade), json=body(gap="MISSING_RETIRED_ORIGIN"), headers=headers
    )
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == before


async def test_later_creation_audit_changes_status_without_authenticating_its_timing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    first = await client.post(path(trade), json=body(), headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade,)).mappings().one()
        audit(
            con,
            book["owner"]["id"],
            "trade",
            trade,
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json"),
        )
    before = database_snapshot(engine)
    status = await client.get(path(trade))
    assert status.status_code == 200 and status.json()["state"] == "EVIDENCE_CHANGED"
    assert status.json()["current_gap"] is None and status.json()["latest"] == first.json()
    assert status.json()["history_completeness"] == "UNVERIFIED"
    replay = await client.post(path(trade), json=body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize("withdrawal", ["none", "before", "after"])
async def test_later_matching_creation_audit_never_grants_a_reviewed_trade_change(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    method: str,
    withdrawal: str,
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1, key="synthetic-original-review")
    acknowledged = await client.post(path(trade), json=body(), headers=headers)
    assert acknowledged.status_code == 200
    if withdrawal == "before":
        result = await client.post(
            path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 1)
        )
        assert result.status_code == 200
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (trade,)).mappings().one()
        audit(
            con,
            book["owner"]["id"],
            "trade",
            trade,
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json"),
        )
    if withdrawal == "after":
        result = await client.post(
            path(trade), json=body("WITHDRAW", "1"), headers=version_headers(client, 1)
        )
        assert result.status_code == 200
    before = database_snapshot(engine)
    change = (
        payload(
            book,
            trade_date="2999-01-02",
            fx_rate_hkd=None,
            quantity="11",
            reason="Correct this synthetic reviewed trade",
        )
        if method == "PATCH"
        else {"reason": "Void this synthetic reviewed trade"}
    )
    response = await client.request(
        method, f"/api/v1/trades/{trade}", json=change, headers=version_headers(client, 1)
    )
    assert response.status_code == 409 and response.json()["code"] == "NEEDS_REVIEW"
    safe(response)
    assert trade not in response.text
    assert database_snapshot(engine) == before
    replay = await client.post(path(trade), json=body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == acknowledged.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize("corrupt_successor_review", [False, True])
async def test_reviewed_missing_creation_also_fences_an_existing_successor(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    method: str,
    corrupt_successor_review: bool,
) -> None:
    original_id = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    acknowledged = await client.post(
        path(original_id), json=body(), headers=version_headers(client, 1)
    )
    assert acknowledged.status_code == 200
    successor_id = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        original = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (original_id,)).mappings().one()
        )
        audit(
            con,
            book["owner"]["id"],
            "trade",
            original_id,
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(original).model_dump(mode="json"),
        )
        trade_service._retire(con, original, "SUPERSEDED")
        at = stamp()
        insert(
            con,
            "trades",
            {
                **dict(original),
                "id": successor_id,
                "revision": 2,
                "supersedes_id": original_id,
                "created_at": at,
                "updated_at": at,
            },
        )
        successor = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (successor_id,)).mappings().one()
        )
        if corrupt_successor_review:
            fake = review_values(con, book["owner"]["id"], successor_id)
            insert(con, "trade_history_reviews", fake)
        request_id = str(uuid4())
        audit(
            con,
            book["owner"]["id"],
            "trade",
            successor_id,
            "REVISE",
            request_id=request_id,
            before=dict(original),
            after=as_trade(successor).model_dump(mode="json"),
            reason=REASON,
        )
        trade_audit.remember_created(
            con, UUID(book["owner"]["id"]), UUID(successor_id), UUID(request_id)
        )
    before = database_snapshot(engine)
    change = (
        payload(
            book,
            trade_date="2999-01-02",
            fx_rate_hkd=None,
            quantity="11",
            reason="Correct this synthetic reviewed root",
        )
        if method == "PATCH"
        else {"reason": "Void this synthetic reviewed root"}
    )
    response = await client.request(
        method, f"/api/v1/trades/{successor_id}", json=change, headers=version_headers(client, 1)
    )
    expected = (
        (503, "TRADE_AUDIT_UNAVAILABLE") if corrupt_successor_review else (409, "NEEDS_REVIEW")
    )
    assert (response.status_code, response.json()["code"]) == expected
    safe(response)
    assert database_snapshot(engine) == before
    assert (await client.get(path(original_id))).status_code == 200
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_invalid_review_history_fails_closed_before_a_trade_change(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], method: str
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    with unit_of_work(engine, write=True) as con:
        fake = review_values(con, book["owner"]["id"], trade)
        insert(con, "trade_history_reviews", fake)
    before = database_snapshot(engine)
    change = (
        payload(
            book,
            trade_date="2999-01-02",
            fx_rate_hkd=None,
            reason="Correct this synthetic reviewed trade",
        )
        if method == "PATCH"
        else {"reason": "Void this synthetic reviewed trade"}
    )
    response = await client.request(
        method, f"/api/v1/trades/{trade}", json=change, headers=version_headers(client, 1)
    )
    assert response.status_code == 503 and response.json()["code"] == "TRADE_AUDIT_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_reviewed_gap_in_an_unrelated_root_does_not_block_a_trade_change(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], method: str
) -> None:
    reviewed = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    acknowledged = await client.post(
        path(reviewed), json=body(), headers=version_headers(client, 1)
    )
    assert acknowledged.status_code == 200
    trade_body = payload(book, trade_date="2999-01-02", fx_rate_hkd=None)
    target = await create(client, "trades", trade_body)
    reviews = database_snapshot(engine)["trade_history_reviews"]
    change = (
        {**trade_body, "quantity": "11", "reason": "Correct this unrelated synthetic trade"}
        if method == "PATCH"
        else {"reason": "Void this unrelated synthetic trade"}
    )
    response = await client.request(
        method, f"/api/v1/trades/{target['id']}", json=change, headers=version_headers(client, 1)
    )
    assert response.status_code == 200
    assert database_snapshot(engine)["trade_history_reviews"] == reviews
    assert (await client.get(path(reviewed))).json()["state"] == "ACKNOWLEDGED_UNVERIFIED"


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_retired_origin_gap_alone_does_not_claim_or_block_a_current_trade(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], method: str
) -> None:
    retired, _ = retired_draft(engine, book, has_creation_audit=True)
    acknowledged = await client.post(
        path(retired),
        json=body(gap="MISSING_RETIRED_ORIGIN"),
        headers=version_headers(client, 2),
    )
    assert acknowledged.status_code == 200
    with unit_of_work(engine) as con:
        successor = con.exec_driver_sql(
            "SELECT id FROM trades WHERE supersedes_id=?", (retired,)
        ).scalar_one()
    change = (
        payload(
            book,
            trade_date="2999-01-02",
            fx_rate_hkd=None,
            quantity="12",
            reason="Correct this synthetic current trade",
        )
        if method == "PATCH"
        else {"reason": "Void this synthetic current trade"}
    )
    response = await client.request(
        method, f"/api/v1/trades/{successor}", json=change, headers=version_headers(client, 1)
    )
    assert response.status_code == 200
    status = await client.get(path(retired))
    assert status.status_code == 200 and status.json()["history_completeness"] == "UNVERIFIED"
    assert status.json()["current_gap"] == "MISSING_RETIRED_ORIGIN"


async def test_review_history_paginates_the_real_immutable_revision_order(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    identifiers = []
    for version in range(27):
        decision = "ACKNOWLEDGE_UNVERIFIED" if version % 2 == 0 else "WITHDRAW"
        result = await client.post(
            path(trade), json=body(decision, str(version)), headers=version_headers(client, 1)
        )
        assert result.status_code == 200
        identifiers.append(result.json()["id"])
    before = database_snapshot(engine)
    first = await client.get(path(trade) + "/revisions")
    assert first.status_code == 200 and len(first.json()["items"]) == 25
    second = await client.get(
        path(trade) + "/revisions", params={"cursor": first.json()["next_cursor"]}
    )
    assert second.status_code == 200 and len(second.json()["items"]) == 2
    actual = [record["id"] for record in first.json()["items"] + second.json()["items"]]
    assert actual == list(reversed(identifiers)) and second.json()["next_cursor"] is None
    filtered = await client.get(path(trade) + "/revisions", params={"q": "%"})
    assert filtered.status_code == 200 and filtered.json()["items"] == []
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["0", "-1", "01", "1.0", "9" * 4500, "9223372036854775808"])
async def test_invalid_history_cursor_is_rejected_before_sql_binding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], value: str
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    result = await client.get(path(trade) + "/revisions", params={"cursor": cursor})
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


async def test_review_never_reads_provider_files_or_constructs_a_provider_client(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)
    headers = version_headers(client, 1)
    with (
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider I/O")),
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private I/O")),
    ):
        first = await client.post(path(trade), json=body(), headers=headers)
        assert first.status_code == 200
        before = database_snapshot(engine)
        assert (await client.post(path(trade), json=body(), headers=headers)).json() == first.json()
        assert (await client.get(path(trade))).status_code == 200
        assert (await client.get(path(trade) + "/revisions")).status_code == 200
        assert database_snapshot(engine) == before


async def test_late_review_invalidation_failure_rolls_back_audit_and_idempotency(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)

    def fail(*args: Any) -> None:
        raise ValueError("synthetic late invalidation failure")

    monkeypatch.setattr(trade_audit, "_invalidate_root", fail)
    before = database_snapshot(engine)
    result = await client.post(path(trade), json=body(), headers=version_headers(client, 1))
    assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_response_serialization_failure_rolls_back_the_whole_review(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    trade = synthetic_legacy_draft(engine, book, unrelated_audit=False)

    def fail(*args: Any, **kwargs: Any) -> str:
        raise ValueError("private-untrusted serialization failure")

    monkeypatch.setattr(TradeHistoryReviewRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    result = await client.post(path(trade), json=body(), headers=version_headers(client, 1))
    assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before
