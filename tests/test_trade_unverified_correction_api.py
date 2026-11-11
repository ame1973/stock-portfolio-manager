"""Protected unverified corrections preserve scope, authentication, and atomic replay."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import PASSWORD, create, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trade_correction_scope import decision, second_auditless_revision
from tests.test_trade_unverified_correction import REASON, active_auditless_buy
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio
Operation = Literal["revise", "void"]


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def path(trade: UUID | str, operation: Operation) -> str:
    return f"/api/v1/trades/{trade}/unverified-corrections/{operation}"


def pin(trade: UUID | str, version: int = 1, revision: str = "1") -> dict[str, Any]:
    return {
        "trade_id": str(trade),
        "trade_version": version,
        "review_revision": revision,
        "gap": "MISSING_CREATION_AUDIT",
    }


def body(trade: UUID | str, operation: Operation, book: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "confirmation": "UNVERIFIED_TRADE_CORRECTION_ONLY",
        "reviews": [pin(trade)],
    }
    if operation == "revise":
        data["revision"] = payload(
            book, trade_date="2999-01-02", quantity="11", fx_rate_hkd="7.9", reason=REASON
        )
    else:
        data["reason"] = REASON
    return data


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "private-untrusted" not in response.text


async def test_release_failure_rolls_back_cash_and_idempotency_before_original_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, "void", book)
    headers = version_headers(client, 1, key="synthetic-correction-release-failure")
    before = database_snapshot(engine)
    with patch.object(
        Connection,
        "_release_savepoint_impl",
        side_effect=SQLAlchemyError("private-untrusted release failure"),
    ):
        response = await client.post(path(trade, "void"), json=data, headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "TRADE_CORRECTION_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(path(trade, "void"), json=data, headers=headers)
    assert retry.status_code == 200
    safe(retry)
    with unit_of_work(engine) as con:
        entries = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,entry_role FROM cash_ledger_entries "
            "WHERE source_type='TRADE' AND source_id=? ORDER BY rowid",
            (str(trade),),
        ).all()
    assert [tuple(row) for row in entries] == [
        (-1000000000, 7800000000000, "PRIMARY"),
        (1000000000, 7800000000000, "REVERSAL"),
    ]


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_scoped_correction_is_atomic_and_replays_after_withdrawal(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    data = body(trade, operation, book)
    headers = version_headers(client, 1, key="synthetic-unverified-correction-intent")
    before = database_snapshot(engine)
    original = await client.post(path(trade, operation), json=data, headers=headers)
    assert original.status_code == 200, original.text
    safe(original)
    assert "etag" not in original.headers
    result = original.json()
    assert UUID(result["authorization_id"])
    assert result["scope"] == "UNVERIFIED_TRADE_CORRECTION_ONLY"
    assert result["history_completeness"] == "UNVERIFIED"
    assert result["trade"]["status"] == ("DRAFT_SCHEDULED" if operation == "revise" else "VOID")
    assert result["trade"]["root_id"] == str(trade)
    after = database_snapshot(engine)
    assert len(after["trade_correction_authorizations"]) == 1
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert after["cash_ledger_entries"] == before["cash_ledger_entries"]
    with unit_of_work(engine) as con:
        audit = con.exec_driver_sql(
            "SELECT request_id FROM audit_events "
            "WHERE entity_type='trade_correction_authorization' AND entity_id=?",
            (result["authorization_id"],),
        ).scalar_one()
        assert audit == original.headers["x-request-id"]
    decision(engine, owner, trade, trade_version=2, revision=1, action="WITHDRAW")
    after_withdrawal = database_snapshot(engine)
    replayed = await client.post(path(trade, operation), json=data, headers=headers)
    assert replayed.status_code == 200 and replayed.json() == result
    safe(replayed)
    assert database_snapshot(engine) == after_withdrawal
    conflict = await client.post(
        path(trade, operation),
        json={**data, "reviews": [pin(trade, revision="2")]},
        headers=headers,
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == after_withdrawal


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_active_correction_reverses_original_cash_at_original_fx(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    if operation == "revise":
        data["revision"]["trade_date"] = "2026-01-02"
    result = await client.post(
        path(trade, operation), json=data, headers=version_headers(client, 1)
    )
    assert result.status_code == 200, result.text
    with unit_of_work(engine) as con:
        old = con.exec_driver_sql(
            "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro,entry_role "
            "FROM cash_ledger_entries WHERE source_type='TRADE' AND source_id=? ORDER BY rowid",
            (str(trade),),
        ).all()
        assert [tuple(row) for row in old] == [
            (-1000000000, 7800000000000, -7800000000, "PRIMARY"),
            (1000000000, 7800000000000, 7800000000, "REVERSAL"),
        ]
        if operation == "revise":
            current = con.exec_driver_sql(
                "SELECT amount_micro,fx_rate_hkd_e12,booked_hkd_micro FROM cash_ledger_entries "
                "WHERE source_type='TRADE' AND source_id=? AND entry_role='PRIMARY'",
                (result.json()["trade"]["id"],),
            ).one()
            assert tuple(current) == (-1100000000, 7900000000000, -8690000000)


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_ordinary_trade_routes_remain_fenced_after_review(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    before = database_snapshot(engine)
    response = await client.request(
        "PATCH" if operation == "revise" else "DELETE",
        f"/api/v1/trades/{trade}",
        json=data["revision"] if operation == "revise" else {"reason": REASON},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 409 and response.json()["code"] == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key", "reauth"])
async def test_authentication_csrf_origin_and_recent_password_are_required(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
    failure: str,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    headers = version_headers(client, 1)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "invalid-synthetic-csrf"
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
    response = await client.post(
        path(trade, operation), json=body(trade, operation, book), headers=headers
    )
    assert (
        response.status_code
        == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422, "reauth": 403}[failure]
    )
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_cached_replay_requires_a_fresh_password_confirmation(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    headers = version_headers(client, 1, key="synthetic-unverified-correction-reauth")
    data = body(trade, operation, book)
    original = await client.post(path(trade, operation), json=data, headers=headers)
    assert original.status_code == 200
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path(trade, operation), json=data, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    after_confirmation = database_snapshot(engine)
    replayed = await client.post(path(trade, operation), json=data, headers=headers)
    assert replayed.status_code == 200 and replayed.json() == original.json()
    assert database_snapshot(engine) == after_confirmation


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize(
    "failure",
    [
        "confirmation",
        "missing-confirmation",
        "gap",
        "revision-number",
        "revision-zero",
        "extra",
        "reason-nul",
        "duplicate",
        "too-many",
    ],
)
async def test_invalid_scope_is_rejected_without_echo_or_writes(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
    failure: str,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    if failure == "confirmation":
        data["confirmation"] = "VERIFIED"
    elif failure == "missing-confirmation":
        del data["confirmation"]
    elif failure == "gap":
        data["reviews"][0]["gap"] = "MISSING_RETIRED_ORIGIN"
    elif failure == "revision-number":
        data["reviews"][0]["review_revision"] = 1
    elif failure == "revision-zero":
        data["reviews"][0]["review_revision"] = "0"
    elif failure == "extra":
        data["private-untrusted"] = "private-untrusted"
    elif failure == "reason-nul":
        if operation == "revise":
            data["revision"]["reason"] = "private-untrusted\0reason"
        else:
            data["reason"] = "private-untrusted\0reason"
    elif failure == "duplicate":
        data["reviews"].append(deepcopy(data["reviews"][0]))
    else:
        data["reviews"] = [pin(uuid4()) for _ in range(101)]
    before = database_snapshot(engine)
    response = await client.post(
        path(trade, operation), json=data, headers=version_headers(client, 1)
    )
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_every_missing_root_gap_needs_an_exact_current_pin(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    root = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, root)
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, root, trade_version=2)
    decision(engine, owner, current)
    data = body(current, operation, book)
    before = database_snapshot(engine)
    missing = await client.post(
        path(current, operation), json=data, headers=version_headers(client, 1)
    )
    assert missing.status_code == 409 and missing.json()["code"] == "TRADE_REVIEW_SCOPE_CHANGED"
    assert database_snapshot(engine) == before
    data["reviews"] = [pin(root, 2), pin(current)]
    data["reviews"][0]["review_revision"] = "2"
    stale = await client.post(
        path(current, operation), json=data, headers=version_headers(client, 1)
    )
    assert stale.status_code in (409, 412)
    assert database_snapshot(engine) == before
    data["reviews"][0]["review_revision"] = "1"
    headers = version_headers(client, 1, key="synthetic-multiple-gap-correction")
    accepted = await client.post(path(current, operation), json=data, headers=headers)
    assert accepted.status_code == 200
    with unit_of_work(engine) as con:
        scope = con.exec_driver_sql(
            "SELECT scope_json FROM trade_correction_authorizations"
        ).scalar_one()
        assert {entry["trade_id"] for entry in json.loads(scope)} == {str(root), str(current)}
    after = database_snapshot(engine)
    reordered = {**data, "reviews": list(reversed(data["reviews"]))}
    replayed = await client.post(path(current, operation), json=reordered, headers=headers)
    assert replayed.status_code == 200 and replayed.json() == accepted.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_competing_requests_and_same_key_replay_commit_only_once(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    headers = version_headers(client, 1, key="synthetic-concurrent-unverified-correction")
    first, same = await asyncio.gather(
        *(client.post(path(trade, operation), json=data, headers=headers) for _ in range(2))
    )
    assert first.status_code == same.status_code == 200 and first.json() == same.json()
    before = database_snapshot(engine)
    other = await client.post(path(trade, operation), json=data, headers=version_headers(client, 1))
    assert other.status_code in (409, 412)
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM trade_correction_authorizations").scalar_one()
            == 1
        )


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize(
    "tamper", ["authorization", "trade", "scope", "missing-scope", "missing-history"]
)
async def test_corrupt_cached_response_is_not_replayed(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
    tamper: str,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    headers = version_headers(client, 1, key="synthetic-corrupt-unverified-replay")
    original = await client.post(path(trade, operation), json=data, headers=headers)
    assert original.status_code == 200
    cached = original.json()
    if tamper == "authorization":
        cached["authorization_id"] = str(uuid4())
    elif tamper == "trade":
        cached["trade"]["note"] = "private-untrusted"
    elif tamper == "missing-scope":
        del cached["scope"]
    elif tamper == "missing-history":
        del cached["history_completeness"]
    else:
        cached["scope"] = "VERIFIED"
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(cached), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(path(trade, operation), json=data, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_foreign_owner_cannot_correct_another_owners_trade(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
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
    response = await client.post(
        path(trade, operation),
        json=body(trade, operation, book),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 404
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_oversell_replay_rolls_back_the_entire_correction(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    await create(
        client, "trades", payload(book, side="SELL", trade_date="2026-01-03", quantity="9")
    )
    data = body(trade, operation, book)
    if operation == "revise":
        data["revision"].update(trade_date="2026-01-02", quantity="8")
    before = database_snapshot(engine)
    response = await client.post(
        path(trade, operation), json=data, headers=version_headers(client, 1)
    )
    assert response.status_code == 409 and response.json()["code"] == "INSUFFICIENT_SHARES"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_security_is_rechecked_in_the_writer_before_new_or_cached_action(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    replay: bool,
    failure: str,
) -> None:
    import spm_api.trade_routes as routes
    from spm_api.mutations import mutate

    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    headers = version_headers(client, 1, key="synthetic-unverified-writer-security")
    if replay:
        assert (
            await client.post(path(trade, operation), json=data, headers=headers)
        ).status_code == 200
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
    response = await client.post(path(trade, operation), json=data, headers=headers)
    assert response.status_code == (401 if failure == "revoked" else 403)
    safe(response)
    assert database_snapshot(engine) == checkpoint["before"]


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_serialization_failure_rolls_back_finance_audit_and_idempotency(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    from spm_api.trade_models import TradeUnverifiedCorrectionRecord

    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    if operation == "revise":
        data["revision"]["trade_date"] = "2026-01-02"
    before = database_snapshot(engine)

    def fail(*args: Any, **kwargs: Any) -> str:
        raise ValueError("private-untrusted synthetic serialization failure")

    monkeypatch.setattr(TradeUnverifiedCorrectionRecord, "model_dump_json", fail)
    response = await client.post(
        path(trade, operation), json=data, headers=version_headers(client, 1)
    )
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize(
    "header", [None, "1", '"0"', '"01"', '"9223372036854775808"', '"' + "9" * 4500 + '"']
)
async def test_if_match_is_required_and_bounded_before_integer_conversion(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
    header: str | None,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    response = await client.post(
        path(trade, operation), json=body(trade, operation, book), headers=headers
    )
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_withdrawal_between_dependency_and_writer_cannot_authorize_correction(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    import spm_api.trade_routes as routes
    from spm_api.mutations import mutate

    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    checkpoint: dict[str, Any] = {}

    def withdraw_after_dependency(*args: Any, **kwargs: Any) -> Any:
        decision(engine, owner, trade, revision=1, action="WITHDRAW")
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", withdraw_after_dependency)
    response = await client.post(
        path(trade, operation),
        json=body(trade, operation, book),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 409 and response.json()["code"] == "TRADE_REVIEW_REQUIRED"
    safe(response)
    assert database_snapshot(engine) == checkpoint["before"]


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize("matching", [False, True])
async def test_late_creation_audit_never_grants_a_new_correction(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
    matching: bool,
) -> None:
    from spm_api.audit import audit
    from spm_api.trade_codec import as_trade

    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json") if matching else {"synthetic": "conflict"},
        )
    before = database_snapshot(engine)
    response = await client.post(
        path(trade, operation),
        json=body(trade, operation, book),
        headers=version_headers(client, 1),
    )
    assert response.status_code == (409 if matching else 503)
    assert response.json()["code"] == (
        "TRADE_REVIEW_CHANGED" if matching else "TRADE_REVIEW_UNAVAILABLE"
    )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_valid_unrelated_authorization_cannot_replace_the_cached_one(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    owner = UUID(book["owner"]["id"])
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    second = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, owner, first)
    decision(engine, owner, second)
    first_body = body(first, operation, book)
    headers = version_headers(client, 1, key="synthetic-first-correction-replay")
    first_result = await client.post(path(first, operation), json=first_body, headers=headers)
    second_result = await client.post(
        path(second, operation),
        json=body(second, operation, book),
        headers=version_headers(client, 1),
    )
    assert first_result.status_code == second_result.status_code == 200
    cached = first_result.json()
    cached["authorization_id"] = second_result.json()["authorization_id"]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(cached), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replayed = await client.post(path(first, operation), json=first_body, headers=headers)
    assert replayed.status_code == 503
    safe(replayed)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_original_http_result_replays_after_a_subsequent_correction(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), first)
    initial = body(first, "revise", book)
    headers = version_headers(client, 1, key="synthetic-historical-correction-http")
    first_result = await client.post(path(first, "revise"), json=initial, headers=headers)
    assert first_result.status_code == 200
    current = UUID(first_result.json()["trade"]["id"])
    subsequent = body(current, operation, book)
    subsequent["reviews"] = [pin(first, version=2)]
    second_result = await client.post(
        path(current, operation), json=subsequent, headers=version_headers(client, 1)
    )
    assert second_result.status_code == 200
    after = database_snapshot(engine)
    replayed = await client.post(path(first, "revise"), json=initial, headers=headers)
    assert replayed.status_code == 200 and replayed.json() == first_result.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("value", [1, 1.0, True, None, "01", "-1", "1.0", "1e0", "9" * 4500])
async def test_review_revision_requires_exact_bounded_decimal_string(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], value: object
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, "void", book)
    data["reviews"][0]["review_revision"] = value
    before = database_snapshot(engine)
    response = await client.post(path(trade, "void"), json=data, headers=version_headers(client, 1))
    assert response.status_code == 422
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["9007199254740993", "9223372036854775807"])
async def test_large_exact_review_revision_is_fenced_without_rounding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], value: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, "void", book)
    data["reviews"][0]["review_revision"] = value
    before = database_snapshot(engine)
    response = await client.post(path(trade, "void"), json=data, headers=version_headers(client, 1))
    assert response.status_code == 412
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["quantity", "price", "fee", "tax", "fx_rate_hkd"])
async def test_financial_floats_are_rejected_at_the_api_boundary(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, "revise", book)
    data["revision"][field] = 1.5
    before = database_snapshot(engine)
    response = await client.post(
        path(trade, "revise"), json=data, headers=version_headers(client, 1)
    )
    assert response.status_code == 422
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_stale_physical_version_does_not_write(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    decision(engine, UUID(book["owner"]["id"]), trade)
    before = database_snapshot(engine)
    response = await client.post(
        path(trade, operation),
        json=body(trade, operation, book),
        headers=version_headers(client, 2),
    )
    assert response.status_code == 412
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["revise", "void"])
@pytest.mark.parametrize("sequence", [9007199254740993, 9223372036854775807])
async def test_original_and_replayed_http_responses_preserve_int64_digits(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Operation,
    sequence: int,
) -> None:
    trade, at = uuid4(), stamp()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO ledger_sequences(sequence,kind,fact_id) VALUES (?,'TRADE',?)",
            (sequence, str(trade)),
        )
        insert(
            con,
            "trades",
            {
                "id": str(trade),
                "root_id": str(trade),
                "created_sequence": sequence,
                "portfolio_id": book["portfolio"]["id"],
                "account_id": book["account"]["id"],
                "security_id": book["security"]["id"],
                "side": "BUY",
                "trade_date": "2999-01-02",
                "quantity_e8": 1000000000,
                "price_e8": 10000000000,
                "currency": "USD",
                "gross_micro": 1000000000,
                "settlement_micro": 1000000000,
                "status": "DRAFT_SCHEDULED",
                "created_at": at,
                "updated_at": at,
            },
        )
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    headers = version_headers(client, 1, key="synthetic-exact-int64-correction")
    result = await client.post(path(trade, operation), json=data, headers=headers)
    assert result.status_code == 200
    assert f'"created_sequence":{sequence}' in result.text
    after = database_snapshot(engine)
    replayed = await client.post(path(trade, operation), json=data, headers=headers)
    assert replayed.status_code == 200 and replayed.text == result.text
    assert f'"created_sequence":{sequence}' in replayed.text
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["revise", "void"])
async def test_late_authorization_audit_failure_is_private_and_rolls_back_everything(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from spm_api import trade_unverified_correction

    trade = active_auditless_buy(engine, book)
    decision(engine, UUID(book["owner"]["id"]), trade)
    data = body(trade, operation, book)
    if operation == "revise":
        data["revision"]["trade_date"] = "2026-01-02"
    before = database_snapshot(engine)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("private-untrusted", {}, RuntimeError("private-untrusted"))

    monkeypatch.setattr(trade_unverified_correction, "audit", fail)
    response = await client.post(
        path(trade, operation), json=data, headers=version_headers(client, 1)
    )
    assert response.status_code == 503 and response.json()["code"] == "TRADE_CORRECTION_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
