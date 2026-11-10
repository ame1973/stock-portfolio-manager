"""Historical correction authorizations validate retained evidence without granting new use."""

from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api import trade_audit, trade_correction_authorization, trade_history_review, trade_service
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.trade_codec import as_trade
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_trade_correction_authorization_migration import (
    REASON,
    TABLE,
    authorization_values,
)
from tests.test_trade_history_review_migration import review_values
from tests.test_trades import book as book

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    "defect",
    [
        "not-text",
        "invalid-json",
        "not-array",
        "empty",
        "whitespace",
        "not-object",
        "missing-key",
        "extra-key",
        "boolean-version",
        "invalid-version",
        "numeric-revision",
        "zero-revision",
        "leading-zero-revision",
        "overflow-revision",
        "wrong-gap",
        "bad-trade-id",
        "bad-review-id",
        "duplicate-trade",
    ],
)
async def test_pure_scope_parser_rejects_noncanonical_or_invalid_pins(defect: str) -> None:
    pin: dict[str, Any] = {
        "trade_id": str(uuid4()),
        "trade_version": 1,
        "review_id": str(uuid4()),
        "review_revision": "1",
        "gap": "MISSING_CREATION_AUDIT",
    }
    raw: object = canonical([pin])
    if defect == "not-text":
        raw = None
    elif defect == "invalid-json":
        raw = "["
    elif defect == "not-array":
        raw = "{}"
    elif defect == "empty":
        raw = "[]"
    elif defect == "whitespace":
        raw = " " + str(raw)
    elif defect == "not-object":
        raw = canonical(["invalid"])
    elif defect == "duplicate-trade":
        raw = canonical([pin, pin])
    else:
        if defect == "missing-key":
            pin.pop("gap")
        elif defect == "extra-key":
            pin["extra"] = "invalid"
        elif defect == "boolean-version":
            pin["trade_version"] = True
        elif defect == "invalid-version":
            pin["trade_version"] = 3
        elif defect == "numeric-revision":
            pin["review_revision"] = 1
        elif defect == "zero-revision":
            pin["review_revision"] = "0"
        elif defect == "leading-zero-revision":
            pin["review_revision"] = "01"
        elif defect == "overflow-revision":
            pin["review_revision"] = "9223372036854775808"
        elif defect == "wrong-gap":
            pin["gap"] = "MISSING_RETIRED_ORIGIN"
        elif defect == "bad-trade-id":
            pin["trade_id"] = "invalid"
        elif defect == "bad-review-id":
            pin["review_id"] = "invalid"
        raw = canonical([pin])
    with pytest.raises(ValueError):
        trade_correction_authorization._pins(raw)


async def test_reader_requires_a_consistent_transaction_and_hides_unknown_records(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    owner, identity = UUID(book["owner"]["id"]), uuid4()
    before = database_snapshot(engine)
    with (
        engine.connect() as con,
        pytest.raises(
            trade_correction_authorization.CorrectionAuthorizationError,
            match="INVALID_TRADE_CORRECTION_SCOPE",
        ),
    ):
        trade_correction_authorization.get(con, owner, identity)
    with unit_of_work(engine) as con:
        assert trade_correction_authorization.get(con, owner, identity) is None
    assert database_snapshot(engine) == before


def audit_payload(values: dict[str, Any], pins: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": values["id"],
        "owner_id": values["owner_id"],
        "root_id": values["root_id"],
        "trade_id": values["trade_id"],
        "trade_version": values["trade_version"],
        "operation": values["operation"],
        "result_trade_id": values["result_trade_id"],
        "scope": pins,
        "intent_sha256": values["intent_sha256"],
        "request_id": values["request_id"],
        "reason_sha256": digest(canonical(REASON)),
        "scope_type": "UNVERIFIED_TRADE_CORRECTION_ONLY",
    }


def stored(
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"] = "REVISE",
    *,
    two_gaps: bool = False,
    canonical_scope: bool = True,
    remember_result: bool = True,
    reverse_scope: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    values, pins = authorization_values(
        engine, book, operation, two_gaps=two_gaps, remember_result=remember_result
    )
    values["id"] = str(uuid4())
    if reverse_scope:
        pins.reverse()
        values["scope_json"] = canonical(pins)
    if not canonical_scope:
        values["scope_json"] = " " + values["scope_json"]
    payload = audit_payload(values, pins)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade_correction_authorization",
            values["id"],
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=values["request_id"],
            after=payload,
            reason=REASON,
        )
        entry = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", values["id"]),
        ).one()
        values.update(audit_id=entry.id, created_at=entry.created_at)
        insert(con, TABLE, values)
    return values, pins


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
@pytest.mark.parametrize("two_gaps", [False, True])
async def test_read_checks_correlated_decisions_and_facts_without_claiming_authority(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
    two_gaps: bool,
) -> None:
    values, pins = stored(engine, book, operation, two_gaps=two_gaps)
    owner, identity = UUID(values["owner_id"]), UUID(values["id"])
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        record = trade_correction_authorization.get(con, owner, identity)
        assert record is not None
        assert record.operation == operation
        assert record.scope == "UNVERIFIED_TRADE_CORRECTION_ONLY"
        assert record.history_completeness == "UNVERIFIED"
        assert [str(pin.trade_id) for pin in record.pins] == [pin["trade_id"] for pin in pins]
        assert [str(pin.review_revision) for pin in record.pins] == [
            pin["review_revision"] for pin in pins
        ]
        assert all(value not in repr(record) for value in (str(owner), values["trade_id"], REASON))
    assert database_snapshot(engine) == before


async def test_unrelated_well_shaped_sql_audit_is_not_authorization_evidence(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = authorization_values(engine, book)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            trade_correction_authorization.CorrectionAuthorizationError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ),
    ):
        trade_correction_authorization.get(con, UUID(values["owner_id"]), UUID(values["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "defect", ["noncanonical-scope", "missing-result-origin", "reversed-scope"]
)
async def test_invalid_retained_evidence_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], defect: str
) -> None:
    values, _ = stored(
        engine,
        book,
        canonical_scope=defect != "noncanonical-scope",
        remember_result=defect != "missing-result-origin",
        two_gaps=defect == "reversed-scope",
        reverse_scope=defect == "reversed-scope",
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            trade_correction_authorization.CorrectionAuthorizationError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ),
    ):
        trade_correction_authorization.get(con, UUID(values["owner_id"]), UUID(values["id"]))
    assert database_snapshot(engine) == before


async def test_historical_read_survives_withdrawal_and_matching_later_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = stored(engine, book)
    owner, target = UUID(values["owner_id"]), UUID(values["trade_id"])
    with unit_of_work(engine, write=True) as con:
        history = trade_history_review.history(con, owner, target)
        assert history
        original = history[0]
        row = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(target),)).mappings().one()
        )
        dto = as_trade(row).model_copy(
            update={
                "status": original.observed_status,
                "version": original.observed_trade_version,
                "updated_at": original.observed_updated_at,
            }
        )
        audit(
            con,
            str(owner),
            "trade",
            str(target),
            "CREATE",
            request_id=str(uuid4()),
            after=dto.model_dump(mode="json"),
        )
        trade_history_review.save(
            con,
            owner,
            target,
            review_id=uuid4(),
            request_id=uuid4(),
            expected_trade_version=2,
            expected_revision=1,
            decision="WITHDRAW",
            reason=REASON,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        record = trade_correction_authorization.get(con, owner, UUID(values["id"]))
        assert record is not None and record.history_completeness == "UNVERIFIED"
        assert record.pins[0].review_revision == 1
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("later", ["REVISE", "VOID"])
async def test_historical_result_remains_readable_after_later_audited_retirement(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], later: str
) -> None:
    values, _ = stored(engine, book)
    owner, result = values["owner_id"], values["result_trade_id"]
    with unit_of_work(engine, write=True) as con:
        previous = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (result,)).mappings().one()
        )
        trade_service._retire(con, previous, "SUPERSEDED" if later == "REVISE" else "VOID")
        if later == "REVISE":
            successor, at = str(uuid4()), stamp()
            insert(
                con,
                "trades",
                {
                    **dict(previous),
                    "id": successor,
                    "revision": previous["revision"] + 1,
                    "supersedes_id": result,
                    "created_at": at,
                    "updated_at": at,
                },
            )
            row = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (successor,))
                .mappings()
                .one()
            )
            after = as_trade(row).model_dump(mode="json")
        else:
            successor, after = result, {"status": "VOID", "version": 2}
        request = str(uuid4())
        audit(
            con,
            owner,
            "trade",
            successor,
            later,
            request_id=request,
            before=dict(previous),
            after=after,
            reason="Retire this synthetic later trade revision",
        )
        if later == "REVISE":
            trade_audit.remember_created(con, UUID(owner), UUID(successor), UUID(request))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert trade_correction_authorization.get(con, UUID(owner), UUID(values["id"])) is not None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("duplicate", ["authorization", "trade"])
async def test_later_duplicate_audit_cannot_hide_behind_old_valid_evidence(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], duplicate: str
) -> None:
    values, _ = stored(engine, book)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            values["owner_id"],
            "trade_correction_authorization" if duplicate == "authorization" else "trade",
            values["id"] if duplicate == "authorization" else values["result_trade_id"],
            "TRADE_CORRECTION_UNVERIFIED" if duplicate == "authorization" else "REVISE",
            request_id=str(uuid4()),
            before=None if duplicate == "authorization" else {"synthetic": "unrelated"},
            after={"synthetic": "duplicate evidence"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            trade_correction_authorization.CorrectionAuthorizationError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ),
    ):
        trade_correction_authorization.get(con, UUID(values["owner_id"]), UUID(values["id"]))
    assert database_snapshot(engine) == before


async def test_later_corrupt_trade_link_cannot_hide_behind_a_valid_result(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = stored(engine, book)
    owner, current_id = values["owner_id"], values["result_trade_id"]
    with unit_of_work(engine, write=True) as con:
        for step in range(2):
            previous = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (current_id,))
                .mappings()
                .one()
            )
            trade_service._retire(con, previous, "SUPERSEDED")
            successor, at = str(uuid4()), stamp()
            insert(
                con,
                "trades",
                {
                    **dict(previous),
                    "id": successor,
                    "revision": previous["revision"] + 1,
                    "supersedes_id": current_id,
                    "created_at": at,
                    "updated_at": at,
                },
            )
            row = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (successor,))
                .mappings()
                .one()
            )
            request = str(uuid4())
            audit(
                con,
                owner,
                "trade",
                successor,
                "REVISE",
                request_id=request,
                before=dict(previous) if step == 0 else {"synthetic": "unrelated predecessor"},
                after=as_trade(row).model_dump(mode="json"),
                reason="Revise this synthetic later trade",
            )
            trade_audit.remember_created(con, UUID(owner), UUID(successor), UUID(request))
            current_id = successor
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            trade_correction_authorization.CorrectionAuthorizationError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ),
    ):
        trade_correction_authorization.get(con, UUID(owner), UUID(values["id"]))
    assert database_snapshot(engine) == before


async def test_corrupt_later_review_cannot_hide_behind_an_old_valid_pin(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, pins = stored(engine, book, two_gaps=True)
    with unit_of_work(engine, write=True) as con:
        previous = (
            con.exec_driver_sql(
                "SELECT * FROM trade_history_reviews WHERE id=?", (pins[0]["review_id"],)
            )
            .mappings()
            .one()
        )
        malformed = review_values(
            con,
            values["owner_id"],
            pins[0]["trade_id"],
            decision="WITHDRAW",
            previous=dict(previous),
            reason=REASON,
        )
        insert(con, "trade_history_reviews", malformed)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            trade_correction_authorization.CorrectionAuthorizationError,
            match="TRADE_CORRECTION_UNAVAILABLE",
        ),
    ):
        trade_correction_authorization.get(con, UUID(values["owner_id"]), UUID(values["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("operation", ["REVISE", "VOID"])
async def test_recorded_target_origin_supports_a_gap_on_an_earlier_revision(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    operation: Literal["REVISE", "VOID"],
) -> None:
    first, pins = stored(engine, book)
    target, owner = first["result_trade_id"], first["owner_id"]
    pinned = [{**pins[0], "trade_version": 2}]
    request, identifier = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        previous = (
            con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (target,)).mappings().one()
        )
        trade_service._retire(con, previous, "SUPERSEDED" if operation == "REVISE" else "VOID")
        result = target
        if operation == "REVISE":
            result, at = str(uuid4()), stamp()
            insert(
                con,
                "trades",
                {
                    **dict(previous),
                    "id": result,
                    "revision": previous["revision"] + 1,
                    "supersedes_id": target,
                    "created_at": at,
                    "updated_at": at,
                },
            )
            successor = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (result,)).mappings().one()
            )
            after = as_trade(successor).model_dump(mode="json")
        else:
            after = {"status": "VOID", "version": 2}
        audit(
            con,
            owner,
            "trade",
            result,
            operation,
            request_id=request,
            before=dict(previous),
            after=after,
            reason=REASON,
        )
        if operation == "REVISE":
            trade_audit.remember_created(con, UUID(owner), UUID(result), UUID(request))
        values = {
            "id": identifier,
            "owner_id": owner,
            "root_id": first["root_id"],
            "trade_id": target,
            "trade_version": 1,
            "operation": operation,
            "result_trade_id": result,
            "scope_json": canonical(pinned),
            "intent_sha256": "b" * 64,
            "request_id": request,
        }
        audit(
            con,
            owner,
            "trade_correction_authorization",
            identifier,
            "TRADE_CORRECTION_UNVERIFIED",
            request_id=request,
            after=audit_payload(values, pinned),
            reason=REASON,
        )
        entry = con.exec_driver_sql(
            "SELECT id,created_at FROM audit_events WHERE entity_type=? AND entity_id=?",
            ("trade_correction_authorization", identifier),
        ).one()
        values.update(audit_id=entry.id, created_at=entry.created_at)
        insert(con, TABLE, values)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        previous_grant = trade_correction_authorization.get(con, UUID(owner), UUID(first["id"]))
        next_grant = trade_correction_authorization.get(con, UUID(owner), UUID(identifier))
        assert previous_grant is not None and next_grant is not None
        assert next_grant.trade_id == UUID(target) and next_grant.pins[0].trade_version == 2
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_read_the_historical_authorization(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    values, _ = stored(engine, book)
    foreign = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(foreign),),
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert trade_correction_authorization.get(con, foreign, UUID(values["id"])) is None
    assert database_snapshot(engine) == before
