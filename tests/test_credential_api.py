"""Private immutable imports bind disabled owned revisions, never source access or income."""

import asyncio
import hmac
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, local
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.mutations as mutations
import spm_api.profile_routes as routes
import spm_api.providers.credentials as credentials
import spm_api.providers.profiles as profiles
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.profile_models import ProviderCredentialImport, ProviderProfileRecord
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.security import COOKIE
from tests.helpers import database_snapshot, version_headers
from tests.test_profile_api import PATH, REASON, changed, create, safe
from tests.test_profile_api import source as source
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio
KEY = "synthetic-managed-import-value-A"
OTHER = "synthetic-managed-import-value-B"


@pytest.fixture
async def vault(source: dict[str, Any], settings: Settings, tmp_path: Path) -> Path:
    root = tmp_path / "managed-credentials"
    root.mkdir(mode=0o700)
    settings.managed_provider_secret_dir = root
    return root


def url(provider: str = "eodhd") -> str:
    return f"{PATH}/{provider}/credential"


def body(value: Any = KEY, **changes: Any) -> dict[str, Any]:
    return {"credential": value, "reason": REASON, **changes}


def bound(
    engine: Engine, source: dict[str, Any], provider: Any = "eodhd"
) -> profiles.ManagedProfile:
    with unit_of_work(engine) as con:
        record = profiles.latest(con, UUID(source["owner"]["id"]), provider)
    assert record is not None
    return record


@pytest.mark.parametrize("provider", ["eodhd", "alphavantage", "massive"])
async def test_import_prepares_exact_private_edition_and_commits_only_owned_disabled_binding(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
    provider: credentials.KeyProvider,
) -> None:
    original = await create(client, provider)
    before = database_snapshot(engine)
    response = await client.post(url(provider), json=body(), headers=version_headers(client, 1))
    assert response.status_code == 200
    result = response.json()
    assert response.headers["etag"] == '"2"' and result["version"] == 2
    assert result["credential_reference_present"] and not result["runtime_selection_installed"]
    assert result["policy"] == original["policy"] and not result["policy"]["enabled"]
    assert result["scope"] == "POLICY_DEFINITION_ONLY"
    safe(response)
    current = bound(engine, source, provider)
    assert current.credential_artifact_id is not None
    reference = credentials.CredentialRef(provider, current.credential_artifact_id)
    reopened = credentials.managed_artifacts(settings).load(reference)
    assert hmac.compare_digest(reopened.credential.get_secret_value(), KEY)
    assert len(list(vault.iterdir())) == 2
    assert vault.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in vault.iterdir())
    assert str(reference.artifact_id) not in response.text and KEY not in response.text
    assert source["store"].unchanged(source["prepared"].configuration)
    after = database_snapshot(engine)
    allowed = {"provider_profile_revisions", "audit_events", "idempotency_keys"}
    assert {key: rows for key, rows in before.items() if key not in allowed} == {
        key: rows for key, rows in after.items() if key not in allowed
    }
    assert KEY not in str(after) and digest(KEY) not in str(after)
    with unit_of_work(engine) as con:
        events = con.exec_driver_sql(
            "SELECT action,entity_id,request_id FROM audit_events "
            "WHERE request_id=? ORDER BY action",
            (response.headers["x-request-id"],),
        ).all()
    assert [tuple(event) for event in events] == [
        ("PROVIDER_CREDENTIAL_IMPORTED", result["id"], response.headers["x-request-id"]),
        ("PROVIDER_PROFILE_REVISED", result["id"], response.headers["x-request-id"]),
    ]


async def test_replay_skips_all_private_io_and_preserves_original_after_rotation_and_clear(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    headers = version_headers(client, 1, key="synthetic-credential-import-public-intent")
    first = await client.post(url(), json=body(), headers=headers)
    assert first.status_code == 200
    second = await client.post(url(), json=body(OTHER), headers=version_headers(client, 2))
    assert second.status_code == 200 and second.json()["version"] == 3
    assert len(list(vault.iterdir())) == 4
    cleared = await client.patch(
        f"{PATH}/eodhd",
        json=changed(second.json(), clear_credential=True),
        headers=version_headers(client, 3),
    )
    assert cleared.status_code == 200 and cleared.json()["version"] == 4
    before = database_snapshot(engine)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A committed credential replay must not touch private storage")

    monkeypatch.setattr(credentials, "managed_artifacts", forbidden)
    settings.managed_provider_secret_dir = None
    replay = await client.post(url(), json=body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["etag"] == '"2"'
    assert (await client.get(f"{PATH}/eodhd")).json()["version"] == 4
    assert database_snapshot(engine) == before and len(list(vault.iterdir())) == 4


async def test_sensitive_fingerprint_uses_actual_value_and_session_not_masked_model_dump(
    vault: Path, client: httpx.AsyncClient, engine: Engine
) -> None:
    await create(client, "eodhd")
    headers = version_headers(client, 1)
    result = await client.post(url(), json=body(), headers=headers)
    assert result.status_code == 200
    before = database_snapshot(engine)
    for payload, reused in (
        (body(OTHER), headers),
        (body(reason="Another explicit owner import reason."), headers),
        (body(), {**headers, "If-Match": '"2"'}),
    ):
        conflict = await client.post(url(), json=payload, headers=reused)
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    fingerprint = hmac.new(
        str(client.cookies.get(COOKIE)).encode(),
        canonical({"version": 1, **body()}).encode(),
        "sha256",
    ).hexdigest()
    with unit_of_work(engine) as con:
        recorded = con.exec_driver_sql(
            "SELECT request_sha256 FROM idempotency_keys WHERE idem_key=?",
            (headers["Idempotency-Key"],),
        ).scalar_one()
    assert recorded == digest(canonical(fingerprint))
    assert database_snapshot(engine) == before and len(list(vault.iterdir())) == 2


async def test_rotation_forces_disabled_policy_and_preserves_previous_file_and_history(
    vault: Path, source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await create(client, "eodhd")
    imported = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert imported.status_code == 200
    previous = bound(engine, source)
    enabled = {
        **imported.json()["policy"],
        "enabled": True,
        "terms_acknowledged": True,
        "terms_reference": "Synthetic owner plan reference",
        "terms_reason": REASON,
        "requests_per_minute": 2,
        "requests_per_day": 20,
    }
    declaration = await client.patch(
        f"{PATH}/eodhd",
        json=changed(imported.json(), policy=enabled),
        headers=version_headers(client, 2),
    )
    assert declaration.status_code == 200 and declaration.json()["policy"]["enabled"]
    rotated = await client.post(url(), json=body(OTHER), headers=version_headers(client, 3))
    assert rotated.status_code == 200 and rotated.json()["version"] == 4
    assert rotated.json()["policy"] == {**enabled, "enabled": False}
    current = bound(engine, source)
    assert current.credential_artifact_id != previous.credential_artifact_id
    assert len(list(vault.iterdir())) == 4
    history = (await client.get(f"{PATH}/eodhd/revisions")).json()["items"]
    assert [item["version"] for item in history] == [4, 3, 2, 1]
    assert [item["credential_reference_present"] for item in history] == [True, True, True, False]
    assert not rotated.json()["runtime_selection_installed"]


@pytest.mark.parametrize("value", ["!", "X" * 256])
async def test_exact_managed_credential_length_boundaries_are_not_trimmed(
    vault: Path, client: httpx.AsyncClient, value: str
) -> None:
    await create(client, "eodhd")
    result = await client.post(url(), json=body(value), headers=version_headers(client, 1))
    assert result.status_code == 200
    assert all(
        hmac.compare_digest(path.read_bytes(), value.encode()) for path in vault.glob("*.key")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"credential": ""},
        {"credential": "X" * 257},
        {"credential": " synthetic-leading-space"},
        {"credential": "synthetic-trailing-space "},
        {"credential": "synthetic-newline\n"},
        {"credential": "synthetic\x00nul"},
        {"credential": "synthetic-非ASCII"},
        {"credential": 123},
        {"credential": False},
        {"credential": None},
        {"credential": {"value": KEY}},
        {"reason": "short"},
        {"reason": "synthetic reason\x00hidden"},
        {"artifact_id": str(uuid4())},
        {"owner_id": str(uuid4())},
        {"path": "/private/client-selected-credential"},
        {"enabled": True},
    ],
)
async def test_invalid_import_never_touches_private_storage_or_echoes_input(
    vault: Path,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    await create(client, "eodhd")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid import must fail before any private I/O")

    monkeypatch.setattr(credentials, "managed_artifacts", forbidden)
    before = database_snapshot(engine)
    rejected = await client.post(url(), json=body(**changes), headers=version_headers(client, 1))
    assert rejected.status_code == 422
    assert KEY not in rejected.text and "client-selected-credential" not in rejected.text
    safe(rejected)
    assert database_snapshot(engine) == before and not list(vault.iterdir())


@pytest.mark.parametrize(
    "provider", ["yahoo", "yfinance", "hkex_evidence", "sec_evidence", "manual"]
)
async def test_keyless_profiles_reject_import_without_file_or_policy_changes(
    vault: Path, client: httpx.AsyncClient, engine: Engine, provider: str
) -> None:
    await create(client, provider)
    before = database_snapshot(engine)
    rejected = await client.post(url(provider), json=body(), headers=version_headers(client, 1))
    assert rejected.status_code == 422
    assert database_snapshot(engine) == before and not list(vault.iterdir())


@pytest.mark.parametrize(
    "failure", ["missing", "stale", "version", "csrf", "origin", "reauth", "revoked"]
)
async def test_preparation_is_not_a_grant_for_missing_stale_or_unauthorized_intent(
    vault: Path, client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
    if failure != "missing":
        await create(client, "eodhd")
    headers = version_headers(client, 1)
    expected = 403
    if failure == "missing":
        expected = 404
    elif failure == "stale":
        headers["If-Match"], expected = '"2"', 412
    elif failure == "version":
        del headers["If-Match"]
        expected = 422
    elif failure == "csrf":
        headers["X-CSRF-Token"] = "incorrect-synthetic-csrf-value"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.invalid"
    else:
        with unit_of_work(engine, write=True) as con:
            if failure == "reauth":
                con.exec_driver_sql("UPDATE sessions SET reauthenticated_at=NULL")
            else:
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
                expected = 401
    before = database_snapshot(engine)
    rejected = await client.post(url(), json=body(), headers=headers)
    assert rejected.status_code == expected
    safe(rejected)
    assert database_snapshot(engine) == before and not list(vault.iterdir())


@pytest.mark.parametrize("failure", ["reauth", "revoked", "csrf"])
async def test_security_is_rechecked_after_private_preparation_and_failed_files_stay_unselected(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await create(client, "eodhd")
    prepare = credentials.CredentialArtifactStore.prepare
    checkpoint: dict[str, Any] = {}

    def changed_after_prepare(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        with unit_of_work(engine, write=True) as con:
            if failure == "reauth":
                con.exec_driver_sql("UPDATE sessions SET reauthenticated_at=NULL")
            elif failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            else:
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", (digest("new-csrf"),))
        checkpoint["before"] = database_snapshot(engine)
        return result

    monkeypatch.setattr(credentials.CredentialArtifactStore, "prepare", changed_after_prepare)
    rejected = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert rejected.status_code == (401 if failure == "revoked" else 403)
    assert database_snapshot(engine) == checkpoint["before"]
    assert len(list(vault.iterdir())) == 2
    assert bound(engine, source).credential_artifact_id is None


@pytest.mark.parametrize("failure", ["audit", "serialization"])
async def test_late_failure_rolls_back_binding_audit_and_idempotency_not_private_files(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await create(client, "eodhd")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic late credential import failure")

    if failure == "audit":
        monkeypatch.setattr(routes, "audit", fail)
    else:
        monkeypatch.setattr(ProviderProfileRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    with pytest.raises(RuntimeError, match="Synthetic late credential import failure"):
        await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert database_snapshot(engine) == before
    assert len(list(vault.iterdir())) == 2 and bound(engine, source).version == 1


@pytest.mark.parametrize("failure", ["unconfigured", "partial", "changed"])
async def test_storage_failure_is_fixed_no_store_and_never_an_import_success(
    vault: Path,
    client: httpx.AsyncClient,
    engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await create(client, "eodhd")
    expected_files = 0
    if failure == "unconfigured":
        settings.managed_provider_secret_dir = None
    elif failure == "partial":
        write = credentials._write
        count = 0

        def fail_marker(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Synthetic private filesystem detail must not escape")
            return write(*args, **kwargs)

        monkeypatch.setattr(credentials, "_write", fail_marker)
        expected_files = 1
    else:
        monkeypatch.setattr(credentials.CredentialArtifactStore, "unchanged", lambda *_: False)
        expected_files = 2
    before = database_snapshot(engine)
    rejected = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert rejected.status_code == 503
    assert rejected.json()["code"] == "PROVIDER_CREDENTIAL_STORAGE_UNAVAILABLE"
    assert "filesystem detail" not in rejected.text and str(vault) not in rejected.text
    safe(rejected)
    assert database_snapshot(engine) == before and len(list(vault.iterdir())) == expected_files


@pytest.mark.parametrize("same_intent", [True, False])
async def test_concurrent_imports_have_one_binding_with_retained_unselected_preparations(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    same_intent: bool,
) -> None:
    await create(client, "eodhd")
    prepare = credentials.CredentialArtifactStore.prepare
    rendezvous = Barrier(2, timeout=10)

    def both_prepared(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        rendezvous.wait()
        return result

    monkeypatch.setattr(credentials.CredentialArtifactStore, "prepare", both_prepared)
    first = version_headers(client, 1)
    second = first if same_intent else version_headers(client, 1)
    replies = await asyncio.gather(
        client.post(url(), json=body(), headers=first),
        client.post(url(), json=body(), headers=second),
    )
    assert sorted(reply.status_code for reply in replies) == (
        [200, 200] if same_intent else [200, 412]
    )
    if same_intent:
        assert replies[0].json() == replies[1].json()
    assert bound(engine, source).version == 2 and len(list(vault.glob("*.ready.json"))) == 2
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='PROVIDER_CREDENTIAL_IMPORTED'"
            ).scalar_one()
            == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM idempotency_keys WHERE route=?", (f"POST {url()}",)
            ).scalar_one()
            == 1
        )


async def test_private_files_are_never_accessed_while_the_mutation_writer_is_open(
    vault: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    state = local()
    real_uow = unit_of_work
    factory, prepare, load = (
        credentials.managed_artifacts,
        credentials.CredentialArtifactStore.prepare,
        credentials.CredentialArtifactStore.load,
    )

    @contextmanager
    def observed_uow(*args: Any, **kwargs: Any) -> Any:
        with real_uow(*args, **kwargs) as con:
            state.writer = bool(kwargs.get("write"))
            try:
                yield con
            finally:
                state.writer = False

    def outside(function: Any) -> Any:
        def checked(*args: Any, **kwargs: Any) -> Any:
            assert not getattr(state, "writer", False)
            return function(*args, **kwargs)

        return checked

    monkeypatch.setattr(mutations, "unit_of_work", observed_uow)
    monkeypatch.setattr(credentials, "managed_artifacts", outside(factory))
    monkeypatch.setattr(credentials.CredentialArtifactStore, "prepare", outside(prepare))
    monkeypatch.setattr(credentials.CredentialArtifactStore, "load", outside(load))
    result = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert result.status_code == 200


async def test_import_does_not_replace_operator_selection_or_probe_a_provider(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    await create(client, "eodhd")
    operator = ProviderConfigurationStore(settings)
    observed = operator.load("eodhd")
    imported = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert imported.status_code == 200 and operator.load("eodhd") == observed
    assert source["store"].unchanged(source["prepared"].configuration)
    health = await client.get("/api/v1/providers/eodhd/health")
    assert health.status_code == 200 and KEY not in health.text


def test_private_input_keeps_masked_serialization_and_repr() -> None:
    value = ProviderCredentialImport.model_validate(body())
    assert value.model_dump(mode="json")["credential"] == "**********"
    assert KEY not in repr(value) and KEY not in value.model_dump_json()


async def test_cached_replay_rechecks_reauthentication_after_its_advisory_read(
    vault: Path,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    headers = version_headers(client, 1)
    original = await client.post(url(), json=body(), headers=headers)
    assert original.status_code == 200
    real_mutate = mutations.mutate
    checkpoint: dict[str, Any] = {}

    def expired_after_preflight(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("UPDATE sessions SET reauthenticated_at=NULL")
        checkpoint["before"] = database_snapshot(engine)
        return real_mutate(*args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A replay must not prepare another private edition")

    monkeypatch.setattr(routes, "mutate", expired_after_preflight)
    monkeypatch.setattr(credentials, "managed_artifacts", forbidden)
    rejected = await client.post(url(), json=body(), headers=headers)
    assert rejected.status_code == 403 and rejected.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == checkpoint["before"] and len(list(vault.iterdir())) == 2


async def test_removed_advisory_replay_fails_closed_without_preparing_inside_writer(
    vault: Path,
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    headers = version_headers(client, 1)
    assert (await client.post(url(), json=body(), headers=headers)).status_code == 200
    real_mutate = mutations.mutate
    checkpoint: dict[str, Any] = {}

    def removed_after_preflight(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "DELETE FROM idempotency_keys WHERE idem_key=?", (headers["Idempotency-Key"],)
            )
        checkpoint["before"] = database_snapshot(engine)
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", removed_after_preflight)
    rejected = await client.post(url(), json=body(), headers=headers)
    assert rejected.status_code == 409 and rejected.json()["code"] == "CREDENTIAL_RETRY_REQUIRED"
    assert database_snapshot(engine) == checkpoint["before"] and len(list(vault.iterdir())) == 2


async def test_failed_complete_preparation_is_never_discovered_or_adopted_on_retry(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    headers = version_headers(client, 1)
    real_audit = audit

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic uncertain import after ready preparation")

    monkeypatch.setattr(routes, "audit", fail)
    before = database_snapshot(engine)
    with pytest.raises(RuntimeError, match="Synthetic uncertain import"):
        await client.post(url(), json=body(), headers=headers)
    abandoned = {path.name for path in vault.iterdir()}
    assert len(abandoned) == 2 and database_snapshot(engine) == before
    monkeypatch.setattr(routes, "audit", real_audit)
    retried = await client.post(url(), json=body(), headers=headers)
    assert retried.status_code == 200 and bound(engine, source).version == 2
    reference = bound(engine, source).credential_artifact_id
    assert all(str(reference) not in name for name in abandoned)
    assert len(list(vault.iterdir())) == 4 and abandoned <= {path.name for path in vault.iterdir()}
    committed = database_snapshot(engine)
    replay = await client.post(url(), json=body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == retried.json()
    assert database_snapshot(engine) == committed and len(list(vault.iterdir())) == 4


async def test_foreign_definition_does_not_authorize_private_import_for_current_owner(
    vault: Path, client: httpx.AsyncClient, engine: Engine
) -> None:
    from spm_api.audit import now
    from tests.test_provider_profiles import policy

    other = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-hash',?,?)",
            (str(other), stamp(), stamp()),
        )
        profiles.save(
            con, other, policy(), expected_version=None, at=now(), reason=REASON, request_id=uuid4()
        )
    before = database_snapshot(engine)
    rejected = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert rejected.status_code == 404
    assert database_snapshot(engine) == before and not list(vault.iterdir())


async def test_changed_owner_after_preparation_cannot_claim_the_old_owners_artifact(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    prepare = credentials.CredentialArtifactStore.prepare
    checkpoint: dict[str, Any] = {}

    def changed_owner(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        other = uuid4()
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic-hash',?,?)",
                (str(other), stamp(), stamp()),
            )
            con.exec_driver_sql("UPDATE sessions SET owner_id=?", (str(other),))
        checkpoint["before"] = database_snapshot(engine)
        return result

    monkeypatch.setattr(credentials.CredentialArtifactStore, "prepare", changed_owner)
    rejected = await client.post(url(), json=body(), headers=version_headers(client, 1))
    assert rejected.status_code == 401
    assert database_snapshot(engine) == checkpoint["before"] and len(list(vault.iterdir())) == 2
    assert bound(engine, source).credential_artifact_id is None


async def test_out_of_domain_version_rejects_before_private_preparation(
    vault: Path, client: httpx.AsyncClient, engine: Engine
) -> None:
    await create(client, "eodhd")
    before = database_snapshot(engine)
    rejected = await client.post(url(), json=body(), headers=version_headers(client, 2147483648))
    assert rejected.status_code == 422
    assert database_snapshot(engine) == before and not list(vault.iterdir())


async def test_exhausted_definition_does_not_prepare_an_unbindable_edition(
    vault: Path,
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create(client, "eodhd")
    exhausted = bound(engine, source).model_copy(update={"version": 2147483647})
    monkeypatch.setattr(profiles, "latest", lambda *_: exhausted)
    before = database_snapshot(engine)
    rejected = await client.post(url(), json=body(), headers=version_headers(client, 2147483647))
    assert rejected.status_code == 409
    assert database_snapshot(engine) == before and not list(vault.iterdir())
