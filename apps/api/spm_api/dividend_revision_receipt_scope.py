"""Freeze receipt and cash rows linked to a recorded event comparison."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from spm_api.dividend_revision_ledger_scope import Rows, _freeze


@dataclass(frozen=True, slots=True)
class RecordedReceiptScope:
    """Private raw evidence, not authenticated payment or complete ownership history."""

    owner_id: UUID = field(repr=False)
    event_id: UUID = field(repr=False)
    entitlements: Rows = field(repr=False)
    legacy_receipts: Rows = field(repr=False)
    legacy_origins: Rows = field(repr=False)
    direct_receipts: Rows = field(repr=False)
    direct_origins: Rows = field(repr=False)
    cash: Rows = field(repr=False)
    audits: Rows = field(repr=False)


def read(con: Connection, owner: UUID, event: UUID) -> RecordedReceiptScope:
    """Read exact linked rows in the caller's snapshot without granting financial use.

    The independent history readers still validate available relationships. Missing legacy
    origins and cash remain unverified; this snapshot does not establish their existence.
    """
    if (
        not con.in_transaction()
        or not isinstance(owner, UUID)
        or not isinstance(event, UUID)
        or con.execute(
            text(
                "SELECT 1 FROM dividend_event_controls ctl JOIN owners o ON o.id=ctl.owner_id "
                "WHERE ctl.event_id=:event AND ctl.owner_id=:owner AND o.is_active=1"
            ),
            {"event": str(event), "owner": str(owner)},
        ).first()
        is None
    ):
        raise ValueError("Invalid recorded receipt scope")
    parameters = {"event": str(event)}
    entitlements = _freeze(
        con.execute(
            text(
                "SELECT ent.* FROM dividend_entitlements ent "
                "JOIN dividend_event_revisions rev ON rev.id=ent.revision_id "
                "WHERE rev.event_id=:event"
            ),
            parameters,
        ).mappings()
    )
    legacy = _freeze(
        con.execute(
            text(
                "SELECT receipt.* FROM dividend_receipts receipt "
                "JOIN dividend_entitlements ent ON ent.id=receipt.entitlement_id "
                "JOIN dividend_event_revisions rev ON rev.id=ent.revision_id "
                "WHERE rev.event_id=:event"
            ),
            parameters,
        ).mappings()
    )
    direct = _freeze(
        con.execute(
            text("SELECT * FROM dividend_direct_receipts WHERE event_id=:event"), parameters
        ).mappings()
    )
    legacy_origins = list(
        con.execute(
            text(
                "SELECT origin.* FROM dividend_receipt_origins origin "
                "WHERE origin.event_id=:event OR EXISTS("
                "SELECT 1 FROM dividend_receipts receipt "
                "JOIN dividend_entitlements ent ON ent.id=receipt.entitlement_id "
                "JOIN dividend_event_revisions rev ON rev.id=ent.revision_id "
                "WHERE rev.event_id=:event AND receipt.id=origin.receipt_id)"
            ),
            parameters,
        ).mappings()
    )
    direct_origins = list(
        con.execute(
            text(
                "SELECT origin.* FROM dividend_direct_receipt_origins origin "
                "WHERE origin.event_id=:event OR EXISTS("
                "SELECT 1 FROM dividend_direct_receipts receipt "
                "WHERE receipt.event_id=:event AND receipt.id=origin.receipt_id)"
            ),
            parameters,
        ).mappings()
    )

    cash: list[RowMapping] = []
    audits: list[RowMapping] = []

    def linked_audits(kind: str, identifier: str) -> None:
        audits.extend(
            con.execute(
                text(
                    "SELECT * FROM audit_events WHERE entity_id=:id AND entity_type IN "
                    "(:kind,:intent,:http_intent,:void_intent,:void_http_intent)"
                ),
                {
                    "id": identifier,
                    "kind": kind,
                    "intent": kind + "_intent",
                    "http_intent": kind + "_http_intent",
                    "void_intent": kind + "_void_intent",
                    "void_http_intent": kind + "_void_http_intent",
                },
            ).mappings()
        )

    # ponytail: scan linked histories per receipt; batch lookups if measured cost grows.
    for prefix, source, receipts in (
        ("dividend_receipt", "DIVIDEND_RECEIPT", legacy),
        ("dividend_direct_receipt", "DIVIDEND_DIRECT_RECEIPT", direct),
    ):
        for identifier, _ in receipts:
            linked_audits(prefix, identifier)
            cash.extend(
                con.execute(
                    text(
                        "SELECT * FROM cash_ledger_entries "
                        "WHERE source_type=:type AND source_id=:id"
                    ),
                    {"type": source, "id": identifier},
                ).mappings()
            )
    for origin in (*legacy_origins, *direct_origins):
        cash.extend(
            con.execute(
                text("SELECT * FROM cash_ledger_entries WHERE id=:id"),
                {"id": origin["cash_entry_id"]},
            ).mappings()
        )
    for identifier, _ in _freeze(cash):
        cash.extend(
            con.execute(
                text("SELECT * FROM cash_ledger_entries WHERE reverses_entry_id=:id"),
                {"id": identifier},
            ).mappings()
        )
    frozen_cash = _freeze(cash)
    for identifier, _ in frozen_cash:
        linked_audits("cash_entry", identifier)
    return RecordedReceiptScope(
        owner,
        event,
        entitlements,
        legacy,
        _freeze(legacy_origins, key="receipt_id"),
        direct,
        _freeze(direct_origins, key="receipt_id"),
        frozen_cash,
        _freeze(audits),
    )
