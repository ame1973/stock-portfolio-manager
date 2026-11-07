"""Protected owned definitions expose installed selection without fetching or adopting income."""

import base64
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.profile_routes as routes
import spm_api.providers.profiles as profiles
import spm_api.security as security
from spm_api.audit import now, stamp
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.profile_models import ProviderProfileRecord
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.credentials import CredentialRef
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.test_provider_persistence import book as book
from tests.test_provider_profiles import policy

pytestmark = pytest.mark.anyio
PATH = "/api/v1/provider-profiles"
REASON = "Explicit owner policy definition through the protected API."


@pytest.fixture
async def source(book: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    at = now()
    monkeypatch.setattr(routes, "now", lambda: at)
    return book


def body(provider: str = "yahoo", **changes: Any) -> dict[str, Any]:
    return {"policy": policy(provider).model_dump(mode="json"), "reason": REASON, **changes}


async def create(client: httpx.AsyncClient, provider: str = "yahoo") -> dict[str, Any]:
    response = await client.post(PATH, json=body(provider), headers=write_headers(client))
    assert response.status_code == 201, response.text
    return dict(response.json())


def changed(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {"policy": record["policy"], "reason": REASON, **changes}


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    for field in (
        "owner_id",
        "credential_artifact_id",
        "profile_sha256",
        "profile_json",
        "policy_json",
        "provider_secret_dir",
    ):
        assert field not in response.text


def cursor(value: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()


async def test_disabled_create_read_and_bounded_list_are_owned_audited_and_file_free(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spm_api.providers.configuration import ProviderConfigurationStore
    from spm_api.providers.credentials import CredentialArtifactStore

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Definition HTTP must not load any provider or credential files")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(CredentialArtifactStore, "load", forbidden)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body(), headers=write_headers(client))
    assert response.status_code == 201
    record = response.json()
    safe(response)
    assert response.headers["location"] == f"{PATH}/yahoo" and response.headers["etag"] == '"1"'
    assert record["policy"]["enabled"] is False and record["version"] == 1
    assert (
        record["scope"] == "OWNED_PROVIDER_POLICY" and record["runtime_selection_installed"] is True
    )
    assert record["credential_reference_present"] is False and record["reason"] == REASON
    after = database_snapshot(engine)
    assert {
        key: rows
        for key, rows in before.items()
        if key not in {"provider_profile_revisions", "audit_events", "idempotency_keys"}
    } == {
        key: rows
        for key, rows in after.items()
        if key not in {"provider_profile_revisions", "audit_events", "idempotency_keys"}
    }
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT request_id FROM audit_events WHERE action='PROVIDER_PROFILE_CREATED'"
            ).scalar_one()
            == response.headers["x-request-id"]
        )
    for url in (PATH, f"{PATH}/yahoo", f"{PATH}/yahoo/revisions"):
        result = await client.get(url)
        assert result.status_code == 200
        safe(result)
        assert result.json() == (
            record if url.endswith("/yahoo") else {"items": [record], "next_cursor": None}
        )
    assert database_snapshot(engine) == after


async def test_full_cas_enabled_declaration_and_disable_do_not_dispatch_or_rewrite(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await create(client)
    enabled = {
        **original["policy"],
        "enabled": True,
        "terms_acknowledged": True,
        "terms_reference": "Synthetic user rights reference",
        "terms_reason": REASON,
        "requests_per_minute": 2,
        "requests_per_day": 20,
    }
    result = await client.patch(
        f"{PATH}/yahoo", json=changed(original, policy=enabled), headers=version_headers(client, 1)
    )
    assert result.status_code == 200 and result.headers["etag"] == '"2"'
    record = result.json()
    assert record["policy"]["enabled"] and record["runtime_selection_installed"]
    stale_before = database_snapshot(engine)
    stale = await client.patch(
        f"{PATH}/yahoo", json=changed(original), headers=version_headers(client, 1)
    )
    assert stale.status_code == 412
    assert database_snapshot(engine) == stale_before
    paused = await client.patch(
        f"{PATH}/yahoo",
        json=changed(record, policy={**enabled, "enabled": False}),
        headers=version_headers(client, 2),
    )
    assert paused.status_code == 200 and paused.json()["version"] == 3
    assert paused.json()["policy"]["enabled"] is False
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM jobs").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT enabled FROM provider_profile_revisions WHERE version=2"
            ).scalar_one()
            == 1
        )


async def test_post_patch_replays_keep_original_dto_etag_and_version_fingerprint(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    post_headers = write_headers(client)
    created = await client.post(PATH, json=body(), headers=post_headers)
    original = created.json()
    headers = version_headers(client, 1)
    revised = await client.patch(f"{PATH}/yahoo", json=changed(original), headers=headers)
    current = revised.json()
    await client.patch(f"{PATH}/yahoo", json=changed(current), headers=version_headers(client, 2))
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body(), headers=post_headers)
    assert (
        replay.status_code == 201 and replay.json() == original and replay.headers["etag"] == '"1"'
    )
    replay = await client.patch(f"{PATH}/yahoo", json=changed(original), headers=headers)
    assert (
        replay.status_code == 200 and replay.json() == current and replay.headers["etag"] == '"2"'
    )
    conflict = await client.patch(
        f"{PATH}/yahoo", json=changed(original), headers={**headers, "If-Match": '"2"'}
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before


async def test_reauthentication_is_checked_inside_writer_even_before_cached_replay(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = write_headers(client)
    created = await client.post(PATH, json=body(), headers=headers)
    assert created.status_code == 201
    real_mutate = mutate
    checkpoint: dict[str, Any] = {}

    def expired_between_dependency_and_writer(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("UPDATE sessions SET reauthenticated_at=NULL")
        checkpoint["before"] = database_snapshot(engine)
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", expired_between_dependency_and_writer)
    rejected = await client.post(PATH, json=body(), headers=headers)
    assert rejected.status_code == 403 and rejected.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == checkpoint["before"]
    monkeypatch.setattr(routes, "mutate", real_mutate)
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body(), headers=headers)
    assert replay.status_code == 201 and replay.json() == created.json()
    assert database_snapshot(engine) == before


async def test_exact_expired_reauth_rejects_new_write_but_not_owner_reads(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await create(client)
    at = now() + timedelta(seconds=2)
    monkeypatch.setattr(security, "now", lambda: at)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE sessions SET reauthenticated_at=?", (stamp(at - security.REAUTH_AGE),)
        )
    before = database_snapshot(engine)
    rejected = await client.patch(
        f"{PATH}/yahoo", json=changed(record), headers=version_headers(client, 1)
    )
    assert rejected.status_code == 403 and rejected.json()["code"] == "REAUTH_REQUIRED"
    assert (await client.get(f"{PATH}/yahoo")).status_code == 200
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": "synthetic-private-input-not-echoed"},
        {"credential_artifact_id": str(uuid4())},
        {"owner_id": str(uuid4())},
        {"reason": "short"},
        {"reason": "valid reason\x00hidden"},
        {"policy": {"provider_id": "yahoo", "enabled": "true"}},
        {
            "policy": {
                "provider_id": "yahoo",
                "enabled": True,
                "requests_per_minute": 1,
                "requests_per_day": 5,
            }
        },
        {"policy": {"provider_id": "yahoo", "raw_payload_storage": True}},
        {"policy": {"provider_id": "yahoo", "requests_per_minute": 1.5}},
        {"policy": {"provider_id": "manual", "requests_per_day": 1}},
    ],
)
async def test_invalid_create_rejects_without_echo_or_partial_facts(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    rejected = await client.post(PATH, json=body(**changes), headers=write_headers(client))
    assert rejected.status_code == 422
    assert "synthetic-private-input-not-echoed" not in rejected.text
    safe(rejected)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["0", "01", "-1", "1.0", "1e1", "2147483648", "9" * 1000, "１"])
async def test_history_cursor_is_bounded_canonical_metadata_before_sql(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    await create(client)
    before = database_snapshot(engine)
    response = await client.get(f"{PATH}/yahoo/revisions", params={"cursor": cursor(value)})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    assert database_snapshot(engine) == before


async def test_history_keyset_returns_exact_newest_physical_versions(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    records = [await create(client)]
    for version in (1, 2):
        response = await client.patch(
            f"{PATH}/yahoo", json=changed(records[-1]), headers=version_headers(client, version)
        )
        assert response.status_code == 200
        records.append(response.json())
    before = database_snapshot(engine)
    first = await client.get(f"{PATH}/yahoo/revisions", params={"limit": 2})
    assert first.status_code == 200 and first.json()["items"] == list(reversed(records[1:]))
    second = await client.get(
        f"{PATH}/yahoo/revisions", params={"limit": 2, "cursor": first.json()["next_cursor"]}
    )
    assert second.status_code == 200 and second.json() == {
        "items": records[:1],
        "next_cursor": None,
    }
    assert database_snapshot(engine) == before


async def test_foreign_owner_definitions_are_not_adopted_or_exposed(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    other = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-hash',?,?)",
            (str(other), stamp(), stamp()),
        )
        profiles.save(
            con,
            other,
            policy("yahoo"),
            expected_version=None,
            at=now(),
            reason=REASON,
            request_id=uuid4(),
        )
    before = database_snapshot(engine)
    assert (await client.get(PATH)).json() == {"items": [], "next_cursor": None}
    for url in (f"{PATH}/yahoo", f"{PATH}/yahoo/revisions"):
        assert (await client.get(url)).status_code == 404
    assert (
        await client.patch(f"{PATH}/yahoo", json=body(), headers=version_headers(client, 1))
    ).status_code == 404
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("url", [PATH, f"{PATH}/yahoo", f"{PATH}/yahoo/revisions"])
async def test_anonymous_definition_reads_are_protected(
    source: dict[str, Any], client: httpx.AsyncClient, url: str
) -> None:
    client.cookies.clear()
    result = await client.get(url)
    assert result.status_code == 401
    safe(result)


async def test_provider_identity_cannot_move_and_missing_cas_fails(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await create(client)
    before = database_snapshot(engine)
    mismatch = await client.patch(
        f"{PATH}/yahoo", json=body("eodhd"), headers=version_headers(client, 1)
    )
    assert mismatch.status_code == 422
    missing = await client.patch(f"{PATH}/yahoo", json=body(), headers=write_headers(client))
    assert missing.status_code == 422
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["csrf", "origin", "reauth", "revoked"])
async def test_mutation_security_fails_without_profile_audit_or_idempotency(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine, failure: str
) -> None:
    headers = write_headers(client)
    if failure == "csrf":
        headers["X-CSRF-Token"] = "wrong-token-at-least-sixteen-characters"
    elif failure == "origin":
        headers["Origin"] = "https://untrusted.invalid"
    else:
        with unit_of_work(engine, write=True) as con:
            if failure == "reauth":
                con.exec_driver_sql("UPDATE sessions SET reauthenticated_at=NULL")
            else:
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
    before = database_snapshot(engine)
    result = await client.post(PATH, json=body(), headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == before


async def test_only_reference_presence_is_exposed_and_clearing_keeps_operator_files(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = await create(client, "eodhd")
    reference = CredentialRef("eodhd", uuid4())
    operator = ProviderConfigurationStore(source["settings"])
    observed = operator.load("eodhd")
    with unit_of_work(engine, write=True) as con:
        profiles.save(
            con,
            UUID(source["owner"]["id"]),
            policy("eodhd"),
            expected_version=1,
            at=datetime.fromisoformat(original["created_at"]),
            reason=REASON,
            request_id=uuid4(),
            credential=reference,
        )
    current = await client.get(f"{PATH}/eodhd")
    assert current.json()["credential_reference_present"] is True
    assert current.json()["runtime_selection_installed"] is True
    assert str(reference.artifact_id) not in current.text
    safe(current)
    cleared = await client.patch(
        f"{PATH}/eodhd",
        json=changed(current.json(), clear_credential=True),
        headers=version_headers(client, 2),
    )
    assert cleared.status_code == 200 and cleared.json()["version"] == 3
    assert cleared.json()["credential_reference_present"] is False and operator.unchanged(observed)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT credential_artifact_id FROM provider_profile_revisions WHERE version=2"
        ).scalar_one() == str(reference.artifact_id)


@pytest.mark.parametrize("failure", ["audit", "serialization"])
async def test_late_response_or_audit_failure_rolls_back_all_mutation_facts(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic protected-profile late failure")

    if failure == "audit":
        monkeypatch.setattr(profiles, "audit", fail)
    else:
        monkeypatch.setattr(ProviderProfileRecord, "model_dump_json", fail)
    before = database_snapshot(engine)
    with pytest.raises(RuntimeError, match="Synthetic protected-profile late failure"):
        await client.post(PATH, json=body(), headers=write_headers(client))
    assert database_snapshot(engine) == before


async def test_public_idempotency_key_is_not_a_client_selected_intent_uuid(
    source: dict[str, Any], client: httpx.AsyncClient
) -> None:
    result = await client.post(
        PATH,
        json=body(),
        headers=write_headers(client, key="synthetic-managed-policy-public-intent"),
    )
    assert result.status_code == 201
    safe(result)


async def test_corrupt_stored_policy_returns_fixed_503_without_echo_or_repair(
    source: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    await create(client)
    with engine.begin() as con:
        con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_update")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        con.exec_driver_sql(
            "UPDATE provider_profile_revisions SET terms_reference=?", ("private-secret\x00hidden",)
        )
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(engine)
    for url in (PATH, f"{PATH}/yahoo", f"{PATH}/yahoo/revisions"):
        response = await client.get(url)
        assert (
            response.status_code == 503
            and response.json()["code"] == "PROVIDER_PROFILE_UNAVAILABLE"
        )
        assert "private-secret" not in response.text
        safe(response)
    assert database_snapshot(engine) == before
