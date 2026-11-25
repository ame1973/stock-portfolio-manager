"""Build audited projections from current recorded facts, without posting income."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as fee_writer
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit, canonical, digest, now, stamp
from spm_api.db import savepoint
from spm_api.decimal_types import INT64_MAX, FxRate, IsoDate, to_scaled
from spm_api.dividend_account_scope import account
from spm_api.dividend_math import accrue
from spm_api.dividend_projection_arithmetic import _AMOUNT_FIELDS
from spm_api.dividend_projection_history import RetainedProjection, _payload
from spm_api.dividend_projection_history import history as projection_history
from spm_api.errors import Model

Number = Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
HeadNumber = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
NextNumber = Annotated[int, Field(strict=True, ge=0, lt=INT64_MAX)]
Source = Annotated[str, Field(strict=True, min_length=1, max_length=100, pattern=r"^[^\x00]+$")]


class ProjectionWriteError(ValueError):
    """Fixed codes only; do not disclose recorded holdings or private intent."""


class ProjectionIntent(Model):
    """Explicitly observed inputs; callers must authenticate before invoking the writer."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(repr=False)
    event_id: UUID = Field(repr=False)
    revision_id: UUID = Field(repr=False)
    revision_no: events.Version
    account_id: UUID = Field(repr=False)
    portfolio_id: UUID = Field(repr=False)
    broker_id: UUID = Field(repr=False)
    account_version: Number
    broker_version: Number
    expected_data_revision: HeadNumber
    expected_projection_no: NextNumber
    expected_projection_id: UUID | None = Field(repr=False)
    as_of: IsoDate
    eligibility_rule: Literal["ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"]
    tax_effective_on: IsoDate
    expected_tax_head_no: HeadNumber
    expected_tax_head_id: UUID | None = Field(repr=False)
    expected_tax_selected_id: UUID | None = Field(repr=False)
    fee_effective_on: IsoDate
    expected_fee_head_no: HeadNumber
    expected_fee_head_id: UUID | None = Field(repr=False)
    expected_fee_selected_id: UUID | None = Field(repr=False)
    fx_rate_hkd: FxRate | None = Field(repr=False)
    fx_source: Source | None = Field(repr=False)
    fee_rate_event: FxRate | None = Field(repr=False)
    fee_rate_source: Source | None = Field(repr=False)
    reason: events.Reason = Field(repr=False)

    @field_validator("fx_source", "fee_rate_source")
    @classmethod
    def canonical_source(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("A source must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def complete_pins(self) -> Self:
        for number, head, selected in (
            (self.expected_projection_no, self.expected_projection_id, None),
            (self.expected_tax_head_no, self.expected_tax_head_id, self.expected_tax_selected_id),
            (self.expected_fee_head_no, self.expected_fee_head_id, self.expected_fee_selected_id),
        ):
            if (number == 0) != (head is None) or (number == 0 and selected is not None):
                raise ValueError("A head number must match its observed identities")
        if (self.fx_rate_hkd is None) != (self.fx_source is None) or (
            self.fee_rate_event is None
        ) != (self.fee_rate_source is None):
            raise ValueError("An exchange rate and its source must be supplied together")
        return self


def _writer(con: Connection, owner: UUID) -> None:
    if not con.in_transaction() or con.get_execution_options().get("writer") is not True:
        raise ProjectionWriteError("WRITE_UNIT_OF_WORK_REQUIRED")
    if not isinstance(owner, UUID):
        raise ProjectionWriteError("INVALID_DIVIDEND_PROJECTION_SCOPE")
    if (
        con.execute(
            text("SELECT 1 FROM owners WHERE id=:id AND is_active=1"), {"id": str(owner)}
        ).first()
        is None
    ):
        raise ProjectionWriteError("DIVIDEND_PROJECTION_OWNER_UNAVAILABLE")


def _intent_payload(intent: ProjectionIntent, request_id: UUID) -> dict[str, str]:
    return {
        "projection_id": str(intent.id),
        "event_id": str(intent.event_id),
        "account_id": str(intent.account_id),
        "request_id": str(request_id),
        "intent_sha256": digest(canonical(intent.model_dump(mode="json", warnings="error"))),
        "scope": "UNVERIFIED_PROJECTION_WRITE_INTENT_ONLY",
    }


def _verify_intent(
    con: Connection,
    owner: UUID,
    intent: ProjectionIntent,
    request_id: UUID,
    record: RetainedProjection,
) -> None:
    rows = (
        con.execute(
            text(
                "SELECT * FROM audit_events WHERE entity_type='dividend_projection_intent' "
                "AND entity_id=:id"
            ),
            {"id": str(intent.id)},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
    row = rows[0]
    try:
        events._uuid(row["id"])
        version, tax_provenance, fee_provenance = (
            record.version,
            record.tax_provenance,
            record.fee_provenance,
        )
        if (
            (version.id, version.event_id, version.revision_id, version.revision_no)
            != (intent.id, intent.event_id, intent.revision_id, intent.revision_no)
            or (version.account_id, version.projection_no, version.previous_id)
            != (intent.account_id, intent.expected_projection_no + 1, intent.expected_projection_id)
            or (record.owner_id, record.portfolio_id, record.broker_id)
            != (owner, intent.portfolio_id, intent.broker_id)
            or (record.account_version, record.broker_version, record.portfolio_data_revision)
            != (intent.account_version, intent.broker_version, intent.expected_data_revision)
            or record.as_of != date.fromisoformat(intent.as_of)
            or record.eligibility_rule != intent.eligibility_rule
            or record.reason != intent.reason
            or record.request_id != request_id
            or tax_provenance is None
            or fee_provenance is None
            or (
                tax_provenance.effective_on,
                tax_provenance.observed_head_no,
                tax_provenance.observed_head_id,
                tax_provenance.selected_setting_id,
            )
            != (
                date.fromisoformat(intent.tax_effective_on),
                intent.expected_tax_head_no,
                intent.expected_tax_head_id,
                intent.expected_tax_selected_id,
            )
            or (
                fee_provenance.effective_on,
                fee_provenance.observed_head_no,
                fee_provenance.observed_head_id,
                fee_provenance.selected_setting_id,
            )
            != (
                date.fromisoformat(intent.fee_effective_on),
                intent.expected_fee_head_no,
                intent.expected_fee_head_id,
                intent.expected_fee_selected_id,
            )
            or row["owner_id"] != str(owner)
            or row["action"] != "DIVIDEND_PROJECTION_INTENT_RECORDED"
            or row["request_id"] != str(request_id)
            or row["reason"] != intent.reason
            or row["before_sha256"] is not None
            or row["after_sha256"] != digest(canonical(_intent_payload(intent, request_id)))
            or events._time(row["created_at"]) != record.created_at
        ):
            raise ValueError
        if version.state == "EXPECTED":
            if version.accrual is None or record.fee_currency is None:
                raise ValueError
            fx = _fx(
                intent.fx_rate_hkd, intent.fx_source, identity=version.accrual.currency == "HKD"
            )
            fee_fx = _fx(
                intent.fee_rate_event,
                intent.fee_rate_source,
                identity=record.fee_currency == version.accrual.currency,
            )
            if (record.fx_rate_hkd, record.fx_source) != fx or (
                record.fee_rate_event,
                record.fee_rate_source,
            ) != fee_fx:
                raise ValueError
    except (ValueError, TypeError, KeyError, PydanticSerializationError):
        raise ProjectionWriteError("DIVIDEND_PROJECTION_CONFLICT") from None


def _current_inputs(
    con: Connection, owner: UUID, intent: ProjectionIntent
) -> tuple[
    events.ReviewedEvent, eligibility.RecordedEligibilityBook, tax.TaxSelection, fees.FeeSelection
]:
    try:
        event = event_writer.require_current_source(
            con, owner, intent.event_id, revision_id=intent.revision_id, version=intent.revision_no
        )
        rejections.require_clear(
            con, owner, intent.event_id, revision_id=intent.revision_id, version=intent.revision_no
        )
        current = account(con, owner, intent.account_id)
        if current is None:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_NOT_FOUND")
        if (
            current["portfolio_id"] != str(intent.portfolio_id)
            or current["broker_id"] != str(intent.broker_id)
            or current["account_version"] != intent.account_version
            or current["broker_version"] != intent.broker_version
        ):
            raise ProjectionWriteError("DIVIDEND_PROJECTION_SCOPE_CHANGED")
        ex_date = event.record.terms.ex_date
        if ex_date is None:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        book = eligibility.get(
            con,
            owner,
            intent.portfolio_id,
            event.record.terms.security_id,
            ex_date=date.fromisoformat(ex_date),
            as_of=date.fromisoformat(intent.as_of),
            rule=intent.eligibility_rule,
        )
        if book is None:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_NOT_FOUND")
        if book.data_revision != intent.expected_data_revision:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_INPUT_CHANGED")
        selected_tax = tax.require_current(
            con,
            owner,
            intent.event_id,
            intent.revision_id,
            intent.revision_no,
            intent.account_id,
            effective_on=date.fromisoformat(intent.tax_effective_on),
            expected_setting_no=intent.expected_tax_head_no,
            expected_head_id=intent.expected_tax_head_id,
        )
        selected_fee = fee_writer.require_current(
            con,
            owner,
            intent.event_id,
            intent.revision_id,
            intent.revision_no,
            intent.account_id,
            effective_on=date.fromisoformat(intent.fee_effective_on),
            expected_setting_no=intent.expected_fee_head_no,
            expected_head_id=intent.expected_fee_head_id,
            expected_portfolio_id=intent.portfolio_id,
            expected_broker_id=intent.broker_id,
            expected_account_version=intent.account_version,
            expected_broker_version=intent.broker_version,
        )
        if (
            selected_tax.selected.id if selected_tax.selected else None
        ) != intent.expected_tax_selected_id or (
            selected_fee.selected.id if selected_fee.selected else None
        ) != intent.expected_fee_selected_id:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_INPUT_CHANGED")
        return event, book, selected_tax, selected_fee
    except ProjectionWriteError:
        raise
    except (
        events.EventError,
        rejections.RejectionError,
        tax.TaxSettingError,
        fees.FeeSettingError,
    ) as error:
        code = str(error)
        if code in {
            "DIVIDEND_EVENT_NOT_FOUND",
            "DIVIDEND_EVENT_VERSION_CONFLICT",
            "DIVIDEND_EVENT_SOURCE_CHANGED",
            "DIVIDEND_REVISION_REJECTED",
            "DIVIDEND_REJECTION_VERSION_CONFLICT",
            "CANDIDATE_NOT_FOUND",
            "CANDIDATE_VERSION_CONFLICT",
            "CANDIDATE_DISMISSED",
            "CANDIDATE_SOURCE_UNAVAILABLE",
            "CANDIDATE_SOURCE_CHANGED",
            "CANDIDATE_BASIS_UNAVAILABLE",
            "CANDIDATE_BASIS_CHANGED",
            "DIVIDEND_TAX_SETTING_CHANGED",
            "DIVIDEND_TAX_SCOPE_CHANGED",
            "DIVIDEND_FEE_SETTING_CHANGED",
            "DIVIDEND_FEE_SCOPE_CHANGED",
        }:
            raise ProjectionWriteError(code) from None
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None
    except eligibility.EligibilityHistoryError:
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None


def _fx(
    rate: str | None, source: str | None, *, identity: bool
) -> tuple[Decimal | None, str | None]:
    if identity:
        if rate is not None and (Decimal(rate) != 1 or source != "IDENTITY"):
            raise ProjectionWriteError("INVALID_DIVIDEND_PROJECTION_FX")
        return Decimal(1), "IDENTITY"
    if source == "IDENTITY":
        raise ProjectionWriteError("INVALID_DIVIDEND_PROJECTION_FX")
    return (Decimal(rate) if rate is not None else None), source


def _scaled(value: Decimal, scale: int) -> int:
    return to_scaled(format(value, "f"), scale)


def _values(
    intent: ProjectionIntent,
    event: events.ReviewedEvent,
    book: eligibility.RecordedEligibilityBook,
    selected_tax: tax.TaxSelection,
    selected_fee: fees.FeeSelection,
) -> dict[str, Any]:
    accounts = [item for item in book.accounts if item.account_id == str(intent.account_id)]
    if len(accounts) != 1:
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
    observed = accounts[0]
    terms = event.record.terms
    if terms.currency is None or terms.dps is None:
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
    fx, fx_source = _fx(intent.fx_rate_hkd, intent.fx_source, identity=terms.currency == "HKD")
    fee = selected_fee.selected
    fee_currency = fee.fee_currency if fee else None
    if fee_currency is None:
        if intent.fee_rate_event is not None:
            raise ProjectionWriteError("INVALID_DIVIDEND_PROJECTION_FX")
        fee_fx, fee_source = None, None
    else:
        fee_fx, fee_source = _fx(
            intent.fee_rate_event, intent.fee_rate_source, identity=fee_currency == terms.currency
        )
    state = "NEEDS_REVIEW"
    values: dict[str, Any] = {name: None for name in _AMOUNT_FIELDS}
    if event.record.review_status == "WITHDRAWN":
        state = "CANCELLED"
    elif (
        event.record.review_status in ("PROVISIONAL", "CORROBORATED", "VERIFIED")
        and terms.adjustment_basis == "AS_DECLARED"
        and observed.state != "NEEDS_REVIEW"
        and observed.recorded_quantity is not None
        and book.trade_history_audit_scope != "RETIRED_TRADE_HISTORY_UNVERIFIED"
        and book.trade_creation_audit_scope != "CREATION_GAP_REVIEWED_UNVERIFIED"
    ):
        quantity = Decimal(observed.recorded_quantity)
        if quantity == 0:
            state = "NO_POSITION"
        elif (
            selected_tax.status == "ESTIMATED"
            and selected_tax.selected is not None
            and selected_tax.selected.rate is not None
            and selected_fee.status == "ESTIMATED"
            and fee is not None
            and fee.fee is not None
            and fee_currency is not None
            and (fee_currency == terms.currency or Decimal(fee.fee) == 0 or fee_fx is not None)
        ):
            result = accrue(
                str(intent.event_id),
                str(intent.account_id),
                terms.currency,
                quantity,
                Decimal(terms.dps),
                Decimal(selected_tax.selected.rate),
                Decimal(fee.fee),
                fx,
                fee_currency=fee_currency,
                fee_rate_event=fee_fx,
            )
            state = "EXPECTED"
            values.update(
                eligible_shares_e8=_scaled(quantity, 8),
                gross_micro=_scaled(result.gross_local, 6),
                tax_micro=_scaled(result.tax_local, 6),
                fee_micro=_scaled(result.fee_local, 6),
                expected_net_micro=_scaled(result.net_local, 6),
                rounding_delta_micro=_scaled(result.rounding_delta_local, 6),
                fx_rate_hkd_e12=_scaled(fx, 12) if fx is not None else None,
                fx_source=fx_source,
                expected_hkd_micro=_scaled(result.booked_hkd, 6)
                if result.booked_hkd is not None
                else None,
                tax_rate_snapshot_ppm=to_scaled(selected_tax.selected.rate, 6),
                fee_original_micro=to_scaled(fee.fee, 6),
                fee_original_currency=fee_currency,
                fee_rate_event_e12=_scaled(fee_fx, 12) if fee_fx is not None else None,
                fee_rate_source=fee_source,
                quality=result.quality,
            )
    values.update(
        state=state,
        currency=terms.currency,
        tax_provenance_version=1,
        tax_effective_on=intent.tax_effective_on,
        tax_observed_head_no=selected_tax.head_no,
        tax_observed_head_id=str(selected_tax.head_id) if selected_tax.head_id else None,
        tax_selected_setting_id=str(selected_tax.selected.id) if selected_tax.selected else None,
        tax_estimate_status=selected_tax.status,
        fee_provenance_version=1,
        fee_effective_on=intent.fee_effective_on,
        fee_observed_head_no=selected_fee.head_no,
        fee_observed_head_id=str(selected_fee.head_id) if selected_fee.head_id else None,
        fee_selected_setting_id=str(selected_fee.selected.id) if selected_fee.selected else None,
        fee_estimate_status=selected_fee.status,
    )
    return values


def _check_time(
    con: Connection,
    intent: ProjectionIntent,
    event: events.ReviewedEvent,
    book: eligibility.RecordedEligibilityBook,
    previous: RetainedProjection | None,
    at: datetime,
) -> None:
    times = [event.record.created_at]
    if book.latest_input_at is not None:
        times.append(book.latest_input_at)
    scope = con.execute(
        text(
            "SELECT p.created_at AS portfolio_at,a.created_at AS account_at FROM portfolios p "
            "JOIN accounts a ON a.portfolio_id=p.id WHERE p.id=:portfolio AND a.id=:account"
        ),
        {"portfolio": str(intent.portfolio_id), "account": str(intent.account_id)},
    ).first()
    if scope is None:
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
    times.extend(
        (eligibility._timestamp(scope.portfolio_at), eligibility._timestamp(scope.account_at))
    )
    # ponytail: scan invalidations; index or checkpoint if histories become large.
    invalidations = con.execute(
        text("SELECT created_at FROM projection_invalidations WHERE portfolio_id=:portfolio"),
        {"portfolio": str(intent.portfolio_id)},
    )
    times.extend(eligibility._timestamp(row.created_at) for row in invalidations)
    if previous is not None:
        times.append(previous.created_at)
    source = con.execute(
        text(
            "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision' "
            "AND entity_id=:id AND action='DIVIDEND_EVENT_REVISED'"
        ),
        {"id": str(intent.revision_id)},
    ).all()
    if len(source) != 1:
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
    times.append(events._time(source[0].created_at))
    # Check the event's complete verified source and resolution histories, not only its head.
    source_history = con.execute(
        text("""
            WITH candidates AS (
                SELECT c.* FROM dividend_candidate_revisions c
                WHERE c.candidate_id IN (
                    SELECT linked.candidate_id FROM dividend_revision_decisions d
                    JOIN dividend_candidate_revisions linked ON linked.id=d.candidate_revision_id
                    WHERE d.event_id=:event
                )
            ), matches AS (
                SELECT json_extract(j.value,'$.observation_id') AS observation_id,
                       json_extract(j.value,'$.basis_assertion_id') AS basis_id
                FROM candidates c JOIN json_each(c.matches_json) j
            )
            SELECT a.created_at AS happened_at FROM audit_events a
            JOIN dividend_event_revisions r ON r.id=a.entity_id
            WHERE r.event_id=:event AND a.entity_type='dividend_event_revision'
            UNION ALL SELECT c.created_at FROM candidates c
            UNION ALL SELECT a.created_at FROM candidates c
            JOIN audit_events a ON a.entity_id=c.id
            WHERE a.entity_type='dividend_candidate_revision'
              AND a.action='DIVIDEND_CANDIDATE_REVISED'
            UNION ALL SELECT o.retrieved_at FROM matches m
            JOIN dividend_observations o ON o.id=m.observation_id
            UNION ALL SELECT b.created_at FROM matches m
            JOIN dividend_basis_assertions b ON b.id=m.basis_id
            UNION ALL SELECT a.created_at FROM matches m
            JOIN dividend_basis_assertions b ON b.id=m.basis_id
            JOIN audit_events a ON a.entity_id=b.id
            WHERE a.entity_type='dividend_basis_assertion' AND a.action='DIVIDEND_BASIS_ASSERTED'
            UNION ALL SELECT r.created_at FROM dividend_event_rejections r WHERE r.event_id=:event
            UNION ALL SELECT a.created_at FROM dividend_event_rejections r
            JOIN audit_events a ON a.id=r.audit_id WHERE r.event_id=:event
            UNION ALL SELECT r.created_at FROM dividend_rejection_resolutions r
            WHERE r.event_id=:event
            UNION ALL SELECT a.created_at FROM dividend_rejection_resolutions r
            JOIN audit_events a ON a.id=r.audit_id WHERE r.event_id=:event
        """),
        {"event": str(intent.event_id)},
    )
    times.extend(eligibility._timestamp(row.happened_at) for row in source_history)
    for table, number in (
        ("dividend_tax_settings", intent.expected_tax_head_no),
        ("dividend_fee_settings", intent.expected_fee_head_no),
    ):
        rows = con.execute(
            text(
                f"SELECT s.created_at,a.created_at AS audit_at FROM {table} s "
                "JOIN audit_events a ON a.id=s.audit_id "
                "WHERE s.event_id=:event AND s.revision_id=:revision AND s.account_id=:account "
                "AND s.setting_no<=:number"
            ),
            {
                "event": str(intent.event_id),
                "revision": str(intent.revision_id),
                "account": str(intent.account_id),
                "number": number,
            },
        ).all()
        if len(rows) != number:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        for row in rows:
            times.extend((events._time(row.created_at), events._time(row.audit_at)))
    if any(at < value for value in times):
        raise ProjectionWriteError("DIVIDEND_PROJECTION_TIME_CONFLICT")


def save(
    con: Connection, owner: UUID, intent: ProjectionIntent, *, request_id: UUID
) -> RetainedProjection:
    """Append one recorded-history projection atomically; this is not a financial grant."""
    try:
        return _save(con, owner, intent, request_id=request_id)
    except ProjectionWriteError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None


def _save(
    con: Connection, owner: UUID, intent: ProjectionIntent, *, request_id: UUID
) -> RetainedProjection:
    _writer(con, owner)
    try:
        if not isinstance(intent, ProjectionIntent) or not isinstance(request_id, UUID):
            raise ValueError
        validated = ProjectionIntent.model_validate(intent.model_dump(warnings="error"))
        if validated != intent:
            raise ValueError
        intent = validated
    except (ValueError, TypeError, OverflowError, PydanticSerializationError):
        raise ProjectionWriteError("INVALID_DIVIDEND_PROJECTION_SCOPE") from None
    with savepoint(con):
        retained = projection_history(con, owner, intent.event_id, intent.account_id)
        if retained is None:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_NOT_FOUND")
        for record in retained:
            if record.version.id == intent.id:
                if record.request_id != request_id:
                    raise ProjectionWriteError("DIVIDEND_PROJECTION_CONFLICT")
                _verify_intent(con, owner, intent, request_id, record)
                return record
        if any(record.request_id == request_id for record in retained):
            raise ProjectionWriteError("DIVIDEND_PROJECTION_CONFLICT")
        if con.execute(
            text("SELECT 1 FROM dividend_projection_versions WHERE id=:id"), {"id": str(intent.id)}
        ).first():
            raise ProjectionWriteError("DIVIDEND_PROJECTION_CONFLICT")
        if con.execute(
            text(
                "SELECT 1 FROM audit_events WHERE entity_type IN "
                "('dividend_projection','dividend_projection_intent') AND entity_id=:id LIMIT 1"
            ),
            {"id": str(intent.id)},
        ).first():
            raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        previous = retained[-1] if retained else None
        if (len(retained), previous.version.id if previous else None) != (
            intent.expected_projection_no,
            intent.expected_projection_id,
        ):
            raise ProjectionWriteError("DIVIDEND_PROJECTION_VERSION_CONFLICT")
        event, book, selected_tax, selected_fee = _current_inputs(con, owner, intent)
        at = now()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        at = at.astimezone(UTC)
        _check_time(con, intent, event, book, previous, at)
        values: dict[str, Any] = {
            "id": str(intent.id),
            "owner_id": str(owner),
            "event_id": str(intent.event_id),
            "revision_id": str(intent.revision_id),
            "portfolio_id": str(intent.portfolio_id),
            "account_id": str(intent.account_id),
            "broker_id": str(intent.broker_id),
            "account_version": intent.account_version,
            "broker_version": intent.broker_version,
            "projection_no": intent.expected_projection_no + 1,
            "previous_id": str(previous.version.id) if previous else None,
            "as_of": intent.as_of,
            "eligibility_rule": intent.eligibility_rule,
            "portfolio_data_revision": book.data_revision,
            "retired_trade_scope": book.trade_history_audit_scope,
            "creation_audit_scope": book.trade_creation_audit_scope,
            "history_completeness": "UNVERIFIED",
            "request_id": str(request_id),
            "created_at": stamp(at),
            **_values(intent, event, book, selected_tax, selected_fee),
        }
        audit(
            con,
            str(owner),
            "dividend_projection",
            str(intent.id),
            "DIVIDEND_PROJECTION_CREATED",
            request_id=str(request_id),
            after=_payload(values, intent.revision_no, intent.reason),
            reason=intent.reason,
            at=at,
        )
        audit_id = con.execute(
            text(
                "SELECT id FROM audit_events WHERE entity_type='dividend_projection' "
                "AND entity_id=:id"
            ),
            {"id": str(intent.id)},
        ).scalar_one()
        values["audit_id"] = audit_id
        columns = ",".join(values)
        parameters = ",".join(":" + key for key in values)
        con.execute(
            text(f"INSERT INTO dividend_projection_versions({columns}) VALUES ({parameters})"),
            values,
        )
        audit(
            con,
            str(owner),
            "dividend_projection_intent",
            str(intent.id),
            "DIVIDEND_PROJECTION_INTENT_RECORDED",
            request_id=str(request_id),
            after=_intent_payload(intent, request_id),
            reason=intent.reason,
            at=at,
        )
        updated = projection_history(con, owner, intent.event_id, intent.account_id)
        if (
            updated is None
            or len(updated) != len(retained) + 1
            or updated[-1].version.id != intent.id
        ):
            raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        _verify_intent(con, owner, intent, request_id, updated[-1])
        # The current-use reader reconstructs the intent, so import it after this module loads.
        from spm_api.dividend_projection_current import require_current

        checked = require_current(
            con,
            owner,
            intent.event_id,
            intent.account_id,
            projection_id=intent.id,
            projection_no=intent.expected_projection_no + 1,
            as_of=date.fromisoformat(intent.as_of),
        )
        if checked != updated[-1]:
            raise ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE")
        # Derived snapshots do not change their own ledger input revision or schedule a rebuild.
        return checked
