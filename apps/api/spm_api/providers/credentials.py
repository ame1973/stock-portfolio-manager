"""Prepare immutable private credentials; no active binding, DB, provider or HTTP side effects."""

import hmac
import os
from dataclasses import dataclass, field
from decimal import DecimalException
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, SecretStr

from spm_api.audit import canonical
from spm_api.config import Settings
from spm_api.errors import Model
from spm_api.providers.configuration import (
    FileSignature,
    _directory,
    _FileRejected,
    _read_file,
    _signature,
)
from spm_api.providers.http import exact_json

type KeyProvider = Literal["eodhd", "alphavantage", "massive"]
_KEYS = ("eodhd", "alphavantage", "massive")


class CredentialError(ValueError):
    """Fixed codes only; never contain credentials, paths or private exception text."""


def exact_credential(value: SecretStr) -> SecretStr:
    """Validate the managed input without trimming, echoing or hashing its actual value."""
    if not isinstance(value, SecretStr):
        raise CredentialError("INVALID_CREDENTIAL_VALUE")
    actual = value.get_secret_value()
    if not 1 <= len(actual) <= 256 or any(not 33 <= ord(char) <= 126 for char in actual):
        raise CredentialError("INVALID_CREDENTIAL_VALUE")
    return value


@dataclass(frozen=True)
class CredentialRef:
    provider_id: KeyProvider
    artifact_id: UUID

    def __post_init__(self) -> None:
        if self.provider_id not in _KEYS or not isinstance(self.artifact_id, UUID):
            raise CredentialError("INVALID_CREDENTIAL_REFERENCE")


@dataclass(frozen=True)
class CredentialSnapshot:
    """Private preparation evidence, not an owner authorization or an active provider profile."""

    reference: CredentialRef
    credential: SecretStr = field(repr=False)
    revision: tuple[FileSignature, FileSignature] = field(repr=False)


class _Ready(Model):
    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(strict=True, ge=1, le=1)
    provider_id: KeyProvider
    artifact_id: UUID
    credential_length: int = Field(strict=True, ge=1, le=256)


def _platform() -> None:
    if not all(
        hasattr(os, name)
        for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK", "fsync", "fchmod")
    ):
        raise CredentialError("CREDENTIAL_STORAGE_UNAVAILABLE")


def _write(directory: int, name: str, body: bytes) -> FileSignature:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        position = 0
        while position < len(body):
            written = os.write(descriptor, body[position:])
            if written <= 0:
                raise OSError("Incomplete private artifact write")
            position += written
        os.fsync(descriptor)
        signature = _signature(os.fstat(descriptor))
        if signature != _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)):
            raise CredentialError("CREDENTIAL_ARTIFACT_CHANGED")
        return signature
    finally:
        os.close(descriptor)


class CredentialArtifactStore:
    """Prepare outside writer UoWs; never create directories or automatically adopt files."""

    def __init__(self, settings: Settings, *, directory: Path) -> None:
        self._root = directory.absolute()
        try:
            root = self._root.resolve()
            excluded = (settings.data_dir.resolve(), settings.backup_dir.resolve())
        except (OSError, RuntimeError):
            raise CredentialError("UNSAFE_CREDENTIAL_LOCATION") from None
        if any(root.is_relative_to(path) or path.is_relative_to(root) for path in excluded):
            raise CredentialError("UNSAFE_CREDENTIAL_LOCATION")

    @staticmethod
    def _names(reference: CredentialRef) -> tuple[str, str]:
        if not isinstance(reference, CredentialRef):
            raise CredentialError("INVALID_CREDENTIAL_REFERENCE")
        reference.__post_init__()
        base = f"{reference.provider_id}.{reference.artifact_id}"
        return f"{base}.key", f"{base}.ready.json"

    def prepare(self, provider_id: KeyProvider, credential: SecretStr) -> CredentialSnapshot:
        _platform()
        if provider_id not in _KEYS:
            raise CredentialError("INVALID_CREDENTIAL_REFERENCE")
        body = exact_credential(credential).get_secret_value().encode("ascii")
        reference = CredentialRef(provider_id, uuid4())
        key, marker = self._names(reference)
        manifest = _Ready(
            schema_version=1,
            provider_id=provider_id,
            artifact_id=reference.artifact_id,
            credential_length=len(body),
        )
        descriptor = -1
        try:
            descriptor = _directory(self._root, private=True)
            key_signature = _write(descriptor, key, body)
            marker_signature = _write(
                descriptor, marker, canonical(manifest.model_dump(mode="json")).encode()
            )
            os.fsync(descriptor)
            result = self.load(reference)
            if result.revision != (key_signature, marker_signature) or not hmac.compare_digest(
                result.credential.get_secret_value().encode("ascii"), body
            ):
                raise CredentialError("CREDENTIAL_ARTIFACT_CHANGED")
            return result
        except (_FileRejected, OSError):
            # Unselected failed/crash-uncertain files are retained, never overwritten or adopted.
            raise CredentialError("CREDENTIAL_PREPARATION_FAILED") from None
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def load(self, reference: CredentialRef) -> CredentialSnapshot:
        _platform()
        key, marker = self._names(reference)
        try:
            metadata, metadata_signature = _read_file(
                self._root, marker, kind="ARTIFACT", maximum=1024
            )
            parsed = _Ready.model_validate(exact_json(metadata))
            if canonical(parsed.model_dump(mode="json")).encode() != metadata or (
                parsed.provider_id,
                parsed.artifact_id,
            ) != (reference.provider_id, reference.artifact_id):
                raise CredentialError("CREDENTIAL_ARTIFACT_UNAVAILABLE")
            body, key_signature = _read_file(self._root, key, kind="CREDENTIAL", maximum=256)
            if len(body) != parsed.credential_length or any(not 33 <= byte <= 126 for byte in body):
                raise CredentialError("CREDENTIAL_ARTIFACT_UNAVAILABLE")
            checked, checked_signature = _read_file(
                self._root, marker, kind="ARTIFACT", maximum=1024
            )
            if checked != metadata or checked_signature != metadata_signature:
                raise CredentialError("CREDENTIAL_ARTIFACT_UNAVAILABLE")
            return CredentialSnapshot(
                reference, SecretStr(body.decode("ascii")), (key_signature, metadata_signature)
            )
        except (_FileRejected, OSError, ValueError, DecimalException, RecursionError):
            raise CredentialError("CREDENTIAL_ARTIFACT_UNAVAILABLE") from None

    def unchanged(self, observed: CredentialSnapshot) -> bool:
        try:
            current = self.load(observed.reference)
        except CredentialError:
            return False
        return current.revision == observed.revision and hmac.compare_digest(
            current.credential.get_secret_value(), observed.credential.get_secret_value()
        )


def managed_artifacts(settings: Settings) -> CredentialArtifactStore:
    """Use explicit separate storage only; never create a root or fall back to operator secrets."""
    root = settings.managed_provider_secret_dir
    if root is None:
        raise CredentialError("MANAGED_CREDENTIAL_STORAGE_NOT_CONFIGURED")
    try:
        resolved, operator = root.resolve(), settings.provider_secret_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        raise CredentialError("UNSAFE_MANAGED_CREDENTIAL_LOCATION") from None
    if resolved.is_relative_to(operator) or operator.is_relative_to(resolved):
        raise CredentialError("UNSAFE_MANAGED_CREDENTIAL_LOCATION")
    return CredentialArtifactStore(settings, directory=root)
