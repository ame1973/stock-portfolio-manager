"""Candidate hints never merge source observations or create financial facts."""

import json
from dataclasses import FrozenInstanceError
from itertools import permutations
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.dividend_candidates import CandidateObservation, suggest
from spm_api.observation_service import original_dividend
from spm_api.providers.base import DividendObservation, EvidenceCitation
from spm_api.providers.http import ProviderHTTP
from spm_api.providers.manual import DividendEvidence
from tests.helpers import database_snapshot
from tests.test_provider_persistence import book as book
from tests.test_provider_persistence import fetched
from tests.test_provider_persistence import save as capture_source

SECURITY = UUID("00000000-0000-4000-8000-000000000001")
CASES: dict[str, dict[str, Any]] = {
    row["id"]: row
    for row in json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "packages/finance-fixtures/dividend_provider_cases.json"
        ).read_text()
    )["cases"]
}


def observation(identifier: int, provider: str = "eodhd", **changes: Any) -> CandidateObservation:
    source = DividendObservation.model_validate(
        {
            "provider_id": provider,
            "security_id": SECURITY,
            "provider_symbol": "EXPLICIT.SOURCE",
            "ex_date": "2026-01-08",
            "dps": "0.5",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            **changes,
        }
    )
    return CandidateObservation(observation_id=UUID(int=identifier), observation=source)


def test_same_day_regular_special_fixture_remains_two_independent_seeds() -> None:
    data = CASES["DIV-004"]
    sources = [
        observation(
            i,
            row["provider"],
            ex_date=row["ex_date"],
            dps=row["dps"],
            currency=row["currency"],
            event_type=row["event_type"],
        )
        for i, row in enumerate(data["observations"], 1)
    ]
    result = suggest(sources)
    assert len(result.seeds) == data["expected"]["event_count"]
    assert [seed.observation_id for seed in result.seeds] == [
        item.observation_id for item in sources
    ]
    assert [seed.terms.event_type for seed in result.seeds] == ["REGULAR", "SPECIAL"]
    assert result.scope == "SUGGESTIONS_ONLY"
    assert result.hints[0].reasons == {
        "SAME_EX_DATE",
        "DIFFERENT_EVENT_TYPES",
        "DIFFERENT_REPORTED_DPS",
    }
    assert not hasattr(result.seeds[0], "event_id")


def test_nearby_dates_are_only_review_hints_not_an_event_match() -> None:
    data = CASES["DIV-016"]
    sources = [
        observation(i, row["provider"], ex_date=row["ex_date"], dps=row["dps"])
        for i, row in enumerate(data["observations"], 1)
    ]
    result = suggest(sources)
    assert len(result.seeds) == 2 and len(result.hints) == 1
    assert result.hints[0].reasons == {data["expected"]["review_reason"]}
    assert data["expected"]["auto_merge"] is False


def test_corrected_provider_event_id_is_a_possible_source_revision_not_a_merge() -> None:
    data = CASES["DIV-017"]
    sources = [
        observation(i, row["provider"], provider_event_id=row["provider_event_id"], dps=row["dps"])
        for i, row in enumerate(data["observations"], 1)
    ]
    result = suggest(sources)
    assert len(result.seeds) == data["expected"]["observation_versions"]
    assert "POSSIBLE_SOURCE_REVISION" in result.hints[0].reasons
    assert "DIFFERENT_REPORTED_DPS" in result.hints[0].reasons
    assert result.seeds[0].source_sha256 != result.seeds[1].source_sha256
    assert [seed.terms.dps for seed in result.seeds] == ["0.40000000", "0.45000000"]


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-01-08", "SAME_EX_DATE"),
        ("2026-01-09", "POSSIBLE_DATE_MISMATCH"),
        ("2026-01-11", "POSSIBLE_DATE_MISMATCH"),
        ("2026-01-12", None),
        ("2026-01-05", "POSSIBLE_DATE_MISMATCH"),
        ("2026-01-04", None),
    ],
)
def test_date_window_is_inclusive_three_days_without_implicit_matching(
    day: str, expected: str | None
) -> None:
    result = suggest([observation(2, "yahoo", ex_date=day), observation(1)])
    assert len(result.seeds) == 2
    assert [seed.observation_id.int for seed in result.seeds] == [1, 2]
    assert [hint.reasons for hint in result.hints] == ([{expected}] if expected else [])


@pytest.mark.parametrize(
    "pair",
    [
        ("0001-01-01", "0001-01-04"),
        ("9999-12-28", "9999-12-31"),
        ("2024-02-28", "2024-03-02"),
    ],
)
def test_calendar_boundaries_use_explicit_dates_without_guessing_today(
    pair: tuple[str, str],
) -> None:
    result = suggest([observation(1, ex_date=pair[0]), observation(2, "yahoo", ex_date=pair[1])])
    assert result.hints[0].reasons == {"POSSIBLE_DATE_MISMATCH"}


@pytest.mark.parametrize(
    "changes",
    [
        {"security_id": str(uuid4())},
        {"ex_date": None},
        {"ex_date": "2026-01-12"},
    ],
)
def test_different_security_missing_or_far_dates_do_not_generate_a_date_hint(
    changes: dict[str, Any],
) -> None:
    result = suggest([observation(1), observation(2, "yahoo", **changes)])
    assert len(result.seeds) == 2 and result.hints == ()


def test_reused_provider_event_id_can_signal_a_revision_even_with_missing_or_far_dates() -> None:
    first = observation(1, provider_event_id="EVT-1", ex_date=None)
    second = observation(2, provider_event_id="EVT-1", ex_date="2030-01-01")
    result = suggest([first, second])
    assert result.hints[0].reasons == {"POSSIBLE_SOURCE_REVISION"}
    assert result.seeds[0].terms.ex_date is None and result.seeds[1].terms.ex_date == "2030-01-01"


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "alphavantage"},
        {"provider_event_id": "evt-1"},
        {"provider_event_id": None},
        {"security_id": str(uuid4())},
    ],
)
def test_event_ids_never_cross_provider_security_or_exact_identifier(
    changes: dict[str, Any],
) -> None:
    first = observation(1, provider_event_id="EVT-1", ex_date=None)
    second = observation(2, **{"provider_event_id": "EVT-1", "ex_date": None, **changes})
    assert suggest([first, second]).hints == ()


def test_same_day_unknown_types_with_different_raw_amounts_stay_ambiguous() -> None:
    first = observation(1, event_type="UNKNOWN", dps="0.1")
    second = observation(2, "alphavantage", event_type="UNKNOWN", dps="0.2")
    result = suggest([first, second])
    assert len(result.seeds) == 2
    assert result.hints[0].reasons == {
        "SAME_EX_DATE",
        "DIFFERENT_REPORTED_DPS",
        "AMBIGUOUS_MULTIPLE",
    }


def test_nulls_unknown_adjustment_and_cross_currency_are_not_filled_or_converted() -> None:
    first = observation(1, dps=None, currency=None, adjustment_basis="UNKNOWN", pay_date=None)
    second = observation(2, "yahoo", dps="0", currency="HKD", adjustment_basis="SPLIT_ADJUSTED")
    result = suggest([first, second])
    assert result.seeds[0].terms.dps is None and result.seeds[0].terms.currency is None
    assert (
        result.seeds[0].terms.pay_date is None
        and result.seeds[0].terms.adjustment_basis == "UNKNOWN"
    )
    assert result.seeds[1].terms.dps == "0.00000000"
    assert (
        result.seeds[1].terms.currency == "HKD"
        and result.seeds[1].terms.adjustment_basis == "SPLIT_ADJUSTED"
    )
    assert result.hints[0].reasons == {"SAME_EX_DATE"}


def test_yahoo_wrappers_share_one_origin_but_remain_separate_observations() -> None:
    result = suggest([observation(1, "yahoo"), observation(2, "yfinance")])
    assert [seed.origin_group for seed in result.seeds] == ["YAHOO", "YAHOO"]
    assert len(result.seeds) == 2 and len(result.hints) == 1
    assert not hasattr(result, "verified") and not hasattr(result, "independent_origins")


def test_input_order_does_not_change_seed_or_hint_order_and_inputs_are_unmodified() -> None:
    sources = [
        observation(3, "yahoo", ex_date="2026-01-10"),
        observation(1),
        observation(2, "alphavantage", event_type="SPECIAL"),
    ]
    snapshots = [item.model_dump(mode="json") for item in sources]
    expected = suggest(sources)
    assert all(suggest(order) == expected for order in permutations(sources))
    assert [item.model_dump(mode="json") for item in sources] == snapshots
    with pytest.raises(FrozenInstanceError):
        expected.hints[0].left_observation_id = uuid4()  # type: ignore[misc]


@pytest.mark.parametrize(
    "failure", ["duplicate-id", "duplicate-terms", "not-reference", "bad-copy"]
)
def test_invalid_duplicate_or_forged_internal_inputs_fail_without_echo(failure: str) -> None:
    first, second = observation(1), observation(2, "yahoo")
    if failure == "duplicate-id":
        second = second.model_copy(update={"observation_id": first.observation_id})
    elif failure == "duplicate-terms":
        second = first.model_copy(update={"observation_id": second.observation_id})
    elif failure == "not-reference":
        second = cast(Any, {"private-invalid-marker": "untrusted"})
    else:
        second = second.model_copy(update={"observation_id": "private-invalid-marker"})
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_CANDIDATE_INPUT$") as error:
        suggest([first, second])
    assert "private-invalid-marker" not in str(error.value)


def test_empty_and_bounded_worst_case_return_without_unbounded_pairing() -> None:
    assert suggest([]).seeds == () and suggest([]).hints == ()
    sources = [observation(i, provider_event_id=f"EVT-{i}") for i in range(1, 101)]
    result = suggest(sources)
    assert len(result.seeds) == 100 and len(result.hints) == 4950
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_CANDIDATE_INPUT$"):
        suggest([*sources, observation(101, provider_event_id="EVT-101")])


def test_cited_evidence_is_not_an_extra_vendor_vote_or_authenticated_document() -> None:
    citation = EvidenceCitation(
        id=uuid4(), citation="Synthetic citation", reason="Explicit human quotation."
    )
    manual = DividendEvidence(
        provider_id="manual",
        security_id=SECURITY,
        provider_symbol="MANUAL",
        ex_date="2026-01-08",
        evidence=citation,
        dps="0.5",
        currency="USD",
    )
    result = suggest(
        [observation(1), CandidateObservation(observation_id=UUID(int=2), observation=manual)]
    )
    assert result.seeds[1].origin_group == "USER_VERIFIED"
    assert result.scope == "SUGGESTIONS_ONLY" and len(result.seeds) == 2
    assert not hasattr(result, "confidence") and not hasattr(result, "verified")


def test_different_reported_currencies_are_visible_not_converted() -> None:
    result = suggest([observation(1, currency="HKD"), observation(2, "yahoo", currency="USD")])
    assert result.hints[0].reasons == {"SAME_EX_DATE", "DIFFERENT_CURRENCIES"}
    assert [seed.terms.currency for seed in result.seeds] == ["HKD", "USD"]


def test_changed_raw_amount_is_not_interpreted_as_equal_or_comparable() -> None:
    first = observation(1, dps="0.50000000", adjustment_basis="AS_DECLARED")
    second = observation(2, "yahoo", dps="0.50000001", adjustment_basis="SPLIT_ADJUSTED")
    result = suggest([first, second])
    assert result.hints[0].reasons == {
        "SAME_EX_DATE",
        "DIFFERENT_REPORTED_DPS",
        "AMBIGUOUS_MULTIPLE",
    }
    assert [seed.terms.dps for seed in result.seeds] == ["0.50000000", "0.50000001"]
    assert [seed.terms.adjustment_basis for seed in result.seeds] == [
        "AS_DECLARED",
        "SPLIT_ADJUSTED",
    ]


@pytest.mark.parametrize("failure", ["float-copy", "official-attribution", "oversized-event-id"])
def test_source_revalidation_rejects_invalid_frozen_copies_safely(failure: str) -> None:
    item = observation(1)
    if failure == "float-copy":
        source = item.observation.model_copy(update={"dps": 0.5})
    elif failure == "oversized-event-id":
        source = item.observation.model_copy(update={"provider_event_id": "x" * 201})
    else:
        citation = EvidenceCitation(id=uuid4(), citation="Synthetic", reason="Synthetic assertion.")
        source = item.observation.model_copy(
            update={"provider_id": "hkex_evidence", "evidence": citation}
        )
    invalid = item.model_copy(update={"observation": source})
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_CANDIDATE_INPUT$"):
        suggest([invalid])


@pytest.mark.anyio
async def test_real_immutable_source_observations_produce_no_database_or_network_writes(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await fetched(
        book,
        payload=[
            {"date": "2026-01-08", "value": "0.6", "currency": "EUR"},
            {"date": "2026-01-10", "unadjustedValue": "0.9", "currency": "USD"},
        ],
    )
    receipt = capture_source(engine, book, page)
    with unit_of_work(engine) as con:
        sources = [
            (UUID(identifier), original_dividend(con, UUID(identifier)))
            for identifier in receipt.observation_ids
        ]
    assert all(source is not None for _, source in sources)
    refs = [
        CandidateObservation(observation_id=identifier, observation=source)
        for identifier, source in sources
        if source is not None
    ]

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Suggestions must not construct provider HTTP clients")

    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    result = suggest(refs)
    assert len(result.seeds) == 2 and result.hints[0].reasons == {
        "POSSIBLE_DATE_MISMATCH",
        "DIFFERENT_REPORTED_DPS",
    }
    terms = {seed.observation_id: seed.terms for seed in result.seeds}
    first, second = (terms[identifier] for identifier, _ in sources)
    assert first.dps == "0.60000000" and first.currency is None
    assert first.adjustment_basis == "SPLIT_ADJUSTED" and first.pay_date is None
    assert second.dps == "0.90000000" and second.currency == "USD"
    assert second.adjustment_basis == "UNKNOWN" and second.pay_date is None
    assert {seed.source_sha256 for seed in result.seeds} == {
        source.normalized_sha256 for _, source in sources if source is not None
    }
    assert database_snapshot(engine) == before
