"""Synthetic checks for current event-source revalidation inside one writer."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.test_action_opening_migration import action
from tests.test_dividend_basis_migration import SECURITY
from tests.test_dividend_candidate_service import basis as basis
from tests.test_dividend_candidate_service import decision as candidate_decision
from tests.test_dividend_candidate_service import save as save_candidate
from tests.test_dividend_event_migration import OTHER, OWNER, publish, staged
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import (
    AT,
    candidate_intent,
    manual,
    next_intent,
    submit,
    terms,
)


@pytest.fixture(autouse=True)
def private_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def current(
    engine: Engine,
    event_id: UUID,
    revision_id: UUID,
    version: int,
    *,
    owner: UUID = OWNER,
) -> events.ReviewedEvent:
    with unit_of_work(engine, write=True) as con:
        return writer.require_current_source(
            con, owner, event_id, revision_id=revision_id, version=version
        )


@pytest.mark.parametrize("kind", ["MANUAL", "INCOMPLETE", "VERIFY", "WITHDRAW"])
def test_current_manual_decisions_remain_explicit_and_read_only(
    engine: Engine, source: dict[str, Any], kind: str
) -> None:
    intent = manual(
        terms=terms(adjustment_basis="UNKNOWN" if kind == "INCOMPLETE" else "AS_DECLARED")
    )
    result = submit(engine, intent)
    selected = intent
    if kind in {"VERIFY", "WITHDRAW"}:
        selected = next_intent(intent, kind)
        result = submit(engine, selected, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert current(engine, selected.event_id, selected.revision_id, selected.revision_no) == result
    assert database_snapshot(engine) == before


def test_current_candidate_revalidates_without_provider_or_private_configuration(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    result = submit(engine, intent)
    before = database_snapshot(engine)
    with (
        patch.object(
            ProviderConfigurationStore, "load", side_effect=AssertionError("private file")
        ),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider HTTP")),
    ):
        assert current(engine, intent.event_id, intent.revision_id, 1) == result
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["OPEN", "DISMISSED"])
def test_later_candidate_decision_fences_new_use_but_not_history(
    engine: Engine, basis: SplitBasisAssertion, state: str
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    original = submit(engine, intent)
    later = candidate.model_copy(
        update={
            "id": uuid4(),
            "revision_no": 2,
            "state": state,
            "matches": () if state == "DISMISSED" else candidate.matches,
        }
    )
    save_candidate(engine, later, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, intent.event_id) == original
    with pytest.raises(events.EventError, match="^CANDIDATE_VERSION_CONFLICT$"):
        current(engine, intent.event_id, intent.revision_id, 1)
    assert database_snapshot(engine) == before


def test_changed_action_basis_fences_current_candidate_but_retains_event_history(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    original = submit(engine, intent)
    reference = basis.splits[0]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?",
            (str(reference.id),),
        )
        action(
            con,
            str(uuid4()),
            root_id=str(reference.root_id),
            supersedes_id=str(reference.id),
            security_id=str(SECURITY),
            revision=2,
            effective_date=reference.effective_date,
            ratio_num=2,
            ratio_den=1,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, intent.event_id) == original
    with pytest.raises(events.EventError, match="^CANDIDATE_BASIS_CHANGED$"):
        current(engine, intent.event_id, intent.revision_id, 1)
    assert database_snapshot(engine) == before


def test_explicit_manual_verification_is_not_silently_replaced_by_old_candidate_state(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    submit(engine, intent)
    verify = next_intent(intent, "VERIFY")
    verified = submit(engine, verify, at=AT + timedelta(seconds=1))
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=2))
    before = database_snapshot(engine)
    assert current(engine, verify.event_id, verify.revision_id, 2) == verified
    assert verified.record.review_status == "VERIFIED" and verified.record.manual_locked
    assert database_snapshot(engine) == before


def test_exact_event_revision_pins_and_owner_scope_are_required(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    original = submit(engine, first)
    second = next_intent(first, "VERIFY")
    selected = submit(engine, second, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert current(engine, second.event_id, second.revision_id, 2) == selected
    for revision_id, version in (
        (first.revision_id, 1),
        (uuid4(), 2),
        (second.revision_id, 1),
        (first.revision_id, 2),
    ):
        with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
            current(engine, first.event_id, revision_id, version)
    for owner, event_id in ((OTHER, first.event_id), (OWNER, uuid4())):
        with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_NOT_FOUND$"):
            current(engine, event_id, second.revision_id, 2, owner=owner)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, first.event_id, version=1) == original
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("version", [None, True, False, 0, -1, 2_147_483_648, 1.0, "1"])
def test_invalid_version_pin_is_never_coerced(
    engine: Engine, source: dict[str, Any], version: Any
) -> None:
    intent = manual()
    submit(engine, intent)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^INVALID_DIVIDEND_EVENT_SCOPE$"):
        current(engine, intent.event_id, intent.revision_id, version)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["owner", "event", "revision"])
def test_invalid_identifier_types_do_not_leak_or_write(
    engine: Engine, source: dict[str, Any], field: str
) -> None:
    intent = manual()
    submit(engine, intent)
    args: dict[str, Any] = {
        "owner_id": OWNER,
        "event_id": intent.event_id,
        "revision_id": intent.revision_id,
    }
    args[{"owner": "owner_id", "event": "event_id", "revision": "revision_id"}[field]] = (
        "private-invalid"
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^INVALID_DIVIDEND_EVENT_SCOPE$") as error,
    ):
        writer.require_current_source(con, version=1, **args)
    assert "private-invalid" not in str(error.value)
    assert database_snapshot(engine) == before


def test_active_owner_and_caller_owned_writer_transaction_are_required(
    engine: Engine, source: dict[str, Any]
) -> None:
    intent = manual()
    submit(engine, intent)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        writer.require_current_source(
            con, OWNER, intent.event_id, revision_id=intent.revision_id, version=1
        )
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_OWNER_UNAVAILABLE$"):
        current(engine, intent.event_id, intent.revision_id, 1, owner=uuid4())
    assert database_snapshot(engine) == before


def test_withdrawal_stays_explicit_when_its_historical_candidate_is_dismissed(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    submit(engine, intent)
    withdrawal = next_intent(intent, "WITHDRAW")
    closed = submit(engine, withdrawal, at=AT + timedelta(seconds=1))
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=2))
    before = database_snapshot(engine)
    assert current(engine, withdrawal.event_id, withdrawal.revision_id, 2) == closed
    assert closed.record.review_status == "WITHDRAWN"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["CONFLICT", "INCOMPLETE"])
def test_unresolved_candidate_status_remains_visible_not_an_economic_grant(
    engine: Engine, basis: SplitBasisAssertion, state: str
) -> None:
    candidate = candidate_decision(basis)
    candidate = candidate.model_copy(
        update={"terms": terms(dps="1.10000000")} if state == "CONFLICT" else {"matches": ()}
    )
    review = save_candidate(engine, candidate)
    assert review.assessment.status == state
    intent = candidate_intent(candidate)
    original = submit(engine, intent)
    before = database_snapshot(engine)
    assert current(engine, intent.event_id, intent.revision_id, 1) == original
    assert original.record.review_status == state
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("mismatch", ["identity", "terms", "assessment"])
def test_a_different_current_candidate_or_assessment_never_becomes_the_event(
    engine: Engine, basis: SplitBasisAssertion, mismatch: str
) -> None:
    candidate = candidate_decision(basis)
    reviewed = save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    submit(engine, intent)
    if mismatch == "assessment":
        altered = replace(reviewed, assessment=replace(reviewed.assessment, status="CORROBORATED"))
    else:
        other = candidate.model_copy(
            update={"id": uuid4()} if mismatch == "identity" else {"terms": terms(dps="1.10000000")}
        )
        altered = replace(reviewed, record=reviewed.record.model_copy(update={"decision": other}))
    before = database_snapshot(engine)
    with (
        patch.object(candidates, "require_current", return_value=altered),
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_SOURCE_CHANGED$"),
    ):
        current(engine, intent.event_id, intent.revision_id, 1)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("malformed", ["missing", "identity", "version"])
def test_unreadable_candidate_scope_fails_without_private_data(
    engine: Engine, basis: SplitBasisAssertion, malformed: str
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    reviewed = submit(engine, intent)
    row = (
        None
        if malformed == "missing"
        else SimpleNamespace(
            candidate_id="private-invalid"
            if malformed == "identity"
            else str(candidate.candidate_id),
            revision_no=True if malformed == "version" else candidate.revision_no,
        )
    )
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def injected(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "SELECT candidate_id,revision_no FROM dividend_candidate_revisions" in str(
                statement
            ):
                return SimpleNamespace(first=lambda: row)
            return execute(statement, *args, **kwargs)

        with (
            patch.object(events, "get", return_value=reviewed),
            patch.object(con, "execute", side_effect=injected),
            pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$") as error,
        ):
            writer.require_current_source(
                con, OWNER, intent.event_id, revision_id=intent.revision_id, version=1
            )
    assert "private-invalid" not in str(error.value)
    assert database_snapshot(engine) == before


def test_well_shaped_but_falsely_hashed_new_event_never_authorizes_current_source(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, revision, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, revision)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"):
        current(engine, UUID(parent["id"]), UUID(revision["id"]), 1)
    assert database_snapshot(engine) == before


def test_inactive_owner_cannot_turn_historical_read_into_current_source(
    engine: Engine, source: dict[str, Any]
) -> None:
    intent = manual()
    original = submit(engine, intent)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, intent.event_id) == original
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_OWNER_UNAVAILABLE$"):
        current(engine, intent.event_id, intent.revision_id, 1)
    assert database_snapshot(engine) == before


def test_caller_changes_in_the_same_transaction_require_fresh_revalidation(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    original = submit(engine, intent)
    later = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    with unit_of_work(engine, write=True) as con:
        assert (
            writer.require_current_source(
                con, OWNER, intent.event_id, revision_id=intent.revision_id, version=1
            )
            == original
        )
        candidates.save(con, OWNER, later, at=AT + timedelta(seconds=1), request_id=uuid4())
        with pytest.raises(events.EventError, match="^CANDIDATE_VERSION_CONFLICT$"):
            writer.require_current_source(
                con, OWNER, intent.event_id, revision_id=intent.revision_id, version=1
            )
        assert events.get(con, OWNER, intent.event_id) == original


def test_stale_read_cannot_be_relabelled_as_current_writer(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    original = submit(engine, first)
    second = next_intent(first, "VERIFY")
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, first.event_id) == original
        with pytest.raises(RuntimeError, match="^TRANSACTION_MODE_FROZEN$"):
            con.execution_options(writer=True)
        selected = submit(engine, second, at=AT + timedelta(seconds=1))
        assert events.get(con, OWNER, first.event_id) == original
        with pytest.raises(events.EventError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"):
            writer.require_current_source(
                con, OWNER, first.event_id, revision_id=first.revision_id, version=1
            )
    assert current(engine, second.event_id, second.revision_id, 2) == selected
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        current(engine, first.event_id, first.revision_id, 1)


def test_current_source_remains_consistent_until_the_writer_transaction_ends(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    original = submit(engine, first)
    second = next_intent(first, "VERIFY")
    started, acquired = Event(), Event()

    def publish_later() -> events.ReviewedEvent:
        started.set()
        with unit_of_work(engine, write=True) as con:
            acquired.set()
            return writer.save(con, OWNER, second, at=AT + timedelta(seconds=1), request_id=uuid4())

    with ThreadPoolExecutor(max_workers=1) as executor:
        with unit_of_work(engine, write=True) as con:
            assert (
                writer.require_current_source(
                    con, OWNER, first.event_id, revision_id=first.revision_id, version=1
                )
                == original
            )
            pending = executor.submit(publish_later)
            assert started.wait(2)
            assert not acquired.wait(0.2)
            assert events.get(con, OWNER, first.event_id) == original
        selected = pending.result(timeout=10)
        assert acquired.is_set()
    assert current(engine, second.event_id, second.revision_id, 2) == selected
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        current(engine, first.event_id, first.revision_id, 1)
