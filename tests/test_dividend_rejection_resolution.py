"""A rejected event needs an explicitly pinned, audited immediate successor."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_resolution as resolutions
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OWNER, add_posted_receipt, entitlement
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent, owned_account
from tests.test_dividend_rejection_service import case as case
from tests.test_dividend_rejection_writer import intent as rejection_intent


def reject(engine: Engine, event: Any) -> rejections.RejectionRecord:
    with unit_of_work(engine, write=True) as con:
        return rejection_writer.save(con, OWNER, rejection_intent(event), request_id=uuid4())


def resolve(
    engine: Engine,
    event: Any,
    rejection_id: Any,
    *,
    kind: str = "VERIFY",
    request: Any = None,
    at: datetime | None = None,
    **changes: Any,
) -> tuple[Any, Any, datetime, Any]:
    args = {"resolve_rejection_id": rejection_id, **changes}
    if kind == "MANUAL":
        args["terms"] = event.terms
    successor = next_intent(event, kind, number=event.revision_no + 1, **args)
    request = request or uuid4()
    at = at or datetime.now(UTC)
    with unit_of_work(engine, write=True) as con:
        saved = event_writer.save(con, OWNER, successor, at=at, request_id=request)
    return successor, saved, at, request


def test_current_rejection_requires_an_explicit_pin_before_any_successor(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_REJECTION_CONFIRMATION_REQUIRED$"),
    ):
        event_writer.save(
            con, OWNER, next_intent(case, "VERIFY"), at=datetime.now(UTC), request_id=uuid4()
        )
    with pytest.raises(events.EventError, match="^DIVIDEND_REJECTION_VERSION_CONFLICT$"):
        resolve(engine, case, uuid4())
    assert rejected.revision_id == case.revision_id
    assert database_snapshot(engine) == before


def test_matching_rejection_pin_publishes_one_audited_successor_without_financial_writes(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    successor, saved, at, request = resolve(engine, case, rejected.id)
    assert saved.record.revision_id == successor.revision_id
    assert saved.record.revision_no == 2 and saved.record.review_status == "VERIFIED"
    assert saved.resolution is not None and saved.resolution.rejection_id == rejected.id
    after = database_snapshot(engine)
    allowed = {
        "dividend_events",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "dividend_rejection_resolutions",
        "audit_events",
    }
    assert {key: value for key, value in before.items() if key not in allowed} == {
        key: value for key, value in after.items() if key not in allowed
    }
    assert len(after["dividend_rejection_resolutions"]) == 1
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(engine) as con:
        historical = events.get(con, OWNER, case.event_id, version=1)
        assert historical is not None and historical.record.revision_id == case.revision_id
        assert rejections.history(con, OWNER, case.event_id) == (rejected,)
    with unit_of_work(engine, write=True) as con:
        rejections.require_clear(
            con, OWNER, case.event_id, revision_id=successor.revision_id, version=2
        )
        assert event_writer.save(con, OWNER, successor, at=at, request_id=request) == saved
    assert database_snapshot(engine) == after


def test_resolution_pin_without_a_current_rejection_is_not_silently_ignored(
    engine: Engine, case: Any
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_REJECTION_VERSION_CONFLICT$"):
        resolve(engine, case, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("history", ["entitlement", "receipt"])
def test_existing_financial_history_still_blocks_resolution_and_preserves_locked_fx(
    engine: Engine, case: Any, history: str
) -> None:
    _, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        claim = entitlement({"id": str(case.revision_id), "currency": "USD"}, account)
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        if history == "receipt":
            add_posted_receipt(con, claim, fx=7_800_000_000_000)
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        resolve(engine, case, rejected.id)
    assert database_snapshot(engine) == before


def test_competing_resolution_attempts_never_publish_two_successors(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)

    def attempt(_: int) -> str:
        try:
            resolve(engine, case, rejected.id)
            return "saved"
        except events.EventError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sorted(outcomes) == ["DIVIDEND_EVENT_VERSION_CONFLICT", "saved"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_event_revisions").scalar_one() == 2
        )
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_rejection_resolutions").scalar_one()
            == 1
        )


def test_multiple_rejections_and_resolutions_retain_history_and_zero_write_replays(
    engine: Engine, case: Any
) -> None:
    first_intent, first_request = rejection_intent(case), uuid4()
    with unit_of_work(engine, write=True) as con:
        first_rejection = rejection_writer.save(con, OWNER, first_intent, request_id=first_request)
    first_next, first_saved, first_at, first_next_request = resolve(
        engine, case, first_rejection.id
    )
    second_intent, second_request = rejection_intent(first_next), uuid4()
    with unit_of_work(engine, write=True) as con:
        second_rejection = rejection_writer.save(
            con, OWNER, second_intent, request_id=second_request
        )
        with pytest.raises(rejections.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"):
            rejections.require_clear(
                con, OWNER, case.event_id, revision_id=first_next.revision_id, version=2
            )
    second_next, second_saved, second_at, second_next_request = resolve(
        engine, first_next, second_rejection.id, kind="WITHDRAW"
    )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert rejections.history(con, OWNER, case.event_id) == (first_rejection, second_rejection)
        rejections.require_clear(
            con, OWNER, case.event_id, revision_id=second_next.revision_id, version=3
        )
        assert (
            rejection_writer.save(con, OWNER, first_intent, request_id=first_request)
            == first_rejection
        )
        assert (
            rejection_writer.save(con, OWNER, second_intent, request_id=second_request)
            == second_rejection
        )
        assert (
            event_writer.save(con, OWNER, first_next, at=first_at, request_id=first_next_request)
            == first_saved
        )
        assert (
            event_writer.save(con, OWNER, second_next, at=second_at, request_id=second_next_request)
            == second_saved
        )
    assert database_snapshot(engine) == before


def test_manual_override_can_explicitly_resolve_a_rejection_without_losing_its_origin(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    successor, saved, at, request = resolve(
        engine, case, rejected.id, kind="MANUAL", manual_override=True
    )
    assert saved.manual_override and saved.resolution is not None
    assert saved.resolution.rejection_id == rejected.id
    second = reject(engine, successor)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert rejections.history(con, OWNER, case.event_id) == (rejected, second)
        assert event_writer.save(con, OWNER, successor, at=at, request_id=request) == saved
    assert database_snapshot(engine) == before


def test_replaying_a_resolution_with_a_changed_pin_is_a_conflict(engine: Engine, case: Any) -> None:
    rejected = reject(engine, case)
    successor, _, at, request = resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    for changed in (None, uuid4()):
        with (
            unit_of_work(engine, write=True) as con,
            pytest.raises(events.EventError, match="^DIVIDEND_EVENT_REVISION_CONFLICT$"),
        ):
            event_writer.save(
                con,
                OWNER,
                successor.model_copy(update={"resolve_rejection_id": changed}),
                at=at,
                request_id=request,
            )
    assert database_snapshot(engine) == before


def test_a_resolution_cannot_predate_its_rejection_or_audit(engine: Engine, case: Any) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_TIME_CONFLICT$"):
        resolve(engine, case, rejected.id, at=rejected.created_at - timedelta(microseconds=1))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("target", ["rejection", "resolution", "successor"])
def test_late_duplicate_audit_invalidates_complete_resolution_history(
    engine: Engine, case: Any, target: str
) -> None:
    rejected = reject(engine, case)
    successor, _, at, request = resolve(engine, case, rejected.id)
    entity, identifier, action = {
        "rejection": ("dividend_event_rejection", rejected.id, "DIVIDEND_EVENT_REVISION_REJECTED"),
        "resolution": (
            "dividend_event_revision",
            successor.revision_id,
            "DIVIDEND_EVENT_REJECTION_RESOLUTION",
        ),
        "successor": ("dividend_event_revision", successor.revision_id, "DIVIDEND_EVENT_REVISED"),
    }[target]
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            entity,
            str(identifier),
            action,
            request_id=str(uuid4()),
            before={"synthetic": "duplicate"},
            after={"synthetic": "duplicate"},
            reason="Synthetic late duplicate audit",
        )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"):
            events.get(con, OWNER, case.event_id)
        with pytest.raises(
            rejections.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
        ):
            rejections.history(con, OWNER, case.event_id)
        with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"):
            event_writer.save(con, OWNER, successor, at=at, request_id=request)
    assert database_snapshot(engine) == before


def test_late_resolution_marker_cannot_reclassify_an_ordinary_successor(
    engine: Engine, case: Any
) -> None:
    successor = next_intent(case, "VERIFY")
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, OWNER, successor, at=datetime.now(UTC), request_id=uuid4())
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            str(successor.revision_id),
            "DIVIDEND_EVENT_REJECTION_RESOLUTION",
            request_id=str(uuid4()),
            before={"synthetic": "late marker"},
            after={"synthetic": "late marker"},
            reason="Synthetic late resolution marker",
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, case.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["resolution_audit", "resolution_insert", "post_read"])
def test_caught_resolution_failure_rolls_back_every_inner_write(
    engine: Engine, case: Any, failure: str
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    original_append = resolutions.append
    original_get = events.get
    reads = 0

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        audit(*args, **kwargs)
        raise RuntimeError("synthetic failure after resolution audit")

    def fail_insert(*args: Any, **kwargs: Any) -> None:
        original_append(*args, **kwargs)
        raise RuntimeError("synthetic failure after resolution insert")

    def fail_post_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        result = original_get(*args, **kwargs)
        if result is not None and result.record.revision_no == 2:
            reads += 1
            raise RuntimeError("synthetic failure after publication")
        return result

    target, name, effect = {
        "resolution_audit": (resolutions, "audit", fail_audit),
        "resolution_insert": (resolutions, "append", fail_insert),
        "post_read": (events, "get", fail_post_read),
    }[failure]
    with unit_of_work(engine, write=True) as con:
        with (
            patch.object(target, name, side_effect=effect),
            pytest.raises(RuntimeError, match="synthetic failure"),
        ):
            event_writer.save(
                con,
                OWNER,
                next_intent(case, "VERIFY", resolve_rejection_id=rejected.id),
                at=datetime.now(UTC),
                request_id=uuid4(),
            )
        assert con.in_transaction()
    assert reads == (1 if failure == "post_read" else 0)
    assert database_snapshot(engine) == before
