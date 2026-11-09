"""Internal event decisions retain history and fence economic projection changes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
from spm_api.audit import canonical
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion
from spm_api.dividend_evidence import DividendTerms
from spm_api.ledger_service import post_cash
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import action
from tests.test_dividend_basis_migration import SECURITY
from tests.test_dividend_candidate_service import basis as basis
from tests.test_dividend_candidate_service import decision as candidate_decision
from tests.test_dividend_candidate_service import save as save_candidate
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source

AT = datetime(2026, 11, 9, 12, tzinfo=UTC)


def terms(**changes: Any) -> DividendTerms:
    return DividendTerms.model_validate(
        {
            "security_id": SECURITY,
            "ex_date": "2026-01-08",
            "dps": "0.90000000",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            **changes,
        }
    )


def manual(**changes: Any) -> writer.EventIntent:
    return writer.EventIntent.model_validate(
        {
            "event_id": uuid4(),
            "revision_id": uuid4(),
            "revision_no": 1,
            "kind": "MANUAL",
            "terms": terms(),
            "citation": "Synthetic owner reference",
            "source_url": "https://example.test/dividend?private=synthetic",
            "reason": REASON,
            **changes,
        }
    )


def submit(engine: Engine, intent: writer.EventIntent, **options: Any) -> events.ReviewedEvent:
    args = {"owner_id": OWNER, "at": AT, "request_id": uuid4(), **options}
    with unit_of_work(engine, write=True) as con:
        return writer.save(con, intent=intent, **args)


def next_intent(
    first: writer.EventIntent, kind: str, *, number: int = 2, **changes: Any
) -> writer.EventIntent:
    return writer.EventIntent.model_validate(
        {
            "event_id": first.event_id,
            "revision_id": uuid4(),
            "revision_no": number,
            "kind": kind,
            "citation": "Explicit synthetic follow-up decision",
            "reason": REASON,
            **changes,
        }
    )


def test_manual_creation_publishes_audited_exact_terms_without_economic_effects(
    engine: Engine, source: dict[str, Any]
) -> None:
    intent, request = manual(), uuid4()
    before = database_snapshot(engine)
    result = submit(engine, intent, request_id=request)
    assert result.record.event_id == intent.event_id
    assert result.record.revision_id == intent.revision_id
    assert result.record.terms == intent.terms
    assert result.record.review_status == "PROVISIONAL"
    assert result.record.manual_locked is False and result.assessment is None
    assert "private=synthetic" not in repr(result)
    after = database_snapshot(engine)
    allowed = {
        "dividend_events",
        "dividend_event_controls",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "audit_events",
    }
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert all(len(after[name]) == len(before[name]) + 1 for name in allowed)
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, intent.event_id) == result
        assert events.get(con, OTHER, intent.event_id) is None
    committed = database_snapshot(engine)
    assert submit(engine, intent, request_id=request) == result
    assert database_snapshot(engine) == committed


def test_verification_withdrawal_and_explicit_manual_reopening_retain_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    original = submit(engine, first)
    verify = next_intent(first, "VERIFY")
    verified = submit(engine, verify, at=AT + timedelta(seconds=1))
    assert verified.record.terms == original.record.terms
    assert verified.record.review_status == "VERIFIED" and verified.record.manual_locked
    withdrawn = next_intent(first, "WITHDRAW", number=3)
    closed = submit(engine, withdrawn, at=AT + timedelta(seconds=2))
    assert closed.record.review_status == "WITHDRAWN" and closed.record.manual_locked
    assert closed.record.terms == original.record.terms
    retry_verify = next_intent(first, "VERIFY", number=4)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_WITHDRAWN$"):
        submit(engine, retry_verify, at=AT + timedelta(seconds=3))
    assert database_snapshot(engine) == before
    reopen = next_intent(first, "MANUAL", number=4, terms=terms(dps="1.10000000"))
    reopened = submit(engine, reopen, at=AT + timedelta(seconds=3))
    assert reopened.record.review_status == "PROVISIONAL" and not reopened.record.manual_locked
    assert reopened.record.terms.dps == "1.10000000"
    with unit_of_work(engine) as con:
        assert events.get(con, OWNER, first.event_id, version=1) == original
        assert events.get(con, OWNER, first.event_id, version=2) == verified
        assert events.get(con, OWNER, first.event_id, version=3) == closed
        assert events.get(con, OWNER, first.event_id) == reopened


def test_candidate_adoption_preserves_exact_current_basis_and_source_evidence(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    reviewed = save_candidate(engine, candidate)
    intent = writer.EventIntent(
        event_id=uuid4(),
        revision_id=uuid4(),
        revision_no=1,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason=REASON,
    )
    request = uuid4()
    result = submit(engine, intent, request_id=request)
    assert result.assessment is not None and result.assessment == reviewed.assessment
    assert result.record.candidate_revision_id == candidate.id
    assert result.record.terms.dps == "0.90000000"
    assert result.assessment.checks[0].basis_assertion_id == basis.id
    with unit_of_work(engine) as con:
        link = con.exec_driver_sql(
            "SELECT observation_id,field_names_json FROM dividend_evidence_links "
            "WHERE revision_id=?",
            (str(intent.revision_id),),
        ).one()
    assert link.observation_id == str(basis.observation_id)
    dismissed = candidate.model_copy(
        update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED", "matches": ()}
    )
    save_candidate(engine, dismissed, at=AT + timedelta(seconds=1))
    before = database_snapshot(engine)
    assert submit(engine, intent, request_id=request) == result
    revised = intent.model_copy(update={"revision_id": uuid4(), "revision_no": 2})
    with pytest.raises(events.EventError, match="^CANDIDATE_VERSION_CONFLICT$"):
        submit(engine, revised, at=AT + timedelta(seconds=2))
    assert database_snapshot(engine) == before


def test_candidate_cannot_silently_replace_a_verified_manual_decision(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    first = manual()
    submit(engine, first)
    verify = next_intent(first, "VERIFY")
    submit(engine, verify, at=AT + timedelta(seconds=1))
    attempted = writer.EventIntent(
        event_id=first.event_id,
        revision_id=uuid4(),
        revision_no=3,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason=REASON,
    )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_LOCKED$"):
        submit(engine, attempted, at=AT + timedelta(seconds=2))
    assert database_snapshot(engine) == before


def test_writer_requires_current_active_owner_and_writer_transaction(
    engine: Engine, source: dict[str, Any]
) -> None:
    intent = manual()
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(events.EventError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        writer.save(con, OWNER, intent, at=AT, request_id=uuid4())
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_OWNER_UNAVAILABLE$"):
        submit(engine, intent, owner_id=uuid4())
    assert database_snapshot(engine) == before


def test_stale_versions_and_reused_physical_ids_cannot_rewrite_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    intent, request = manual(), uuid4()
    original = submit(engine, intent, request_id=request)
    before = database_snapshot(engine)
    different = intent.model_copy(update={"reason": "A different explicit decision reason."})
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_REVISION_CONFLICT$"):
        submit(engine, different, request_id=request)
    stale = next_intent(intent, "VERIFY", number=3)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        submit(engine, stale, at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before
    assert submit(engine, intent, request_id=request) == original


def owned_account(engine: Engine, owner: UUID = OWNER) -> tuple[str, str]:
    portfolio, broker, account = (str(uuid4()) for _ in range(3))
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": portfolio,
                "owner_id": str(owner),
                "name": portfolio,
                "created_at": "2026-01-01T00:00:00.000000Z",
            },
        )
        insert(con, "brokers", {"id": broker, "owner_id": str(owner), "name": broker})
        insert(
            con,
            "accounts",
            {
                "id": account,
                "portfolio_id": portfolio,
                "broker_id": broker,
                "name": account,
                "default_currency": "USD",
                "created_at": "2026-01-01T00:00:00.000000Z",
            },
        )
    return portfolio, account


def test_event_changes_invalidate_only_owned_affected_portfolios_from_earliest_ex_date(
    engine: Engine, source: dict[str, Any]
) -> None:
    owned, account = owned_account(engine)
    foreign, other_account = owned_account(engine, OTHER)
    with unit_of_work(engine, write=True) as con:
        for portfolio, target in ((owned, account), (foreign, other_account)):
            insert(
                con,
                "positions",
                {
                    "portfolio_id": portfolio,
                    "account_id": target,
                    "security_id": str(SECURITY),
                    "projection_version": 0,
                    "shares_e8": 1000000000,
                    "remaining_cost_hkd_micro": 780000000,
                    "realized_hkd_micro": 0,
                    "calculated_at_utc": "2026-01-01T00:00:00.000000Z",
                },
            )
    before = database_snapshot(engine)
    first = manual()
    submit(engine, first)
    amended = next_intent(first, "MANUAL", terms=terms(ex_date="2025-12-31"))
    submit(engine, amended, at=AT + timedelta(seconds=1))
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT portfolio_id,from_date,reason,source_id FROM projection_invalidations "
            "ORDER BY rowid"
        ).all()
        assert [tuple(row) for row in rows] == [
            (owned, "2026-01-08", "DIVIDEND_EVENT_CHANGED", str(first.revision_id)),
            (owned, "2025-12-31", "DIVIDEND_EVENT_CHANGED", str(amended.revision_id)),
        ]
        assert (
            con.exec_driver_sql(
                "SELECT data_revision FROM portfolios WHERE id=?", (owned,)
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT data_revision FROM portfolios WHERE id=?", (foreign,)
            ).scalar_one()
            == 0
        )
    after = database_snapshot(engine)
    for name in (
        "positions",
        "trades",
        "cash_ledger_entries",
        "dividend_entitlements",
        "dividend_receipts",
    ):
        assert after[name] == before[name]


@pytest.mark.parametrize("with_receipt", [False, True])
def test_existing_entitlement_fences_even_unchanged_verification_until_atomic_rebuild(
    engine: Engine, source: dict[str, Any], with_receipt: bool
) -> None:
    _, account = owned_account(engine)
    first, request = manual(), uuid4()
    original = submit(engine, first, request_id=request)
    entitlement, receipt = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "dividend_entitlements",
            {
                "id": entitlement,
                "revision_id": str(first.revision_id),
                "account_id": account,
                "eligible_shares_e8": 1000000000,
                "gross_micro": 10000000,
                "expected_net_micro": 10000000,
                "currency": "USD",
                "quality": "ACCRUED",
                "projection_version": 0,
                "created_at": "2026-11-09T12:00:00.000000Z",
            },
        )
        if with_receipt:
            insert(
                con,
                "dividend_receipts",
                {
                    "id": receipt,
                    "entitlement_id": entitlement,
                    "account_id": account,
                    "received_date": "2026-01-09",
                    "received_micro": 6000000,
                    "currency": "USD",
                    "fx_rate_hkd_e12": 7800000000000,
                    "received_hkd_micro": 46800000,
                    "created_at": "2026-11-09T12:00:00.000000Z",
                },
            )
            post_cash(
                con,
                account=account,
                day="2026-01-09",
                currency="USD",
                amount=6000000,
                fx=7800000000000,
                booked=46800000,
                kind="DIVIDEND_RECEIPT",
                source_type="DIVIDEND_RECEIPT",
                source_id=receipt,
            )
    committed = database_snapshot(engine)
    assert submit(engine, first, request_id=request) == original
    assert database_snapshot(engine) == committed
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        submit(engine, next_intent(first, "VERIFY"), at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == committed
    with unit_of_work(engine) as con:
        current = events.get(con, OWNER, first.event_id)
        assert current is not None and current.record.revision_no == 1


def test_a_historical_entitlement_also_fences_a_later_event_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    _, account = owned_account(engine)
    first = manual()
    submit(engine, first)
    verify = next_intent(first, "VERIFY")
    submit(engine, verify, at=AT + timedelta(seconds=1))
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "dividend_entitlements",
            {
                "id": str(uuid4()),
                "revision_id": str(first.revision_id),
                "account_id": account,
                "eligible_shares_e8": 1000000000,
                "gross_micro": 10000000,
                "expected_net_micro": 10000000,
                "currency": "USD",
                "quality": "ACCRUED",
                "projection_version": 0,
                "created_at": "2026-11-09T12:00:02.000000Z",
            },
        )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"):
        submit(
            engine,
            next_intent(first, "MANUAL", number=3, terms=terms(dps="1.1")),
            at=AT + timedelta(seconds=3),
        )
    assert database_snapshot(engine) == before


def test_fractional_historical_revision_replay_fails_with_fixed_corruption_code(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    submit(engine, first)
    verify, request = next_intent(first, "VERIFY"), uuid4()
    submit(engine, verify, at=AT + timedelta(seconds=1), request_id=request)
    third = next_intent(first, "MANUAL", number=3, terms=terms())
    submit(engine, third, at=AT + timedelta(seconds=2))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_event_revision_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_event_revisions SET revision_no=1.5 WHERE id=?",
            (str(verify.revision_id),),
        )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"):
        submit(engine, verify, at=AT + timedelta(seconds=1), request_id=request)
    assert database_snapshot(engine) == before


def test_new_candidate_use_rechecks_actual_action_scope_but_historical_replay_survives(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    candidate = candidate_decision(basis)
    save_candidate(engine, candidate)
    first = writer.EventIntent(
        event_id=uuid4(),
        revision_id=uuid4(),
        revision_no=1,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason=REASON,
    )
    request = uuid4()
    result = submit(engine, first, request_id=request)
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
    assert submit(engine, first, request_id=request) == result
    other = first.model_copy(update={"event_id": uuid4(), "revision_id": uuid4()})
    with pytest.raises(events.EventError, match="^CANDIDATE_BASIS_CHANGED$"):
        submit(engine, other, at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before


def test_new_adoption_cannot_hide_a_corrupt_earlier_candidate_audit(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    first = candidate_decision(basis)
    save_candidate(engine, first)
    second = first.model_copy(update={"id": uuid4(), "revision_no": 2})
    save_candidate(engine, second, at=AT)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? WHERE entity_id=? "
            "AND action='DIVIDEND_CANDIDATE_REVISED'",
            ("a" * 64, str(first.id)),
        )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        submit(engine, candidate_intent(second), at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before


def conflicting_candidate(
    engine: Engine, basis: SplitBasisAssertion
) -> candidates.CandidateDecision:
    body = candidate_decision(basis).model_copy(update={"terms": terms(dps="1.10000000")})
    result = save_candidate(engine, body)
    assert result.assessment.status == "CONFLICT"
    return body


def candidate_intent(body: candidates.CandidateDecision) -> writer.EventIntent:
    return writer.EventIntent(
        event_id=uuid4(),
        revision_id=uuid4(),
        revision_no=1,
        kind="CANDIDATE",
        candidate_id=body.candidate_id,
        candidate_version=body.revision_no,
        reason=REASON,
    )


def test_conflict_reviews_are_per_owned_portfolio_and_resolved_with_explicit_revision(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    affected = [owned_account(engine), owned_account(engine)]
    foreign, foreign_account = owned_account(engine, OTHER)
    with unit_of_work(engine, write=True) as con:
        for portfolio, account in [*affected, (foreign, foreign_account)]:
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
    initial = submit(engine, intent)
    assert initial.record.review_status == "CONFLICT"
    with unit_of_work(engine) as con:
        reviews = con.exec_driver_sql(
            "SELECT portfolio_id,status,reason_code FROM review_items "
            "WHERE event_id=? ORDER BY portfolio_id",
            (str(intent.event_id),),
        ).all()
        assert [tuple(row) for row in reviews] == [
            (portfolio, "OPEN", "DIVIDEND_SOURCE_CONFLICT") for portfolio, _ in sorted(affected)
        ]
    verify, request = next_intent(intent, "VERIFY"), uuid4()
    result = submit(engine, verify, at=AT + timedelta(seconds=1), request_id=request)
    assert result.record.review_status == "VERIFIED" and result.record.manual_locked
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM review_items WHERE event_id=? "
                "AND status='RESOLVED' AND resolved_at=?",
                (str(intent.event_id), "2026-11-09T12:00:01.000000Z"),
            ).scalar_one()
            == 2
        )
        review_audits = con.exec_driver_sql(
            "SELECT action,count(*) FROM audit_events WHERE "
            "action LIKE 'DIVIDEND_EVIDENCE_REVIEW_%' GROUP BY action ORDER BY action"
        ).all()
        assert [tuple(row) for row in review_audits] == [
            ("DIVIDEND_EVIDENCE_REVIEW_CREATED", 2),
            ("DIVIDEND_EVIDENCE_REVIEW_RESOLVED", 2),
        ]
        assert (
            con.exec_driver_sql(
                "SELECT data_revision FROM portfolios WHERE id=?", (foreign,)
            ).scalar_one()
            == 0
        )
    before = database_snapshot(engine)
    assert submit(engine, verify, at=AT + timedelta(seconds=1), request_id=request) == result
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "damage",
    [
        "detail",
        "detail-oversize",
        "detail-unknown-revision",
        "missing-audit",
        "audit-hash",
        "duplicate-audit",
        "foreign-portfolio",
        "wrong-reason",
        "wrong-severity",
        "wrong-time",
    ],
)
def test_corrupt_review_cannot_be_silently_resolved_or_publish_a_revision(
    engine: Engine, basis: SplitBasisAssertion, damage: str
) -> None:
    portfolio, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
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
    foreign, _ = owned_account(engine, OTHER)
    with unit_of_work(engine, write=True) as con:
        review = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=?", (str(intent.event_id),)
        ).scalar_one()
        if damage == "detail":
            con.exec_driver_sql(
                "UPDATE review_items SET detail_json=? WHERE id=?",
                ('{"scope":"EVENT_EVIDENCE_REVIEW"}', review),
            )
        elif damage in {"detail-oversize", "detail-unknown-revision"}:
            detail = {"scope": "EVENT_EVIDENCE_REVIEW", "revision_id": str(uuid4())}
            if damage == "detail-oversize":
                detail["padding"] = "x" * 5000
            con.exec_driver_sql(
                "UPDATE review_items SET detail_json=? WHERE id=?", (canonical(detail), review)
            )
        elif damage == "missing-audit":
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_delete")
            con.exec_driver_sql(
                "DELETE FROM audit_events WHERE entity_id=? "
                "AND action='DIVIDEND_EVIDENCE_REVIEW_CREATED'",
                (review,),
            )
        elif damage == "audit-hash":
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            con.exec_driver_sql(
                "UPDATE audit_events SET after_sha256=? WHERE entity_id=? "
                "AND action='DIVIDEND_EVIDENCE_REVIEW_CREATED'",
                ("a" * 64, review),
            )
        elif damage == "duplicate-audit":
            row = dict(
                con.exec_driver_sql(
                    "SELECT * FROM audit_events WHERE entity_id=? "
                    "AND action='DIVIDEND_EVIDENCE_REVIEW_CREATED'",
                    (review,),
                )
                .mappings()
                .one()
            )
            insert(con, "audit_events", {**row, "id": str(uuid4())})
        elif damage == "foreign-portfolio":
            con.exec_driver_sql(
                "UPDATE review_items SET portfolio_id=? WHERE id=?", (foreign, review)
            )
        else:
            column, value = {
                "wrong-reason": ("reason_code", "OTHER"),
                "wrong-severity": ("severity", "INFO"),
                "wrong-time": ("created_at", "2026-11-10T12:00:00.000000Z"),
            }[damage]
            con.exec_driver_sql(f"UPDATE review_items SET {column}=? WHERE id=?", (value, review))
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"):
        submit(engine, next_intent(intent, "VERIFY"), at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before


def test_two_event_writers_use_cas_and_identical_replay_never_duplicates_effects(
    engine: Engine, source: dict[str, Any]
) -> None:
    portfolio, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
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
    first = manual()
    request = uuid4()
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(lambda _: submit(engine, first, request_id=request), range(2)))
    assert replies[0] == replies[1]
    candidates = [next_intent(first, "VERIFY"), next_intent(first, "VERIFY")]

    def attempt(intent: writer.EventIntent) -> str:
        try:
            submit(engine, intent, at=AT + timedelta(seconds=1))
            return "COMMITTED"
        except events.EventError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, candidates))
    assert sorted(outcomes) == ["COMMITTED", "DIVIDEND_EVENT_VERSION_CONFLICT"]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_event_revisions WHERE event_id=?",
                (str(first.event_id),),
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='DIVIDEND_EVENT_REVISED'"
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM projection_invalidations WHERE portfolio_id=?", (portfolio,)
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT data_revision FROM portfolios WHERE id=?", (portfolio,)
            ).scalar_one()
            == 2
        )


def test_unresolved_manual_basis_creates_review_until_explicit_correction(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual(terms=terms(adjustment_basis="UNKNOWN"))
    result = submit(engine, first)
    assert result.record.review_status == "INCOMPLETE"
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT portfolio_id,status,reason_code FROM review_items WHERE event_id=?",
            (str(first.event_id),),
        ).one() == (None, "OPEN", "DIVIDEND_INCOMPLETE")
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_UNRESOLVED_BASIS$"):
        submit(engine, next_intent(first, "VERIFY"), at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before
    corrected = next_intent(first, "MANUAL", terms=terms())
    assert (
        submit(engine, corrected, at=AT + timedelta(seconds=1)).record.review_status
        == "PROVISIONAL"
    )
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT status FROM review_items WHERE event_id=?", (str(first.event_id),)
            ).scalar_one()
            == "RESOLVED"
        )


def test_same_day_distributions_keep_distinct_explicit_event_identities(
    engine: Engine, source: dict[str, Any]
) -> None:
    regular = manual(terms=terms(event_type="REGULAR"))
    special = manual(terms=terms(event_type="SPECIAL"))
    first, second = submit(engine, regular), submit(engine, special)
    assert first.record.event_id != second.record.event_id
    assert first.record.terms.ex_date == second.record.terms.ex_date
    assert first.record.terms.event_type == "REGULAR"
    assert second.record.terms.event_type == "SPECIAL"
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_events WHERE security_id=? AND provenance_version=1",
                (str(SECURITY),),
            ).scalar_one()
            == 2
        )


def test_one_candidate_cannot_be_implicitly_reused_for_a_second_stable_event(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = candidate_decision(basis)
    save_candidate(engine, body)
    first = candidate_intent(body)
    submit(engine, first)
    another = candidate_intent(body)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_CANDIDATE_ALREADY_USED$"):
        submit(engine, another, at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before


def test_another_owner_cannot_use_a_candidate_or_learn_its_evidence(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = candidate_decision(basis)
    save_candidate(engine, body)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^CANDIDATE_NOT_FOUND$") as error:
        submit(engine, candidate_intent(body), owner_id=OTHER)
    assert str(basis.observation_id) not in str(error.value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["ex_date", "dps", "currency"])
def test_incomplete_candidate_cannot_be_published_as_a_complete_event(
    engine: Engine, basis: SplitBasisAssertion, field: str
) -> None:
    body = candidate_decision(basis).model_copy(update={"terms": terms(**{field: None})})
    save_candidate(engine, body)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^INVALID_DIVIDEND_EVENT_DECISION$"):
        submit(engine, candidate_intent(body))
    assert database_snapshot(engine) == before


def test_candidate_adoption_never_reads_private_configuration_or_opens_provider_http(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = candidate_decision(basis)
    save_candidate(engine, body)
    with (
        patch.object(
            ProviderConfigurationStore, "load", side_effect=AssertionError("private file")
        ),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider HTTP")),
    ):
        assert submit(engine, candidate_intent(body)).record.terms.dps == "0.90000000"


@pytest.mark.parametrize("damage", ["owner", "request", "clock", "intent", "invalid-copy"])
def test_internal_scope_and_untrusted_copied_intents_fail_without_writes(
    engine: Engine, source: dict[str, Any], damage: str
) -> None:
    intent = manual()
    options: dict[str, Any] = {}
    if damage == "owner":
        options["owner_id"] = str(OWNER)
        code = "INVALID_DIVIDEND_EVENT_SCOPE"
    elif damage == "request":
        options["request_id"] = str(uuid4())
        code = "INVALID_DIVIDEND_EVENT_DECISION"
    elif damage == "clock":
        options["at"] = AT.replace(tzinfo=None)
        code = "INVALID_DIVIDEND_EVENT_DECISION"
    elif damage == "intent":
        intent = "not-an-intent"  # type: ignore[assignment]
        code = "INVALID_DIVIDEND_EVENT_DECISION"
    else:
        intent = intent.model_copy(update={"revision_no": True})
        code = "INVALID_DIVIDEND_EVENT_DECISION"
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match=f"^{code}$"):
        submit(engine, intent, **options)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "kind",
    [
        "candidate-with-terms",
        "candidate-without-version",
        "manual-without-citation",
        "verify-with-terms",
        "withdraw-with-candidate",
    ],
)
def test_intents_cannot_mix_explicit_decision_authority(kind: str) -> None:
    values: dict[str, Any] = {
        "event_id": uuid4(),
        "revision_id": uuid4(),
        "revision_no": 1,
        "reason": REASON,
    }
    if kind == "candidate-with-terms":
        values.update(kind="CANDIDATE", candidate_id=uuid4(), candidate_version=1, terms=terms())
    elif kind == "candidate-without-version":
        values.update(kind="CANDIDATE", candidate_id=uuid4())
    elif kind == "manual-without-citation":
        values.update(kind="MANUAL", terms=terms())
    elif kind == "verify-with-terms":
        values.update(kind="VERIFY", citation="Synthetic citation", terms=terms())
    else:
        values.update(kind="WITHDRAW", citation="Synthetic citation", candidate_id=uuid4())
    with pytest.raises(ValidationError):
        writer.EventIntent.model_validate(values)


def test_wrong_owner_and_backwards_clock_cannot_mutate_an_event(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    submit(engine, first)
    proposed = next_intent(first, "VERIFY")
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_NOT_FOUND$"):
        submit(engine, proposed, owner_id=OTHER, at=AT + timedelta(seconds=1))
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_TIME_CONFLICT$"):
        submit(engine, proposed, at=AT - timedelta(seconds=1))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "failing_action", ["DIVIDEND_EVENT_REVISED", "DIVIDEND_EVIDENCE_REVIEW_CREATED"]
)
def test_late_audit_failure_rolls_back_event_links_reviews_and_invalidation(
    engine: Engine, basis: SplitBasisAssertion, failing_action: str
) -> None:
    portfolio, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
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
    body = conflicting_candidate(engine, basis)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_late_event_audit BEFORE INSERT ON audit_events "
            f"WHEN NEW.action='{failing_action}' "
            "BEGIN SELECT RAISE(ABORT,'SYNTHETIC_EVENT_AUDIT_FAILURE'); END"
        )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="SYNTHETIC_EVENT_AUDIT_FAILURE"):
        submit(engine, candidate_intent(body))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("amount", "currency", "day"),
    [("0", "HKD", "0001-01-01"), ("92233720368.54775807", "USD", "9999-12-31")],
)
def test_event_writer_preserves_exact_zero_and_scaled_int64_boundaries(
    engine: Engine, source: dict[str, Any], amount: str, currency: str, day: str
) -> None:
    intent = manual(terms=terms(dps=amount, currency=currency, ex_date=day), source_url=None)
    result = submit(engine, intent)
    assert result.record.terms.dps == ("0.00000000" if amount == "0" else amount)
    assert result.record.terms.currency == currency and result.record.terms.ex_date == day


@pytest.mark.parametrize("kind", ["VERIFY", "WITHDRAW"])
def test_verify_or_withdraw_requires_a_prior_event(
    engine: Engine, source: dict[str, Any], kind: str
) -> None:
    intent = writer.EventIntent(
        event_id=uuid4(),
        revision_id=uuid4(),
        revision_no=1,
        kind=kind,
        citation="Synthetic prior evidence",
        reason=REASON,
    )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_NOT_FOUND$"):
        submit(engine, intent)
    assert database_snapshot(engine) == before


def test_candidate_cannot_overwrite_an_unlocked_withdrawn_event(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = candidate_decision(basis)
    save_candidate(engine, body)
    first = manual()
    submit(engine, first)
    withdraw = next_intent(first, "WITHDRAW")
    submit(engine, withdraw, at=AT + timedelta(seconds=1))
    attempted = candidate_intent(body).model_copy(
        update={"event_id": first.event_id, "revision_no": 3}
    )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_WITHDRAWN$"):
        submit(engine, attempted, at=AT + timedelta(seconds=2))
    assert database_snapshot(engine) == before


def test_event_creation_cannot_predate_its_audited_candidate(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = candidate_decision(basis)
    saved = save_candidate(engine, body)
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_TIME_CONFLICT$"):
        submit(engine, candidate_intent(body), at=saved.record.created_at - timedelta(seconds=1))
    assert database_snapshot(engine) == before


def test_event_security_cannot_change_and_unknown_security_is_not_created(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    submit(engine, first)
    other_security = uuid4()
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_SECURITY_CHANGED$"):
        submit(
            engine,
            next_intent(first, "MANUAL", terms=terms(security_id=other_security)),
            at=AT + timedelta(seconds=1),
        )
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_SECURITY_UNAVAILABLE$"):
        submit(engine, manual(terms=terms(security_id=other_security)))
    assert database_snapshot(engine) == before


def test_invalidation_failure_rolls_back_event_revision_review_and_audit(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    portfolio, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
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
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_event_invalidation BEFORE INSERT ON "
            "projection_invalidations WHEN NEW.reason='DIVIDEND_EVENT_CHANGED' "
            "BEGIN SELECT RAISE(ABORT,'SYNTHETIC_INVALIDATION_FAILURE'); END"
        )
    body = conflicting_candidate(engine, basis)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="SYNTHETIC_INVALIDATION_FAILURE"):
        submit(engine, candidate_intent(body))
    assert database_snapshot(engine) == before


def test_new_event_version_and_physical_revision_id_collisions_fail_closed(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = manual()
    submit(engine, first)
    before = database_snapshot(engine)
    collision = manual(revision_id=first.revision_id)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_REVISION_CONFLICT$"):
        submit(engine, collision)
    missing_first = manual(revision_no=2)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        submit(engine, missing_first)
    assert database_snapshot(engine) == before


def test_legacy_event_is_never_silently_adopted_by_the_new_writer(
    engine: Engine, source: dict[str, Any]
) -> None:
    legacy_id = uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "dividend_events",
            {
                "id": str(legacy_id),
                "security_id": str(SECURITY),
                "event_identity_key": str(legacy_id),
                "workflow_status": "INCOMPLETE",
                "created_at": "2026-11-09T12:00:00.000000Z",
            },
        )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_NOT_FOUND$"):
        submit(engine, manual(event_id=legacy_id))
    assert database_snapshot(engine) == before


def test_cas_publication_failure_rolls_back_audit_and_partial_event(
    engine: Engine, source: dict[str, Any]
) -> None:
    intent = manual()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_event_cas_ignored BEFORE UPDATE ON "
            "dividend_events BEGIN SELECT RAISE(IGNORE); END"
        )
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        submit(engine, intent)
    assert database_snapshot(engine) == before
