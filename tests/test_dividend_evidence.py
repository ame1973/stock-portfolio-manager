"""Explicitly matched source evidence is not automatic event identity or verified income."""

import json
from dataclasses import FrozenInstanceError
from itertools import permutations
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from spm_api.dividend_evidence import DividendTerms, EvidenceMatch, assess
from spm_api.master_models import ProviderId
from spm_api.providers.base import DividendObservation, EvidenceCitation

SECURITY = UUID("00000000-0000-4000-8000-000000000001")
CANDIDATE = UUID("00000000-0000-4000-8000-000000000002")
CASES: dict[str, dict[str, Any]] = {
    row["id"]: row
    for row in json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "packages/finance-fixtures/dividend_provider_cases.json"
        ).read_text()
    )["cases"]
}


def terms(**changes: Any) -> DividendTerms:
    return DividendTerms.model_validate(
        {
            "security_id": SECURITY,
            "ex_date": "2026-01-08",
            "dps": "1",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            **changes,
        }
    )


def match(
    provider: ProviderId = "eodhd",
    *,
    candidate_id: UUID = CANDIDATE,
    observation_id: UUID | None = None,
    **changes: Any,
) -> EvidenceMatch:
    source = DividendObservation.model_validate(
        {
            "provider_id": provider,
            "security_id": SECURITY,
            "provider_symbol": "EXPLICIT.SOURCE",
            "ex_date": "2026-01-08",
            "dps": "1",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            **changes,
        }
    )
    return EvidenceMatch(
        candidate_id=candidate_id,
        observation_id=observation_id
        or uuid5(NAMESPACE_URL, f"{provider}:{source.normalized_sha256}"),
        observation=source,
        reason="Explicit reviewed candidate association, not a date matching rule.",
    )


@pytest.mark.parametrize("identifier", ["DIV-001", "DIV-002"])
def test_fixture_independent_origins_drive_comparison_not_wrapper_count(identifier: str) -> None:
    data = CASES[identifier]
    matches = [
        match(
            row["provider"],
            ex_date=row["ex_date"],
            dps=row["dps"],
            currency=row["currency"],
            adjustment_basis=row["basis"],
        )
        for row in data["observations"]
    ]
    result = assess(CANDIDATE, terms(ex_date="2026-08-01", dps="0.5", currency="HKD"), matches)
    assert result.status == data["expected"]["status"]
    assert len(result.independent_origins) == data["expected"]["independent_groups"]
    assert not result.conflicting_fields and not result.incomplete_reasons


def test_fixture_independent_dps_conflict_does_not_choose_the_first_provider() -> None:
    data = CASES["DIV-003"]
    matches = [
        match(
            row["provider"],
            ex_date=row["ex_date"],
            dps=row["dps"],
            currency=row["currency"],
            adjustment_basis=row["basis"],
        )
        for row in data["observations"]
    ]
    proposed = terms(ex_date="2026-03-10", dps="0.14")
    before = proposed.model_dump(), [item.model_dump() for item in matches]
    for ordered in permutations(matches):
        result = assess(CANDIDATE, proposed, ordered)
        assert result.status == data["expected"]["status"]
        assert result.conflicting_fields == {"dps"}
        assert result.eligible_observation_ids == (matches[0].observation_id,)
    assert (proposed.model_dump(), [item.model_dump() for item in matches]) == before


@pytest.mark.parametrize(
    "left,right,agree",
    [
        ("0", "0.000001", True),
        ("0", "0.00000101", False),
        ("1", "1.0001", True),
        ("1", "1.00010001", False),
        ("100", "100.01", True),
        ("100", "100.010001", False),
        ("100.010001", "100", False),
        ("0.0001", "0.000101", True),
        ("92233720368.54775807", "92233720368.54775806", True),
    ],
)
def test_exact_tolerance_is_conservative_symmetric_and_never_rounds_terms(
    left: str, right: str, agree: bool
) -> None:
    proposed = terms(dps=left)
    first, second = match(dps=left), match("alphavantage", dps=right)
    before = proposed.dps, first.observation.dps, second.observation.dps
    result = assess(CANDIDATE, proposed, [first, second])
    assert result.status == ("CORROBORATED" if agree else "CONFLICT")
    assert result.conflicting_fields == (set() if agree else {"dps"})
    assert (proposed.dps, first.observation.dps, second.observation.dps) == before


def test_nearby_values_cannot_bridge_a_nontransitive_tolerance_into_corroboration() -> None:
    result = assess(
        CANDIDATE, terms(), [match(dps="0.99995"), match("alphavantage", dps="1.00005")]
    )
    assert all(
        "dps" in item.supporting_fields and not item.conflicting_fields for item in result.checks
    )
    assert result.status == "CONFLICT" and result.conflicting_fields == {"dps"}


@pytest.mark.parametrize(
    "field,old,new",
    [
        ("ex_date", "2026-01-08", "2026-01-10"),
        ("announce_date", "2025-12-01", "2025-12-02"),
        ("record_date", "2026-01-09", "2026-01-10"),
        ("pay_date", "2026-02-01", "2026-02-02"),
        ("event_type", "REGULAR", "SPECIAL"),
        ("currency", "USD", "HKD"),
    ],
)
def test_known_field_contradictions_remain_conflicts(field: str, old: str, new: str) -> None:
    original: dict[str, Any] = {field: old}
    changed: dict[str, Any] = {field: new}
    result = assess(
        CANDIDATE,
        terms(**original),
        [match(**original), match("alphavantage", **changed)],
    )
    assert result.status == "CONFLICT" and result.conflicting_fields == {field}
    assert result.independent_origins == {"EODHD"}


def test_optional_unknown_fields_are_not_filled_or_conflated_with_conflicts() -> None:
    proposed = terms()
    result = assess(
        CANDIDATE,
        proposed,
        [match(pay_date="2026-02-01", event_type="REGULAR"), match("alphavantage")],
    )
    assert result.status == "CORROBORATED"
    assert proposed.pay_date is None and proposed.event_type == "UNKNOWN"
    assert all(
        "pay_date" not in item.supporting_fields and "event_type" not in item.supporting_fields
        for item in result.checks
    )


def test_optional_source_conflict_is_detected_even_when_proposed_date_is_unknown() -> None:
    result = assess(
        CANDIDATE,
        terms(),
        [match(pay_date="2026-02-01"), match("alphavantage", pay_date="2026-02-02")],
    )
    assert result.status == "CONFLICT" and result.conflicting_fields == {"pay_date"}


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("ex_date", None, "MISSING_EX_DATE"),
        ("dps", None, "MISSING_DPS"),
        ("currency", None, "MISSING_CURRENCY"),
        ("adjustment_basis", "UNKNOWN", "UNRESOLVED_ADJUSTMENT"),
        ("adjustment_basis", "SPLIT_ADJUSTED", "UNRESOLVED_ADJUSTMENT"),
    ],
)
def test_incomplete_proposed_terms_do_not_borrow_missing_facts_from_two_sources(
    field: str, value: str | None, reason: str
) -> None:
    proposed = terms(**{field: value})
    result = assess(CANDIDATE, proposed, [match(), match("alphavantage")])
    assert result.status == "INCOMPLETE" and reason in result.incomplete_reasons
    assert not result.independent_origins and not result.eligible_observation_ids
    assert getattr(proposed, field) == value


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("ex_date", None, "MISSING_EX_DATE"),
        ("dps", None, "MISSING_DPS"),
        ("currency", None, "MISSING_CURRENCY"),
        ("adjustment_basis", "UNKNOWN", "UNRESOLVED_ADJUSTMENT"),
        ("adjustment_basis", "SPLIT_ADJUSTED", "UNRESOLVED_ADJUSTMENT"),
    ],
)
def test_one_incomplete_assertion_cannot_vote_as_a_complete_source(
    field: str, value: str | None, reason: str
) -> None:
    changed: dict[str, Any] = {field: value}
    source = match(**changed)
    result = assess(CANDIDATE, terms(), [source])
    assert result.status == "INCOMPLETE" and reason in result.incomplete_reasons
    assert result.checks[0].incomplete_reasons == source.observation.incomplete_reasons
    assert not result.independent_origins


def test_unresolved_adjusted_amount_is_not_compared_as_declared_or_as_zero() -> None:
    data = CASES["DIV-006"]["observations"][0]
    result = assess(
        CANDIDATE,
        terms(),
        [match(data["provider"], dps=data["dps"], adjustment_basis=data["basis"])],
    )
    assert result.status == CASES["DIV-006"]["expected"]["status"]
    assert CASES["DIV-006"]["expected"]["reason"] in result.incomplete_reasons
    assert not result.conflicting_fields and "dps" not in result.checks[0].supporting_fields


def test_two_eligible_sources_can_corroborate_while_another_remains_explicitly_incomplete() -> None:
    incomplete = match("yahoo", ex_date=None, currency=None, dps="0.03", adjustment_basis="UNKNOWN")
    result = assess(CANDIDATE, terms(), [match(), match("alphavantage"), incomplete])
    assert result.status == "CORROBORATED"
    assert result.independent_origins == {"EODHD", "ALPHAVANTAGE"}
    assert result.incomplete_reasons == incomplete.observation.incomplete_reasons
    assert incomplete.observation_id not in result.eligible_observation_ids


def test_incomplete_source_does_not_hide_its_known_date_contradiction() -> None:
    result = assess(
        CANDIDATE,
        terms(pay_date="2026-02-01"),
        [
            match(),
            match("alphavantage"),
            match("yahoo", adjustment_basis="UNKNOWN", pay_date="2026-02-02"),
        ],
    )
    assert result.status == "CONFLICT" and result.conflicting_fields == {"pay_date"}
    assert "UNRESOLVED_ADJUSTMENT" in result.incomplete_reasons


def test_no_matched_evidence_is_not_a_provisional_or_zero_income_event() -> None:
    result = assess(CANDIDATE, terms(), [])
    assert result.status == "INCOMPLETE"
    assert result.incomplete_reasons == {"NO_MATCHED_EVIDENCE"}
    assert not result.eligible_observation_ids and not result.checks


def test_empty_source_currency_is_not_replaced_with_a_quote_or_candidate_currency() -> None:
    result = assess(CANDIDATE, terms(currency="USD"), [match(currency=None)])
    assert result.status == "INCOMPLETE" and "MISSING_CURRENCY" in result.incomplete_reasons
    assert "dps" not in result.checks[0].supporting_fields


def test_period_and_source_currency_labels_do_not_infer_type_currency_or_corroboration() -> None:
    first = match(source_currency="EUR", source_period="QUARTERLY", source_frequency=4)
    result = assess(CANDIDATE, terms(), [first])
    assert result.status == "PROVISIONAL" and result.independent_origins == {"EODHD"}
    assert first.observation.currency == "USD" and first.observation.event_type == "UNKNOWN"


@pytest.mark.parametrize(
    "providers",
    [
        ("manual",),
        ("hkex_evidence",),
        ("sec_evidence",),
        ("manual", "hkex_evidence", "sec_evidence"),
        ("yahoo", "yfinance", "manual"),
    ],
)
def test_manual_official_and_duplicate_yahoo_references_never_add_a_vendor_vote_or_verification(
    providers: tuple[ProviderId, ...],
) -> None:
    citation = EvidenceCitation(
        id=uuid4(),
        citation="Synthetic reference marked verified by its author",
        reason="Explicit source quotation is not independent document verification.",
    )
    result = assess(
        CANDIDATE, terms(), [match(provider, evidence=citation) for provider in providers]
    )
    assert result.status == "PROVISIONAL"
    assert result.independent_origins == ({"YAHOO"} if "yahoo" in providers else set())


def test_same_ex_date_distributions_need_distinct_explicit_candidate_associations() -> None:
    data = CASES["DIV-004"]
    other = uuid4()
    regular = match(event_type="REGULAR", dps=data["observations"][0]["dps"])
    special = match(candidate_id=other, event_type="SPECIAL", dps=data["observations"][1]["dps"])
    assert (
        assess(CANDIDATE, terms(event_type="REGULAR", dps="0.25"), [regular]).status
        == "PROVISIONAL"
    )
    assert assess(other, terms(event_type="SPECIAL", dps="0.05"), [special]).status == "PROVISIONAL"
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
        assess(CANDIDATE, terms(), [regular, special])
    assert regular.observation.ex_date == special.observation.ex_date


def test_nearby_fixture_dates_are_a_conflict_not_an_automatic_match_or_merge() -> None:
    data = CASES["DIV-016"]
    first, second = [
        match(row["provider"], ex_date=row["ex_date"], dps=row["dps"])
        for row in data["observations"]
    ]
    result = assess(CANDIDATE, terms(ex_date="2026-07-10", dps="0.1"), [first, second])
    assert result.status == "CONFLICT" and result.conflicting_fields == {"ex_date"}
    assert first.observation.ex_date == "2026-07-10" and second.observation.ex_date == "2026-07-12"


def test_corrected_provider_event_retains_both_assertions_and_requires_a_decision() -> None:
    rows = CASES["DIV-017"]["observations"]
    old, new = [
        match(row["provider"], provider_event_id=row["provider_event_id"], dps=row["dps"])
        for row in rows
    ]
    result = assess(CANDIDATE, terms(dps="0.4"), [old, new])
    assert result.status == "CONFLICT" and result.conflicting_fields == {"dps"}
    assert old.observation.provider_event_id == new.observation.provider_event_id == "EVT1"
    assert old.observation.normalized_sha256 != new.observation.normalized_sha256
    assert old.observation.dps == "0.40000000" and new.observation.dps == "0.45000000"


def test_missing_future_pay_date_alone_does_not_fill_today_or_block_provisional_terms() -> None:
    row = CASES["DIV-018"]["observations"][0]
    # Sparse fixture context is explicit; a real unknown Yahoo currency/basis never inherits it.
    source = match(
        row["provider"], ex_date=row["ex_date"], pay_date=row["pay_date"], dps=row["dps"]
    )
    proposed = terms(ex_date=row["ex_date"], dps=row["dps"])
    result = assess(CANDIDATE, proposed, [source])
    assert result.status == CASES["DIV-018"]["expected"]["status"]
    assert source.observation.pay_date is proposed.pay_date is None
    raw = match(
        "yahoo",
        ex_date=row["ex_date"],
        pay_date=None,
        dps=row["dps"],
        currency=None,
        adjustment_basis="UNKNOWN",
    )
    assert assess(CANDIDATE, proposed, [raw]).status == "INCOMPLETE"


def test_assessment_and_trace_are_invariant_under_every_input_order() -> None:
    matches = [match(), match("alphavantage"), match("yahoo"), match("manual")]
    expected = assess(CANDIDATE, terms(), matches)
    for ordered in permutations(matches):
        assert assess(CANDIDATE, terms(), ordered) == expected
    assert expected.independent_origins == {"EODHD", "ALPHAVANTAGE", "YAHOO"}


@pytest.mark.parametrize(
    "change", ["candidate", "security", "duplicate", "duplicate provider", "too many"]
)
def test_invalid_evidence_scope_fails_closed_without_dumping_source_content(change: str) -> None:
    first = match()
    if change == "candidate":
        matches = [match(candidate_id=uuid4())]
    elif change == "security":
        matches = [match(security_id=uuid4())]
    elif change == "duplicate":
        matches = [first, first]
    elif change == "duplicate provider":
        matches = [first, match("alphavantage", observation_id=first.observation_id)]
    else:
        matches = [match(observation_id=UUID(int=index)) for index in range(101)]
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
        assess(CANDIDATE, terms(), matches)


def test_maximum_bounded_evidence_never_inflates_one_vendor_into_multiple_origins() -> None:
    result = assess(
        CANDIDATE, terms(), [match(observation_id=UUID(int=index)) for index in range(100)]
    )
    assert result.status == "PROVISIONAL" and result.independent_origins == {"EODHD"}
    assert len(result.checks) == len(result.eligible_observation_ids) == 100


@pytest.mark.parametrize(
    "amount", [0.5, True, 1, "1e-8", "NaN", "-1", "0.000000001", "92233720368.54775808"]
)
def test_proposed_dps_cannot_coerce_round_or_overflow(amount: Any) -> None:
    with pytest.raises(ValidationError):
        terms(dps=amount)


@pytest.mark.parametrize(
    "changes",
    [
        {"ex_date": "2026-02-30"},
        {"currency": "EUR"},
        {"event_type": "RECEIVED"},
        {"adjustment_basis": "VERIFIED"},
        {"quote_currency": "HKD"},
        {"owner_id": str(SECURITY)},
    ],
)
def test_proposed_terms_reject_invalid_calendar_unknown_types_and_ownership_claims(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        terms(**changes)


@pytest.mark.parametrize("forged", ["terms", "source", "reason", "identifier", "type"])
def test_frozen_model_copy_does_not_bypass_comparison_validation(forged: str) -> None:
    proposed, item = terms(), match()
    if forged == "terms":
        proposed = proposed.model_copy(update={"dps": "0.000000001"})
    elif forged == "source":
        item = item.model_copy(
            update={"observation": item.observation.model_copy(update={"currency": "EUR"})}
        )
    elif forged == "reason":
        item = item.model_copy(update={"reason": "Private synthetic reason\0must not be dumped"})
    elif forged == "identifier":
        item = item.model_copy(update={"observation_id": "not a UUID"})
    else:
        item = cast(Any, {"private": "not a validated match"})
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
        assess(CANDIDATE, proposed, [item])


def test_invalid_outer_types_are_fixed_errors_not_arbitrary_attribute_or_iteration_failures() -> (
    None
):
    invalid: tuple[tuple[object, object, object], ...] = (
        (None, terms(), ()),
        (CANDIDATE, None, ()),
        (CANDIDATE, terms(), None),
    )
    for candidate, proposed, matches in invalid:
        with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
            assess(cast(Any, candidate), cast(Any, proposed), cast(Any, matches))


def test_inputs_results_and_private_match_reasons_are_frozen_and_not_dumped_in_repr() -> None:
    proposed, item = terms(), match()
    with pytest.raises(ValidationError):
        cast(Any, proposed).dps = "2"
    with pytest.raises(ValidationError):
        cast(Any, item).reason = "Another source association reason."
    result = assess(CANDIDATE, proposed, [item])
    with pytest.raises(FrozenInstanceError):
        cast(Any, result).status = "VERIFIED"
    assert item.reason not in repr(item) and "EXPLICIT.SOURCE" not in repr(item)
    assert item.reason not in repr(result)
    assert result.checks[0].supporting_fields == {"ex_date", "dps", "currency", "adjustment_basis"}
