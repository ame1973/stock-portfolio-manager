"""Protected basis declarations are owned, immutable and never canonical financial adoption."""

import asyncio
import base64
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.basis_routes as routes
import spm_api.dividend_basis_service as service
from spm_api.audit import stamp
from spm_api.basis_models import DividendBasisRecord
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.symbols import resolve_symbol
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import create, database_snapshot, version_headers, write_headers
from tests.test_provider_persistence import book as book
from tests.test_provider_persistence import fetched
from tests.test_provider_persistence import save as capture_source

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/basis-assertions"
REASON = "Explicit synthetic declared split-only source scope, not verified market history."


@pytest.fixture
async def basis_book(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> dict[str, Any]:
    page = await fetched(book, payload=[{"date": "2026-01-08", "value": "0.6", "currency": "USD"}])
    captured = capture_source(engine, book, page)
    action_body = {
        "security_id": book["security"]["id"],
        "action_type": "SPLIT",
        "effective_date": "2026-01-08",
        "ratio_numerator": "3",
        "ratio_denominator": "2",
        "status": "CONFIRMED",
        "reason": REASON,
    }
    action = await create(client, "corporate-actions", action_body)
    return {
        **book,
        "original": page.rows[0],
        "observation_id": captured.observation_ids[0],
        "action": action,
        "action_body": action_body,
    }


def payload(book: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": book["observation_id"],
        "source_sha256": book["original"].normalized_sha256,
        "share_basis_date": "2026-01-10",
        "source_basis": "AFTER_EFFECTIVE_DATE_ACTIONS",
        "target_basis": "PRE_EX_DATE_EVENTS",
        "complete": True,
        "split_only": True,
        "actions": [{"action_id": book["action"]["id"], "version": book["action"]["version"]}],
        "citation": {
            "citation": "Synthetic basis declaration page 2",
            "reason": REASON,
            "url": "https://example.com/synthetic-basis.pdf?private=fixture#scope",
        },
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store" and response.headers["x-request-id"]
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
    else:
        assert 'request_id":' not in response.text
    for value in (
        "owner_id",
        "assertion_json",
        "assertion_sha256",
        "credential",
        "private=fixture",
    ):
        assert value not in response.text


def only_declarations(before: Any, after: Any) -> None:
    allowed = {"dividend_basis_assertions", "audit_events", "idempotency_keys"}
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }


async def test_protected_create_binds_server_ids_audit_exact_history_and_location(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    response = await client.post(
        PATH,
        json=payload(basis_book),
        headers={**write_headers(client), "X-Request-ID": "untrusted-caller-id"},
    )
    assert response.status_code == 201
    safe(response)
    result = response.json()
    assert UUID(result["id"]).version == 4
    assert (
        response.headers["location"] == f"{PATH}/{result['id']}" and "etag" not in response.headers
    )
    assert result["observation_id"] == basis_book["observation_id"]
    assert result["source_sha256"] == basis_book["original"].normalized_sha256
    assert (
        result["scope"] == "BASIS_DECLARATION_ONLY"
        and result["canonical_adoption"] == "NOT_ADOPTED"
    )
    assert result["current_use_scope"] == "REVALIDATION_REQUIRED_FOR_NEW_ADOPTION"
    assert result["market_history"] == "OWNER_ASSERTED_NOT_VERIFIED"
    assert result["citation"]["id"] == result["id"]
    assert result["citation"]["url"] is None and result["citation"]["url_withheld"]
    snapshot = result["actions"][0]
    assert snapshot["action_id"] == basis_book["action"]["id"]
    assert snapshot["observed_version"] == basis_book["action"]["version"]
    assert snapshot["ratio_numerator"] == "3" and snapshot["ratio_denominator"] == "2"
    assert (
        snapshot["observed_status"] == "CONFIRMED"
        and snapshot["observed_lifecycle_status"] == "CURRENT"
    )
    only_declarations(before, database_snapshot(engine))
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT owner_id,entity_id,request_id FROM audit_events "
            "WHERE action='DIVIDEND_BASIS_ASSERTED'"
        ).one()
        assert tuple(row) == (
            basis_book["owner"]["id"],
            result["id"],
            response.headers["x-request-id"],
        )
        assert row[2] != "untrusted-caller-id"
    before_read = database_snapshot(engine)
    read = await client.get(response.headers["location"])
    assert read.status_code == 200 and read.json() == result
    listed = await client.get(PATH, params={"observation_id": basis_book["observation_id"]})
    assert listed.status_code == 200 and listed.json()["items"] == [result]
    safe(read)
    safe(listed)
    assert database_snapshot(engine) == before_read


async def test_original_http_replay_remains_reachable_after_action_revision(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    headers, body = write_headers(client, key="uncertain-basis-owner-intent"), payload(basis_book)
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    changed = await client.patch(
        f"/api/v1/corporate-actions/{basis_book['action']['id']}",
        headers=version_headers(client, 1),
        json={
            **basis_book["action_body"],
            "ratio_numerator": "2",
            "ratio_denominator": "1",
            "reason": REASON,
        },
    )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == original.json()
    assert replay.headers["location"] == original.headers["location"]
    historical = await client.get(original.headers["location"])
    assert historical.status_code == 200 and historical.json() == original.json()
    stale = await client.post(PATH, json=body, headers=write_headers(client))
    assert stale.status_code == 409 and stale.json()["code"] == "BASIS_ACTIONS_CHANGED"
    assert database_snapshot(engine) == before


async def test_unauthenticated_read_and_create_are_not_public_success(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    for path in (PATH, f"{PATH}/{uuid4()}"):
        response = await client.get(path)
        assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_security_dependencies_reject_without_preparing(
    basis_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unauthorized requests must not prepare a declaration")

    monkeypatch.setattr(routes, "_prepare", forbidden)
    headers = write_headers(client)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "incorrect-synthetic-csrf"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        headers["Idempotency-Key"] = "short"
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(basis_book), headers=headers)
    assert response.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "expired", "csrf"])
async def test_current_auth_is_rechecked_inside_writer_before_new_or_cached_result(
    basis_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    headers, body = write_headers(client), payload(basis_book)
    if replay:
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 201
    checkpoint: dict[str, Any] = {}

    def change_after_dependency(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            elif failure == "expired":
                con.exec_driver_sql("UPDATE sessions SET expires_at='2000-01-01T00:00:00.000000Z'")
            else:
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", change_after_dependency)
    response = await client.post(PATH, json=body, headers=headers)
    assert response.status_code == (403 if failure == "csrf" else 401)
    safe(response)
    assert database_snapshot(engine) == checkpoint["before"]


async def test_unchanged_concurrent_retries_share_one_receipt_and_changed_intent_conflicts(
    basis_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, body = write_headers(client), payload(basis_book)
    responses = await asyncio.gather(
        *(client.post(PATH, json=body, headers=headers) for _ in range(2))
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_basis_assertions").scalar_one() == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='DIVIDEND_BASIS_ASSERTED'"
            ).scalar_one()
            == 1
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("An existing or conflicting intent must not prepare")

    monkeypatch.setattr(routes, "_prepare", forbidden)
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == responses[0].json()
    changed = await client.post(
        PATH, json={**body, "share_basis_date": "2026-01-11"}, headers=headers
    )
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before


async def test_same_action_set_in_a_different_order_preserves_the_intent(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    second = await create(
        client,
        "corporate-actions",
        {
            **basis_book["action_body"],
            "effective_date": "2026-01-09",
            "ratio_numerator": "2",
            "ratio_denominator": "1",
        },
    )
    body, headers = payload(basis_book), write_headers(client)
    body["actions"].append({"action_id": second["id"], "version": second["version"]})
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    before = database_snapshot(engine)
    replay = await client.post(
        PATH, json={**body, "actions": list(reversed(body["actions"]))}, headers=headers
    )
    assert replay.status_code == 201 and replay.json() == original.json()
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["audit", "serialization", "cached"])
async def test_late_failure_and_corrupt_cached_result_never_leave_or_repair_writes(
    basis_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    headers, body = write_headers(client), payload(basis_book)
    if failure == "cached":
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 201
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                ('{"private-invalid-marker":"invalid"}', headers["Idempotency-Key"]),
            )
    else:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("private-invalid-marker")

        if failure == "audit":
            monkeypatch.setattr(service, "audit", fail)
        else:
            monkeypatch.setattr(DividendBasisRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "BASIS_ASSERTION_UNAVAILABLE"
    assert "private-invalid-marker" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "tamper", ["private-url", "ratio", "missing-history", "different-intent", "scope"]
)
async def test_well_formed_cached_receipt_is_fenced_against_audited_history(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, tamper: str
) -> None:
    headers, body = write_headers(client), payload(basis_book)
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    altered = original.json()
    if tamper == "private-url":
        altered["citation"]["url"] = "https://example.com/basis.pdf?private=fixture"
        altered["citation"]["url_withheld"] = False
    elif tamper == "ratio":
        altered["actions"][0]["ratio_numerator"] = "4"
    elif tamper == "missing-history":
        altered["id"] = str(uuid4())
        altered["citation"]["id"] = altered["id"]
    elif tamper == "scope":
        altered["canonical_adoption_scope"] = "LINKAGE_CHECKED"
    else:
        different = await client.post(
            PATH, json={**body, "share_basis_date": "2026-01-11"}, headers=write_headers(client)
        )
        assert different.status_code == 201
        altered = different.json()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(altered), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "BASIS_ASSERTION_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "private-untrusted-id"),
        ("owner_id", "private-untrusted-owner"),
        ("request_id", "private-untrusted-request"),
        ("security_id", "private-untrusted-security"),
        ("ex_date", "2026-01-08"),
        ("status", "CONFIRMED"),
        ("ratio_numerator", "3"),
        ("canonical_adoption", "VERIFIED"),
        ("canonical_adoption_scope", "LINKAGE_CHECKED"),
        ("complete", False),
        ("complete", 1),
        ("split_only", False),
        ("source_basis", "UNKNOWN"),
        ("target_basis", "UNKNOWN"),
        ("share_basis_date", "2026-02-30"),
        ("source_sha256", "0" * 63),
        ("actions", [{"action_id": "bad-uuid", "version": 1}]),
        ("actions", [{"action_id": str(uuid4()), "version": True}]),
        ("actions", [{"action_id": str(uuid4()), "version": 9223372036854775808}]),
    ],
)
async def test_untrusted_or_invalid_inputs_reject_without_echo_or_writes(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str, value: Any
) -> None:
    before = database_snapshot(engine)
    response = await client.post(
        PATH, json={**payload(basis_book), field: value}, headers=write_headers(client)
    )
    assert response.status_code == 422
    assert "private-untrusted" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


async def test_duplicate_pins_and_nested_forged_ratios_reject(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(basis_book)
    pin = body["actions"][0]
    before = database_snapshot(engine)
    for actions in ([pin, pin], [{**pin, "ratio_numerator": "4"}]):
        response = await client.post(
            PATH, json={**body, "actions": actions}, headers=write_headers(client)
        )
        assert response.status_code == 422
        safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["missing-source", "wrong-hash", "unknown-basis", "legacy"])
async def test_source_pins_must_identify_a_real_original_dated_adjusted_assertion(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
    body = payload(basis_book)
    if failure == "missing-source":
        body["observation_id"] = str(uuid4())
    elif failure == "wrong-hash":
        body["source_sha256"] = "0" * 64
    elif failure == "unknown-basis":
        page = await fetched(basis_book)
        captured = capture_source(engine, basis_book, page)
        body.update(
            observation_id=captured.observation_ids[0], source_sha256=page.rows[0].normalized_sha256
        )
    else:
        with unit_of_work(engine, write=True) as con:
            guard = con.exec_driver_sql(
                "SELECT sql FROM sqlite_schema WHERE name='trg_dividend_observation_no_update'"
            ).scalar_one()
            con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
            con.exec_driver_sql(
                "UPDATE dividend_observations SET provenance_version=0 WHERE id=?",
                (body["observation_id"],),
            )
            con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert (
        response.status_code
        == {"missing-source": 404, "wrong-hash": 409, "unknown-basis": 422, "legacy": 404}[failure]
    )
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["omitted", "missing", "version", "outside-window"])
async def test_action_scope_is_current_complete_and_within_the_explicit_interval(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
    body = payload(basis_book)
    if failure == "omitted":
        body["actions"] = []
    elif failure == "missing":
        body["actions"][0]["action_id"] = str(uuid4())
    elif failure == "version":
        body["actions"][0]["version"] += 1
    else:
        body["share_basis_date"] = "2026-01-07"
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == (422 if failure == "outside-window" else 409)
    safe(response)
    assert database_snapshot(engine) == before


async def test_history_is_owner_scoped_and_immutable(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await client.post(PATH, json=payload(basis_book), headers=write_headers(client))
    assert original.status_code == 201
    url = original.headers["location"]
    for method in ("put", "patch", "delete"):
        response = await client.request(method, url, headers=write_headers(client), json={})
        assert response.status_code == 405 and response.headers["cache-control"] == "no-store"
    owner = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (owner, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, owner, "synthetic-other-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    hidden = await client.get(url)
    missing = await client.get(f"{PATH}/{uuid4()}")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["code"] == missing.json()["code"] == "NOT_FOUND"
    listed = await client.get(PATH)
    assert listed.status_code == 200 and listed.json()["items"] == []
    assert database_snapshot(engine) == before


async def test_exact_int64_action_ratios_are_json_strings_and_original_terms_remain_unchanged(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "1234",
            "name": "Unheld synthetic security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "eodhd",
            "provider_symbol": "1234.HK",
            "effective_from": "2020-01-01",
        },
    )
    prepared = basis_book["prepared"]
    with unit_of_work(engine) as con:
        resolution = resolve_symbol(con, UUID(security["id"]), "eodhd", prepared.segment.window)
    local = {
        **basis_book,
        "prepared": prepare_fetch(
            prepared.owner_id, prepared.configuration, resolution.segments[0], "DIVIDENDS"
        ),
    }
    page = await fetched(local, payload=[{"date": "2026-01-08", "value": "0.6"}])
    receipt = capture_source(engine, local, page)
    action = await create(
        client,
        "corporate-actions",
        {
            **basis_book["action_body"],
            "security_id": security["id"],
            "ratio_numerator": "9223372036854775807",
            "ratio_denominator": "9223372036854775806",
        },
    )
    body = payload(basis_book)
    body.update(
        observation_id=receipt.observation_ids[0], source_sha256=page.rows[0].normalized_sha256
    )
    body["actions"] = [{"action_id": action["id"], "version": action["version"]}]
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 201
    terms = response.json()["actions"][0]
    assert terms["ratio_numerator"] == "9223372036854775807"
    assert terms["ratio_denominator"] == "9223372036854775806"
    only_declarations(before, database_snapshot(engine))
    assert (await client.get(response.headers["location"])).json() == response.json()


async def test_bounded_time_keyset_and_literal_filters_do_not_reorder_or_write(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    created = []
    for _ in range(27):
        response = await client.post(PATH, json=payload(basis_book), headers=write_headers(client))
        assert response.status_code == 201
        created.append(response.json())
    expected = sorted(created, key=lambda item: (item["created_at"], item["id"]), reverse=True)
    before = database_snapshot(engine)
    params = {
        "limit": 25,
        "security_id": basis_book["security"]["id"],
        "observation_id": basis_book["observation_id"],
        "q": "0005",
    }
    first = await client.get(PATH, params=params)
    assert first.status_code == 200 and first.json()["items"] == expected[:25]
    cursor = first.json()["next_cursor"]
    assert json.loads(base64.urlsafe_b64decode(cursor)) == [
        expected[24]["created_at"],
        expected[24]["id"],
    ]
    second = await client.get(PATH, params={**params, "cursor": cursor})
    assert second.status_code == 200 and second.json()["items"] == expected[25:]
    assert second.json()["next_cursor"] is None
    for query in ("%", "_"):
        response = await client.get(PATH, params={"q": query})
        assert response.status_code == 200 and response.json()["items"] == []
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["2026-01-10", "2026-01-10T00:00:00+00:00", "bad"])
async def test_invalid_time_cursor_rejects_before_query(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    response = await client.get(PATH, params={"cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["digest", "audit"])
async def test_corrupt_historical_declaration_fails_closed_without_echo_or_repair(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, corruption: str
) -> None:
    original = await client.post(PATH, json=payload(basis_book), headers=write_headers(client))
    assert original.status_code == 201
    with unit_of_work(engine, write=True) as con:
        trigger = (
            "trg_dividend_basis_no_update"
            if corruption == "digest"
            else "trg_audit_event_no_update"
        )
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name=?", (trigger,)
        ).scalar_one()
        con.exec_driver_sql(f"DROP TRIGGER {trigger}")
        if corruption == "digest":
            con.exec_driver_sql(
                "UPDATE dividend_basis_assertions SET assertion_sha256=?", ("0" * 64,)
            )
        else:
            con.exec_driver_sql(
                "UPDATE audit_events SET reason='private-invalid-marker' "
                "WHERE action='DIVIDEND_BASIS_ASSERTED'"
            )
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    for url in (original.headers["location"], PATH):
        response = await client.get(url)
        assert (
            response.status_code == 503 and response.json()["code"] == "BASIS_ASSERTION_UNAVAILABLE"
        )
        assert "private-invalid-marker" not in response.text
        safe(response)
    assert database_snapshot(engine) == before


async def test_missing_filter_targets_are_not_empty_or_repaired(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    for field in ("observation_id", "security_id"):
        response = await client.get(PATH, params={field: str(uuid4())})
        assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
        safe(response)
    assert database_snapshot(engine) == before


async def test_noncanonical_retained_identity_fails_list_without_normalizing_it(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await client.post(PATH, json=payload(basis_book), headers=write_headers(client))
    assert original.status_code == 201
    with unit_of_work(engine, write=True) as con:
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_dividend_basis_no_update'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_dividend_basis_no_update")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        con.exec_driver_sql(
            "UPDATE dividend_basis_assertions SET id=? WHERE id=?",
            ("{" + original.json()["id"] + "}", original.json()["id"]),
        )
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    response = await client.get(PATH)
    assert response.status_code == 503 and response.json()["code"] == "BASIS_ASSERTION_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_action_void_preserves_original_replay_but_new_empty_scope_is_explicit(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body, headers = payload(basis_book), write_headers(client)
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    voided = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{basis_book['action']['id']}",
        json={"reason": "Void synthetic split after basis statement"},
        headers=version_headers(client, 1),
    )
    assert voided.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == original.json()
    history = await client.get(original.headers["location"])
    assert history.status_code == 200 and history.json() == original.json()
    stale = await client.post(PATH, json=body, headers=write_headers(client))
    assert stale.status_code == 409 and stale.json()["code"] == "BASIS_ACTIONS_CHANGED"
    assert database_snapshot(engine) == before
    replacement = await client.post(
        PATH, json={**body, "actions": []}, headers=write_headers(client)
    )
    assert replacement.status_code == 201 and replacement.json()["actions"] == []
    assert replacement.json()["id"] != original.json()["id"]
    only_declarations(before, database_snapshot(engine))


async def test_unallocated_action_blocks_new_declaration_instead_of_being_ignored(
    basis_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    review = await create(
        client,
        "corporate-actions",
        {
            **basis_book["action_body"],
            "action_type": "MERGER",
            "effective_date": "2026-01-09",
            "ratio_numerator": None,
            "ratio_denominator": None,
            "status": "NEEDS_REVIEW",
        },
    )
    assert review["review_required"] is True
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(basis_book), headers=write_headers(client))
    assert response.status_code == 409 and response.json()["code"] == "BASIS_ACTIONS_CHANGED"
    assert database_snapshot(engine) == before
