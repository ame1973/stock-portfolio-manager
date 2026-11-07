"""Read owned policies and private editions without writes, discovery or runtime adoption."""

from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine, text
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


class OwnerConfigurationStore(ProviderConfigurationStore):
    """Private selection core; importing it does not switch dispatch or diagnostic consumers."""

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
        snapshot = ManagedPolicySnapshot(
            **record.policy.model_dump(),
            version=record.version,
            revision_id=record.id,
            owner_id=record.owner_id,
            credential_artifact_id=record.credential_artifact_id,
        )
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
            return result
        except profiles.ProfileError as error:
            code = (
                "PROVIDER_OWNER_UNAVAILABLE"
                if str(error) == "PROFILE_OWNER_UNAVAILABLE"
                else "MANAGED_PROFILE_UNAVAILABLE"
            )
            return LoadedConfiguration(provider_id, code)
        except SQLAlchemyError:
            return LoadedConfiguration(provider_id, "MANAGED_PROFILE_UNAVAILABLE")
