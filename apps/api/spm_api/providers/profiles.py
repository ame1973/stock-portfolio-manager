"""Internal owned policy revisions; HTTP authorization, private I/O and runtime remain external."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, canonical, digest, stamp
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, ProviderId
from spm_api.mutations import check_version
from spm_api.providers.base import registry
from spm_api.providers.configuration import ProviderPolicy
from spm_api.providers.credentials import CredentialRef

_FLAGS = ("enabled", "terms_acknowledged", "raw_payload_storage")


class ProfileError(ValueError):
    """Fixed codes only; never include private rows, references or exception payloads."""


class ManagedProfile(Model):
    """Private definition, not a public DTO, active adapter or authenticated import receipt."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    owner_id: UUID = Field(repr=False)
    policy: ProviderPolicy = Field(repr=False)
    version: int = Field(strict=True, ge=1, le=2147483647)
    credential_artifact_id: UUID | None = Field(repr=False)
    change_reason: ChangeReason = Field(repr=False)
    request_id: UUID = Field(repr=False)
    created_at: Annotated[AwareDatetime, Field(strict=True)]

    @model_validator(mode="after")
    def explicit_selection(self) -> Self:
        mode = registry()[self.policy.provider_id].access_mode
        if self.credential_artifact_id is not None and mode != "API_KEY":
            raise ValueError("This profile does not accept API credentials")
        if self.policy.enabled and (
            self.version == 1
            or not self.policy.terms_acknowledged
            or (mode == "API_KEY" and self.credential_artifact_id is None)
        ):
            raise ValueError(
                "Enabled managed selection requires a prior definition and access terms"
            )
        if any(
            "\x00" in value
            for value in (
                self.change_reason,
                self.policy.terms_reference,
                self.policy.terms_reason or "",
            )
        ):
            raise ValueError("Policy references and reasons cannot contain NUL")
        return self


def _flat(record: ManagedProfile) -> dict[str, Any]:
    result = record.model_dump(mode="json", exclude={"policy", "created_at"})
    result.update(record.policy.model_dump(mode="json"))
    result["created_at"] = stamp(record.created_at.astimezone(UTC))
    for name in _FLAGS:
        result[name] = int(result[name])
    return result


def from_row(row: RowMapping) -> ManagedProfile:
    original = dict(row)
    try:
        values = dict(original)
        policy = {key: values.pop(key) for key in ProviderPolicy.model_fields}
        for name in _FLAGS:
            if type(policy[name]) is not int or policy[name] not in (0, 1):
                raise ValueError("Invalid stored boolean")
            policy[name] = bool(policy[name])
        values["policy"] = policy
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        result = ManagedProfile.model_validate(values)
        if _flat(result) != original:
            raise ValueError("Noncanonical stored definition")
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ProfileError("PROVIDER_PROFILE_UNAVAILABLE") from None


def _scope(owner: UUID, provider: ProviderId) -> None:
    if not isinstance(owner, UUID) or not isinstance(provider, str) or provider not in registry():
        raise ProfileError("INVALID_PROFILE_SCOPE")


def _foreign_reference(
    con: Connection, owner: UUID, provider: ProviderId, reference: UUID | None
) -> bool:
    if reference is None:
        return False
    return (
        con.execute(
            text("""
        SELECT 1 FROM provider_profile_revisions WHERE credential_artifact_id=:reference
          AND (owner_id!=:owner OR provider_id!=:provider) LIMIT 1
    """),
            {"reference": str(reference), "owner": str(owner), "provider": provider},
        ).first()
        is not None
    )


def latest(con: Connection, owner_id: UUID, provider_id: ProviderId) -> ManagedProfile | None:
    """Select the greatest version, including disabled/cleared; never fall back to files."""
    _scope(owner_id, provider_id)
    parameters = {"owner": str(owner_id), "provider": provider_id}
    row = (
        con.execute(
            text("""
        SELECT * FROM provider_profile_revisions WHERE owner_id=:owner AND provider_id=:provider
        ORDER BY version DESC LIMIT 1
    """),
            parameters,
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    record = from_row(row)
    progress = con.execute(
        text("""
        SELECT count(*),min(version),max(created_at) FROM provider_profile_revisions
        WHERE owner_id=:owner AND provider_id=:provider
    """),
        parameters,
    ).one()
    if tuple(progress) != (record.version, 1, stamp(record.created_at)) or _foreign_reference(
        con, owner_id, provider_id, record.credential_artifact_id
    ):
        raise ProfileError("PROVIDER_PROFILE_UNAVAILABLE")
    return record


def _credential(
    value: CredentialRef | Literal["KEEP"] | None,
    previous: ManagedProfile | None,
    provider: ProviderId,
) -> UUID | None:
    if value == "KEEP":
        return previous.credential_artifact_id if previous is not None else None
    if value is None:
        return None
    if isinstance(value, CredentialRef):
        try:
            value.__post_init__()
            if value.provider_id == provider:
                return value.artifact_id
        except ValueError:
            pass
    raise ProfileError("INVALID_PROFILE_CREDENTIAL")


def _audit_values(record: ManagedProfile) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "provider_id": record.policy.provider_id,
        "version": record.version,
        "enabled": record.policy.enabled,
        "credential_present": record.credential_artifact_id is not None,
        "policy_sha256": digest(canonical(record.policy.model_dump(mode="json"))),
    }


def save(
    con: Connection,
    owner_id: UUID,
    policy: ProviderPolicy,
    *,
    expected_version: int | None,
    at: datetime,
    reason: str,
    request_id: UUID,
    credential: CredentialRef | Literal["KEEP"] | None = "KEEP",
) -> ManagedProfile:
    """Append selection/audit in a caller-owned writer; references must be checked before entry."""
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise ProfileError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(policy, ProviderPolicy) or not isinstance(request_id, UUID):
        raise ProfileError("INVALID_PROVIDER_PROFILE")
    try:
        policy = ProviderPolicy.model_validate(policy.model_dump(mode="json"))
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ValueError("Explicit aware clock required")
        clock = at.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise ProfileError("INVALID_PROVIDER_PROFILE") from None
    _scope(owner_id, policy.provider_id)
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner_id)}
        ).first()
        is None
    ):
        raise ProfileError("PROFILE_OWNER_UNAVAILABLE")
    if expected_version is not None and (
        type(expected_version) is not int or not 1 <= expected_version <= 2147483647
    ):
        raise ProfileError("INVALID_PROFILE_VERSION")
    previous = latest(con, owner_id, policy.provider_id)
    if expected_version is None:
        if previous is not None:
            raise ProfileError("PROFILE_ALREADY_MANAGED")
    else:
        if previous is None:
            raise ProfileError("PROFILE_NOT_FOUND")
        check_version(previous.version, expected_version)
    if previous is not None:
        if clock < previous.created_at:
            raise ProfileError("PROFILE_CLOCK_REGRESSION")
        if previous.version == 2147483647:
            raise ProfileError("PROFILE_VERSION_EXHAUSTED")
    reference = _credential(credential, previous, policy.provider_id)
    if _foreign_reference(con, owner_id, policy.provider_id, reference):
        raise ProfileError("PROFILE_CREDENTIAL_SCOPE")
    try:
        record = ManagedProfile(
            id=uuid4(),
            owner_id=owner_id,
            policy=policy,
            version=previous.version + 1 if previous is not None else 1,
            credential_artifact_id=reference,
            change_reason=reason,
            request_id=request_id,
            created_at=clock,
        )
    except ValidationError:
        raise ProfileError("INVALID_PROVIDER_PROFILE") from None
    values = _flat(record)
    con.execute(
        text(
            f"INSERT INTO provider_profile_revisions({','.join(values)}) "
            f"VALUES ({','.join(':' + key for key in values)})"
        ),
        values,
    )
    audit(
        con,
        str(owner_id),
        "provider_profile_revision",
        str(record.id),
        "PROVIDER_PROFILE_CREATED" if previous is None else "PROVIDER_PROFILE_REVISED",
        before=_audit_values(previous) if previous is not None else None,
        after=_audit_values(record),
        reason=record.change_reason,
        request_id=str(request_id),
    )
    return record
