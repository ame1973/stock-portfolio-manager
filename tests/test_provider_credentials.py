"""Immutable credential files are preparation, never activation or provider access."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

import spm_api.providers.credentials as credentials
from spm_api.audit import canonical
from spm_api.config import Settings
from spm_api.providers.configuration import FileSignature, ProviderConfigurationStore, _read_file
from spm_api.providers.credentials import CredentialArtifactStore, CredentialError, CredentialRef
from tests.helpers import database_snapshot
from tests.test_provider_configuration import KEY
from tests.test_provider_persistence import book as book


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    result = tmp_path / "prepared-credentials"
    result.mkdir(mode=0o700)
    return result


def store(settings: Settings, directory: Path) -> CredentialArtifactStore:
    return CredentialArtifactStore(settings, directory=directory)


def paths(directory: Path, ref: CredentialRef) -> tuple[Path, Path]:
    base = f"{ref.provider_id}.{ref.artifact_id}"
    return directory / f"{base}.key", directory / f"{base}.ready.json"


def contents(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


@pytest.mark.parametrize("provider", ["eodhd", "alphavantage", "massive"])
def test_prepare_and_read_exact_private_artifact_without_any_activation(
    settings: Settings, directory: Path, provider: Any
) -> None:
    repository = store(settings, directory)
    snapshot = repository.prepare(provider, SecretStr(KEY.decode()))
    assert snapshot.reference.provider_id == provider
    assert isinstance(snapshot.reference.artifact_id, UUID)
    assert snapshot.credential.get_secret_value().encode() == KEY
    assert repository.load(snapshot.reference) == snapshot
    assert repository.unchanged(snapshot)
    key, marker = paths(directory, snapshot.reference)
    assert key.read_bytes() == KEY
    assert stat.S_IMODE(key.stat().st_mode) == stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert key.stat().st_uid == marker.stat().st_uid == os.geteuid()
    body = marker.read_bytes()
    assert KEY not in body and sha256(KEY).hexdigest().encode() not in body
    assert KEY.decode() not in repr(snapshot) and str(directory) not in repr(snapshot)
    assert not (settings.data_dir / "provider-config").exists()
    with pytest.raises(FrozenInstanceError):
        snapshot.reference.artifact_id = uuid4()  # type: ignore[misc]


def test_importing_or_reading_missing_artifacts_never_creates_directories(
    settings: Settings, tmp_path: Path
) -> None:
    missing = tmp_path / "not-created"
    repository = store(settings, missing)
    with pytest.raises(CredentialError):
        repository.load(CredentialRef("eodhd", uuid4()))
    assert not missing.exists()
    with pytest.raises(CredentialError):
        repository.prepare("eodhd", SecretStr(KEY.decode()))
    assert not missing.exists()


@pytest.mark.parametrize(
    "value",
    ["", "has space", "has\ttab", "trailing\n", "trailing\r\n", "non-ascii-密鑰", "a" * 257],
)
def test_api_style_secret_values_are_bounded_exact_and_never_silently_trimmed(
    settings: Settings, directory: Path, value: str
) -> None:
    with pytest.raises(CredentialError) as error:
        store(settings, directory).prepare("eodhd", SecretStr(value))
    assert str(error.value) == "INVALID_CREDENTIAL_VALUE"
    assert contents(directory) == {}


@pytest.mark.parametrize(
    "provider",
    [
        "yahoo",
        "yfinance",
        "manual",
        "hkex_evidence",
        "sec_evidence",
        "../eodhd",
        "eodhd/other",
        1,
        None,
    ],
)
def test_non_key_profiles_and_untrusted_provider_names_cannot_create_a_file(
    settings: Settings, directory: Path, provider: Any
) -> None:
    with pytest.raises(CredentialError):
        store(settings, directory).prepare(provider, SecretStr(KEY.decode()))
    assert contents(directory) == {}


@pytest.mark.parametrize("value", [KEY, KEY.decode(), None, 1])
def test_writer_requires_explicit_server_only_secret_type(
    settings: Settings, directory: Path, value: Any
) -> None:
    with pytest.raises(CredentialError, match="^INVALID_CREDENTIAL_VALUE$"):
        store(settings, directory).prepare("eodhd", value)
    assert contents(directory) == {}


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_insecure_directory_fails_without_writing(
    settings: Settings, directory: Path, mode: int
) -> None:
    directory.chmod(mode)
    with pytest.raises(CredentialError):
        store(settings, directory).prepare("eodhd", SecretStr(KEY.decode()))
    assert contents(directory) == {}


@pytest.mark.parametrize(
    "location", ["data", "data-child", "backup", "backup-child", "common-parent"]
)
def test_credential_artifacts_cannot_overlap_financial_or_backup_storage(
    settings: Settings, tmp_path: Path, location: str
) -> None:
    target = {
        "data": settings.data_dir,
        "data-child": settings.data_dir / "keys",
        "backup": settings.backup_dir,
        "backup-child": settings.backup_dir / "keys",
        "common-parent": tmp_path,
    }[location]
    before = target.exists()
    with pytest.raises(CredentialError, match="^UNSAFE_CREDENTIAL_LOCATION$"):
        store(settings, target).prepare("eodhd", SecretStr(KEY.decode()))
    assert target.exists() == before


def test_symlinked_root_or_ancestor_is_not_followed(
    settings: Settings, directory: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "unsafe-alias"
    alias.symlink_to(directory, target_is_directory=True)
    for target in (alias, alias / "nested"):
        with pytest.raises(CredentialError):
            store(settings, target).prepare("eodhd", SecretStr(KEY.decode()))
    assert contents(directory) == {}


def test_existing_uuid_artifact_is_never_overwritten_or_deleted(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = uuid4()
    monkeypatch.setattr(credentials, "uuid4", lambda: identifier)
    repository = store(settings, directory)
    first = repository.prepare("eodhd", SecretStr(KEY.decode()))
    before = contents(directory)
    with pytest.raises(CredentialError):
        repository.prepare("eodhd", SecretStr("different-synthetic-value"))
    assert contents(directory) == before and repository.unchanged(first)


def test_short_writes_finish_exactly_and_zero_write_leaves_no_ready_marker(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, body: write(fd, body[:3]))
    repository = store(settings, directory)
    snapshot = repository.prepare("eodhd", SecretStr(KEY.decode()))
    assert repository.load(snapshot.reference).credential.get_secret_value().encode() == KEY
    identifier = uuid4()
    monkeypatch.setattr(credentials, "uuid4", lambda: identifier)
    monkeypatch.setattr(os, "write", lambda *_: 0)
    with pytest.raises(CredentialError):
        repository.prepare("eodhd", SecretStr(KEY.decode()))
    ref = CredentialRef("eodhd", identifier)
    key, marker = paths(directory, ref)
    assert key.exists() and not marker.exists()
    with pytest.raises(CredentialError):
        repository.load(ref)


@pytest.mark.parametrize("target", ["key", "marker"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "permissions"])
def test_replaced_or_unsafe_artifact_parts_fail_closed_without_echo_or_repair(
    settings: Settings, directory: Path, target: str, kind: str, tmp_path: Path
) -> None:
    repository = store(settings, directory)
    snapshot = repository.prepare("eodhd", SecretStr(KEY.decode()))
    key, marker = paths(directory, snapshot.reference)
    path = key if target == "key" else marker
    original = path.read_bytes()
    if kind == "permissions":
        path.chmod(0o644)
    else:
        path.unlink()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_bytes(original)
        elsewhere.chmod(0o600)
        if kind == "symlink":
            path.symlink_to(elsewhere)
        elif kind == "hardlink":
            os.link(elsewhere, path)
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises(CredentialError) as error:
        repository.load(snapshot.reference)
    assert KEY.decode() not in str(error.value) and str(directory) not in str(error.value)
    assert not repository.unchanged(snapshot)
    assert path.exists()


def test_marker_identity_and_canonical_metadata_cannot_relabel_an_artifact(
    settings: Settings, directory: Path
) -> None:
    repository = store(settings, directory)
    snapshot = repository.prepare("eodhd", SecretStr(KEY.decode()))
    _, marker = paths(directory, snapshot.reference)
    original = json.loads(marker.read_bytes())
    for change in (
        {"provider_id": "massive"},
        {"artifact_id": str(uuid4())},
        {"schema_version": True},
        {"credential_length": 0},
        {"api_key": KEY.decode()},
    ):
        marker.write_text(json.dumps({**original, **change}))
        with pytest.raises(CredentialError):
            repository.load(snapshot.reference)
    assert not repository.unchanged(snapshot)


def test_same_length_same_reported_signatures_still_compare_secret_in_memory(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = store(settings, directory)
    first = repository.prepare("eodhd", SecretStr(KEY.decode()))
    original = _read_file
    signatures: dict[str, FileSignature] = {}

    def read(*args: Any, **kwargs: Any) -> Any:
        body, signature = original(*args, **kwargs)
        name = args[1]
        signatures.setdefault(name, signature)
        return body, signatures[name]

    monkeypatch.setattr(credentials, "_read_file", read)
    observed = repository.load(first.reference)
    key, _ = paths(directory, observed.reference)
    key.write_bytes(b"x" * len(KEY))
    changed = repository.load(observed.reference)
    assert changed.revision == observed.revision
    assert not repository.unchanged(observed)


@pytest.mark.parametrize("missing", ["O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"])
def test_unsupported_secure_open_platform_fails_before_any_write(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delattr(os, missing)
    with pytest.raises(CredentialError, match="^CREDENTIAL_STORAGE_UNAVAILABLE$"):
        store(settings, directory).prepare("eodhd", SecretStr(KEY.decode()))
    assert contents(directory) == {}


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"not-json",
        b'{"schema_version":1,"schema_version":1}',
        b'{"credential_length":NaN}',
        b'{"credential_length":1e999999999999999999999999999999}',
        b" " * 1025,
    ],
)
def test_malformed_bounded_manifest_fails_with_only_a_fixed_safe_code(
    settings: Settings, directory: Path, payload: bytes
) -> None:
    repository = store(settings, directory)
    snapshot = repository.prepare("eodhd", SecretStr(KEY.decode()))
    _, marker = paths(directory, snapshot.reference)
    marker.write_bytes(payload)
    with pytest.raises(CredentialError, match="^CREDENTIAL_ARTIFACT_UNAVAILABLE$"):
        repository.load(snapshot.reference)


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_file_or_directory_fsync_failure_never_returns_preparation_or_overwrites_old_files(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch, failure_at: int
) -> None:
    repository = store(settings, directory)
    old = repository.prepare("eodhd", SecretStr(KEY.decode()))
    before = contents(directory)
    original = os.fsync
    calls = 0

    def fail(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise OSError("private-storage-error:" + KEY.decode())
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(CredentialError) as error:
        repository.prepare("eodhd", SecretStr("different-synthetic-key"))
    assert str(error.value) == "CREDENTIAL_PREPARATION_FAILED"
    assert KEY.decode() not in str(error.value)
    assert all(contents(directory)[name] == value for name, value in before.items())
    assert repository.unchanged(old)


def test_financial_directory_symlink_alias_is_still_excluded(
    settings: Settings, directory: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "financial-alias"
    alias.symlink_to(directory, target_is_directory=True)
    local = settings.model_copy(update={"data_dir": alias})
    with pytest.raises(CredentialError, match="^UNSAFE_CREDENTIAL_LOCATION$"):
        store(local, directory).prepare("eodhd", SecretStr(KEY.decode()))
    assert contents(directory) == {}


@pytest.mark.parametrize(
    "provider,identifier", [("../eodhd", uuid4()), ("eodhd", str(uuid4())), ("eodhd", None)]
)
def test_references_are_typed_identities_not_caller_paths(provider: Any, identifier: Any) -> None:
    with pytest.raises(CredentialError, match="^INVALID_CREDENTIAL_REFERENCE$"):
        CredentialRef(provider, identifier)


@pytest.mark.parametrize("reference", [None, "../key", ("eodhd", uuid4())])
def test_reader_rejects_nonreference_input_without_directory_changes(
    settings: Settings, directory: Path, reference: Any
) -> None:
    with pytest.raises(CredentialError, match="^INVALID_CREDENTIAL_REFERENCE$"):
        store(settings, directory).load(reference)
    assert contents(directory) == {}


def test_unresolvable_location_has_no_private_path_or_exception_echo(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("private-location:" + KEY.decode())

    monkeypatch.setattr(Path, "resolve", broken)
    with pytest.raises(CredentialError, match="^UNSAFE_CREDENTIAL_LOCATION$"):
        store(settings, directory)
    assert contents(directory) == {}


@pytest.mark.parametrize("payload", [b"x" * (len(KEY) + 1), b"\x00" * len(KEY)])
def test_corrupted_key_length_or_characters_fail_without_trimming_or_repair(
    settings: Settings, directory: Path, payload: bytes
) -> None:
    repository = store(settings, directory)
    prepared = repository.prepare("eodhd", SecretStr(KEY.decode()))
    key, _ = paths(directory, prepared.reference)
    key.write_bytes(payload)
    with pytest.raises(CredentialError, match="^CREDENTIAL_ARTIFACT_UNAVAILABLE$"):
        repository.load(prepared.reference)
    assert key.read_bytes() == payload


def test_changed_marker_during_credential_read_is_not_accepted(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = store(settings, directory)
    prepared = repository.prepare("eodhd", SecretStr(KEY.decode()))
    _, marker = paths(directory, prepared.reference)
    metadata = json.loads(marker.read_bytes())

    def read(*args: Any, **kwargs: Any) -> Any:
        result = _read_file(*args, **kwargs)
        if kwargs["kind"] == "CREDENTIAL":
            marker.write_text(canonical({**metadata, "credential_length": len(KEY) + 1}))
        return result

    monkeypatch.setattr(credentials, "_read_file", read)
    with pytest.raises(CredentialError, match="^CREDENTIAL_ARTIFACT_UNAVAILABLE$"):
        repository.load(prepared.reference)


def test_path_replacement_after_sync_cannot_return_a_prepared_artifact(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync

    def sync(descriptor: int) -> None:
        original(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            key = next(directory.glob("*.key"))
            moved = directory / "synthetic-replacement"
            moved.write_bytes(b"x" * len(KEY))
            moved.chmod(0o600)
            os.replace(moved, key)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(CredentialError, match="^CREDENTIAL_ARTIFACT_CHANGED$"):
        store(settings, directory).prepare("eodhd", SecretStr(KEY.decode()))
    assert not list(directory.glob("*.ready.json"))


def test_reloaded_changed_secret_cannot_be_returned_as_the_requested_preparation(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = store(settings, directory)
    original = repository.load

    def read(reference: CredentialRef) -> Any:
        return replace(original(reference), credential=SecretStr("x" * len(KEY)))

    monkeypatch.setattr(repository, "load", read)
    with pytest.raises(CredentialError, match="^CREDENTIAL_ARTIFACT_CHANGED$"):
        repository.prepare("eodhd", SecretStr(KEY.decode()))


def test_concurrent_same_uuid_preparation_retains_one_exact_edition_without_overwriting(
    settings: Settings, directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = uuid4()
    monkeypatch.setattr(credentials, "uuid4", lambda: identifier)
    repository = store(settings, directory)

    def attempt(value: str) -> Any:
        try:
            return repository.prepare("eodhd", SecretStr(value))
        except CredentialError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [KEY.decode(), "other-synthetic-key"]))
    ready = [item for item in results if item is not None]
    assert len(ready) == 1 and len(contents(directory)) == 2
    assert repository.unchanged(ready[0])


@pytest.mark.anyio
async def test_preparation_does_not_change_any_financial_source_or_operator_config_fact(
    book: dict[str, Any], engine: Engine, directory: Path
) -> None:
    settings = book["settings"]
    operator = ProviderConfigurationStore(settings)
    existing = operator.load("eodhd")
    before = database_snapshot(engine)
    prepared = store(settings, directory).prepare("eodhd", SecretStr("new-synthetic-not-active"))
    assert prepared.credential.get_secret_value() == "new-synthetic-not-active"
    assert operator.unchanged(existing)
    assert database_snapshot(engine) == before
