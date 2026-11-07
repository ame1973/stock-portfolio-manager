"""Managed provenance and scoped fallback are fenced in the same short source writer."""

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.providers.credentials as credentials
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.persistence import (
    capture_page,
    persist_context,
    prepare_fetch,
    validate_selection,
)
from tests.helpers import database_snapshot, version_headers
from tests.test_profile_api import PATH, changed, create
from tests.test_profile_api import source as source
from tests.test_provider_persistence import FINISH, START, fetched
from tests.test_provider_persistence import book as book
from tests.test_provider_selection import local as local
from tests.test_provider_selection import manage, store

pytestmark = pytest.mark.anyio


@pytest.fixture
async def selected(
    source: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> dict[str, Any]:
    record = await manage(client)
    selected_store = store(local, engine, source)
    loaded = selected_store.load("eodhd")
    prepared = prepare_fetch(
        UUID(source["owner"]["id"]), loaded, source["prepared"].segment, "DIVIDENDS"
    )
    return {**source, "record": record, "prepared": prepared, "store": selected_store}


def save(engine: Engine, selected: dict[str, Any], page: Any, *, run: Any = None) -> Any:
    with unit_of_work(engine, write=True) as con:
        return capture_page(
            con,
            selected["prepared"],
            page,
            run_id=run or uuid4(),
            started_at=START,
            finished_at=FINISH,
            duration_ms=12,
        )


async def test_scoped_operator_capture_replays_after_override_but_new_pages_are_rejected(
    source: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> None:
    loaded = store(local, engine, source).load("eodhd")
    prepared = prepare_fetch(
        UUID(source["owner"]["id"]), loaded, source["prepared"].segment, "DIVIDENDS"
    )
    case = {**source, "prepared": prepared}
    page, run = await fetched(case), uuid4()
    original = save(engine, case, page, run=run)
    assert original.observations_new == 1
    await create(client, "eodhd")
    before = database_snapshot(engine)
    assert save(engine, case, page, run=run) == original
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, case, page)
    assert database_snapshot(engine) == before


async def test_current_managed_source_context_captures_exact_identity_without_financial_changes(
    selected: dict[str, Any], engine: Engine
) -> None:
    page = await fetched(selected)
    before = database_snapshot(engine)
    receipt = save(engine, selected, page)
    assert receipt.observations_new == 1
    after = database_snapshot(engine)
    allowed = {
        "provider_fetch_contexts",
        "provider_runs",
        "dividend_observations",
        "split_observations",
        "provider_observation_sightings",
        "audit_events",
    }
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT json_extract(profile_json,'$.configuration_source'), "
            "json_extract(profile_json,'$.revision_id') FROM provider_fetch_contexts WHERE id=?",
            (str(receipt.context_id),),
        ).one() == ("MANAGED_PROFILE", selected["record"]["id"])


async def test_completed_managed_capture_replays_after_later_disable_without_any_writes(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(selected)
    run = uuid4()
    original = save(engine, selected, page, run=run)
    record = selected["record"]
    disabled = await client.patch(
        f"{PATH}/eodhd",
        json=changed(record, policy={**record["policy"], "enabled": False}),
        headers=version_headers(client, 3),
    )
    assert disabled.status_code == 200
    before = database_snapshot(engine)
    assert save(engine, selected, page, run=run) == original
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("existing_context", [True, False])
async def test_new_page_rechecks_latest_managed_decision_even_for_retained_context(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine, existing_context: bool
) -> None:
    page = await fetched(selected)
    if existing_context:
        with unit_of_work(engine, write=True) as con:
            persist_context(con, selected["prepared"])
    record = selected["record"]
    result = await client.patch(
        f"{PATH}/eodhd", json=changed(record), headers=version_headers(client, 3)
    )
    assert result.status_code == 200
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, selected, page)
    assert database_snapshot(engine) == before


async def test_scoped_operator_fallback_cannot_commit_after_a_managed_definition_appears(
    source: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> None:
    loaded = store(local, engine, source).load("eodhd")
    prepared = prepare_fetch(
        UUID(source["owner"]["id"]), loaded, source["prepared"].segment, "DIVIDENDS"
    )
    page = await fetched({**source, "prepared": prepared})
    await create(client, "eodhd")
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, {**source, "prepared": prepared}, page)
    assert database_snapshot(engine) == before


async def test_explicit_unscoped_legacy_library_call_remains_compatible_without_owner_selection(
    source: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> None:
    loaded = ProviderConfigurationStore(local).load("eodhd")
    prepared = prepare_fetch(
        UUID(source["owner"]["id"]), loaded, source["prepared"].segment, "DIVIDENDS"
    )
    page = await fetched({**source, "prepared": prepared})
    await create(client, "eodhd")
    receipt = save(engine, {**source, "prepared": prepared}, page)
    assert receipt.observations_new == 1
    assert (await client.get(f"{PATH}/eodhd")).json()["runtime_selection_installed"] is True


async def test_managed_context_keeps_existing_exact_observation_reads_private_and_no_store(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(selected)
    receipt = save(engine, selected, page)
    before = database_snapshot(engine)
    response = await client.get(
        "/api/v1/dividends/observations",
        params={
            "security_id": selected["security"]["id"],
            "provider_id": "eodhd",
        },
    )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    row = response.json()["items"][0]
    assert row["id"] == receipt.observation_ids[0]
    assert row["dps"] == "0.90000000" and row["currency"] == "USD"
    assert row["adjustment_basis"] == "UNKNOWN" and row["scope"] == "SOURCE_ONLY"
    assert row["canonical_adoption"] == "NOT_ADOPTED" and row["sightings_count"] == 1
    for field in ("profile_json", "profile_sha256", "credential_artifact_id", "selection_owner_id"):
        assert field not in response.text
    assert database_snapshot(engine) == before


async def test_private_files_are_not_read_by_current_selection_writer_checks(
    selected: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await fetched(selected)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Source writer must use retained metadata, never private files")

    monkeypatch.setattr(credentials, "managed_artifacts", forbidden)
    monkeypatch.setattr(credentials.CredentialArtifactStore, "load", forbidden)
    assert save(engine, selected, page).observations_new == 1


async def test_scope_marker_cannot_be_reused_for_another_prepared_owner(
    source: dict[str, Any], local: Any, engine: Engine
) -> None:
    loaded = store(local, engine, source).load("eodhd")
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_OWNER_MISMATCH"):
        prepare_fetch(uuid4(), loaded, source["prepared"].segment, "DIVIDENDS")
    assert database_snapshot(engine) == before


async def test_managed_metadata_hash_cannot_be_substituted_after_private_preparation(
    selected: dict[str, Any], engine: Engine
) -> None:
    prepared = selected["prepared"]
    revision = prepared.configuration.revision
    assert revision is not None
    forged = replace(
        prepared,
        configuration=replace(
            prepared.configuration, revision=replace(revision, profile_sha256="f" * 64)
        ),
    )
    page = await fetched(selected)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, {**selected, "prepared": forged}, page)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["inactive_owner", "missing_history", "corrupt_history"])
async def test_scoped_source_writer_fails_closed_on_lost_or_unreadable_owned_authority(
    selected: dict[str, Any], engine: Engine, failure: str
) -> None:
    page = await fetched(selected)
    with engine.begin() as con:
        if failure == "inactive_owner":
            con.exec_driver_sql("UPDATE owners SET is_active=0")
        elif failure == "missing_history":
            con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_delete")
            con.exec_driver_sql("DELETE FROM provider_profile_revisions")
        else:
            con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_update")
            con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            con.exec_driver_sql(
                "UPDATE provider_profile_revisions SET terms_reference='bad' || char(0)"
            )
            con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, selected, page)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["empty", "rate_limit", "partial"])
async def test_late_policy_change_rejects_empty_error_and_partial_evidence_equally(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine, kind: str
) -> None:
    if kind == "empty":
        page = await fetched(selected, payload=[])
    elif kind == "rate_limit":
        page = await fetched(selected, status=429)
    else:
        page = await fetched(
            selected,
            payload=[
                {"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "USD"},
                {"date": "2026-01-09", "unadjustedValue": "bad-input", "currency": "USD"},
            ],
        )
        assert not page.complete and len(page.rows) == 1
    record = selected["record"]
    assert (
        await client.patch(
            f"{PATH}/eodhd", json=changed(record), headers=version_headers(client, 3)
        )
    ).status_code == 200
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, selected, page)
    assert database_snapshot(engine) == before


async def test_new_upstream_grant_checks_selection_before_any_quota_or_work_mutation(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    from tests.test_upstream_execution import new_work, reserve

    case = new_work(selected, engine)
    assert (
        await client.patch(
            f"{PATH}/eodhd", json=changed(selected["record"]), headers=version_headers(client, 3)
        )
    ).status_code == 200
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        reserve(engine, case)
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM provider_budgets").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 0
        assert con.exec_driver_sql("SELECT attempt_count FROM job_items").scalar_one() == 0


async def test_existing_spending_survives_rejected_post_io_policy_change_without_a_page(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    from spm_api.providers.budgets import TOKEN_UNITS
    from spm_api.providers.execution import abandon_attempt
    from tests.test_upstream_execution import complete, new_work, reserve

    case = new_work(selected, engine)
    grant = reserve(engine, case)
    assert grant.attempt is not None and grant.attempt.created
    page = await fetched(case)
    assert (
        await client.patch(
            f"{PATH}/eodhd", json=changed(selected["record"]), headers=version_headers(client, 3)
        )
    ).status_code == 200
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        assert (
            con.exec_driver_sql("SELECT tokens_num FROM provider_budgets").scalar_one()
            == 4 * TOKEN_UNITS
        )
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM upstream_outcomes").scalar_one() == 0
        abandon_attempt(
            con,
            case["lease"],
            grant.attempt.id,
            at=START + timedelta(seconds=1),
            reason="CONFIG_CHANGED",
        )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT disposition,reason FROM upstream_outcomes").one() == (
            "ABANDONED",
            "CONFIG_CHANGED",
        )
        assert (
            con.exec_driver_sql("SELECT tokens_num FROM provider_budgets").scalar_one()
            == 4 * TOKEN_UNITS
        )


async def test_committed_quota_checkpoint_and_capture_replay_ignore_later_policy_revision(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    from tests.test_upstream_execution import complete, new_work, reserve

    case = new_work(selected, engine)
    grant = reserve(engine, case)
    page = await fetched(case)
    assert grant.attempt is not None
    recorded = complete(engine, case, grant.attempt.id, page)
    assert not recorded.replayed and recorded.receipt.observations_new == 1
    assert (
        await client.patch(
            f"{PATH}/eodhd", json=changed(selected["record"]), headers=version_headers(client, 3)
        )
    ).status_code == 200
    before = database_snapshot(engine)
    assert complete(engine, case, grant.attempt.id, page).replayed
    replay = reserve(engine, case, identifier=grant.attempt.id)
    assert replay.attempt is not None and not replay.attempt.created
    assert database_snapshot(engine) == before


async def test_selection_fence_requires_writer_even_when_only_metadata_is_consulted(
    selected: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(ValueError, match="WRITE_UNIT_OF_WORK_REQUIRED"),
    ):
        validate_selection(con, selected["prepared"])
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("owner", [False, "not-a-server-uuid"])
async def test_private_scope_marker_requires_a_server_uuid(
    selected: dict[str, Any], engine: Engine, owner: Any
) -> None:
    loaded = selected["prepared"].configuration
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="INVALID_PROVIDER_OWNER"):
        replace(loaded, selection_owner_id=owner)
    assert database_snapshot(engine) == before


async def test_default_operator_rechecker_cannot_validate_an_owned_fallback_claim(
    source: dict[str, Any], local: Any, engine: Engine
) -> None:
    selected_store = store(local, engine, source)
    loaded = selected_store.load("eodhd")
    operator = ProviderConfigurationStore(local)
    before = database_snapshot(engine)
    assert selected_store.unchanged(loaded)
    assert not operator.unchanged(loaded)
    assert operator.unchanged(replace(loaded, selection_owner_id=None))
    assert database_snapshot(engine) == before


async def test_ready_wrapper_cannot_turn_a_matching_disabled_decision_into_source_authority(
    selected: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    from spm_api.audit import canonical, digest

    record = selected["record"]
    response = await client.patch(
        f"{PATH}/eodhd",
        json=changed(record, policy={**record["policy"], "enabled": False}),
        headers=version_headers(client, 3),
    )
    assert response.status_code == 200
    prepared = selected["prepared"]
    loaded = prepared.configuration
    assert loaded.profile is not None and loaded.revision is not None
    disabled = loaded.profile.model_copy(
        update={
            "version": 4,
            "revision_id": UUID(response.json()["id"]),
            "enabled": False,
        }
    )
    forged = replace(
        prepared,
        configuration=replace(
            loaded,
            profile=disabled,
            revision=replace(
                loaded.revision,
                profile_sha256=digest(canonical(disabled.model_dump(mode="json"))),
            ),
        ),
    )
    page = await fetched(selected)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        save(engine, {**selected, "prepared": forged}, page)
    assert database_snapshot(engine) == before
