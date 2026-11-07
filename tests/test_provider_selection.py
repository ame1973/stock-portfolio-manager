"""Owned managed selection never revives older/operator access or fabricates file provenance."""

import hmac
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import local as ThreadLocal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.providers.configuration as configuration
import spm_api.providers.credentials as credentials
import spm_api.providers.profiles as profiles
import spm_api.providers.selection as selection
from spm_api.audit import canonical, digest, now, stamp
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.providers.budgets import load_budget
from spm_api.providers.configuration import (
    ConfigurationRevision,
    ManagedConfigurationRevision,
    ManagedPolicySnapshot,
    ProviderConfigurationStore,
    ProviderFile,
)
from spm_api.providers.persistence import capture_page, prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.selection import OwnerConfigurationStore
from tests.helpers import database_snapshot, version_headers
from tests.test_profile_api import PATH, REASON, changed, create
from tests.test_profile_api import source as source
from tests.test_provider_configuration import KEY as OPERATOR_KEY
from tests.test_provider_configuration import policy as file_policy
from tests.test_provider_configuration import provision
from tests.test_provider_persistence import FINISH, START
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio
KEY = "synthetic-managed-selection-key-A"
OTHER = "synthetic-managed-selection-key-B"


@pytest.fixture
async def local(source: dict[str, Any], settings: Settings, tmp_path: Path) -> Settings:
    settings.provider_secret_dir = source["settings"].provider_secret_dir
    root = tmp_path / "managed-private"
    root.mkdir(mode=0o700)
    settings.managed_provider_secret_dir = root
    return settings


def store(settings: Settings, engine: Engine, source: dict[str, Any]) -> OwnerConfigurationStore:
    return OwnerConfigurationStore(settings, engine, UUID(source["owner"]["id"]))


async def manage(
    client: httpx.AsyncClient, provider: str = "eodhd", *, minute: int = 5, day: int = 50
) -> dict[str, Any]:
    record = await create(client, provider)
    if provider in ("eodhd", "alphavantage", "massive"):
        imported = await client.post(
            f"{PATH}/{provider}/credential",
            json={"credential": KEY, "reason": REASON},
            headers=version_headers(client, 1),
        )
        assert imported.status_code == 200
        record = imported.json()
    policy = {
        **record["policy"],
        "enabled": True,
        "terms_acknowledged": True,
        "terms_reference": "Synthetic owner rights reference, not verified licensing",
        "terms_reason": REASON,
    }
    if provider in ("eodhd", "alphavantage", "massive", "yahoo", "yfinance"):
        policy.update(requests_per_minute=minute, requests_per_day=day)
    result = await client.patch(
        f"{PATH}/{provider}",
        json=changed(record, policy=policy),
        headers=version_headers(client, record["version"]),
    )
    assert result.status_code == 200 and result.json()["runtime_selection_installed"] is True
    return dict(result.json())


async def test_no_definition_reuses_actual_operator_snapshot_without_writes(
    local: Settings, source: dict[str, Any], engine: Engine
) -> None:
    selected_store = store(local, engine, source)
    before = database_snapshot(engine)
    expected = ProviderConfigurationStore(local).load("eodhd")
    selected = selected_store.load("eodhd")
    assert replace(selected, selection_owner_id=None) == expected and selected.error_code is None
    assert selected.selection_owner_id == UUID(source["owner"]["id"])
    assert isinstance(selected.profile, ProviderFile)
    assert isinstance(selected.revision, ConfigurationRevision)
    assert selected_store.unchanged(selected) and database_snapshot(engine) == before


async def test_latest_disabled_definition_blocks_ready_operator_without_any_private_reads(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = ProviderConfigurationStore(local).load("eodhd")
    assert operator.error_code is None
    definition = await create(client, "eodhd")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Disabled managed selection must not fall back or read a key")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(credentials.CredentialArtifactStore, "load", forbidden)
    selected_store = store(local, engine, source)
    before = database_snapshot(engine)
    selected = selected_store.load("eodhd")
    assert selected.error_code == "PROVIDER_DISABLED" and not selected.health.configured
    assert isinstance(selected.profile, ManagedPolicySnapshot)
    assert selected.profile.revision_id == UUID(definition["id"])
    assert selected.profile.version == 1 and selected.revision is None
    assert not selected.access.enabled and selected.access.credential is None
    assert not selected_store.unchanged(operator) and database_snapshot(engine) == before


async def test_absent_or_inactive_owner_never_receives_operator_fallback(
    local: Settings, source: dict[str, Any], engine: Engine
) -> None:
    missing = OwnerConfigurationStore(local, engine, uuid4())
    before = database_snapshot(engine)
    assert missing.load("eodhd").error_code == "PROVIDER_OWNER_UNAVAILABLE"
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0")
    before = database_snapshot(engine)
    inactive = store(local, engine, source)
    assert inactive.load("eodhd").error_code == "PROVIDER_OWNER_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("provider", ["eodhd", "alphavantage", "massive"])
async def test_enabled_owned_selection_retains_real_definition_and_exact_edition_not_file_versions(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    provider: Any,
) -> None:
    record = await manage(client, provider)
    selected_store = store(local, engine, source)
    before = database_snapshot(engine)
    loaded = selected_store.load(provider)
    assert loaded.error_code is None and loaded.health.configured
    assert loaded.health.reason == "CONFIGURATION_LOADED_NOT_PROBED"
    assert isinstance(loaded.profile, ManagedPolicySnapshot)
    assert isinstance(loaded.revision, ManagedConfigurationRevision)
    assert loaded.profile.owner_id == UUID(source["owner"]["id"])
    assert loaded.profile.revision_id == UUID(record["id"]) and loaded.profile.version == 3
    assert loaded.profile.configuration_source == "MANAGED_PROFILE"
    assert loaded.profile.credential_artifact_id is not None
    actual = credentials.managed_artifacts(local).load(
        credentials.CredentialRef(provider, loaded.profile.credential_artifact_id)
    )
    assert loaded.revision.credential_files == actual.revision
    assert loaded.revision.profile_sha256 == digest(
        canonical(loaded.profile.model_dump(mode="json"))
    )
    assert loaded.revision.profile_sha256 != digest(KEY)
    assert loaded.access.credential is not None
    assert hmac.compare_digest(loaded.access.credential.get_secret_value(), KEY)
    assert selected_store.unchanged(loaded)
    assert KEY not in repr(loaded) and str(loaded.profile.owner_id) not in repr(loaded.profile)
    assert str(loaded.profile.credential_artifact_id) not in repr(loaded.profile)
    async with configured_adapter(loaded) as adapter:
        assert (await adapter.healthcheck()).configured
    assert database_snapshot(engine) == before
    for field, value in {"version": 4, "owner_id": uuid4()}.items():
        with pytest.raises(ValidationError):
            setattr(loaded.profile, field, value)


@pytest.mark.parametrize(
    "provider", ["yahoo", "yfinance", "hkex_evidence", "sec_evidence", "manual"]
)
async def test_keyless_and_manual_managed_profiles_do_not_require_or_read_private_storage(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
) -> None:
    await manage(client, provider)
    local.managed_provider_secret_dir = None

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Keyless selection must not read an operator or managed key")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(credentials, "managed_artifacts", forbidden)
    selected_store = store(local, engine, source)
    before = database_snapshot(engine)
    loaded = selected_store.load(provider)
    assert loaded.error_code is None and isinstance(loaded.profile, ManagedPolicySnapshot)
    assert loaded.profile.credential_artifact_id is None and loaded.access.credential is None
    assert isinstance(loaded.revision, ManagedConfigurationRevision)
    assert loaded.revision.credential_files is None and selected_store.unchanged(loaded)
    assert database_snapshot(engine) == before


async def test_imported_disabled_edition_does_not_enable_selection_or_load_a_key(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    result = await client.post(
        f"{PATH}/eodhd/credential",
        json={"credential": KEY, "reason": REASON},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Import is not permission to load or use a credential")

    monkeypatch.setattr(credentials, "managed_artifacts", forbidden)
    before = database_snapshot(engine)
    loaded = store(local, engine, source).load("eodhd")
    assert loaded.error_code == "PROVIDER_DISABLED" and loaded.access.credential is None
    assert isinstance(loaded.profile, ManagedPolicySnapshot)
    assert loaded.profile.version == 2 and loaded.profile.credential_artifact_id is not None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "failure", ["unconfigured", "missing_root", "missing_file", "mode", "overlap"]
)
async def test_unavailable_managed_edition_never_falls_back_to_ready_operator(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    failure: str,
) -> None:
    await manage(client)
    selected_store = store(local, engine, source)
    observed = selected_store.load("eodhd")
    assert observed.error_code is None
    root = local.managed_provider_secret_dir
    assert root is not None
    if failure == "unconfigured":
        local.managed_provider_secret_dir = None
    elif failure == "missing_root":
        root.rename(root.with_name("retained-private-root"))
    elif failure == "missing_file":
        key = next(root.glob("*.key"))
        key.rename(key.with_suffix(".retained"))
    elif failure == "mode":
        next(root.glob("*.key")).chmod(0o644)
    else:
        local.managed_provider_secret_dir = local.data_dir
    before = database_snapshot(engine)
    loaded = selected_store.load("eodhd")
    assert loaded.error_code is not None and not loaded.health.configured
    assert isinstance(loaded.profile, ManagedPolicySnapshot) and loaded.profile.version == 3
    assert loaded.access.credential is None and not selected_store.unchanged(observed)
    operator = ProviderConfigurationStore(local).load("eodhd")
    assert operator.error_code is None and operator.access.credential is not None
    assert hmac.compare_digest(operator.access.credential.get_secret_value(), OPERATOR_KEY.decode())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("cleared", [True, False])
async def test_latest_disabled_or_cleared_revision_never_revives_prior_enabled_access(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    cleared: bool,
) -> None:
    record = await manage(client)
    selected_store = store(local, engine, source)
    observed = selected_store.load("eodhd")
    result = await client.patch(
        f"{PATH}/eodhd",
        json=changed(
            record, policy={**record["policy"], "enabled": False}, clear_credential=cleared
        ),
        headers=version_headers(client, 3),
    )
    assert result.status_code == 200 and result.json()["version"] == 4
    before = database_snapshot(engine)
    loaded = selected_store.load("eodhd")
    assert loaded.error_code == "PROVIDER_DISABLED" and isinstance(
        loaded.profile, ManagedPolicySnapshot
    )
    assert loaded.profile.version == 4 and loaded.profile.revision_id == UUID(result.json()["id"])
    assert (loaded.profile.credential_artifact_id is None) is cleared
    assert not selected_store.unchanged(observed) and database_snapshot(engine) == before


async def test_readable_operator_and_owned_keys_do_not_change_the_default_runtime_or_api_flag(
    local: Settings, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    operator = ProviderConfigurationStore(local).load("eodhd")
    await manage(client)
    before = database_snapshot(engine)
    owned = store(local, engine, source).load("eodhd")
    assert owned.access.credential is not None and operator.access.credential is not None
    assert not hmac.compare_digest(
        owned.access.credential.get_secret_value(), operator.access.credential.get_secret_value()
    )
    assert ProviderConfigurationStore(local).load("eodhd") == operator
    assert (await client.get(f"{PATH}/eodhd")).json()["runtime_selection_installed"] is True
    assert database_snapshot(engine) == before


async def test_another_owners_definition_is_not_an_owned_binding_or_a_credential_vote(
    local: Settings, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await manage(client)
    other = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-hash',?,?)",
            (str(other), stamp(), stamp()),
        )
    before = database_snapshot(engine)
    other_loaded = OwnerConfigurationStore(local, engine, other).load("eodhd")
    assert other_loaded.error_code is None and isinstance(other_loaded.profile, ProviderFile)
    assert other_loaded.access.credential is not None
    assert hmac.compare_digest(
        other_loaded.access.credential.get_secret_value(), OPERATOR_KEY.decode()
    )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("changed", ["disabled", "owner"])
async def test_metadata_change_while_reading_private_edition_fails_closed(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    await manage(client)
    real_load = credentials.CredentialArtifactStore.load
    checkpoint: dict[str, Any] = {}

    def concurrent_change(*args: Any, **kwargs: Any) -> Any:
        loaded = real_load(*args, **kwargs)
        with unit_of_work(engine, write=True) as con:
            if changed == "disabled":
                current = profiles.latest(con, UUID(source["owner"]["id"]), "eodhd")
                assert current is not None
                profiles.save(
                    con,
                    current.owner_id,
                    current.policy.model_copy(update={"enabled": False}),
                    expected_version=3,
                    at=current.created_at,
                    reason=REASON,
                    request_id=uuid4(),
                )
            else:
                con.exec_driver_sql("UPDATE owners SET is_active=0")
        checkpoint["before"] = database_snapshot(engine)
        return loaded

    monkeypatch.setattr(credentials.CredentialArtifactStore, "load", concurrent_change)
    loaded = store(local, engine, source).load("eodhd")
    assert loaded.error_code == (
        "CONFIG_CHANGED_DURING_READ" if changed == "disabled" else "PROVIDER_OWNER_UNAVAILABLE"
    )
    assert loaded.access.credential is None and database_snapshot(engine) == checkpoint["before"]


async def test_new_managed_definition_during_operator_read_invalidates_old_fallback(
    local: Settings,
    source: dict[str, Any],
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_provider_profiles import policy

    real_load = ProviderConfigurationStore.load
    checkpoint: dict[str, Any] = {}

    def created_while_reading(*args: Any, **kwargs: Any) -> Any:
        loaded = real_load(*args, **kwargs)
        with unit_of_work(engine, write=True) as con:
            profiles.save(
                con,
                UUID(source["owner"]["id"]),
                policy(),
                expected_version=None,
                at=now(),
                reason=REASON,
                request_id=uuid4(),
            )
        checkpoint["before"] = database_snapshot(engine)
        return loaded

    monkeypatch.setattr(ProviderConfigurationStore, "load", created_while_reading)
    result = store(local, engine, source).load("eodhd")
    assert result.error_code == "CONFIG_CHANGED_DURING_READ" and result.access.credential is None
    assert database_snapshot(engine) == checkpoint["before"]


async def test_same_reported_file_signatures_still_require_actual_constant_time_key_comparison(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await manage(client)
    selected_store = store(local, engine, source)
    observed = selected_store.load("eodhd")
    assert isinstance(observed.revision, ManagedConfigurationRevision)
    root = local.managed_provider_secret_dir
    assert root is not None
    signatures = {
        path.stat().st_ino: configuration._signature(path.stat()) for path in root.iterdir()
    }
    next(root.glob("*.key")).write_bytes(OTHER.encode())
    monkeypatch.setattr(configuration, "_signature", lambda info: signatures[info.st_ino])
    before = database_snapshot(engine)
    current = selected_store.load("eodhd")
    assert current.error_code is None and current.revision == observed.revision
    assert not selected_store.unchanged(observed) and database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["database", "stored_policy"])
async def test_database_or_retained_policy_failure_cannot_recover_through_operator_files(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await manage(client)
    if failure == "database":

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("Synthetic private database details must not escape")

        monkeypatch.setattr(selection, "unit_of_work", unavailable)
    else:
        with engine.begin() as con:
            con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_update")
            con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            con.exec_driver_sql(
                "UPDATE provider_profile_revisions SET terms_reference='bad' || char(0)"
            )
            con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(engine)
    loaded = store(local, engine, source).load("eodhd")
    assert loaded.error_code == "MANAGED_PROFILE_UNAVAILABLE"
    assert not loaded.access.enabled and loaded.access.credential is None
    assert "private database" not in repr(loaded)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("owner", [None, False, "not-an-owner", str(uuid4())])
async def test_owner_identity_requires_a_server_checked_uuid_before_any_read(
    local: Settings, engine: Engine, owner: Any
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="INVALID_PROVIDER_OWNER"):
        OwnerConfigurationStore(local, engine, owner)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("provider", ["not-a-provider"])
async def test_unknown_provider_is_rejected_without_file_or_database_probe(
    local: Settings, source: dict[str, Any], engine: Engine, provider: Any
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="Unsupported provider configuration"):
        store(local, engine, source).load(provider)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("managed", [True, False])
async def test_private_readers_run_after_all_owned_metadata_transactions_close(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
) -> None:
    if managed:
        await manage(client)
    active = ThreadLocal()
    real_uow = unit_of_work
    real_operator, real_private, factory = (
        ProviderConfigurationStore.load,
        credentials.CredentialArtifactStore.load,
        credentials.managed_artifacts,
    )

    @contextmanager
    def tracked(*args: Any, **kwargs: Any) -> Any:
        with real_uow(*args, **kwargs) as con:
            active.transaction = True
            try:
                yield con
            finally:
                active.transaction = False

    def outside(function: Any) -> Any:
        def checked(*args: Any, **kwargs: Any) -> Any:
            assert not getattr(active, "transaction", False)
            return function(*args, **kwargs)

        return checked

    monkeypatch.setattr(selection, "unit_of_work", tracked)
    monkeypatch.setattr(ProviderConfigurationStore, "load", outside(real_operator))
    monkeypatch.setattr(credentials.CredentialArtifactStore, "load", outside(real_private))
    monkeypatch.setattr(credentials, "managed_artifacts", outside(factory))
    result = store(local, engine, source).load("eodhd")
    assert result.error_code is None


async def test_shared_yahoo_limits_use_owned_decisions_and_operator_peers_without_sdk_votes(
    local: Settings, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    provision(
        local, "yfinance", body=file_policy("yfinance", requests_per_minute=2, requests_per_day=20)
    )
    await manage(client, "yahoo", minute=5, day=50)
    selected_store = store(local, engine, source)
    before = database_snapshot(engine)
    budget = load_budget(selected_store, "yahoo")
    assert budget.limits.origin_group == "YAHOO"
    assert budget.limits.requests_per_minute == 2 and budget.limits.requests_per_day == 20
    assert len(budget.limits.members) == 2 and budget.unchanged(selected_store)
    assert isinstance(budget.primary.profile, ManagedPolicySnapshot)
    assert database_snapshot(engine) == before
    await create(client, "yfinance")
    checkpoint = database_snapshot(engine)
    assert not budget.unchanged(selected_store)
    current = load_budget(selected_store, "yahoo")
    assert current.limits.requests_per_minute == 5 and len(current.limits.members) == 1
    assert database_snapshot(engine) == checkpoint


async def test_disabled_managed_peer_revision_is_observed_even_while_still_unconfigured(
    local: Settings, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await manage(client, "yahoo")
    peer = await create(client, "yfinance")
    selected_store = store(local, engine, source)
    snapshot = load_budget(selected_store, "yahoo")
    assert snapshot.unchanged(selected_store)
    revised = await client.patch(
        f"{PATH}/yfinance", json=changed(peer), headers=version_headers(client, 1)
    )
    assert revised.status_code == 200
    before = database_snapshot(engine)
    assert not snapshot.unchanged(selected_store) and database_snapshot(engine) == before


async def test_cross_owner_prepared_context_rejects_before_capture_or_network(
    local: Settings, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await manage(client)
    loaded = store(local, engine, source).load("eodhd")
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_OWNER_MISMATCH"):
        prepare_fetch(uuid4(), loaded, source["prepared"].segment, "DIVIDENDS")
    assert database_snapshot(engine) == before


async def test_actual_adapter_uses_owned_key_and_retains_exact_managed_provenance(
    local: Settings, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await manage(client)
    loaded = store(local, engine, source).load("eodhd")
    prepared = prepare_fetch(
        UUID(source["owner"]["id"]), loaded, source["prepared"].segment, "DIVIDENDS"
    )
    metadata = prepared.metadata()
    assert json.loads(metadata["profile_json"])["configuration_source"] == "MANAGED_PROFILE"
    assert KEY not in metadata["profile_json"] and digest(KEY) not in metadata["profile_json"]
    before = database_snapshot(engine)
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        assert hmac.compare_digest(request.url.params["api_token"], KEY)
        calls.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(
                b'[{"date":"2026-01-08","unadjustedValue":"0.9","currency":"USD"}]'
            ),
        )

    async with configured_adapter(loaded, transport=httpx.MockTransport(upstream)) as adapter:
        page = await adapter.fetch_dividends(prepared.segment)
    assert calls == ["/api/div/0005.HK"] and page.rows[0].dps == "0.90000000"
    assert store(local, engine, source).unchanged(loaded) and database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        receipt = capture_page(
            con,
            prepared,
            page,
            run_id=uuid4(),
            started_at=START,
            finished_at=FINISH,
            duration_ms=12,
        )
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


async def test_invalid_internal_rights_snapshot_cannot_grant_access(
    local: Settings,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await manage(client)
    with unit_of_work(engine) as con:
        record = profiles.latest(con, UUID(source["owner"]["id"]), "eodhd")
    assert record is not None
    malformed = record.model_copy(
        update={"policy": record.policy.model_copy(update={"terms_acknowledged": False})}
    )
    monkeypatch.setattr(OwnerConfigurationStore, "_current", lambda *_: malformed)
    before = database_snapshot(engine)
    result = store(local, engine, source).load("eodhd")
    assert result.error_code == "RIGHTS_NOT_ACKNOWLEDGED" and not result.access.enabled
    assert database_snapshot(engine) == before
