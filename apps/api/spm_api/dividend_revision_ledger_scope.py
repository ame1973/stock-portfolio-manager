"""Freeze the raw ledger evidence consumed by a recorded revision observation."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

import spm_api.dividend_eligibility_service as eligibility

Cell = str | int | None
Rows = tuple[tuple[str, tuple[Cell, ...]], ...]


@dataclass(frozen=True, slots=True)
class RecordedLedgerScope:
    """Private raw rows, not verified ownership, completeness or a financial grant."""

    owner_id: UUID = field(repr=False)
    security_id: UUID = field(repr=False)
    portfolio_ids: tuple[UUID, ...] = field(repr=False)
    trades: Rows = field(repr=False)
    transfers: Rows = field(repr=False)
    actions: Rows = field(repr=False)
    trade_origins: Rows = field(repr=False)
    trade_reviews: Rows = field(repr=False)
    audits: Rows = field(repr=False)


def _freeze(rows: Iterable[RowMapping], *, key: str = "id") -> Rows:
    """Retain exact SQLite scalar types and sort immutable rows by their identity."""
    by_id: dict[str, tuple[Cell, ...]] = {}
    for row in rows:
        identity = row[key]
        values = tuple(row.values())
        if (
            not isinstance(identity, str)
            or not identity
            or any(type(value) not in (str, int, type(None)) for value in values)
        ):
            raise ValueError("Invalid recorded ledger row")
        frozen = cast(tuple[Cell, ...], values)
        if identity in by_id and by_id[identity] != frozen:
            raise ValueError("Inconsistent recorded ledger row")
        by_id[identity] = frozen
    return tuple((identity, by_id[identity]) for identity in sorted(by_id))


def read(
    con: Connection, owner: UUID, security: UUID, portfolio_ids: tuple[UUID, ...]
) -> RecordedLedgerScope:
    """Read exact related facts and audits in the caller's existing database snapshot.

    This only records raw values; eligibility's separate readers validate the histories.
    An unchanged snapshot is not authentication, history completeness or permission to write.
    """
    if (
        not con.in_transaction()
        or not isinstance(owner, UUID)
        or not isinstance(security, UUID)
        or not isinstance(portfolio_ids, tuple)
        or any(not isinstance(identifier, UUID) for identifier in portfolio_ids)
        or tuple(sorted(set(portfolio_ids))) != portfolio_ids
    ):
        raise ValueError("Invalid recorded ledger scope")
    actual = tuple(
        eligibility._canonical_uuid(value)
        for value in con.execute(
            text("SELECT id FROM portfolios WHERE owner_id=:owner ORDER BY id"),
            {"owner": str(owner)},
        ).scalars()
    )
    if actual != portfolio_ids:
        raise ValueError("Inconsistent recorded portfolio scope")
    trades: list[RowMapping] = []
    transfers: list[RowMapping] = []
    for portfolio in portfolio_ids:
        trades.extend(eligibility._trade_rows(con, str(portfolio), str(security)))
        transfers.extend(eligibility._transfer_rows(con, str(portfolio), str(security)))
    actions = eligibility._action_rows(con, str(security))
    frozen_trades = _freeze(trades)
    frozen_transfers = _freeze(transfers)
    frozen_actions = _freeze(actions)

    origins: list[RowMapping] = []
    reviews: list[RowMapping] = []
    audits: list[RowMapping] = []

    def linked_audits(kind: str, identifier: str) -> None:
        audits.extend(
            con.execute(
                text("SELECT * FROM audit_events WHERE entity_type=:kind AND entity_id=:id"),
                {"kind": kind, "id": identifier},
            ).mappings()
        )

    # ponytail: read linked histories per fact; batch these indexed lookups if measured cost grows.
    for trade in frozen_trades:
        identifier = trade[0]
        for kind in ("trade", "trade_audit_origin"):
            linked_audits(kind, identifier)
        origins.extend(
            con.execute(
                text("SELECT * FROM trade_audit_origins WHERE trade_id=:id"), {"id": identifier}
            ).mappings()
        )
        reviews.extend(
            con.execute(
                text("SELECT * FROM trade_history_reviews WHERE trade_id=:id"), {"id": identifier}
            ).mappings()
        )
    frozen_reviews = _freeze(reviews)
    for review in frozen_reviews:
        linked_audits("trade_history_review", review[0])
    for kind, facts in (
        ("security_transfer", frozen_transfers),
        ("corporate_action", frozen_actions),
    ):
        for fact in facts:
            linked_audits(kind, fact[0])
    return RecordedLedgerScope(
        owner,
        security,
        portfolio_ids,
        frozen_trades,
        frozen_transfers,
        frozen_actions,
        _freeze(origins, key="trade_id"),
        frozen_reviews,
        _freeze(audits),
    )
