"""Read retained dividend projection history without authorizing current use."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
from spm_api.audit import canonical, digest
from spm_api.decimal_types import INT64_MAX, from_scaled
from spm_api.dividend_eligibility import EligibilityRule
from spm_api.dividend_eligibility_service import TradeCreationAuditScope, TradeHistoryAuditScope
from spm_api.dividend_projection import ProjectionScope, ProjectionVersion, select_current
from spm_api.dividend_projection_arithmetic import checked_accrual
from spm_api.master_models import Currency


class ProjectionHistoryError(ValueError):
    """Fixed codes only; never include retained data or private rationale."""


@dataclass(frozen=True, slots=True)
class RetainedProjection:
    version: ProjectionVersion = field(repr=False)
    owner_id: UUID = field(repr=False)
    portfolio_id: UUID = field(repr=False)
    broker_id: UUID = field(repr=False)
    account_version: int
    broker_version: int
    portfolio_data_revision: int
    as_of: date
    eligibility_rule: EligibilityRule
    retired_trade_scope: TradeHistoryAuditScope
    creation_audit_scope: TradeCreationAuditScope
    eligible_shares: Decimal | None = field(repr=False)
    tax_rate: Decimal | None = field(repr=False)
    fee_original: Decimal | None = field(repr=False)
    fee_currency: Currency | None = field(repr=False)
    fee_rate_event: Decimal | None = field(repr=False)
    fee_rate_source: str | None = field(repr=False)
    fx_rate_hkd: Decimal | None = field(repr=False)
    fx_source: str | None = field(repr=False)
    request_id: UUID = field(repr=False)
    audit_id: UUID = field(repr=False)
    created_at: datetime = field(repr=False)
    reason: str = field(repr=False)
    history_completeness: Literal["UNVERIFIED"] = "UNVERIFIED"
    scope: Literal["UNVERIFIED_PROJECTION_HISTORY_ONLY"] = "UNVERIFIED_PROJECTION_HISTORY_ONLY"


_PAYLOAD_FIELDS = (
    "id",
    "owner_id",
    "event_id",
    "revision_id",
    "portfolio_id",
    "account_id",
    "broker_id",
    "account_version",
    "broker_version",
    "projection_no",
    "previous_id",
    "state",
    "as_of",
    "eligibility_rule",
    "portfolio_data_revision",
    "retired_trade_scope",
    "creation_audit_scope",
    "history_completeness",
    "eligible_shares_e8",
    "gross_micro",
    "tax_micro",
    "fee_micro",
    "expected_net_micro",
    "rounding_delta_micro",
    "currency",
    "fx_rate_hkd_e12",
    "fx_source",
    "expected_hkd_micro",
    "tax_rate_snapshot_ppm",
    "fee_original_micro",
    "fee_original_currency",
    "fee_rate_event_e12",
    "fee_rate_source",
    "quality",
    "request_id",
    "created_at",
)


def _payload(row: Mapping[str, Any], revision_no: int, reason: str) -> dict[str, Any]:
    if (
        type(revision_no) is not int
        or not 1 <= revision_no <= 2_147_483_647
        or not isinstance(reason, str)
        or "\x00" in reason
        or reason != reason.strip()
        or not 10 <= len(reason) <= 1000
    ):
        raise ValueError
    return {
        **{key: row[key] for key in _PAYLOAD_FIELDS},
        "revision_no": revision_no,
        "reason_sha256": digest(canonical(reason)),
        "scope": "UNVERIFIED_PROJECTION_HISTORY_ONLY",
    }


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError
    return result


def _integer(value: object, minimum: int, maximum: int = INT64_MAX) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError
    return value


def _money(value: object, scale: int) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError
    return Decimal(from_scaled(value, scale))


def _owned_account(con: Connection, owner: UUID, account: UUID) -> bool:
    row = con.execute(
        text("""
            SELECT p.owner_id AS portfolio_owner,b.owner_id AS broker_owner
            FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
              JOIN brokers b ON b.id=a.broker_id WHERE a.id=:account
        """),
        {"account": str(account)},
    ).first()
    if row is None or row.portfolio_owner != str(owner):
        return False
    if row.broker_owner != str(owner):
        raise ValueError
    return True


def history(
    con: Connection, owner: UUID, event_id: UUID, account_id: UUID
) -> tuple[RetainedProjection, ...] | None:
    """Read a whole retained chain in one transaction; not an eligibility or session grant."""
    if (
        not con.in_transaction()
        or not isinstance(owner, UUID)
        or not isinstance(event_id, UUID)
        or not isinstance(account_id, UUID)
    ):
        raise ProjectionHistoryError("INVALID_DIVIDEND_PROJECTION_SCOPE")
    try:
        head = events.get(con, owner, event_id)
        if head is None or not _owned_account(con, owner, account_id):
            return None
        revisions: dict[int, events.ReviewedEvent] = {head.record.revision_no: head}
        rows = con.execute(
            text("""
                SELECT v.*,r.revision_no,p.owner_id AS retained_portfolio_owner,
                    b.owner_id AS retained_broker_owner
                FROM dividend_projection_versions v
                LEFT JOIN dividend_event_revisions r ON r.id=v.revision_id
                LEFT JOIN portfolios p ON p.id=v.portfolio_id
                LEFT JOIN brokers b ON b.id=v.broker_id
                WHERE v.event_id=:event AND v.account_id=:account ORDER BY v.projection_no
            """),
            {"event": str(event_id), "account": str(account_id)},
        ).mappings()
        result: list[RetainedProjection] = []
        request_ids: set[UUID] = set()
        for number, raw_row in enumerate(rows, 1):
            row = cast(Mapping[str, Any], raw_row)
            identifier = events._uuid(row["id"])
            portfolio_id, broker_id = (
                events._uuid(row["portfolio_id"]),
                events._uuid(row["broker_id"]),
            )
            request_id, audit_id = events._uuid(row["request_id"]), events._uuid(row["audit_id"])
            revision_id = events._uuid(row["revision_id"])
            created_at = events._time(row["created_at"])
            revision_no = _integer(row["revision_no"], 1, 2_147_483_647)
            previous = result[-1] if result else None
            if (
                row["owner_id"] != str(owner)
                or row["event_id"] != str(event_id)
                or row["account_id"] != str(account_id)
                or row["retained_portfolio_owner"] != str(owner)
                or row["retained_broker_owner"] != str(owner)
                or _integer(row["projection_no"], 1) != number
                or row["previous_id"] != (str(previous.version.id) if previous else None)
                or (previous is not None and created_at < previous.created_at)
                or request_id in request_ids
            ):
                raise ValueError
            request_ids.add(request_id)
            if revision_no not in revisions:
                reviewed = events.get(con, owner, event_id, version=revision_no)
                if reviewed is None:
                    raise ValueError
                revisions[revision_no] = reviewed
            reviewed = revisions[revision_no]
            if reviewed.record.revision_id != revision_id:
                raise ValueError
            source_audits = con.execute(
                text("""
                    SELECT created_at FROM audit_events
                    WHERE entity_type='dividend_event_revision' AND entity_id=:id
                      AND action='DIVIDEND_EVENT_REVISED'
                """),
                {"id": str(revision_id)},
            ).all()
            if (
                len(source_audits) != 1
                or created_at < reviewed.record.created_at
                or created_at < events._time(source_audits[0].created_at)
            ):
                raise ValueError
            audits = (
                con.execute(
                    text("""
                    SELECT * FROM audit_events
                    WHERE entity_type='dividend_projection' AND entity_id=:id
                      AND action='DIVIDEND_PROJECTION_CREATED'
                """),
                    {"id": str(identifier)},
                )
                .mappings()
                .all()
            )
            if len(audits) != 1:
                raise ValueError
            audit = audits[0]
            reason = audit["reason"]
            if (
                audit["id"] != str(audit_id)
                or audit["owner_id"] != str(owner)
                or audit["request_id"] != str(request_id)
                or audit["created_at"] != row["created_at"]
                or audit["before_sha256"] is not None
                or audit["after_sha256"] != digest(canonical(_payload(row, revision_no, reason)))
            ):
                raise ValueError
            account_version = _integer(row["account_version"], 1)
            broker_version = _integer(row["broker_version"], 1)
            data_revision = _integer(row["portfolio_data_revision"], 0)
            as_of = _day(row["as_of"])
            if (
                row["eligibility_rule"] not in ("ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW")
                or row["retired_trade_scope"]
                not in (
                    "NO_RETIRED_TRADES",
                    "RETIRED_TRADE_HISTORY_UNVERIFIED",
                    "RETIRED_TRADE_HASHES_MATCHED",
                )
                or row["creation_audit_scope"]
                not in ("NO_RECORDED_CREATION_GAP_REVIEW", "CREATION_GAP_REVIEWED_UNVERIFIED")
                or row["history_completeness"] != "UNVERIFIED"
            ):
                raise ValueError
            accrual = checked_accrual(row, reviewed.record)
            version = ProjectionVersion(
                identifier,
                event_id,
                revision_id,
                revision_no,
                account_id,
                number,
                previous.version.id if previous else None,
                cast(Any, row["state"]),
                accrual,
            )
            result.append(
                RetainedProjection(
                    version=version,
                    owner_id=owner,
                    portfolio_id=portfolio_id,
                    broker_id=broker_id,
                    account_version=account_version,
                    broker_version=broker_version,
                    portfolio_data_revision=data_revision,
                    as_of=as_of,
                    eligibility_rule=cast(EligibilityRule, row["eligibility_rule"]),
                    retired_trade_scope=cast(TradeHistoryAuditScope, row["retired_trade_scope"]),
                    creation_audit_scope=cast(TradeCreationAuditScope, row["creation_audit_scope"]),
                    eligible_shares=_money(row["eligible_shares_e8"], 8),
                    tax_rate=_money(row["tax_rate_snapshot_ppm"], 6),
                    fee_original=_money(row["fee_original_micro"], 6),
                    fee_currency=cast(Currency | None, row["fee_original_currency"]),
                    fee_rate_event=_money(row["fee_rate_event_e12"], 12),
                    fee_rate_source=cast(str | None, row["fee_rate_source"]),
                    fx_rate_hkd=_money(row["fx_rate_hkd_e12"], 12),
                    fx_source=cast(str | None, row["fx_source"]),
                    request_id=request_id,
                    audit_id=audit_id,
                    created_at=created_at,
                    reason=reason,
                )
            )
        if result:
            last = result[-1].version
            select_current(
                [ProjectionScope(event_id, last.revision_id, last.revision_no, account_id)],
                [item.version for item in result],
            )
        return tuple(result)
    except (ValueError, TypeError, KeyError, OverflowError, SQLAlchemyError):
        raise ProjectionHistoryError("DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE") from None
