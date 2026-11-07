"""Owned managed policy definitions, never credential values or runtime-health assertions."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from spm_api.errors import Model
from spm_api.master_models import ChangeReason
from spm_api.providers.configuration import ProviderPolicy


class ProviderProfileCreate(Model):
    policy: ProviderPolicy
    reason: ChangeReason


class ProviderProfileUpdate(ProviderProfileCreate):
    clear_credential: bool = Field(default=False, strict=True)


class ProviderProfileRecord(Model):
    id: UUID
    policy: ProviderPolicy
    version: int = Field(strict=True, ge=1, le=2147483647)
    reason: ChangeReason
    created_at: AwareDatetime
    credential_reference_present: bool
    runtime_selection_installed: bool
    scope: Literal["POLICY_DEFINITION_ONLY"] = "POLICY_DEFINITION_ONLY"
