"""Read-only provider files enforce policy and never expose secrets in diagnostics."""

import json
import os
import stat
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

import spm_api.providers.configuration as files
from spm_api.cli import main
from spm_api.config import Settings
from spm_api.providers.base import FetchState, registry
from spm_api.providers.configuration import (
    LoadedConfiguration,
    ProviderConfigurationStore,
    ProviderFile,
)
from spm_api.providers.http import ProviderAccess
from spm_api.providers.runtime import check_configurations, configured_adapter
from spm_api.providers.symbols import AliasPeriod, AliasSegment, FetchWindow
from tests.helpers import database_snapshot

KEY = b"synthetic-file-test-key"
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")


@pytest.fixture
def local(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        provider_secret_dir=tmp_path / "credentials",
    )


def policy(provider: Any = "eodhd", **changes: Any) -> dict[str, Any]:
    mode = registry()[provider].access_mode
    return {
        "provider_id": provider,
        "version": 1,
        "enabled": True,
        "terms_acknowledged": True,
        "terms_reference": "Synthetic operator plan/terms reference",
        "terms_reason": "Acknowledged the synthetic terms for this isolated software test.",
        **({"requests_per_minute": 5, "requests_per_day": 50} if mode != "MANUAL_EVIDENCE" else {}),
        **(
            {"credential_source": "LOCAL_FILE", "credential_version": 1}
            if mode == "API_KEY"
            else {}
        ),
        **changes,
    }


def provision(
    local: Settings, provider: Any = "eodhd", *, body: Any = None, key: bytes = KEY
) -> tuple[Path, Path]:
    root = local.data_dir / "provider-config"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    local.provider_secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    local.provider_secret_dir.chmod(0o700)
    profile = root / f"{provider}.json"
    data = policy(provider) if body is None else body
    profile.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    profile.chmod(0o600)
    credential = local.provider_secret_dir / f"{provider}.1.key"
    credential.write_bytes(key)
    credential.chmod(0o600)
    return profile, credential


def segment(provider: Any = "eodhd", market: Any = "HK") -> AliasSegment:
    return AliasSegment(
        alias=AliasPeriod(
            id=UUID("00000000-0000-4000-8000-000000000002"),
            security_id=UUID("00000000-0000-4000-8000-000000000001"),
            provider_id=provider,
            provider_symbol="0005.HK",
            effective_from="2020-01-01",
        ),
        market=market,
        exchange_timezone="Asia/Hong_Kong",
        window=WINDOW,
    )


def test_missing_profiles_default_unconfigured_and_do_not_create_any_files(local: Settings) -> None:
    store = ProviderConfigurationStore(local)
    for provider in registry():
        loaded = store.load(provider)
        assert loaded.error_code == "CONFIG_NOT_FOUND"
        assert loaded.health.state == FetchState.UNCONFIGURED and not loaded.health.configured
        assert loaded.access.credential is None and not loaded.access.enabled
        assert not store.unchanged(loaded)
    assert not local.data_dir.exists() and not local.provider_secret_dir.exists()
    default = LoadedConfiguration("eodhd")
    assert default.health.reason == "CONFIG_NOT_LOADED" and not default.health.configured
    with pytest.raises(ValueError, match="Ready configuration"):
        LoadedConfiguration("eodhd", error_code=None)


def test_valid_private_files_yield_frozen_server_access_but_no_live_health_claim(
    local: Settings,
) -> None:
    profile, credential = provision(local)
    before = (profile.read_bytes(), credential.read_bytes(), profile.stat(), credential.stat())
    store = ProviderConfigurationStore(local)
    loaded = store.load("eodhd")
    assert loaded.error_code is None and loaded.profile is not None and loaded.revision is not None
    assert loaded.profile.requests_per_minute == 5 and loaded.profile.requests_per_day == 50
    assert loaded.profile.quota_group == "EODHD" and loaded.profile.raw_payload_storage is False
    assert (
        loaded.access.credential is not None
        and loaded.access.credential.get_secret_value() == KEY.decode()
    )
    assert (
        loaded.health.state == FetchState.STALE
        and loaded.health.reason == "CONFIGURATION_LOADED_NOT_PROBED"
    )
    assert loaded.health.live_validation == "NOT_TESTED" and loaded.health.last_success_at is None
    assert loaded.revision.profile_sha256 == sha256(profile.read_bytes()).hexdigest()
    assert loaded.revision.profile_sha256 != sha256(KEY).hexdigest()
    assert KEY.decode() not in repr(loaded) + loaded.health.model_dump_json()
    assert "credential" not in loaded.health.model_dump_json()
    assert store.unchanged(loaded)
    after = (profile.read_bytes(), credential.read_bytes(), profile.stat(), credential.stat())
    assert before[:2] == after[:2]
    assert [item.st_mtime_ns for item in before[2:]] == [item.st_mtime_ns for item in after[2:]]
    field_name = "error_code"
    with pytest.raises(FrozenInstanceError):
        setattr(loaded, field_name, "changed")


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_owner_read_only_and_read_write_files_are_accepted(local: Settings, mode: int) -> None:
    profile, credential = provision(local)
    profile.chmod(mode)
    credential.chmod(mode)
    assert ProviderConfigurationStore(local).load("eodhd").error_code is None


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_exact_one_terminal_newline_is_supported_without_stripping_other_bytes(
    local: Settings, ending: bytes
) -> None:
    provision(local, key=KEY + ending)
    loaded = ProviderConfigurationStore(local).load("eodhd")
    assert loaded.error_code is None and loaded.access.credential is not None
    assert loaded.access.credential.get_secret_value() == KEY.decode()


@pytest.mark.parametrize(
    "key", [b"", b" key", b"key ", b"key\n\n", b"key\r", b"key\x00", b"\xff", b"a" * 257]
)
def test_invalid_credential_syntax_is_not_exposed_or_rounded(local: Settings, key: bytes) -> None:
    provision(local, key=key)
    loaded = ProviderConfigurationStore(local).load("eodhd")
    assert loaded.error_code == "INVALID_CREDENTIAL" and loaded.access.credential is None
    assert not loaded.access.enabled and loaded.health.state == FetchState.UNCONFIGURED


@pytest.mark.parametrize("which", ["profile", "credential"])
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700])
def test_local_files_reject_group_other_or_executable_permissions(
    local: Settings, which: str, mode: int
) -> None:
    profile, credential = provision(local)
    (profile if which == "profile" else credential).chmod(mode)
    result = ProviderConfigurationStore(local).load("eodhd")
    assert (
        result.error_code
        == ("CONFIG" if which == "profile" else "CREDENTIAL") + "_UNSAFE_PERMISSIONS"
    )
    assert result.access.credential is None and not result.access.enabled


@pytest.mark.parametrize("which", ["profile", "credential"])
def test_private_directory_permissions_are_required(local: Settings, which: str) -> None:
    profile, credential = provision(local)
    (profile if which == "profile" else credential).parent.chmod(0o755)
    result = ProviderConfigurationStore(local).load("eodhd")
    assert (
        result.error_code
        == ("CONFIG" if which == "profile" else "CREDENTIAL") + "_UNSAFE_DIRECTORY"
    )


@pytest.mark.parametrize("which", ["profile", "credential"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_links_and_nonregular_files_never_become_configs_or_credentials(
    local: Settings, which: str, kind: str, tmp_path: Path
) -> None:
    profile, credential = provision(local)
    target = profile if which == "profile" else credential
    original = target.read_bytes()
    target.unlink()
    if kind in {"symlink", "hardlink"}:
        other = tmp_path / "outside-private-file"
        other.write_bytes(original)
        other.chmod(0o600)
        target.symlink_to(other) if kind == "symlink" else os.link(other, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.mkdir(mode=0o700)
    result = ProviderConfigurationStore(local).load("eodhd")
    prefix = "CONFIG" if which == "profile" else "CREDENTIAL"
    assert result.error_code == prefix + ("_UNSAFE_PATH" if kind == "symlink" else "_UNSAFE_FILE")
    assert result.access.credential is None


@pytest.mark.parametrize("which", ["profile", "credential"])
def test_symlinked_ancestor_directories_are_not_resolved_through(
    local: Settings, which: str
) -> None:
    profile, credential = provision(local)
    directory = (profile if which == "profile" else credential).parent
    moved = directory.with_name(directory.name + "-moved")
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == ("CONFIG" if which == "profile" else "CREDENTIAL") + "_UNSAFE_PATH"


@pytest.mark.parametrize("where", ["data", "backup", "parent"])
def test_credentials_stay_outside_data_and_backup_trees(local: Settings, where: str) -> None:
    provision(local)
    changed = local.model_copy(
        update={
            "provider_secret_dir": local.data_dir / "keys"
            if where == "data"
            else local.backup_dir / "keys"
            if where == "backup"
            else local.data_dir.parent
        }
    )
    result = ProviderConfigurationStore(changed).load("eodhd")
    assert result.error_code == "UNSAFE_SECRET_LOCATION"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 0},
        {"version": "1"},
        {"enabled": "true"},
        {"terms_acknowledged": 1},
        {"terms_reference": ""},
        {"terms_reason": None},
        {"requests_per_minute": None},
        {"requests_per_day": None},
        {"requests_per_day": 0},
        {"requests_per_day": True},
        {"requests_per_day": "50"},
        {"requests_per_day": 2147483648},
        {"credential_version": None},
        {"credential_version": "../private"},
        {"credential_source": "../path"},
        {"credential_source": None},
        {"credential_file": "/private/operator-key"},
        {"api_key": "private-inline-key"},
        {"raw_payload_storage": True},
        {"raw_payload_storage": 0},
    ],
)
def test_policy_requires_explicit_strict_budgets_rights_and_fixed_credential_references(
    local: Settings, changes: dict[str, Any]
) -> None:
    provision(local, body=policy(**changes))
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == "CONFIG_INVALID" and not result.access.enabled
    assert "private" not in result.health.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"null",
        b"not JSON",
        b'{"enabled":true,"enabled":false}',
        b'{"requests_per_day":NaN}',
        b'{"requests_per_day":1e999999999999999999999999999}',
    ],
)
def test_malformed_duplicate_or_unbounded_json_is_sanitized(local: Settings, body: bytes) -> None:
    provision(local, body=body)
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == "CONFIG_INVALID" and result.profile is None


def test_one_provider_file_cannot_replace_another_provider_policy(local: Settings) -> None:
    provision(local, body=policy("massive"))
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == "CONFIG_PROVIDER_MISMATCH"
    invalid: Any = "../eodhd"
    with pytest.raises(ValueError, match="Unsupported provider"):
        ProviderConfigurationStore(local).load(invalid)


@pytest.mark.parametrize("state", ["disabled", "unacknowledged", "no-key", "missing-key"])
def test_keys_are_not_read_until_explicit_enablement_rights_and_reference_exist(
    local: Settings, state: str
) -> None:
    body = policy()
    if state == "disabled":
        body["enabled"] = False
    elif state == "unacknowledged":
        body["terms_acknowledged"] = False
    elif state == "no-key":
        body["credential_source"] = body["credential_version"] = None
    _, key = provision(local, body=body)
    key.unlink()
    expected = {
        "disabled": "PROVIDER_DISABLED",
        "unacknowledged": "RIGHTS_NOT_ACKNOWLEDGED",
        "no-key": "NO_API_KEY",
        "missing-key": "CREDENTIAL_NOT_FOUND",
    }[state]
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == expected and result.access.credential is None


@pytest.mark.parametrize("kind", ["profile", "credential"])
def test_input_size_is_bounded_before_parsing(local: Settings, kind: str) -> None:
    profile, key = provision(local)
    (profile if kind == "profile" else key).write_bytes(
        b"a" * (16385 if kind == "profile" else 259)
    )
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == ("CONFIG" if kind == "profile" else "CREDENTIAL") + "_TOO_LARGE"


def test_snapshot_changes_on_policy_or_key_rotation_without_hashing_plaintext(
    local: Settings,
) -> None:
    profile, key = provision(local)
    store = ProviderConfigurationStore(local)
    first = store.load("eodhd")
    assert store.unchanged(first)
    key.write_bytes(b"different-synthetic-key")
    assert not store.unchanged(first)
    second = store.load("eodhd")
    profile.write_text(json.dumps(policy(version=2, credential_version=2)))
    next_key = key.with_name("eodhd.2.key")
    next_key.write_bytes(KEY)
    next_key.chmod(0o600)
    third = store.load("eodhd")
    assert third.error_code is None and not store.unchanged(second)
    assert third.profile is not None and third.profile.version == 2
    assert first.revision != third.revision and second.revision != third.revision
    assert first.access.credential is not None and second.access.credential is not None
    assert first.access.credential.get_secret_value() != second.access.credential.get_secret_value()


def test_same_metadata_cannot_hide_a_same_length_credential_rewrite(
    local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(local)
    store = ProviderConfigurationStore(local)
    original = store.load("eodhd")
    assert original.error_code is None
    changed = LoadedConfiguration(
        "eodhd",
        error_code=None,
        profile=original.profile,
        revision=original.revision,
        access=ProviderAccess(
            enabled=True, terms_acknowledged=True, credential=SecretStr("different-synthetic-key")
        ),
    )
    monkeypatch.setattr(store, "load", lambda _: changed)
    assert not store.unchanged(original)


def test_mid_read_replacement_and_profile_change_during_key_read_fail_closed(
    local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, _ = provision(local)
    original = files._read_file

    def changed(directory: Path, name: str, **kwargs: Any) -> Any:
        result = original(directory, name, **kwargs)
        if kwargs["kind"] == "CREDENTIAL":
            profile.write_text(json.dumps(policy(version=2)))
        return result

    monkeypatch.setattr(files, "_read_file", changed)
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == "CONFIG_CHANGED_DURING_READ" and result.access.credential is None


def test_low_level_file_replacement_is_detected_with_descriptor_metadata(
    local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision(local)
    original = os.stat

    def replaced(path: Any, **kwargs: Any) -> Any:
        info = original(path, **kwargs)
        if kwargs.get("follow_symlinks") is False:
            fields = {
                name: getattr(info, name)
                for name in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_mode",
                    "st_uid",
                    "st_nlink",
                )
            }
            fields["st_ino"] += 1
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(os, "stat", replaced)
    result = ProviderConfigurationStore(local).load("eodhd")
    assert result.error_code == "CONFIG_CHANGED_DURING_READ" and result.access.credential is None


@pytest.mark.parametrize(
    "read_only,mode,expected",
    [
        (False, 0o444, "CREDENTIAL_UNSAFE_DOCKER_SECRET"),
        (True, 0o666, "CREDENTIAL_UNSAFE_DOCKER_SECRET"),
        (True, 0o644, None),
        (True, 0o444, None),
    ],
)
def test_docker_secret_requires_a_read_only_mount_not_just_world_readable_mode(
    local: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    mode: int,
    expected: str | None,
) -> None:
    provision(local, body=policy(credential_source="DOCKER_SECRET"))
    docker = tmp_path / "docker-secrets"
    docker.mkdir(mode=0o755)
    key = docker / "spm-eodhd-1"
    key.write_bytes(KEY)
    key.chmod(mode)
    monkeypatch.setattr(
        os, "fstatvfs", lambda _: SimpleNamespace(f_flag=os.ST_RDONLY if read_only else 0)
    )
    result = ProviderConfigurationStore(local, docker_secrets=docker).load("eodhd")
    assert result.error_code == expected
    if expected is None:
        assert (
            result.access.credential is not None
            and result.access.credential.get_secret_value() == KEY.decode()
        )


@pytest.mark.parametrize(
    "provider", ["yahoo", "yfinance", "manual", "hkex_evidence", "sec_evidence"]
)
def test_non_key_profiles_reject_credentials_and_yahoo_profiles_share_quota_group(
    provider: Any,
) -> None:
    with pytest.raises(ValidationError):
        ProviderFile.model_validate(
            policy(provider, credential_source="LOCAL_FILE", credential_version=1)
        )
    profile = ProviderFile.model_validate(policy(provider))
    if provider in {"yahoo", "yfinance"}:
        assert profile.quota_group == "YAHOO"
    else:
        with pytest.raises(ValidationError):
            ProviderFile.model_validate(policy(provider, requests_per_day=1))


@pytest.mark.anyio
async def test_all_real_bindings_healthcheck_locally_without_a_request(
    local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    for provider in registry():
        provision(local, provider)

    async def no_send(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Configuration diagnostics must not make any upstream request")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_send)
    reports = await check_configurations(local)
    assert len(reports) == 8
    for report in reports:
        assert report.configured and report.state == FetchState.STALE
        assert report.live_validation == "NOT_TESTED" and report.last_success_at is None
        assert KEY.decode() not in report.model_dump_json()
    assert (
        next(row for row in reports if row.provider_id == "yfinance").reason
        == "SDK_PARSER_READY_NOT_PROBED"
    )


@pytest.mark.anyio
async def test_factory_uses_only_loaded_server_key_and_never_writes_sqlite(
    local: Settings, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    provision(local)
    loaded = ProviderConfigurationStore(local).load("eodhd")
    before = database_snapshot(engine)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.params["api_token"] == KEY.decode()
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(
                b'[{"date":"2026-01-08","unadjustedValue":"0.3","currency":"USD"}]'
            ),
        )

    async with configured_adapter(loaded, transport=httpx.MockTransport(handle)) as adapter:
        result = await adapter.fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.rows[0].dps == "0.30000000"
    assert len(calls) == 1 and KEY.decode() not in result.model_dump_json() + caplog.text
    assert result.rows[0].adjustment_basis == "UNKNOWN" and database_snapshot(engine) == before
    with pytest.raises(ValueError, match="PROVIDER_UNCONFIGURED"):
        async with configured_adapter(LoadedConfiguration("eodhd")):
            pytest.fail("Unconfigured binding must not be created")


def test_diagnostic_cli_needs_no_database_and_outputs_only_safe_configuration_status(
    local: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_engine(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Provider diagnostics must not create/migrate a database")

    monkeypatch.setattr("spm_api.cli.Settings", lambda: local)
    monkeypatch.setattr("spm_api.cli.make_engine", no_engine)
    monkeypatch.setattr("sys.argv", ["spm-manage", "providers-check"])
    main()
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert report["scope"] == "CONFIGURATION_ONLY" and len(report["providers"]) == 8
    assert all(row["state"] == "UNCONFIGURED" for row in report["providers"])
    assert KEY.decode() not in raw and str(local.data_dir) not in raw
    assert not local.data_dir.exists() and not local.provider_secret_dir.exists()


def test_maximum_key_with_crlf_is_exact_and_never_printed(local: Settings) -> None:
    provision(local, key=b"a" * 256 + b"\r\n")
    loaded = ProviderConfigurationStore(local).load("eodhd")
    assert loaded.error_code is None and loaded.access.credential is not None
    assert loaded.access.credential.get_secret_value() == "a" * 256
    assert "a" * 256 not in repr(loaded) + loaded.health.model_dump_json()


@pytest.mark.parametrize("directory", [False, True])
def test_foreign_file_or_directory_owners_are_rejected_even_if_open_succeeds(
    local: Settings, monkeypatch: pytest.MonkeyPatch, directory: bool
) -> None:
    provision(local)
    original = os.fstat

    def foreign(descriptor: int) -> Any:
        info = original(descriptor)
        if stat.S_ISDIR(info.st_mode) == directory:
            values = {
                name: getattr(info, name)
                for name in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                    "st_mode",
                    "st_uid",
                    "st_nlink",
                )
            }
            values["st_uid"] += 1
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(os, "fstat", foreign)
    loaded = ProviderConfigurationStore(local).load("eodhd")
    assert loaded.error_code == (
        "CONFIG_UNSAFE_DIRECTORY" if directory else "CONFIG_UNSAFE_PERMISSIONS"
    )
    assert loaded.access.credential is None


@pytest.mark.anyio
async def test_one_invalid_provider_keeps_an_independent_valid_profile(local: Settings) -> None:
    _, key = provision(local)
    provision(local, "yahoo")
    key.chmod(0o644)
    reports = {report.provider_id: report for report in await check_configurations(local)}
    assert reports["eodhd"].state == FetchState.UNCONFIGURED
    assert reports["eodhd"].reason == "CREDENTIAL_UNSAFE_PERMISSIONS"
    assert reports["yahoo"].configured and reports["yahoo"].state == FetchState.STALE
    assert reports["yahoo"].reason == "NOT_PROBED"


def test_configured_cli_never_serializes_key_paths_profiles_or_revisions(
    local: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provision(local)
    monkeypatch.setattr("spm_api.cli.Settings", lambda: local)
    monkeypatch.setattr("sys.argv", ["spm-manage", "providers-check"])
    main()
    raw = capsys.readouterr().out
    report = next(row for row in json.loads(raw)["providers"] if row["provider_id"] == "eodhd")
    assert report["configured"] and report["state"] == "STALE" and report["last_success_at"] is None
    assert KEY.decode() not in raw and str(local.provider_secret_dir) not in raw
    assert "terms_reference" not in raw and "profile_sha256" not in raw
    assert not local.database_path.exists()
