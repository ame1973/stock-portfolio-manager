"""Internal managed provider changes are owned, audited and inert until runtime integration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

import spm_api.providers.profiles as profiles
from spm_api.audit import canonical, digest, stamp
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.providers.configuration import ProviderConfigurationStore, ProviderFile, ProviderPolicy
from spm_api.providers.credentials import CredentialArtifactStore, CredentialRef
from spm_api.providers.profiles import ManagedProfile, ProfileError
from tests.helpers import database_snapshot
from tests.test_provider_persistence import book as book

OWNER, OTHER = uuid4(), uuid4()
START = datetime(2026, 11, 7, tzinfo=UTC)
REASON = "Explicit synthetic owner provider policy decision."
PROVIDERS = (
    "eodhd",
    "alphavantage",
    "massive",
    "yahoo",
    "yfinance",
    "hkex_evidence",
    "sec_evidence",
    "manual",
)


@pytest.fixture
def owners(engine: Engine) -> Engine:
    with unit_of_work(engine, write=True) as con:
        for owner in (OWNER, OTHER):
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic-hash',?,?)",
                (str(owner), stamp(START), stamp(START)),
            )
    return engine


def policy(provider: Any = "eodhd", **changes: Any) -> ProviderPolicy:
    return ProviderPolicy.model_validate({"provider_id": provider, **changes})


def declared(provider: Any = "eodhd", **changes: Any) -> ProviderPolicy:
    return policy(
        provider,
        **{
            "enabled": True,
            "terms_acknowledged": True,
            "terms_reference": "Synthetic rights reference",
            "terms_reason": "Synthetic owner assertion, not verified licensing.",
            "requests_per_minute": 5,
            "requests_per_day": 50,
            **changes,
        },
    )


def save(engine: Engine, body: ProviderPolicy | None = None, **changes: Any) -> ManagedProfile:
    arguments = {
        "owner_id": OWNER,
        "expected_version": None,
        "at": START,
        "reason": REASON,
        "request_id": uuid4(),
        **changes,
    }
    with unit_of_work(engine, write=True) as con:
        return profiles.save(con, policy=body or policy(), **arguments)


def latest(engine: Engine, owner: UUID = OWNER, provider: Any = "eodhd") -> ManagedProfile | None:
    with unit_of_work(engine) as con:
        return profiles.latest(con, owner, provider)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_disabled_create_has_one_revision_and_correlated_audit_only(
    owners: Engine, provider: str
) -> None:
    before = database_snapshot(owners)
    result = save(owners, policy(provider))
    assert result.version == 1 and not result.policy.enabled
    assert result.credential_artifact_id is None and latest(owners, provider=provider) == result
    assert result.change_reason == REASON and result.created_at == START
    after = database_snapshot(owners)
    assert {
        key: rows
        for key, rows in before.items()
        if key not in {"provider_profile_revisions", "audit_events"}
    } == {
        key: rows
        for key, rows in after.items()
        if key not in {"provider_profile_revisions", "audit_events"}
    }
    with unit_of_work(owners) as con:
        assert con.exec_driver_sql(
            "SELECT owner_id,entity_type,entity_id,action,reason,request_id FROM audit_events"
        ).one() == (
            str(OWNER),
            "provider_profile_revision",
            str(result.id),
            "PROVIDER_PROFILE_CREATED",
            REASON,
            str(result.request_id),
        )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_operator_and_managed_policies_reuse_the_same_strict_access_rules(provider: str) -> None:
    body = policy(provider)
    operator = ProviderFile.model_validate({**body.model_dump(mode="json"), "version": 1})
    assert {
        key: operator.model_dump()[key] for key in ProviderPolicy.model_fields
    } == body.model_dump()
    assert body.quota_group == operator.quota_group


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "true"},
        {"enabled": 1},
        {"terms_acknowledged": 1},
        {"raw_payload_storage": True},
        {"requests_per_minute": True},
        {"requests_per_minute": "5"},
        {"requests_per_minute": 0},
        {"requests_per_day": 2147483648},
        {"requests_per_day": 0.1},
        {"terms_acknowledged": True},
        {"api_key": "synthetic-inline-private"},
        {"credential_source": "LOCAL_FILE"},
        {"credential_version": 1},
        {"path": "/private/untrusted.key"},
        {"provider_id": "../eodhd"},
    ],
)
def test_policy_rejects_coercion_secrets_paths_or_incomplete_rights(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as error:
        policy(**changes)
    assert "synthetic-inline-private" not in str(error.value)


@pytest.mark.parametrize("provider", ["hkex_evidence", "sec_evidence", "manual"])
def test_manual_policy_cannot_claim_network_budgets(provider: str) -> None:
    with pytest.raises(ValidationError):
        policy(provider, requests_per_minute=1)


def test_latest_absence_is_a_zero_write_read_not_an_operator_or_file_adoption(
    owners: Engine,
) -> None:
    before = database_snapshot(owners)
    assert latest(owners) is None and latest(owners, OTHER) is None
    assert database_snapshot(owners) == before


@pytest.mark.parametrize(
    "body", [declared("yahoo"), policy(enabled=True, requests_per_minute=5, requests_per_day=50)]
)
def test_first_revision_cannot_enable_access(owners: Engine, body: ProviderPolicy) -> None:
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^INVALID_PROVIDER_PROFILE$"):
        save(owners, body)
    assert database_snapshot(owners) == before


def test_new_duplicate_stale_missing_and_other_owner_changes_never_overwrite(
    owners: Engine,
) -> None:
    first = save(owners)
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^PROFILE_ALREADY_MANAGED$"):
        save(owners)
    with pytest.raises(ProfileError, match="^PROFILE_NOT_FOUND$"):
        save(owners, owner_id=OTHER, expected_version=1)
    assert database_snapshot(owners) == before
    second = save(owners, expected_version=first.version)
    before = database_snapshot(owners)
    with pytest.raises(ApiError) as error:
        save(owners, expected_version=first.version)
    assert error.value.status == 412 and latest(owners) == second
    assert database_snapshot(owners) == before


def test_latest_disabled_or_cleared_revision_never_resurrects_an_enabled_selection(
    owners: Engine,
) -> None:
    save(owners, policy("yahoo"))
    enabled = save(owners, declared("yahoo"), expected_version=1)
    disabled = save(owners, policy("yahoo"), expected_version=2)
    assert enabled.version == 2 and disabled.version == 3
    assert latest(owners, provider="yahoo") == disabled and not disabled.policy.enabled
    with unit_of_work(owners) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM provider_profile_revisions").scalar_one() == 3
        )
        assert con.exec_driver_sql("SELECT count(*) FROM audit_events").scalar_one() == 3


def test_prepared_reference_rotation_keep_and_clear_are_audited_without_reading_secrets(
    owners: Engine, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private-editions"
    directory.mkdir(mode=0o700)
    artifacts = CredentialArtifactStore(settings, directory=directory)
    first = artifacts.prepare("eodhd", SecretStr("synthetic-private-controller-first"))
    second = artifacts.prepare("eodhd", SecretStr("synthetic-private-controller-second"))
    files = {path.name: path.read_bytes() for path in directory.iterdir()}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Private I/O is forbidden in the controller writer")

    monkeypatch.setattr(CredentialArtifactStore, "load", forbidden)
    initial = save(owners, credential=first.reference)
    enabled = save(owners, declared(), expected_version=1)
    rotated = save(owners, declared(), expected_version=2, credential=second.reference)
    disabled = save(owners, policy(), expected_version=3)
    cleared = save(owners, policy(), expected_version=4, credential=None)
    assert (
        initial.credential_artifact_id
        == enabled.credential_artifact_id
        == first.reference.artifact_id
    )
    assert (
        rotated.credential_artifact_id
        == disabled.credential_artifact_id
        == second.reference.artifact_id
    )
    assert cleared.credential_artifact_id is None and latest(owners) == cleared
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == files
    for record in (initial, enabled, rotated, disabled, cleared):
        assert "synthetic-private-controller" not in repr(record)
        assert str(first.reference.artifact_id) not in repr(record)
    with unit_of_work(owners) as con:
        rows = con.exec_driver_sql(
            "SELECT before_sha256,after_sha256,reason FROM audit_events ORDER BY rowid"
        ).all()
        assert rows[0][0] is None and len({row[1] for row in rows}) == 5
        assert all(row[2] == REASON for row in rows)
        serialized = canonical(
            [tuple(row) for row in con.exec_driver_sql("SELECT * FROM audit_events")]
        )
        assert "synthetic-private-controller" not in serialized
        assert digest("synthetic-private-controller-first") not in serialized


@pytest.mark.parametrize("credential", [CredentialRef("massive", uuid4()), "../key", 1])
def test_untrusted_or_mismatched_reference_is_rejected_without_partial_history(
    owners: Engine, credential: Any
) -> None:
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^INVALID_PROFILE_CREDENTIAL$"):
        save(owners, credential=credential)
    assert database_snapshot(owners) == before


@pytest.mark.parametrize("changes", [{"owner_id": OTHER}, {"body": policy("massive")}])
def test_edition_cannot_be_rebound_across_actor_or_provider(
    owners: Engine, changes: dict[str, Any]
) -> None:
    reference = CredentialRef("eodhd", uuid4())
    save(owners, credential=reference)
    before = database_snapshot(owners)
    reference = replace(reference, provider_id="massive") if "body" in changes else reference
    with pytest.raises(ProfileError, match="^PROFILE_CREDENTIAL_SCOPE$"):
        save(owners, credential=reference, **changes)
    assert database_snapshot(owners) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_version": True},
        {"expected_version": 0},
        {"expected_version": 1.0},
        {"expected_version": 2147483648},
        {"at": START.replace(tzinfo=None)},
        {"at": 1},
        {"at": "2026-11-07T00:00:00Z"},
        {"reason": "short"},
        {"reason": "a valid reason\x00hidden"},
        {"request_id": "not-a-uuid"},
    ],
)
def test_invalid_internal_inputs_fail_with_fixed_codes_and_zero_writes(
    owners: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(owners)
    with pytest.raises(ProfileError):
        save(owners, **changes)
    assert database_snapshot(owners) == before


def test_aware_non_utc_clock_is_canonical_and_regression_is_rejected(owners: Engine) -> None:
    observed = START.astimezone(timezone(timedelta(hours=8)))
    first = save(owners, at=observed)
    assert first.created_at == START
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^PROFILE_CLOCK_REGRESSION$"):
        save(owners, expected_version=1, at=START - timedelta(microseconds=1))
    assert database_snapshot(owners) == before


def test_controller_requires_writer_and_active_owner(owners: Engine) -> None:
    before = database_snapshot(owners)
    with (
        pytest.raises(ProfileError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
        unit_of_work(owners) as con,
    ):
        profiles.save(
            con, OWNER, policy(), expected_version=None, at=START, reason=REASON, request_id=uuid4()
        )
    assert database_snapshot(owners) == before
    with unit_of_work(owners, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^PROFILE_OWNER_UNAVAILABLE$"):
        save(owners)
    assert database_snapshot(owners) == before


def test_late_audit_failure_rolls_back_policy_and_all_unrelated_tables(
    owners: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    save(owners)
    before = database_snapshot(owners)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Synthetic late audit failure")

    monkeypatch.setattr(profiles, "audit", fail)
    with pytest.raises(RuntimeError, match="Synthetic late audit failure"):
        save(owners, expected_version=1)
    assert database_snapshot(owners) == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("version", 3),
        ("enabled", 2),
        ("terms_reference", "private\x00hidden"),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2026-11-07T00:00:00.000000+00:00"),
        ("request_id", str(uuid4()).upper()),
    ],
)
def test_corrupt_retained_rows_fail_safely_without_echo_or_repair(
    owners: Engine, column: str, value: Any
) -> None:
    save(owners)
    with owners.begin() as con:
        con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_update")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        con.exec_driver_sql(f"UPDATE provider_profile_revisions SET {column}=?", (value,))
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^PROVIDER_PROFILE_UNAVAILABLE$") as error:
        latest(owners)
    assert "private" not in str(error.value)
    assert database_snapshot(owners) == before


def test_corrupted_keyless_provider_cannot_retain_a_credential_selection(owners: Engine) -> None:
    save(owners, credential=CredentialRef("eodhd", uuid4()))
    with owners.begin() as con:
        con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_update")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        con.exec_driver_sql("UPDATE provider_profile_revisions SET provider_id='yahoo'")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^PROVIDER_PROFILE_UNAVAILABLE$"):
        latest(owners, provider="yahoo")
    assert database_snapshot(owners) == before


def test_corrupted_cross_owner_reference_is_not_a_readable_selection(owners: Engine) -> None:
    reference = CredentialRef("eodhd", uuid4())
    save(owners, credential=reference)
    save(owners, owner_id=OTHER)
    with owners.begin() as con:
        con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_update")
        con.exec_driver_sql(
            "UPDATE provider_profile_revisions SET credential_artifact_id=? WHERE owner_id=?",
            (str(reference.artifact_id), str(OTHER)),
        )
    before = database_snapshot(owners)
    for owner in (OWNER, OTHER):
        with pytest.raises(ProfileError, match="^PROVIDER_PROFILE_UNAVAILABLE$"):
            latest(owners, owner)
    assert database_snapshot(owners) == before


@pytest.mark.parametrize("provider", PROVIDERS)
def test_supported_later_declarations_are_not_adapter_readiness(
    owners: Engine, provider: Any
) -> None:
    save(owners, policy(provider))
    credential: Any = "KEEP"
    if provider in {"eodhd", "alphavantage", "massive"}:
        credential = CredentialRef(provider, uuid4())
    body = (
        declared(provider, requests_per_minute=None, requests_per_day=None)
        if provider in {"hkex_evidence", "sec_evidence", "manual"}
        else declared(provider)
    )
    result = save(owners, body, expected_version=1, credential=credential)
    assert result.version == 2 and result.policy.enabled
    assert latest(owners, provider=provider) == result


@pytest.mark.parametrize(
    "body,credential",
    [
        (declared(), None),
        (policy(enabled=True, requests_per_minute=5, requests_per_day=50), "KEEP"),
        (policy(terms_reference="private\x00hidden"), "KEEP"),
    ],
)
def test_later_invalid_clear_rights_or_metadata_cannot_append(
    owners: Engine, body: ProviderPolicy, credential: Any
) -> None:
    save(owners, credential=CredentialRef("eodhd", uuid4()))
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^INVALID_PROVIDER_PROFILE$"):
        save(owners, body, expected_version=1, credential=credential)
    assert database_snapshot(owners) == before


@pytest.mark.parametrize(
    "owner,provider", [("not-a-uuid", "eodhd"), (OWNER, "../eodhd"), (OWNER, []), (None, "eodhd")]
)
def test_reader_scope_inputs_are_validated_before_sql(
    owners: Engine, owner: Any, provider: Any
) -> None:
    before = database_snapshot(owners)
    with pytest.raises(ProfileError, match="^INVALID_PROFILE_SCOPE$"):
        latest(owners, owner, provider)
    assert database_snapshot(owners) == before


def test_constructed_invalid_policy_and_changed_reference_are_revalidated(owners: Engine) -> None:
    before = database_snapshot(owners)
    forged = ProviderPolicy.model_construct(provider_id="manual", requests_per_day=1)
    with pytest.raises(ProfileError, match="^INVALID_PROVIDER_PROFILE$"):
        save(owners, forged)
    reference = CredentialRef("eodhd", uuid4())
    object.__setattr__(reference, "artifact_id", "../private.key")
    with pytest.raises(ProfileError, match="^INVALID_PROFILE_CREDENTIAL$"):
        save(owners, credential=reference)
    assert database_snapshot(owners) == before


def test_exhausted_version_rejects_before_inserting_or_auditing(
    owners: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = save(owners).model_copy(update={"version": 2147483647})
    before = database_snapshot(owners)
    monkeypatch.setattr(profiles, "latest", lambda *args: previous)
    with pytest.raises(ProfileError, match="^PROFILE_VERSION_EXHAUSTED$"):
        save(owners, expected_version=2147483647)
    assert database_snapshot(owners) == before


def test_two_controller_writers_commit_one_revision_and_one_audit(owners: Engine) -> None:
    save(owners)

    def attempt(_: int) -> int:
        try:
            return save(owners, expected_version=1).version
        except ApiError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [2, 412]
    with unit_of_work(owners) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM provider_profile_revisions").scalar_one() == 2
        )
        assert con.exec_driver_sql("SELECT count(*) FROM audit_events").scalar_one() == 2


@pytest.mark.anyio
async def test_actual_http_financial_and_operator_facts_are_unchanged_by_audited_controller(
    book: dict[str, Any], engine: Engine
) -> None:
    settings = book["settings"]
    operator = ProviderConfigurationStore(settings)
    observed = operator.load("eodhd")
    before = database_snapshot(engine)
    created = save(engine, owner_id=UUID(book["owner"]["id"]))
    disabled = save(engine, owner_id=created.owner_id, expected_version=1)
    after = database_snapshot(engine)
    assert {
        key: rows
        for key, rows in before.items()
        if key not in {"provider_profile_revisions", "audit_events"}
    } == {
        key: rows
        for key, rows in after.items()
        if key not in {"provider_profile_revisions", "audit_events"}
    }
    assert disabled.version == 2 and operator.unchanged(observed)
