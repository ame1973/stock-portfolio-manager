"""Owned event history validates original decisions without granting current adoption."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_basis_service as bases
import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
from spm_api.audit import audit, canonical, digest
from spm_api.db import unit_of_work
from spm_api.decimal_types import from_scaled
from spm_api.dividend_basis import SplitBasisAssertion
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import action
from tests.test_dividend_basis_migration import cloned_source
from tests.test_dividend_candidate_service import AT as CANDIDATE_AT
from tests.test_dividend_candidate_service import basis as basis
from tests.test_dividend_candidate_service import decision as candidate_decision
from tests.test_dividend_candidate_service import save as save_candidate
from tests.test_dividend_event_migration import (
    OTHER,
    OWNER,
    REASON,
    control,
    decision,
    event,
    publish,
    revision,
)
from tests.test_dividend_event_migration import (
    source as source,
)


def audit_payload(
    parent: dict[str, Any],
    rev: dict[str, Any],
    provenance: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the independent audit contract for a synthetic event."""
    terms = {
        "security_id": parent["security_id"],
        "ex_date": rev["ex_date"],
        "announce_date": rev["announce_date"],
        "record_date": rev["record_date"],
        "pay_date": rev["pay_date"],
        "event_type": rev["event_type"],
        "dps": from_scaled(rev["dps_e8"], 8),
        "currency": rev["currency"],
        "adjustment_basis": rev["adjustment_basis"],
    }
    citation = {"citation": provenance["citation"], "source_url": provenance["source_url"]}
    return {
        "event_id": parent["id"],
        "revision_id": rev["id"],
        "revision_no": rev["revision_no"],
        "terms": terms,
        "review_status": rev["review_status"],
        "manual_locked": bool(rev["manual_locked"]),
        "decision_kind": provenance["decision_kind"],
        "candidate_revision_id": provenance["candidate_revision_id"],
        "citation_sha256": (
            digest(canonical(citation))
            if provenance["citation"] is not None or provenance["source_url"] is not None
            else None
        ),
        "evidence": evidence or [],
        "created_at": rev["created_at"],
    }


def manual_event(
    engine: Engine, source: dict[str, Any], **changes: Any
) -> tuple[dict[str, Any], ...]:
    parent = event(source)
    values = {"adjustment_basis": "AS_DECLARED", "review_status": "PROVISIONAL", **changes}
    rev = revision(parent, source, **values)
    provenance = decision(
        parent,
        rev,
        source,
        decision_kind="MANUAL",
        candidate_revision_id=None,
        evidence_count=0,
        citation="Synthetic owner reference",
        source_url="https://example.test/dividend?private=synthetic",
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            rev["id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=provenance["request_id"],
            after=audit_payload(parent, rev, provenance),
            reason=REASON,
        )
        publish(con, parent, rev)
    return parent, rev, provenance


def append_manual(
    engine: Engine,
    source: dict[str, Any],
    parent: dict[str, Any],
    previous: dict[str, Any],
    prior_decision: dict[str, Any],
    *,
    kind: str,
    status: str,
    locked: int,
    dps: int | None = None,
    prior_evidence: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rev = revision(
        parent,
        source,
        revision_no=previous["revision_no"] + 1,
        adjustment_basis="AS_DECLARED",
        review_status=status,
        manual_locked=locked,
        dps_e8=previous["dps_e8"] if dps is None else dps,
    )
    provenance = decision(
        parent,
        rev,
        source,
        decision_kind=kind,
        candidate_revision_id=None,
        evidence_count=0,
        citation="Synthetic explicit decision reference",
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            rev["id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=provenance["request_id"],
            before=audit_payload(parent, previous, prior_decision, prior_evidence),
            after=audit_payload(parent, rev, provenance),
            reason=REASON,
        )
        publish(con, parent, rev, rev["revision_no"])
    return rev, provenance


def candidate_event(
    engine: Engine, basis: SplitBasisAssertion
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], candidates.CandidateDecision]:
    body = candidate_decision(basis)
    reviewed = save_candidate(engine, body)
    with unit_of_work(engine) as con:
        candidate_row = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_candidate_revisions WHERE id=?", (str(body.id),)
            )
            .mappings()
            .one()
        )
    source = {"candidate": candidate_row}
    parent = event(source)
    rev = revision(parent, source, review_status=reviewed.assessment.status)
    provenance = decision(parent, rev, source, candidate_revision_id=str(body.id))
    evidence = [
        {"observation_id": str(check.observation_id), "fields": sorted(check.supporting_fields)}
        for check in reviewed.assessment.checks
    ]
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        for item in evidence:
            insert(
                con,
                "dividend_evidence_links",
                {
                    "revision_id": rev["id"],
                    "observation_id": item["observation_id"],
                    "field_names_json": canonical(item["fields"]),
                },
            )
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            rev["id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=provenance["request_id"],
            after=audit_payload(parent, rev, provenance, evidence),
            reason=REASON,
        )
        publish(con, parent, rev)
    return parent, rev, provenance, body


@pytest.mark.parametrize(
    ("dps", "currency", "day", "expected"),
    [
        (0, "HKD", "0001-01-01", "0.00000000"),
        (9223372036854775807, "USD", "9999-12-31", "92233720368.54775807"),
    ],
)
def test_event_reader_preserves_zero_int64_extremes_and_explicit_currency(
    engine: Engine, source: dict[str, Any], dps: int, currency: str, day: str, expected: str
) -> None:
    parent, _, _ = manual_event(engine, source, dps_e8=dps, currency=currency, ex_date=day)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = events.get(con, OWNER, UUID(parent["id"]))
        assert result is not None
        assert (
            result.record.terms.dps,
            result.record.terms.currency,
            result.record.terms.ex_date,
        ) == (expected, currency, day)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("basis", ["UNKNOWN", "SPLIT_ADJUSTED"])
def test_manual_unresolved_adjustment_is_retained_only_as_incomplete(
    engine: Engine, source: dict[str, Any], basis: str
) -> None:
    parent, _, _ = manual_event(engine, source, adjustment_basis=basis, review_status="INCOMPLETE")
    with unit_of_work(engine) as con:
        result = events.get(con, OWNER, UUID(parent["id"]))
        assert result is not None
        assert result.record.terms.adjustment_basis == basis
        assert result.record.review_status == "INCOMPLETE"


@pytest.mark.parametrize(
    ("status", "basis", "locked"),
    [
        ("PROVISIONAL", "UNKNOWN", 0),
        ("PROVISIONAL", "SPLIT_ADJUSTED", 0),
        ("CORROBORATED", "AS_DECLARED", 0),
        ("VERIFIED", "AS_DECLARED", 1),
    ],
)
def test_manual_sql_cannot_fabricate_corroboration_or_verification(
    engine: Engine, source: dict[str, Any], status: str, basis: str, locked: int
) -> None:
    parent, _, _ = manual_event(
        engine, source, adjustment_basis=basis, review_status=status, manual_locked=locked
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


def test_legacy_event_remains_unadopted_and_missing_event_is_not_found(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = {**event(source), "provenance_version": 0}
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, UUID(parent["id"])) is None
        assert events.get(con, OWNER, uuid4()) is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["ex_date", "dps", "currency"])
def test_internal_event_envelope_requires_explicit_financial_identity(
    engine: Engine, source: dict[str, Any], field: str
) -> None:
    parent, _, _ = manual_event(engine, source)
    with unit_of_work(engine) as con:
        result = events.get(con, OWNER, UUID(parent["id"]))
    assert result is not None
    values = result.record.model_dump()
    values["terms"][field] = None
    with pytest.raises(ValidationError):
        events.EventRevision.model_validate(values)


def test_internal_event_envelope_rejects_duplicate_and_unsorted_evidence(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, _, _ = manual_event(engine, source)
    with unit_of_work(engine) as con:
        result = events.get(con, OWNER, UUID(parent["id"]))
    assert result is not None
    first = events.EventEvidence(observation_id=UUID(int=1), fields=("ex_date",))
    second = events.EventEvidence(observation_id=UUID(int=2), fields=("dps",))
    with pytest.raises(ValidationError):
        events.EventEvidence(observation_id=uuid4(), fields=("dps", "dps"))
    for evidence in ((first, first), (second, first)):
        with pytest.raises(ValidationError):
            events.EventRevision.model_validate(
                {**result.record.model_dump(), "evidence": evidence}
            )


@pytest.mark.parametrize("value", [None, 1, "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
def test_internal_event_uuid_decoder_rejects_noncanonical_values(value: Any) -> None:
    with pytest.raises(ValueError):
        events._uuid(value)


@pytest.mark.parametrize("value", [None, 1, "2026-11-09T12:00:00", "2026-11-09T12:00:00+00:00"])
def test_internal_event_timestamp_decoder_requires_canonical_utc(value: Any) -> None:
    with pytest.raises(ValueError):
        events._time(value)


def test_owned_audited_manual_event_reads_without_writes_or_private_repr(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, rev, provenance = manual_event(engine, source)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = events.get(con, OWNER, UUID(parent["id"]))
        assert result is not None
        assert result.record.event_id == UUID(parent["id"])
        assert result.record.revision_id == UUID(rev["id"])
        assert result.record.revision_no == 1
        assert result.record.terms.dps == "0.60000000"
        assert result.record.review_status == "PROVISIONAL"
        assert result.record.citation == provenance["citation"]
        assert result.record.source_url == provenance["source_url"]
        assert result.assessment is None
        assert "private=synthetic" not in repr(result)
        assert events.get(con, OTHER, UUID(parent["id"])) is None
    assert database_snapshot(engine) == before


def test_event_reader_does_not_adopt_bare_sql_audit_shapes(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = event(source)
    rev = revision(parent, source, adjustment_basis="AS_DECLARED", review_status="PROVISIONAL")
    provenance = decision(
        parent,
        rev,
        source,
        decision_kind="MANUAL",
        candidate_revision_id=None,
        evidence_count=0,
        citation="Synthetic owner reference",
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            rev["id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=provenance["request_id"],
            after={"not": "the original decision"},
            reason=REASON,
        )
        publish(con, parent, rev)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        try:
            events.get(con, OWNER, UUID(parent["id"]))
        except events.EventError as error:
            assert str(error) == "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
        else:
            raise AssertionError("A fabricated audit hash cannot authorize an event history")
    assert database_snapshot(engine) == before


def test_historical_decisions_retain_exact_terms_across_verify_override_and_withdraw(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, first, original_decision = manual_event(engine, source)
    second, second_decision = append_manual(
        engine, source, parent, first, original_decision, kind="VERIFY", status="VERIFIED", locked=1
    )
    third, third_decision = append_manual(
        engine,
        source,
        parent,
        second,
        second_decision,
        kind="MANUAL",
        status="PROVISIONAL",
        locked=0,
        dps=70000000,
    )
    fourth, _ = append_manual(
        engine, source, parent, third, third_decision, kind="WITHDRAW", status="WITHDRAWN", locked=0
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        history = [
            events.get(con, OWNER, UUID(parent["id"]), version=version) for version in range(1, 5)
        ]
        assert all(row is not None for row in history)
        assert [
            (
                row.record.revision_no,
                row.record.terms.dps,
                row.record.review_status,
                row.record.manual_locked,
            )
            for row in history
            if row is not None
        ] == [
            (1, "0.60000000", "PROVISIONAL", False),
            (2, "0.60000000", "VERIFIED", True),
            (3, "0.70000000", "PROVISIONAL", False),
            (4, "0.70000000", "WITHDRAWN", False),
        ]
        current = events.get(con, OWNER, UUID(parent["id"]))
        assert current == history[-1] and current is not None
        assert current.record.revision_id == UUID(fourth["id"])
        assert events.get(con, OWNER, UUID(parent["id"]), version=5) is None
        assert events.get(con, OTHER, UUID(parent["id"]), version=1) is None
    assert database_snapshot(engine) == before


def test_corrupt_ancestor_audit_cannot_be_hidden_by_a_later_valid_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, first, first_decision = manual_event(engine, source)
    second, second_decision = append_manual(
        engine, source, parent, first, first_decision, kind="VERIFY", status="VERIFIED", locked=1
    )
    append_manual(
        engine,
        source,
        parent,
        second,
        second_decision,
        kind="MANUAL",
        status="PROVISIONAL",
        locked=0,
        dps=70000000,
    )
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='dividend_event_revision' AND entity_id=?",
            ("a" * 64, first["id"]),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


def test_corrupt_later_audit_does_not_revive_earlier_event_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, first, first_decision = manual_event(engine, source)
    second, _ = append_manual(
        engine, source, parent, first, first_decision, kind="VERIFY", status="VERIFIED", locked=1
    )
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='dividend_event_revision' AND entity_id=?",
            ("a" * 64, second["id"]),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]), version=1)
    assert database_snapshot(engine) == before


def test_forged_audits_cannot_hide_reversed_event_timestamps(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, first, first_decision = manual_event(engine, source)
    second, _ = append_manual(
        engine, source, parent, first, first_decision, kind="VERIFY", status="VERIFIED", locked=1
    )
    moved = {**first, "created_at": "2026-11-09T13:00:00.000000Z"}
    first_hash = digest(canonical(audit_payload(parent, moved, first_decision)))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_event_revision_no_update")
        con.exec_driver_sql("DROP TRIGGER trg_dividend_revision_decisions_no_update")
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_event_revisions SET created_at=? WHERE id=?",
            (moved["created_at"], first["id"]),
        )
        con.exec_driver_sql(
            "UPDATE dividend_revision_decisions SET created_at=? WHERE revision_id=?",
            (moved["created_at"], first["id"]),
        )
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='dividend_event_revision' AND entity_id=?",
            (first_hash, first["id"]),
        )
        con.exec_driver_sql(
            "UPDATE audit_events SET before_sha256=? "
            "WHERE entity_type='dividend_event_revision' AND entity_id=?",
            (first_hash, second["id"]),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("state", ["verified", "withdrawn"])
def test_audited_candidate_cannot_override_manual_lock_or_withdrawal(
    engine: Engine, basis: SplitBasisAssertion, state: str
) -> None:
    parent, first, first_decision, body = candidate_event(engine, basis)
    with unit_of_work(engine) as con:
        prior = events.get(con, OWNER, UUID(parent["id"]))
        assert prior is not None
        evidence = [item.model_dump(mode="json") for item in prior.record.evidence]
        candidate_row = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_candidate_revisions WHERE id=?", (str(body.id),)
            )
            .mappings()
            .one()
        )
    source = {"candidate": candidate_row}
    kind, status, locked = (
        ("VERIFY", "VERIFIED", 1) if state == "verified" else ("WITHDRAW", "WITHDRAWN", 0)
    )
    second, second_decision = append_manual(
        engine,
        source,
        parent,
        first,
        first_decision,
        kind=kind,
        status=status,
        locked=locked,
        prior_evidence=evidence,
    )
    third = revision(parent, source, revision_no=3, review_status="PROVISIONAL")
    third_decision = decision(parent, third, source, candidate_revision_id=str(body.id))
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_event_revisions", third)
        insert(con, "dividend_revision_decisions", third_decision)
        for item in evidence:
            insert(
                con,
                "dividend_evidence_links",
                {
                    "revision_id": third["id"],
                    "observation_id": item["observation_id"],
                    "field_names_json": canonical(item["fields"]),
                },
            )
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            third["id"],
            "DIVIDEND_EVENT_REVISED",
            request_id=third_decision["request_id"],
            before=audit_payload(parent, second, second_decision),
            after=audit_payload(parent, third, third_decision, evidence),
            reason=REASON,
        )
        con.exec_driver_sql("DROP TRIGGER trg_managed_dividend_event_update")
        publish(con, parent, third, 3)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


def test_fractional_revision_cannot_hide_a_gap_in_retained_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent, first, first_decision = manual_event(engine, source)
    second, second_decision = append_manual(
        engine, source, parent, first, first_decision, kind="VERIFY", status="VERIFIED", locked=1
    )
    append_manual(
        engine,
        source,
        parent,
        second,
        second_decision,
        kind="MANUAL",
        status="PROVISIONAL",
        locked=0,
    )
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_event_revision_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_event_revisions SET revision_no=1.5 WHERE id=?", (second["id"],)
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("damage", ["verify-terms", "withdraw-lock"])
def test_audited_verify_and_withdraw_must_preserve_prior_terms_and_lock(
    engine: Engine, source: dict[str, Any], damage: str
) -> None:
    parent, first, first_decision = manual_event(engine, source)
    second, second_decision = append_manual(
        engine, source, parent, first, first_decision, kind="VERIFY", status="VERIFIED", locked=1
    )
    if damage == "verify-terms":
        target, provenance, column, value = second, second_decision, "dps_e8", 70000000
    else:
        target, provenance = append_manual(
            engine,
            source,
            parent,
            second,
            second_decision,
            kind="WITHDRAW",
            status="WITHDRAWN",
            locked=1,
        )
        column, value = "manual_locked", 0
    forged = {**target, column: value}
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_event_revision_no_update")
        con.exec_driver_sql(
            f"UPDATE dividend_event_revisions SET {column}=? WHERE id=?", (value, target["id"])
        )
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='dividend_event_revision' AND entity_id=?",
            (digest(canonical(audit_payload(parent, forged, provenance))), target["id"]),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["VERIFY", "WITHDRAW"])
def test_withdrawn_event_requires_an_explicit_new_manual_revision(
    engine: Engine, source: dict[str, Any], kind: str
) -> None:
    parent, first, first_decision = manual_event(engine, source)
    second, second_decision = append_manual(
        engine, source, parent, first, first_decision, kind="WITHDRAW", status="WITHDRAWN", locked=0
    )
    if kind == "VERIFY":
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("DROP TRIGGER trg_dividend_revision_decision_insert")
    append_manual(
        engine,
        source,
        parent,
        second,
        second_decision,
        kind=kind,
        status="VERIFIED" if kind == "VERIFY" else "WITHDRAWN",
        locked=1 if kind == "VERIFY" else 0,
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


def test_candidate_history_performs_no_provider_or_private_configuration_io(
    engine: Engine, basis: SplitBasisAssertion, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, _, _, _ = candidate_event(engine, basis)
    before = database_snapshot(engine)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Event history must not access provider HTTP or private configuration")

    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, UUID(parent["id"])) is not None
    assert database_snapshot(engine) == before


def test_candidate_event_retains_original_basis_after_candidate_and_action_changes(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    parent, _, _, body = candidate_event(engine, basis)
    with unit_of_work(engine) as con:
        original = events.get(con, OWNER, UUID(parent["id"]))
        assert original is not None and original.assessment is not None
        assert original.record.terms.dps == "0.90000000"
        assert original.assessment.checks[0].comparison_dps == "0.90000000"
        assert original.assessment.checks[0].basis_assertion_id == basis.id
    dismissed = body.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=CANDIDATE_AT + timedelta(seconds=1))
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
            security_id=str(reference.security_id),
            revision=2,
            effective_date=reference.effective_date,
            ratio_num=2,
            ratio_den=1,
        )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, UUID(parent["id"])) == original
    with (
        pytest.raises(bases.BasisError, match="^BASIS_ACTIONS_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        bases.require_current(con, OWNER, basis.id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("version", [0, -1, True, 2147483648, "1"])
def test_event_history_scope_rejects_invalid_versions_without_writes(
    engine: Engine, source: dict[str, Any], version: Any
) -> None:
    parent, _, _ = manual_event(engine, source)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^INVALID_DIVIDEND_EVENT_SCOPE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]), version=version)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("decision_kind", "CANDIDATE"),
        ("decision_kind", "VERIFY"),
        ("decision_kind", "WITHDRAW"),
        ("citation", None),
    ],
)
def test_forged_audit_cannot_make_an_invalid_decision_valid(
    engine: Engine, source: dict[str, Any], column: str, value: Any
) -> None:
    parent, rev, provenance = manual_event(engine, source)
    forged = {**provenance, column: value}
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_revision_decisions_no_update")
        con.exec_driver_sql(
            f"UPDATE dividend_revision_decisions SET {column}=? WHERE revision_id=?",
            (value, rev["id"]),
        )
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='dividend_event_revision' AND entity_id=?",
            (digest(canonical(audit_payload(parent, rev, forged))), rev["id"]),
        )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "damage",
    [
        "control-owner",
        "control-request",
        "control-time",
        "parent-status",
        "parent-version",
        "parent-version-real",
        "parent-identity",
        "parent-provenance",
        "revision-terms",
        "revision-lock",
        "revision-reason",
        "decision-citation",
        "decision-url",
        "decision-owner",
        "decision-time",
        "audit-hash",
        "audit-owner",
        "missing-audit",
        "missing-decision",
        "missing-control",
    ],
)
def test_corrupt_event_history_fails_closed_without_repair_or_disclosure(
    engine: Engine, source: dict[str, Any], damage: str
) -> None:
    parent, rev, _ = manual_event(engine, source)
    column: str
    value: Any
    with unit_of_work(engine, write=True) as con:
        if damage.startswith("control-"):
            con.exec_driver_sql("DROP TRIGGER trg_dividend_event_controls_no_update")
            column, value = {
                "control-owner": ("owner_id", str(OTHER)),
                "control-request": ("request_id", str(uuid4())),
                "control-time": ("created_at", "2026-11-10T12:00:00.000000Z"),
            }[damage]
            con.exec_driver_sql(
                f"UPDATE dividend_event_controls SET {column}=? WHERE event_id=?",
                (value, parent["id"]),
            )
        elif damage.startswith("parent-"):
            con.exec_driver_sql("DROP TRIGGER trg_managed_dividend_event_update")
            column, value = {
                "parent-status": ("workflow_status", "CORROBORATED"),
                "parent-version": ("version", 2),
                "parent-version-real": ("version", 1.5),
                "parent-identity": ("event_identity_key", "not-the-event-identity"),
                "parent-provenance": ("provenance_version", 0),
            }[damage]
            con.exec_driver_sql(
                f"UPDATE dividend_events SET {column}=? WHERE id=?", (value, parent["id"])
            )
        elif damage.startswith("revision-"):
            con.exec_driver_sql("DROP TRIGGER trg_dividend_event_revision_no_update")
            if damage == "revision-terms":
                con.exec_driver_sql(
                    "UPDATE dividend_event_revisions SET dps_e8=dps_e8+1 WHERE id=?", (rev["id"],)
                )
            else:
                column, value = (
                    ("manual_locked", 1)
                    if damage == "revision-lock"
                    else ("reason", "  Synthetic invalid reason  ")
                )
                con.exec_driver_sql(
                    f"UPDATE dividend_event_revisions SET {column}=? WHERE id=?", (value, rev["id"])
                )
        elif damage.startswith("decision-"):
            con.exec_driver_sql("DROP TRIGGER trg_dividend_revision_decisions_no_update")
            column, value = {
                "decision-citation": ("citation", "Synthetic changed reference"),
                "decision-url": ("source_url", "file:///private/source"),
                "decision-owner": ("owner_id", str(OTHER)),
                "decision-time": ("created_at", "2026-11-10T12:00:00.000000Z"),
            }[damage]
            con.exec_driver_sql(
                f"UPDATE dividend_revision_decisions SET {column}=? WHERE revision_id=?",
                (value, rev["id"]),
            )
        elif damage == "missing-control":
            con.exec_driver_sql("DROP TRIGGER trg_dividend_event_controls_no_delete")
            con.exec_driver_sql(
                "DELETE FROM dividend_event_controls WHERE event_id=?", (parent["id"],)
            )
        elif damage == "missing-decision":
            con.exec_driver_sql("DROP TRIGGER trg_dividend_revision_decisions_no_delete")
            con.exec_driver_sql(
                "DELETE FROM dividend_revision_decisions WHERE revision_id=?", (rev["id"],)
            )
        elif damage == "missing-audit":
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_delete")
            con.exec_driver_sql(
                "DELETE FROM audit_events "
                "WHERE entity_type='dividend_event_revision' AND entity_id=?",
                (rev["id"],),
            )
        else:
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            column, value = (
                ("after_sha256", "a" * 64) if damage == "audit-hash" else ("owner_id", str(OTHER))
            )
            con.exec_driver_sql(
                f"UPDATE audit_events SET {column}=? "
                "WHERE entity_type='dividend_event_revision' AND entity_id=?",
                (value, rev["id"]),
            )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


def test_event_reader_rejects_more_than_one_hundred_source_links(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    parent, rev, _, _ = candidate_event(engine, basis)
    observed = {"observation_id": str(basis.observation_id), "source_sha256": basis.source_sha256}
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_managed_dividend_evidence_insert")
        for number in range(1, 101):
            extra = cloned_source(con, observed, dps=f"0.{number:08d}")
            insert(
                con,
                "dividend_evidence_links",
                {
                    "revision_id": rev["id"],
                    "observation_id": extra["observation_id"],
                    "field_names_json": "[]",
                },
            )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "damage",
    [
        "link-removed",
        "link-altered",
        "link-json-object",
        "link-noncanonical",
        "link-duplicate",
        "link-oversize",
        "candidate-hash",
        "candidate-owner",
        "source-normalized",
        "basis-hash",
        "candidate-audit",
        "basis-audit",
    ],
)
def test_candidate_event_rejects_corrupt_original_lineage(
    engine: Engine, basis: SplitBasisAssertion, damage: str
) -> None:
    parent, rev, provenance, body = candidate_event(engine, basis)
    with unit_of_work(engine, write=True) as con:
        if damage == "link-removed":
            con.exec_driver_sql("DROP TRIGGER trg_managed_dividend_evidence_no_delete")
            con.exec_driver_sql(
                "DELETE FROM dividend_evidence_links WHERE revision_id=?", (rev["id"],)
            )
        elif damage == "link-altered":
            con.exec_driver_sql("DROP TRIGGER trg_managed_dividend_evidence_no_update")
            con.exec_driver_sql(
                "UPDATE dividend_evidence_links SET field_names_json='[]' WHERE revision_id=?",
                (rev["id"],),
            )
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            forged = audit_payload(
                parent,
                rev,
                provenance,
                [{"observation_id": str(basis.observation_id), "fields": []}],
            )
            con.exec_driver_sql(
                "UPDATE audit_events SET after_sha256=? "
                "WHERE entity_type='dividend_event_revision' AND entity_id=?",
                (digest(canonical(forged)), rev["id"]),
            )
        elif damage in {"link-json-object", "link-noncanonical", "link-duplicate", "link-oversize"}:
            con.exec_driver_sql("DROP TRIGGER trg_managed_dividend_evidence_no_update")
            wire = {
                "link-json-object": "{}",
                "link-noncanonical": '[ "dps" ]',
                "link-duplicate": '["dps","dps"]',
                "link-oversize": canonical(["x" * 5000]),
            }[damage]
            con.exec_driver_sql(
                "UPDATE dividend_evidence_links SET field_names_json=? WHERE revision_id=?",
                (wire, rev["id"]),
            )
        elif damage in {"candidate-hash", "candidate-owner"}:
            con.exec_driver_sql("DROP TRIGGER trg_dividend_candidate_no_update")
            column, value = (
                ("matches_sha256", "a" * 64)
                if damage == "candidate-hash"
                else ("owner_id", str(OTHER))
            )
            con.exec_driver_sql(
                f"UPDATE dividend_candidate_revisions SET {column}=? WHERE id=?",
                (value, str(body.id)),
            )
        elif damage == "source-normalized":
            con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
            con.exec_driver_sql(
                "UPDATE dividend_observations SET normalized_json='{}' WHERE id=?",
                (str(basis.observation_id),),
            )
        elif damage == "basis-hash":
            con.exec_driver_sql("DROP TRIGGER trg_dividend_basis_no_update")
            con.exec_driver_sql(
                "UPDATE dividend_basis_assertions SET assertion_sha256=? WHERE id=?",
                ("a" * 64, str(basis.id)),
            )
        else:
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            target = body.id if damage == "candidate-audit" else basis.id
            con.exec_driver_sql(
                "UPDATE audit_events SET after_sha256=? WHERE entity_id=?", ("a" * 64, str(target))
            )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
    ):
        events.get(con, OWNER, UUID(parent["id"]))
    assert database_snapshot(engine) == before
