"""Protected HTTP proofs for explicit owner tax estimates."""

import asyncio
import base64
import json
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_writer as writer
import spm_api.dividend_tax_models as models
import spm_api.dividend_tax_routes as routes
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.master_service import invalidate_portfolio
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import (
    COOKIE,
    CSRF_COOKIE,
    issue_session,
    matching_dividend_event_version,
)
from tests.helpers import (
    PASSWORD,
    create,
    database_snapshot,
    initialize,
    version_headers,
    write_headers,
)
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement

pytestmark = pytest.mark.anyio
REASON = "Synthetic owner tax estimate, not confirmed withholding."


@pytest.fixture
async def book(client: httpx.AsyncClient, engine: Engine) -> dict[str, Any]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic tax portfolio"})
    broker = await create(client, "brokers", {"name": "Synthetic tax broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic tax account",
            "default_currency": "USD",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Synthetic tax security",
            "quote_currency": "HKD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    event = writer.EventIntent.model_validate(
        {
            "event_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "revision_no": 1,
            "kind": "MANUAL",
            "terms": {
                "security_id": security["id"],
                "ex_date": "2026-01-08",
                "dps": "0.90000000",
                "currency": "USD",
                "adjustment_basis": "AS_DECLARED",
            },
            "citation": "Synthetic event reference",
            "reason": REASON,
        }
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(con, UUID(owner["id"]), event, at=now(), request_id=uuid4())
    return {
        "owner": owner,
        "portfolio": portfolio,
        "broker": broker,
        "account": account,
        "event": event,
    }


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account']['id']}/tax-settings"
    )


async def selection(
    client: httpx.AsyncClient, book: dict[str, Any], day: str = "2026-01-08"
) -> dict[str, Any]:
    response = await client.get(path(book), params={"effective_on": day})
    assert response.status_code == 200, response.text
    return dict(response.json())


def decision(
    scope: dict[str, Any], *, rate: Any = "0.3", day: str = "2026-01-01"
) -> dict[str, Any]:
    return {
        "revision_id": scope["revision_id"],
        "portfolio_id": scope["portfolio_id"],
        "broker_id": scope["broker_id"],
        "account_version": scope["account_version"],
        "broker_version": scope["broker_version"],
        "expected_head_no": scope["head_no"],
        "expected_head_id": scope["head_id"],
        "effective_from": day,
        "rate": rate,
        "reason": REASON,
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text
    assert "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        for private in (REASON, "private-value", "0.300000"):
            assert private not in response.text


def only_tax(before: Any, after: Any) -> None:
    allowed = {
        "dividend_tax_settings",
        "audit_events",
        "idempotency_keys",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: value for key, value in before.items() if key not in allowed} == {
        key: value for key, value in after.items() if key not in allowed
    }


async def test_owner_can_read_missing_and_append_one_audited_estimate(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    missing_response = await client.get(path(book), params={"effective_on": "2026-01-08"})
    assert missing_response.status_code == 200
    safe(missing_response)
    assert "etag" not in missing_response.headers
    missing = missing_response.json()
    assert (missing["head_no"], missing["head_id"], missing["tax_status"], missing["selected"]) == (
        "0",
        None,
        "MISSING",
        None,
    )
    assert missing["selected_scope"] == "NO_SELECTED_SETTING"
    assert missing["account_version"] == missing["broker_version"] == "1"
    assert missing["scope"] == "OWNER_ESTIMATE_ONLY"
    assert missing["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before
    headers = version_headers(client, 1, key="synthetic-tax-initial-estimate")
    response = await client.post(path(book), json=decision(missing), headers=headers)
    assert response.status_code == 201, response.text
    safe(response)
    assert "etag" not in response.headers
    item = response.json()
    assert UUID(item["id"]).version == 4
    assert item["setting_no"] == "1" and item["previous_id"] is None
    assert item["tax_status"] == "ESTIMATED" and item["rate"] == "0.300000"
    assert item["scope"] == "OWNER_ESTIMATE_ONLY"
    assert item["current_use_scope"] == "REVALIDATION_REQUIRED"
    after = database_snapshot(engine)
    only_tax(before, after)
    assert len(after["dividend_tax_settings"]) == len(before["dividend_tax_settings"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT rate_ppm,request_id FROM dividend_tax_settings WHERE id=?", (item["id"],)
        ).one()
        assert row.rate_ppm == 300000 and row.request_id == response.headers["x-request-id"]
        entry = con.exec_driver_sql(
            "SELECT owner_id,request_id FROM audit_events "
            "WHERE entity_type='dividend_tax_setting' AND entity_id=?",
            (item["id"],),
        ).one()
        assert tuple(entry) == (book["owner"]["id"], response.headers["x-request-id"])
    current = await selection(client, book)
    assert current["head_no"] == "1" and current["head_id"] == item["id"]
    assert current["selected"] == item and current["selected_scope"] == "ACCOUNT_SCOPE_MATCH"
    assert current["tax_status"] == "ESTIMATED"
    replay = await client.post(path(book), json=decision(missing), headers=headers)
    assert replay.status_code == 201 and replay.json() == item
    assert database_snapshot(engine) == after


async def test_effective_dates_unknown_zero_and_paginated_history_are_distinct(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first_scope = await selection(client, book)
    first = await client.post(
        path(book),
        json=decision(first_scope, rate="0.35", day="2026-06-01"),
        headers=version_headers(client, 1),
    )
    assert first.status_code == 201
    before_effective = await selection(client, book)
    assert before_effective["tax_status"] == "MISSING" and before_effective["head_no"] == "1"
    second = await client.post(
        path(book), json=decision(before_effective, rate=None), headers=version_headers(client, 1)
    )
    assert second.status_code == 201 and second.json()["tax_status"] == "UNKNOWN"
    unknown = await selection(client, book)
    assert unknown["tax_status"] == "UNKNOWN" and unknown["selected"]["rate"] is None
    third = await client.post(
        path(book), json=decision(unknown, rate="0"), headers=version_headers(client, 1)
    )
    assert third.status_code == 201
    selected = await selection(client, book)
    assert selected["tax_status"] == "ESTIMATED" and selected["selected"]["rate"] == "0.000000"
    assert selected["head_no"] == "3" and selected["selected"]["setting_no"] == "3"
    future = await selection(client, book, "2026-07-01")
    assert future["selected"]["id"] == first.json()["id"] and future["head_no"] == "3"
    params = {"revision_id": str(book["event"].revision_id), "revision_no": "1", "limit": "2"}
    before = database_snapshot(engine)
    page = await client.get(path(book) + "/history", params=params)
    assert page.status_code == 200
    safe(page)
    assert [row["setting_no"] for row in page.json()["items"]] == ["3", "2"]
    assert page.json()["next_cursor"]
    last = await client.get(
        path(book) + "/history", params={**params, "cursor": page.json()["next_cursor"]}
    )
    assert [row["setting_no"] for row in last.json()["items"]] == ["1"]
    assert last.json()["next_cursor"] is None
    assert database_snapshot(engine) == before


async def test_account_scope_change_is_labeled_and_stale_input_cannot_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    initial = await selection(client, book)
    first = await client.post(
        path(book), json=decision(initial), headers=version_headers(client, 1)
    )
    assert first.status_code == 201
    old = await selection(client, book)
    broker = await client.patch(
        f"/api/v1/brokers/{book['broker']['id']}",
        json={"dividend_fee": "1.234567"},
        headers=version_headers(client, 1),
    )
    assert broker.status_code == 200
    changed = await selection(client, book)
    assert changed["broker_version"] == "2"
    assert changed["selected_scope"] == "ACCOUNT_SCOPE_CHANGED"
    assert changed["selected"]["id"] == first.json()["id"]
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=decision(old), headers=version_headers(client, 1))
    assert denied.status_code == 409 and denied.json()["code"] == "DIVIDEND_TAX_SCOPE_CHANGED"
    safe(denied)
    assert database_snapshot(engine) == before
    updated = await client.post(
        path(book), json=decision(changed), headers=version_headers(client, 1)
    )
    assert updated.status_code == 201 and updated.json()["broker_version"] == "2"
    assert (await selection(client, book))["selected_scope"] == "ACCOUNT_SCOPE_MATCH"


async def test_new_event_revision_does_not_inherit_setting_but_old_retry_is_historical(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-historical-replay")
    original = await client.post(path(book), json=decision(scope), headers=headers)
    assert original.status_code == 201
    follow = writer.EventIntent(
        event_id=book["event"].event_id,
        revision_id=uuid4(),
        revision_no=2,
        kind="VERIFY",
        citation="Synthetic verification reference",
        reason=REASON,
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(con, UUID(book["owner"]["id"]), follow, at=now(), request_id=uuid4())
    current = await selection(client, book)
    assert current["revision_no"] == 2 and current["revision_id"] == str(follow.revision_id)
    assert current["head_no"] == "0" and current["tax_status"] == "MISSING"
    before = database_snapshot(engine)
    historical = await client.get(
        path(book) + "/history",
        params={"revision_id": str(book["event"].revision_id), "revision_no": 1},
    )
    assert historical.status_code == 200 and historical.json()["items"] == [original.json()]
    replay = await client.post(path(book), json=decision(scope), headers=headers)
    assert replay.status_code == 201 and replay.json() == original.json()
    stale = await client.post(path(book), json=decision(scope), headers=version_headers(client, 1))
    assert stale.status_code == 412
    assert database_snapshot(engine) == before
    fresh = await client.post(
        path(book), json=decision(current), headers=version_headers(client, 2)
    )
    assert fresh.status_code == 201 and fresh.json()["setting_no"] == "1"


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_security_dependencies_reject_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    scope = await selection(client, book)
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
    response = await client.post(path(book), json=decision(scope), headers=headers)
    assert response.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(response)
    if failure == "cookie":
        for suffix, params in (
            ("", {"effective_on": "2026-01-08"}),
            ("/history", {"revision_id": str(book["event"].revision_id), "revision_no": "1"}),
        ):
            read = await client.get(path(book) + suffix, params=params)
            assert read.status_code == 401
            safe(read)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_read_or_write_tax_settings(
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
    result = await client.post(path(book), json=decision(scope), headers=version_headers(client, 1))
    assert result.status_code == 404
    safe(result)
    assert database_snapshot(engine) == before


async def test_recent_password_is_rechecked_for_historical_replay(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-reauth-replay")
    first = await client.post(path(book), json=decision(scope), headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=decision(scope), headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    safe(denied)
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(path(book), json=decision(scope), headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
async def test_writer_rechecks_security_after_dependencies(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1)
    if replay:
        assert (
            await client.post(path(book), json=decision(scope), headers=headers)
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
    result = await client.post(path(book), json=decision(scope), headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


@pytest.mark.parametrize(
    "header", [None, "1", '"0"', '"01"', '"2147483648"', '"' + "9" * 4500 + '"']
)
async def test_if_match_is_bounded_before_integer_conversion(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], header: str | None
) -> None:
    scope = await selection(client, book)
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    result = await client.post(path(book), json=decision(scope), headers=headers)
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "value",
    [
        0,
        1.0,
        True,
        None,
        "",
        "00",
        "01",
        "-1",
        "1.0",
        "1e0",
        "1\n",
        "9223372036854775807",
        "9" * 4500,
    ],
)
async def test_head_requires_advanceable_canonical_string(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], value: object
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    body = {**decision(scope), "expected_head_no": value}
    result = await client.post(path(book), json=body, headers=version_headers(client, 1))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["9007199254740993", "9223372036854775806"])
async def test_large_exact_head_is_compared_without_number_rounding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], value: str
) -> None:
    scope = await selection(client, book)
    body = {**decision(scope), "expected_head_no": value, "expected_head_id": str(uuid4())}
    before = database_snapshot(engine)
    result = await client.post(path(book), json=body, headers=version_headers(client, 1))
    assert result.status_code == 412
    assert database_snapshot(engine) == before
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    history = await client.get(
        path(book) + "/history",
        params={"revision_id": str(book["event"].revision_id), "revision_no": 1, "cursor": cursor},
    )
    assert history.status_code == 200 and history.json()["items"] == []
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("rate", [0.3, -1, True, "-0.1", "1.000001", "0.0000001", "1e-1", "nan"])
async def test_invalid_rate_is_not_rounded_echoed_or_written(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], rate: object
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    result = await client.post(
        path(book), json=decision(scope, rate=rate), headers=version_headers(client, 1)
    )
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "failure",
    ["missing-rate", "missing-head", "extra", "date", "reason", "zero-version", "numeric-version"],
)
async def test_invalid_input_is_not_echoed_or_written(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    scope = await selection(client, book)
    body = decision(scope)
    if failure == "missing-rate":
        del body["rate"]
    elif failure == "missing-head":
        del body["expected_head_id"]
    elif failure == "extra":
        body["private-value"] = "not accepted"
    elif failure == "date":
        body["effective_from"] = "2026-02-30"
    elif failure == "reason":
        body["reason"] = "private-value\0invalid"
    elif failure == "zero-version":
        body["broker_version"] = "0"
    else:
        body["account_version"] = 1
    before = database_snapshot(engine)
    result = await client.post(path(book), json=body, headers=version_headers(client, 1))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


async def test_competing_writes_and_key_replays_are_serialized(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    body = decision(scope)
    headers = version_headers(client, 1, key="synthetic-same-tax-intent")
    first, replay = await asyncio.gather(
        *(client.post(path(book), json=body, headers=headers) for _ in range(2))
    )
    assert first.status_code == replay.status_code == 201 and first.json() == replay.json()
    before = database_snapshot(engine)
    changed = await client.post(path(book), json={**body, "rate": "0.2"}, headers=headers)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await client.post(path(book), json=body, headers=version_headers(client, 1))
    assert stale.status_code == 412
    assert database_snapshot(engine) == before
    current = await selection(client, book)
    results = await asyncio.gather(
        *(
            client.post(path(book), json=decision(current), headers=version_headers(client, 1))
            for _ in range(2)
        )
    )
    assert sorted(result.status_code for result in results) == [201, 412]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_tax_settings").scalar_one() == 2
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='dividend_tax_setting'"
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "corruption", ["missing-scope", "rate", "reason", "setting-no", "identifier", "status"]
)
async def test_cached_response_must_match_original_audited_history_and_intent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-corrupt-cache")
    first = await client.post(path(book), json=decision(scope), headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql(
            "SELECT id,response_json FROM idempotency_keys WHERE idem_key=?",
            (headers["Idempotency-Key"],),
        ).one()
        cached = json.loads(row.response_json)
        if corruption == "missing-scope":
            del cached["current_use_scope"]
        elif corruption == "rate":
            cached["rate"] = "0.500000"
        elif corruption == "reason":
            cached["reason"] = "A different private-value synthetic reason"
        elif corruption == "setting-no":
            cached["setting_no"] = "2"
        elif corruption == "status":
            cached["tax_status"] = "UNKNOWN"
        else:
            cached["id"] = str(uuid4())
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE id=?", (json.dumps(cached), row.id)
        )
    before = database_snapshot(engine)
    result = await client.post(path(book), json=decision(scope), headers=headers)
    assert result.status_code == 503 and result.json()["code"] == "DIVIDEND_TAX_HISTORY_UNAVAILABLE"
    safe(result)
    assert database_snapshot(engine) == before


async def test_duplicate_late_audit_blocks_read_replay_and_new_write_without_repair(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-duplicate-audit")
    first = await client.post(path(book), json=decision(scope), headers=headers)
    assert first.status_code == 201
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_tax_setting",
            first.json()["id"],
            "DIVIDEND_TAX_SETTING_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    responses = [
        await client.get(path(book), params={"effective_on": "2026-01-08"}),
        await client.get(
            path(book) + "/history",
            params={"revision_id": str(book["event"].revision_id), "revision_no": 1},
        ),
        await client.post(path(book), json=decision(scope), headers=headers),
        await client.post(path(book), json=decision(scope), headers=version_headers(client, 1)),
    ]
    for result in responses:
        assert result.status_code == 503
        safe(result)
    assert database_snapshot(engine) == before


async def test_invalidation_failure_rolls_back_audit_idempotency_and_unchanged_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-retry-after-failure")
    before = database_snapshot(engine)
    real = invalidate_portfolio

    def fail(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        raise SQLAlchemyError("private-value")

    monkeypatch.setattr(tax, "invalidate_portfolio", fail)
    failed = await client.post(path(book), json=decision(scope), headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before
    monkeypatch.setattr(tax, "invalidate_portfolio", real)
    retry = await client.post(path(book), json=decision(scope), headers=headers)
    assert retry.status_code == 201


async def test_existing_receipt_cash_and_locked_fx_are_not_rewritten(
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
    result = await client.post(path(book), json=decision(scope), headers=version_headers(client, 1))
    assert result.status_code == 201
    after = database_snapshot(engine)
    only_tax(before, after)
    for name in (
        "dividend_entitlements",
        "dividend_receipts",
        "cash_ledger_entries",
        "dividend_event_revisions",
    ):
        assert after[name] == before[name]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
            ).scalar_one()
            == 7_800_000_000_000
        )


async def test_account_and_broker_versions_preserve_exact_int64_wire_digits(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    account_version = 9_007_199_254_740_993
    broker_version = 9_223_372_036_854_775_807
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET version=? WHERE id=?", (account_version, book["account"]["id"])
        )
        con.exec_driver_sql(
            "UPDATE brokers SET version=? WHERE id=?", (broker_version, book["broker"]["id"])
        )
    scope = await selection(client, book)
    assert scope["account_version"] == "9007199254740993"
    assert scope["broker_version"] == "9223372036854775807"
    result = await client.post(path(book), json=decision(scope), headers=version_headers(client, 1))
    assert result.status_code == 201, result.text
    assert result.json()["account_version"] == scope["account_version"]
    assert result.json()["broker_version"] == scope["broker_version"]
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT account_version,broker_version FROM dividend_tax_settings WHERE id=?",
            (result.json()["id"],),
        ).one()
        assert tuple(row) == (account_version, broker_version)


async def test_account_scope_is_rechecked_inside_the_writer(
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
    response = await client.post(
        path(book), json=decision(scope), headers=version_headers(client, 1)
    )
    assert response.status_code == 409 and response.json()["code"] == "DIVIDEND_TAX_SCOPE_CHANGED"
    safe(response)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_savepoint_release_failure_does_not_leave_an_idempotency_key(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-savepoint-release")
    before = database_snapshot(engine)
    with patch.object(
        Connection,
        "_release_savepoint_impl",
        side_effect=SQLAlchemyError("private-value synthetic release failure"),
    ):
        failed = await client.post(path(book), json=decision(scope), headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before
    retry = await client.post(path(book), json=decision(scope), headers=headers)
    assert retry.status_code == 201


async def test_response_conversion_failure_rolls_back_every_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-response-conversion")
    before = database_snapshot(engine)
    original = routes._public

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("private-value synthetic conversion failure")

    monkeypatch.setattr(routes, "_public", fail)
    failed = await client.post(path(book), json=decision(scope), headers=headers)
    assert failed.status_code == 503
    safe(failed)
    assert database_snapshot(engine) == before
    monkeypatch.setattr(routes, "_public", original)
    retry = await client.post(path(book), json=decision(scope), headers=headers)
    assert retry.status_code == 201


async def test_tax_api_does_not_read_provider_configuration_or_open_http(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with (
        patch.object(
            ProviderConfigurationStore, "load", side_effect=AssertionError("private file")
        ),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider HTTP")),
    ):
        scope = await selection(client, book)
        response = await client.post(
            path(book), json=decision(scope), headers=version_headers(client, 1)
        )
        assert response.status_code == 201
        assert (await selection(client, book))["selected"]["id"] == response.json()["id"]


@pytest.mark.parametrize("corruption", ["scope", "effective-day", "head-id", "status"])
async def test_selection_dto_rejects_inconsistent_claims(
    client: httpx.AsyncClient, book: dict[str, Any], corruption: str
) -> None:
    scope = await selection(client, book)
    result = await client.post(path(book), json=decision(scope), headers=version_headers(client, 1))
    assert result.status_code == 201
    current = await selection(client, book)
    if corruption == "scope":
        current["selected_scope"] = "ACCOUNT_SCOPE_CHANGED"
    elif corruption == "effective-day":
        current["effective_on"] = "2025-12-31"
    elif corruption == "head-id":
        current["head_id"] = str(uuid4())
    else:
        current["tax_status"] = "UNKNOWN"
    with pytest.raises(ValidationError):
        models.TaxSettingSelection.model_validate(current)


async def test_executable_contract_uses_string_versions_and_security_headers(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    path_template = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings"
    post = schema["paths"][path_template]["post"]
    headers = {item["name"]: item for item in post["parameters"] if item["in"] == "header"}
    assert all(
        headers[name]["required"] for name in ("If-Match", "X-CSRF-Token", "Idempotency-Key")
    )
    assert post["security"] == [{"cookieAuth": []}]
    write_schema = schema["components"]["schemas"]["TaxSettingWrite"]
    for name in ("account_version", "broker_version", "expected_head_no"):
        assert write_schema["properties"][name]["type"] == "string"
        assert name in write_schema["required"]
    record_schema = schema["components"]["schemas"]["TaxSettingRecord"]
    for name in ("account_version", "broker_version", "setting_no", "scope", "current_use_scope"):
        assert name in record_schema["required"]
    assert record_schema["properties"]["setting_no"]["type"] == "string"


async def test_exact_physical_revision_and_observed_head_id_are_required(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    empty = await selection(client, book)
    before = database_snapshot(engine)
    wrong_revision = {**decision(empty), "revision_id": str(uuid4())}
    result = await client.post(path(book), json=wrong_revision, headers=version_headers(client, 1))
    assert result.status_code == 412
    assert database_snapshot(engine) == before
    first = await client.post(path(book), json=decision(empty), headers=version_headers(client, 1))
    assert first.status_code == 201
    current = await selection(client, book)
    before = database_snapshot(engine)
    wrong_head = {**decision(current), "expected_head_id": str(uuid4())}
    stale = await client.post(path(book), json=wrong_head, headers=version_headers(client, 1))
    assert stale.status_code == 412
    safe(stale)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("missing_id", [True, False])
async def test_head_number_and_identity_must_be_explicitly_consistent(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], missing_id: bool
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    body = {
        **decision(scope),
        "expected_head_no": "1" if missing_id else "0",
        "expected_head_id": None if missing_id else str(uuid4()),
    }
    result = await client.post(path(book), json=body, headers=version_headers(client, 1))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("head", ["9223372036854775808", "9999999999999999999"])
async def test_head_above_int64_is_rejected_without_rounding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], head: str
) -> None:
    scope = await selection(client, book)
    before = database_snapshot(engine)
    body = {**decision(scope), "expected_head_no": head, "expected_head_id": str(uuid4())}
    result = await client.post(path(book), json=body, headers=version_headers(client, 1))
    assert result.status_code == 422
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("cursor_value", ["0", "01", "1e2", "1\n", "9223372036854775808"])
async def test_history_cursor_rejects_noncanonical_or_out_of_range_int64(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], cursor_value: str
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps([cursor_value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    response = await client.get(
        path(book) + "/history",
        params={"revision_id": str(book["event"].revision_id), "revision_no": 1, "cursor": cursor},
    )
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["head", "status", "scope"])
async def test_empty_selection_dto_requires_consistent_head_and_missing_state(
    client: httpx.AsyncClient, book: dict[str, Any], corruption: str
) -> None:
    empty = await selection(client, book)
    if corruption == "head":
        empty["head_no"] = "1"
    elif corruption == "status":
        empty["tax_status"] = "UNKNOWN"
    else:
        empty["selected_scope"] = "ACCOUNT_SCOPE_MATCH"
    with pytest.raises(ValidationError):
        models.TaxSettingSelection.model_validate(empty)


async def test_read_conversion_failures_are_private_and_never_repair_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = await selection(client, book)
    response = await client.post(
        path(book), json=decision(scope), headers=version_headers(client, 1)
    )
    assert response.status_code == 201
    before = database_snapshot(engine)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("private-value synthetic conversion failure")

    monkeypatch.setattr(routes, "_public", fail)
    current = await client.get(path(book), params={"effective_on": "2026-01-08"})
    history = await client.get(
        path(book) + "/history",
        params={"revision_id": str(book["event"].revision_id), "revision_no": 1},
    )
    for result in (current, history):
        assert result.status_code == 503
        safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("code", "status", "public_code"),
    [
        ("DIVIDEND_TAX_OWNER_UNAVAILABLE", 401, "UNAUTHORIZED"),
        ("INVALID_DIVIDEND_TAX_SCOPE", 422, "INVALID_DIVIDEND_TAX_SCOPE"),
        ("DIVIDEND_TAX_VERSION_CONFLICT", 412, "PRECONDITION_FAILED"),
        ("DIVIDEND_EVENT_VERSION_CONFLICT", 412, "PRECONDITION_FAILED"),
        ("unexpected-private-value", 503, "DIVIDEND_TAX_HISTORY_UNAVAILABLE"),
    ],
)
async def test_internal_failures_map_to_fixed_private_errors(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
    public_code: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise tax.TaxSettingError(code)

    monkeypatch.setattr(routes, "_event", fail)
    before = database_snapshot(engine)
    result = await client.get(path(book), params={"effective_on": "2026-01-08"})
    assert result.status_code == status and result.json()["code"] == public_code
    assert "unexpected-private-value" not in result.text
    safe(result)
    assert database_snapshot(engine) == before


async def test_replay_requires_original_setting_in_complete_retained_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = await selection(client, book)
    headers = version_headers(client, 1, key="synthetic-tax-incomplete-replay")
    first = await client.post(path(book), json=decision(scope), headers=headers)
    assert first.status_code == 201
    before = database_snapshot(engine)
    monkeypatch.setattr(tax, "history", lambda *args, **kwargs: ())
    result = await client.post(path(book), json=decision(scope), headers=headers)
    assert result.status_code == 503
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("header", ['"0"', '"01"', "1", '"1\n"', '"abc"'])
async def test_internal_event_version_guard_rejects_noncanonical_headers(header: str) -> None:
    with pytest.raises(ApiError) as error:
        matching_dividend_event_version(header)
    assert error.value.status == 422 and error.value.code == "INVALID_EVENT_VERSION"
