"""Freeze original projection and setting evidence before a possible revision rebuild."""

from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as events
import spm_api.dividend_revision_projection_scope as projection_scope
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "entity",
    [
        "duplicate_projection_intent",
        "dividend_projection",
        "dividend_projection_http_intent",
        "dividend_fee_http_intent",
    ],
)
async def test_projection_and_setting_audit_additions_stale_a_rebuild_observation(
    engine: Engine, scope: dict[str, Any], entity: str
) -> None:
    book, observed = scope["book"], scope["observed"]
    account = next(item for item in observed.preview.snapshot.retained.accounts if item.projections)
    projection = account.projections[-1]
    assert projection.fee_provenance is not None
    assert projection.fee_provenance.selected_setting_id is not None
    identifier = (
        projection.fee_provenance.selected_setting_id
        if entity == "dividend_fee_http_intent"
        else projection.version.id
    )
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(con, book["owner"], scope["decision"], scope["proposal"], observed)
        if entity == "duplicate_projection_intent":
            inserted = con.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,owner_id,entity_type,entity_id,action,before_sha256,after_sha256,"
                "reason,request_id,created_at FROM audit_events "
                "WHERE entity_type='dividend_projection_intent' AND entity_id=? "
                "AND action='DIVIDEND_PROJECTION_INTENT_RECORDED'",
                (str(uuid4()), str(identifier)),
            )
            assert inserted.rowcount == 1
        else:
            audit(
                con,
                str(book["owner"]),
                entity,
                str(identifier),
                "SYNTHETIC_LATE_EVIDENCE",
                request_id=str(uuid4()),
                after={"scope": "synthetic"},
            )
        fresh = rebuild.observe(
            con, book["owner"], scope["decision"].event_id, scope["decision"], scope["proposal"]
        )
        assert fresh is not None and fresh.preview == observed.preview
        current.require_current(con, book["owner"], scope["decision"], scope["proposal"], observed)
    assert database_snapshot(engine) == before


async def test_standalone_raw_scope_retains_original_projection_settings_and_audits(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, observed = scope["book"], scope["observed"]
    previous = observed.event.record
    with unit_of_work(engine) as con:
        raw = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
    assert observed.projections == raw
    assert raw.owner_id == book["owner"] and raw.event_id == previous.event_id
    assert len(raw.projections) == len(raw.tax_settings) == len(raw.fee_settings) == 1
    assert {row[1][2] for row in raw.audits} >= {
        "dividend_projection",
        "dividend_projection_intent",
        "dividend_tax_setting",
        "dividend_fee_setting",
    }
    assert str(raw.owner_id) not in repr(raw) and raw.projections[0][0] not in repr(raw)
    before = database_snapshot(engine)

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_projection_http_intent",
            raw.projections[0][0],
            "SYNTHETIC_LATE_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        changed = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
        assert changed != raw and changed.projections == raw.projections
        raise Rollback
    assert database_snapshot(engine) == before


async def test_standalone_raw_scope_excludes_real_successor_settings_and_projection(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    settings(engine, book, rate="0", fee="0")
    previous = book["event"]
    with unit_of_work(engine) as con:
        raw = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
    assert not raw.projections and len(raw.tax_settings) == len(raw.fee_settings) == 1
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    with unit_of_work(engine, write=True) as con:
        successor = events.save(con, book["owner"], decision, at=now(), request_id=uuid4())
    successor_book = {**book, "event": successor.record}
    tax, fee = settings(engine, successor_book, rate="0", fee="0")
    new_projection = save(engine, successor_book, intent(engine, successor_book, tax, fee), uuid4())
    with unit_of_work(engine) as con:
        assert (
            projection_scope.read(
                con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
            )
            == raw
        )
        successor_scope = projection_scope.read(
            con,
            book["owner"],
            previous.event_id,
            successor.record.revision_id,
            successor.record.revision_no,
        )
    assert [row[0] for row in successor_scope.projections] == [str(new_projection.version.id)]
    assert len(successor_scope.tax_settings) == len(successor_scope.fee_settings) == 2


async def test_standalone_scope_requires_exact_owner_revision_and_transaction(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, previous = scope["book"], scope["observed"].event.record
    with (
        engine.connect() as con,
        pytest.raises(ValueError, match="^Invalid recorded projection scope$"),
    ):
        projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
    with unit_of_work(engine) as con:
        for owner, event, revision, number in (
            (uuid4(), previous.event_id, previous.revision_id, previous.revision_no),
            (book["owner"], uuid4(), previous.revision_id, previous.revision_no),
            (book["owner"], previous.event_id, uuid4(), previous.revision_no),
            (book["owner"], previous.event_id, previous.revision_id, 2),
            (cast(Any, "private-owner"), previous.event_id, previous.revision_id, 1),
            (book["owner"], cast(Any, "private-event"), previous.revision_id, 1),
            (book["owner"], previous.event_id, cast(Any, "private-revision"), 1),
        ):
            with pytest.raises(ValueError, match="^Invalid recorded projection scope$"):
                projection_scope.read(con, owner, event, revision, number)
        for number in (None, True, False, 0, -1, 2_147_483_648, 1.0, "1"):
            with pytest.raises(ValueError, match="^Invalid recorded projection scope$"):
                projection_scope.read(
                    con, book["owner"], previous.event_id, previous.revision_id, cast(Any, number)
                )


@pytest.mark.parametrize("invalid", ["null_id", "binary_id", "binary_reason"])
async def test_standalone_scope_rejects_malformed_linked_audits(
    engine: Engine, scope: dict[str, Any], invalid: str
) -> None:
    class Rollback(Exception):
        pass

    book, previous = scope["book"], scope["observed"].event.record
    identity: Any = (
        None if invalid == "null_id" else b"private" if invalid == "binary_id" else str(uuid4())
    )
    reason: Any = b"private" if invalid == "binary_reason" else "Synthetic evidence"
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        raw = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,reason,created_at) "
            "VALUES (?,?,'dividend_projection_http_intent',?,'SYNTHETIC_EVIDENCE',?,?)",
            (identity, str(book["owner"]), raw.projections[0][0], reason, stamp()),
        )
        with pytest.raises(ValueError, match="^Invalid recorded ledger row$"):
            projection_scope.read(
                con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
            )
        raise Rollback
    assert database_snapshot(engine) == before


async def test_standalone_scope_excludes_other_event_for_same_owner_and_account(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, previous = scope["book"], scope["observed"].event.record
    with unit_of_work(engine) as con:
        raw = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
    other = manual(
        terms=terms(security_id=previous.terms.security_id, dps="2", currency="USD"),
        reason="Synthetic distinct event for the same recorded account.",
    )
    with unit_of_work(engine, write=True) as con:
        other_event = events.save(con, book["owner"], other, at=now(), request_id=uuid4())
    other_book = {**book, "event": other_event.record}
    tax, fee = settings(engine, other_book, rate="0", fee="0")
    other_projection = save(engine, other_book, intent(engine, other_book, tax, fee), uuid4())
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_projection_http_intent",
            str(other_projection.version.id),
            "SYNTHETIC_UNRELATED_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "synthetic"},
        )
        assert (
            projection_scope.read(
                con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
            )
            == raw
        )
        other_raw = projection_scope.read(
            con,
            book["owner"],
            other_event.record.event_id,
            other_event.record.revision_id,
            other_event.record.revision_no,
        )
    assert [row[0] for row in other_raw.projections] == [str(other_projection.version.id)]
    assert str(other_projection.version.id) not in {row[0] for row in raw.projections}


async def test_standalone_scope_uses_the_readers_wal_snapshot(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, previous = scope["book"], scope["observed"].event.record
    with unit_of_work(engine) as reader:
        raw = projection_scope.read(
            reader, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
        with unit_of_work(engine, write=True) as writer:
            audit(
                writer,
                str(book["owner"]),
                "dividend_projection_http_intent",
                raw.projections[0][0],
                "SYNTHETIC_LATE_EVIDENCE",
                request_id=str(uuid4()),
                after={"scope": "synthetic"},
            )
        assert (
            projection_scope.read(
                reader, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
            )
            == raw
        )
    with unit_of_work(engine) as con:
        fresh = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
    assert fresh != raw and fresh.projections == raw.projections


async def test_standalone_scope_rejects_forged_numeric_type_equivalence(
    engine: Engine, scope: dict[str, Any]
) -> None:
    book, previous = scope["book"], scope["observed"].event.record
    with unit_of_work(engine) as con:
        raw = projection_scope.read(
            con, book["owner"], previous.event_id, previous.revision_id, previous.revision_no
        )
    identifier, values = raw.projections[0]
    row: list[Any] = list(values)
    index = next(i for i, value in enumerate(row) if type(value) is int and value != 0)
    row[index] = float(row[index])
    forged = replace(raw, projections=((identifier, tuple(row)), *raw.projections[1:]))
    assert forged == raw
    assert not current._exact(forged, raw)
    forged_observation = replace(scope["observed"], projections=forged)
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(
            con, book["owner"], scope["decision"], scope["proposal"], forged_observation
        )
    assert database_snapshot(engine) == before
