"""Owned policy definitions and write-only private imports, never credential response values."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, field_validator

from spm_api.errors import Model
from spm_api.master_models import ChangeReason
from spm_api.providers.configuration import ProviderPolicy
from spm_api.providers.credentials import exact_credential


class ProviderProfileCreate(Model):
    policy: ProviderPolicy
    reason: ChangeReason


class ProviderProfileUpdate(ProviderProfileCreate):
    clear_credential: bool = Field(default=False, strict=True)


class ProviderCredentialImport(Model):
    credential: SecretStr = Field(strict=True, min_length=1, max_length=256, repr=False)
    reason: Annotated[ChangeReason, Field(pattern=r"^[^\x00]*$")]

    _exact = field_validator("credential")(exact_credential)


class ProviderProfileRecord(Model):
    id: UUID
    policy: ProviderPolicy
    version: int = Field(strict=True, ge=1, le=2147483647)
    reason: ChangeReason
    created_at: AwareDatetime
    credential_reference_present: bool
    runtime_selection_installed: bool
    scope: Literal["POLICY_DEFINITION_ONLY"] = "POLICY_DEFINITION_ONLY"
