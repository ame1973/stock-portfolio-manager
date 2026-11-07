"""Select explicit owned policy/edition scope; operator CLI remains a separate file-only path."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.providers.credentials as credentials
import spm_api.providers.profiles as profiles
from spm_api.audit import canonical, digest
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.master_models import ProviderId
from spm_api.providers.base import registry
from spm_api.providers.configuration import (
    LoadedConfiguration,
    ManagedConfigurationRevision,
    ManagedPolicySnapshot,
    ProviderConfigurationStore,
)
from spm_api.providers.http import ProviderAccess


def validate_configuration(
    con: Connection, configuration: LoadedConfiguration, owner: UUID
) -> None:
    """Check one observed owned decision using DB metadata only, including inactive peers."""
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise ValueError("WRITE_UNIT_OF_WORK_REQUIRED")
    profile = configuration.profile
    if configuration.selection_owner_id not in (None, owner) or (
        isinstance(profile, ManagedPolicySnapshot) and profile.owner_id != owner
    ):
        raise ValueError("PROVIDER_OWNER_MISMATCH")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:owner AND is_active=1"), {"owner": str(owner)}
        ).first()
        is None
    ):
        raise ValueError("PROVIDER_CONFIG_CHANGED")
    try:
        current = profiles.latest(con, owner, configuration.provider_id)
    except profiles.ProfileError:
        raise ValueError("PROVIDER_CONFIG_CHANGED") from None
    if isinstance(profile, ManagedPolicySnapshot):
        if (
            current is None
            or profiles.policy_snapshot(current) != profile
            or (
                configuration.revision is not None
                and digest(canonical(profile.model_dump(mode="json")))
                != configuration.revision.profile_sha256
            )
        ):
            raise ValueError("PROVIDER_CONFIG_CHANGED")
    elif current is not None:
        raise ValueError("PROVIDER_CONFIG_CHANGED")


class OwnerConfigurationStore(ProviderConfigurationStore):
    """Owned actor scope for queue/schedule/worker/diagnostics; never infer an owner from files."""

    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        owner_id: UUID,
        *,
        docker_secrets: Path = Path("/run/secrets"),
    ) -> None:
        if not isinstance(owner_id, UUID):
            raise ValueError("INVALID_PROVIDER_OWNER")
        super().__init__(settings, docker_secrets=docker_secrets)
        self._settings, self._engine, self._owner = settings, engine, owner_id

    def _current(self, provider: ProviderId) -> profiles.ManagedProfile | None:
        with unit_of_work(self._engine) as con:
            if (
                con.execute(
                    text("SELECT 1 FROM owners WHERE id=:owner AND is_active=1"),
                    {"owner": str(self._owner)},
                ).first()
                is None
            ):
                raise profiles.ProfileError("PROFILE_OWNER_UNAVAILABLE")
            return profiles.latest(con, self._owner, provider)

    def _managed(self, record: profiles.ManagedProfile) -> LoadedConfiguration:
        provider = record.policy.provider_id
        snapshot = profiles.policy_snapshot(record)
        if not snapshot.enabled:
            return LoadedConfiguration(provider, "PROVIDER_DISABLED", snapshot)
        if not snapshot.terms_acknowledged:
            return LoadedConfiguration(provider, "RIGHTS_NOT_ACKNOWLEDGED", snapshot)
        credential, signatures = None, None
        if provider in ("eodhd", "alphavantage", "massive"):
            # The owned controller/codec already rejects enabled API-key policies without a ref.
            assert record.credential_artifact_id is not None
            try:
                edition = credentials.managed_artifacts(self._settings).load(
                    credentials.CredentialRef(provider, record.credential_artifact_id)
                )
            except credentials.CredentialError as error:
                return LoadedConfiguration(provider, str(error), snapshot)
            credential, signatures = edition.credential, edition.revision
        return LoadedConfiguration(
            provider,
            error_code=None,
            profile=snapshot,
            access=ProviderAccess(enabled=True, terms_acknowledged=True, credential=credential),
            revision=ManagedConfigurationRevision(
                digest(canonical(snapshot.model_dump(mode="json"))), signatures
            ),
        )

    def load(self, provider_id: ProviderId) -> LoadedConfiguration:
        if provider_id not in registry():
            raise ValueError("Unsupported provider configuration")
        try:
            previous = self._current(provider_id)
            # No database connection/transaction remains open during either private-file reader.
            result = super().load(provider_id) if previous is None else self._managed(previous)
            if self._current(provider_id) != previous:
                return LoadedConfiguration(provider_id, "CONFIG_CHANGED_DURING_READ")
            return replace(result, selection_owner_id=self._owner)
        except profiles.ProfileError as error:
            code = (
                "PROVIDER_OWNER_UNAVAILABLE"
                if str(error) == "PROFILE_OWNER_UNAVAILABLE"
                else "MANAGED_PROFILE_UNAVAILABLE"
            )
            return LoadedConfiguration(provider_id, code)
        except SQLAlchemyError:
            return LoadedConfiguration(provider_id, "MANAGED_PROFILE_UNAVAILABLE")
