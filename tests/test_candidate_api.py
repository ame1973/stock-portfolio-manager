"""Protected candidate review decisions do not adopt events or create receipts."""

import asyncio
import base64
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.candidate_routes as routes
import spm_api.dividend_candidate_service as service
from spm_api.audit import stamp
from spm_api.candidate_models import CandidateRecord
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import create, database_snapshot, version_headers, write_headers
from tests.test_basis_api import basis_book as basis_book
from tests.test_basis_api import payload as basis_payload
from tests.test_provider_persistence import book as book
from tests.test_provider_persistence import fetched
from tests.test_provider_persistence import save as capture_source

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/candidates"
REASON = "Explicit synthetic association for this possible distribution."


@pytest.fixture
async def candidate_book(basis_book: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/dividends/basis-assertions",
        json=basis_payload(basis_book),
        headers=write_headers(client),
    )
    assert response.status_code == 201
    return {**basis_book, "basis": response.json()}


def payload(book: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "OPEN",
        "terms": {
            "security_id": book["security"]["id"],
            "ex_date": "2026-01-08",
            "event_type": "UNKNOWN",
            "dps": "0.9",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
        },
        "matches": [
            {
                "observation_id": book["observation_id"],
                "source_sha256": book["original"].normalized_sha256,
                "basis_assertion_id": book["basis"]["id"],
                "reason": REASON,
            }
        ],
        "change_reason": REASON,
    }


def only_candidates(before: Any, after: Any) -> None:
    allowed = {"dividend_candidate_revisions", "audit_events", "idempotency_keys"}
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store" and response.headers["x-request-id"]
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
    else:
        assert '"request_id":' not in response.text
    for item in ("owner_id", "matches_json", "matches_sha256", "assertion_json", "private=fixture"):
        assert item not in response.text


async def test_protected_create_binds_ids_exact_evidence_audit_and_no_financial_facts(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(candidate_book), headers=write_headers(client))
    assert response.status_code == 201
    safe(response)
    record = response.json()
    assert UUID(record["id"]).version == UUID(record["revision_id"]).version == 4
    assert record["id"] != record["revision_id"]
    assert response.headers["location"] == f"{PATH}/{record['id']}"
    assert response.headers["etag"] == '"1"' and record["version"] == 1
    assert record["terms"]["dps"] == "0.90000000" and record["terms"]["currency"] == "USD"
    assert record["assessment"]["status"] == "PROVISIONAL"
    assert record["assessment"]["independent_origins"] == ["EODHD"]
    assert record["assessment"]["checks"][0]["comparison_dps"] == "0.90000000"
    assert record["scope"] == "CANDIDATE_REVIEW_ONLY"
    assert record["canonical_adoption"] == "NOT_ADOPTED"
    assert record["current_use_scope"] == "REVALIDATION_REQUIRED"
    only_candidates(before, database_snapshot(engine))
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT owner_id,entity_id,request_id FROM audit_events "
            "WHERE action='DIVIDEND_CANDIDATE_REVISED'"
        ).one()
        assert tuple(row) == (
            candidate_book["owner"]["id"],
            record["revision_id"],
            response.headers["x-request-id"],
        )
    before = database_snapshot(engine)
    read = await client.get(response.headers["location"])
    assert read.status_code == 200 and read.json() == record and read.headers["etag"] == '"1"'
    listing = await client.get(PATH)
    assert listing.status_code == 200 and listing.json()["items"] == [record]
    safe(read)
    safe(listing)
    assert database_snapshot(engine) == before


async def test_revision_cas_history_and_completed_replay_survive_action_correction(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    create_headers = write_headers(client, key="uncertain-candidate-create-intent")
    original = await client.post(PATH, json=body, headers=create_headers)
    assert original.status_code == 201
    path = original.headers["location"]
    revised_body = {**body, "terms": {**body["terms"], "pay_date": "2026-02-01"}}
    revision_headers = version_headers(client, 1, key="uncertain-candidate-revision-intent")
    revised = await client.patch(path, json=revised_body, headers=revision_headers)
    assert revised.status_code == 200 and revised.json()["version"] == 2
    assert revised.headers["etag"] == '"2"'
    stale = await client.patch(path, json=body, headers=version_headers(client, 1))
    assert stale.status_code == 412
    history = await client.get(path + "/revisions")
    assert history.status_code == 200
    assert [row["version"] for row in history.json()["items"]] == [2, 1]
    corrected = await client.patch(
        f"/api/v1/corporate-actions/{candidate_book['action']['id']}",
        headers=version_headers(client, 1),
        json={**candidate_book["action_body"], "ratio_numerator": "2", "ratio_denominator": "1"},
    )
    assert corrected.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=create_headers)
    assert replay.status_code == 201 and replay.json() == original.json()
    replay_revision = await client.patch(path, json=revised_body, headers=revision_headers)
    assert replay_revision.status_code == 200 and replay_revision.json() == revised.json()
    assert replay_revision.headers["etag"] == '"2"'
    fresh = await client.patch(path, json=revised_body, headers=version_headers(client, 2))
    assert fresh.status_code == 409 and fresh.json()["code"] == "CANDIDATE_BASIS_CHANGED"
    assert database_snapshot(engine) == before
    dismiss = {**body, "state": "DISMISSED", "matches": []}
    dismissed = await client.patch(path, json=dismiss, headers=version_headers(client, 2))
    assert dismissed.status_code == 200 and dismissed.json()["version"] == 3
    assert dismissed.json()["state"] == "DISMISSED"
    historical = await client.get(path + "/revisions")
    assert [row["version"] for row in historical.json()["items"]] == [3, 2, 1]


async def test_no_owner_cannot_read_or_create_candidate(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    for path in (PATH, f"{PATH}/{uuid4()}", f"{PATH}/{uuid4()}/revisions"):
        response = await client.get(path)
        assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_security_dependencies_reject_before_candidate_decision(
    candidate_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unauthorized requests cannot create a candidate decision")

    monkeypatch.setattr(routes, "_decision", forbidden)
    headers = write_headers(client)
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "invalid-synthetic-csrf"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        headers["Idempotency-Key"] = "short"
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(candidate_book), headers=headers)
    assert response.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "expired", "csrf"])
async def test_current_auth_is_rechecked_inside_writer_before_new_or_cached_response(
    candidate_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    headers, body = write_headers(client), payload(candidate_book)
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


async def test_concurrent_retries_and_competing_versions_are_atomic(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body, headers = payload(candidate_book), write_headers(client)
    first, replay = await asyncio.gather(
        *(client.post(PATH, json=body, headers=headers) for _ in range(2))
    )
    assert first.status_code == replay.status_code == 201 and first.json() == replay.json()
    path = first.headers["location"]
    one, other = await asyncio.gather(
        client.patch(path, json=body, headers=version_headers(client, 1)),
        client.patch(path, json=body, headers=version_headers(client, 1)),
    )
    assert sorted([one.status_code, other.status_code]) == [200, 412]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_candidate_revisions").scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='DIVIDEND_CANDIDATE_REVISED'"
            ).scalar_one()
            == 2
        )


async def test_idempotency_rejects_changed_body_and_observed_version(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body, headers = payload(candidate_book), write_headers(client)
    created = await client.post(PATH, json=body, headers=headers)
    assert created.status_code == 201
    before = database_snapshot(engine)
    changed = await client.post(PATH, json={**body, "state": "DISMISSED"}, headers=headers)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before
    path, revision_headers = created.headers["location"], version_headers(client, 1)
    assert (await client.patch(path, json=body, headers=revision_headers)).status_code == 200
    before = database_snapshot(engine)
    revision_headers["If-Match"] = '"2"'
    conflict = await client.patch(path, json=body, headers=revision_headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["audit", "serialization", "cached"])
async def test_late_failures_and_invalid_cached_json_roll_back_or_fail_closed(
    candidate_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    headers, body = write_headers(client), payload(candidate_book)
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
            monkeypatch.setattr(CandidateRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=headers)
    assert (
        response.status_code == 503 and response.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
    )
    assert "private-invalid-marker" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "tamper",
    ["amount", "assessment", "missing-history", "different-intent", "private-extra", "scope"],
)
async def test_valid_shaped_cached_receipt_is_fenced_against_audited_original(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, tamper: str
) -> None:
    headers, body = write_headers(client), payload(candidate_book)
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    altered = original.json()
    if tamper == "amount":
        altered["terms"]["dps"] = "0.90000001"
    elif tamper == "assessment":
        altered["assessment"]["status"] = "CORROBORATED"
    elif tamper == "missing-history":
        altered["id"] = str(uuid4())
        altered["revision_id"] = str(uuid4())
    elif tamper == "private-extra":
        altered["private"] = "private-invalid-marker"
    elif tamper == "scope":
        altered["canonical_adoption_scope"] = "LINKAGE_CHECKED"
    else:
        different = await client.post(
            PATH, json={**body, "state": "DISMISSED"}, headers=write_headers(client)
        )
        assert different.status_code == 201
        altered = different.json()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(altered), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    result = await client.post(PATH, json=body, headers=headers)
    assert result.status_code == 503 and result.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
    assert "private-invalid-marker" not in result.text
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        "id",
        "revision_id",
        "version",
        "owner_id",
        "canonical_adoption",
        "canonical_adoption_scope",
        "state",
        "reason",
        "extra-match",
        "duplicate-match",
        "bad-hash",
        "bad-id",
        "too-many",
        "amount-number",
        "amount-precision",
        "amount-overflow",
        "currency",
        "date",
        "security",
    ],
)
async def test_untrusted_or_invalid_input_rejects_without_echo_or_writes(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, change: str
) -> None:
    body = payload(candidate_book)
    if change in {
        "id",
        "revision_id",
        "version",
        "owner_id",
        "canonical_adoption",
        "canonical_adoption_scope",
    }:
        body[change] = "private-untrusted-value"
    elif change == "state":
        body["state"] = "VERIFIED"
    elif change == "reason":
        body["change_reason"] = "private-untrusted\0reason"
    elif change == "extra-match":
        body["matches"][0]["owner_id"] = "private-untrusted-owner"
    elif change == "duplicate-match":
        body["matches"].append(dict(body["matches"][0]))
    elif change == "bad-hash":
        body["matches"][0]["source_sha256"] = "0" * 63
    elif change == "bad-id":
        body["matches"][0]["observation_id"] = "private-untrusted-id"
    elif change == "too-many":
        body["matches"] = [
            {**body["matches"][0], "observation_id": str(uuid4())} for _ in range(101)
        ]
    elif change == "amount-number":
        body["terms"]["dps"] = 0.9
    elif change == "amount-precision":
        body["terms"]["dps"] = "0.123456789"
    elif change == "amount-overflow":
        body["terms"]["dps"] = "92233720368.54775808"
    elif change == "currency":
        body["terms"]["currency"] = "EUR"
    elif change == "date":
        body["terms"]["ex_date"] = "2026-02-30"
    else:
        body["terms"]["security_id"] = "private-untrusted-security"
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 422
    assert "private-untrusted" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "header", [None, "1", '"0"', '"01"', '"2147483648"', '"' + "9" * 4500 + '"']
)
async def test_candidate_if_match_is_explicit_and_bounded_before_integer_conversion(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, header: str | None
) -> None:
    body = payload(candidate_book)
    created = await client.post(PATH, json=body, headers=write_headers(client))
    assert created.status_code == 201
    headers = write_headers(client)
    if header is not None:
        headers["If-Match"] = header
    before = database_snapshot(engine)
    response = await client.patch(created.headers["location"], json=body, headers=headers)
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "failure", ["missing-source", "wrong-hash", "missing-basis", "missing-security"]
)
async def test_new_http_decisions_pin_real_source_and_owned_basis(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
    body = payload(candidate_book)
    if failure == "missing-source":
        body["matches"][0]["observation_id"] = str(uuid4())
    elif failure == "wrong-hash":
        body["matches"][0]["source_sha256"] = "0" * 64
    elif failure == "missing-basis":
        body["matches"][0]["basis_assertion_id"] = str(uuid4())
    else:
        body["terms"]["security_id"] = str(uuid4())
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 409
    assert (
        response.json()["code"]
        == {
            "missing-source": "CANDIDATE_SOURCE_UNAVAILABLE",
            "wrong-hash": "CANDIDATE_SOURCE_CHANGED",
            "missing-basis": "CANDIDATE_BASIS_UNAVAILABLE",
            "missing-security": "CANDIDATE_SECURITY_UNAVAILABLE",
        }[failure]
    )
    safe(response)
    assert database_snapshot(engine) == before


async def test_owned_candidate_reads_and_updates_do_not_adopt_other_owners_history(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    original = await client.post(PATH, json=body, headers=write_headers(client))
    assert original.status_code == 201
    path, owner = original.headers["location"], str(uuid4())
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
    for url in (path, path + "/revisions"):
        response = await client.get(url)
        assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
        safe(response)
    revised = await client.patch(path, json=body, headers=version_headers(client, 1))
    assert revised.status_code == 404 and revised.json()["code"] == "NOT_FOUND"
    listed = await client.get(PATH)
    assert listed.status_code == 200 and listed.json()["items"] == []
    assert database_snapshot(engine) == before


async def test_distinct_same_day_candidates_and_filters_do_not_infer_merges(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    first = await client.post(PATH, json=body, headers=write_headers(client))
    second = await client.post(
        PATH,
        json={**body, "terms": {**body["terms"], "event_type": "SPECIAL"}},
        headers=write_headers(client),
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["terms"]["ex_date"] == second.json()["terms"]["ex_date"]
    dismissed = await client.patch(
        first.headers["location"],
        json={**body, "state": "DISMISSED"},
        headers=version_headers(client, 1),
    )
    assert dismissed.status_code == 200
    before = database_snapshot(engine)
    opened = await client.get(PATH, params={"state": "OPEN", "q": "0005"})
    closed = await client.get(
        PATH, params={"state": "DISMISSED", "security_id": body["terms"]["security_id"]}
    )
    assert [row["id"] for row in opened.json()["items"]] == [second.json()["id"]]
    assert [row["id"] for row in closed.json()["items"]] == [first.json()["id"]]
    for query in ("%", "_"):
        assert (await client.get(PATH, params={"q": query})).json()["items"] == []
    assert database_snapshot(engine) == before


async def test_time_keyset_uses_actual_latest_physical_revision_ids(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    records = []
    for _ in range(27):
        response = await client.post(PATH, json=body, headers=write_headers(client))
        assert response.status_code == 201
        records.append(response.json())
    expected = sorted(
        records, key=lambda row: (row["created_at"], row["revision_id"]), reverse=True
    )
    before = database_snapshot(engine)
    params = {"limit": 25, "security_id": body["terms"]["security_id"], "q": "0005"}
    first = await client.get(PATH, params=params)
    assert first.status_code == 200 and first.json()["items"] == expected[:25]
    cursor = first.json()["next_cursor"]
    assert json.loads(base64.urlsafe_b64decode(cursor)) == [
        expected[24]["created_at"],
        expected[24]["revision_id"],
    ]
    second = await client.get(PATH, params={**params, "cursor": cursor})
    assert second.status_code == 200 and second.json()["items"] == expected[25:]
    assert second.json()["next_cursor"] is None and database_snapshot(engine) == before


async def test_revision_keyset_keeps_physical_history_and_original_etags(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    created = await client.post(PATH, json=body, headers=write_headers(client))
    assert created.status_code == 201
    path, records = created.headers["location"], [created.json()]
    for version in range(1, 27):
        response = await client.patch(
            path,
            json={**body, "change_reason": f"Explicit synthetic revision number {version + 1}."},
            headers=version_headers(client, version),
        )
        assert response.status_code == 200 and response.headers["etag"] == f'"{version + 1}"'
        records.append(response.json())
    expected = list(reversed(records))
    before = database_snapshot(engine)
    first = await client.get(path + "/revisions", params={"limit": 25})
    assert first.status_code == 200 and first.json()["items"] == expected[:25]
    cursor = first.json()["next_cursor"]
    assert json.loads(base64.urlsafe_b64decode(cursor)) == [
        str(expected[24]["version"]),
        expected[24]["revision_id"],
    ]
    second = await client.get(path + "/revisions", params={"limit": 25, "cursor": cursor})
    assert second.status_code == 200 and second.json()["items"] == expected[25:]
    assert second.json()["next_cursor"] is None and database_snapshot(engine) == before


@pytest.mark.parametrize(
    "kind,value",
    [
        ("time", "2026-01-01"),
        ("time", "2026-01-01T00:00:00+00:00"),
        ("time", "bad"),
        ("version", "0"),
        ("version", "01"),
        ("version", str(2**63)),
        ("version", "bad"),
    ],
)
async def test_invalid_resource_cursor_rejects_before_sql_binding(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str, value: str
) -> None:
    created = await client.post(PATH, json=payload(candidate_book), headers=write_headers(client))
    assert created.status_code == 201
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    url = PATH if kind == "time" else created.headers["location"] + "/revisions"
    before = database_snapshot(engine)
    response = await client.get(url, params={"cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["digest", "audit", "noncanonical-id"])
async def test_corrupt_candidate_history_fails_safely_without_echo_or_repair(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, corruption: str
) -> None:
    created = await client.post(PATH, json=payload(candidate_book), headers=write_headers(client))
    assert created.status_code == 201
    with unit_of_work(engine, write=True) as con:
        trigger = (
            "trg_audit_event_no_update"
            if corruption == "audit"
            else "trg_dividend_candidate_no_update"
        )
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name=?", (trigger,)
        ).scalar_one()
        con.exec_driver_sql(f"DROP TRIGGER {trigger}")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        if corruption == "audit":
            con.exec_driver_sql(
                "UPDATE audit_events SET reason='private-invalid-marker' "
                "WHERE action='DIVIDEND_CANDIDATE_REVISED'"
            )
        elif corruption == "digest":
            con.exec_driver_sql(
                "UPDATE dividend_candidate_revisions SET matches_sha256=?", ("0" * 64,)
            )
        else:
            con.exec_driver_sql(
                "UPDATE dividend_candidate_revisions SET candidate_id=?",
                ("{" + created.json()["id"] + "}",),
            )
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    urls = (
        (PATH,)
        if corruption == "noncanonical-id"
        else (PATH, created.headers["location"], created.headers["location"] + "/revisions")
    )
    for url in urls:
        response = await client.get(url)
        assert (
            response.status_code == 503
            and response.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
        )
        assert "private-invalid-marker" not in response.text
        safe(response)
    assert database_snapshot(engine) == before


async def test_reordering_explicit_matches_does_not_rotate_a_retry_intent(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(
        candidate_book,
        payload=[{"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "USD"}],
    )
    captured = capture_source(engine, candidate_book, page)
    body, headers = payload(candidate_book), write_headers(client)
    body["matches"].append(
        {
            "observation_id": captured.observation_ids[0],
            "source_sha256": page.rows[0].normalized_sha256,
            "basis_assertion_id": None,
            "reason": REASON,
        }
    )
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201 and len(original.json()["matches"]) == 2
    before = database_snapshot(engine)
    replay = await client.post(
        PATH, json={**body, "matches": list(reversed(body["matches"]))}, headers=headers
    )
    assert replay.status_code == 201 and replay.json() == original.json()
    assert database_snapshot(engine) == before


async def test_oversized_utf8_associations_reject_before_source_reads(
    candidate_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Oversized association lists cannot read source assertions")

    monkeypatch.setattr(service, "original_dividend", forbidden)
    body = payload(candidate_book)
    body["matches"] = [
        {**body["matches"][0], "observation_id": str(uuid4()), "reason": "中" * 1000}
        for _ in range(44)
    ]
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CANDIDATE_DECISION"
    safe(response)
    assert database_snapshot(engine) == before


async def test_candidate_routes_do_not_load_provider_files_or_construct_http_clients(
    candidate_book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Candidate review cannot access private provider files or upstream")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderConfigurationStore, "unchanged", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    body, headers = payload(candidate_book), write_headers(client)
    created = await client.post(PATH, json=body, headers=headers)
    assert created.status_code == 201
    before = database_snapshot(engine)
    assert (await client.post(PATH, json=body, headers=headers)).json() == created.json()
    assert (await client.get(created.headers["location"])).json() == created.json()
    assert (await client.get(PATH)).json()["items"] == [created.json()]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("tamper", ["wrong-version", "different-candidate"])
async def test_cached_patch_is_bound_to_requested_candidate_and_observed_version(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, tamper: str
) -> None:
    body = payload(candidate_book)
    created = await client.post(PATH, json=body, headers=write_headers(client))
    headers = version_headers(client, 1)
    changed = await client.patch(created.headers["location"], json=body, headers=headers)
    assert changed.status_code == 200
    altered = changed.json()
    if tamper == "wrong-version":
        altered["version"] = 1
    else:
        other = await client.post(PATH, json=body, headers=write_headers(client))
        other_revision = await client.patch(
            other.headers["location"], json=body, headers=version_headers(client, 1)
        )
        assert other_revision.status_code == 200
        altered = other_revision.json()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(altered), headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.patch(created.headers["location"], json=body, headers=headers)
    assert replay.status_code == 503 and replay.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
    safe(replay)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_select_another_owners_basis_declaration(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body, owner = payload(candidate_book), str(uuid4())
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
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 409 and response.json()["code"] == "CANDIDATE_BASIS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_corrupt_latest_owner_never_revives_an_older_candidate_version(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    create_headers, revision_headers = write_headers(client), version_headers(client, 1)
    first = await client.post(PATH, json=body, headers=create_headers)
    assert first.status_code == 201
    path = first.headers["location"]
    second = await client.patch(path, json=body, headers=revision_headers)
    assert second.status_code == 200 and second.json()["version"] == 2
    with unit_of_work(engine, write=True) as con:
        owner = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (owner, "synthetic-owner", stamp(), stamp()),
        )
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_dividend_candidate_no_update'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_dividend_candidate_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_candidate_revisions SET owner_id=? WHERE id=?",
            (owner, second.json()["revision_id"]),
        )
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    for url in (PATH, path, path + "/revisions"):
        result = await client.get(url)
        assert (
            result.status_code == 503 and result.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
        )
        safe(result)
    stale = await client.patch(path, json=body, headers=version_headers(client, 1))
    assert stale.status_code == 503 and stale.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
    for response in (
        await client.post(PATH, json=body, headers=create_headers),
        await client.patch(path, json=body, headers=revision_headers),
    ):
        assert (
            response.status_code == 503
            and response.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
        )
    with (
        pytest.raises(service.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"),
        unit_of_work(engine, write=True) as con,
    ):
        service.require_current(
            con, UUID(candidate_book["owner"]["id"]), UUID(first.json()["id"]), version=1
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corrupt_version", [1, 2])
async def test_corrupt_candidate_audit_anywhere_blocks_http_reads_and_replays(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, corrupt_version: int
) -> None:
    body = payload(candidate_book)
    create_headers, revision_headers = write_headers(client), version_headers(client, 1)
    first = await client.post(PATH, json=body, headers=create_headers)
    assert first.status_code == 201
    path = first.headers["location"]
    second = await client.patch(path, json=body, headers=revision_headers)
    assert second.status_code == 200
    damaged = first if corrupt_version == 1 else second
    with unit_of_work(engine, write=True) as con:
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_audit_event_no_update'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? WHERE entity_id=? "
            "AND action='DIVIDEND_CANDIDATE_REVISED'",
            ("a" * 64, damaged.json()["revision_id"]),
        )
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    responses = [await client.get(url) for url in (PATH, path, path + "/revisions")]
    responses.extend(
        [
            await client.post(PATH, json=body, headers=create_headers),
            await client.patch(path, json=body, headers=revision_headers),
            await client.patch(path, json=body, headers=version_headers(client, 2)),
        ]
    )
    for response in responses:
        assert response.status_code == 503
        assert response.json()["code"] == "CANDIDATE_HISTORY_UNAVAILABLE"
        safe(response)
    assert database_snapshot(engine) == before


async def test_exact_maximum_zero_and_missing_terms_remain_separate_without_evidence(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    body["matches"] = []
    body["terms"].update(
        dps="92233720368.54775807", currency=None, ex_date=None, adjustment_basis="UNKNOWN"
    )
    maximum = await client.post(PATH, json=body, headers=write_headers(client))
    assert maximum.status_code == 201
    result = maximum.json()
    assert result["terms"]["dps"] == "92233720368.54775807"
    assert result["terms"]["currency"] is None and result["terms"]["ex_date"] is None
    assert result["assessment"]["status"] == "INCOMPLETE"
    assert {
        "MISSING_CURRENCY",
        "MISSING_EX_DATE",
        "UNRESOLVED_ADJUSTMENT",
        "NO_MATCHED_EVIDENCE",
    } <= set(result["assessment"]["incomplete_reasons"])
    zero = await client.post(
        PATH, json={**body, "terms": {**body["terms"], "dps": "0"}}, headers=write_headers(client)
    )
    missing = await client.post(
        PATH, json={**body, "terms": {**body["terms"], "dps": None}}, headers=write_headers(client)
    )
    assert zero.status_code == missing.status_code == 201
    assert zero.json()["terms"]["dps"] == "0.00000000" and missing.json()["terms"]["dps"] is None
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_events").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0


async def test_revision_cannot_move_a_candidate_to_another_real_security(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    first = await client.post(PATH, json=body, headers=write_headers(client))
    assert first.status_code == 201
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHER",
            "name": "Other synthetic security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    before = database_snapshot(engine)
    response = await client.patch(
        first.headers["location"],
        json={**body, "terms": {**body["terms"], "security_id": security["id"]}},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 409 and response.json()["code"] == "CANDIDATE_VERSION_CONFLICT"
    safe(response)
    assert database_snapshot(engine) == before


async def test_missing_filter_target_and_unsupported_history_mutations_are_not_success(
    candidate_book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    body = payload(candidate_book)
    original = await client.post(PATH, json=body, headers=write_headers(client))
    assert original.status_code == 201
    before = database_snapshot(engine)
    missing = await client.get(PATH, params={"security_id": str(uuid4())})
    assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"
    for method in ("put", "delete"):
        response = await client.request(
            method, original.headers["location"], headers=write_headers(client), json={}
        )
        assert response.status_code == 405 and response.headers["cache-control"] == "no-store"
    response = await client.patch(
        original.headers["location"] + "/revisions", headers=version_headers(client, 1), json=body
    )
    assert response.status_code == 405 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
