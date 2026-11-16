"""Revalidate one retained projection without granting income or authenticating a caller."""

from datetime import date
from uuid import UUID

from pydantic_core import PydanticSerializationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_projection_writer as writer
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_projection_history import RetainedProjection, history


def require_current(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    account_id: UUID,
    *,
    projection_id: UUID,
    projection_no: int,
    as_of: date,
) -> RetainedProjection:
    """Recompute current recorded inputs in the same writer; authenticate separately."""
    try:
        return _require_current(
            con,
            owner,
            event_id,
            account_id,
            projection_id=projection_id,
            projection_no=projection_no,
            as_of=as_of,
        )
    except writer.ProjectionWriteError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        SQLAlchemyError,
        PydanticSerializationError,
    ):
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None


def _require_current(
    con: Connection,
    owner: UUID,
    event_id: UUID,
    account_id: UUID,
    *,
    projection_id: UUID,
    projection_no: int,
    as_of: date,
) -> RetainedProjection:
    writer._writer(con, owner)
    if (
        not all(isinstance(value, UUID) for value in (event_id, account_id, projection_id))
        or type(projection_no) is not int
        or not 1 <= projection_no <= INT64_MAX
        or type(as_of) is not date
    ):
        raise writer.ProjectionWriteError("INVALID_DIVIDEND_PROJECTION_SCOPE")
    retained = history(con, owner, event_id, account_id)
    if retained is None or not retained:
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_NOT_FOUND")
    record = retained[-1]
    version = record.version
    if (version.id, version.projection_no) != (projection_id, projection_no):
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_VERSION_CONFLICT")
    if record.as_of != as_of:
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_AS_OF_CHANGED")
    tax, fee = record.tax_provenance, record.fee_provenance
    if tax is None or fee is None:
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_UNVERIFIED")
    intent = writer.ProjectionIntent(
        id=version.id,
        event_id=version.event_id,
        revision_id=version.revision_id,
        revision_no=version.revision_no,
        account_id=version.account_id,
        portfolio_id=record.portfolio_id,
        broker_id=record.broker_id,
        account_version=record.account_version,
        broker_version=record.broker_version,
        expected_data_revision=record.portfolio_data_revision,
        expected_projection_no=version.projection_no - 1,
        expected_projection_id=version.previous_id,
        as_of=record.as_of.isoformat(),
        eligibility_rule=record.eligibility_rule,
        tax_effective_on=tax.effective_on.isoformat(),
        expected_tax_head_no=tax.observed_head_no,
        expected_tax_head_id=tax.observed_head_id,
        expected_tax_selected_id=tax.selected_setting_id,
        fee_effective_on=fee.effective_on.isoformat(),
        expected_fee_head_no=fee.observed_head_no,
        expected_fee_head_id=fee.observed_head_id,
        expected_fee_selected_id=fee.selected_setting_id,
        fx_rate_hkd=format(record.fx_rate_hkd, "f") if record.fx_rate_hkd is not None else None,
        fx_source=record.fx_source,
        fee_rate_event=format(record.fee_rate_event, "f")
        if record.fee_rate_event is not None
        else None,
        fee_rate_source=record.fee_rate_source,
        reason=record.reason,
    )
    event, book, selected_tax, selected_fee = writer._current_inputs(con, owner, intent)
    try:
        writer._check_time(con, intent, event, book, None, record.created_at)
    except writer.ProjectionWriteError as error:
        if str(error) == "DIVIDEND_PROJECTION_TIME_CONFLICT":
            raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_STALE") from None
        raise
    values = writer._values(intent, event, book, selected_tax, selected_fee)
    stored = (
        con.execute(
            text("SELECT * FROM dividend_projection_versions WHERE id=:id"),
            {"id": str(projection_id)},
        )
        .mappings()
        .first()
    )
    if (
        stored is None
        or record.retired_trade_scope != book.trade_history_audit_scope
        or record.creation_audit_scope != book.trade_creation_audit_scope
        or any(stored[name] != value for name, value in values.items())
    ):
        raise writer.ProjectionWriteError("DIVIDEND_PROJECTION_STALE")
    # SQL/audit consistency and this recomputation do not authenticate the original actor.
    return record
