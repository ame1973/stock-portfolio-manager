"""Internal rejection writes retain decisions and invalidate without economic grants."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_service as rejection
import spm_api.dividend_rejection_writer as writer
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion
from spm_api.master_service import invalidate_portfolio
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_candidate_service import basis as basis
from tests.test_dividend_candidate_service import decision as candidate_decision
from tests.test_dividend_candidate_service import save as save_candidate
from tests.test_dividend_event_migration import (
    OTHER,
    OWNER,
    REASON,
    add_posted_receipt,
    entitlement,
)
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import (
    AT,
    candidate_intent,
    manual,
    next_intent,
    owned_account,
    submit,
)
from tests.test_dividend_projection_migration import snapshot_values
from tests.test_dividend_rejection_service import case as case
from tests.test_dividend_rejection_service import store_rejection


def intent(event: Any, **changes: Any) -> writer.RejectionIntent:
    return writer.RejectionIntent.model_validate(
        {
            "id": uuid4(),
            "event_id": event.event_id,
            "revision_id": event.revision_id,
            "revision_no": event.revision_no,
            "citation": "Synthetic owner rejection reference",
            "source_url": "https://example.test/notice?private=synthetic",
            "reason": REASON,
            **changes,
        }
    )


def save(
    engine: Engine,
    value: writer.RejectionIntent,
    *,
    owner: UUID = OWNER,
    request: UUID | None = None,
) -> rejection.RejectionRecord:
    with unit_of_work(engine, write=True) as con:
        return writer.save(con, owner, value, request_id=request or uuid4())


def affected_portfolio(engine: Engine, event: Any, owner: UUID = OWNER) -> tuple[str, str]:
    portfolio, account = owned_account(engine, owner)
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "positions",
            {
                "portfolio_id": portfolio,
                "account_id": account,
                "security_id": str(event.terms.security_id),
                "projection_version": 0,
                "shares_e8": 100_000_000,
                "remaining_cost_hkd_micro": 0,
                "realized_hkd_micro": 0,
                "calculated_at_utc": stamp(),
            },
        )
    return portfolio, account


def test_rejection_appends_audited_decision_and_only_owned_invalidations(
    engine: Engine, case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    portfolio, account = affected_portfolio(engine, case)
    other_portfolio, _ = affected_portfolio(engine, case, OTHER)
    with unit_of_work(engine, write=True) as con:
        claim = entitlement({"id": str(case.revision_id), "currency": "USD"}, account)
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
        projection = snapshot_values(
            con,
            {"id": str(case.event_id)},
            {"id": str(case.revision_id), "currency": "USD"},
            account,
        )
        insert(con, "dividend_projection_versions", projection)
    before = database_snapshot(engine)
    request, value = uuid4(), intent(case)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A rejection must not contact providers or read credentials")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    result = save(engine, value, request=request)
    assert result.id == value.id and result.request_id == request
    assert result.decision == "REJECT_REVISION" and result.scope == "OWNER_REVISION_REJECTION_ONLY"
    assert result.citation == value.citation and result.source_url == value.source_url
    assert value.source_url is not None
    assert value.citation not in repr(result) and value.source_url not in repr(result)
    after = database_snapshot(engine)
    changed = {
        "dividend_event_rejections",
        "audit_events",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: rows for key, rows in before.items() if key not in changed} == {
        key: rows for key, rows in after.items() if key not in changed
    }
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
    with unit_of_work(engine) as con:
        assert rejection.history(con, OWNER, case.event_id) == (result,)
        rows = con.exec_driver_sql(
            "SELECT id,data_revision FROM portfolios WHERE id IN (?,?) ORDER BY id",
            (portfolio, other_portfolio),
        ).all()
        invalidation = con.exec_driver_sql(
            "SELECT portfolio_id,source_id,reason,from_date FROM projection_invalidations "
            "WHERE source_id=?",
            (str(value.id),),
        ).one()
    revisions = {str(row.id): int(row.data_revision) for row in rows}
    assert revisions[portfolio] == 1 and revisions[other_portfolio] == 0
    assert tuple(invalidation) == (
        portfolio,
        str(value.id),
        "DIVIDEND_REVISION_REJECTED",
        case.terms.ex_date,
    )
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"),
    ):
        rejection.require_clear(
            con, OWNER, case.event_id, revision_id=case.revision_id, version=case.revision_no
        )
    assert database_snapshot(engine) == after


def test_exact_internal_replay_is_zero_write_and_conflicting_reuse_fails(
    engine: Engine, case: Any
) -> None:
    value, request = intent(case), uuid4()
    first = save(engine, value, request=request)
    before = database_snapshot(engine)
    assert save(engine, value, request=request) == first
    for field in ("id", "event_id"):
        assert not writer._replay_matches(first.model_copy(update={field: uuid4()}), value, request)
    for changed, new_request in (
        (value, uuid4()),
        (value.model_copy(update={"revision_id": uuid4()}), request),
        (value.model_copy(update={"revision_no": 2}), request),
        (value.model_copy(update={"citation": "Different synthetic citation"}), request),
        (value.model_copy(update={"source_url": None}), request),
        (value.model_copy(update={"reason": "Different explicit synthetic reason."}), request),
    ):
        with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_CONFLICT$"):
            save(engine, changed, request=new_request)
    with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REVISION_ALREADY_REJECTED$"):
        save(engine, intent(case), request=uuid4())
    assert database_snapshot(engine) == before


def test_rejecting_manual_override_binds_its_exact_source_origin(engine: Engine, case: Any) -> None:
    followup = next_intent(case, "MANUAL", terms=case.terms, manual_override=True)
    with unit_of_work(engine, write=True) as con:
        saved = event_writer.save(con, OWNER, followup, at=datetime.now(UTC), request_id=uuid4())
        assert saved.manual_override
    value, request = intent(followup), uuid4()
    result = save(engine, value, request=request)
    before = database_snapshot(engine)
    assert result.revision_id == followup.revision_id
    assert save(engine, value, request=request) == result
    assert database_snapshot(engine) == before


def test_late_duplicate_audit_invalidates_historical_replay(engine: Engine, case: Any) -> None:
    value, request = intent(case), uuid4()
    save(engine, value, request=request)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_event_rejection",
            str(value.id),
            "DIVIDEND_EVENT_REVISION_REJECTED",
            request_id=str(uuid4()),
            before={"synthetic": "late duplicate"},
            after={"synthetic": "late duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"):
        save(engine, value, request=request)
    assert database_snapshot(engine) == before


def test_rejection_replay_survives_later_candidate_dismissal(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    event = candidate_intent(candidate)
    submit(engine, event)
    value, request = intent(event), uuid4()
    result = save(engine, value, request=request)
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert save(engine, value, request=request) == result
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("id", "not-a-uuid"),
        ("event_id", "not-a-uuid"),
        ("revision_id", []),
        ("revision_no", True),
        ("revision_no", 0),
        ("citation", " "),
        ("source_url", "https://user:password@example.test/notice"),
        ("reason", "short"),
    ],
)
def test_mutated_or_invalid_intents_never_write(
    engine: Engine, case: Any, field: str, bad: Any
) -> None:
    before = database_snapshot(engine)
    with pytest.raises(ValidationError):
        intent(case, **{field: bad})
    mutated = intent(case).model_copy(update={field: bad})
    with pytest.raises(rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"):
        save(engine, mutated)
    assert database_snapshot(engine) == before


def test_writer_requires_an_active_owner_a_real_writer_and_exact_event_pins(
    engine: Engine, case: Any
) -> None:
    value = intent(case)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rejection.RejectionError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        writer.save(con, OWNER, value, request_id=uuid4())
    for owner, expected in (
        (OTHER, "DIVIDEND_REJECTION_NOT_FOUND"),
        (uuid4(), "DIVIDEND_REJECTION_OWNER_UNAVAILABLE"),
    ):
        with pytest.raises(rejection.RejectionError, match=f"^{expected}$"):
            save(engine, value, owner=owner)
    for changed in (
        value.model_copy(update={"revision_id": uuid4()}),
        value.model_copy(update={"revision_no": 2}),
    ):
        with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_VERSION_CONFLICT$"):
            save(engine, changed)
    assert database_snapshot(engine) == before


def test_old_event_pin_cannot_reject_a_newer_revision(engine: Engine, case: Any) -> None:
    value = intent(case)
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con, OWNER, next_intent(case, "VERIFY"), at=datetime.now(UTC), request_id=uuid4()
        )
    before = database_snapshot(engine)
    with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_VERSION_CONFLICT$"):
        save(engine, value)
    assert database_snapshot(engine) == before


def test_explicit_rejection_does_not_require_a_dismissed_candidate_to_be_current(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    event = candidate_intent(candidate)
    submit(engine, event)
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=1))
    result = save(engine, intent(event))
    assert result.decision == "REJECT_REVISION" and result.revision_id == event.revision_id
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"),
    ):
        rejection.require_clear(
            con, OWNER, event.event_id, revision_id=event.revision_id, version=1
        )


def test_invalid_internal_arguments_fail_with_fixed_codes(engine: Engine, case: Any) -> None:
    before = database_snapshot(engine)
    invalid_inputs: list[tuple[Any, Any]] = [(None, uuid4()), (intent(case), "not-a-uuid")]
    invalid_owner: Any = "not-a-uuid"
    with unit_of_work(engine, write=True) as con:
        for invalid_intent, request in invalid_inputs:
            with pytest.raises(
                rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"
            ):
                writer.save(con, OWNER, invalid_intent, request_id=request)
        with pytest.raises(rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"):
            writer.save(con, invalid_owner, intent(case), request_id=uuid4())
    assert database_snapshot(engine) == before


def test_existing_rejection_id_cannot_be_reused_for_another_event(
    engine: Engine, case: Any
) -> None:
    other = manual()
    submit(engine, other)
    first = intent(case)
    save(engine, first)
    before = database_snapshot(engine)
    with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_CONFLICT$"):
        save(engine, intent(other, id=first.id))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", [None, datetime(2026, 1, 1)])
def test_invalid_clock_fails_closed(engine: Engine, case: Any, invalid: Any) -> None:
    before = database_snapshot(engine)
    with (
        patch.object(writer, "now", return_value=invalid),
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, intent(case))
    assert database_snapshot(engine) == before


def test_clock_regression_does_not_record_a_rejection(engine: Engine, case: Any) -> None:
    before = database_snapshot(engine)
    with (
        patch.object(writer, "now", return_value=datetime(2000, 1, 1, tzinfo=UTC)),
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_TIME_CONFLICT$"),
    ):
        save(engine, intent(case))
    assert database_snapshot(engine) == before


def test_disappearing_event_cannot_be_recorded(engine: Engine, case: Any) -> None:
    before = database_snapshot(engine)
    with (
        patch.object(writer, "history", return_value=()),
        patch.object(events, "get", return_value=None),
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_NOT_FOUND$"),
    ):
        save(engine, intent(case))
    assert database_snapshot(engine) == before


def test_missing_source_audit_fails_without_writes(engine: Engine, case: Any) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def missing(*args: Any, **kwargs: Any) -> Any:
            statement = str(args[0])
            if (
                "SELECT created_at FROM audit_events" in statement
                and "DIVIDEND_EVENT_REVISED" in statement
            ):
                result = MagicMock()
                result.all.return_value = []
                return result
            return execute(*args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=missing),
            pytest.raises(
                rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
            ),
        ):
            writer.save(con, OWNER, intent(case), request_id=uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_duplicated_new_audit_rolls_back_before_rejection_insert(
    engine: Engine, case: Any, duplicate: bool
) -> None:
    before = database_snapshot(engine)

    def wrong_audit(*args: Any, **kwargs: Any) -> None:
        if duplicate:
            audit(*args, **kwargs)
            audit(*args, **kwargs)

    with (
        patch.object(writer, "audit", side_effect=wrong_audit),
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, intent(case))
    assert database_snapshot(engine) == before


def test_invalid_source_without_ex_date_rolls_back_written_audit_and_rejection(
    engine: Engine, case: Any
) -> None:
    with unit_of_work(engine) as con:
        reviewed = events.get(con, OWNER, case.event_id)
        assert reviewed is not None
    invalid_terms = reviewed.record.terms.model_copy(update={"ex_date": None})
    invalid = replace(reviewed, record=reviewed.record.model_copy(update={"terms": invalid_terms}))
    before = database_snapshot(engine)
    with (
        patch.object(writer, "history", return_value=()),
        patch.object(events, "get", return_value=invalid),
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        save(engine, intent(case))
    assert database_snapshot(engine) == before


def test_orphan_rejection_audit_cannot_be_adopted_or_replaced(engine: Engine, case: Any) -> None:
    value = intent(case)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_event_rejection",
            str(value.id),
            "DIVIDEND_EVENT_REVISION_REJECTED",
            request_id=str(uuid4()),
            before={"synthetic": "not bound"},
            after={"synthetic": "not bound"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"):
        save(engine, value)
    assert database_snapshot(engine) == before


def test_forged_existing_rejection_is_not_replayed_or_replaced(engine: Engine, case: Any) -> None:
    with unit_of_work(engine, write=True) as con:
        store_rejection(con, case.event_id, corrupt_after=True)
    before = database_snapshot(engine)
    with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"):
        save(engine, intent(case))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["audit", "invalidation", "post_read"])
def test_caught_writer_failure_rolls_back_decision_audit_and_invalidations(
    engine: Engine, case: Any, failure: str
) -> None:
    affected_portfolio(engine, case)
    before = database_snapshot(engine)
    original_history = rejection.history
    calls = 0

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        audit(*args, **kwargs)
        raise SQLAlchemyError("synthetic private audit failure")

    def fail_invalidation(*args: Any, **kwargs: Any) -> None:
        invalidate_portfolio(*args, **kwargs)
        raise ValueError("synthetic private invalidation failure")

    def fail_post_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return None if calls > 1 else original_history(*args, **kwargs)

    target, name, effect = {
        "audit": (writer, "audit", fail_audit),
        "invalidation": (writer, "invalidate_portfolio", fail_invalidation),
        "post_read": (writer, "history", fail_post_read),
    }[failure]
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(target, name, side_effect=effect),
        pytest.raises(
            rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
        ) as captured,
    ):
        writer.save(con, OWNER, intent(case), request_id=uuid4())
    assert "synthetic private" not in str(captured.value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["begin", "release"])
def test_uncertain_savepoint_aborts_outer_work(engine: Engine, case: Any, phase: str) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "synthetic_outer_work",
            str(uuid4()),
            "SYNTHETIC_OUTER_WORK",
            request_id=str(uuid4()),
            after={"synthetic": True},
            reason=REASON,
        )
        target = con if phase == "begin" else engine.dialect
        method = "begin_nested" if phase == "begin" else "do_release_savepoint"
        with (
            patch.object(target, method, side_effect=SQLAlchemyError("synthetic private failure")),
            pytest.raises(
                rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
            ),
        ):
            writer.save(con, OWNER, intent(case), request_id=uuid4())
        assert not con.in_transaction()
    assert database_snapshot(engine) == before


def test_outer_rollback_removes_successful_rejection(engine: Engine, case: Any) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        unit_of_work(engine, write=True) as con,
    ):
        writer.save(con, OWNER, intent(case), request_id=uuid4())
        raise RuntimeError("synthetic rollback")
    assert database_snapshot(engine) == before


def test_concurrent_identical_internal_retries_are_zero_write_after_first_commit(
    engine: Engine, case: Any
) -> None:
    value, request = intent(case), uuid4()
    before = database_snapshot(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(engine, value, request=request), range(2)))
    assert results[0] == results[1]
    after = database_snapshot(engine)
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1


@pytest.mark.parametrize("winner", ["rejection", "revision"])
def test_serialized_competitor_rechecks_rejection_and_event_scope(
    engine: Engine, case: Any, winner: str
) -> None:
    affected_portfolio(engine, case)
    before = database_snapshot(engine)
    started = Event()
    value, request = intent(case), uuid4()

    def compete() -> str:
        started.set()
        try:
            if winner == "rejection":
                with unit_of_work(engine, write=True) as con:
                    event_writer.save(
                        con,
                        OWNER,
                        next_intent(case, "VERIFY"),
                        at=datetime.now(UTC) + timedelta(seconds=1),
                        request_id=uuid4(),
                    )
            else:
                save(engine, value, request=request)
        except IntegrityError:
            return "revision_blocked"
        except rejection.RejectionError as error:
            return str(error)
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_of_work(engine, write=True) as con:
            if winner == "rejection":
                writer.save(con, OWNER, value, request_id=request)
            else:
                event_writer.save(
                    con,
                    OWNER,
                    next_intent(case, "VERIFY"),
                    at=datetime.now(UTC),
                    request_id=uuid4(),
                )
            competing = pool.submit(compete)
            assert started.wait(timeout=5)
        expected = (
            "revision_blocked" if winner == "rejection" else "DIVIDEND_REJECTION_VERSION_CONFLICT"
        )
        assert competing.result(timeout=10) == expected
    after = database_snapshot(engine)
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + (
        1 if winner == "rejection" else 0
    )
    assert len(after["dividend_event_revisions"]) == len(before["dividend_event_revisions"]) + (
        1 if winner == "revision" else 0
    )


def test_rejection_and_event_revision_race_commits_only_one_decision(
    engine: Engine, case: Any
) -> None:
    affected_portfolio(engine, case)
    before = database_snapshot(engine)
    barrier = Barrier(2)
    value, request = intent(case), uuid4()

    def reject() -> str:
        barrier.wait()
        try:
            save(engine, value, request=request)
            return "rejected"
        except rejection.RejectionError as error:
            return str(error)

    def revise() -> str:
        barrier.wait()
        try:
            with unit_of_work(engine, write=True) as con:
                event_writer.save(
                    con,
                    OWNER,
                    next_intent(case, "VERIFY"),
                    at=datetime.now(UTC) + timedelta(seconds=1),
                    request_id=uuid4(),
                )
            return "revised"
        except IntegrityError:
            return "revision_blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(reject), pool.submit(revise)
        outcomes = {first.result(), second.result()}
    assert outcomes in (
        {"rejected", "revision_blocked"},
        {"DIVIDEND_REJECTION_VERSION_CONFLICT", "revised"},
    )
    after = database_snapshot(engine)
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    assert len(after["projection_invalidations"]) == len(before["projection_invalidations"]) + 1
    assert len(after["dividend_event_revisions"]) == len(before["dividend_event_revisions"]) + (
        1 if "revised" in outcomes else 0
    )
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + (
        1 if "rejected" in outcomes else 0
    )


def test_concurrent_distinct_rejections_never_double_append(engine: Engine, case: Any) -> None:
    values = [intent(case), intent(case)]

    def attempt(value: writer.RejectionIntent) -> str:
        try:
            save(engine, value)
            return "saved"
        except rejection.RejectionError as error:
            return str(error)

    before = database_snapshot(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, values))
    assert sorted(results) == ["DIVIDEND_REVISION_ALREADY_REJECTED", "saved"]
    after = database_snapshot(engine)
    assert len(after["dividend_event_rejections"]) == len(before["dividend_event_rejections"]) + 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
