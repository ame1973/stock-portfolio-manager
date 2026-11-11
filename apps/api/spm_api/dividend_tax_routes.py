"""Owner-scoped tax estimates; no projection, receipt, or confirmed withholding."""

import re
from datetime import date
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text

import spm_api.dividend_event_service as events
import spm_api.dividend_tax_models as models
import spm_api.dividend_tax_service as tax
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX, IsoDate
from spm_api.errors import ApiError, Error
from spm_api.master_service import owned_account, owned_broker
from spm_api.mutations import check_version, mutate
from spm_api.pagination import Page, PageDep, paginate
from spm_api.security import Auth, AuthDep, WriteDep

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings",
    tags=["Dividend tax estimates"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _version(
    value: Annotated[str, Header(alias="If-Match", max_length=12, pattern=r'^"[1-9][0-9]{0,9}"$')],
) -> int:
    if re.fullmatch(r'"[1-9][0-9]{0,9}"', value) is None:
        raise ApiError(422, "INVALID_EVENT_VERSION", "The event version is invalid")
    number = int(value[1:-1])
    if number > 2_147_483_647:
        raise ApiError(422, "INVALID_EVENT_VERSION", "The event version is invalid")
    return number


EventVersion = Annotated[int, Depends(_version)]
HistoryVersion = Annotated[int, Query(ge=1, le=2_147_483_647)]


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIVIDEND_TAX_HISTORY_UNAVAILABLE", "The tax history cannot be read safely"
    )


def _failure(error: events.EventError | tax.TaxSettingError) -> ApiError:
    code = str(error)
    if code in {"DIVIDEND_TAX_NOT_FOUND", "DIVIDEND_EVENT_NOT_FOUND"}:
        return ApiError(404, "NOT_FOUND", "Dividend tax scope not found")
    if code == "DIVIDEND_TAX_OWNER_UNAVAILABLE":
        return ApiError(401, "UNAUTHORIZED", "Authentication is required")
    if code == "INVALID_DIVIDEND_TAX_SCOPE":
        return ApiError(422, code, "The tax setting is invalid")
    if code in {"DIVIDEND_TAX_VERSION_CONFLICT", "DIVIDEND_EVENT_VERSION_CONFLICT"}:
        return ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
    if code in {
        "DIVIDEND_TAX_CONFLICT",
        "DIVIDEND_TAX_SCOPE_CHANGED",
        "DIVIDEND_TAX_SETTING_CHANGED",
        "DIVIDEND_EVENT_SOURCE_CHANGED",
        "CANDIDATE_NOT_FOUND",
        "CANDIDATE_VERSION_CONFLICT",
        "CANDIDATE_DISMISSED",
        "CANDIDATE_SOURCE_UNAVAILABLE",
        "CANDIDATE_SOURCE_CHANGED",
        "CANDIDATE_BASIS_UNAVAILABLE",
        "CANDIDATE_BASIS_CHANGED",
    }:
        return ApiError(409, code, "The tax setting no longer matches its observed scope")
    return _unavailable()


def _event(con: Connection, owner: UUID, event_id: UUID) -> events.ReviewedEvent:
    result = events.get(con, owner, event_id)
    if result is None:
        raise tax.TaxSettingError("DIVIDEND_TAX_NOT_FOUND")
    return result


def _history(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    revision_id: UUID,
    revision_no: int,
    account_id: UUID,
) -> tuple[tax.TaxSetting, ...]:
    result = tax.history(con, owner, event_id, revision_id, revision_no, account_id)
    if result is None:
        raise tax.TaxSettingError("DIVIDEND_TAX_NOT_FOUND")
    return result


def _public(record: tax.TaxSetting) -> models.TaxSettingRecord:
    return models.TaxSettingRecord(
        id=record.id,
        event_id=record.event_id,
        revision_id=record.revision_id,
        revision_no=record.revision_no,
        account_id=record.account_id,
        portfolio_id=record.portfolio_id,
        broker_id=record.broker_id,
        account_version=str(record.account_version),
        broker_version=str(record.broker_version),
        setting_no=str(record.setting_no),
        previous_id=record.previous_id,
        effective_from=record.effective_from,
        tax_status=record.tax_status,
        rate=record.rate,
        reason=record.reason,
        created_at=record.created_at,
        scope="OWNER_ESTIMATE_ONLY",
        current_use_scope="REVALIDATION_REQUIRED",
    )


@router.get("", operation_id="get_dividend_tax_estimate")
def current_estimate(
    event_id: UUID,
    account_id: UUID,
    request: Request,
    auth: AuthDep,
    effective_on: IsoDate,
) -> models.TaxSettingSelection:
    try:
        with unit_of_work(request.app.state.engine) as con:
            event = _event(con, auth.owner.id, event_id).record
            settings = _history(
                con, auth.owner.id, event_id, event.revision_id, event.revision_no, account_id
            )
            selected = tax.select(settings, date.fromisoformat(effective_on))
            account = owned_account(con, str(auth.owner.id), str(account_id))
            broker = owned_broker(con, str(auth.owner.id), account["broker_id"])
            matched = selected.selected is not None and (
                str(selected.selected.portfolio_id) == account["portfolio_id"]
                and str(selected.selected.broker_id) == account["broker_id"]
                and selected.selected.account_version == account["version"]
                and selected.selected.broker_version == broker["version"]
            )
            scope: Literal["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]
            if selected.selected is None:
                scope = "NO_SELECTED_SETTING"
            else:
                scope = "ACCOUNT_SCOPE_MATCH" if matched else "ACCOUNT_SCOPE_CHANGED"
            return models.TaxSettingSelection(
                event_id=event_id,
                revision_id=event.revision_id,
                revision_no=event.revision_no,
                account_id=account_id,
                portfolio_id=account["portfolio_id"],
                broker_id=account["broker_id"],
                account_version=str(account["version"]),
                broker_version=str(broker["version"]),
                effective_on=effective_on,
                head_no=str(selected.head_no),
                head_id=selected.head_id,
                tax_status=selected.status,
                selected=_public(selected.selected) if selected.selected is not None else None,
                selected_scope=scope,
                scope="OWNER_ESTIMATE_ONLY",
                current_use_scope="REVALIDATION_REQUIRED",
            )
    except (events.EventError, tax.TaxSettingError) as error:
        raise _failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.get("/history", operation_id="list_dividend_tax_estimate_history")
def estimate_history(
    event_id: UUID,
    account_id: UUID,
    request: Request,
    auth: AuthDep,
    page: PageDep,
    revision_id: UUID,
    revision_no: HistoryVersion,
) -> Page[models.TaxSettingRecord]:
    cursor = page.cursor_value
    if cursor is not None and (
        re.fullmatch(r"[1-9][0-9]{0,18}", cursor) is None or int(cursor) > INT64_MAX
    ):
        raise ApiError(422, "INVALID_CURSOR", "The tax history cursor is invalid")
    try:
        with unit_of_work(request.app.state.engine) as con:
            settings = _history(con, auth.owner.id, event_id, revision_id, revision_no, account_id)
            records = {str(record.id): record for record in settings}
            rows = (
                con.execute(
                    text(r"""
                    SELECT id,setting_no FROM dividend_tax_settings
                    WHERE owner_id=:owner AND event_id=:event AND revision_id=:revision
                      AND account_id=:account
                      AND (id LIKE :search ESCAPE '\'
                        OR CAST(setting_no AS TEXT) LIKE :search ESCAPE '\')
                      AND (:cursor_value IS NULL OR (setting_no,id)<(:cursor_value,:cursor_id))
                    ORDER BY setting_no DESC,id DESC LIMIT :limit
                """),
                    {
                        **page.params(),
                        "owner": str(auth.owner.id),
                        "event": str(event_id),
                        "revision": str(revision_id),
                        "account": str(account_id),
                        "cursor_value": int(cursor) if cursor is not None else None,
                    },
                )
                .mappings()
                .all()
            )
            return paginate(rows, page, "setting_no", lambda row: _public(records[row["id"]]))
    except (events.EventError, tax.TaxSettingError) as error:
        raise _failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None


@router.post("", status_code=201, operation_id="save_dividend_tax_estimate")
def save_estimate(
    event_id: UUID,
    account_id: UUID,
    body: models.TaxSettingWrite,
    request: Request,
    headers: WriteDep,
    version: EventVersion,
) -> models.TaxSettingRecord:
    expected = int(body.expected_head_no)

    def action(con: Connection, auth: Auth) -> models.TaxSettingRecord:
        current = _event(con, auth.owner.id, event_id).record
        check_version(current.revision_no, version)
        if current.revision_id != body.revision_id:
            raise ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
        settings = _history(con, auth.owner.id, event_id, body.revision_id, version, account_id)
        check_version(len(settings), expected)
        if (settings[-1].id if settings else None) != body.expected_head_id:
            raise ApiError(412, "PRECONDITION_FAILED", "The observed scope changed; reload it")
        intent = tax.SettingIntent(
            id=uuid4(),
            event_id=event_id,
            revision_id=body.revision_id,
            revision_no=version,
            account_id=account_id,
            portfolio_id=body.portfolio_id,
            broker_id=body.broker_id,
            account_version=int(body.account_version),
            broker_version=int(body.broker_version),
            expected_setting_no=expected,
            effective_from=body.effective_from,
            rate=body.rate,
            reason=body.reason,
        )
        return _public(
            tax.save(con, auth.owner.id, intent, request_id=UUID(request.state.request_id))
        )

    def replay(con: Connection, auth: Auth, cached: models.TaxSettingRecord) -> None:
        settings = tax.history(con, auth.owner.id, event_id, body.revision_id, version, account_id)
        if settings is None or len(settings) <= expected:
            raise tax.TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE")
        original = settings[expected]
        if (
            _public(original) != cached
            or original.event_id != event_id
            or original.account_id != account_id
            or original.revision_id != body.revision_id
            or original.revision_no != version
            or original.setting_no != expected + 1
            or original.previous_id != body.expected_head_id
            or original.portfolio_id != body.portfolio_id
            or original.broker_id != body.broker_id
            or original.account_version != int(body.account_version)
            or original.broker_version != int(body.broker_version)
            or original.effective_from != body.effective_from
            or original.rate != body.rate
            or original.reason != body.reason
        ):
            raise tax.TaxSettingError("DIVIDEND_TAX_HISTORY_UNAVAILABLE")

    try:
        return mutate(
            request,
            headers,
            {"event_version": version, **body.model_dump(mode="json")},
            models.TaxSettingRecord,
            action,
            status=201,
            reauth=True,
            validate_replay=replay,
        )
    except (events.EventError, tax.TaxSettingError) as error:
        raise _failure(error) from None
    except (ValueError, TypeError, KeyError, OverflowError, PydanticSerializationError):
        raise _unavailable() from None
