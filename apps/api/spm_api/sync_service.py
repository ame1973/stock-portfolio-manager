"""Safe source-job DTOs and HTTP intents reuse the fenced controller, never performing HTTP."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, RowMapping, text

import spm_api.job_service as jobs
from spm_api.audit import now
from spm_api.config import Settings
from spm_api.errors import ApiError
from spm_api.job_models import ProviderSelection, SourceJobPlan
from spm_api.master_models import ProviderId
from spm_api.pagination import PageQuery
from spm_api.providers.selection import OwnerConfigurationStore
from spm_api.providers.symbols import FetchWindow
from spm_api.security import Auth
from spm_api.sync_models import (
    JobCreated,
    SourceJob,
    SourceJobItem,
    SourceJobResult,
    SyncDividendRequest,
)


def selections(
    settings: Settings, engine: Engine, owner_id: UUID, provider_ids: tuple[ProviderId, ...]
) -> tuple[ProviderSelection, ...]:
    """Read the explicit owner's selection before mutate; never infer an owner from files."""
    store = OwnerConfigurationStore(settings, engine, owner_id)
    result = []
    for provider in provider_ids:
        loaded = store.load(provider)
        result.append(
            ProviderSelection(
                provider_id=provider,
                profile_sha256=loaded.revision.profile_sha256
                if loaded.error_code is None and loaded.revision is not None
                else None,
            )
        )
    return tuple(result)


def enqueue(
    con: Connection,
    auth: Auth,
    body: SyncDividendRequest,
    selected: tuple[ProviderSelection, ...],
    request_id: str,
    *,
    selection_owner_id: UUID,
) -> JobCreated:
    if auth.owner.id != selection_owner_id:
        raise ApiError(409, "SOURCE_SELECTION_CHANGED", "Prepare the source selection again")
    plan = SourceJobPlan(
        security_ids=body.security_ids,
        providers=selected,
        window=FetchWindow(start=body.from_date, end=body.to_date),
        operations=body.operations,
        force=body.force,
    )
    try:
        row = jobs.enqueue(
            con, auth.owner.id, plan, intent_id=uuid4(), at=now(), request_id=request_id
        )
    except jobs.JobError as error:
        if str(error) == "JOB_SECURITY_NOT_FOUND":
            raise ApiError(
                422, "INVALID_SECURITY_SCOPE", "A requested security is unavailable"
            ) from None
        raise ApiError(
            409, "JOB_REQUEST_REJECTED", "The job request could not be accepted"
        ) from None
    return JobCreated(job_id=UUID(row["id"]))


def owned_job(con: Connection, owner_id: UUID, job_id: UUID) -> dict[str, Any]:
    row = (
        con.execute(
            text("""
        SELECT * FROM jobs WHERE id=:id AND owner_id=:owner AND runtime_version=1 AND kind=:kind
    """),
            {"id": str(job_id), "owner": str(owner_id), "kind": jobs.KIND},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Managed source job not found")
    return dict(row)


def as_job(con: Connection, row: Mapping[str, Any] | RowMapping) -> SourceJob:
    progress, result = jobs._summary(con, dict(row))
    public = {
        name: row[name]
        for name in SourceJob.model_fields
        if name not in {"scope", "canonical_reconciliation", "result", "progress_done"}
    }
    return SourceJob.model_validate(
        {**public, "progress_done": progress, "result": SourceJobResult.model_validate_json(result)}
    )


def as_item(row: RowMapping) -> SourceJobItem:
    return SourceJobItem.model_validate({name: row[name] for name in SourceJobItem.model_fields})


def cancel(con: Connection, auth: Auth, job_id: UUID, request_id: str) -> SourceJob:
    owned_job(con, auth.owner.id, job_id)
    try:
        row = jobs.request_cancel(con, auth.owner.id, job_id, at=now(), request_id=request_id)
    except jobs.JobError:
        raise ApiError(
            409, "JOB_CANCEL_REJECTED", "The cancellation could not be accepted"
        ) from None
    return as_job(con, row)


def validate_item_cursor(page: PageQuery) -> None:
    value = page.cursor_value
    if value is not None and (
        not value.isascii()
        or not value.isdigit()
        or len(value) > 4
        or not 0 <= int(value) <= 1599
        or str(int(value)) != value
    ):
        raise ApiError(422, "INVALID_CURSOR", "Invalid work-item pagination cursor")
