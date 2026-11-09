"""Owned candidate decisions retain explicit evidence without adopting financial facts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

import spm_api.dividend_basis_service as bases
import spm_api.dividend_candidate_service as candidates
from spm_api.audit import canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_basis import SplitBasisAssertion
from spm_api.dividend_evidence import DividendTerms
from spm_api.observation_service import original_dividend
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import action
from tests.test_dividend_basis_migration import SECURITY, cloned_source, seeded
from tests.test_dividend_candidate_migration import candidate
from tests.test_provider_persistence import book as book
from tests.test_provider_persistence import fetched
from tests.test_provider_persistence import save as capture_source

OWNER = UUID("c0000000-0000-4000-8000-000000000001")
OTHER = UUID("c0000000-0000-4000-8000-000000000002")
AT = datetime(2026, 11, 9, tzinfo=UTC)
REASON = "Explicit source association for this possible distribution."


@pytest.fixture
def basis(engine: Engine) -> SplitBasisAssertion:
    with unit_of_work(engine, write=True) as con:
        original = seeded(con)
        for owner in (OWNER, OTHER):
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic-internal-actor-not-http-auth',?,?)",
                (str(owner), stamp(AT), stamp(AT)),
            )
        assertion = SplitBasisAssertion.model_validate_json(original["assertion_json"])
        bases.save(con, OWNER, assertion, at=AT, request_id=uuid4())
    return assertion


def decision(basis: SplitBasisAssertion, **changes: Any) -> candidates.CandidateDecision:
    return candidates.CandidateDecision(
        id=uuid4(),
        candidate_id=uuid4(),
        revision_no=1,
        state="OPEN",
        terms=DividendTerms(
            security_id=SECURITY,
            ex_date="2026-01-08",
            dps="0.90000000",
            currency="USD",
            adjustment_basis="AS_DECLARED",
        ),
        matches=(
            candidates.CandidateMatch(
                observation_id=basis.observation_id,
                source_sha256=basis.source_sha256,
                basis_assertion_id=basis.id,
                reason=REASON,
            ),
        ),
        change_reason=REASON,
        **changes,
    )


def save(engine: Engine, body: candidates.CandidateDecision, **changes: Any) -> Any:
    options = {"owner_id": OWNER, "at": AT, "request_id": uuid4(), **changes}
    with unit_of_work(engine, write=True) as con:
        return candidates.save(con, decision=body, **options)


def get(
    engine: Engine, identifier: UUID, *, version: int | None = None, owner: UUID = OWNER
) -> Any:
    with unit_of_work(engine) as con:
        return candidates.get(con, owner, identifier, version=version)


def current(engine: Engine, identifier: UUID, version: int) -> Any:
    with unit_of_work(engine, write=True) as con:
        return candidates.require_current(con, OWNER, identifier, version=version)


def test_audited_explicit_match_compares_exact_basis_without_financial_adoption(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body, request = decision(basis), uuid4()
    before = database_snapshot(engine)
    result = save(engine, body, request_id=request)
    assert result.record.decision == body and result.record.owner_id == OWNER
    assert result.record.request_id == request and result.record.created_at == AT
    assert result.assessment.status == "PROVISIONAL"
    assert result.assessment.independent_origins == {"YAHOO"}
    assert result.assessment.checks[0].comparison_dps == "0.90000000"
    assert result.assessment.checks[0].basis_assertion_id == basis.id
    assert get(engine, body.candidate_id) == result
    assert current(engine, body.candidate_id, 1) == result
    after = database_snapshot(engine)
    assert {
        name: rows
        for name, rows in before.items()
        if name not in {"dividend_candidate_revisions", "audit_events"}
    } == {
        name: rows
        for name, rows in after.items()
        if name not in {"dividend_candidate_revisions", "audit_events"}
    }
    assert len(after["dividend_candidate_revisions"]) == 1
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT owner_id,entity_type,entity_id,action,reason,request_id,before_sha256 "
            "FROM audit_events WHERE action='DIVIDEND_CANDIDATE_REVISED'"
        ).one()
        assert tuple(row) == (
            str(OWNER),
            "dividend_candidate_revision",
            str(body.id),
            "DIVIDEND_CANDIDATE_REVISED",
            REASON,
            str(request),
            None,
        )


def test_candidate_versions_are_explicit_owned_and_historical(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    first = decision(basis)
    old = save(engine, first)
    newer = first.model_copy(update={"id": uuid4(), "revision_no": 2, "state": "DISMISSED"})
    latest = save(engine, newer, at=AT + timedelta(seconds=1))
    assert get(engine, first.candidate_id) == latest
    assert get(engine, first.candidate_id, version=1) == old
    assert get(engine, first.candidate_id, owner=OTHER) is None
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_VERSION_CONFLICT$"):
        save(engine, newer.model_copy(update={"id": uuid4()}))
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_DISMISSED$"):
        current(engine, first.candidate_id, 2)
    assert database_snapshot(engine) == before


def test_historical_replay_survives_action_correction_but_new_use_is_fenced(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body, request = decision(basis), uuid4()
    original = save(engine, body, request_id=request)
    ref = basis.splits[0]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?", (str(ref.id),)
        )
        action(
            con,
            str(uuid4()),
            root_id=str(ref.root_id),
            supersedes_id=str(ref.id),
            security_id=str(SECURITY),
            revision=2,
            effective_date=ref.effective_date,
            ratio_num=2,
            ratio_den=1,
        )
    before = database_snapshot(engine)
    assert get(engine, body.candidate_id) == original
    assert save(engine, body, request_id=request) == original
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_BASIS_CHANGED$"):
        current(engine, body.candidate_id, 1)
    revision = body.model_copy(update={"id": uuid4(), "revision_no": 2})
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_BASIS_CHANGED$"):
        save(engine, revision, at=AT + timedelta(seconds=1))
    assert database_snapshot(engine) == before
    dismissed = revision.model_copy(update={"state": "DISMISSED", "matches": ()})
    assert save(engine, dismissed, at=AT + timedelta(seconds=1)).record.decision == dismissed
    assert get(engine, body.candidate_id, version=1) == original


def test_unaudited_sql_candidate_never_becomes_an_accepted_predecessor(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    values = candidate(
        {
            "observation_id": str(basis.observation_id),
            "source_sha256": basis.source_sha256,
            "id": str(basis.id),
        },
        owner_id=str(OWNER),
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_candidate_revisions", values)
    before = database_snapshot(engine)
    identifier = UUID(values["candidate_id"])
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        get(engine, identifier)
    body = decision(basis).model_copy(update={"candidate_id": identifier, "revision_no": 2})
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        save(engine, body)
    assert database_snapshot(engine) == before


def test_late_audit_failure_rolls_back_every_table(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("""
            CREATE TRIGGER test_reject_candidate_audit BEFORE INSERT ON audit_events
            WHEN NEW.action='DIVIDEND_CANDIDATE_REVISED'
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_CANDIDATE_AUDIT_FAILURE'); END
        """)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError, match="SYNTHETIC_CANDIDATE_AUDIT_FAILURE"):
        save(engine, decision(basis))
    assert database_snapshot(engine) == before


def test_concurrent_writers_cannot_both_append_the_same_revision(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    first = decision(basis)
    save(engine, first)
    versions = [first.model_copy(update={"id": uuid4(), "revision_no": 2}) for _ in range(2)]

    def attempt(body: candidates.CandidateDecision) -> str:
        try:
            save(engine, body, at=AT + timedelta(seconds=1))
            return "saved"
        except candidates.CandidateError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, versions))
    assert sorted(results) == ["CANDIDATE_VERSION_CONFLICT", "saved"]
    assert get(engine, first.candidate_id).record.decision.revision_no == 2


def test_same_day_candidates_remain_distinct_and_unmatched_values_stay_unknown(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    first = decision(basis)
    second = decision(basis).model_copy(
        update={
            "terms": first.terms.model_copy(
                update={"event_type": "SPECIAL", "dps": "0.00000000", "currency": None}
            ),
            "matches": (),
        }
    )
    left, right = save(engine, first), save(engine, second)
    assert left.record.decision.candidate_id != right.record.decision.candidate_id
    assert left.record.decision.terms.ex_date == right.record.decision.terms.ex_date
    assert right.assessment.status == "INCOMPLETE"
    assert right.record.decision.terms.dps == "0.00000000"
    assert right.record.decision.terms.currency is None
    assert get(engine, first.candidate_id) == left and get(engine, second.candidate_id) == right


def test_explicit_changed_source_stays_a_conflict_not_an_automatic_merge(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    with unit_of_work(engine, write=True) as con:
        other = cloned_source(
            con,
            {"observation_id": str(basis.observation_id)},
            dps="0.70000000",
            adjustment_basis="AS_DECLARED",
        )
    body = decision(basis)
    extra = candidates.CandidateMatch(
        observation_id=UUID(other["observation_id"]),
        source_sha256=other["source_sha256"],
        reason=REASON,
    )
    body = candidates.CandidateDecision.model_validate(
        {**body.model_dump(), "matches": (*body.matches, extra)}
    )
    result = save(engine, body)
    assert result.assessment.status == "CONFLICT"
    assert result.assessment.conflicting_fields == {"dps"}
    assert result.assessment.independent_origins == {"YAHOO"}
    assert len(result.assessment.checks) == 2
    assert get(engine, body.candidate_id) == result
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT dps_e8,adjustment_basis FROM dividend_observations "
            "WHERE id IN (?,?) ORDER BY dps_e8",
            (str(basis.observation_id), other["observation_id"]),
        ).all()
    assert [tuple(row) for row in rows] == [(60000000, "SPLIT_ADJUSTED"), (70000000, "AS_DECLARED")]


@pytest.mark.parametrize("operation", ["save", "current"])
def test_new_decisions_and_current_use_require_active_owner_and_writer(
    engine: Engine, basis: SplitBasisAssertion, operation: str
) -> None:
    body, request = decision(basis), uuid4()
    record = save(engine, body, request_id=request)
    before = database_snapshot(engine)
    with (
        pytest.raises(candidates.CandidateError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
        unit_of_work(engine) as con,
    ):
        if operation == "save":
            candidates.save(con, OWNER, body, at=AT, request_id=request)
        else:
            candidates.require_current(con, OWNER, body.candidate_id, version=1)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_OWNER_UNAVAILABLE$"):
        if operation == "save":
            save(engine, body, request_id=request)
        else:
            current(engine, body.candidate_id, 1)
    assert get(engine, body.candidate_id) == record and database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["owner", "candidate", "zero", "negative", "huge", "bool", "text"]
)
def test_read_scope_is_validated_before_sql_binding(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    body = decision(basis)
    save(engine, body)
    owner: Any = "private-invalid-owner" if problem == "owner" else OWNER
    identifier: Any = "private-invalid-candidate" if problem == "candidate" else body.candidate_id
    version: Any = {"zero": 0, "negative": -1, "huge": 2**63, "bool": True, "text": "1"}.get(
        problem
    )
    before = database_snapshot(engine)
    with (
        pytest.raises(candidates.CandidateError, match="^INVALID_CANDIDATE_SCOPE$") as error,
        unit_of_work(engine) as con,
    ):
        candidates.get(con, owner, identifier, version=version)
    assert "private-invalid" not in str(error.value) and database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["owner", "request", "naive", "epoch", "overflow", "body"])
def test_invalid_internal_arguments_never_write_or_echo(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    body = decision(basis)
    args: dict[str, Any] = {"owner_id": OWNER, "decision": body, "at": AT, "request_id": uuid4()}
    changes: dict[str, tuple[str, Any]] = {
        "owner": ("owner_id", "private-invalid-owner"),
        "request": ("request_id", "private-invalid-request"),
        "naive": ("at", AT.replace(tzinfo=None)),
        "epoch": ("at", 1),
        "overflow": ("at", datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14)))),
        "body": ("decision", {}),
    }
    key, value = changes[problem]
    args[key] = value
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError) as error, unit_of_work(engine, write=True) as con:
        candidates.save(con, **args)
    assert "private-invalid" not in str(error.value) and database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    ["reason", "match-reason", "hash", "amount", "version", "duplicate", "too-many", "bytes"],
)
def test_frozen_model_copies_cannot_bypass_candidate_validation(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    body = decision(basis)
    match = body.matches[0]
    changes: dict[str, Any] = {
        "reason": {"change_reason": "private-invalid\0decision"},
        "match-reason": {
            "matches": (match.model_copy(update={"reason": "private-invalid\0match"}),)
        },
        "hash": {"matches": (match.model_copy(update={"source_sha256": "private-invalid"}),)},
        "amount": {"terms": body.terms.model_copy(update={"dps": 0.9})},
        "version": {"revision_no": True},
        "duplicate": {"matches": (match, match)},
        "too-many": {
            "matches": tuple(
                match.model_copy(update={"observation_id": uuid4()}) for _ in range(101)
            )
        },
        "bytes": {
            "matches": tuple(
                match.model_copy(update={"observation_id": uuid4(), "reason": "中" * 1000})
                for _ in range(44)
            )
        },
    }
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^INVALID_CANDIDATE_DECISION$") as error:
        save(engine, body.model_copy(update=changes[problem]))
    assert "private-invalid" not in str(error.value) and database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "missing-source",
        "wrong-hash",
        "missing-basis",
        "foreign-basis",
        "wrong-basis-source",
        "missing-security",
    ],
)
def test_new_decisions_validate_original_source_and_owned_basis_pins(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    body = decision(basis)
    match = body.matches[0]
    owner = OWNER
    expected = {
        "missing-source": "CANDIDATE_SOURCE_UNAVAILABLE",
        "wrong-hash": "CANDIDATE_SOURCE_CHANGED",
        "missing-basis": "CANDIDATE_BASIS_UNAVAILABLE",
        "foreign-basis": "CANDIDATE_BASIS_UNAVAILABLE",
        "wrong-basis-source": "CANDIDATE_BASIS_CHANGED",
        "missing-security": "CANDIDATE_SECURITY_UNAVAILABLE",
    }[problem]
    if problem == "missing-source":
        match = match.model_copy(update={"observation_id": uuid4()})
    elif problem == "wrong-hash":
        match = match.model_copy(update={"source_sha256": "0" * 64})
    elif problem == "missing-basis":
        match = match.model_copy(update={"basis_assertion_id": uuid4()})
    elif problem == "foreign-basis":
        owner = OTHER
    elif problem == "wrong-basis-source":
        with unit_of_work(engine, write=True) as con:
            other = cloned_source(
                con, {"observation_id": str(basis.observation_id)}, dps="0.70000000"
            )
        match = match.model_copy(
            update={
                "observation_id": UUID(other["observation_id"]),
                "source_sha256": other["source_sha256"],
            }
        )
    else:
        body = body.model_copy(
            update={"terms": body.terms.model_copy(update={"security_id": uuid4()})}
        )
    body = body.model_copy(update={"matches": (match,)})
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match=f"^{expected}$"):
        save(engine, body, owner_id=owner)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["request", "clock", "terms", "owner"])
def test_replayed_revision_identity_cannot_be_reattributed_or_changed(
    engine: Engine, basis: SplitBasisAssertion, change: str
) -> None:
    body, request = decision(basis), uuid4()
    save(engine, body, request_id=request)
    arguments: dict[str, Any] = {"request_id": request}
    if change == "request":
        arguments["request_id"] = uuid4()
    elif change == "clock":
        arguments["at"] = AT + timedelta(microseconds=1)
    elif change == "owner":
        arguments["owner_id"] = OTHER
    else:
        body = body.model_copy(update={"change_reason": "A distinct candidate decision reason."})
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_REVISION_CONFLICT$"):
        save(engine, body, **arguments)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["foreign-owner", "foreign-security", "gap", "earlier-time"])
def test_new_revision_obeys_owner_security_version_and_clock_fences(
    engine: Engine, basis: SplitBasisAssertion, change: str
) -> None:
    body = decision(basis)
    save(engine, body)
    revised = body.model_copy(update={"id": uuid4(), "revision_no": 2})
    options: dict[str, Any] = {"at": AT + timedelta(seconds=1)}
    if change == "foreign-owner":
        options["owner_id"] = OTHER
    elif change == "foreign-security":
        revised = revised.model_copy(
            update={"terms": revised.terms.model_copy(update={"security_id": uuid4()})}
        )
    elif change == "gap":
        revised = revised.model_copy(update={"revision_no": 3})
    else:
        options["at"] = AT - timedelta(microseconds=1)
    code = "CANDIDATE_TIME_CONFLICT" if change == "earlier-time" else "CANDIDATE_VERSION_CONFLICT"
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match=f"^{code}$"):
        save(engine, revised, **options)
    assert database_snapshot(engine) == before


def test_aware_non_utc_creation_is_canonical_and_unknown_version_is_not_found(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = decision(basis)
    result = save(engine, body, at=AT.astimezone(timezone(timedelta(hours=8))))
    assert result.record.created_at == AT and get(engine, body.candidate_id) == result
    assert get(engine, body.candidate_id, version=2) is None
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_VERSION_CONFLICT$"):
        current(engine, body.candidate_id, 2)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_NOT_FOUND$"):
        current(engine, uuid4(), 1)


def test_no_private_files_or_upstream_http_are_touched_by_candidate_history(
    engine: Engine, basis: SplitBasisAssertion, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Candidate decisions cannot read private files or construct HTTP clients"
        )

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderConfigurationStore, "unchanged", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    body = decision(basis)
    result = save(engine, body)
    assert (
        get(engine, body.candidate_id) == result and current(engine, body.candidate_id, 1) == result
    )
    assert REASON not in repr(result) and REASON not in repr(body)


@pytest.mark.parametrize(
    "problem",
    [
        "wire",
        "digest",
        "terms",
        "created",
        "request",
        "audit-missing",
        "audit-duplicate",
        "audit-hash",
    ],
)
def test_corrupt_history_or_audit_is_never_repaired_or_reused(
    engine: Engine, basis: SplitBasisAssertion, problem: str
) -> None:
    body, request = decision(basis), uuid4()
    save(engine, body, request_id=request)
    with unit_of_work(engine, write=True) as con:
        if problem in {"wire", "digest", "terms", "created", "request"}:
            con.exec_driver_sql("DROP TRIGGER trg_dividend_candidate_no_update")
            con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            if problem == "wire":
                matches = [match.model_dump(mode="json") for match in body.matches]
                matches[0]["reason"] = "A different private-canary association reason."
                wire = canonical(matches)
                con.exec_driver_sql(
                    "UPDATE dividend_candidate_revisions "
                    "SET matches_json=?,matches_sha256=? WHERE id=?",
                    (wire, digest(wire), str(body.id)),
                )
            else:
                field, value = {
                    "digest": ("matches_sha256", "0" * 64),
                    "terms": ("dps_e8", 90000001),
                    "created": ("created_at", "2026-11-09T00:00:00.000000+00:00"),
                    "request": ("request_id", str(uuid4()).upper()),
                }[problem]
                con.exec_driver_sql(
                    f"UPDATE dividend_candidate_revisions SET {field}=? WHERE id=?",
                    (value, str(body.id)),
                )
            con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif problem == "audit-missing":
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_delete")
            con.exec_driver_sql(
                "DELETE FROM audit_events WHERE action='DIVIDEND_CANDIDATE_REVISED'"
            )
        elif problem == "audit-duplicate":
            row = dict(
                con.exec_driver_sql(
                    "SELECT * FROM audit_events WHERE action='DIVIDEND_CANDIDATE_REVISED'"
                )
                .mappings()
                .one()
            )
            insert(con, "audit_events", {**row, "id": str(uuid4())})
        else:
            con.exec_driver_sql("DROP TRIGGER trg_audit_event_no_update")
            con.exec_driver_sql(
                "UPDATE audit_events SET after_sha256=? WHERE action='DIVIDEND_CANDIDATE_REVISED'",
                ("0" * 64,),
            )
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$") as error:
        get(engine, body.candidate_id)
    assert "private-canary" not in str(error.value)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        save(engine, body, request_id=request)
    assert database_snapshot(engine) == before


def test_original_source_corruption_blocks_history_replay_and_new_association(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body, request = decision(basis), uuid4()
    save(engine, body, request_id=request)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_observations SET dps_e8=60000001 WHERE id=?",
            (str(basis.observation_id),),
        )
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        get(engine, body.candidate_id)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        save(engine, body, request_id=request)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_SOURCE_UNAVAILABLE$"):
        save(engine, decision(basis))
    assert database_snapshot(engine) == before


def test_a_first_revision_cannot_skip_its_initial_version(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = decision(basis).model_copy(update={"revision_no": 2})
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_VERSION_CONFLICT$"):
        save(engine, body)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["missing", "owner", "security", "time"])
def test_corrupt_predecessor_never_validates_later_revision(
    engine: Engine, basis: SplitBasisAssertion, change: str
) -> None:
    first = decision(basis)
    save(engine, first)
    second = first.model_copy(update={"id": uuid4(), "revision_no": 2})
    save(engine, second, at=AT + timedelta(seconds=1))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        if change == "missing":
            con.exec_driver_sql("DROP TRIGGER trg_dividend_candidate_no_delete")
            con.exec_driver_sql(
                "DELETE FROM dividend_candidate_revisions WHERE id=?", (str(first.id),)
            )
        else:
            con.exec_driver_sql("DROP TRIGGER trg_dividend_candidate_no_update")
            field, value = {
                "owner": ("owner_id", str(OTHER)),
                "security": ("security_id", str(uuid4())),
                "time": ("created_at", stamp(AT + timedelta(seconds=2))),
            }[change]
            if change == "security":
                con.exec_driver_sql(
                    "INSERT INTO securities(id,exchange_code,display_ticker,name,"
                    "quote_currency,created_at) "
                    "VALUES (?,'NASDAQ','CORRUPT','Corrupted synthetic security','USD',?)",
                    (value, stamp(AT)),
                )
            con.exec_driver_sql(
                f"UPDATE dividend_candidate_revisions SET {field}=? WHERE id=?",
                (value, str(first.id)),
            )
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        get(engine, first.candidate_id)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        current(engine, first.candidate_id, 2)
    assert database_snapshot(engine) == before


def test_missing_historical_basis_does_not_degrade_to_unmatched_evidence(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = decision(basis)
    save(engine, body)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_basis_no_delete")
        con.exec_driver_sql("DELETE FROM dividend_basis_assertions WHERE id=?", (str(basis.id),))
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        get(engine, body.candidate_id)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_BASIS_UNAVAILABLE$"):
        current(engine, body.candidate_id, 1)
    assert database_snapshot(engine) == before


def test_oversized_retained_matches_fail_before_json_decoding(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = decision(basis)
    save(engine, body)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("DROP TRIGGER trg_dividend_candidate_no_update")
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        con.exec_driver_sql(
            "UPDATE dividend_candidate_revisions SET matches_json=? WHERE id=?",
            ("[" + " " * 131073 + "]", str(body.id)),
        )
        con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    before = database_snapshot(engine)
    with pytest.raises(candidates.CandidateError, match="^CANDIDATE_HISTORY_UNAVAILABLE$"):
        get(engine, body.candidate_id)
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_actual_mock_http_source_capture_is_explicitly_matched_without_more_io(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = capture_source(engine, book, await fetched(book))
    source_id = UUID(receipt.observation_ids[0])
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine) as con:
        source = original_dividend(con, source_id)
    assert source is not None and source.dps == "0.90000000"
    body = candidates.CandidateDecision(
        id=uuid4(),
        candidate_id=uuid4(),
        revision_no=1,
        state="OPEN",
        terms=DividendTerms(
            security_id=source.security_id,
            ex_date=source.ex_date,
            dps=source.dps,
            currency=source.currency,
            adjustment_basis=source.adjustment_basis,
        ),
        matches=(
            candidates.CandidateMatch(
                observation_id=source_id, source_sha256=source.normalized_sha256, reason=REASON
            ),
        ),
        change_reason=REASON,
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Candidate evaluation cannot issue additional provider requests")

    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    request = uuid4()
    with unit_of_work(engine, write=True) as con:
        result = candidates.save(con, owner, body, at=AT, request_id=request)
    assert result.assessment.status == "INCOMPLETE"
    assert "UNRESOLVED_ADJUSTMENT" in result.assessment.incomplete_reasons
    assert result.assessment.checks[0].comparison_dps == "0.90000000"
    with unit_of_work(engine) as con:
        assert candidates.get(con, owner, body.candidate_id) == result
        assert original_dividend(con, source_id) == source
    after = database_snapshot(engine)
    assert {
        name: rows
        for name, rows in before.items()
        if name not in {"dividend_candidate_revisions", "audit_events"}
    } == {
        name: rows
        for name, rows in after.items()
        if name not in {"dividend_candidate_revisions", "audit_events"}
    }


def test_current_use_requires_an_explicit_integer_version(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = decision(basis)
    save(engine, body)
    before = database_snapshot(engine)
    for invalid in (None, True, "1", 0, 2**63):
        with (
            pytest.raises(candidates.CandidateError, match="^INVALID_CANDIDATE_SCOPE$"),
            unit_of_work(engine, write=True) as con,
        ):
            candidates.require_current(con, OWNER, body.candidate_id, version=invalid)  # type: ignore[arg-type]
    assert database_snapshot(engine) == before


def test_one_hundred_explicit_source_matches_are_retained_and_compared(
    engine: Engine, basis: SplitBasisAssertion
) -> None:
    body = decision(basis)
    refs = [body.matches[0].model_copy(update={"basis_assertion_id": None})]
    with unit_of_work(engine, write=True) as con:
        for index in range(1, 100):
            source = cloned_source(
                con, {"observation_id": str(basis.observation_id)}, dps=f"0.{index:08d}"
            )
            refs.append(
                candidates.CandidateMatch(
                    observation_id=UUID(source["observation_id"]),
                    source_sha256=source["source_sha256"],
                    reason=REASON,
                )
            )
    body = candidates.CandidateDecision.model_validate({**body.model_dump(), "matches": refs})
    result = save(engine, body)
    assert result.assessment.status == "INCOMPLETE" and len(result.assessment.checks) == 100
    assert len(result.record.decision.matches) == 100
    assert get(engine, body.candidate_id) == result
