"""Retained dividend evidence reviews remain scoped and unverified."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
import spm_api.dividend_evidence_review_history as reviews
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion
from spm_api.dividend_event_writer import EventIntent
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_basis_migration import SECURITY
from tests.test_dividend_candidate_service import basis as basis
from tests.test_dividend_event_migration import OTHER, OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import (
    AT,
    candidate_intent,
    conflicting_candidate,
    manual,
    next_intent,
    owned_account,
    submit,
    terms,
)


def incomplete(engine: Engine) -> EventIntent:
    intent = manual(terms=terms(adjustment_basis="UNKNOWN"))
    submit(engine, intent)
    return intent


def test_empty_history_is_not_confirmation_and_foreign_events_are_private(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = manual()
    submit(engine, intent)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None
        assert value.event_id == intent.event_id and value.revision_id == intent.revision_id
        assert value.items == () and value.history_completeness == "UNVERIFIED"
        assert value.eligibility_scope == "NOT_ESTABLISHED"
        assert reviews.history(con, OTHER, intent.event_id) is None
        assert reviews.history(con, OWNER, uuid4()) is None
    assert database_snapshot(engine) == before


def test_open_then_resolved_review_retains_both_event_revisions(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and len(value.items) == 1
        item = value.items[0]
        assert item.status == "OPEN" and item.reason_code == "DIVIDEND_INCOMPLETE"
        assert item.revision_id == intent.revision_id and item.revision_no == 1
        assert item.portfolio_id is None and item.account_id is None
        assert item.incomplete_reasons == ("UNRESOLVED_ADJUSTMENT",)
        assert item.resolved_by_revision_id is None
        assert "synthetic" not in repr(value).lower()
    assert database_snapshot(engine) == before
    successor = next_intent(intent, "MANUAL", terms=terms())
    submit(engine, successor, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and value.revision_id == successor.revision_id
        assert len(value.items) == 1
        assert value.items[0].status == "RESOLVED"
        assert value.items[0].revision_id == intent.revision_id
        assert value.items[0].resolved_by_revision_id == successor.revision_id
    assert database_snapshot(engine) == before


def test_conflict_reviews_cover_only_retained_owned_portfolio_rows(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    owned = [owned_account(engine), owned_account(engine)]
    foreign = owned_account(engine, OTHER)
    with unit_of_work(engine, write=True) as con:
        for portfolio, account in [*owned, foreign]:
            insert(
                con,
                "positions",
                {
                    "portfolio_id": portfolio,
                    "account_id": account,
                    "security_id": str(SECURITY),
                    "projection_version": 0,
                    "shares_e8": 100000000,
                    "remaining_cost_hkd_micro": 0,
                    "realized_hkd_micro": 0,
                    "calculated_at_utc": "2026-01-01T00:00:00.000000Z",
                },
            )
    intent = candidate_intent(conflicting_candidate(engine, basis))
    submit(engine, intent)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and len(value.items) == 2
        assert {str(item.portfolio_id) for item in value.items} == {row[0] for row in owned}
        assert all(
            item.status == "OPEN" and item.reason_code == "DIVIDEND_SOURCE_CONFLICT"
            for item in value.items
        )
        assert all("dps" in item.conflicting_fields for item in value.items)
        assert reviews.history(con, OTHER, intent.event_id) is None
    assert database_snapshot(engine) == before


def test_acknowledgement_is_explicitly_unverified_even_after_resolution(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE review_items SET status='ACKNOWLEDGED' WHERE event_id=?",
            (str(intent.event_id),),
        )
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and value.items[0].status == "ACKNOWLEDGED"
        assert value.items[0].acknowledgement_scope == "NOT_ESTABLISHED"
    successor = next_intent(intent, "MANUAL", terms=terms())
    submit(engine, successor, at=AT + timedelta(seconds=1))
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and value.items[0].status == "RESOLVED"
        assert value.items[0].acknowledgement_scope == "NOT_ESTABLISHED"


def test_incomplete_successor_closes_old_review_and_adds_a_new_one(
    engine: Engine, source: dict[str, object]
) -> None:
    first = incomplete(engine)
    successor = next_intent(first, "MANUAL", terms=terms(adjustment_basis="UNKNOWN"))
    submit(engine, successor, at=AT + timedelta(seconds=1))
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, first.event_id)
        assert value is not None and len(value.items) == 2
        by_revision = {item.revision_id: item for item in value.items}
        assert by_revision[first.revision_id].status == "RESOLVED"
        assert by_revision[first.revision_id].resolved_by_revision_id == successor.revision_id
        assert by_revision[successor.revision_id].status == "OPEN"


def test_missing_creation_audit_is_not_treated_as_empty_or_repaired(
    engine: Engine, source: dict[str, object]
) -> None:
    original = audit

    def omit_review(*args: Any, **kwargs: Any) -> None:
        if args[2] != "review_item":
            original(*args, **kwargs)

    with patch.object(writer, "audit", side_effect=omit_review):
        intent = incomplete(engine)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


def test_duplicate_creation_audit_fails_without_changing_old_audits(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    with unit_of_work(engine, write=True) as con:
        identifier = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=?", (str(intent.event_id),)
        ).scalar_one()
        audit(con, str(OWNER), "review_item", identifier, "DIVIDEND_EVIDENCE_REVIEW_CREATED")
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "damage", ["missing", "wrong-successor", "duplicate", "owner", "request", "reason", "before"]
)
def test_resolution_requires_exact_successor_audit_without_repair(
    engine: Engine, source: dict[str, object], damage: str
) -> None:
    intent = incomplete(engine)
    successor = next_intent(intent, "MANUAL", terms=terms())
    original = audit

    def altered(*args: Any, **kwargs: Any) -> None:
        if args[2] == "review_item" and args[4] == "DIVIDEND_EVIDENCE_REVIEW_RESOLVED":
            if damage == "missing":
                return
            arguments = list(args)
            if damage == "wrong-successor":
                kwargs["after"] = {"status": "RESOLVED", "revision_id": str(uuid4())}
            elif damage == "owner":
                arguments[1] = str(OTHER)
            elif damage == "request":
                kwargs["request_id"] = str(uuid4())
            elif damage == "reason":
                kwargs["reason"] = "Synthetic different audit reason"
            elif damage == "before":
                kwargs["before"] = {"status": "DISMISSED"}
            original(*arguments, **kwargs)
            if damage == "duplicate":
                original(*arguments, **kwargs)
            return
        original(*args, **kwargs)

    with patch.object(writer, "audit", side_effect=altered):
        submit(engine, successor, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


def test_foreign_portfolio_or_attached_account_fails_closed(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    foreign, foreign_account = owned_account(engine, OTHER)
    for column, value in (("portfolio_id", foreign), ("account_id", foreign_account)):
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                f"UPDATE review_items SET {column}=? WHERE event_id=?",
                (value, str(intent.event_id)),
            )
        before = database_snapshot(engine)
        with (
            unit_of_work(engine) as con,
            pytest.raises(
                reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"
            ),
        ):
            reviews.history(con, OWNER, intent.event_id)
        assert database_snapshot(engine) == before
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                f"UPDATE review_items SET {column}=NULL WHERE event_id=?", (str(intent.event_id),)
            )


def test_duplicate_retained_scope_is_rejected_even_if_rows_are_well_formed(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    with unit_of_work(engine, write=True) as con:
        row = dict(
            con.exec_driver_sql(
                "SELECT * FROM review_items WHERE event_id=?", (str(intent.event_id),)
            )
            .mappings()
            .one()
        )
        creation = dict(
            con.exec_driver_sql(
                "SELECT * FROM audit_events WHERE entity_type='review_item' AND entity_id=? "
                "AND action='DIVIDEND_EVIDENCE_REVIEW_CREATED'",
                (row["id"],),
            )
            .mappings()
            .one()
        )
        duplicate = str(uuid4())
        insert(con, "review_items", {**row, "id": duplicate})
        insert(con, "audit_events", {**creation, "id": str(uuid4()), "entity_id": duplicate})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("damage", ["oversize", "array", "unknown-revision", "invalid-id"])
def test_invalid_detail_or_identifier_is_private_and_unavailable(
    engine: Engine, source: dict[str, object], damage: str
) -> None:
    intent = incomplete(engine)
    with unit_of_work(engine, write=True) as con:
        if damage == "invalid-id":
            con.exec_driver_sql(
                "UPDATE review_items SET id='private-invalid-id' WHERE event_id=?",
                (str(intent.event_id),),
            )
        else:
            wire = {
                "oversize": '{"private":"' + "x" * 5000 + '"}',
                "array": "[]",
                "unknown-revision": '{"revision_id":"' + str(uuid4()) + '"}',
            }[damage]
            con.exec_driver_sql(
                "UPDATE review_items SET detail_json=? WHERE event_id=?",
                (wire, str(intent.event_id)),
            )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con, pytest.raises(reviews.EvidenceReviewError) as caught:
        reviews.history(con, OWNER, intent.event_id)
    assert str(caught.value) == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    assert "private-invalid-id" not in repr(caught.value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("damage", ["missing-revision", "swapped-revision"])
def test_inconsistent_historical_revision_reader_fails_closed(
    engine: Engine, source: dict[str, object], damage: str
) -> None:
    intent = incomplete(engine)
    successor = next_intent(intent, "MANUAL", terms=terms())
    submit(engine, successor, at=AT + timedelta(seconds=1))
    original = events.get

    def changed(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("version") == 1:
            return None if damage == "missing-revision" else original(*args)
        return original(*args, **kwargs)

    before = database_snapshot(engine)
    with (
        patch.object(events, "get", side_effect=changed),
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("damage", ["code", "fields", "field-type", "reasons", "reason-type"])
def test_forged_review_content_fails_closed(
    engine: Engine, source: dict[str, object], damage: str
) -> None:
    intent = incomplete(engine)
    content: dict[str, object] = {
        "scope": "EVENT_EVIDENCE_REVIEW",
        "revision_id": str(intent.revision_id),
        "conflicting_fields": [],
        "incomplete_reasons": ["UNRESOLVED_ADJUSTMENT"],
    }
    code = "DIVIDEND_INCOMPLETE"
    if damage == "code":
        code = "PRIVATE_WRONG_CODE"
    elif damage == "fields":
        content["conflicting_fields"] = "not-a-list"
    elif damage == "field-type":
        content["conflicting_fields"] = [1]
    elif damage == "reasons":
        content["incomplete_reasons"] = "not-a-list"
    else:
        content["incomplete_reasons"] = [1]
    before = database_snapshot(engine)
    with (
        patch.object(writer, "_review_content", return_value=(code, content)),
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("damage", ["owner", "request", "reason", "before", "after"])
def test_forged_creation_audit_fails_without_modifying_old_audits(
    engine: Engine, source: dict[str, object], damage: str
) -> None:
    original = audit

    def altered(*args: Any, **kwargs: Any) -> None:
        arguments = list(args)
        if args[2] == "review_item" and args[4] == "DIVIDEND_EVIDENCE_REVIEW_CREATED":
            if damage == "owner":
                arguments[1] = str(OTHER)
            elif damage == "request":
                kwargs["request_id"] = str(uuid4())
            elif damage == "reason":
                kwargs["reason"] = "Synthetic different audit reason"
            elif damage == "before":
                kwargs["before"] = {"status": "OPEN"}
            else:
                kwargs["after"] = {"scope": "forged synthetic creation"}
        original(*arguments, **kwargs)

    with patch.object(writer, "audit", side_effect=altered):
        intent = incomplete(engine)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


def test_review_read_uses_one_wal_snapshot_during_a_real_successor(
    engine: Engine, source: dict[str, object]
) -> None:
    intent = incomplete(engine)
    successor = next_intent(intent, "MANUAL", terms=terms())
    with unit_of_work(engine) as con:
        con.exec_driver_sql(
            "SELECT id FROM dividend_events WHERE id=?", (str(intent.event_id),)
        ).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(submit, engine, successor, at=AT + timedelta(seconds=1)).result(timeout=30)
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and value.revision_id == intent.revision_id
        assert len(value.items) == 1 and value.items[0].status == "OPEN"
    with unit_of_work(engine) as con:
        value = reviews.history(con, OWNER, intent.event_id)
        assert value is not None and value.revision_id == successor.revision_id
        assert value.items[0].status == "RESOLVED"


@pytest.mark.parametrize(
    "column,value",
    [
        ("detail_json", '{"scope":"EVENT_EVIDENCE_REVIEW"}'),
        ("reason_code", "OTHER"),
        ("severity", "INFO"),
        ("created_at", "2026-11-10T12:00:00.000000Z"),
        ("status", "DISMISSED"),
        ("resolved_at", "2026-11-10T12:00:00.000000Z"),
    ],
)
def test_tampered_review_shape_fails_closed_without_repair(
    engine: Engine, source: dict[str, object], column: str, value: str
) -> None:
    intent = incomplete(engine)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"UPDATE review_items SET {column}=? WHERE event_id=?", (value, str(intent.event_id))
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(reviews.EvidenceReviewError, match="^DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE$"),
    ):
        reviews.history(con, OWNER, intent.event_id)
    assert database_snapshot(engine) == before


def test_reader_requires_transaction_and_uuid_scope(engine: Engine) -> None:
    with (
        engine.connect() as con,
        pytest.raises(
            reviews.EvidenceReviewError, match="^INVALID_DIVIDEND_EVIDENCE_REVIEW_SCOPE$"
        ),
    ):
        reviews.history(con, OWNER, uuid4())
    with (
        unit_of_work(engine) as con,
        pytest.raises(
            reviews.EvidenceReviewError, match="^INVALID_DIVIDEND_EVIDENCE_REVIEW_SCOPE$"
        ),
    ):
        reviews.history(con, "not-an-owner", uuid4())  # type: ignore[arg-type]
