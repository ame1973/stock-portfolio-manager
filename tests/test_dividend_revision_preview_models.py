"""Exact hypothetical revision contracts do not establish eligibility or payment."""

from copy import deepcopy
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from spm_api.dividend_revision_preview_models import PreviewPage, PreviewRequest
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_revision_preview import calculate, proposal, source_estimate
from tests.test_transfers import graph as graph


def estimate(account: str) -> dict[str, Any]:
    return {
        "account_id": account,
        "tax_rate": "0",
        "fee": "0",
        "fee_currency": "USD",
        "fee_rate_event": None,
        "fee_rate_source": None,
        "fx_rate_hkd": "8",
        "fx_source": "synthetic private source",
    }


def terms() -> dict[str, Any]:
    return {
        "ex_date": "2026-01-08",
        "as_of": "2026-01-08",
        "eligibility_rule": "ORDINARY_PRE_EX_DATE",
        "dps": "1.1",
        "currency": "USD",
        "adjustment_basis": "AS_DECLARED",
    }


def response() -> dict[str, Any]:
    account, revision = str(uuid4()), str(uuid4())
    return {
        "event_id": str(uuid4()),
        "revision_id": revision,
        "revision_no": 2,
        "security_id": str(uuid4()),
        "terms": terms(),
        "items": [
            {
                "account_id": account,
                "portfolio_id": str(uuid4()),
                "broker_id": str(uuid4()),
                "account_status": "ACTIVE",
                "broker_status": "ACTIVE",
                "account_version": "2",
                "broker_version": "3",
                "portfolio_version": "4",
                "portfolio_data_revision": "5",
                "retired_trade_scope": "NO_RETIRED_TRADES",
                "creation_audit_scope": "NO_RECORDED_CREATION_GAP_REVIEW",
                "eligibility_state": "RECORDED_PRE_EX_DATE",
                "recorded_quantity": "10",
                "eligibility_reasons": [],
                "pending_action_ids": [],
                "estimate": estimate(account),
                "previous_projection_id": str(uuid4()),
                "previous_revision_id": revision,
                "previous_as_of": "2026-01-08",
                "previous_currency": "USD",
                "previous_expected": "10",
                "proposed_amounts": {
                    "currency": "USD",
                    "gross_local": "11",
                    "tax_local": "0",
                    "fee_local": "0",
                    "net_local": "11",
                    "rounding_delta_local": "0",
                    "estimated_hkd": "88",
                    "quality": "OK",
                },
                "expected_delta": "1",
                "recorded_received": "6",
                "recorded_outstanding": "5",
                "active_receipts": "1",
                "void_receipts": "0",
                "unverified_origins": "0",
                "unavailable_cash_links": "0",
                "comparison_quality": "OK",
                "state": "RECORDED_PARTIAL",
            }
        ],
        "snapshot_token": "a" * 64,
        "next_after_account_id": None,
        "scope": "HYPOTHETICAL_RECORDED_COMPARISON_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
    }


def request() -> dict[str, Any]:
    value = response()
    return {
        "revision_id": value["revision_id"],
        "proposal": {**terms(), "accounts": [value["items"][0]["estimate"]]},
        "limit": 25,
        "after_account_id": None,
        "snapshot_token": None,
    }


def test_exact_hypothetical_amounts_and_private_repr() -> None:
    value = response()
    page = PreviewPage.model_validate(value)
    item = page.items[0]
    assert item.proposed_amounts is not None
    assert item.proposed_amounts.net_local == "11.000000"
    assert item.expected_delta == "1.000000"
    assert item.recorded_received == "6.000000" and item.recorded_outstanding == "5.000000"
    assert item.recorded_quantity == "10.00000000"
    assert page.scope == "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"
    assert page.eligibility_scope == "NOT_ESTABLISHED"
    assert page.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert "synthetic private source" not in repr(page)
    assert value["items"][0]["account_id"] not in repr(page)
    assert PreviewRequest.model_validate(request()).proposal.dps == "1.10000000"


@pytest.mark.parametrize("limit", [0, 101, True, 1.5, "10"])
def test_page_limit_is_strict_and_bounded(limit: object) -> None:
    with pytest.raises(ValidationError):
        PreviewRequest.model_validate({**request(), "limit": limit})


@pytest.mark.parametrize(
    "changes",
    [
        {"after_account_id": str(uuid4())},
        {"snapshot_token": "a" * 64},
        {"after_account_id": str(uuid4()), "snapshot_token": "private-not-a-token"},
        {"after_account_id": str(uuid4()), "snapshot_token": "A" * 64},
        {"extra": "private input"},
    ],
)
def test_request_cursor_is_explicit_paired_and_redacted(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as caught:
        PreviewRequest.model_validate({**request(), **changes})
    assert "private input" not in str(caught.value)
    assert "private-not-a-token" not in str(caught.value)


def test_valid_next_page_cursor_is_not_an_authorization() -> None:
    value = request()
    value.update(after_account_id=str(uuid4()), snapshot_token="f" * 64, limit=1)
    parsed = PreviewRequest.model_validate(value)
    assert parsed.after_account_id is not None and parsed.snapshot_token == "f" * 64
    assert parsed.limit == 1


def test_forged_model_instances_are_revalidated_without_private_input_in_errors() -> None:
    page = PreviewPage.model_validate(response())
    wrong_account = page.items[0].model_copy(update={"account_id": uuid4()})
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(page.model_copy(update={"items": [wrong_account]}))
    request_model = PreviewRequest.model_validate(request())
    forged_proposal = request_model.proposal.model_copy(update={"dps": 1.1})
    forged_request = request_model.model_copy(update={"proposal": forged_proposal})
    with pytest.raises(ValidationError):
        PreviewRequest.model_validate(forged_request)
    bad_estimate = page.items[0].estimate.model_copy(update={"fx_source": " private source "})
    forged_item = page.items[0].model_copy(update={"estimate": bad_estimate})
    with pytest.raises(ValidationError) as caught:
        PreviewPage.model_validate(page.model_copy(update={"items": [forged_item]}))
    assert " private source " not in str(caught.value)
    invalid_number = page.items[0].estimate.model_copy(update={"tax_rate": 0.5})
    forged_item = page.items[0].model_copy(update={"estimate": invalid_number})
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(page.model_copy(update={"items": [forged_item]}))


@pytest.mark.parametrize(
    "changes",
    [
        {"revision_no": True},
        {"revision_no": 0},
        {"revision_no": 2_147_483_648},
        {"scope": "CONFIRMED"},
        {"payment_scope": "VERIFIED"},
        {"eligibility_scope": "GRANTED"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "CURRENT"},
        {"snapshot_token": "not-valid"},
        {"next_after_account_id": str(uuid4())},
        {"extra": "private input"},
    ],
)
def test_page_rejects_unbounded_versions_false_grants_and_bad_cursor(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PreviewPage.model_validate({**response(), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"account_version": "0"},
        {"broker_version": 3},
        {"portfolio_version": "01"},
        {"portfolio_data_revision": "9223372036854775808"},
        {"recorded_quantity": 10.0},
        {"recorded_quantity": "-1"},
        {"recorded_quantity": "0.000000001"},
        {"account_status": "DELETED"},
        {"broker_status": "UNVERIFIED"},
        {"eligibility_state": "VERIFIED"},
        {"active_receipts": 1},
        {"void_receipts": "-1"},
        {"unverified_origins": "2"},
        {"unavailable_cash_links": "2"},
        {"previous_projection_id": None},
        {"previous_revision_id": None},
        {"previous_as_of": None},
        {"previous_currency": None},
        {"previous_expected": None},
        {"proposed_amounts": None},
        {"expected_delta": "2"},
        {"recorded_received": None},
        {"recorded_outstanding": None},
        {"state": "ESTIMATES_UNAVAILABLE"},
        {"recorded_received": "7"},
        {"recorded_outstanding": "4"},
        {"comparison_quality": "MISSING_FX"},
        {"state": "RECORDED_RECEIVED"},
        {"eligibility_reasons": ["UNALLOCATED_ACTION"]},
        {"pending_action_ids": [str(uuid4())]},
        {"estimate": estimate(str(uuid4()))},
    ],
)
def test_item_rejects_inconsistent_scope_quantities_counts_and_arithmetic(
    changes: dict[str, object],
) -> None:
    value = response()
    value["items"][0].update(changes)
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("currency", "HKD"),
        ("gross_local", "11.000001"),
        ("tax_local", "1"),
        ("fee_local", "1"),
        ("net_local", "11.000001"),
        ("rounding_delta_local", "0.000001"),
        ("estimated_hkd", "87.999999"),
        ("quality", "NEEDS_REVIEW"),
    ],
)
def test_proposed_amounts_are_recomputed_not_trusted(field: str, wrong: str) -> None:
    value = response()
    value["items"][0]["proposed_amounts"][field] = wrong
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_missing_hkd_fx_keeps_native_comparison_and_separate_quality() -> None:
    value = response()
    item = value["items"][0]
    item["estimate"].update(fx_rate_hkd=None, fx_source=None)
    item["proposed_amounts"].update(estimated_hkd=None, quality="MISSING_FX")
    item["comparison_quality"] = "MISSING_FX"
    actual = PreviewPage.model_validate(value).items[0]
    assert actual.proposed_amounts is not None
    assert actual.proposed_amounts.estimated_hkd is None
    assert actual.proposed_amounts.quality == actual.comparison_quality == "MISSING_FX"
    assert actual.recorded_outstanding == "5.000000"


def test_no_receipt_and_mixed_currency_do_not_become_zero() -> None:
    for state, active, quality in (
        ("NO_RECORDED_ACTIVE_RECEIPTS", "0", None),
        ("CURRENCY_REVIEW", "1", "NEEDS_REVIEW"),
    ):
        value = response()
        item = value["items"][0]
        item.update(
            state=state,
            active_receipts=active,
            comparison_quality=quality,
            recorded_received=None,
            recorded_outstanding=None,
        )
        actual = PreviewPage.model_validate(value).items[0]
        assert actual.recorded_received is actual.recorded_outstanding is None
        item["recorded_received"] = "0"
        with pytest.raises(ValidationError):
            PreviewPage.model_validate(value)


def test_stale_projection_keeps_previous_value_but_not_delta() -> None:
    value = response()
    value["items"][0].update(previous_revision_id=str(uuid4()), expected_delta=None)
    page = PreviewPage.model_validate(value)
    assert page.items[0].previous_expected == "10.000000"
    assert page.items[0].expected_delta is None
    value["items"][0]["expected_delta"] = "1"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_page_items_are_ordered_unique_and_cursor_matches_last_item() -> None:
    value = response()
    first = value["items"][0]
    second = deepcopy(first)
    second["account_id"] = str(uuid4())
    second["estimate"]["account_id"] = second["account_id"]
    value["items"] = sorted([first, second], key=lambda item: item["account_id"])
    value["next_after_account_id"] = value["items"][-1]["account_id"]
    assert len(PreviewPage.model_validate(value).items) == 2
    value["items"].reverse()
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)
    value["items"] = [first, first]
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_explicit_unknown_estimate_requires_no_hypothetical_amount() -> None:
    value = response()
    item = value["items"][0]
    item["estimate"]["tax_rate"] = None
    item.update(
        state="ESTIMATES_UNAVAILABLE",
        proposed_amounts=None,
        expected_delta=None,
        recorded_received=None,
        recorded_outstanding=None,
        comparison_quality=None,
    )
    actual = PreviewPage.model_validate(value).items[0]
    assert actual.estimate.tax_rate is None and actual.proposed_amounts is None
    item["state"] = "NO_RECORDED_ACTIVE_RECEIPTS"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def no_comparison(item: dict[str, Any], state: str) -> None:
    item.update(
        state=state,
        proposed_amounts=None,
        expected_delta=None,
        recorded_received=None,
        recorded_outstanding=None,
        comparison_quality=None,
    )


@pytest.mark.parametrize(
    ("kind", "state"),
    [
        ("basis", "EVENT_BASIS_REVIEW"),
        ("retired", "LEDGER_AUDIT_REVIEW"),
        ("creation", "LEDGER_AUDIT_REVIEW"),
        ("eligibility", "ELIGIBILITY_REVIEW"),
        ("zero", "NO_RECORDED_POSITION"),
        ("zero-with-cash", "NO_RECORDED_POSITION_CASH_REVIEW"),
        ("zero-with-missing-void", "NO_RECORDED_POSITION_CASH_REVIEW"),
        ("tax", "ESTIMATES_UNAVAILABLE"),
        ("fee", "ESTIMATES_UNAVAILABLE"),
        ("fee-currency", "ESTIMATES_UNAVAILABLE"),
        ("fee-fx", "FEE_FX_UNAVAILABLE"),
    ],
)
def test_each_unavailable_reason_is_distinct_and_fail_closed(kind: str, state: str) -> None:
    value = response()
    item = value["items"][0]
    if kind == "basis":
        value["terms"]["adjustment_basis"] = "UNKNOWN"
    elif kind == "retired":
        item["retired_trade_scope"] = "RETIRED_TRADE_HISTORY_UNVERIFIED"
    elif kind == "creation":
        item["creation_audit_scope"] = "CREATION_GAP_REVIEWED_UNVERIFIED"
    elif kind == "eligibility":
        value["terms"]["eligibility_rule"] = "REQUIRES_REVIEW"
        item.update(
            eligibility_state="NEEDS_REVIEW",
            recorded_quantity=None,
            eligibility_reasons=["ELIGIBILITY_RULE_REQUIRES_REVIEW"],
        )
    elif kind.startswith("zero"):
        item["recorded_quantity"] = "0"
        if kind == "zero":
            item["active_receipts"] = "0"
        elif kind == "zero-with-missing-void":
            item.update(active_receipts="0", void_receipts="1", unavailable_cash_links="1")
    elif kind in {"tax", "fee"}:
        item["estimate"]["tax_rate" if kind == "tax" else "fee"] = None
    elif kind == "fee-currency":
        item["estimate"].update(fee=None, fee_currency=None)
    else:
        item["estimate"].update(fee="1", fee_currency="HKD")
    no_comparison(item, state)
    assert PreviewPage.model_validate(value).items[0].state == state
    item["recorded_received"] = "0"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_pending_actions_are_explicit_and_not_attached_to_other_reasons() -> None:
    value = response()
    item = value["items"][0]
    item.update(
        eligibility_state="NEEDS_REVIEW",
        recorded_quantity=None,
        eligibility_reasons=["UNALLOCATED_ACTION"],
        pending_action_ids=[str(uuid4())],
    )
    no_comparison(item, "ELIGIBILITY_REVIEW")
    assert len(PreviewPage.model_validate(value).items[0].pending_action_ids) == 1
    changes: tuple[dict[str, Any], ...] = (
        {"pending_action_ids": []},
        {"pending_action_ids": item["pending_action_ids"] * 2},
        {"eligibility_reasons": []},
        {"eligibility_reasons": ["SPLIT_FRACTIONAL_RESIDUE"]},
        {"recorded_quantity": "0"},
    )
    for updates in changes:
        other = deepcopy(value)
        other["items"][0].update(updates)
        with pytest.raises(ValidationError):
            PreviewPage.model_validate(other)
    item.update(eligibility_reasons=["SPLIT_FRACTIONAL_RESIDUE"], pending_action_ids=[])
    assert PreviewPage.model_validate(value).items[0].state == "ELIGIBILITY_REVIEW"


def test_upcoming_timing_and_absent_old_projection_are_explicit() -> None:
    value = response()
    value["terms"].update(ex_date="2026-01-09", as_of="2026-01-08")
    item = value["items"][0]
    item.update(
        eligibility_state="UPCOMING_RECORDED_HOLDINGS",
        previous_projection_id=None,
        previous_revision_id=None,
        previous_as_of=None,
        previous_currency=None,
        previous_expected=None,
        expected_delta=None,
    )
    actual = PreviewPage.model_validate(value).items[0]
    assert actual.eligibility_state == "UPCOMING_RECORDED_HOLDINGS"
    item["eligibility_state"] = "RECORDED_PRE_EX_DATE"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)
    item["eligibility_state"] = "UPCOMING_RECORDED_HOLDINGS"
    value["terms"]["eligibility_rule"] = "REQUIRES_REVIEW"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_nonexpected_old_projection_has_identity_but_no_invented_amount() -> None:
    value = response()
    item = value["items"][0]
    item.update(previous_currency=None, previous_expected=None, expected_delta=None)
    actual = PreviewPage.model_validate(value).items[0]
    assert actual.previous_projection_id is not None and actual.previous_expected is None
    item["previous_expected"] = "0"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


@pytest.mark.parametrize(
    ("state", "received", "outstanding", "quality"),
    [
        ("RECORDED_PARTIAL", "0", "11", "OK"),
        ("RECORDED_RECEIVED", "11", "0", "OK"),
        ("RECORDED_OVER_RECEIVED", "12", "-1", "NEEDS_REVIEW"),
        ("RECORDED_NEEDS_REVIEW", "-1", "12", "NEEDS_REVIEW"),
        ("RECORDED_NEEDS_REVIEW", "6", "5", "NEEDS_REVIEW"),
    ],
)
def test_recorded_cash_states_preserve_signed_values_and_quality(
    state: str, received: str, outstanding: str, quality: str
) -> None:
    value = response()
    item = value["items"][0]
    item.update(
        state=state,
        recorded_received=received,
        recorded_outstanding=outstanding,
        comparison_quality=quality,
    )
    assert PreviewPage.model_validate(value).items[0].state == state
    item["comparison_quality"] = "MISSING_FX"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_negative_net_is_reviewed_and_not_treated_as_overreceipt() -> None:
    value = response()
    item = value["items"][0]
    item["estimate"]["fee"] = "12"
    item["proposed_amounts"].update(
        fee_local="12", net_local="-1", estimated_hkd="-8", quality="NEEDS_REVIEW"
    )
    item.update(
        expected_delta="-11",
        recorded_outstanding="-7",
        state="RECORDED_NEEDS_REVIEW",
        comparison_quality="NEEDS_REVIEW",
    )
    actual = PreviewPage.model_validate(value).items[0]
    assert actual.proposed_amounts is not None and actual.proposed_amounts.net_local == "-1.000000"
    item["state"] = "RECORDED_OVER_RECEIVED"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_missing_cash_link_blocks_both_zero_and_mixed_currency_interpretations() -> None:
    value = response()
    item = value["items"][0]
    item.update(
        unavailable_cash_links="1",
        state="CASH_LINK_UNAVAILABLE",
        recorded_received=None,
        recorded_outstanding=None,
        comparison_quality=None,
    )
    assert PreviewPage.model_validate(value).items[0].state == "CASH_LINK_UNAVAILABLE"
    item["state"] = "CURRENCY_REVIEW"
    item["comparison_quality"] = "NEEDS_REVIEW"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_fee_and_hkd_use_independent_exact_rates_without_early_rounding() -> None:
    value = response()
    item = value["items"][0]
    item["estimate"].update(
        fee="1", fee_currency="HKD", fee_rate_event="0.123456789123", fee_rate_source="explicit"
    )
    item["proposed_amounts"].update(
        fee_local="0.123457", net_local="10.876543", estimated_hkd="87.012346"
    )
    item.update(expected_delta="0.876543", recorded_outstanding="4.876543")
    assert PreviewPage.model_validate(value).items[0].expected_delta == "0.876543"
    item["proposed_amounts"]["estimated_hkd"] = "87.012344"
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)


def test_empty_page_and_maximum_page_size() -> None:
    value = response()
    value["items"] = []
    assert PreviewPage.model_validate(value).items == []
    value["next_after_account_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)
    template = response()["items"][0]
    value["revision_id"] = template["previous_revision_id"]
    items = []
    for _ in range(101):
        item = deepcopy(template)
        item["account_id"] = str(uuid4())
        item["estimate"]["account_id"] = item["account_id"]
        items.append(item)
    value["items"] = sorted(items, key=lambda item: item["account_id"])
    value["next_after_account_id"] = None
    with pytest.raises(ValidationError):
        PreviewPage.model_validate(value)
    value["items"] = value["items"][:100]
    assert len(PreviewPage.model_validate(value).items) == 100


def public_snapshot(result: Any) -> PreviewPage:
    """Map an actual synthetic internal result to the proposed contract for integration tests."""
    estimates = {value.account_id: value for value in result.proposal.accounts}
    comparisons = {value.account_id: value for value in result.accounts}
    items = []
    for portfolio in result.snapshot.portfolios:
        for pin, observation in zip(portfolio.accounts, portfolio.book.accounts, strict=True):
            current = comparisons[pin.account_id]
            amount = current.proposed_accrual
            items.append(
                {
                    "account_id": pin.account_id,
                    "portfolio_id": portfolio.portfolio.id,
                    "broker_id": pin.broker_id,
                    "account_status": pin.account_status,
                    "broker_status": pin.broker_status,
                    "account_version": str(pin.account_version),
                    "broker_version": str(pin.broker_version),
                    "portfolio_version": str(portfolio.portfolio.version),
                    "portfolio_data_revision": str(portfolio.portfolio.data_revision),
                    "retired_trade_scope": portfolio.book.trade_history_audit_scope,
                    "creation_audit_scope": portfolio.book.trade_creation_audit_scope,
                    "eligibility_state": observation.state,
                    "recorded_quantity": observation.recorded_quantity,
                    "eligibility_reasons": list(observation.reasons),
                    "pending_action_ids": list(observation.pending_action_ids),
                    "estimate": estimates[pin.account_id].model_dump(),
                    "previous_projection_id": current.previous_projection_id,
                    "previous_revision_id": current.previous_revision_id,
                    "previous_as_of": current.previous_as_of.isoformat()
                    if current.previous_as_of is not None
                    else None,
                    "previous_currency": current.previous_currency,
                    "previous_expected": None
                    if current.previous_expected is None
                    else format(current.previous_expected, "f"),
                    "proposed_amounts": None
                    if amount is None
                    else {
                        "currency": amount.currency,
                        "gross_local": format(amount.gross_local, "f"),
                        "tax_local": format(amount.tax_local, "f"),
                        "fee_local": format(amount.fee_local, "f"),
                        "net_local": format(amount.net_local, "f"),
                        "rounding_delta_local": format(amount.rounding_delta_local, "f"),
                        "estimated_hkd": format(amount.booked_hkd, "f")
                        if amount.booked_hkd is not None
                        else None,
                        "quality": amount.quality,
                    },
                    "expected_delta": None
                    if current.expected_delta is None
                    else format(current.expected_delta, "f"),
                    "recorded_received": None
                    if current.recorded_received is None
                    else format(current.recorded_received, "f"),
                    "recorded_outstanding": None
                    if current.recorded_outstanding is None
                    else format(current.recorded_outstanding, "f"),
                    "active_receipts": str(current.active_receipts),
                    "void_receipts": str(current.void_receipts),
                    "unverified_origins": str(current.unverified_origins),
                    "unavailable_cash_links": str(current.unavailable_cash_links),
                    "comparison_quality": current.comparison_quality,
                    "state": current.state,
                }
            )
    retained = result.snapshot.retained
    return PreviewPage.model_validate(
        {
            "event_id": retained.event_id,
            "revision_id": retained.revision_id,
            "revision_no": retained.revision_no,
            "security_id": result.snapshot.security_id,
            "terms": result.proposal.model_dump(exclude={"accounts"}),
            "items": sorted(items, key=lambda value: str(value["account_id"])),
            "snapshot_token": "a" * 64,
            "next_after_account_id": None,
            "scope": result.scope,
            "history_completeness": result.history_completeness,
            "current_use_scope": result.current_use_scope,
            "eligibility_scope": result.eligibility_scope,
            "payment_scope": result.payment_scope,
        }
    )


@pytest.mark.anyio
async def test_real_synthetic_preview_maps_into_contract_without_writes_or_upstream(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    tax, fee = settings(engine, prepared, rate="0", fee="0")
    save(engine, prepared, intent(engine, prepared, tax, fee), uuid4())
    direct_receipt(engine, prepared, received_micro=6_000_000)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("network")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("network")),
    ):
        actual = public_snapshot(calculate(engine, prepared, proposal(graph)))
    by_account = {str(value.account_id): value for value in actual.items}
    held = by_account[graph["source"]["id"]]
    empty = by_account[graph["target"]["id"]]
    assert held.previous_expected == "10.000000" and held.expected_delta == "1.000000"
    assert held.recorded_received == "6.000000" and held.recorded_outstanding == "5.000000"
    assert held.state == "RECORDED_PARTIAL" and empty.state == "NO_RECORDED_POSITION"
    assert empty.proposed_amounts is None and empty.recorded_received is None
    assert database_snapshot(engine) == before


@pytest.mark.anyio
async def test_actual_unknown_fx_review_and_different_basis_fit_contract(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(engine, prepared, received_micro=6_000_000)
    before = database_snapshot(engine)
    value = source_estimate(graph, fx_rate_hkd=None, fx_source=None)
    actual = public_snapshot(calculate(engine, prepared, value))
    held = next(item for item in actual.items if str(item.account_id) == graph["source"]["id"])
    assert held.proposed_amounts is not None
    assert held.proposed_amounts.quality == held.comparison_quality == "MISSING_FX"
    assert held.proposed_amounts.estimated_hkd is None and held.recorded_outstanding == "5.000000"
    basis_result = calculate(engine, prepared, proposal(graph, adjustment_basis="UNKNOWN"))
    basis = public_snapshot(basis_result)
    assert all(item.state == "EVENT_BASIS_REVIEW" for item in basis.items)
    review = public_snapshot(
        calculate(engine, prepared, proposal(graph, eligibility_rule="REQUIRES_REVIEW"))
    )
    assert all(item.state == "ELIGIBILITY_REVIEW" for item in review.items)
    assert database_snapshot(engine) == before
