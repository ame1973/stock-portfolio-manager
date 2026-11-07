"""Reauthenticated owned policies and disabled private imports for explicit runtime selection."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import Connection, text

import spm_api.providers.credentials as credentials
import spm_api.providers.profiles as service
from spm_api.audit import audit, now
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error
from spm_api.master_models import ProviderId
from spm_api.mutations import check_version, has_replay, mutate, sensitive_fingerprint
from spm_api.pagination import Page, PageDep, paginate, validate_version_page
from spm_api.profile_models import (
    ProviderCredentialImport,
    ProviderProfileCreate,
    ProviderProfileRecord,
    ProviderProfileUpdate,
)
from spm_api.providers.base import registry
from spm_api.security import Auth, AuthDep, VersionDep, WriteDep

router = APIRouter(
    prefix="/api/v1/provider-profiles",
    tags=["Managed provider definitions"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _public(record: service.ManagedProfile) -> ProviderProfileRecord:
    return ProviderProfileRecord(
        id=record.id,
        policy=record.policy,
        version=record.version,
        reason=record.change_reason,
        created_at=record.created_at,
        credential_reference_present=record.credential_artifact_id is not None,
        runtime_selection_installed=True,
    )


def _owned(con: Connection, owner: UUID, provider: ProviderId) -> service.ManagedProfile:
    record = service.latest(con, owner, provider)
    if record is None:
        raise service.ProfileError("PROFILE_NOT_FOUND")
    return record


@router.get("", operation_id="list_provider_profiles")
def definitions(request: Request, auth: AuthDep) -> Page[ProviderProfileRecord]:
    """Return at most the eight explicitly managed profiles, not operator files or health."""
    with unit_of_work(request.app.state.engine) as con:
        return Page(
            items=[
                _public(record)
                for provider in sorted(registry())
                if (record := service.latest(con, auth.owner.id, provider)) is not None
            ]
        )


@router.post("", status_code=201, operation_id="create_provider_profile")
def create_definition(
    body: ProviderProfileCreate, request: Request, response: Response, headers: WriteDep
) -> ProviderProfileRecord:
    result = mutate(
        request,
        headers,
        body.model_dump(mode="json"),
        ProviderProfileRecord,
        lambda con, auth: _public(
            service.save(
                con,
                auth.owner.id,
                body.policy,
                expected_version=None,
                at=now(),
                reason=body.reason,
                request_id=UUID(request.state.request_id),
            )
        ),
        status=201,
        reauth=True,
    )
    response.headers["Location"] = f"/api/v1/provider-profiles/{result.policy.provider_id}"
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/{provider_id}", operation_id="get_provider_profile")
def definition(
    provider_id: ProviderId, request: Request, response: Response, auth: AuthDep
) -> ProviderProfileRecord:
    with unit_of_work(request.app.state.engine) as con:
        result = _public(_owned(con, auth.owner.id, provider_id))
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/{provider_id}", operation_id="update_provider_profile")
def update_definition(
    provider_id: ProviderId,
    body: ProviderProfileUpdate,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> ProviderProfileRecord:
    if provider_id != body.policy.provider_id:
        raise ApiError(
            422, "INVALID_PROVIDER_PROFILE", "A provider definition cannot change identity"
        )
    result = mutate(
        request,
        headers,
        {"version": version, **body.model_dump(mode="json")},
        ProviderProfileRecord,
        lambda con, auth: _public(
            service.save(
                con,
                auth.owner.id,
                body.policy,
                expected_version=version,
                at=now(),
                reason=body.reason,
                request_id=UUID(request.state.request_id),
                credential=None if body.clear_credential else "KEEP",
            )
        ),
        reauth=True,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.post("/{provider_id}/credential", operation_id="import_provider_credential")
def import_credential(
    provider_id: ProviderId,
    body: ProviderCredentialImport,
    request: Request,
    response: Response,
    headers: WriteDep,
    version: VersionDep,
) -> ProviderProfileRecord:
    """Bind a prepared edition to a new disabled definition; never enable or probe a provider."""
    if provider_id not in ("eodhd", "alphavantage", "massive"):
        raise ApiError(422, "INVALID_PROVIDER_CREDENTIAL", "This provider does not accept a key")
    if not 1 <= version <= 2147483647:
        raise service.ProfileError("INVALID_PROFILE_VERSION")
    fingerprint = sensitive_fingerprint(
        request,
        {
            "version": version,
            "credential": body.credential.get_secret_value(),
            "reason": body.reason,
        },
    )
    previous: service.ManagedProfile | None = None
    prepared: credentials.CredentialSnapshot | None = None
    if not has_replay(request, headers, fingerprint, reauth=True):
        with unit_of_work(request.app.state.engine) as con:
            previous = _owned(con, headers.auth.owner.id, provider_id)
            check_version(previous.version, version)
            if previous.version == 2147483647:
                raise service.ProfileError("PROFILE_VERSION_EXHAUSTED")
        store = credentials.managed_artifacts(request.app.state.settings)
        prepared = store.prepare(provider_id, body.credential)
        if not store.unchanged(prepared):
            raise credentials.CredentialError("CREDENTIAL_ARTIFACT_CHANGED")

    def bind(con: Connection, auth: Auth) -> ProviderProfileRecord:
        if previous is None or prepared is None:
            # A removed advisory replay is not permission to create files inside the writer.
            raise ApiError(
                409, "CREDENTIAL_RETRY_REQUIRED", "Repeat the import with a fresh intent"
            )
        if auth.owner.id != previous.owner_id:
            raise ApiError(401, "UNAUTHORIZED", "Sign in to continue")
        record = service.save(
            con,
            auth.owner.id,
            previous.policy.model_copy(update={"enabled": False}),
            expected_version=version,
            at=now(),
            reason=body.reason,
            request_id=UUID(request.state.request_id),
            credential=prepared.reference,
        )
        audit(
            con,
            str(auth.owner.id),
            "provider_profile_revision",
            str(record.id),
            "PROVIDER_CREDENTIAL_IMPORTED",
            after={
                "provider_id": provider_id,
                "version": record.version,
                "credential_present": True,
            },
            reason=body.reason,
            request_id=request.state.request_id,
        )
        return _public(record)

    result = mutate(request, headers, fingerprint, ProviderProfileRecord, bind, reauth=True)
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.get("/{provider_id}/revisions", operation_id="list_provider_profile_revisions")
def revisions(
    provider_id: ProviderId, request: Request, auth: AuthDep, page: PageDep
) -> Page[ProviderProfileRecord]:
    validate_version_page(page)
    with unit_of_work(request.app.state.engine) as con:
        _owned(con, auth.owner.id, provider_id)
        rows = (
            con.execute(
                text(r"""
            SELECT * FROM provider_profile_revisions
            WHERE owner_id=:owner AND provider_id=:provider
              AND (id LIKE :search ESCAPE '\' OR change_reason LIKE :search ESCAPE '\'
                    OR CAST(version AS TEXT) LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (version,id)<(:cursor_value,:cursor_id))
            ORDER BY version DESC,id DESC LIMIT :limit
        """),
                {
                    **page.params(),
                    "owner": str(auth.owner.id),
                    "provider": provider_id,
                    "cursor_value": int(page.cursor_value)
                    if page.cursor_value is not None
                    else None,
                },
            )
            .mappings()
            .all()
        )
        return paginate(rows, page, "version", lambda row: _public(service.from_row(row)))
