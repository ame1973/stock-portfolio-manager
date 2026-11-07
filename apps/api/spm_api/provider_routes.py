"""Authenticated read-only diagnostics never perform upstream requests or adopt market coverage."""

import json
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import text

import spm_api.provider_service as service
from spm_api.db import unit_of_work
from spm_api.errors import ApiError, Error
from spm_api.job_models import SourceOperation
from spm_api.master_models import ProviderId
from spm_api.master_service import security_row
from spm_api.pagination import Page, PageDep, paginate
from spm_api.provider_models import ProviderCoverage, ProviderHealth, ProviderInfo
from spm_api.providers.base import registry
from spm_api.providers.runtime import check_configuration, check_configurations
from spm_api.providers.selection import OwnerConfigurationStore
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1/providers",
    tags=["Providers"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


@router.get("", operation_id="list_providers")
async def providers(request: Request, auth: AuthDep) -> Page[ProviderInfo]:
    reports = await check_configurations(
        request.app.state.settings,
        store=OwnerConfigurationStore(
            request.app.state.settings, request.app.state.engine, auth.owner.id
        ),
    )
    return Page(
        items=[
            service.as_provider(report) for report in sorted(reports, key=lambda r: r.provider_id)
        ]
    )


@router.get("/coverage", operation_id="get_provider_coverage")
async def coverage(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    security_id: UUID | None = None,
    provider_id: ProviderId | None = None,
    operation: SourceOperation = "DIVIDENDS",
) -> Page[ProviderCoverage]:
    """A requested/parsed window is never returned as authoritative coverage_start/end."""
    if page.cursor_value is not None and page.cursor_value not in registry():
        raise ApiError(422, "INVALID_CURSOR", "Invalid provider coverage cursor")
    selected = (provider_id,) if provider_id is not None else tuple(sorted(registry()))
    store = OwnerConfigurationStore(
        request.app.state.settings, request.app.state.engine, auth.owner.id
    )
    reports = {
        provider: await check_configuration(request.app.state.settings, provider, store=store)
        for provider in selected
    }
    with unit_of_work(request.app.state.engine) as con:
        if security_id is not None:
            security_row(con, str(security_id))
        rows = (
            con.execute(
                text(r"""
            SELECT s.id,e.market,p.value AS provider_key,
              EXISTS(SELECT 1 FROM provider_status ps
                WHERE ps.provider_id=p.value AND ps.security_id=s.id) AS legacy_status_ignored
            FROM securities s JOIN exchanges e ON e.code=s.exchange_code
            CROSS JOIN json_each(:providers) p
            WHERE (:security IS NULL OR s.id=:security)
              AND (s.display_ticker LIKE :search ESCAPE '\' OR p.value LIKE :search ESCAPE '\')
              AND (:cursor_value IS NULL OR (p.value,s.id)>(:cursor_value,:cursor_id))
            ORDER BY p.value,s.id LIMIT :limit
        """),
                {
                    **page.params(),
                    "providers": json.dumps(selected),
                    "security": str(security_id) if security_id is not None else None,
                },
            )
            .mappings()
            .all()
        )
        return paginate(
            rows,
            page,
            "provider_key",
            lambda row: service.as_coverage(con, row, reports[row["provider_key"]], operation),
        )


@router.get("/{provider_id}/health", operation_id="get_provider_health")
async def health(provider_id: ProviderId, request: Request, auth: AuthDep) -> ProviderHealth:
    configuration = await check_configuration(
        request.app.state.settings,
        provider_id,
        store=OwnerConfigurationStore(
            request.app.state.settings, request.app.state.engine, auth.owner.id
        ),
    )
    with unit_of_work(request.app.state.engine) as con:
        return service.as_health(con, configuration)
