"""Actual HTTP queue, worker and diagnostics consume only their explicit owner's current policy."""

import hmac
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.providers.profiles as profiles
import spm_api.providers.selection as selection
import spm_api.sync_service as source_api
from spm_api.audit import canonical, now
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.providers.budgets import BudgetSnapshot
from spm_api.providers.configuration import ProviderConfigurationStore, ProviderPolicy
from spm_api.providers.runtime import check_configurations
from spm_api.security import COOKIE, read_auth
from spm_api.sync_models import SyncDividendRequest
from spm_worker.dispatcher import dispatch_once
from spm_worker.scheduler import schedule_once
from tests.helpers import database_snapshot, version_headers, write_headers
from tests.test_managed_contexts import selected as selected
from tests.test_owned_budget_fences import lower_peer
from tests.test_owned_budget_fences import shared as shared
from tests.test_profile_api import PATH, changed
from tests.test_profile_api import source as source
from tests.test_provider_persistence import book as book
from tests.test_provider_selection import KEY, manage
from tests.test_provider_selection import local as local
from tests.test_provider_yahoo import chart, event
from tests.test_provider_yahoo import response as yahoo_response
from tests.test_schedule_migration import DUE
from tests.test_schedule_service import create as schedule_create
from tests.test_schedule_service import revise as schedule_enable
from tests.test_source_dispatcher import Clock, response, unchanged_finances
from tests.test_source_dispatcher import enqueue as enqueue_legacy

pytestmark = pytest.mark.anyio


async def queue(
    client: httpx.AsyncClient, selected: dict[str, Any], *, provider: str = "eodhd"
) -> UUID:
    result = await client.post(
        "/api/v1/dividends/sync",
        headers=write_headers(client),
        json={
            "security_ids": [selected["security"]["id"]],
            "provider_ids": [provider],
            "from_date": "2026-01-01",
            "to_date": "2026-01-31",
        },
    )
    assert result.status_code == 202
    return UUID(result.json()["job_id"])


async def test_actual_queue_and_dispatch_use_the_owned_import_not_the_operator_key(
    selected: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> None:
    identifier = await queue(client, selected)
    before = database_snapshot(engine)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        if not hmac.compare_digest(request.url.params["api_token"], KEY):
            raise AssertionError("Worker did not use the authorized owned credential")
        calls.append(request.url.path)
        with unit_of_work(engine, write=True) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        return response()

    assert await dispatch_once(
        engine, local, uuid4(), clock=Clock(now()), transport=httpx.MockTransport(handle)
    )
    assert calls == ["/api/div/0005.HK"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT status FROM jobs WHERE id=?", (str(identifier),)
            ).scalar_one()
            == "SUCCEEDED"
        )
        assert con.exec_driver_sql(
            "SELECT json_extract(profile_json,'$.configuration_source'),"
            "json_extract(profile_json,'$.revision_id') FROM provider_fetch_contexts"
        ).one() == ("MANAGED_PROFILE", selected["record"]["id"])
    unchanged_finances(before, database_snapshot(engine))


async def test_owned_disable_controls_http_diagnostics_without_changing_operator_cli_checks(
    selected: dict[str, Any],
    local: Any,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = selected["record"]
    disabled = await client.patch(
        f"{PATH}/eodhd",
        headers=version_headers(client, 3),
        json=changed(record, policy={**record["policy"], "enabled": False}),
    )
    assert disabled.status_code == 200
    before = database_snapshot(engine)
    health = await client.get("/api/v1/providers/eodhd/health")
    assert health.status_code == 200 and health.headers["cache-control"] == "no-store"
    assert health.json()["configured"] is False
    assert health.json()["state"] == "UNCONFIGURED"
    with monkeypatch.context() as patch:
        patch.setattr(
            selection, "unit_of_work", lambda *_: pytest.fail("Operator CLI checked the DB")
        )
        cli_reports = await check_configurations(local)
    operator = next(report for report in cli_reports if report.provider_id == "eodhd")
    assert operator.configured and operator.state == "STALE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bypass_private_check", [False, True])
async def test_owned_change_during_http_is_discarded_with_config_changed_without_refund(
    selected: dict[str, Any],
    local: Any,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    bypass_private_check: bool,
) -> None:
    identifier = await queue(client, selected)
    calls = []
    if bypass_private_check:
        monkeypatch.setattr(BudgetSnapshot, "unchanged", lambda *_: True)

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        record = selected["record"]
        result = await client.patch(
            f"{PATH}/eodhd",
            headers=version_headers(client, 3),
            json=changed(record, policy={**record["policy"], "enabled": False}),
        )
        assert result.status_code == 200
        return response()

    assert await dispatch_once(
        engine, local, uuid4(), clock=Clock(now()), transport=httpx.MockTransport(handle)
    )
    assert calls == ["/api/div/0005.HK"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT status FROM jobs WHERE id=?", (str(identifier),)
            ).scalar_one()
            == "FAILED"
        )
        assert con.exec_driver_sql("SELECT last_error_code,attempt_count FROM job_items").one() == (
            "CONFIG_CHANGED",
            1,
        )
        assert con.exec_driver_sql("SELECT disposition,reason FROM upstream_outcomes").one() == (
            "ABANDONED",
            "CONFIG_CHANGED",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert (
            con.exec_driver_sql("SELECT tokens_num FROM provider_budgets").scalar_one()
            == 4 * 60_000_000
        )


async def test_owned_change_after_preflight_but_before_grant_spends_zero_requests_or_rounds(
    selected: dict[str, Any],
    local: Any,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await queue(client, selected)
    owner = UUID(selected["owner"]["id"])
    modified: list[bool] = []

    def checked(*args: Any) -> bool:
        if not modified:
            with unit_of_work(engine, write=True) as con:
                profiles.save(
                    con,
                    owner,
                    ProviderPolicy.model_validate(
                        {**selected["record"]["policy"], "enabled": False}
                    ),
                    expected_version=3,
                    at=now(),
                    reason="Pause after the last private configuration check.",
                    request_id=uuid4(),
                )
            modified.append(True)
        return True

    monkeypatch.setattr(BudgetSnapshot, "unchanged", checked)
    assert await dispatch_once(
        engine,
        local,
        uuid4(),
        clock=Clock(now()),
        transport=httpx.MockTransport(lambda _: pytest.fail("Changed configuration sent HTTP")),
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT status,last_error_code,attempt_count FROM job_items"
        ).one() == ("FAILED", "CONFIG_CHANGED", 0)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM provider_budgets").scalar_one() == 0


async def test_explicit_owned_schedule_materializes_the_same_managed_pin_for_the_real_dispatcher(
    selected: dict[str, Any], local: Any, engine: Engine
) -> None:
    schedule_enable(engine, schedule_create(engine, selected))
    await schedule_once(engine, local, clock=Clock(DUE))
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        if not hmac.compare_digest(request.url.params["api_token"], KEY):
            raise AssertionError("Scheduled work did not use its owned credential")
        calls.append(request.url.path)
        return response()

    assert await dispatch_once(
        engine, local, uuid4(), clock=Clock(DUE), transport=httpx.MockTransport(handle)
    )
    assert calls == ["/api/div/0005.HK"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT outcome FROM source_schedule_occurrences").scalar_one()
            == "QUEUED"
        )
        assert con.exec_driver_sql("SELECT status FROM jobs").scalar_one() == "SUCCEEDED"
        assert (
            con.exec_driver_sql(
                "SELECT json_extract(profile_json,'$.revision_id') FROM provider_fetch_contexts"
            ).scalar_one()
            == selected["record"]["id"]
        )


@pytest.mark.parametrize(
    "failure", ["disable", "clear", "rotation", "unsafe_edition", "missing_storage"]
)
async def test_unavailable_owned_policy_never_resurrects_the_operator_profile_for_queued_work(
    selected: dict[str, Any],
    local: Any,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await queue(client, selected)
    record = selected["record"]
    if failure in ("disable", "clear"):
        result = await client.patch(
            f"{PATH}/eodhd",
            headers=version_headers(client, 3),
            json={
                **changed(record, policy={**record["policy"], "enabled": False}),
                "clear_credential": failure == "clear",
            },
        )
        assert result.status_code == 200
    elif failure == "rotation":
        result = await client.post(
            f"{PATH}/eodhd/credential",
            headers=version_headers(client, 3),
            json={
                "credential": "synthetic-new-owned-edition",
                "reason": "Explicit synthetic owned rotation keeps it disabled.",
            },
        )
        assert result.status_code == 200
    elif failure == "missing_storage":
        monkeypatch.setattr(local, "managed_provider_secret_dir", None)
    else:
        root = local.managed_provider_secret_dir
        assert root is not None
        next(root.glob("*.key")).chmod(0o644)
    monkeypatch.setattr(
        ProviderConfigurationStore,
        "load",
        lambda *_: pytest.fail("Owned selection fell back to operator files"),
    )
    assert await dispatch_once(
        engine,
        local,
        uuid4(),
        clock=Clock(now()),
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Unavailable owned selection sent HTTP")
        ),
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT status,last_error_code,attempt_count FROM job_items"
        ).one() == ("SKIPPED", "UNCONFIGURED", 0)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


async def test_old_operator_plan_never_becomes_permission_for_a_new_owned_credential(
    source: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> None:
    enqueue_legacy(source, engine)
    await manage(client)
    assert await dispatch_once(
        engine,
        local,
        uuid4(),
        clock=Clock(now()),
        transport=httpx.MockTransport(
            lambda _: pytest.fail("Old operator pin adopted a managed key")
        ),
    )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT status,last_error_code,attempt_count FROM job_items"
        ).one() == ("FAILED", "CONFIG_CHANGED", 0)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0


@pytest.mark.parametrize(
    "path", ["/api/v1/providers", "/api/v1/providers/eodhd/health", "/api/v1/providers/coverage"]
)
async def test_every_authenticated_diagnostic_uses_owned_precedence_but_stays_private_and_read_only(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine, path: str
) -> None:
    record = selected["record"]
    assert (
        await client.patch(
            f"{PATH}/eodhd",
            headers=version_headers(client, 3),
            json=changed(record, policy={**record["policy"], "enabled": False}),
        )
    ).status_code == 200
    before = database_snapshot(engine)
    result = await client.get(
        path, params={"security_id": selected["security"]["id"], "provider_id": "eodhd"}
    )
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    if path.endswith("/health"):
        value = result.json()
    elif path.endswith("/coverage"):
        value = result.json()["items"][0]["configuration"]
    else:
        value = next(row for row in result.json()["items"] if row["provider_id"] == "eodhd")[
            "configuration"
        ]
    assert value["state"] == "UNCONFIGURED" and not value["configured"]
    for private in (
        "owner_id",
        "credential_artifact_id",
        "profile_sha256",
        "selection_owner_id",
        "profile_json",
    ):
        assert private not in result.text
    assert database_snapshot(engine) == before


async def test_queue_writer_rejects_an_actor_different_from_private_selection_preparation(
    selected: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> None:
    body = SyncDividendRequest(
        security_ids=(UUID(selected["security"]["id"]),),
        provider_ids=("eodhd",),
        from_date="2026-01-01",
        to_date="2026-01-31",
    )
    owner = UUID(selected["owner"]["id"])
    prepared = source_api.selections(local, engine, owner, body.provider_ids)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
        with pytest.raises(ApiError) as error:
            source_api.enqueue(con, auth, body, prepared, str(uuid4()), selection_owner_id=uuid4())
        assert error.value.code == "SOURCE_SELECTION_CHANGED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change_peer", [False, True])
async def test_real_dispatch_passes_the_complete_owned_shared_budget_at_both_boundaries(
    shared: dict[str, Any],
    local: Any,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    change_peer: bool,
) -> None:
    await queue(client, shared, provider="yahoo")
    calls = []
    if change_peer:
        monkeypatch.setattr(BudgetSnapshot, "unchanged", lambda *_: True)

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if change_peer:
            await lower_peer(client, shared["peer"])
        return yahoo_response(chart({"dividends": event(currency="USD")}))

    assert await dispatch_once(
        engine, local, uuid4(), clock=Clock(now()), transport=httpx.MockTransport(handle)
    )
    assert calls == ["/v8/finance/chart/0005.HK"]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT origin_group,minute_limit,day_limit,tokens_num FROM provider_budgets"
        ).one() == ("YAHOO", 2, 20, 60_000_000)
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT disposition,reason FROM upstream_outcomes").one() == (
            ("ABANDONED", "CONFIG_CHANGED") if change_peer else ("COMPLETED", None)
        )
        if change_peer:
            assert con.exec_driver_sql("SELECT status,last_error_code FROM job_items").one() == (
                "FAILED",
                "CONFIG_CHANGED",
            )
            assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        else:
            assert con.exec_driver_sql("SELECT status FROM job_items").scalar_one() == "SUCCEEDED"
            assert con.exec_driver_sql(
                "SELECT dps_e8,currency FROM dividend_observations"
            ).one() == (90_000_000, "USD")


async def test_retained_pre_install_idempotent_response_preserves_its_original_scope_and_capability(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    intent = write_headers(client)
    body = {
        "policy": {"provider_id": "yahoo"},
        "reason": "Synthetic retained pre-install policy receipt.",
    }
    created = await client.post(PATH, json=body, headers=intent)
    assert created.status_code == 201 and created.json()["runtime_selection_installed"]
    retained = {
        **created.json(),
        "runtime_selection_installed": False,
        "scope": "POLICY_DEFINITION_ONLY",
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (canonical(retained), intent["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=intent)
    assert replay.status_code == 201 and replay.headers["etag"] == '"1"'
    assert replay.json() == retained and replay.headers["cache-control"] == "no-store"
    current = await client.get(f"{PATH}/yahoo")
    assert (
        current.json()["runtime_selection_installed"]
        and current.json()["scope"] == "OWNED_PROVIDER_POLICY"
    )
    assert database_snapshot(engine) == before
