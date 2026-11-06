"""Authenticated source-only observation lists do not fetch, reconcile or adopt financial facts."""

from uuid import UUID

from fastapi import APIRouter, Request

import spm_api.observation_service as service
from spm_api.db import unit_of_work
from spm_api.decimal_types import IsoDate
from spm_api.errors import Error
from spm_api.master_models import ProviderId
from spm_api.observation_models import (
    DividendObservationRecord,
    ObservationPage,
    SplitObservationRecord,
)
from spm_api.pagination import PageDep
from spm_api.security import AuthDep

router = APIRouter(
    prefix="/api/v1",
    tags=["Source observations"],
    responses={code: {"model": Error} for code in (401, 403, 404, 422, 503)},
)


@router.get("/dividends/observations", operation_id="list_dividend_observations")
def dividends(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    security_id: UUID | None = None,
    provider_id: ProviderId | None = None,
    from_date: IsoDate | None = None,
    to_date: IsoDate | None = None,
) -> ObservationPage[DividendObservationRecord]:
    with unit_of_work(request.app.state.engine) as con:
        return service.observations(
            con,
            page,
            "DIVIDENDS",
            service.as_dividend,
            security_id=security_id,
            provider_id=provider_id,
            from_date=from_date,
            to_date=to_date,
        )


@router.get("/providers/split-observations", operation_id="list_split_observations")
def splits(
    request: Request,
    auth: AuthDep,
    page: PageDep,
    security_id: UUID | None = None,
    provider_id: ProviderId | None = None,
    from_date: IsoDate | None = None,
    to_date: IsoDate | None = None,
) -> ObservationPage[SplitObservationRecord]:
    with unit_of_work(request.app.state.engine) as con:
        return service.observations(
            con,
            page,
            "SPLITS",
            service.as_split,
            security_id=security_id,
            provider_id=provider_id,
            from_date=from_date,
            to_date=to_date,
        )
