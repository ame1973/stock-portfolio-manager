"""Read proposed revision scope and decision together without granting a rebuild."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_service as event_history
import spm_api.dividend_event_writer as events
import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_rejection_writer as rejection_writer
import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_scope as revision_scope
from spm_api.audit import now
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_revision_preview import proposal
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def decision(book: dict[str, Any], graph: dict[str, Any], **changes: Any) -> events.EventIntent:
    previous = book["event"]
    return manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
        **changes,
    )


async def test_observation_binds_decision_event_all_accounts_and_retained_financial_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    old = save(engine, book, intent(engine, book, tax, fee), uuid4())
    direct_receipt(engine, book, received_micro=6_000_000)
    proposed, next_decision = proposal(graph), decision(book, graph)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("network")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("network")),
        unit_of_work(engine) as con,
    ):
        observed = rebuild.observe(
            con, book["owner"], book["event"].event_id, next_decision, proposed
        )
    assert observed is not None
    assert observed.event.record.revision_id == book["event"].revision_id
    assert observed.decision == next_decision and observed.preview.proposal == proposed
    assert observed.candidate is None and observed.rejections == ()
    assert observed.evidence_reviews.event_id == book["event"].event_id
    assert observed.scope == "RECORDED_REBUILD_OBSERVATION_ONLY"
    assert observed.history_completeness == "UNVERIFIED"
    assert observed.current_use_scope == "REVALIDATION_REQUIRED"
    assert observed.eligibility_scope == observed.payment_scope == "NOT_ESTABLISHED"
    accounts = {item.account_id: item for item in observed.preview.accounts}
    assert set(accounts) == {UUID(graph["source"]["id"]), UUID(graph["target"]["id"])}
    source = accounts[book["account_id"]]
    assert source.previous_projection_id == old.version.id
    assert (source.previous_expected, source.recorded_received, source.recorded_outstanding) == (
        Decimal("10.000000"),
        Decimal("6.000000"),
        Decimal("5.000000"),
    )
    assert source.proposed_accrual is not None and source.proposed_accrual.net_local == Decimal(
        "11"
    )
    assert graph["source"]["id"] not in repr(observed) and "6.000000" not in repr(observed)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": uuid4()},
        {"revision_id": None},
        {"revision_no": 1},
        {"revision_no": 3},
        {"revision_no": True},
        {"reason": "private"},
        {"terms": None},
    ],
)
async def test_invalid_or_stale_decision_fails_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], change: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = decision(book, graph).model_copy(update=change)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con, pytest.raises(rebuild.RebuildError):
        rebuild.observe(con, book["owner"], book["event"].event_id, value, proposal(graph))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"dps": "1.2"},
        {"ex_date": "2026-01-09"},
        {"currency": "HKD"},
        {"adjustment_basis": "UNKNOWN"},
    ],
)
async def test_proposal_must_match_the_explicit_manual_decision(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], change: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    raw = proposal(graph).model_dump(mode="python")
    raw.update(change)
    if change.get("currency") == "HKD":
        raw["accounts"] = [
            {**row, "fx_rate_hkd": None, "fx_source": None} for row in raw["accounts"]
        ]
    proposed = preview.RevisionProposal.model_validate(raw)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_PROPOSAL_MISMATCH$"),
    ):
        rebuild.observe(con, book["owner"], book["event"].event_id, decision(book, graph), proposed)
    assert database_snapshot(engine) == before


async def test_existing_revision_id_cannot_be_adopted_by_a_new_observation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = decision(book, graph).model_copy(update={"revision_id": book["event"].revision_id})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_EVENT_REVISION_CONFLICT$"),
    ):
        rebuild.observe(con, book["owner"], book["event"].event_id, value, proposal(graph))
    assert database_snapshot(engine) == before


async def test_current_rejection_requires_its_exact_observed_pin(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    value = rejection_writer.RejectionIntent(
        id=uuid4(),
        event_id=previous.event_id,
        revision_id=previous.revision_id,
        revision_no=previous.revision_no,
        citation="Synthetic rejection reference",
        source_url=None,
        reason="Synthetic owner rejection requiring an explicit resolution.",
    )
    with unit_of_work(engine, write=True) as con:
        retained = rejection_writer.save(con, book["owner"], value, request_id=uuid4())
    original = decision(book, graph)
    before = database_snapshot(engine)
    for changed, code in (
        (original, "DIVIDEND_REJECTION_CONFIRMATION_REQUIRED"),
        (
            original.model_copy(update={"resolve_rejection_id": uuid4()}),
            "DIVIDEND_REJECTION_VERSION_CONFLICT",
        ),
    ):
        with unit_of_work(engine) as con, pytest.raises(rebuild.RebuildError, match=f"^{code}$"):
            rebuild.observe(con, book["owner"], previous.event_id, changed, proposal(graph))
    pinned = original.model_copy(update={"resolve_rejection_id": retained.id})
    with unit_of_work(engine) as con:
        result = rebuild.observe(con, book["owner"], previous.event_id, pinned, proposal(graph))
    assert result is not None and result.rejections == (retained,)
    assert database_snapshot(engine) == before


async def test_resolved_rejection_remains_historical_without_a_new_pin(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    rejection = rejection_writer.RejectionIntent(
        id=uuid4(),
        event_id=previous.event_id,
        revision_id=previous.revision_id,
        revision_no=previous.revision_no,
        citation="Synthetic rejection reference",
        source_url=None,
        reason="Synthetic owner rejection requiring an explicit resolution.",
    )
    with unit_of_work(engine, write=True) as con:
        retained = rejection_writer.save(con, book["owner"], rejection, request_id=uuid4())
    successor = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1"),
        resolve_rejection_id=retained.id,
    )
    with unit_of_work(engine, write=True) as con:
        events.save(con, book["owner"], successor, at=now(), request_id=uuid4())
    book["event"] = successor
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        value = rebuild.observe(
            con, book["owner"], successor.event_id, decision(book, graph), proposal(graph)
        )
    assert value is not None and value.rejections == (retained,)
    assert value.event.resolution is not None and value.event.resolution.rejection_id == retained.id
    assert database_snapshot(engine) == before


async def test_candidate_decision_is_retained_not_a_current_source_grant(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    original = decision(book, graph)
    assert original.terms is not None
    candidate = candidates.CandidateDecision(
        id=uuid4(),
        candidate_id=uuid4(),
        revision_no=1,
        state="OPEN",
        terms=original.terms,
        matches=(),
        change_reason="Synthetic explicit candidate without source observations.",
    )
    with unit_of_work(engine, write=True) as con:
        retained = candidates.save(con, book["owner"], candidate, at=now(), request_id=uuid4())
    value = events.EventIntent(
        event_id=original.event_id,
        revision_id=original.revision_id,
        revision_no=original.revision_no,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason="Synthetic candidate association without an eligibility grant.",
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], original.event_id, value, proposal(graph))
    assert observed is not None and observed.candidate == retained
    assert observed.source_scope == "CURRENT_SOURCE_NOT_REVALIDATED"
    forged_assessment = replace(retained.assessment, status="CONFLICT")
    for substituted in (None, replace(retained, assessment=forged_assessment)):
        with (
            unit_of_work(engine) as con,
            patch.object(candidates, "get", return_value=substituted),
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
        ):
            rebuild.observe(con, book["owner"], original.event_id, value, proposal(graph))
    assert database_snapshot(engine) == before


async def test_foreign_event_is_not_found_and_scope_remains_private(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert (
            rebuild.observe(
                con, uuid4(), book["event"].event_id, decision(book, graph), proposal(graph)
            )
            is None
        )
    assert database_snapshot(engine) == before


async def test_substituted_hypothetical_amount_cannot_replace_the_recomputed_result(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    original = preview.read

    def forged(*args: Any, **kwargs: Any) -> preview.RevisionPreview | None:
        value = original(*args, **kwargs)
        assert value is not None
        return replace(
            value,
            accounts=(
                replace(value.accounts[0], recorded_received=Decimal("999")),
                *value.accounts[1:],
            ),
        )

    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(preview, "read", side_effect=forged),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert database_snapshot(engine) == before


async def test_observation_rejects_a_substituted_empty_rejection_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    rejection = rejection_writer.RejectionIntent(
        id=uuid4(),
        event_id=previous.event_id,
        revision_id=previous.revision_id,
        revision_no=previous.revision_no,
        citation="Synthetic rejection reference",
        source_url=None,
        reason="Synthetic owner rejection requiring an explicit resolution.",
    )
    with unit_of_work(engine, write=True) as con:
        retained = rejection_writer.save(con, book["owner"], rejection, request_id=uuid4())
    with unit_of_work(engine) as con:
        assert rejections.history(con, book["owner"], previous.event_id) == (retained,)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(rejections, "history", return_value=()),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], previous.event_id, decision(book, graph), proposal(graph)
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("substitution", ["empty", "invalid_timestamp"])
async def test_observation_rejects_substituted_evidence_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], substitution: str
) -> None:
    book = await prepare(client, engine, graph)
    previous = book["event"]
    successor = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1", adjustment_basis="UNKNOWN"),
    )
    with unit_of_work(engine, write=True) as con:
        events.save(con, book["owner"], successor, at=now(), request_id=uuid4())
    book["event"] = successor
    with unit_of_work(engine) as con:
        retained = reviews.history(con, book["owner"], successor.event_id)
    assert retained is not None and retained.items
    malformed: dict[str, Any] = {"created_at": "private synthetic timestamp"}
    substituted = (
        ()
        if substitution == "empty"
        else (replace(retained.items[0], **malformed), *retained.items[1:])
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(reviews, "history", return_value=replace(retained, items=substituted)),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], successor.event_id, decision(book, graph), proposal(graph)
        )
    assert database_snapshot(engine) == before


async def test_observation_rejects_substituted_candidate_terms(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    proposed_decision = decision(book, graph)
    assert proposed_decision.terms is not None
    candidate = candidates.CandidateDecision(
        id=uuid4(),
        candidate_id=uuid4(),
        revision_no=1,
        state="OPEN",
        terms=terms(security_id=graph["security"]["id"], dps="1.2"),
        matches=(),
        change_reason="Synthetic explicit candidate without source observations.",
    )
    with unit_of_work(engine, write=True) as con:
        retained = candidates.save(con, book["owner"], candidate, at=now(), request_id=uuid4())
    forged = replace(
        retained,
        record=retained.record.model_copy(
            update={"decision": candidate.model_copy(update={"terms": proposed_decision.terms})}
        ),
    )
    value = events.EventIntent(
        event_id=proposed_decision.event_id,
        revision_id=proposed_decision.revision_id,
        revision_no=proposed_decision.revision_no,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason="Synthetic candidate association without an eligibility grant.",
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(candidates, "get", return_value=forged),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(con, book["owner"], value.event_id, value, proposal(graph))
    assert database_snapshot(engine) == before


async def test_observation_rejects_substituted_event_terms_on_verification(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    proposed_decision = decision(book, graph)
    assert proposed_decision.terms is not None
    with unit_of_work(engine) as con:
        retained = event_history.get(con, book["owner"], proposed_decision.event_id)
    assert retained is not None
    forged = replace(
        retained, record=retained.record.model_copy(update={"terms": proposed_decision.terms})
    )
    value = events.EventIntent(
        event_id=proposed_decision.event_id,
        revision_id=proposed_decision.revision_id,
        revision_no=proposed_decision.revision_no,
        kind="VERIFY",
        citation="Synthetic owner reference",
        reason="Synthetic verification without an eligibility grant.",
    )
    original = event_history.get
    calls = 0

    def substituted(*args: Any, **kwargs: Any) -> event_history.ReviewedEvent | None:
        nonlocal calls
        calls += 1
        return forged if calls == 1 else original(*args, **kwargs)

    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(event_history, "get", side_effect=substituted),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(con, book["owner"], value.event_id, value, proposal(graph))
    assert database_snapshot(engine) == before


async def test_observation_rejects_malformed_event_resolution(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    with unit_of_work(engine) as con:
        retained = event_history.get(con, book["owner"], book["event"].event_id)
    assert retained is not None
    malformed: dict[str, Any] = {"resolution": "private synthetic resolution"}
    with (
        unit_of_work(engine) as con,
        patch.object(event_history, "get", return_value=replace(retained, **malformed)),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )


@pytest.mark.parametrize("invalid", ["transaction", "owner", "event", "decision", "proposal"])
async def test_observation_requires_a_valid_caller_owned_scope(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], invalid: str
) -> None:
    book = await prepare(client, engine, graph)
    args: list[Any] = [
        book["owner"],
        book["event"].event_id,
        decision(book, graph),
        proposal(graph),
    ]
    if invalid != "transaction":
        args[["owner", "event", "decision", "proposal"].index(invalid)] = object()
    before = database_snapshot(engine)
    with engine.connect() as con:
        if invalid != "transaction":
            con.begin()
        with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_SCOPE$"):
            rebuild.observe(con, *args)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("state", "version", "code"),
    [
        ("MISSING", 1, "CANDIDATE_NOT_FOUND"),
        ("OPEN", 2, "CANDIDATE_VERSION_CONFLICT"),
        ("DISMISSED", 1, "CANDIDATE_DISMISSED"),
    ],
)
async def test_candidate_requires_an_existing_open_exact_version(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    state: str,
    version: int,
    code: str,
) -> None:
    book = await prepare(client, engine, graph)
    proposed = decision(book, graph)
    assert proposed.terms is not None
    candidate_id = uuid4()
    if state != "MISSING":
        candidate = candidates.CandidateDecision.model_validate(
            {
                "id": uuid4(),
                "candidate_id": candidate_id,
                "revision_no": 1,
                "state": state,
                "terms": proposed.terms,
                "matches": (),
                "change_reason": "Synthetic candidate decision.",
            }
        )
        with unit_of_work(engine, write=True) as con:
            candidates.save(con, book["owner"], candidate, at=now(), request_id=uuid4())
    value = events.EventIntent(
        event_id=proposed.event_id,
        revision_id=proposed.revision_id,
        revision_no=proposed.revision_no,
        kind="CANDIDATE",
        candidate_id=candidate_id,
        candidate_version=version,
        reason="Synthetic candidate association without an eligibility grant.",
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con, pytest.raises(rebuild.RebuildError, match=f"^{code}$"):
        rebuild.observe(con, book["owner"], value.event_id, value, proposal(graph))
    assert database_snapshot(engine) == before


async def test_missing_substituted_event_is_unavailable_not_not_found(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(event_history, "get", return_value=None),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": uuid4()},
        {"revision_id": uuid4()},
        {"revision_no": 9},
        {"scope": "FORGED_SCOPE"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "VERIFIED"},
        {"eligibility_scope": "ESTABLISHED"},
    ],
)
async def test_substituted_review_envelope_cannot_claim_a_scope_or_grant(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], changes: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    with unit_of_work(engine) as con:
        retained = reviews.history(con, book["owner"], book["event"].event_id)
    assert retained is not None
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(reviews, "history", return_value=replace(retained, **changes)),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert database_snapshot(engine) == before


async def test_history_failure_does_not_disclose_private_values(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    private = "private synthetic owner reason 198765.432100"
    with (
        unit_of_work(engine) as con,
        patch.object(reviews, "history", side_effect=ValueError(private)),
        pytest.raises(rebuild.RebuildError) as exc,
    ):
        rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert str(exc.value) == "DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE"
    assert exc.value.__cause__ is None and private not in repr(exc.value)


@pytest.mark.parametrize("kind", ["VERIFY", "WITHDRAW"])
async def test_verification_and_withdrawal_preview_existing_terms_only(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    book = await prepare(client, engine, graph)
    existing = decision(book, graph)
    value = events.EventIntent.model_validate(
        {
            "event_id": existing.event_id,
            "revision_id": existing.revision_id,
            "revision_no": existing.revision_no,
            "kind": kind,
            "citation": "Synthetic owner reference",
            "reason": "Synthetic decision without an eligibility grant.",
        }
    )
    explicit = preview.RevisionProposal.model_validate(
        {**proposal(graph).model_dump(mode="python"), "dps": "1"}
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], value.event_id, value, explicit)
    assert observed is not None and observed.decision.kind == kind
    assert observed.preview.scope == "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"
    assert observed.eligibility_scope == observed.payment_scope == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("missing", ["rejection", "evidence", "preview"])
async def test_missing_reader_result_is_unavailable(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], missing: str
) -> None:
    book = await prepare(client, engine, graph)
    reader = {"rejection": rejections, "evidence": reviews, "preview": preview}[missing]
    method = "read" if missing == "preview" else "history"
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(reader, method, return_value=None),
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
    ):
        rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert database_snapshot(engine) == before


async def test_inconsistent_independent_revision_scope_fails_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    proposed = proposal(graph)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        retained = preview.read(con, book["owner"], book["event"].event_id, proposed)
        assert retained is not None
        with (
            patch.object(preview, "read", return_value=retained),
            patch.object(revision_scope, "read", return_value=None),
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
        ):
            rebuild.observe(
                con, book["owner"], book["event"].event_id, decision(book, graph), proposed
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("duplicate", ["estimate", "history"])
async def test_duplicate_account_rows_cannot_form_a_comparison(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], duplicate: str
) -> None:
    book = await prepare(client, engine, graph)
    direct_receipt(engine, book, received_micro=6_000_000)
    explicit = proposal(graph)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        event = event_history.get(con, book["owner"], book["event"].event_id)
        retained = preview.read(con, book["owner"], book["event"].event_id, explicit)
        assert event is not None and retained is not None and retained.snapshot.retained.accounts
        snapshot = retained.snapshot
        if duplicate == "estimate":
            explicit = explicit.model_copy(
                update={"accounts": (*explicit.accounts, explicit.accounts[0])}
            )
        else:
            old = snapshot.retained
            snapshot = replace(
                snapshot, retained=replace(old, accounts=(*old.accounts, old.accounts[0]))
            )
        forged = replace(retained, snapshot=snapshot, proposal=explicit)
        with (
            patch.object(revision_scope, "read", return_value=snapshot),
            pytest.raises(ValueError, match="^Inconsistent recorded accounts$"),
        ):
            rebuild._verify_preview(con, book["owner"], event.record, explicit, forged)
    assert database_snapshot(engine) == before


async def test_concurrent_receipt_does_not_mix_the_wal_observation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    original = preview.read
    saved: list[dict[str, Any]] = []

    def raced(*args: Any, **kwargs: Any) -> preview.RevisionPreview | None:
        value = original(*args, **kwargs)
        if not saved:
            with ThreadPoolExecutor(max_workers=1) as pool:
                saved.append(
                    pool.submit(direct_receipt, engine, book, received_micro=6_000_000).result(
                        timeout=30
                    )
                )
        return value

    with unit_of_work(engine) as con, patch.object(preview, "read", side_effect=raced):
        observed = rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert observed is not None
    source = next(
        item for item in observed.preview.accounts if item.account_id == book["account_id"]
    )
    assert source.active_receipts == 0 and len(saved) == 1
    with unit_of_work(engine) as con:
        latest = rebuild.observe(
            con, book["owner"], book["event"].event_id, decision(book, graph), proposal(graph)
        )
    assert latest is not None
    assert (
        next(
            item for item in latest.preview.accounts if item.account_id == book["account_id"]
        ).active_receipts
        == 1
    )
