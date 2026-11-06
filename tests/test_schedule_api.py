"""Protected schedule metadata is exact, owner-scoped, no-store and never financial adoption."""

import asyncio
import base64
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.job_service as jobs
import spm_api.schedule_routes as routes
import spm_api.schedule_service as service
from spm_api.db import unit_of_work
from spm_api.job_models import ProviderSelection, SourceJobPlan
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.schedule_models import SourceScheduleRecord
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.test_provider_persistence import START
from tests.test_provider_persistence import book as book
from tests.test_schedule_migration import DUE, policy

pytestmark = pytest.mark.anyio
PATH = "/api/v1/source-schedules"


@pytest.fixture
async def source(book: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(routes, "now", lambda: START)
    return book


def body(
    source: dict[str, Any], *, name: str = "Synthetic source schedule", **changes: Any
) -> dict[str, Any]:
    return {"name": name, "policy": policy(source).model_dump(mode="json"), **changes}


async def create(
    client: httpx.AsyncClient, source: dict[str, Any], **changes: Any
) -> httpx.Response:
    response = await client.post(PATH, json=body(source, **changes), headers=write_headers(client))
    assert response.status_code == 201, response.text
    return response


def changed(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "name": record["name"],
        "policy": record["policy"],
        "enabled": True,
        "reason": "Explicitly changed a synthetic schedule through the owner API.",
        **changes,
    }


async def revise(
    client: httpx.AsyncClient, record: dict[str, Any], **changes: Any
) -> httpx.Response:
    response = await client.patch(
        f"{PATH}/{record['id']}",
        json=changed(record, **changes),
        headers=version_headers(client, record["version"]),
    )
    assert response.status_code == 200, response.text
    return response


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    for field in (
        "owner_id",
        "policy_json",
        "policy_sha256",
        "profile_sha256",
        "profile_json",
        "request_json",
        "dedupe_key",
        "lease_owner",
        "credential",
        "cursor_in",
    ):
        assert field not in response.text


def cursor(value: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()


async def test_create_is_disabled_audited_owned_and_truthful_about_dispatch(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("Schedule HTTP must not read private provider profiles")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    before = database_snapshot(engine)
    response = await create(client, source)
    record = response.json()
    safe(response)
    assert response.headers["location"] == f"{PATH}/{record['id']}"
    assert response.headers["etag"] == '"1"'
    assert record["enabled"] is False and record["next_run_at"] is None
    assert record["scope"] == "SOURCE_ONLY" and record["canonical_reconciliation"] == "NOT_RUN"
    assert record["automatic_materialization_installed"] is False
    assert record["exchange_timezone"] == "Asia/Hong_Kong"
    assert record["policy"]["operations"] == ["DIVIDENDS"]
    assert record["policy"]["calendar_basis"] == "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR"
    assert record["occurrence_count"] == 0 and record["last_job_id"] is None
    after = database_snapshot(engine)
    allowed = {"source_schedules", "audit_events", "idempotency_keys"}
    assert {t: rows for t, rows in before.items() if t not in allowed} == {
        t: rows for t, rows in after.items() if t not in allowed
    }
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT request_id FROM audit_events WHERE action='SOURCE_SCHEDULE_CREATED'"
            ).scalar_one()
            == response.headers["x-request-id"]
        )


async def test_get_and_list_are_read_only_and_preserve_scope_without_private_payloads(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    record = (await create(client, source)).json()
    before = database_snapshot(engine)
    one = await client.get(f"{PATH}/{record['id']}")
    listed = await client.get(PATH)
    history = await client.get(f"{PATH}/{record['id']}/occurrences")
    for response in (one, listed, history):
        assert response.status_code == 200
        safe(response)
    assert one.json() == record and one.headers["etag"] == '"1"'
    assert listed.json()["items"] == [record]
    assert history.json() == {"items": [], "next_cursor": None}
    assert database_snapshot(engine) == before


async def test_full_cas_revision_enables_only_intent_and_pause_does_not_create_jobs(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = (await create(client, source)).json()
    enabled = await revise(client, original)
    record = enabled.json()
    safe(enabled)
    assert record["enabled"] is True and record["version"] == 2
    assert datetime.fromisoformat(record["next_run_at"]) == DUE
    assert record["automatic_materialization_installed"] is False
    assert enabled.headers["etag"] == '"2"'
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 0
    paused = await revise(client, record, enabled=False)
    assert paused.json()["enabled"] is False and paused.json()["next_run_at"] is None
    assert paused.headers["etag"] == '"3"'


async def test_stale_editor_and_failed_mutations_leave_every_table_unchanged(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = (await create(client, source)).json()
    await revise(client, original)
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{original['id']}",
        json=changed(original, enabled=False),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 412 and response.json()["code"] == "PRECONDITION_FAILED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_post_and_patch_replays_keep_original_response_after_metadata_changes(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    post_headers = write_headers(client)
    first = await client.post(PATH, json=body(source), headers=post_headers)
    record = first.json()
    patch_headers = version_headers(client, 1)
    enabled = await client.patch(
        f"{PATH}/{record['id']}", json=changed(record), headers=patch_headers
    )
    assert enabled.status_code == 200
    await revise(client, enabled.json(), enabled=False)
    before = database_snapshot(engine)
    replay_create = await client.post(PATH, json=body(source), headers=post_headers)
    replay_patch = await client.patch(
        f"{PATH}/{record['id']}", json=changed(record), headers=patch_headers
    )
    assert replay_create.json() == record and replay_create.headers["etag"] == '"1"'
    assert replay_patch.json() == enabled.json() and replay_patch.headers["etag"] == '"2"'
    assert (await client.get(f"{PATH}/{record['id']}")).json()["version"] == 3
    assert database_snapshot(engine) == before
    conflict = await client.patch(
        f"{PATH}/{record['id']}", json=changed(record), headers={**patch_headers, "If-Match": '"3"'}
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True},
        {"owner_id": str(uuid4())},
        {"force": False},
        {"api_key": "synthetic-not-an-actual-key"},
    ],
)
async def test_create_cannot_smuggle_server_controls(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body(source, **changes), headers=write_headers(client))
    assert response.status_code == 422
    assert "synthetic-not-an-actual-key" not in response.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes", [{"enabled": 1}, {"enabled": "true"}, {"reason": "short"}, {"version": 1}]
)
async def test_update_rejects_coercion_short_reason_and_unknown_fields(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, changes: dict[str, Any]
) -> None:
    record = (await create(client, source)).json()
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{record['id']}",
        json=changed(record, **changes),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 422
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("path", [PATH, f"{PATH}/{uuid4()}", f"{PATH}/{uuid4()}/occurrences"])
async def test_protected_reads_require_authentication(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 401
    safe(response)


@pytest.mark.parametrize(
    "problem,status",
    [
        ("missing-csrf", 422),
        ("wrong-csrf", 403),
        ("missing-key", 422),
        ("missing-version", 422),
        ("bad-version", 422),
    ],
)
async def test_patch_requires_csrf_idempotency_and_quoted_version(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, problem: str, status: int
) -> None:
    record = (await create(client, source)).json()
    headers = version_headers(client, 1)
    if problem == "missing-csrf":
        del headers["X-CSRF-Token"]
    elif problem == "wrong-csrf":
        headers["X-CSRF-Token"] = "synthetic-wrong-csrf"
    elif problem == "missing-key":
        del headers["Idempotency-Key"]
    elif problem == "missing-version":
        del headers["If-Match"]
    else:
        headers["If-Match"] = "1"
    before = database_snapshot(engine)
    response = await client.patch(f"{PATH}/{record['id']}", json=changed(record), headers=headers)
    assert response.status_code == status
    safe(response)
    assert database_snapshot(engine) == before


async def test_lists_use_literal_search_casefolded_name_id_keysets_and_enablement_filter(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    records = [
        (await create(client, source, name=name)).json()
        for name in ("beta", "Alpha", "alpha", "%_literal")
    ]
    await revise(client, records[0])
    before = database_snapshot(engine)
    expected = sorted(records, key=lambda row: (row["name"].lower(), row["id"]))
    first = (await client.get(PATH, params={"limit": 2})).json()
    second = (await client.get(PATH, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert [row["id"] for row in first["items"] + second["items"]] == [
        row["id"] for row in expected
    ]
    assert second["next_cursor"] is None
    assert [
        row["name"] for row in (await client.get(PATH, params={"q": "%_"})).json()["items"]
    ] == ["%_literal"]
    assert [
        row["id"] for row in (await client.get(PATH, params={"enabled": "true"})).json()["items"]
    ] == [records[0]["id"]]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["x" * 101, " leading", "trailing ", "bad\x00value"])
async def test_name_cursor_is_validated_before_sql_binding(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    before = database_snapshot(engine)
    response = await client.get(PATH, params={"cursor": cursor(value)})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    assert database_snapshot(engine) == before


async def materialized(
    client: httpx.AsyncClient, source: dict[str, Any], engine: Engine, *, count: int = 3
) -> dict[str, Any]:
    record = dict((await revise(client, (await create(client, source)).json())).json())
    selected = (ProviderSelection(provider_id="eodhd", profile_sha256=None),)
    for days in range(count):
        at = DUE + timedelta(days=days)
        with unit_of_work(engine, write=True) as con:
            tick = service.due_schedule(con, UUID(source["owner"]["id"]), UUID(record["id"]), at=at)
            assert tick is not None
            service.materialize(con, tick, selected, at=at)
    return record


async def test_immutable_occurrence_reads_keep_frozen_terms_and_owner_job_links(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    record = await materialized(client, source, engine)
    source["profile"].unlink()
    source["key"].unlink()
    before = database_snapshot(engine)
    one = await client.get(f"{PATH}/{record['id']}")
    first = await client.get(f"{PATH}/{record['id']}/occurrences", params={"limit": 2})
    second = await client.get(
        f"{PATH}/{record['id']}/occurrences",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    for response in (one, first, second):
        assert response.status_code == 200
        safe(response)
    rows = first.json()["items"] + second.json()["items"]
    assert [row["sequence"] for row in rows] == [3, 2, 1]
    assert [row["outcome"] for row in rows] == ["SKIPPED_IN_FLIGHT", "SKIPPED_IN_FLIGHT", "QUEUED"]
    assert rows[-1]["window"] == {"start": "2025-12-12", "end": "2026-01-10"}
    assert rows[-1]["policy"] == record["policy"]
    assert rows[0]["job_id"] is None and rows[0]["blocking_job_id"] == rows[-1]["job_id"]
    assert one.json()["occurrence_count"] == 3 and one.json()["version"] == 5
    assert one.json()["last_job_id"] == rows[-1]["job_id"]
    assert one.headers["etag"] == '"5"'
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("path", ["", "/occurrences"])
async def test_foreign_owner_schedule_reads_are_not_found(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, path: str
) -> None:
    record = (await create(client, source)).json()
    with unit_of_work(engine, write=True) as con:
        owner = dict(con.exec_driver_sql("SELECT * FROM owners").mappings().one())
        owner["id"] = str(uuid4())
        fields = ",".join(owner)
        con.exec_driver_sql(
            f"INSERT INTO owners({fields}) VALUES ({','.join('?' for _ in owner)})",
            tuple(owner.values()),
        )
        con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
        con.exec_driver_sql(
            "UPDATE source_schedules SET owner_id=? WHERE id=?", (owner["id"], record["id"])
        )
    before = database_snapshot(engine)
    response = await client.get(f"{PATH}/{record['id']}{path}")
    assert response.status_code == 404
    assert database_snapshot(engine) == before


async def test_corrupt_policy_is_safe_503_without_read_repairs(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    record = (await create(client, source)).json()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
        con.exec_driver_sql(
            "UPDATE source_schedules SET policy_sha256=? WHERE id=?", ("f" * 64, record["id"])
        )
    before = database_snapshot(engine)
    for path in (PATH, f"{PATH}/{record['id']}"):
        response = await client.get(path)
        assert (
            response.status_code == 503 and response.json()["code"] == "SOURCE_SCHEDULE_UNAVAILABLE"
        )
        safe(response)
    assert database_snapshot(engine) == before


async def test_same_scope_job_with_another_intent_cannot_replace_the_original_receipt(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    record = await materialized(client, source, engine, count=1)
    with unit_of_work(engine, write=True) as con:
        plan = SourceJobPlan.model_validate_json(
            con.exec_driver_sql("SELECT request_json FROM jobs").scalar_one()
        )
        other = jobs.enqueue(con, UUID(source["owner"]["id"]), plan, intent_id=uuid4(), at=DUE)
        con.exec_driver_sql("DROP TRIGGER trg_source_occurrence_no_update")
        con.exec_driver_sql("UPDATE source_schedule_occurrences SET job_id=?", (other["id"],))
    before = database_snapshot(engine)
    response = await client.get(f"{PATH}/{record['id']}/occurrences")
    assert response.status_code == 503 and response.json()["code"] == "SOURCE_SCHEDULE_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "assignment",
    [
        "last_sequence=last_sequence+1",
        "last_due_at='2026-01-11T09:15:00.000000Z'",
        "initial_job_id=last_job_id",
        "next_run_at='2026-01-13T09:15:01.000000Z'",
        "updated_at='2026-01-15T09:15:00.000000Z'",
    ],
)
async def test_corrupt_progress_is_not_exposed_or_repaired(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, assignment: str
) -> None:
    record = await materialized(client, source, engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
        con.exec_driver_sql(f"UPDATE source_schedules SET {assignment} WHERE id=?", (record["id"],))
    before = database_snapshot(engine)
    for path in (PATH, f"{PATH}/{record['id']}"):
        response = await client.get(path)
        assert (
            response.status_code == 503 and response.json()["code"] == "SOURCE_SCHEDULE_UNAVAILABLE"
        )
        safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "assignment",
    [
        "window_start='2000-01-01'",
        "next_run_at='2026-01-11T09:15:01.000000Z'",
        "policy_sha256='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
    ],
)
async def test_corrupt_immutable_receipts_fail_safely_without_raw_dumps(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, assignment: str
) -> None:
    record = await materialized(client, source, engine, count=1)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_source_occurrence_no_update")
        con.exec_driver_sql(f"UPDATE source_schedule_occurrences SET {assignment}")
    before = database_snapshot(engine)
    response = await client.get(f"{PATH}/{record['id']}/occurrences")
    assert response.status_code == 503 and response.json()["code"] == "SOURCE_SCHEDULE_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30T00:00:00.000000Z",
        "0000-01-01T00:00:00.000000Z",
        "2026-01-01T00:00:00",
        "9999-12-31T23:59:59.999999-12:00",
        str(2**70),
    ],
)
async def test_occurrence_cursor_rejects_noncanonical_or_out_of_range_times(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    record = (await create(client, source)).json()
    before = database_snapshot(engine)
    response = await client.get(
        f"{PATH}/{record['id']}/occurrences", params={"cursor": cursor(value)}
    )
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("market", ["US", "HK"])
async def test_scope_is_from_catalogue_and_duplicate_names_are_conflicts(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, market: str
) -> None:
    await create(client, source)
    before = database_snapshot(engine)
    payload = body(source)
    payload["policy"]["market"] = market
    response = await client.post(PATH, json=payload, headers=write_headers(client))
    assert response.status_code == (422 if market == "US" else 409)
    assert database_snapshot(engine) == before


async def test_worker_progress_makes_an_observed_owner_editor_stale(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await materialized(client, source, engine, count=1)
    before = database_snapshot(engine)
    response = await client.patch(
        f"{PATH}/{original['id']}",
        json=changed(original, enabled=False),
        headers=version_headers(client, original["version"]),
    )
    assert response.status_code == 412
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_response_serialization_failure_rolls_back_schedule_audit_and_idempotency(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    record = (await create(client, source)).json()

    def fail(*_: Any, **__: Any) -> Any:
        raise ValueError("SYNTHETIC_SCHEDULE_RESPONSE_FAILURE")

    monkeypatch.setattr(SourceScheduleRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="SYNTHETIC_SCHEDULE_RESPONSE_FAILURE"):
        if operation == "create":
            await client.post(
                PATH,
                json=body(source, name="Failed response definition"),
                headers=write_headers(client),
            )
        else:
            await client.patch(
                f"{PATH}/{record['id']}", json=changed(record), headers=version_headers(client, 1)
            )
    assert database_snapshot(engine) == before


async def test_http_audit_failure_rolls_back_every_mutation_side_effect(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("""
            CREATE TRIGGER synthetic_schedule_http_audit_failure BEFORE INSERT ON audit_events
            WHEN NEW.action='SOURCE_SCHEDULE_CREATED'
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_AUDIT_FAILURE'); END
        """)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body(source), headers=write_headers(client))
    assert response.status_code == 409
    assert database_snapshot(engine) == before


async def test_actual_http_concurrent_create_replays_one_committed_definition(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    headers = write_headers(client)
    responses = await asyncio.gather(
        *[client.post(PATH, json=body(source), headers=headers) for _ in range(2)]
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM source_schedules").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='SOURCE_SCHEDULE_CREATED'"
            ).scalar_one()
            == 1
        )


async def test_actual_http_concurrent_edits_preserve_observed_version_semantics(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    record = (await create(client, source)).json()
    responses = await asyncio.gather(
        *[
            client.patch(
                f"{PATH}/{record['id']}", json=changed(record), headers=version_headers(client, 1)
            )
            for _ in range(2)
        ]
    )
    assert sorted(response.status_code for response in responses) == [200, 412]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT version FROM source_schedules").scalar_one() == 2
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 0


async def test_schedule_identity_has_no_delete_or_unprotected_materialize_endpoint(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    record = (await create(client, source)).json()
    before = database_snapshot(engine)
    deleted = await client.delete(f"{PATH}/{record['id']}", headers=version_headers(client, 1))
    materialize = await client.post(
        f"{PATH}/{record['id']}/materialize", headers=write_headers(client)
    )
    assert deleted.status_code == 405 and materialize.status_code == 404
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["wrong-latest-job", "wrong-blocker", "legacy-job"])
async def test_retained_job_links_cannot_be_misreported_as_owned_managed_progress(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, problem: str
) -> None:
    record = await materialized(client, source, engine)
    with unit_of_work(engine, write=True) as con:
        original = con.exec_driver_sql("SELECT * FROM jobs").mappings().one()
        if problem == "legacy-job":
            con.exec_driver_sql("DROP TRIGGER trg_managed_job_update")
            con.exec_driver_sql("UPDATE jobs SET runtime_version=0 WHERE id=?", (original["id"],))
        else:
            plan = SourceJobPlan.model_validate_json(original["request_json"])
            other = jobs.enqueue(con, UUID(source["owner"]["id"]), plan, intent_id=uuid4(), at=DUE)
            if problem == "wrong-latest-job":
                con.exec_driver_sql("DROP TRIGGER trg_source_schedule_update")
                con.exec_driver_sql(
                    "UPDATE source_schedules SET last_job_id=? WHERE id=?",
                    (other["id"], record["id"]),
                )
            else:
                con.exec_driver_sql("DROP TRIGGER trg_source_occurrence_no_update")
                con.exec_driver_sql(
                    "UPDATE source_schedule_occurrences SET blocking_job_id=? WHERE sequence=2",
                    (other["id"],),
                )
    before = database_snapshot(engine)
    path = f"{PATH}/{record['id']}" + ("" if problem == "wrong-latest-job" else "/occurrences")
    response = await client.get(path)
    assert response.status_code == 503 and response.json()["code"] == "SOURCE_SCHEDULE_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
