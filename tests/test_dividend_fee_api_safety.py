"""Private HTTP, idempotency and failure boundaries for fee estimates."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_fee_models as models
import spm_api.dividend_fee_routes as routes
import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as writer
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.master_service import invalidate_portfolio
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_fee_api import body, path, safe, selection
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "code,status",
    [
        ("DIVIDEND_FEE_NOT_FOUND", 404),
        ("DIVIDEND_FEE_OWNER_UNAVAILABLE", 401),
        ("INVALID_DIVIDEND_FEE_SCOPE", 422),
        ("DIVIDEND_FEE_VERSION_CONFLICT", 412),
        ("DIVIDEND_FEE_TIME_CONFLICT", 409),
        ("synthetic-private", 503),
    ],
)
def test_internal_errors_map_to_fixed_private_http_responses(code: str, status: int) -> None:
    error = routes._failure(fees.FeeSettingError(code))
    assert error.status == status and "synthetic-private" not in error.message
    if status == 503:
        assert error.code == "DIVIDEND_FEE_HISTORY_UNAVAILABLE"


async def test_foreign_owner_and_missing_session_cannot_read_or_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
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
    for suffix, params in (
        ("", {"effective_on": "2026-01-08"}),
        ("/history", {"revision_id": str(book["event"].revision_id), "revision_no": "1"}),
    ):
        result = await client.get(path(book) + suffix, params=params)
        assert result.status_code == 404
        safe(result)
    result = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert result.status_code == 404
    safe(result)
    client.cookies.clear()
    result = await client.get(path(book), params={"effective_on": "2026-01-08"})
    assert result.status_code == 401
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_security_is_rechecked_in_writer_before_new_action_or_replay(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    replay: bool,
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1)
    if replay:
        assert (await client.post(path(book), json=body(scope), headers=headers)).status_code == 201
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
    result = await client.post(path(book), json=body(scope), headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize("corruption", ["scope", "fee", "reason", "number", "id", "status"])
async def test_replay_cache_cannot_replace_the_audited_setting(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-cache-corruption")
    first = await client.post(path(book), json=body(scope, fee="1"), headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql(
            "SELECT id,response_json FROM idempotency_keys WHERE idem_key=?",
            (headers["Idempotency-Key"],),
        ).one()
        cached = json.loads(row.response_json)
        if corruption == "scope":
            del cached["current_use_scope"]
        elif corruption == "fee":
            cached["fee"] = "0.500000"
        elif corruption == "reason":
            cached["reason"] = "Different synthetic private-value reason"
        elif corruption == "number":
            cached["setting_no"] = "2"
        elif corruption == "status":
            cached["fee_status"] = "UNKNOWN"
        else:
            cached["id"] = str(uuid4())
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE id=?", (json.dumps(cached), row.id)
        )
    before = database_snapshot(engine)
    result = await client.post(path(book), json=body(scope, fee="1"), headers=headers)
    assert result.status_code == 503 and result.json()["code"] == "DIVIDEND_FEE_HISTORY_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_http_intent_audit_rejects_a_forged_key_and_request_cache(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    first_headers = version_headers(client, 1, key="synthetic-fee-original-key")
    first = await client.post(path(book), json=body(scope, fee="1"), headers=first_headers)
    assert first.status_code == 201
    second_scope = await selection(client, book)
    second_body = body(second_scope, fee="2")
    second = await client.post(
        path(book),
        json=second_body,
        headers=version_headers(client, 1, key="synthetic-fee-other-key"),
    )
    assert second.status_code == 201
    forged = {
        "event_version": 1,
        **models.FeeSettingWrite.model_validate(second_body).model_dump(mode="json"),
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=?,response_json=? WHERE idem_key=?",
            (
                digest(canonical(forged)),
                json.dumps(second.json()),
                first_headers["Idempotency-Key"],
            ),
        )
    before = database_snapshot(engine)
    result = await client.post(path(book), json=second_body, headers=first_headers)
    assert result.status_code == 503 and result.json()["code"] == "DIVIDEND_FEE_HISTORY_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_duplicate_fee_audit_blocks_read_replay_and_new_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-duplicate")
    first = await client.post(path(book), json=body(scope), headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_fee_setting",
            first.json()["id"],
            "DIVIDEND_FEE_SETTING_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason="Synthetic duplicate audit.",
        )
    before = database_snapshot(engine)
    results = [
        await client.get(path(book), params={"effective_on": "2026-01-08"}),
        await client.get(
            path(book) + "/history",
            params={"revision_id": str(book["event"].revision_id), "revision_no": 1},
        ),
        await client.post(path(book), json=body(scope), headers=headers),
        await client.post(path(book), json=body(scope), headers=version_headers(client, 1)),
    ]
    for result in results:
        assert result.status_code == 503
        safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["invalidation", "http-audit", "conversion", "release"])
async def test_failure_rolls_back_fee_both_audits_and_idempotency_before_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-failure-retry")
    before = database_snapshot(engine)
    original_public = routes._public

    def fail_invalidation(*args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(*args, **kwargs)
        raise SQLAlchemyError("private-value")

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        audit(*args, **kwargs)
        raise SQLAlchemyError("private-value")

    def fail_conversion(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("private-value")

    target, method, side_effect = {
        "invalidation": (writer, "invalidate_portfolio", fail_invalidation),
        "http-audit": (routes, "audit", fail_audit),
        "conversion": (routes, "_public", fail_conversion),
        "release": (Connection, "_release_savepoint_impl", SQLAlchemyError("private-value")),
    }[failure]
    with patch.object(target, method, side_effect=side_effect):
        failed = await client.post(path(book), json=body(scope), headers=headers)
    assert routes._public is original_public
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before
    retry = await client.post(path(book), json=body(scope), headers=headers)
    assert retry.status_code == 201


async def test_account_change_is_visible_but_never_inherits_an_old_setting(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    first = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert first.status_code == 201
    old = await selection(client, book)
    changed = await client.patch(
        f"/api/v1/brokers/{book['broker']['id']}",
        json={"dividend_fee": "1.234567"},
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    latest = await selection(client, book)
    assert latest["broker_version"] == "2" and latest["selected_scope"] == "ACCOUNT_SCOPE_CHANGED"
    assert latest["selected"]["id"] == first.json()["id"]
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=body(old), headers=version_headers(client, 1))
    assert denied.status_code == 409 and denied.json()["code"] == "DIVIDEND_FEE_SCOPE_CHANGED"
    assert database_snapshot(engine) == before
    updated = await client.post(path(book), json=body(latest), headers=version_headers(client, 1))
    assert updated.status_code == 201 and updated.json()["broker_version"] == "2"
    assert (await selection(client, book))["selected_scope"] == "ACCOUNT_SCOPE_MATCH"


async def test_exact_physical_revision_and_head_identity_are_required(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    missing = await selection(client, book)
    before = database_snapshot(engine)
    wrong_revision = await client.post(
        path(book),
        json={**body(missing), "revision_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert wrong_revision.status_code == 412
    assert database_snapshot(engine) == before
    first = await client.post(path(book), json=body(missing), headers=version_headers(client, 1))
    assert first.status_code == 201
    scope = await selection(client, book)
    before = database_snapshot(engine)
    wrong_head = await client.post(
        path(book),
        json={**body(scope), "expected_head_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert wrong_head.status_code == 412
    assert database_snapshot(engine) == before
    absent = await client.get(
        path(book) + "/history",
        params={
            "revision_id": str(uuid4()),
            "revision_no": 1,
        },
    )
    assert absent.status_code == 404
    safe(absent)


async def test_history_search_cannot_search_private_reasons(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    first = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert first.status_code == 201
    before = database_snapshot(engine)
    page = await client.get(
        path(book) + "/history",
        params={
            "revision_id": str(book["event"].revision_id),
            "revision_no": 1,
            "q": "Synthetic owner fee estimate",
        },
    )
    assert page.status_code == 200 and page.json()["items"] == []
    assert database_snapshot(engine) == before


async def test_missing_rejection_history_and_read_conversion_fail_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    first = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert first.status_code == 201
    before = database_snapshot(engine)
    real = rejections.history
    calls = 0

    def missing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs) if calls == 1 else None

    with patch.object(rejections, "history", side_effect=missing):
        result = await client.get(path(book), params={"effective_on": "2026-01-08"})
    assert result.status_code == 503 and calls == 2
    for suffix, params in (
        ("", {"effective_on": "2026-01-08"}),
        ("/history", {"revision_id": str(book["event"].revision_id), "revision_no": "1"}),
    ):
        with patch.object(routes, "_public", side_effect=ValueError("private-value")):
            result = await client.get(path(book) + suffix, params=params)
        assert result.status_code == 503
        safe(result)
    assert database_snapshot(engine) == before


async def test_http_audit_must_follow_its_correlated_fee_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    created = datetime.now(UTC) + timedelta(seconds=1)

    def fee_audit(*args: Any, **kwargs: Any) -> None:
        with patch("spm_api.audit.now", return_value=created + timedelta(seconds=2)):
            audit(*args, **kwargs)

    def http_audit(*args: Any, **kwargs: Any) -> None:
        with patch("spm_api.audit.now", return_value=created + timedelta(seconds=1)):
            audit(*args, **kwargs)

    with (
        patch.object(writer, "now", return_value=created),
        patch.object(writer, "audit", side_effect=fee_audit),
        patch.object(routes, "audit", side_effect=http_audit),
    ):
        denied = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert denied.status_code == 503
    safe(denied)
    assert database_snapshot(engine) == before


async def test_replay_requires_original_setting_in_complete_retained_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-fee-missing-retained")
    first = await client.post(path(book), json=body(scope), headers=headers)
    assert first.status_code == 201
    before = database_snapshot(engine)
    with patch.object(fees, "history", return_value=()):
        result = await client.post(path(book), json=body(scope), headers=headers)
    assert result.status_code == 503
    safe(result)
    assert database_snapshot(engine) == before


async def test_fee_decision_does_not_change_existing_receipt_cash_or_locked_fx(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(book["event"].revision_id), "currency": "USD"}, book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
    scope = await selection(client, book)
    before = database_snapshot(engine)
    result = await client.post(
        path(book), json=body(scope, fee="0.25", currency="USD"), headers=version_headers(client, 1)
    )
    assert result.status_code == 201
    after = database_snapshot(engine)
    for table in (
        "dividend_entitlements",
        "dividend_receipts",
        "cash_ledger_entries",
        "dividend_event_revisions",
    ):
        assert before[table] == after[table]
    with unit_of_work(engine) as con:
        fx = con.exec_driver_sql(
            "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
        ).scalar_one()
    assert fx == 7_800_000_000_000


async def test_account_scope_is_rechecked_in_the_write_transaction(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = await selection(client, book)
    checkpoint: dict[str, Any] = {}

    def changed(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE brokers SET version=version+1 WHERE id=?", (book["broker"]["id"],)
            )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed)
    denied = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert denied.status_code == 409 and denied.json()["code"] == "DIVIDEND_FEE_SCOPE_CHANGED"
    safe(denied)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_exact_int64_versions_and_concurrent_same_key_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET version=? WHERE id=?", (9007199254740993, book["account"]["id"])
        )
        con.exec_driver_sql(
            "UPDATE brokers SET version=? WHERE id=?", (9223372036854775807, book["broker"]["id"])
        )
    scope = await selection(client, book)
    assert scope["account_version"] == "9007199254740993"
    assert scope["broker_version"] == "9223372036854775807"
    headers = version_headers(client, 1, key="synthetic-fee-exact-versions")
    first, replay = await asyncio.gather(
        client.post(path(book), json=body(scope), headers=headers),
        client.post(path(book), json=body(scope), headers=headers),
    )
    assert first.status_code == replay.status_code == 201 and first.json() == replay.json()
    assert first.json()["account_version"] == scope["account_version"]
    assert first.json()["broker_version"] == scope["broker_version"]
    with unit_of_work(engine) as con:
        assert tuple(
            con.exec_driver_sql(
                "SELECT account_version,broker_version FROM dividend_fee_settings WHERE id=?",
                (first.json()["id"],),
            ).one()
        ) == (9007199254740993, 9223372036854775807)


@pytest.mark.parametrize(
    "header", [None, "1", '"0"', '"01"', '"2147483648"', '"' + "9" * 4500 + '"']
)
async def test_if_match_is_bounded_before_conversion(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], header: str | None
) -> None:
    scope = await selection(client, book)
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    result = await client.post(path(book), json=body(scope), headers=headers)
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("cursor", ["0", "01", "-1", "9223372036854775808", "private-value"])
async def test_history_cursor_rejects_invalid_int64(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], cursor: str
) -> None:
    encoded = base64.urlsafe_b64encode(json.dumps([cursor, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    result = await client.get(
        path(book) + "/history",
        params={
            "revision_id": str(book["event"].revision_id),
            "revision_no": 1,
            "cursor": encoded,
        },
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


async def test_effective_read_and_write_do_not_access_provider_configuration(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        scope = await selection(client, book)
        result = await client.post(path(book), json=body(scope), headers=version_headers(client, 1))
    assert result.status_code == 201


async def test_inconsistent_response_models_are_rejected(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    empty = await selection(client, book)
    changes: dict[str, Any]
    for changes in (
        {"head_no": "1", "head_id": None},
        {"fee_status": "UNKNOWN"},
        {"selected_scope": "ACCOUNT_SCOPE_MATCH"},
    ):
        with pytest.raises(ValidationError):
            models.FeeSettingSelection.model_validate({**empty, **changes})
    saved = await client.post(path(book), json=body(empty), headers=version_headers(client, 1))
    assert saved.status_code == 201
    record = saved.json()
    for changes in (
        {"setting_no": "2"},
        {"fee_status": "UNKNOWN"},
        {"fee": None},
        {"fee_currency": None},
    ):
        with pytest.raises(ValidationError):
            models.FeeSettingRecord.model_validate({**record, **changes})
    selected = await selection(client, book)
    for changes in (
        {"fee_status": "UNKNOWN"},
        {"head_no": "2", "head_id": selected["selected"]["id"]},
        {"selected_scope": "ACCOUNT_SCOPE_CHANGED"},
        {"effective_on": "2025-01-01"},
    ):
        with pytest.raises(ValidationError):
            models.FeeSettingSelection.model_validate({**selected, **changes})


def test_openapi_pins_versions_as_strings_and_requires_security() -> None:
    from spm_api.main import create_app

    spec = create_app().openapi()
    uri = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings"
    operation = spec["paths"][uri]["post"]
    headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
    assert operation["security"] == [{"cookieAuth": []}]
    assert all(
        headers[name]["required"] for name in ("If-Match", "X-CSRF-Token", "Idempotency-Key")
    )
    properties = spec["components"]["schemas"]["FeeSettingWrite"]["properties"]
    for name in ("account_version", "broker_version", "expected_head_no"):
        assert properties[name]["type"] == "string"
