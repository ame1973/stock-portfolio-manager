"""Freeze historical projection and setting rows for a possible event revision."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from spm_api.dividend_revision_ledger_scope import Rows, _freeze


@dataclass(frozen=True, slots=True)
class RecordedProjectionScope:
    """Private raw evidence, not authenticated estimates, eligibility or complete history."""

    owner_id: UUID = field(repr=False)
    event_id: UUID = field(repr=False)
    through_revision_id: UUID = field(repr=False)
    through_revision_no: int
    projections: Rows = field(repr=False)
    tax_settings: Rows = field(repr=False)
    fee_settings: Rows = field(repr=False)
    audits: Rows = field(repr=False)


def read(
    con: Connection, owner: UUID, event: UUID, revision: UUID, revision_no: int
) -> RecordedProjectionScope:
    """Read old rows in one snapshot, excluding any later successor's own writes.

    Matching these rows does not validate their audits, authenticate an actor or authorize
    financial use. A caller must independently verify the current head and all new rows.
    """
    if (
        not con.in_transaction()
        or not all(isinstance(value, UUID) for value in (owner, event, revision))
        or type(revision_no) is not int
        or not 1 <= revision_no <= 2_147_483_647
    ):
        raise ValueError("Invalid recorded projection scope")
    parameters = {
        "owner": str(owner),
        "event": str(event),
        "revision": str(revision),
        "revision_no": revision_no,
    }
    boundary = con.execute(
        text(
            "SELECT rev.revision_no FROM dividend_event_revisions rev "
            "JOIN dividend_event_controls ctl ON ctl.event_id=rev.event_id "
            "JOIN owners owner ON owner.id=ctl.owner_id AND owner.is_active=1 "
            "WHERE rev.id=:revision AND rev.event_id=:event AND ctl.owner_id=:owner"
        ),
        parameters,
    ).first()
    if (
        boundary is None
        or type(boundary.revision_no) is not int
        or boundary.revision_no != revision_no
    ):
        raise ValueError("Invalid recorded projection scope")

    def historical(table: str) -> Rows:
        # All table names are selected below; values remain bound parameters.
        return _freeze(
            con.execute(
                text(
                    f"SELECT record.* FROM {table} record "
                    "LEFT JOIN dividend_event_revisions rev ON rev.id=record.revision_id "
                    "WHERE (record.event_id=:event OR rev.event_id=:event) AND "
                    "(rev.id IS NULL OR rev.event_id!=:event "
                    "OR typeof(rev.revision_no)!='integer' OR rev.revision_no<1 "
                    "OR rev.revision_no<=:revision_no)"
                ),
                parameters,
            ).mappings()
        )

    projections = historical("dividend_projection_versions")
    taxes = historical("dividend_tax_settings")
    fees = historical("dividend_fee_settings")
    audits: list[RowMapping] = []
    # ponytail: scan linked audits per row; batch lookups if measured histories grow.
    for prefix, records in (
        ("dividend_projection", projections),
        ("dividend_tax_setting", taxes),
        ("dividend_fee", fees),
    ):
        for identifier, _ in records:
            audits.extend(
                con.execute(
                    text(
                        "SELECT * FROM audit_events WHERE entity_id=:id AND entity_type IN "
                        "(:base,:setting,:intent,:http)"
                    ),
                    {
                        "id": identifier,
                        "base": prefix,
                        "setting": prefix + "_setting",
                        "intent": prefix + "_intent",
                        "http": prefix + "_http_intent",
                    },
                ).mappings()
            )
    return RecordedProjectionScope(
        owner, event, revision, revision_no, projections, taxes, fees, _freeze(audits)
    )
