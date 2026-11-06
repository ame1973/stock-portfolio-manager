"""Real HTTP source enqueue/read/cancel keeps private execution plans out of public DTOs."""

import asyncio
import base64
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.job_service as jobs
import spm_api.sync_service as service
from spm_api.audit import audit
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.job_models import SourceJobPlan
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.sync_models import JobCreated, SourceJob
from spm_worker.dispatcher import dispatch_once
from tests.helpers import database_snapshot, write_headers
from tests.test_provider_configuration import KEY, provision
from tests.test_provider_persistence import START
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


@pytest.fixture
async def source(
    book: dict[str, Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    settings.provider_secret_dir = book["settings"].provider_secret_dir
    monkeypatch.setattr(service, "now", lambda: START)
    return book


def body(source: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "security_ids": [source["security"]["id"]],
        "provider_ids": ["eodhd"],
        "from_date": "2026-01-01",
        "to_date": "2026-01-31",
        **changes,
    }


async def enqueue(
    client: httpx.AsyncClient, source: dict[str, Any], **changes: Any
) -> httpx.Response:
    result = await client.post(
        "/api/v1/dividends/sync", json=body(source, **changes), headers=write_headers(client)
    )
    assert result.status_code == 202, result.text
    return result


def assert_private_free(result: httpx.Response) -> None:
    assert result.headers["cache-control"] == "no-store"
    assert KEY.decode() not in result.text
    for private in (
        "profile_sha256",
        "profile_json",
        "request_json",
        "result_json",
        "alias_json",
        "credential_source",
        "credential_version",
        "lease_owner",
        "dedupe_key",
        "cursor_in",
    ):
        assert private not in result.text


def unchanged_finances(before: Any, after: Any) -> None:
    allowed = {
        "jobs",
        "job_items",
        "job_checkpoints",
        "audit_events",
        "idempotency_keys",
        "provider_fetch_contexts",
        "provider_runs",
        "provider_budgets",
        "upstream_attempts",
        "upstream_outcomes",
        "dividend_observations",
        "split_observations",
        "provider_observation_sightings",
    }
    assert {t: rows for t, rows in before.items() if t not in allowed} == {
        t: rows for t, rows in after.items() if t not in allowed
    }


async def test_202_queues_only_source_intent_and_links_one_audit_to_the_request(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("The API process tried a provider request")

    monkeypatch.setattr(ProviderHTTP, "get_json", no_http)
    before = database_snapshot(engine)
    result = await enqueue(client, source)
    data = result.json()
    assert data["scope"] == "SOURCE_ONLY" and data["canonical_reconciliation"] == "NOT_RUN"
    assert (
        data["status"] == "QUEUED"
        and result.headers["location"] == f"/api/v1/jobs/{data['job_id']}"
    )
    assert_private_free(result)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql("SELECT * FROM jobs").mappings().one()
        assert row["status"] == "QUEUED" and row["attempt_count"] == 0
        plan = json.loads(row["request_json"])
        assert plan["operations"] == ["DIVIDENDS"]
        assert plan["providers"][0]["profile_sha256"] is not None
        assert (
            con.exec_driver_sql(
                "SELECT request_id FROM audit_events WHERE action='JOB_CREATED'"
            ).scalar_one()
            == result.headers["x-request-id"]
        )
        for table in ("provider_runs", "upstream_attempts", "provider_budgets", "dividend_events"):
            assert con.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
    unchanged_finances(before, database_snapshot(engine))


async def test_files_are_loaded_outside_the_writer_and_same_key_replay_survives_rotation(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ProviderConfigurationStore.load

    def load(store: ProviderConfigurationStore, provider: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("SELECT 1")
        return original(store, provider)

    monkeypatch.setattr(ProviderConfigurationStore, "load", load)
    headers = write_headers(client)
    first = await client.post("/api/v1/dividends/sync", json=body(source), headers=headers)
    assert first.status_code == 202
    source["profile"].unlink()
    source["key"].unlink()
    committed = database_snapshot(engine)
    replay = await client.post("/api/v1/dividends/sync", json=body(source), headers=headers)
    assert replay.status_code == 202 and replay.json() == first.json()
    assert database_snapshot(engine) == committed
    conflict = await client.post(
        "/api/v1/dividends/sync", json=body(source, to_date="2026-01-30"), headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == committed


async def test_equivalent_sets_and_default_fields_replay_the_same_intent(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    headers = write_headers(client)
    first = await client.post(
        "/api/v1/dividends/sync",
        json=body(source, provider_ids=["yahoo", "eodhd"]),
        headers=headers,
    )
    assert first.status_code == 202
    committed = database_snapshot(engine)
    replay = await client.post(
        "/api/v1/dividends/sync",
        json=body(
            source,
            security_ids=[source["security"]["id"], source["security"]["id"]],
            provider_ids=["eodhd", "yahoo", "eodhd"],
            force=False,
            operations=["DIVIDENDS"],
        ),
        headers=headers,
    )
    assert replay.status_code == 202 and replay.json() == first.json()
    assert database_snapshot(engine) == committed


async def test_http_queue_to_real_worker_and_safe_poll_do_not_adopt_financial_facts(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    queued = await enqueue(client, source)
    before = database_snapshot(engine)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                b'[{"date":"2026-01-08","unadjustedValue":"0.9","currency":"USD"}]'
            ),
            headers={"content-type": "application/json"},
        )

    assert await dispatch_once(
        engine,
        source["settings"],
        uuid4(),
        clock=lambda: START,
        transport=httpx.MockTransport(respond),
    )
    committed = database_snapshot(engine)
    result = await client.get(queued.headers["location"])
    assert result.status_code == 200 and calls == ["/api/div/0005.HK"]
    assert_private_free(result)
    data = result.json()
    assert data["status"] == "SUCCEEDED" and data["progress_done"] == 1
    assert data["scope"] == "SOURCE_ONLY" and data["canonical_reconciliation"] == "NOT_RUN"
    assert data["result"]["provider_attempts"] == data["result"]["pages_committed"] == 1
    assert data["result"]["observations_new"] == 1
    assert result.headers["etag"] == f'"{data["version"]}"'
    assert database_snapshot(engine) == committed
    unchanged_finances(before, committed)


async def test_missing_configuration_is_pinned_unavailable_not_a_fake_sync_success(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    source["profile"].unlink()
    queued = await enqueue(client, source)
    provision(source["settings"])
    await dispatch_once(
        engine,
        source["settings"],
        uuid4(),
        clock=lambda: START,
        transport=httpx.MockTransport(lambda _: pytest.fail("Unavailable selection activated")),
    )
    result = await client.get(queued.headers["location"])
    assert result.json()["status"] == "FAILED"
    items = await client.get(queued.headers["location"] + "/items")
    assert items.json()["items"][0]["last_error_code"] == "UNCONFIGURED"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


async def test_cancel_queued_job_is_idempotent_audited_and_preserves_zero_scanned_progress(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    queued = await enqueue(client, source)
    path = queued.headers["location"] + "/cancel"
    headers = write_headers(client)
    result = await client.post(path, headers=headers)
    assert result.status_code == 200 and result.json()["status"] == "CANCELLED"
    assert result.json()["progress_done"] == result.json()["result"]["scanned_symbols"] == 0
    assert_private_free(result)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT request_id FROM audit_events WHERE action='JOB_CANCELLED'"
            ).scalar_one()
            == result.headers["x-request-id"]
        )
    committed = database_snapshot(engine)
    replay = await client.post(path, headers=headers, json={})
    assert replay.status_code == 200 and replay.json() == result.json()
    assert database_snapshot(engine) == committed


async def test_job_and_item_keyset_pages_are_bounded_ordered_and_search_is_literal(
    source: dict[str, Any], client: httpx.AsyncClient
) -> None:
    identifiers = [(await enqueue(client, source)).json()["job_id"] for _ in range(3)]
    first = await client.get("/api/v1/jobs", params={"limit": 2})
    assert_private_free(first)
    assert [row["id"] for row in first.json()["items"]] == sorted(identifiers, reverse=True)[:2]
    second = await client.get(
        "/api/v1/jobs", params={"limit": 2, "cursor": first.json()["next_cursor"]}
    )
    assert [row["id"] for row in second.json()["items"]] == sorted(identifiers, reverse=True)[2:]
    assert second.json()["next_cursor"] is None
    assert (await client.get("/api/v1/jobs", params={"q": "%"})).json()["items"] == []
    queued = await enqueue(
        client, source, provider_ids=["eodhd", "yahoo"], operations=["SPLITS", "DIVIDENDS"]
    )
    path = queued.headers["location"] + "/items"
    first = await client.get(path, params={"limit": 2})
    assert [row["ordinal"] for row in first.json()["items"]] == [0, 1]
    second = await client.get(path, params={"limit": 2, "cursor": first.json()["next_cursor"]})
    assert [row["ordinal"] for row in second.json()["items"]] == [2, 3]
    assert second.json()["next_cursor"] is None
    assert (
        len(
            (await client.get(path, params={"provider_id": "yahoo", "status": "PENDING"})).json()[
                "items"
            ]
        )
        == 2
    )
    assert (await client.get(path, params={"q": "%"})).json()["items"] == []
    assert_private_free(second)


async def test_cancel_inflight_work_returns_intent_then_preserves_unknown_spending(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    queued = await enqueue(client, source)
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'[{"date":"2026-01-08","value":"0.9"}]'),
            headers={"content-type": "application/json"},
        )

    task = asyncio.create_task(
        dispatch_once(
            engine,
            source["settings"],
            uuid4(),
            clock=lambda: START,
            transport=httpx.MockTransport(respond),
        )
    )
    await entered.wait()
    headers = write_headers(client)
    accepted = await client.post(queued.headers["location"] + "/cancel", headers=headers)
    assert accepted.status_code == 200 and accepted.json()["status"] == "CANCEL_REQUESTED"
    release.set()
    await task
    final = await client.get(queued.headers["location"])
    assert final.json()["status"] == "CANCELLED" and final.json()["progress_done"] == 0
    committed = database_snapshot(engine)
    replay = await client.post(queued.headers["location"] + "/cancel", headers=headers)
    assert replay.json() == accepted.json() and database_snapshot(engine) == committed
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert (
            con.exec_driver_sql("SELECT disposition FROM upstream_outcomes").scalar_one()
            == "CANCELLED"
        )


async def test_other_owner_jobs_are_not_visible_readable_or_cancellable(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    own = await enqueue(client, source)
    with unit_of_work(engine, write=True) as con:
        other = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at,is_active) "
            "SELECT ?,password_hash,password_changed_at,created_at,1 FROM owners WHERE id=?",
            (other, source["owner"]["id"]),
        )
        plan = con.exec_driver_sql(
            "SELECT request_json FROM jobs WHERE id=?", (own.json()["job_id"],)
        ).scalar_one()
        hidden = jobs.enqueue(
            con, UUID(other), SourceJobPlan.model_validate_json(plan), intent_id=uuid4(), at=START
        )
    before = database_snapshot(engine)
    path = f"/api/v1/jobs/{hidden['id']}"
    for suffix in ("", "/items"):
        result = await client.get(path + suffix)
        assert result.status_code == 404 and result.headers["cache-control"] == "no-store"
    cancelled = await client.post(path + "/cancel", headers=write_headers(client))
    assert cancelled.status_code == 404
    listed = await client.get("/api/v1/jobs")
    assert [row["id"] for row in listed.json()["items"]] == [own.json()["job_id"]]
    assert database_snapshot(engine) == before


async def test_private_or_tampered_cached_job_result_is_not_serialized_or_repaired_by_get(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    queued = await enqueue(client, source)
    with unit_of_work(engine, write=True) as con:
        lease = jobs.claim(con, uuid4(), at=START)
        assert lease is not None
        jobs.start(con, lease, at=START)
        con.exec_driver_sql(
            "UPDATE jobs SET result_json=?,version=version+1,updated_at=? WHERE id=?",
            (
                '{"private":"private-result-marker","observations_new":999}',
                "2026-01-10T00:00:01.000000Z",
                queued.json()["job_id"],
            ),
        )
    before = database_snapshot(engine)
    result = await client.get(queued.headers["location"])
    assert result.status_code == 200 and result.json()["result"]["observations_new"] == 0
    assert "private-result-marker" not in result.text
    assert database_snapshot(engine) == before


async def test_late_enqueue_audit_failure_rolls_back_all_tables(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = audit

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise ApiError(409, "INJECTED", "Synthetic late audit failure")

    monkeypatch.setattr(jobs, "audit", fail)
    before = database_snapshot(engine)
    result = await client.post(
        "/api/v1/dividends/sync", json=body(source), headers=write_headers(client)
    )
    assert result.status_code == 409 and database_snapshot(engine) == before


async def test_response_serialization_failure_cannot_leave_a_queued_job(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("SYNTHETIC_RESPONSE_FAILURE")

    monkeypatch.setattr(JobCreated, "model_dump_json", fail)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="SYNTHETIC_RESPONSE_FAILURE"):
        await client.post(
            "/api/v1/dividends/sync", json=body(source), headers=write_headers(client)
        )
    assert database_snapshot(engine) == before


async def test_revocation_between_private_preflight_and_uow_prevents_enqueue(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.selections
    revoked = []

    def revoke(*args: Any, **kwargs: Any) -> Any:
        selected = original(*args, **kwargs)
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("UPDATE sessions SET revoked_at='2026-11-06T00:00:00.000000Z'")
        revoked.append(database_snapshot(engine))
        return selected

    monkeypatch.setattr(service, "selections", revoke)
    result = await client.post(
        "/api/v1/dividends/sync", json=body(source), headers=write_headers(client)
    )
    assert result.status_code == 401 and database_snapshot(engine) == revoked[0]


@pytest.mark.parametrize(
    "changes",
    [
        {"from_date": "2026-02-30"},
        {"from_date": "2026-02-01"},
        {"force": "false"},
        {"force": 1},
        {"provider_ids": []},
        {"security_ids": []},
        {"operations": []},
        {"operations": ["QUOTES"]},
        {"provider_ids": ["unknown"]},
        {"security_ids": ["not-a-uuid"]},
        {"api_key": "private-input-marker"},
        {"profile_sha256": "a" * 64},
    ],
)
async def test_invalid_or_private_input_is_not_echoed_and_writes_nothing(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, changes: Any
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        "/api/v1/dividends/sync", json=body(source, **changes), headers=write_headers(client)
    )
    assert result.status_code == 422 and "private-input-marker" not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("query", ["from_date", "to_date", "security_ids", "provider_ids"])
async def test_scope_and_window_have_no_guessed_defaults(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, query: str
) -> None:
    payload = body(source)
    del payload[query]
    before = database_snapshot(engine)
    result = await client.post(
        "/api/v1/dividends/sync", json=payload, headers=write_headers(client)
    )
    assert result.status_code == 422 and database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["job", "items"])
@pytest.mark.parametrize(
    "value", ["not-a-cursor", "2026-02-30T00:00:00.000000Z", "999999999999999999999999999", "-1"]
)
async def test_bad_or_cross_resource_cursors_are_rejected_without_writes(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, field: str, value: str
) -> None:
    queued = await enqueue(client, source)
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    path = "/api/v1/jobs" if field == "job" else queued.headers["location"] + "/items"
    before = database_snapshot(engine)
    result = await client.get(path, params={"cursor": cursor})
    assert result.status_code == 422 and database_snapshot(engine) == before


@pytest.mark.parametrize(
    "method,resource",
    [
        ("GET", "list"),
        ("GET", "job"),
        ("GET", "items"),
        ("POST", "sync"),
        ("POST", "cancel"),
    ],
)
async def test_all_source_job_endpoints_require_owner_authentication(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    method: str,
    resource: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = await enqueue(client, source)
    headers = write_headers(client)
    paths = {
        "list": "/api/v1/jobs",
        "job": queued.headers["location"],
        "items": queued.headers["location"] + "/items",
        "sync": "/api/v1/dividends/sync",
        "cancel": queued.headers["location"] + "/cancel",
    }
    client.cookies.clear()
    monkeypatch.setattr(
        service, "selections", lambda *args: pytest.fail("Unauthorized profile read")
    )
    before = database_snapshot(engine)
    result = await client.request(
        method, paths[resource], headers=headers, json=body(source) if resource == "sync" else None
    )
    assert result.status_code == 401 and result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem,expected",
    [
        ("missing-csrf", 422),
        ("wrong-csrf", 403),
        ("missing-key", 422),
        ("short-key", 422),
        ("wrong-origin", 403),
        ("cross-site", 403),
    ],
)
async def test_rejected_write_headers_do_not_load_private_profiles_or_write(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    problem: str,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = write_headers(client)
    if problem == "missing-csrf":
        del headers["X-CSRF-Token"]
    elif problem == "wrong-csrf":
        headers["X-CSRF-Token"] = "x" * 32
    elif problem == "missing-key":
        del headers["Idempotency-Key"]
    elif problem == "short-key":
        headers["Idempotency-Key"] = "short"
    elif problem == "wrong-origin":
        headers["Origin"] = "https://untrusted.invalid"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    monkeypatch.setattr(service, "selections", lambda *args: pytest.fail("Rejected profile read"))
    before = database_snapshot(engine)
    result = await client.post("/api/v1/dividends/sync", json=body(source), headers=headers)
    assert result.status_code == expected and result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_idempotency_header_need_not_be_a_uuid_and_job_identity_is_server_generated(
    source: dict[str, Any], client: httpx.AsyncClient
) -> None:
    result = await client.post(
        "/api/v1/dividends/sync",
        json=body(source),
        headers=write_headers(client, key="synthetic-source-intent-not-a-uuid"),
    )
    assert result.status_code == 202 and UUID(result.json()["job_id"]).version == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"security_ids": [str(uuid4())] * 101},
        {"provider_ids": ["eodhd"] * 9},
        {"operations": ["DIVIDENDS"] * 3},
    ],
)
async def test_input_limits_apply_before_set_normalization(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, changes: Any
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        "/api/v1/dividends/sync", json=body(source, **changes), headers=write_headers(client)
    )
    assert result.status_code == 422 and database_snapshot(engine) == before


async def test_empty_cancel_contract_rejects_accidental_credentials_without_writes(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    queued = await enqueue(client, source)
    before = database_snapshot(engine)
    result = await client.post(
        queued.headers["location"] + "/cancel",
        json={"api_key": "private-cancel-marker"},
        headers=write_headers(client),
    )
    assert result.status_code == 422 and "private-cancel-marker" not in result.text
    assert database_snapshot(engine) == before


async def test_cancel_serialization_failure_rolls_back_control_audit_and_idempotency(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = await enqueue(client, source)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("SYNTHETIC_CANCEL_RESPONSE_FAILURE")

    monkeypatch.setattr(SourceJob, "model_dump_json", fail)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="SYNTHETIC_CANCEL_RESPONSE_FAILURE"):
        await client.post(queued.headers["location"] + "/cancel", headers=write_headers(client))
    assert database_snapshot(engine) == before


async def test_migration_mismatch_prevents_all_queue_side_effects(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    with engine.begin() as con:
        con.exec_driver_sql("UPDATE alembic_version SET version_num='older-schema'")
    before = database_snapshot(engine)
    result = await client.post(
        "/api/v1/dividends/sync", json=body(source), headers=write_headers(client)
    )
    assert result.status_code == 503 and result.json()["code"] == "MIGRATION_REQUIRED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["2026-01-10T00:00:00", "2026-01-10T00:00:00.000000+01:00"])
async def test_job_cursors_require_canonical_aware_utc_timestamps(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    await enqueue(client, source)
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    result = await client.get("/api/v1/jobs", params={"cursor": cursor})
    assert result.status_code == 422 and database_snapshot(engine) == before


async def test_retained_unmanaged_jobs_are_not_adopted_or_exposed_by_the_http_surface(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    own = await enqueue(client, source)
    legacy = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        original = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_managed_job_insert'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_managed_job_insert")
        con.exec_driver_sql(
            "INSERT INTO jobs(id,owner_id,kind,dedupe_key,status,request_json,"
            "created_at) VALUES (?,?,'LEGACY_IMPORT',?,'QUEUED',?,?)",
            (
                legacy,
                source["owner"]["id"],
                str(uuid4()),
                '{"private":"private-legacy-marker"}',
                "2026-01-10T00:00:00.000000Z",
            ),
        )
        con.exec_driver_sql(original)
    before = database_snapshot(engine)
    for suffix in ("", "/items"):
        result = await client.get(f"/api/v1/jobs/{legacy}" + suffix)
        assert result.status_code == 404 and "private-legacy-marker" not in result.text
    result = await client.post(f"/api/v1/jobs/{legacy}/cancel", headers=write_headers(client))
    assert result.status_code == 404
    listed = await client.get("/api/v1/jobs")
    assert [row["id"] for row in listed.json()["items"]] == [own.json()["job_id"]]
    assert database_snapshot(engine) == before


async def test_unknown_security_rolls_back_queue_audit_and_idempotency(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    result = await client.post(
        "/api/v1/dividends/sync",
        json=body(source, security_ids=[str(uuid4())]),
        headers=write_headers(client),
    )
    assert result.status_code == 422 and result.json()["code"] == "INVALID_SECURITY_SCOPE"
    assert database_snapshot(engine) == before
