"""Atomic unverified corrections for authenticated callers; no HTTP authority is supplied here."""

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, RowMapping, text
from sqlalchemy.exc import SQLAlchemyError

from spm_api import trade_audit, trade_correction_authorization, trade_history_review, trade_service
from spm_api.audit import audit, canonical, digest
from spm_api.db import savepoint
from spm_api.decimal_types import INT64_MAX, to_scaled
from spm_api.errors import ApiError
from spm_api.mutations import check_version
from spm_api.security import Auth
from spm_api.trade_models import TradeRecord, TradeRevision


class UnverifiedCorrectionError(ValueError):
    """Fixed codes only; do not expose financial facts or private reasons."""


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    authorization: trade_correction_authorization.CorrectionAuthorization = field(repr=False)
    trade: TradeRecord = field(repr=False)
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class _Intent:
    trade_id: UUID = field(repr=False)
    version: int
    operation: Literal["REVISE", "VOID"]
    change: TradeRevision | str = field(repr=False)
    pins: tuple[trade_history_review.CorrectionReviewPin, ...] = field(repr=False)
    fingerprint: str = field(repr=False)
    reason: str = field(repr=False)


def _intent(
    identifier: UUID,
    version: int,
    operation: Literal["REVISE", "VOID"],
    change: TradeRevision | str,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
) -> _Intent:
    if (
        not isinstance(identifier, UUID)
        or type(version) is not int
        or not 1 <= version <= INT64_MAX
        or operation not in ("REVISE", "VOID")
        or (operation == "REVISE" and not isinstance(change, TradeRevision))
        or (operation == "VOID" and not isinstance(change, str))
    ):
        raise UnverifiedCorrectionError("INVALID_TRADE_CORRECTION_SCOPE")
    if isinstance(change, TradeRevision):
        try:
            change = TradeRevision.model_validate(change.model_dump(mode="python"))
        except (ValidationError, PydanticSerializationError):
            raise UnverifiedCorrectionError("INVALID_TRADE_CORRECTION_SCOPE") from None
    reason = change.reason if isinstance(change, TradeRevision) else change
    if not trade_audit._reason(reason):
        raise UnverifiedCorrectionError("INVALID_TRADE_CORRECTION_SCOPE")
    supplied = trade_history_review._checked_pins(pins)
    ordered = tuple(sorted(supplied.values(), key=lambda item: str(item.trade_id)))
    scope = [
        {
            "trade_id": str(pin.trade_id),
            "trade_version": pin.trade_version,
            "review_revision": str(pin.review_revision),
        }
        for pin in ordered
    ]
    body = (
        change.model_dump(mode="json") if isinstance(change, TradeRevision) else {"reason": reason}
    )
    fingerprint = digest(
        canonical(
            {
                "trade_id": str(identifier),
                "trade_version": version,
                "operation": operation,
                "change": body,
                "reviews": scope,
                "scope": "UNVERIFIED_TRADE_CORRECTION_ONLY",
            }
        )
    )
    return _Intent(identifier, version, operation, change, ordered, fingerprint, reason)


def _matches_revision(body: TradeRevision, result: TradeRecord) -> bool:
    if result.status not in ("ACTIVE", "DRAFT_SCHEDULED"):
        return False
    try:
        expected = trade_service._prepared_values(
            body, scheduled=result.status == "DRAFT_SCHEDULED"
        )
        actual = {
            "portfolio_id": str(result.portfolio_id),
            "account_id": str(result.account_id),
            "security_id": str(result.security_id),
            "side": result.side,
            "trade_date": result.trade_date,
            "executed_at_utc": result.executed_at_utc,
            "quantity_e8": to_scaled(result.quantity, 8),
            "price_e8": to_scaled(result.price, 8),
            "fee_micro": to_scaled(result.fee, 6),
            "tax_micro": to_scaled(result.tax, 6),
            "currency": result.currency,
            "fx_rate_hkd_e12": to_scaled(result.fx_rate_hkd, 12)
            if result.fx_rate_hkd is not None
            else None,
            "fx_source": result.fx_source,
            "gross_micro": to_scaled(result.gross, 6),
            "settlement_micro": to_scaled(result.settlement, 6),
            "booked_hkd_micro": to_scaled(result.booked_hkd, 6)
            if result.booked_hkd is not None
            else None,
            "rounding_delta_micro": to_scaled(result.rounding_delta, 6),
            "settlement_override": int(result.settlement_override),
            "hkd_override": int(result.hkd_override),
            "override_reason": result.override_reason,
            "note": result.note,
            "status": result.status,
        }
        return actual == expected
    except (ValueError, TypeError, OverflowError):
        return False


def _historical_result(
    con: Connection, auth: Auth, record: trade_correction_authorization.CorrectionAuthorization
) -> CorrectionResult:
    row = trade_service.owned_trade(con, str(auth.owner.id), str(record.result_trade_id))
    result = trade_service.as_trade(row)
    if record.operation == "REVISE":
        origin = trade_audit.get_origin(con, auth.owner.id, record.result_trade_id)
        if origin is None:
            raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE")
        result = result.model_copy(
            update={
                "status": origin.original_status,
                "version": 1,
                "updated_at": origin.original_updated_at,
            }
        )
    return CorrectionResult(record, result)


def _match(
    con: Connection,
    auth: Auth,
    record: trade_correction_authorization.CorrectionAuthorization,
    intent: _Intent,
) -> CorrectionResult:
    recorded = {(pin.trade_id, pin.trade_version, pin.review_revision) for pin in record.pins}
    requested = {(pin.trade_id, pin.trade_version, pin.review_revision) for pin in intent.pins}
    if (
        record.trade_id != intent.trade_id
        or record.trade_version != intent.version
        or record.operation != intent.operation
        or recorded != requested
        or record.intent_sha256 != intent.fingerprint
        or record.reason != intent.reason
    ):
        raise UnverifiedCorrectionError("TRADE_CORRECTION_CONFLICT")
    result = _historical_result(con, auth, record)
    if isinstance(intent.change, TradeRevision) and not _matches_revision(
        intent.change, result.trade
    ):
        raise UnverifiedCorrectionError("TRADE_CORRECTION_CONFLICT")
    return result


def _existing(
    con: Connection, auth: Auth, request_id: UUID, intent: _Intent
) -> CorrectionResult | None:
    stored = con.execute(
        text(
            "SELECT id FROM trade_correction_authorizations "
            "WHERE owner_id=:owner AND request_id=:request"
        ),
        {"owner": str(auth.owner.id), "request": str(request_id)},
    ).scalar_one_or_none()
    if stored is None:
        return None
    try:
        stored_id = trade_audit._identity(stored)
    except ValueError:
        raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE") from None
    record = trade_correction_authorization.get(con, auth.owner.id, stored_id)
    if record is None:
        raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE")
    return _match(con, auth, record, intent)


def replay(
    con: Connection,
    auth: Auth,
    authorization_id: UUID,
    identifier: UUID,
    *,
    version: int,
    operation: Literal["REVISE", "VOID"],
    change: TradeRevision | str,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
) -> CorrectionResult:
    """Recheck a historical intent without writing; caller must reauthenticate before replay."""
    trade_history_review._writer(con)
    if not isinstance(auth, Auth) or not isinstance(authorization_id, UUID):
        raise UnverifiedCorrectionError("INVALID_TRADE_CORRECTION_SCOPE")
    intent = _intent(identifier, version, operation, change, pins)
    try:
        record = trade_correction_authorization.get(con, auth.owner.id, authorization_id)
        if record is None:
            raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE")
        return _match(con, auth, record, intent)
    except SQLAlchemyError:
        raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE") from None


def _record(
    con: Connection,
    auth: Auth,
    before: RowMapping,
    result: TradeRecord,
    operation: Literal["REVISE", "VOID"],
    reason: str,
    request_id: UUID,
    fingerprint: str,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
    reviews: tuple[trade_history_review.Review, ...],
) -> CorrectionResult:
    supplied = trade_history_review._checked_pins(pins)
    scope = [
        {
            "trade_id": str(review.trade_id),
            "trade_version": supplied[review.trade_id].trade_version,
            "review_id": str(review.id),
            "review_revision": str(review.revision),
            "gap": review.gap,
        }
        for review in reviews
    ]
    identifier = uuid4()
    values: dict[str, Any] = {
        "id": str(identifier),
        "owner_id": str(auth.owner.id),
        "root_id": before["root_id"],
        "trade_id": before["id"],
        "trade_version": before["version"],
        "operation": operation,
        "result_trade_id": str(result.id),
        "scope_json": canonical(scope),
        "intent_sha256": fingerprint,
        "request_id": str(request_id),
    }
    audit(
        con,
        str(auth.owner.id),
        "trade_correction_authorization",
        str(identifier),
        "TRADE_CORRECTION_UNVERIFIED",
        request_id=str(request_id),
        after=trade_correction_authorization._payload(values, scope, reason),
        reason=reason,
    )
    event = con.execute(
        text("SELECT id,created_at FROM audit_events WHERE entity_type=:type AND entity_id=:id"),
        {"type": "trade_correction_authorization", "id": str(identifier)},
    ).one()
    values.update(audit_id=event.id, created_at=event.created_at)
    columns = ",".join(values)
    parameters = ",".join(":" + key for key in values)
    con.execute(
        text(f"INSERT INTO trade_correction_authorizations({columns}) VALUES ({parameters})"),
        values,
    )
    record = trade_correction_authorization.get(con, auth.owner.id, identifier)
    if record is None:
        raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE")
    return CorrectionResult(record, result)


def _change(
    con: Connection,
    auth: Auth,
    identifier: UUID,
    *,
    version: int,
    operation: Literal["REVISE", "VOID"],
    change: TradeRevision | str,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
    request_id: UUID,
) -> CorrectionResult:
    trade_history_review._writer(con)
    if not isinstance(auth, Auth) or not isinstance(request_id, UUID):
        raise UnverifiedCorrectionError("INVALID_TRADE_CORRECTION_SCOPE")
    intent = _intent(identifier, version, operation, change, pins)
    try:
        with savepoint(con):
            existing = _existing(con, auth, request_id, intent)
            if existing is not None:
                return existing
            before = trade_service.owned_trade(con, str(auth.owner.id), str(identifier))
            check_version(before["version"], version)
            if before["status"] in ("SUPERSEDED", "VOID"):
                raise ApiError(409, "INACTIVE_FACT", "Use the current trade revision")
            reviews = trade_history_review.require_correction_reviews(
                con, auth.owner.id, identifier, intent.pins
            )
            if all(item.trade_id != identifier for item in reviews):
                try:
                    trade_audit.capture_before_retire(con, auth.owner.id, identifier, request_id)
                except ValueError:
                    raise ApiError(
                        503, "TRADE_AUDIT_UNAVAILABLE", "Trade audit history is unavailable"
                    ) from None
            if operation == "REVISE":
                if not isinstance(intent.change, TradeRevision):
                    raise UnverifiedCorrectionError("INVALID_TRADE_CORRECTION_SCOPE")
                values = trade_service._prepare(con, auth, intent.change)
                result = trade_service._revise_prepared(
                    con, auth, before, values, intent.reason, str(request_id)
                )
                if not _matches_revision(intent.change, result):
                    raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE")
            else:
                trade_service._void_prepared(con, auth, before, intent.reason, str(request_id))
                result = trade_service.as_trade(
                    trade_service.owned_trade(con, str(auth.owner.id), str(identifier))
                )
            return _record(
                con,
                auth,
                before,
                result,
                operation,
                intent.reason,
                request_id,
                intent.fingerprint,
                intent.pins,
                reviews,
            )
    except SQLAlchemyError:
        raise UnverifiedCorrectionError("TRADE_CORRECTION_UNAVAILABLE") from None


def revise(
    con: Connection,
    auth: Auth,
    identifier: UUID,
    *,
    version: int,
    body: TradeRevision,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
    request_id: UUID,
) -> CorrectionResult:
    """Revise with explicit scope; caller must authenticate and commit idempotency atomically."""
    return _change(
        con,
        auth,
        identifier,
        version=version,
        operation="REVISE",
        change=body,
        pins=pins,
        request_id=request_id,
    )


def void(
    con: Connection,
    auth: Auth,
    identifier: UUID,
    *,
    version: int,
    reason: str,
    pins: tuple[trade_history_review.CorrectionReviewPin, ...],
    request_id: UUID,
) -> CorrectionResult:
    """Void with explicit scope; caller must authenticate and commit idempotency atomically."""
    return _change(
        con,
        auth,
        identifier,
        version=version,
        operation="VOID",
        change=reason,
        pins=pins,
        request_id=request_id,
    )
