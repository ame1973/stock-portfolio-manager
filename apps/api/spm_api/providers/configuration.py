"""Read-only operator configuration and credential files; no API import, writes or network calls."""

import errno
import hashlib
import hmac
import os
import stat
from dataclasses import dataclass, field
from decimal import DecimalException
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, SecretStr, model_validator

from spm_api.config import Settings
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Note, ProviderId
from spm_api.providers.adapters import AdapterHealth
from spm_api.providers.base import FetchState, OriginGroup, registry
from spm_api.providers.http import ProviderAccess, exact_json


class ProviderPolicy(Model):
    """Shared non-secret access declarations; no credential or filesystem selection."""

    model_config = ConfigDict(frozen=True)

    provider_id: ProviderId
    enabled: bool = Field(default=False, strict=True)
    terms_acknowledged: bool = Field(default=False, strict=True)
    terms_reference: Note = ""
    terms_reason: ChangeReason | None = None
    requests_per_minute: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    requests_per_day: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    raw_payload_storage: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def explicit_policy(self) -> Self:
        mode = registry()[self.provider_id].access_mode
        if self.raw_payload_storage:
            raise ValueError("No adapter currently grants raw-payload storage")
        if self.terms_acknowledged and (
            not self.terms_reference.strip() or self.terms_reason is None
        ):
            raise ValueError("Acknowledgment requires its explicit reference and reason")
        if mode == "MANUAL_EVIDENCE":
            if self.requests_per_minute is not None or self.requests_per_day is not None:
                raise ValueError("Manual evidence cannot claim a network quota")
        elif self.enabled and (self.requests_per_minute is None or self.requests_per_day is None):
            raise ValueError("Enabled network profiles require explicit minute and daily budgets")
        return self

    @property
    def quota_group(self) -> OriginGroup:
        return registry()[self.provider_id].origin_group


class ProviderFile(ProviderPolicy):
    """Trusted operator metadata with fixed, versioned credential references only."""

    version: int = Field(strict=True, ge=1, le=2147483647)
    credential_source: Literal["LOCAL_FILE", "DOCKER_SECRET"] | None = None
    credential_version: int | None = Field(default=None, strict=True, ge=1, le=2147483647)

    @model_validator(mode="after")
    def fixed_credential_reference(self) -> Self:
        if (self.credential_source is None) != (self.credential_version is None):
            raise ValueError("Credential source and immutable version must be supplied together")
        if (
            registry()[self.provider_id].access_mode != "API_KEY"
            and self.credential_source is not None
        ):
            raise ValueError("This provider profile does not accept API credentials")
        return self


class ManagedPolicySnapshot(ProviderPolicy):
    """Private owned decision identity, never an operator-file credential version."""

    configuration_source: Literal["MANAGED_PROFILE"] = "MANAGED_PROFILE"
    version: int = Field(strict=True, ge=1, le=2147483647)
    revision_id: UUID = Field(repr=False)
    owner_id: UUID = Field(repr=False)
    credential_artifact_id: UUID | None = Field(repr=False)


type FileSignature = tuple[int, int, int, int, int, int, int, int]


def _signature(info: os.stat_result) -> FileSignature:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


@dataclass(frozen=True)
class ConfigurationRevision:
    """Private filesystem identity; never hash the plaintext credential or publish this token."""

    profile_sha256: str
    profile_file: FileSignature
    credential_file: FileSignature | None = None


@dataclass(frozen=True)
class ManagedConfigurationRevision:
    """Private managed metadata/edition identity; no invented profile-file signature or key hash."""

    profile_sha256: str
    credential_files: tuple[FileSignature, FileSignature] | None = None


@dataclass(frozen=True)
class LoadedConfiguration:
    """Server-only result; only health is suitable for diagnostics, never access/revision."""

    provider_id: ProviderId
    error_code: str | None = "CONFIG_NOT_LOADED"
    profile: ProviderFile | ManagedPolicySnapshot | None = field(default=None, repr=False)
    access: ProviderAccess = field(default_factory=ProviderAccess, repr=False)
    revision: ConfigurationRevision | ManagedConfigurationRevision | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.error_code is None and (
            self.profile is None
            or self.profile.provider_id != self.provider_id
            or self.revision is None
            or not self.access.enabled
            or not self.access.terms_acknowledged
        ):
            raise ValueError("Ready configuration requires validated policy, access and revision")

    @property
    def health(self) -> AdapterHealth:
        return AdapterHealth(
            provider_id=self.provider_id,
            configured=self.error_code is None,
            state=FetchState.UNCONFIGURED if self.error_code is not None else FetchState.STALE,
            reason=self.error_code or "CONFIGURATION_LOADED_NOT_PROBED",
        )


class _FileRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _directory(path: Path, *, private: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in path.absolute().parts[1:]:
            if component in {".", ".."}:
                raise _FileRejected("UNSAFE_PATH")
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if private:
            if info.st_uid != os.geteuid() or mode not in {0o500, 0o700}:
                raise _FileRejected("UNSAFE_DIRECTORY")
        elif info.st_uid not in {0, os.geteuid()} or mode & 0o022:
            raise _FileRejected("UNSAFE_DIRECTORY")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_file(
    directory: Path, name: str, *, kind: str, maximum: int, docker_secret: bool = False
) -> tuple[bytes, FileSignature]:
    directory_fd = -1
    try:
        directory_fd = _directory(directory, private=not docker_secret)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            mode = stat.S_IMODE(before.st_mode)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise _FileRejected("UNSAFE_FILE")
            if docker_secret:
                read_only = bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)
                if (
                    not read_only
                    or before.st_uid not in {0, os.geteuid()}
                    or mode & 0o133
                    or not mode & 0o400
                ):
                    raise _FileRejected("UNSAFE_DOCKER_SECRET")
            elif before.st_uid != os.geteuid() or mode not in {0o400, 0o600}:
                raise _FileRejected("UNSAFE_PERMISSIONS")
            if before.st_size > maximum:
                raise _FileRejected("TOO_LARGE")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                body = stream.read(maximum + 1)
            if len(body) > maximum:
                raise _FileRejected("TOO_LARGE")
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _signature(before) != _signature(after) or _signature(before) != _signature(current):
                raise _FileRejected("CHANGED_DURING_READ")
            return body, _signature(after)
        finally:
            os.close(descriptor)
    except _FileRejected as error:
        raise _FileRejected(f"{kind}_{error.code}") from None
    except FileNotFoundError:
        raise _FileRejected(f"{kind}_NOT_FOUND") from None
    except PermissionError:
        raise _FileRejected(f"{kind}_PERMISSION_DENIED") from None
    except OSError as error:
        suffix = "UNSAFE_PATH" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "UNREADABLE"
        raise _FileRejected(f"{kind}_{suffix}") from None
    finally:
        if directory_fd != -1:
            os.close(directory_fd)


class ProviderConfigurationStore:
    """Read-only operator provisioning, not a creation/rotation API or audit bypass."""

    def __init__(self, settings: Settings, *, docker_secrets: Path = Path("/run/secrets")) -> None:
        self._profiles = settings.data_dir / "provider-config"
        self._secrets = settings.provider_secret_dir
        self._docker_secrets = docker_secrets
        self._excluded = (settings.data_dir.absolute(), settings.backup_dir.absolute())

    def load(self, provider_id: ProviderId) -> LoadedConfiguration:
        if provider_id not in registry():
            raise ValueError("Unsupported provider configuration")
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fstatvfs"):
            return LoadedConfiguration(provider_id, "UNSUPPORTED_PLATFORM")
        profile: ProviderFile | None = None
        try:
            body, profile_signature = _read_file(
                self._profiles, f"{provider_id}.json", kind="CONFIG", maximum=16384
            )
            try:
                profile = ProviderFile.model_validate(exact_json(body))
            except (ValueError, DecimalException, RecursionError):
                return LoadedConfiguration(provider_id, "CONFIG_INVALID")
            if profile.provider_id != provider_id:
                return LoadedConfiguration(provider_id, "CONFIG_PROVIDER_MISMATCH")
            if not profile.enabled:
                return LoadedConfiguration(provider_id, "PROVIDER_DISABLED", profile)
            if not profile.terms_acknowledged:
                return LoadedConfiguration(provider_id, "RIGHTS_NOT_ACKNOWLEDGED", profile)
            credential = None
            credential_signature = None
            if registry()[provider_id].access_mode == "API_KEY":
                if profile.credential_source is None:
                    return LoadedConfiguration(provider_id, "NO_API_KEY", profile)
                source = profile.credential_source
                number = profile.credential_version
                if source == "LOCAL_FILE":
                    absolute = self._secrets.absolute()
                    if any(
                        absolute.is_relative_to(path) or path.is_relative_to(absolute)
                        for path in self._excluded
                    ):
                        return LoadedConfiguration(provider_id, "UNSAFE_SECRET_LOCATION", profile)
                    directory, name = self._secrets, f"{provider_id}.{number}.key"
                else:
                    directory, name = self._docker_secrets, f"spm-{provider_id}-{number}"
                secret, credential_signature = _read_file(
                    directory,
                    name,
                    kind="CREDENTIAL",
                    maximum=258,
                    docker_secret=source == "DOCKER_SECRET",
                )
                secret = (
                    secret[:-2]
                    if secret.endswith(b"\r\n")
                    else secret[:-1]
                    if secret.endswith(b"\n")
                    else secret
                )
                if not 1 <= len(secret) <= 256 or any(not 33 <= value <= 126 for value in secret):
                    return LoadedConfiguration(provider_id, "INVALID_CREDENTIAL", profile)
                credential = SecretStr(secret.decode("ascii"))
            checked, checked_signature = _read_file(
                self._profiles, f"{provider_id}.json", kind="CONFIG", maximum=16384
            )
            if checked_signature != profile_signature or checked != body:
                return LoadedConfiguration(provider_id, "CONFIG_CHANGED_DURING_READ")
            revision = ConfigurationRevision(
                hashlib.sha256(body).hexdigest(), profile_signature, credential_signature
            )
            return LoadedConfiguration(
                provider_id,
                error_code=None,
                profile=profile,
                access=ProviderAccess(enabled=True, terms_acknowledged=True, credential=credential),
                revision=revision,
            )
        except _FileRejected as error:
            return LoadedConfiguration(provider_id, error.code)

    def unchanged(self, observed: LoadedConfiguration) -> bool:
        if observed.error_code is not None or observed.revision is None:
            return False
        current = self.load(observed.provider_id)
        if current.error_code is not None or current.revision != observed.revision:
            return False
        previous, latest = observed.access.credential, current.access.credential
        if previous is None or latest is None:
            return previous is latest
        # Filesystems can retain identical timestamps/size across a same-length rewrite.
        return hmac.compare_digest(previous.get_secret_value(), latest.get_secret_value())
