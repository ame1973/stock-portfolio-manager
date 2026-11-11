"""Synthetic arithmetic checks for retained projection snapshots, without an economic grant."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from spm_api.dividend_event_service import EventRevision
from spm_api.dividend_evidence import DividendTerms
from spm_api.dividend_math import accrue as original_accrue
from spm_api.dividend_projection import (
    ProjectionError,
    ProjectionScope,
    ProjectionVersion,
    select_current,
)
from spm_api.dividend_projection_arithmetic import checked_accrual

D = Decimal
OWNER, EVENT, REVISION = (UUID(int=number) for number in (1, 2, 3))
ACCOUNT = UUID(int=0xABCDEF)


def event(**changes: Any) -> EventRevision:
    values = {
        "owner_id": OWNER,
        "event_id": EVENT,
        "revision_id": REVISION,
        "revision_no": 1,
        "terms": DividendTerms(
            security_id=UUID(int=5),
            ex_date="2026-01-08",
            dps="1",
            currency="USD",
            adjustment_basis="AS_DECLARED",
        ),
        "review_status": "PROVISIONAL",
        "manual_locked": False,
        "decision_kind": "MANUAL",
        "candidate_revision_id": None,
        "citation": "Synthetic event reference",
        "source_url": None,
        "reason": "Synthetic projection source",
        "request_id": UUID(int=6),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "evidence": (),
        **changes,
    }
    return EventRevision.model_validate(values)


def snapshot(**changes: Any) -> dict[str, Any]:
    return {
        "owner_id": str(OWNER),
        "event_id": str(EVENT),
        "revision_id": str(REVISION),
        "account_id": str(ACCOUNT),
        "state": "EXPECTED",
        "eligibility_rule": "ORDINARY_PRE_EX_DATE",
        "currency": "USD",
        "eligible_shares_e8": 100_000_000,
        "gross_micro": 1_000_000,
        "tax_micro": 0,
        "fee_micro": 1,
        "expected_net_micro": 1_000_000,
        "rounding_delta_micro": 1,
        "fx_rate_hkd_e12": 7_800_000_000_000,
        "fx_source": "MANUAL:synthetic",
        "expected_hkd_micro": 7_799_996,
        "tax_rate_snapshot_ppm": 0,
        "fee_original_micro": 1,
        "fee_original_currency": "HKD",
        "fee_rate_event_e12": 500_000_000_000,
        "fee_rate_source": "MANUAL:synthetic",
        "quality": "OK",
        **changes,
    }


def test_unrounded_original_fee_reconstructs_every_component() -> None:
    actual = checked_accrual(snapshot(), event())
    assert actual is not None
    assert actual.fee_local == D("0.000001")
    assert actual.net_local == D("1.000000")
    assert actual.booked_hkd == D("7.799996")
    assert actual.rounding_delta_local == D("0.000001")


def test_internally_consistent_early_rounded_snapshot_is_not_verified_arithmetic() -> None:
    rounded = original_accrue(
        str(EVENT), str(ACCOUNT), "USD", D("1"), D("1"), D("0"), D("0.000001"), D("7.8")
    )
    version = ProjectionVersion(
        UUID(int=7), EVENT, REVISION, 1, ACCOUNT, 1, None, "EXPECTED", rounded
    )
    assert select_current([ProjectionScope(EVENT, REVISION, 1, ACCOUNT)], [version]) == (version,)
    wrong = snapshot(
        expected_net_micro=999_999, rounding_delta_micro=0, expected_hkd_micro=7_799_992
    )
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(wrong, event())


def test_raw_negative_net_cannot_be_hidden_by_rounding_to_zero() -> None:
    wrong = snapshot(
        eligible_shares_e8=100,
        gross_micro=1,
        fee_rate_event_e12=1_000_001_000_000,
        expected_net_micro=0,
        rounding_delta_micro=0,
        expected_hkd_micro=0,
        quality="OK",
    )
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(wrong, event())
    corrected = {**wrong, "quality": "NEEDS_REVIEW"}
    result = checked_accrual(corrected, event())
    assert result is not None and result.net_local == 0 and result.quality == "NEEDS_REVIEW"


def test_missing_valuation_fx_and_zero_foreign_fee_are_independent() -> None:
    result = checked_accrual(
        snapshot(
            fee_original_micro=0,
            fee_micro=0,
            fee_rate_event_e12=None,
            fee_rate_source=None,
            fx_rate_hkd_e12=None,
            fx_source=None,
            expected_hkd_micro=None,
            rounding_delta_micro=0,
            quality="MISSING_FX",
        ),
        event(),
    )
    assert result is not None and result.net_local == D("1.000000")
    assert result.booked_hkd is None and result.quality == "MISSING_FX"


def test_hkd_event_with_foreign_fee_uses_explicit_fee_rate_and_identity_hkd_fx() -> None:
    terms = event().terms.model_copy(update={"currency": "HKD"})
    values = snapshot(
        currency="HKD",
        fee_original_currency="USD",
        fee_rate_event_e12=7_800_000_000_000,
        fee_micro=8,
        expected_net_micro=999_992,
        rounding_delta_micro=0,
        fx_rate_hkd_e12=1_000_000_000_000,
        fx_source="IDENTITY",
        expected_hkd_micro=999_992,
    )
    actual = checked_accrual(values, event(terms=terms))
    assert actual is not None
    assert actual.fee_local == D("0.000008")
    assert actual.net_local == actual.booked_hkd == D("0.999992")
    for change in (
        {"fx_rate_hkd_e12": None},
        {"fx_rate_hkd_e12": 2_000_000_000_000},
        {"fx_source": "MANUAL:synthetic"},
    ):
        with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
            checked_accrual({**values, **change}, event(terms=terms))


def test_same_currency_fee_requires_identity_snapshot() -> None:
    values = snapshot(
        fee_original_currency="USD",
        fee_rate_event_e12=1_000_000_000_000,
        fee_rate_source="IDENTITY",
        expected_net_micro=999_999,
        rounding_delta_micro=0,
        expected_hkd_micro=7_799_992,
    )
    assert checked_accrual(values, event()) is not None
    for change in (
        {"fee_rate_event_e12": None},
        {"fee_rate_event_e12": 500_000_000_000},
        {"fee_rate_source": "MANUAL:synthetic"},
    ):
        with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
            checked_accrual({**values, **change}, event())


def test_missing_hkd_fx_cannot_be_recorded_as_ok_or_as_zero() -> None:
    values = snapshot(fx_rate_hkd_e12=None, fx_source=None, expected_hkd_micro=None)
    for change in ({"quality": "OK"}, {"quality": "MISSING_FX", "expected_hkd_micro": 0}):
        with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
            checked_accrual({**values, **change}, event())
    actual = checked_accrual({**values, "quality": "MISSING_FX"}, event())
    assert actual is not None and actual.booked_hkd is None
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual({**values, "fx_source": "MANUAL:missing"}, event())


@pytest.mark.parametrize("field", ["fx_source", "fee_rate_source"])
@pytest.mark.parametrize("source", ["", " IDENTITY", "synthetic\x00", "x" * 101])
def test_invalid_fee_or_valuation_provenance_is_not_trusted(field: str, source: str) -> None:
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(**{field: source}), event())


@pytest.mark.parametrize("quality", ["NEEDS_REVIEW", "MISSING_QUOTE", "NOT_COMPUTABLE"])
def test_stricter_recorded_quality_is_preserved(quality: str) -> None:
    actual = checked_accrual(snapshot(quality=quality), event())
    assert actual is not None and actual.quality == quality


@pytest.mark.parametrize("state", ["NO_POSITION", "NEEDS_REVIEW", "CANCELLED"])
def test_non_amount_state_remains_non_amount(state: str) -> None:
    values = snapshot(state=state)
    for field in (
        "eligible_shares_e8",
        "gross_micro",
        "tax_micro",
        "fee_micro",
        "expected_net_micro",
        "rounding_delta_micro",
        "fx_rate_hkd_e12",
        "fx_source",
        "expected_hkd_micro",
        "tax_rate_snapshot_ppm",
        "fee_original_micro",
        "fee_original_currency",
        "fee_rate_event_e12",
        "fee_rate_source",
        "quality",
    ):
        values[field] = None
    assert checked_accrual(values, event()) is None
    values["gross_micro"] = 0
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(values, event())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_id", str(UUID(int=50))),
        ("event_id", str(UUID(int=51))),
        ("revision_id", str(UUID(int=52))),
        ("account_id", "not-a-uuid"),
        ("account_id", str(ACCOUNT).upper()),
        ("state", "UNKNOWN"),
        ("eligibility_rule", "REQUIRES_REVIEW"),
        ("currency", "HKD"),
        ("eligible_shares_e8", 0),
        ("eligible_shares_e8", True),
        ("eligible_shares_e8", 100_000_000.0),
        ("eligible_shares_e8", 2**63),
        ("gross_micro", 1_000_001),
        ("tax_micro", 1),
        ("fee_micro", 2),
        ("expected_net_micro", 999_999),
        ("rounding_delta_micro", 0),
        ("fx_rate_hkd_e12", 0),
        ("fx_source", "IDENTITY"),
        ("expected_hkd_micro", 7_799_995),
        ("tax_rate_snapshot_ppm", 1_000_001),
        ("fee_original_micro", 2),
        ("fee_original_currency", "EUR"),
        ("fee_original_currency", "USD"),
        ("fee_rate_event_e12", None),
        ("fee_rate_event_e12", 0),
        ("fee_rate_source", None),
        ("fee_rate_source", "IDENTITY"),
        ("quality", "MISSING_FX"),
        ("quality", "VERIFIED"),
    ],
)
def test_inconsistent_snapshot_values_fail_without_disclosing_private_input(
    field: str, value: object
) -> None:
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(**{field: value}), event())


@pytest.mark.parametrize("status", ["CONFLICT", "INCOMPLETE", "WITHDRAWN"])
def test_unresolved_or_withdrawn_source_cannot_validate_expected_amount(status: str) -> None:
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(), event(review_status=status))


def test_unresolved_adjustment_basis_cannot_validate_expected_amount() -> None:
    terms = event().terms.model_copy(update={"adjustment_basis": "UNKNOWN"})
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(), event(terms=terms, review_status="INCOMPLETE"))


def test_a_missing_field_and_unvalidated_event_are_not_private_error_messages() -> None:
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(), None)  # type: ignore[arg-type]
    values = snapshot()
    del values["fee_micro"]
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(values, event())
    different_event = event(revision_id=UUID(int=100))
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(), different_event)


@pytest.mark.parametrize(
    ("field", "value"),
    [("revision_no", 0), ("terms", None), ("review_status", "private-invalid-status")],
)
def test_mutated_event_models_fail_closed(field: str, value: object) -> None:
    malformed = event().model_copy(update={field: value})
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(), malformed)


@pytest.mark.parametrize("dps", ["private-not-a-decimal", "NaN", "1e2", 1.0])
def test_unvalidated_dps_never_becomes_a_stored_amount(dps: object) -> None:
    source = event()
    malformed = source.model_copy(update={"terms": source.terms.model_copy(update={"dps": dps})})
    with pytest.raises(ProjectionError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        checked_accrual(snapshot(), malformed)
