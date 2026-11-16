"""Validate an owned account's retained and current version identifiers."""

from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_event_service as events
from spm_api.decimal_types import INT64_MAX


def positive_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= INT64_MAX:
        raise ValueError("Expected a positive version")
    return value


def account(con: Connection, owner: UUID, identifier: UUID) -> RowMapping | None:
    """Check current ownership, without proving a historical association or authorization."""
    row = (
        con.execute(
            text("""
            SELECT a.id,a.portfolio_id,a.broker_id,a.version AS account_version,
                   p.owner_id AS portfolio_owner,b.owner_id AS broker_owner,
                   b.version AS broker_version
            FROM accounts a LEFT JOIN portfolios p ON p.id=a.portfolio_id
              LEFT JOIN brokers b ON b.id=a.broker_id WHERE a.id=:account
        """),
            {"account": str(identifier)},
        )
        .mappings()
        .first()
    )
    if row is None or row["portfolio_owner"] != str(owner):
        return None
    if row["broker_owner"] != str(owner):
        raise ValueError("Inconsistent owned account")
    for key in ("id", "portfolio_id", "broker_id"):
        events._uuid(row[key])
    positive_version(row["account_version"])
    positive_version(row["broker_version"])
    return row
