"""Protected source quotations are distinct from canonical dividend event creation."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Request
from sqlalchemy import Connection

import spm_api.providers.cited as service
from spm_api.cited_models import CitedSourceCreated, CitedSourceInput
from spm_api.errors import ApiError, Error
from spm_api.mutations import has_replay, mutate
from spm_api.security import Auth, WriteDep

router = APIRouter(
    prefix="/api/v1/providers",
    tags=["Cited source assertions"],
    responses={code: {"model": Error} for code in (401, 403, 409, 422, 503)},
)


@router.post("/cited-observations", status_code=201, operation_id="create_cited_source_observation")
async def create_cited_observation(
    body: Annotated[CitedSourceInput, Body(discriminator="operation")],
    request: Request,
    headers: WriteDep,
) -> CitedSourceCreated:
    """Append one cited assertion, never fetch a document or adopt canonical financial terms."""
    payload = body.model_dump(mode="json")
    prepared: service.PreparedCitedEvidence | None = None
    try:
        if not has_replay(request, headers, payload):
            prepared = await service.prepare(
                request.app.state.settings,
                request.app.state.engine,
                headers.auth.owner.id,
                body,
            )

        def record(con: Connection, auth: Auth) -> CitedSourceCreated:
            if prepared is None:
                # A removed advisory receipt cannot authorize private preparation in the writer.
                raise ApiError(
                    409, "CITED_ASSERTION_RETRY_REQUIRED", "Repeat the citation with a fresh intent"
                )
            return service.save(
                con, auth.owner.id, prepared, request_id=UUID(request.state.request_id)
            )

        return mutate(request, headers, payload, CitedSourceCreated, record, status=201)
    except ValueError as error:
        code = str(error)
        if code in {"INVALID_CITED_ASSERTION", "CITED_SOURCE_IDENTITY_UNRESOLVED"}:
            raise ApiError(
                422, code, "Review the cited terms, attribution and dated source identity"
            ) from None
        if code == "CITED_SOURCE_UNCONFIGURED":
            raise ApiError(
                503, code, "Explicit current source access and rights are required"
            ) from None
        if code in {"PROVIDER_CONFIG_CHANGED", "PROVIDER_ALIAS_CHANGED"}:
            raise ApiError(
                409, "CITED_SOURCE_CHANGED", "Source access or identity changed; review and retry"
            ) from None
        if code in {"CITED_ACTOR_CHANGED", "INVALID_CITED_ACTOR"}:
            raise ApiError(401, "UNAUTHORIZED", "Sign in to continue") from None
        raise ApiError(
            503, "CITED_SOURCE_UNAVAILABLE", "The cited source assertion cannot be retained safely"
        ) from None
