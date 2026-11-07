"""A cited, complete split-chain assertion derives units without changing source or ledger facts."""

from datetime import date
from decimal import Decimal as D
from itertools import permutations
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from spm_api.decimal_types import INT64_MAX, from_scaled
from spm_api.dividend_basis import SplitBasisAssertion, SplitReference, normalize_basis
from spm_api.dividend_evidence import EvidenceMatch, assess
from spm_api.providers.base import SplitObservation
from spm_api.replay import Event, Split, Trade, eligible_quantity, replay
from tests.test_dividend_evidence import CANDIDATE, CASES, SECURITY, match, terms


def adjusted(**changes: Any) -> EvidenceMatch:
    values: dict[str, Any] = {"dps": "0.6", "adjustment_basis": "SPLIT_ADJUSTED", **changes}
    return match("yahoo", **values)


def reference(**changes: Any) -> SplitReference:
    return SplitReference.model_validate(
        {
            "id": uuid4(),
            "root_id": uuid4(),
            "security_id": SECURITY,
            "version": 1,
            "action_type": "SPLIT",
            "effective_date": "2026-02-01",
            "ratio_numerator": "3",
            "ratio_denominator": "2",
            "status": "CONFIRMED",
            "lifecycle_status": "CURRENT",
            **changes,
        }
    )


def assertion(source: EvidenceMatch, **changes: Any) -> SplitBasisAssertion:
    return SplitBasisAssertion.model_validate(
        {
            "id": uuid4(),
            "observation_id": source.observation_id,
            "source_sha256": source.observation.normalized_sha256,
            "security_id": source.observation.security_id,
            "ex_date": source.observation.ex_date,
            "share_basis_date": "2026-03-01",
            "source_basis": "AFTER_EFFECTIVE_DATE_ACTIONS",
            "target_basis": "PRE_EX_DATE_EVENTS",
            "complete": True,
            "split_only": True,
            "splits": [reference()],
            "citation": "Synthetic declared complete split-basis evidence page 2",
            "reason": "Synthetic share-basis scope; no document verification or authorization.",
            **changes,
        }
    )


def test_div_005_three_for_two_restores_exact_dps_without_mutating_the_observation() -> None:
    data = CASES["DIV-005"]
    row = data["observations"][0]
    source = adjusted(ex_date=row["ex_date"], dps=row["dps"], adjustment_basis=row["basis"])
    proof = assertion(
        source,
        splits=[
            reference(
                ratio_numerator=data["split"]["ratio_numerator"],
                ratio_denominator=data["split"]["ratio_denominator"],
            )
        ],
    )
    before = source.model_dump(), source.observation.normalized_sha256
    result = normalize_basis(source.observation_id, source.observation, proof)
    assert result.dps == data["expected"]["normalized_unadjusted_dps"]
    assert result.adjustment_basis == "AS_DECLARED" and not result.issues
    assert result.assertion_id == proof.id
    assert (source.model_dump(), source.observation.normalized_sha256) == before
    assert source.observation.adjustment_basis == "SPLIT_ADJUSTED"
    assert source.observation.incomplete_reasons == {"UNRESOLVED_ADJUSTMENT"}


@pytest.mark.parametrize(
    "amount,kind,numerator,denominator,basis,day,expected",
    [
        ("0.9", "REVERSE_SPLIT", 1, 3, "2026-03-01", "2026-02-01", "0.30000000"),
        ("0.9", "SPLIT", 3, 2, "2025-12-31", "2026-01-01", "0.60000000"),
        ("0.3", "REVERSE_SPLIT", 1, 3, "2025-12-31", "2026-01-01", "0.90000000"),
        ("0.6", "SPLIT", 3, 2, "2026-01-08", "2026-01-08", "0.90000000"),
        ("0", "SPLIT", INT64_MAX, 1, "2026-03-01", "2026-02-01", "0.00000000"),
        (
            from_scaled(INT64_MAX - 1, 8),
            "SPLIT",
            str(INT64_MAX),
            str(INT64_MAX - 1),
            "2026-03-01",
            "2026-02-01",
            from_scaled(INT64_MAX, 8),
        ),
    ],
)
def test_exact_historical_and_forward_basis_directions(
    amount: str, kind: str, numerator: Any, denominator: Any, basis: str, day: str, expected: str
) -> None:
    source = adjusted(dps=amount)
    proof = assertion(
        source,
        share_basis_date=basis,
        splits=[
            reference(
                action_type=kind,
                ratio_numerator=numerator,
                ratio_denominator=denominator,
                effective_date=day,
            )
        ],
    )
    result = normalize_basis(source.observation_id, source.observation, proof)
    assert result.dps == expected and result.adjustment_basis == "AS_DECLARED" and not result.issues


def test_same_day_boundary_matches_strict_pre_ex_date_eligibility_not_closing_holdings() -> None:
    ex_date = date(2026, 1, 8)
    events = [
        Event("buy", date(2026, 1, 1), 1, Trade("A", str(SECURITY), "BUY", D("10"), D("100"))),
        Event("split", ex_date, 2, Split(str(SECURITY), 3, 2)),
    ]
    source = adjusted()
    proof = assertion(
        source, share_basis_date="2026-01-08", splits=[reference(effective_date="2026-01-08")]
    )
    result = normalize_basis(source.observation_id, source.observation, proof)
    eligible = eligible_quantity(events, "A", str(SECURITY), ex_date)
    after = replay(events, as_of=ex_date)[("A", str(SECURITY))].quantity
    assert eligible == D("10") and after == D("15")
    assert result.dps is not None and source.observation.dps is not None
    assert eligible * D(result.dps) == after * D(source.observation.dps) == D("9")


@pytest.mark.parametrize(
    "basis,day",
    [
        ("2026-03-01", "2026-01-07"),
        ("2026-03-01", "2026-03-02"),
        ("2025-12-31", "2025-12-31"),
        ("2025-12-31", "2026-01-08"),
        ("2025-12-31", "2025-12-30"),
        ("2025-12-31", "2026-01-09"),
    ],
)
def test_actions_outside_exact_basis_scope_are_not_silently_clipped(basis: str, day: str) -> None:
    source = adjusted()
    with pytest.raises(ValidationError):
        assertion(source, share_basis_date=basis, splits=[reference(effective_date=day)])


@pytest.mark.parametrize(
    "basis,day",
    [
        ("2026-03-01", "2026-01-08"),
        ("2026-03-01", "2026-03-01"),
        ("2025-12-31", "2026-01-01"),
        ("2025-12-31", "2026-01-07"),
    ],
)
def test_inclusive_and_exclusive_action_date_boundaries_are_explicit(basis: str, day: str) -> None:
    source = adjusted()
    proof = assertion(source, share_basis_date=basis, splits=[reference(effective_date=day)])
    expected = "0.90000000" if basis >= "2026-01-08" else "0.40000000"
    assert normalize_basis(source.observation_id, source.observation, proof).dps == expected


@pytest.mark.parametrize("day", ["0001-01-01", "9999-12-31"])
def test_extreme_calendar_dates_need_no_previous_day_or_year_underflow(day: str) -> None:
    source = adjusted(ex_date=day)
    proof = assertion(source, share_basis_date=day, splits=[reference(effective_date=day)])
    assert normalize_basis(source.observation_id, source.observation, proof).dps == "0.90000000"


def test_explicit_complete_empty_chain_is_not_the_default_for_missing_proof() -> None:
    source = adjusted()
    missing = normalize_basis(source.observation_id, source.observation)
    proof = assertion(source, splits=[])
    declared = normalize_basis(source.observation_id, source.observation, proof)
    assert missing.dps == "0.60000000" and missing.adjustment_basis == "SPLIT_ADJUSTED"
    assert missing.issues == {"UNRESOLVED_ADJUSTMENT"} and missing.assertion_id is None
    assert declared.dps == "0.60000000" and declared.adjustment_basis == "AS_DECLARED"
    assert not declared.issues and declared.assertion_id == proof.id


def test_entire_rational_chain_is_reduced_before_precision_or_overflow_checks() -> None:
    source = adjusted(dps="0.00000001")
    splits = [
        reference(action_type="REVERSE_SPLIT", ratio_numerator=1, ratio_denominator=3),
        reference(ratio_numerator=3, ratio_denominator=1, effective_date="2026-02-02"),
    ]
    proof = assertion(source, splits=splits)
    for ordered in permutations(splits):
        changed = proof.model_copy(update={"splits": tuple(ordered)})
        result = normalize_basis(source.observation_id, source.observation, changed)
        assert result.dps == "0.00000001" and not result.issues


@pytest.mark.parametrize(
    "amount,numerator,denominator,kind,issue",
    [
        (from_scaled(INT64_MAX, 8), 2, 1, "SPLIT", "NORMALIZED_DPS_OVERFLOW"),
        ("0.00000001", 3, 2, "SPLIT", "NORMALIZED_DPS_PRECISION"),
        ("0.00000001", 1, INT64_MAX, "REVERSE_SPLIT", "NORMALIZED_DPS_PRECISION"),
    ],
)
def test_nonrepresentable_derived_dps_is_unresolved_without_rounding_or_zero(
    amount: str, numerator: int, denominator: int, kind: str, issue: str
) -> None:
    source = adjusted(dps=amount)
    proof = assertion(
        source,
        splits=[
            reference(action_type=kind, ratio_numerator=numerator, ratio_denominator=denominator)
        ],
    )
    before = source.observation.model_dump()
    result = normalize_basis(source.observation_id, source.observation, proof)
    assert result.dps == source.observation.dps and result.adjustment_basis == "SPLIT_ADJUSTED"
    assert result.issues == {"UNRESOLVED_ADJUSTMENT", issue} and result.assertion_id == proof.id
    assert source.observation.model_dump() == before


@pytest.mark.parametrize("basis", ["UNKNOWN", "SPLIT_ADJUSTED", "AS_DECLARED"])
def test_missing_assertion_never_invents_a_chain_or_changes_known_basis(basis: str) -> None:
    source = adjusted(adjustment_basis=basis)
    result = normalize_basis(source.observation_id, source.observation)
    assert result.dps == source.observation.dps and result.adjustment_basis == basis
    assert result.issues == (set() if basis == "AS_DECLARED" else {"UNRESOLVED_ADJUSTMENT"})


@pytest.mark.parametrize("basis", ["UNKNOWN", "AS_DECLARED"])
def test_adjustment_assertion_cannot_relabel_an_unknown_or_declared_raw_source(basis: str) -> None:
    source = adjusted(adjustment_basis=basis)
    proof = assertion(source)
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_BASIS_ASSERTION$"):
        normalize_basis(source.observation_id, source.observation, proof)


def test_missing_dps_remains_null_even_with_an_explicit_complete_basis_assertion() -> None:
    source = adjusted(dps=None)
    proof = assertion(source)
    result = normalize_basis(source.observation_id, source.observation, proof)
    assert result.dps is None and result.adjustment_basis == "SPLIT_ADJUSTED"
    assert result.issues == {"UNRESOLVED_ADJUSTMENT"} and result.assertion_id == proof.id


@pytest.mark.parametrize(
    "field,value",
    [
        ("complete", False),
        ("complete", 1),
        ("complete", "true"),
        ("split_only", False),
        ("split_only", 1),
        ("source_basis", "FETCH_DATE"),
        ("target_basis", "AFTER_EX_DATE_OPENING"),
        ("share_basis_date", "2026-02-30"),
        ("source_sha256", "A" * 64),
        ("source_sha256", "0" * 63),
        ("reason", "Synthetic\0basis explanation text"),
        ("citation", "Bad\0reference"),
    ],
)
def test_basis_assertion_boundary_rejects_partial_unknown_or_malformed_claims(
    field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        assertion(adjusted(), **{field: value})


@pytest.mark.parametrize(
    "field",
    ["share_basis_date", "source_basis", "target_basis", "complete", "split_only", "splits"],
)
def test_basis_scope_and_completeness_must_be_supplied_not_defaulted(field: str) -> None:
    values = assertion(adjusted()).model_dump()
    del values[field]
    with pytest.raises(ValidationError):
        SplitBasisAssertion.model_validate(values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ratio_numerator", 0),
        ("ratio_numerator", True),
        ("ratio_numerator", 1.5),
        ("ratio_numerator", "3.0"),
        ("ratio_numerator", str(INT64_MAX + 1)),
        ("ratio_denominator", 0),
        ("version", 0),
        ("version", True),
        ("version", "1"),
        ("status", "NEEDS_REVIEW"),
        ("status", "VOID"),
        ("lifecycle_status", "SUPERSEDED"),
        ("action_type", "STOCK_DIVIDEND"),
        ("ratio_numerator", 2),
    ],
)
def test_only_exact_current_confirmed_directional_split_references_are_accepted(
    field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        reference(**{field: value})


def test_reverse_direction_and_one_to_one_are_not_guessed() -> None:
    with pytest.raises(ValidationError):
        reference(action_type="REVERSE_SPLIT", ratio_numerator=3, ratio_denominator=2)
    with pytest.raises(ValidationError):
        reference(action_type="REVERSE_SPLIT", ratio_numerator=1, ratio_denominator=1)


def test_provider_only_split_observation_is_not_an_adopted_action_reference() -> None:
    factor = SplitObservation(
        provider_id="eodhd",
        security_id=SECURITY,
        provider_symbol="EXPLICIT.SOURCE",
        effective_date="2026-02-01",
        ratio_numerator=3,
        ratio_denominator=2,
    )
    with pytest.raises(ValidationError):
        SplitReference.model_validate(factor.model_dump())


@pytest.mark.parametrize("duplicate", ["id", "root"])
def test_an_action_or_another_revision_of_the_same_root_cannot_be_counted_twice(
    duplicate: str,
) -> None:
    first = reference()
    other = first if duplicate == "id" else first.model_copy(update={"id": uuid4(), "version": 2})
    with pytest.raises(ValidationError):
        assertion(adjusted(), splits=[first, other])


def test_cross_security_and_unbounded_action_lists_are_rejected() -> None:
    source = adjusted()
    with pytest.raises(ValidationError):
        assertion(source, splits=[reference(security_id=uuid4())])
    with pytest.raises(ValidationError):
        assertion(source, splits=[reference() for _ in range(101)])
    proof = assertion(source, splits=[reference() for _ in range(100)])
    assert len(proof.splits) == 100


@pytest.mark.parametrize(
    "change", ["observation", "hash", "security", "ex-date", "source changed", "date missing"]
)
def test_assertion_pins_exact_original_observation_identity_hash_security_and_event_day(
    change: str,
) -> None:
    source = adjusted()
    proof = assertion(source)
    if change == "observation":
        proof = proof.model_copy(update={"observation_id": uuid4()})
    elif change == "hash":
        proof = proof.model_copy(update={"source_sha256": "0" * 64})
    elif change == "security":
        source = adjusted(security_id=uuid4())
    elif change == "ex-date":
        source = adjusted(ex_date="2026-01-09")
    elif change == "source changed":
        source = source.model_copy(
            update={"observation": source.observation.model_copy(update={"source_currency": "EUR"})}
        )
    else:
        source = source.model_copy(
            update={"observation": source.observation.model_copy(update={"ex_date": None})}
        )
        proof = proof.model_copy(update={"source_sha256": source.observation.normalized_sha256})
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_BASIS_ASSERTION$"):
        normalize_basis(source.observation_id, source.observation, proof)


@pytest.mark.parametrize("change", ["flag", "direction", "source", "type"])
def test_frozen_internal_copies_are_revalidated_and_do_not_dump_private_inputs(change: str) -> None:
    source = adjusted()
    proof = assertion(source)
    if change == "flag":
        proof = proof.model_copy(update={"complete": False})
    elif change == "direction":
        split = proof.splits[0].model_copy(update={"ratio_numerator": 1})
        proof = proof.model_copy(update={"splits": (split,)})
    elif change == "source":
        source = source.model_copy(
            update={"observation": source.observation.model_copy(update={"dps": 0.6})}
        )
    else:
        proof = cast(Any, {"private": "not a checked assertion"})
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_BASIS_ASSERTION$"):
        normalize_basis(source.observation_id, source.observation, proof)


def test_invalid_outer_identity_or_source_types_fail_with_a_fixed_code() -> None:
    source = adjusted()
    for identifier, observation in [(None, source.observation), (source.observation_id, None)]:
        with pytest.raises(ValueError, match="^INVALID_DIVIDEND_BASIS_ASSERTION$"):
            normalize_basis(cast(Any, identifier), cast(Any, observation))


def test_basis_assertions_are_frozen_and_keep_private_citation_values_out_of_repr() -> None:
    source = adjusted()
    proof = assertion(source, url="https://example.test/private-reference?marker=synthetic")
    with pytest.raises(ValidationError):
        cast(Any, proof).complete = False
    with pytest.raises(ValidationError):
        cast(Any, proof.splits[0]).ratio_numerator = 5
    assert proof.reason not in repr(proof) and proof.citation not in repr(proof)
    assert "marker=synthetic" not in repr(proof)


def with_basis(source: EvidenceMatch, proof: SplitBasisAssertion) -> EvidenceMatch:
    return EvidenceMatch.model_validate(
        {**source.model_dump(), "basis_assertion": proof.model_dump()}
    )


def test_comparison_accepts_div_005_only_with_a_pinned_explicit_basis_assertion() -> None:
    source = adjusted()
    peer = match(dps="0.9")
    proposed = terms(dps="0.9")
    before = source.observation.model_dump(), source.observation.normalized_sha256
    assert assess(CANDIDATE, proposed, [source, peer]).status == "PROVISIONAL"
    proof = assertion(source)
    matched = with_basis(source, proof)
    result = assess(CANDIDATE, proposed, [matched, peer])
    assert result.status == CASES["DIV-005"]["expected"]["status"]
    assert result.independent_origins == {"EODHD", "YAHOO"}
    check = next(row for row in result.checks if row.observation_id == source.observation_id)
    assert check.comparison_dps == "0.90000000" and check.comparison_basis == "AS_DECLARED"
    assert check.basis_assertion_id == proof.id and "dps" in check.supporting_fields
    assert not result.incomplete_reasons
    assert (source.observation.model_dump(), source.observation.normalized_sha256) == before
    assert (
        source.observation.dps == "0.60000000"
        and source.observation.adjustment_basis == "SPLIT_ADJUSTED"
    )


def test_normalized_yahoo_wrappers_still_share_one_origin_and_basis_citation_is_not_a_vote() -> (
    None
):
    first = adjusted()
    second = match("yfinance", dps="0.6", adjustment_basis="SPLIT_ADJUSTED")
    result = assess(
        CANDIDATE,
        terms(dps="0.9"),
        [with_basis(source, assertion(source)) for source in (first, second)],
    )
    assert result.status == "PROVISIONAL" and result.independent_origins == {"YAHOO"}


def test_normalization_does_not_fill_source_currency_from_a_proposal_or_source_label() -> None:
    source = adjusted(currency=None, source_currency="EUR")
    proof = assertion(source)
    result = assess(CANDIDATE, terms(dps="0.9"), [with_basis(source, proof)])
    assert result.status == "INCOMPLETE" and result.incomplete_reasons == {
        "MISSING_CURRENCY",
        "NO_SUPPORTING_EVIDENCE",
    }
    assert (
        result.checks[0].comparison_dps == "0.90000000"
        and result.checks[0].comparison_basis == "AS_DECLARED"
    )
    assert source.observation.currency is None and source.observation.source_currency == "EUR"
    assert "UNRESOLVED_ADJUSTMENT" in source.observation.incomplete_reasons
    assert not result.independent_origins


def test_unrepresentable_basis_keeps_issues_and_does_not_supply_a_second_origin() -> None:
    source = adjusted(dps="0.00000001")
    proof = assertion(source)
    result = assess(
        CANDIDATE, terms(dps="0.00000001"), [with_basis(source, proof), match(dps="0.00000001")]
    )
    assert result.status == "PROVISIONAL" and result.independent_origins == {"EODHD"}
    assert {"NORMALIZED_DPS_PRECISION", "UNRESOLVED_ADJUSTMENT"} <= result.incomplete_reasons
    check = next(row for row in result.checks if row.observation_id == source.observation_id)
    assert check.basis_assertion_id == proof.id and check.comparison_basis == "SPLIT_ADJUSTED"
    assert check.comparison_dps == "0.00000001" and "dps" not in check.supporting_fields


def test_stale_or_forged_basis_in_a_match_is_a_fixed_comparison_input_error() -> None:
    source = adjusted()
    proof = assertion(source, source_sha256="0" * 64)
    matched = with_basis(source, proof)
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
        assess(CANDIDATE, terms(dps="0.9"), [matched])
    forged = matched.model_copy(
        update={"basis_assertion": proof.model_copy(update={"complete": False})}
    )
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
        assess(CANDIDATE, terms(), [forged])


def test_one_assertion_id_cannot_claim_two_different_pinned_observations() -> None:
    first, second = adjusted(), match("alphavantage", dps="0.6", adjustment_basis="SPLIT_ADJUSTED")
    proof = assertion(first)
    other = assertion(second, id=proof.id)
    with pytest.raises(ValueError, match="^INVALID_DIVIDEND_COMPARISON$"):
        assess(CANDIDATE, terms(dps="0.9"), [with_basis(first, proof), with_basis(second, other)])


def test_conditional_comparison_trace_remains_order_independent_and_known_conflicts_win() -> None:
    source = adjusted(pay_date="2026-03-01")
    matched = with_basis(source, assertion(source))
    peers = [matched, match(dps="0.9", pay_date="2026-03-02"), match("alphavantage", dps="0.9")]
    expected = assess(CANDIDATE, terms(dps="0.9"), peers)
    assert expected.status == "CONFLICT" and expected.conflicting_fields == {"pay_date"}
    for ordered in permutations(peers):
        assert assess(CANDIDATE, terms(dps="0.9"), ordered) == expected
