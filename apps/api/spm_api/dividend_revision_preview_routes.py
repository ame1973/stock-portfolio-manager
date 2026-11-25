"""Private, read-only hypothetical dividend-revision comparisons."""

import hmac
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_preview_models as models
from spm_api.db import unit_of_work
from spm_api.dividend_eligibility import AccountEligibility
from spm_api.dividend_revision_scope import AccountPins, PortfolioLedger
from spm_api.errors import ApiError, Error
from spm_api.mutations import check_version, sensitive_fingerprint
from spm_api.security import COOKIE, DividendEventVersionDep, WriteDep, read_auth, validate_csrf

router = APIRouter(
    prefix="/api/v1/dividends/events/{event_id}",
    tags=["Hypothetical dividend revision preview"],
    responses={code: {"model": Error} for code in (401, 403, 404, 409, 412, 422, 503)},
)


def _unavailable() -> ApiError:
    return ApiError(
        503, "DIVIDEND_REVISION_PREVIEW_UNAVAILABLE", "The revision preview cannot be read safely"
    )


def _amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("Invalid hypothetical amount")
    return format(value, "f")


def _ordinal(value: object) -> str:
    if type(value) is not int:
        raise ValueError("Invalid hypothetical ordinal")
    return str(value)


def _day(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not date:
        raise ValueError("Invalid hypothetical date")
    return value.isoformat()


def _account(
    portfolio: PortfolioLedger,
    pin: AccountPins,
    observation: AccountEligibility,
    estimate: preview.AccountEstimate,
    current: preview.AccountPreview,
) -> dict[str, Any]:
    if (
        not isinstance(current, preview.AccountPreview)
        or current.account_id != pin.account_id
        or estimate.account_id != pin.account_id
        or observation.account_id != str(pin.account_id)
        or observation.state != current.eligibility_state
    ):
        raise ValueError("Inconsistent hypothetical account")
    accrual = current.proposed_accrual
    return {
        "account_id": pin.account_id,
        "portfolio_id": portfolio.portfolio.id,
        "broker_id": pin.broker_id,
        "account_status": pin.account_status,
        "broker_status": pin.broker_status,
        "account_version": _ordinal(pin.account_version),
        "broker_version": _ordinal(pin.broker_version),
        "portfolio_version": _ordinal(portfolio.portfolio.version),
        "portfolio_data_revision": _ordinal(portfolio.portfolio.data_revision),
        "retired_trade_scope": portfolio.book.trade_history_audit_scope,
        "creation_audit_scope": portfolio.book.trade_creation_audit_scope,
        "eligibility_state": observation.state,
        "recorded_quantity": observation.recorded_quantity,
        "eligibility_reasons": observation.reasons,
        "pending_action_ids": observation.pending_action_ids,
        "estimate": estimate.model_dump(warnings="error"),
        "previous_projection_id": current.previous_projection_id,
        "previous_revision_id": current.previous_revision_id,
        "previous_as_of": _day(current.previous_as_of),
        "previous_currency": current.previous_currency,
        "previous_expected": _amount(current.previous_expected),
        "proposed_amounts": None
        if accrual is None
        else {
            "currency": accrual.currency,
            "gross_local": _amount(accrual.gross_local),
            "tax_local": _amount(accrual.tax_local),
            "fee_local": _amount(accrual.fee_local),
            "net_local": _amount(accrual.net_local),
            "rounding_delta_local": _amount(accrual.rounding_delta_local),
            "estimated_hkd": _amount(accrual.booked_hkd),
            "quality": accrual.quality,
        },
        "expected_delta": _amount(current.expected_delta),
        "recorded_received": _amount(current.recorded_received),
        "recorded_outstanding": _amount(current.recorded_outstanding),
        "active_receipts": _ordinal(current.active_receipts),
        "void_receipts": _ordinal(current.void_receipts),
        "unverified_origins": _ordinal(current.unverified_origins),
        "unavailable_cash_links": _ordinal(current.unavailable_cash_links),
        "comparison_quality": current.comparison_quality,
        "state": current.state,
    }


def _owned_accounts(con: Connection, owner: UUID) -> dict[UUID, tuple[object, ...]]:
    rows = con.execute(
        text("""
            SELECT a.id,a.portfolio_id,a.broker_id,a.status,b.status AS broker_status,
                   a.version AS account_version,b.version AS broker_version,
                   p.version AS portfolio_version,p.data_revision
            FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
            LEFT JOIN brokers b ON b.id=a.broker_id
            WHERE p.owner_id=:owner ORDER BY a.id
        """),
        {"owner": str(owner)},
    )
    return {events._uuid(row.id): tuple(row)[1:] for row in rows}


def _accounts(
    result: preview.RevisionPreview,
    owner: UUID,
    event: events.EventRevision,
    proposal: preview.RevisionProposal,
    database: dict[UUID, tuple[object, ...]],
) -> list[dict[str, Any]]:
    snapshot = result.snapshot
    if (
        result.proposal != proposal
        or snapshot.retained.event_id != event.event_id
        or snapshot.retained.revision_id != event.revision_id
        or snapshot.retained.revision_no != event.revision_no
        or snapshot.security_id != event.terms.security_id
        or _day(snapshot.proposed_ex_date) != proposal.ex_date
        or _day(snapshot.as_of) != proposal.as_of
        or snapshot.rule != proposal.eligibility_rule
    ):
        raise ValueError("Inconsistent hypothetical event")
    estimates = {item.account_id: item for item in proposal.accounts}
    comparisons = {item.account_id: item for item in result.accounts}
    histories = {item.account_id: item for item in snapshot.retained.accounts}
    if (
        len(comparisons) != len(result.accounts)
        or set(comparisons) != set(database)
        or len(histories) != len(snapshot.retained.accounts)
        or not set(histories) <= set(database)
    ):
        raise ValueError("Inconsistent hypothetical accounts")
    items = []
    seen: set[UUID] = set()
    for portfolio in snapshot.portfolios:
        if (
            portfolio.book.owner_id != owner
            or portfolio.book.portfolio_id != portfolio.portfolio.id
            or portfolio.book.security_id != snapshot.security_id
            or portfolio.book.data_revision != portfolio.portfolio.data_revision
            or portfolio.book.ex_date != snapshot.proposed_ex_date
            or portfolio.book.as_of != snapshot.as_of
            or portfolio.book.rule != snapshot.rule
        ):
            raise ValueError("Inconsistent hypothetical portfolio")
        for pin, observation in zip(portfolio.accounts, portfolio.book.accounts, strict=True):
            expected = (
                str(portfolio.portfolio.id),
                str(pin.broker_id),
                pin.account_status,
                pin.broker_status,
                pin.account_version,
                pin.broker_version,
                portfolio.portfolio.version,
                portfolio.portfolio.data_revision,
            )
            if pin.account_id in seen or database.get(pin.account_id) != expected:
                raise ValueError("Inconsistent current account pins")
            calculated = preview._account(
                snapshot,
                portfolio,
                observation,
                histories.get(pin.account_id),
                estimates[pin.account_id],
                proposal,
            )
            if calculated != comparisons[pin.account_id]:
                raise ValueError("Inconsistent retained hypothetical comparison")
            seen.add(pin.account_id)
            items.append(
                _account(
                    portfolio,
                    pin,
                    observation,
                    estimates[pin.account_id],
                    comparisons[pin.account_id],
                )
            )
    if seen != set(database):
        raise ValueError("Missing hypothetical accounts")
    return sorted(items, key=lambda item: str(item["account_id"]))


@router.post("/revision-preview", operation_id="preview_dividend_revision")
def revision_preview(
    event_id: UUID,
    body: models.PreviewRequest,
    request: Request,
    headers: WriteDep,
    version: DividendEventVersionDep,
) -> models.PreviewPage:
    """Recompute every page; the cursor only detects changes to the displayed snapshot."""
    try:
        with unit_of_work(request.app.state.engine) as con:
            auth = read_auth(con, request.cookies.get(COOKIE))
            validate_csrf(auth, headers.csrf)
            if (auth.owner.id, auth.session_id) != (headers.auth.owner.id, headers.auth.session_id):
                raise ApiError(401, "UNAUTHORIZED", "Sign in to continue")
            owner = auth.owner.id
            current = events.get(con, owner, event_id)
            if current is None:
                raise ApiError(404, "NOT_FOUND", "Dividend event not found")
            check_version(current.record.revision_no, version)
            if body.revision_id != current.record.revision_id:
                raise ApiError(412, "PRECONDITION_FAILED", "This event changed; reload it")
            database = _owned_accounts(con, owner)
            if {item.account_id for item in body.proposal.accounts} != set(database):
                raise ApiError(
                    409, "DIVIDEND_PREVIEW_ACCOUNT_SCOPE_CHANGED", "Review every current account"
                )
            result = preview.read(con, owner, event_id, body.proposal)
            if not isinstance(result, preview.RevisionPreview):
                raise ValueError("Missing hypothetical preview")
            rows = _accounts(result, owner, current.record, body.proposal, database)
            common: dict[str, Any] = {
                "event_id": event_id,
                "revision_id": current.record.revision_id,
                "revision_no": current.record.revision_no,
                "security_id": current.record.terms.security_id,
                "terms": body.proposal.model_dump(exclude={"accounts"}),
                "scope": result.scope,
                "history_completeness": result.history_completeness,
                "current_use_scope": result.current_use_scope,
                "eligibility_scope": result.eligibility_scope,
                "payment_scope": result.payment_scope,
            }
            checked: list[models.PreviewAccount] = []
            for offset in range(0, len(rows), 100):
                checked.extend(
                    models.PreviewPage.model_validate(
                        {
                            **common,
                            "items": rows[offset : offset + 100],
                            "snapshot_token": "0" * 64,
                            "next_after_account_id": None,
                        }
                    ).items
                )
            normalized = body.proposal.model_dump(mode="json")
            normalized["accounts"] = sorted(
                normalized["accounts"], key=lambda row: row["account_id"]
            )
            tag = sensitive_fingerprint(
                request,
                {
                    "domain": "DIVIDEND_REVISION_PREVIEW_SNAPSHOT_V1",
                    "owner": str(owner),
                    "event": str(event_id),
                    "revision_id": str(current.record.revision_id),
                    "revision_no": current.record.revision_no,
                    "security_id": str(current.record.terms.security_id),
                    "proposal": normalized,
                    "items": [item.model_dump(mode="json") for item in checked],
                },
            )
            if body.snapshot_token is not None and not hmac.compare_digest(
                body.snapshot_token, tag
            ):
                raise ApiError(
                    409, "DIVIDEND_PREVIEW_SNAPSHOT_CHANGED", "The preview changed; start again"
                )
            start = 0
            if body.after_account_id is not None:
                ids = [item.account_id for item in checked]
                if body.after_account_id not in ids:
                    raise ApiError(422, "INVALID_CURSOR", "The preview cursor is invalid")
                start = ids.index(body.after_account_id) + 1
            selected = checked[start : start + body.limit]
            next_id = selected[-1].account_id if start + body.limit < len(checked) else None
            return models.PreviewPage.model_validate(
                {
                    **common,
                    "items": selected,
                    "snapshot_token": tag,
                    "next_after_account_id": next_id,
                }
            )
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise _unavailable() from None
