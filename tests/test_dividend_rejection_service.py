"""Audited rejection history is not authentication or an economic grant."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_service as rejection
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion
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


@pytest.fixture
def case(engine: Engine, source: dict[str, Any]) -> Any:
    event = manual()
    submit(engine, event)
    return event


def store_rejection(
    con: Connection,
    event_id: UUID,
    *,
    citation: str = "Synthetic owner reference",
    source_url: str | None = "https://example.test/dividend?synthetic=private",
    corrupt_after: bool = False,
    omit_override: bool = False,
) -> dict[str, Any]:
    reviewed = events.get(con, OWNER, event_id)
    assert reviewed is not None
    record = reviewed.record
    identifier, request = uuid4(), uuid4()
    created = datetime.now(UTC) + timedelta(seconds=1)
    reference = {"citation": citation, "source_url": source_url}
    payload = {
        "rejection_id": str(identifier),
        "decision": "REJECT_REVISION",
        "owner_id": str(OWNER),
        "event_id": str(event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "reference_sha256": digest(canonical(reference)),
        "reason_sha256": digest(canonical(REASON)),
        "created_at": stamp(created),
        "scope": "OWNER_REVISION_REJECTION_ONLY",
    }
    before = events._audit_values(record)
    if reviewed.manual_override and not omit_override:
        before = {**before, "manual_override": True}
    with patch("spm_api.audit.now", return_value=created):
        audit(
            con,
            str(OWNER),
            "dividend_event_rejection",
            str(identifier),
            "DIVIDEND_EVENT_REVISION_REJECTED",
            request_id=str(request),
            before=before,
            after={"synthetic": "incorrect fingerprint"} if corrupt_after else payload,
            reason=REASON,
        )
    audit_id = con.exec_driver_sql(
        "SELECT id FROM audit_events WHERE entity_type='dividend_event_rejection' AND entity_id=?",
        (str(identifier),),
    ).scalar_one()
    values = {
        "id": str(identifier),
        "owner_id": str(OWNER),
        "event_id": str(event_id),
        "revision_id": str(record.revision_id),
        "revision_no": record.revision_no,
        "citation": citation,
        "source_url": source_url,
        "reason": REASON,
        "request_id": str(request),
        "audit_id": audit_id,
        "created_at": stamp(created),
    }
    insert(con, "dividend_event_rejections", values)
    return values


def current(con: Connection, event: Any, owner: UUID = OWNER) -> None:
    rejection.require_clear(
        con,
        owner,
        event.event_id,
        revision_id=event.revision_id,
        version=event.revision_no,
    )


def test_empty_owned_history_and_current_check_do_not_grant_eligibility(
    engine: Engine, case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("History reads must not access providers")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert rejection.history(con, OWNER, case.event_id) == ()
        assert rejection.history(con, OTHER, case.event_id) is None
        assert rejection.history(con, OWNER, uuid4()) is None
    with unit_of_work(engine, write=True) as con:
        current(con, case)
    assert database_snapshot(engine) == before


def test_rejection_history_validates_unredacted_reference_and_preserves_financial_facts(
    engine: Engine, case: Any
) -> None:
    _, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        claim = entitlement({"id": str(case.revision_id), "currency": "USD"}, account)
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
        expected = store_rejection(con, case.event_id)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        retained = rejection.history(con, OWNER, case.event_id)
    assert retained is not None and len(retained) == 1
    item = retained[0]
    assert str(item.id) == expected["id"] and item.revision_id == case.revision_id
    assert item.scope == "OWNER_REVISION_REJECTION_ONLY"
    assert item.citation == expected["citation"] and item.source_url == expected["source_url"]
    assert expected["citation"] not in repr(item) and expected["source_url"] not in repr(item)
    assert REASON not in repr(item)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"),
    ):
        current(con, case)
    assert database_snapshot(engine) == before


def test_rejection_can_cite_an_override_without_erasing_its_origin(
    engine: Engine, case: Any
) -> None:
    followup = next_intent(case, "MANUAL", terms=case.terms, manual_override=True)
    with unit_of_work(engine, write=True) as con:
        saved = event_writer.save(con, OWNER, followup, at=datetime.now(UTC), request_id=uuid4())
        assert saved.manual_override
        expected = store_rejection(con, case.event_id)
    with unit_of_work(engine) as con:
        retained = rejection.history(con, OWNER, case.event_id)
        assert retained is not None and str(retained[0].id) == expected["id"]
        assert retained[0].revision_id == followup.revision_id


@pytest.mark.parametrize("failure", ["wrong_hash", "invalid_url"])
def test_sql_shape_does_not_establish_valid_rejection_history(
    engine: Engine, case: Any, failure: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        store_rejection(
            con,
            case.event_id,
            corrupt_after=failure == "wrong_hash",
            source_url=(
                "https://user:password@example.test/private"
                if failure == "invalid_url"
                else "https://example.test/notice"
            ),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        rejection.history(con, OWNER, case.event_id)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        current(con, case)
    assert database_snapshot(engine) == before


def test_rejection_audit_cannot_hide_an_override_source_label(engine: Engine, case: Any) -> None:
    followup = next_intent(case, "MANUAL", terms=case.terms, manual_override=True)
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, OWNER, followup, at=datetime.now(UTC), request_id=uuid4())
        store_rejection(con, case.event_id, omit_override=True)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        rejection.history(con, OWNER, case.event_id)


@pytest.mark.parametrize("target", ["rejection", "source"])
def test_later_duplicate_audit_fails_the_complete_history_closed(
    engine: Engine, case: Any, target: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        saved = store_rejection(con, case.event_id)
        audit(
            con,
            str(OWNER),
            "dividend_event_rejection" if target == "rejection" else "dividend_event_revision",
            saved["id"] if target == "rejection" else str(case.revision_id),
            "DIVIDEND_EVENT_REVISION_REJECTED"
            if target == "rejection"
            else "DIVIDEND_EVENT_REVISED",
            request_id=str(uuid4()),
            after={"synthetic": "late duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"),
    ):
        rejection.history(con, OWNER, case.event_id)
    assert database_snapshot(engine) == before


def test_current_check_requires_active_writer_and_exact_physical_scope(
    engine: Engine, case: Any
) -> None:
    with (
        unit_of_work(engine) as con,
        pytest.raises(rejection.RejectionError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        current(con, case)
    with unit_of_work(engine, write=True) as con:
        for identifier, number in ((uuid4(), 1), (case.revision_id, 2)):
            with pytest.raises(
                rejection.RejectionError, match="^DIVIDEND_REJECTION_VERSION_CONFLICT$"
            ):
                rejection.require_clear(
                    con, OWNER, case.event_id, revision_id=identifier, version=number
                )
        with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_NOT_FOUND$"):
            current(con, case, owner=OTHER)
        with pytest.raises(
            rejection.RejectionError, match="^DIVIDEND_REJECTION_OWNER_UNAVAILABLE$"
        ):
            current(con, case, owner=uuid4())


def test_clear_rejection_scope_does_not_revalidate_a_dismissed_candidate(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    intent = candidate_intent(candidate)
    submit(engine, intent)
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        current(con, intent)
        with pytest.raises(events.EventError, match="^CANDIDATE_VERSION_CONFLICT$"):
            event_writer.require_current_source(
                con, OWNER, intent.event_id, revision_id=intent.revision_id, version=1
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("invalid", [None, "not-a-uuid", []])
def test_history_and_current_checks_reject_invalid_scope(
    engine: Engine, case: Any, invalid: Any
) -> None:
    with (
        engine.connect() as con,
        pytest.raises(rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"),
    ):
        rejection.history(con, OWNER, case.event_id)
    with unit_of_work(engine, write=True) as con:
        for owner, identifier in ((invalid, case.event_id), (OWNER, invalid)):
            with pytest.raises(
                rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"
            ):
                rejection.history(con, owner, identifier)
        with pytest.raises(rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"):
            rejection.require_clear(
                con, invalid, case.event_id, revision_id=case.revision_id, version=1
            )
        with pytest.raises(rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"):
            rejection.require_clear(con, OWNER, case.event_id, revision_id=invalid, version=1)


@pytest.mark.parametrize("failure", ["owner_query", "event_history", "disappearing_history"])
def test_current_check_sanitizes_database_and_history_errors(
    engine: Engine, case: Any, failure: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        target: Any
        if failure == "owner_query":
            target, name = con, "execute"
            effect: Any = SQLAlchemyError("synthetic private failure")
        elif failure == "event_history":
            target, name = events, "get"
            effect = events.EventError("synthetic private failure")
        else:
            target, name = rejection, "history"
            effect = None
        with (
            patch.object(target, name, side_effect=effect, return_value=None),
            pytest.raises(
                rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
            ) as captured,
        ):
            current(con, case)
        assert "synthetic private failure" not in str(captured.value)


def test_multiple_rejection_rows_fail_closed_without_repair(engine: Engine, case: Any) -> None:
    with unit_of_work(engine) as con:
        execute = con.execute

        def duplicate(*args: Any, **kwargs: Any) -> Any:
            if "SELECT rejected.*" in str(args[0]):
                result = MagicMock()
                result.mappings.return_value.all.return_value = [{}, {}]
                return result
            return execute(*args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=duplicate),
            pytest.raises(
                rejection.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
            ),
        ):
            rejection.history(con, OWNER, case.event_id)


@pytest.mark.parametrize("invalid", [True, 0, -1, 2_147_483_648, "1", None])
def test_current_check_rejects_invalid_revision_versions(
    engine: Engine, case: Any, invalid: Any
) -> None:
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejection.RejectionError, match="^INVALID_DIVIDEND_REJECTION_SCOPE$"),
    ):
        rejection.require_clear(
            con, OWNER, case.event_id, revision_id=case.revision_id, version=invalid
        )


def test_later_revision_invalidates_the_observed_physical_pin(engine: Engine, case: Any) -> None:
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con, OWNER, next_intent(case, "VERIFY"), at=datetime.now(UTC), request_id=uuid4()
        )
        with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_VERSION_CONFLICT$"):
            current(con, case)


def test_rechecking_after_a_rejection_in_the_same_writer_changes_the_result(
    engine: Engine, case: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        current(con, case)
        store_rejection(con, case.event_id)
        with pytest.raises(rejection.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"):
            current(con, case)


def test_wal_historical_reader_cannot_be_promoted_to_a_current_writer(
    engine: Engine, case: Any
) -> None:
    with unit_of_work(engine) as reader:
        assert rejection.history(reader, OWNER, case.event_id) == ()
        with unit_of_work(engine, write=True) as writer:
            store_rejection(writer, case.event_id)
        assert rejection.history(reader, OWNER, case.event_id) == ()
        with pytest.raises(rejection.RejectionError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"):
            current(reader, case)
    with (
        unit_of_work(engine, write=True) as writer,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REVISION_REJECTED$"),
    ):
        current(writer, case)


def test_inactive_owner_retains_history_but_cannot_use_the_writer_fence(
    engine: Engine, case: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        store_rejection(con, case.event_id)
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    with unit_of_work(engine) as con:
        assert rejection.history(con, OWNER, case.event_id) is not None
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rejection.RejectionError, match="^DIVIDEND_REJECTION_OWNER_UNAVAILABLE$"),
    ):
        current(con, case)
