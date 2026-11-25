"""Preview a possible event revision without changing recorded financial facts."""

from dataclasses import replace
from datetime import date
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_scope as revision_scope
import tests.test_dividend_direct_receipt_history as direct_fixture
from spm_api.db import unit_of_work
from spm_api.dividend_math import accrue
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import create, database_snapshot
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import manual, submit
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import receipt
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


def proposal(graph: dict[str, Any], **changes: Any) -> preview.RevisionProposal:
    return preview.RevisionProposal.model_validate(
        {
            "ex_date": "2026-01-08",
            "as_of": "2026-01-08",
            "eligibility_rule": "ORDINARY_PRE_EX_DATE",
            "dps": "1.1",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            "accounts": [
                {
                    "account_id": graph[name]["id"],
                    "tax_rate": "0",
                    "fee": "0",
                    "fee_currency": "USD",
                    "fee_rate_event": None,
                    "fee_rate_source": None,
                    "fx_rate_hkd": "8",
                    "fx_source": "Synthetic explicit historical FX",
                }
                for name in ("source", "target")
            ],
            **changes,
        }
    )


def source_estimate(graph: dict[str, Any], **changes: Any) -> preview.RevisionProposal:
    original = proposal(graph).model_dump(mode="python")
    accounts = [dict(row) for row in original["accounts"]]
    accounts[0].update(changes)
    return preview.RevisionProposal.model_validate({**original, "accounts": accounts})


def calculate(engine: Engine, prepared: dict[str, Any], value: preview.RevisionProposal) -> Any:
    with unit_of_work(engine) as con:
        return preview.read(con, prepared["owner"], prepared["event"].event_id, value)


def item(result: Any, graph: dict[str, Any], name: str = "source") -> Any:
    return next(row for row in result.accounts if row.account_id == UUID(graph[name]["id"]))


async def test_hypothetical_10_6_11_5_keeps_old_cash_and_event_unchanged(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    tax, fee = settings(engine, prepared, rate="0", fee="0")
    old = save(engine, prepared, intent(engine, prepared, tax, fee), uuid4())
    direct_receipt(engine, prepared, received_micro=6_000_000)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("network")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("network")),
    ):
        result = calculate(engine, prepared, proposal(graph))
    assert result is not None and result.scope == "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"
    actual = item(result, graph)
    assert actual.previous_projection_id == old.version.id
    assert actual.previous_expected == Decimal("10.000000")
    assert actual.proposed_accrual.net_local == Decimal("11.000000")
    assert actual.expected_delta == Decimal("1.000000")
    assert actual.recorded_received == Decimal("6.000000")
    assert actual.recorded_outstanding == Decimal("5.000000")
    assert actual.state == "RECORDED_PARTIAL" and actual.active_receipts == 1
    assert result.history_completeness == "UNVERIFIED"
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert "11.000000" not in repr(result) and graph["source"]["id"] not in repr(result)
    assert database_snapshot(engine) == before


async def test_missing_fee_or_tax_never_becomes_zero(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    original = proposal(graph).model_dump(mode="python")
    for field in ("tax_rate", "fee"):
        accounts = [dict(row) for row in original["accounts"]]
        accounts[0][field] = None
        value = preview.RevisionProposal.model_validate({**original, "accounts": accounts})
        before = database_snapshot(engine)
        result = calculate(engine, prepared, value)
        assert result is not None
        actual = item(result, graph)
        assert actual.state == "ESTIMATES_UNAVAILABLE" and actual.proposed_accrual is None
        assert actual.recorded_received is actual.recorded_outstanding is None
        assert database_snapshot(engine) == before


async def test_no_receipt_and_recorded_zero_position_are_not_confirmed_zero_payment(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    result = calculate(engine, prepared, proposal(graph))
    assert result is not None
    held, empty = item(result, graph), item(result, graph, "target")
    assert held.state == "NO_RECORDED_ACTIVE_RECEIPTS" and held.proposed_accrual is not None
    assert held.recorded_received is held.recorded_outstanding is None
    assert empty.state == "NO_RECORDED_POSITION" and empty.proposed_accrual is None
    assert empty.recorded_received is empty.recorded_outstanding is None
    assert database_snapshot(engine) == before


async def test_new_event_currency_must_not_mix_or_relabel_recorded_cash(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(engine, prepared, received_micro=6_000_000)
    value = proposal(
        graph,
        currency="HKD",
        accounts=[
            {
                "account_id": graph[name]["id"],
                "tax_rate": "0",
                "fee": "0",
                "fee_currency": "HKD",
                "fee_rate_event": None,
                "fee_rate_source": None,
                "fx_rate_hkd": None,
                "fx_source": None,
            }
            for name in ("source", "target")
        ],
    )
    before = database_snapshot(engine)
    result = calculate(engine, prepared, value)
    assert result is not None
    actual = item(result, graph)
    assert actual.state == "CURRENCY_REVIEW"
    assert actual.proposed_accrual.net_local == Decimal("11.000000")
    assert actual.recorded_received is actual.recorded_outstanding is None
    assert database_snapshot(engine) == before


async def test_every_account_requires_one_explicit_estimate(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    original = proposal(graph).model_dump(mode="python")
    before = database_snapshot(engine)
    missing = preview.RevisionProposal.model_validate(
        {**original, "accounts": original["accounts"][:1]}
    )
    with pytest.raises(
        preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
    ):
        calculate(engine, prepared, missing)
    with pytest.raises(ValidationError):
        preview.RevisionProposal.model_validate(
            {**original, "accounts": [original["accounts"][0]] * 2}
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("amount", "expected_state", "quality", "outstanding"),
    [
        (0, "RECORDED_PARTIAL", "OK", "11.000000"),
        (11_000_000, "RECORDED_RECEIVED", "OK", "0.000000"),
        (12_000_000, "RECORDED_OVER_RECEIVED", "NEEDS_REVIEW", "-1.000000"),
        (-1_000_000, "RECORDED_NEEDS_REVIEW", "NEEDS_REVIEW", "12.000000"),
    ],
)
async def test_recorded_receipt_states_keep_exact_signed_amounts(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    amount: int,
    expected_state: str,
    quality: str,
    outstanding: str,
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(engine, prepared, received_micro=amount)
    before = database_snapshot(engine)
    result = calculate(engine, prepared, proposal(graph))
    actual = item(result, graph)
    assert actual.state == expected_state and actual.comparison_quality == quality
    assert actual.recorded_received == Decimal(amount).scaleb(-6)
    assert actual.recorded_outstanding == Decimal(outstanding)
    assert actual.proposed_accrual.quality == "OK"
    assert database_snapshot(engine) == before


async def test_unknown_hkd_fx_and_unverified_legacy_origin_are_not_invented(
    engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000, older=True)
    value = source_estimate(graph, fx_rate_hkd=None, fx_source=None)
    before = database_snapshot(engine)
    result = calculate(engine, book, value)
    actual = item(result, graph)
    assert actual.proposed_accrual.quality == actual.comparison_quality == "MISSING_FX"
    assert actual.proposed_accrual.booked_hkd is None
    assert actual.recorded_received == Decimal("6.000000")
    assert actual.recorded_outstanding == Decimal("5.000000")
    assert actual.unverified_origins == 1 and result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
    assert database_snapshot(engine) == before


async def test_foreign_fee_needs_its_own_explicit_rate_but_zero_does_not_invent_one(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(engine, prepared, received_micro=6_000_000)
    before = database_snapshot(engine)
    missing = calculate(engine, prepared, source_estimate(graph, fee="1", fee_currency="HKD"))
    assert item(missing, graph).state == "FEE_FX_UNAVAILABLE"
    assert item(missing, graph).proposed_accrual is None
    zero = calculate(engine, prepared, source_estimate(graph, fee="0", fee_currency="HKD"))
    assert item(zero, graph).state == "RECORDED_PARTIAL"
    assert item(zero, graph).proposed_accrual.net_local == Decimal("11.000000")
    converted = calculate(
        engine,
        prepared,
        source_estimate(
            graph,
            fee="1",
            fee_currency="HKD",
            fee_rate_event="0.123456789123",
            fee_rate_source="Synthetic fee conversion",
        ),
    )
    actual = item(converted, graph)
    assert actual.proposed_accrual.fee_local == Decimal("0.123457")
    assert actual.proposed_accrual.net_local == Decimal("10.876543")
    assert actual.recorded_outstanding == Decimal("4.876543")
    assert database_snapshot(engine) == before


async def test_cash_without_a_structural_link_blocks_comparison_not_the_hypothetical_accrual(
    engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    receipt(engine, book, cash=False)
    before = database_snapshot(engine)
    actual = item(calculate(engine, book, proposal(graph)), graph)
    assert actual.state == "CASH_LINK_UNAVAILABLE" and actual.unavailable_cash_links == 1
    assert actual.proposed_accrual.net_local == Decimal("11.000000")
    assert actual.recorded_received is actual.recorded_outstanding is None
    assert actual.comparison_quality is None
    assert database_snapshot(engine) == before


async def test_direct_void_and_cash_on_zero_recorded_position_are_not_hidden(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    original = direct_receipt(engine, prepared)
    with patch.object(direct_fixture, "OWNER", prepared["owner"]):
        direct_fixture.void_receipt(engine, original)
    target = {**prepared, "account_id": UUID(graph["target"]["id"])}
    direct_receipt(engine, target, received_micro=2_000_000)
    before = database_snapshot(engine)
    result = calculate(engine, prepared, proposal(graph))
    first, second = item(result, graph), item(result, graph, "target")
    assert first.active_receipts == 0 and first.void_receipts == 1
    assert first.state == "NO_RECORDED_ACTIVE_RECEIPTS" and first.recorded_received is None
    assert second.active_receipts == 1 and second.state == "NO_RECORDED_POSITION_CASH_REVIEW"
    assert second.proposed_accrual is second.recorded_received is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("basis", ["SPLIT_ADJUSTED", "UNKNOWN"])
async def test_unresolved_proposed_basis_does_not_generate_an_expected_amount(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], basis: str
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(engine, prepared)
    before = database_snapshot(engine)
    result = calculate(engine, prepared, proposal(graph, adjustment_basis=basis))
    actual = item(result, graph)
    assert actual.state == "EVENT_BASIS_REVIEW" and actual.proposed_accrual is None
    assert actual.active_receipts == 1 and actual.recorded_received is None
    assert database_snapshot(engine) == before


async def test_required_eligibility_review_and_future_cutoff_stay_explicit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    pending = calculate(engine, prepared, proposal(graph, eligibility_rule="REQUIRES_REVIEW"))
    assert item(pending, graph).state == "ELIGIBILITY_REVIEW"
    assert item(pending, graph).proposed_accrual is None
    upcoming = calculate(
        engine, prepared, proposal(graph, ex_date="2026-01-10", as_of="2026-01-09")
    )
    actual = item(upcoming, graph)
    assert actual.eligibility_state == "UPCOMING_RECORDED_HOLDINGS"
    assert actual.proposed_accrual.net_local == Decimal("8.800000")
    assert actual.previous_expected is None and actual.expected_delta is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("audit_field", ["trade_history_audit_scope", "trade_creation_audit_scope"])
async def test_unverified_trade_evidence_cannot_be_promoted_into_a_hypothetical_expectation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], audit_field: str
) -> None:
    prepared = await prepare(client, engine, graph)
    value = proposal(graph)
    with unit_of_work(engine) as con:
        actual = revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=date.fromisoformat(value.ex_date),
            as_of=date.fromisoformat(value.as_of),
            rule=value.eligibility_rule,
        )
    assert actual is not None
    if audit_field == "trade_history_audit_scope":
        altered_book = replace(
            actual.portfolios[0].book, trade_history_audit_scope="RETIRED_TRADE_HISTORY_UNVERIFIED"
        )
    else:
        altered_book = replace(
            actual.portfolios[0].book, trade_creation_audit_scope="CREATION_GAP_REVIEWED_UNVERIFIED"
        )
    altered = replace(actual, portfolios=(replace(actual.portfolios[0], book=altered_book),))
    before = database_snapshot(engine)
    with patch.object(revision_scope, "read", return_value=altered):
        result = calculate(engine, prepared, value)
    assert item(result, graph).state == "LEDGER_AUDIT_REVIEW"
    assert item(result, graph).proposed_accrual is None
    assert database_snapshot(engine) == before


@pytest.fixture
def input_graph() -> dict[str, Any]:
    return {name: {"id": str(uuid4())} for name in ("source", "target")}


@pytest.mark.parametrize(
    "changes",
    [
        {"tax_rate": 0.2},
        {"tax_rate": "1.000001"},
        {"fee": "-1"},
        {"fee": "0.0000001"},
        {"fee_currency": None},
        {"fee_rate_event": "0.1"},
        {"fee_rate_source": "source-without-rate"},
        {"fx_rate_hkd": None},
        {"fx_source": None},
        {"fx_source": " private "},
        {"fx_source": "IDENTITY"},
        {"fee_rate_event": "2", "fee_rate_source": "IDENTITY"},
        {"fee_rate_event": "1", "fee_rate_source": "not-identity"},
        {"fee_currency": "HKD", "fee_rate_event": "1", "fee_rate_source": "IDENTITY"},
        {"fee": None, "fee_currency": None, "fee_rate_event": "1", "fee_rate_source": "manual"},
    ],
)
def test_proposed_estimates_reject_implicit_or_invalid_values(
    input_graph: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError) as caught:
        source_estimate(input_graph, **changes)
    assert " private " not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"dps": 1.1},
        {"dps": "1.000000001"},
        {"dps": "92233720368.54775808"},
        {"currency": "CNY"},
        {"currency": "HKD"},
        {"ex_date": "2026-02-30"},
        {"eligibility_rule": "UNCONFIRMED_DUE_BILL"},
        {"adjustment_basis": "GUESS"},
        {"fake_default_fx": "7.8"},
    ],
)
def test_proposal_contract_rejects_unavailable_or_rounded_inputs(
    input_graph: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        proposal(input_graph, **changes)


async def test_hkd_identity_and_old_as_of_are_preserved_without_invented_delta(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    tax, fee = settings(engine, prepared, rate="0", fee="0")
    old = save(engine, prepared, intent(engine, prepared, tax, fee, as_of="2026-01-07"), uuid4())
    before = database_snapshot(engine)
    old_day = item(calculate(engine, prepared, proposal(graph)), graph)
    assert old_day.previous_projection_id == old.version.id
    assert old_day.previous_expected == Decimal("10.000000")
    assert old_day.expected_delta is None and old_day.previous_as_of == date(2026, 1, 7)
    hkd = proposal(
        graph,
        currency="HKD",
        accounts=[
            {
                "account_id": graph[name]["id"],
                "tax_rate": "0",
                "fee": "0",
                "fee_currency": "HKD",
                "fee_rate_event": "1",
                "fee_rate_source": "IDENTITY",
                "fx_rate_hkd": "1",
                "fx_source": "IDENTITY",
            }
            for name in ("source", "target")
        ],
    )
    value = item(calculate(engine, prepared, hkd), graph)
    assert (
        value.proposed_accrual.net_local
        == value.proposed_accrual.booked_hkd
        == Decimal("11.000000")
    )
    assert value.previous_currency == "USD" and value.expected_delta is None
    assert database_snapshot(engine) == before


async def test_derived_and_receipt_sum_overflow_fail_without_mutating_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    await create(client, "trades", trade_body(graph, quantity="200", trade_date="2026-01-07"))
    before = database_snapshot(engine)
    with pytest.raises(
        preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
    ):
        calculate(engine, prepared, proposal(graph, dps="92233720368.54775807"))
    assert database_snapshot(engine) == before
    direct_receipt(engine, prepared, received_micro=9_223_372_036_854_775_807)
    direct_receipt(engine, prepared, received_micro=1)
    before = database_snapshot(engine)
    with pytest.raises(
        preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
    ):
        calculate(engine, prepared, proposal(graph))
    assert database_snapshot(engine) == before


async def test_delta_overflow_is_not_rounded_or_truncated(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    tax, fee = settings(engine, prepared, rate="0", fee="0")
    save(engine, prepared, intent(engine, prepared, tax, fee), uuid4())
    value = source_estimate(graph, fee="9223372036854.775807", fx_rate_hkd=None, fx_source=None)
    original = value.model_dump(mode="python")
    value = preview.RevisionProposal.model_validate({**original, "dps": "0"})
    calculated = accrue(
        "synthetic-event",
        "synthetic-account",
        "USD",
        Decimal("10"),
        Decimal("0"),
        Decimal("0"),
        Decimal("9223372036854.775807"),
        fee_currency="USD",
    )
    assert calculated.net_local == Decimal("-9223372036854.775807")
    before = database_snapshot(engine)
    with pytest.raises(
        preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
    ):
        calculate(engine, prepared, value)
    assert database_snapshot(engine) == before


async def test_foreign_scope_invalid_arguments_and_substituted_snapshot_fail_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    value = proposal(graph)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert preview.read(con, uuid4(), prepared["event"].event_id, value) is None
        with pytest.raises(
            preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
        ):
            preview.read(con, prepared["owner"], prepared["event"].event_id, object())  # type: ignore[arg-type]
        actual = revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=date.fromisoformat(value.ex_date),
            as_of=date.fromisoformat(value.as_of),
            rule=value.eligibility_rule,
        )
    assert actual is not None
    altered = replace(actual, retained=replace(actual.retained, revision_id=uuid4()))
    with (
        patch.object(revision_scope, "read", return_value=altered),
        pytest.raises(
            preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
        ),
    ):
        calculate(engine, prepared, value)
    with (
        engine.connect() as con,
        pytest.raises(preview.RevisionPreviewError, match="^INVALID_DIVIDEND_REVISION_PREVIEW$"),
    ):
        preview.read(con, prepared["owner"], prepared["event"].event_id, value)
    assert database_snapshot(engine) == before


async def test_swapped_ledger_observations_cannot_be_attributed_to_another_account(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    value = proposal(graph)
    with unit_of_work(engine) as con:
        actual = revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=date.fromisoformat(value.ex_date),
            as_of=date.fromisoformat(value.as_of),
            rule=value.eligibility_rule,
        )
    assert actual is not None and len(actual.portfolios[0].book.accounts) == 2
    reordered = replace(
        actual.portfolios[0].book,
        accounts=tuple(reversed(actual.portfolios[0].book.accounts)),
    )
    forged = replace(actual, portfolios=(replace(actual.portfolios[0], book=reordered),))
    before = database_snapshot(engine)
    with (
        patch.object(revision_scope, "read", return_value=forged),
        pytest.raises(
            preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
        ),
    ):
        calculate(engine, prepared, value)
    assert database_snapshot(engine) == before


async def test_malformed_or_incomplete_nested_scopes_never_return_partial_previews(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    value = proposal(graph)
    with unit_of_work(engine) as con:
        actual = revision_scope.read(
            con,
            prepared["owner"],
            prepared["event"].event_id,
            ex_date=date.fromisoformat(value.ex_date),
            as_of=date.fromisoformat(value.as_of),
            rule=value.eligibility_rule,
        )
    assert actual is not None
    portfolio = actual.portfolios[0]
    cases = (
        replace(actual, portfolios=()),
        replace(actual, portfolios=(portfolio, portfolio)),
        replace(actual, portfolios=(cast(Any, object()),)),
        replace(actual, retained=replace(actual.retained, accounts=(cast(Any, object()),))),
        replace(actual, portfolios=(replace(portfolio, book=cast(Any, object())),)),
        replace(actual, portfolios=(replace(portfolio, portfolio=cast(Any, object())),)),
        replace(actual, portfolios=(replace(portfolio, accounts=(cast(Any, object()),)),)),
        replace(
            actual,
            portfolios=(
                replace(portfolio, book=replace(portfolio.book, accounts=(cast(Any, object()),))),
            ),
        ),
        replace(
            actual,
            portfolios=(
                replace(
                    portfolio,
                    portfolio=replace(portfolio.portfolio, version=portfolio.portfolio.version + 1),
                ),
            ),
        ),
        replace(
            actual, portfolios=(replace(portfolio, book=replace(portfolio.book, owner_id=uuid4())),)
        ),
        replace(actual, as_of=date(2026, 1, 7)),
    )
    before = database_snapshot(engine)
    for forged in cases:
        with (
            patch.object(revision_scope, "read", return_value=forged),
            pytest.raises(
                preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
            ),
        ):
            calculate(engine, prepared, value)
    assert database_snapshot(engine) == before


async def test_substituted_retained_account_cannot_be_duplicated_or_detached(
    engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    value = proposal(graph)
    with unit_of_work(engine) as con:
        actual = revision_scope.read(
            con,
            book["owner"],
            book["event"].event_id,
            ex_date=date.fromisoformat(value.ex_date),
            as_of=date.fromisoformat(value.as_of),
            rule=value.eligibility_rule,
        )
    assert actual is not None and len(actual.retained.accounts) == 1
    retained = actual.retained.accounts[0]
    forged_scopes = (
        replace(actual, retained=replace(actual.retained, accounts=(retained, retained))),
        replace(
            actual,
            retained=replace(actual.retained, accounts=(replace(retained, account_id=uuid4()),)),
        ),
    )
    before = database_snapshot(engine)
    for forged in forged_scopes:
        with (
            patch.object(revision_scope, "read", return_value=forged),
            pytest.raises(
                preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
            ),
        ):
            calculate(engine, book, value)
    assert database_snapshot(engine) == before


async def test_cross_revision_legacy_and_direct_cash_are_counted_once(
    engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000, older=True)
    direct_receipt(engine, book, received_micro=2_000_000)
    before = database_snapshot(engine)
    result = calculate(engine, book, proposal(graph))
    actual = item(result, graph)
    assert actual.active_receipts == 2 and actual.unverified_origins == 1
    assert actual.proposed_accrual.net_local == Decimal("11.000000")
    assert actual.recorded_received == Decimal("8.000000")
    assert actual.recorded_outstanding == Decimal("3.000000")
    assert result.eligibility_scope == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_mixed_cross_store_currencies_are_not_added(
    engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000, older=True)
    direct_receipt(
        engine,
        book,
        currency="HKD",
        received_micro=2_000_000,
        fx_rate_hkd_e12=1_000_000_000_000,
        received_hkd_micro=2_000_000,
    )
    before = database_snapshot(engine)
    actual = item(calculate(engine, book, proposal(graph)), graph)
    assert actual.active_receipts == 2 and actual.state == "CURRENCY_REVIEW"
    assert actual.recorded_received is actual.recorded_outstanding is None
    assert actual.comparison_quality == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


async def test_only_latest_recorded_projection_is_used_for_hypothetical_delta(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    tax, fee = settings(engine, prepared, rate="0", fee="0")
    first = save(engine, prepared, intent(engine, prepared, tax, fee, as_of="2026-01-07"), uuid4())
    second = save(
        engine,
        prepared,
        intent(
            engine,
            prepared,
            tax,
            fee,
            expected_projection_no=1,
            expected_projection_id=first.version.id,
        ),
        uuid4(),
    )
    before = database_snapshot(engine)
    actual = item(calculate(engine, prepared, proposal(graph)), graph)
    assert actual.previous_projection_id == second.version.id
    assert actual.previous_expected == Decimal("10.000000")
    assert actual.expected_delta == Decimal("1.000000")
    assert database_snapshot(engine) == before


async def test_informational_receipt_tax_and_fees_are_not_subtracted_twice(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(
        engine, prepared, received_micro=6_000_000, tax_micro=1_000_000, fee_micro=500_000
    )
    before = database_snapshot(engine)
    value = source_estimate(graph, tax_rate="0.2", fee="0.5")
    actual = item(calculate(engine, prepared, value), graph)
    assert actual.proposed_accrual.gross_local == Decimal("11.000000")
    assert actual.proposed_accrual.tax_local == Decimal("2.200000")
    assert actual.proposed_accrual.fee_local == Decimal("0.500000")
    assert actual.proposed_accrual.net_local == Decimal("8.300000")
    assert actual.recorded_received == Decimal("6.000000")
    assert actual.recorded_outstanding == Decimal("2.300000")
    assert database_snapshot(engine) == before


async def test_untrusted_constructed_model_cannot_bypass_exact_decimal_validation(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    forged = proposal(graph).model_copy(update={"dps": 1.25})
    with pytest.raises(
        preview.RevisionPreviewError, match="^DIVIDEND_REVISION_PREVIEW_UNAVAILABLE$"
    ):
        calculate(engine, prepared, forged)
    assert database_snapshot(engine) == before


async def test_decimal_context_cannot_change_hypothetical_results(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    direct_receipt(engine, prepared, received_micro=6_000_000)
    before = database_snapshot(engine)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        actual = item(
            calculate(engine, prepared, source_estimate(graph, tax_rate="0.2", fee="0.5")), graph
        )
    assert actual.proposed_accrual.net_local == Decimal("8.300000")
    assert actual.recorded_received == Decimal("6.000000")
    assert actual.recorded_outstanding == Decimal("2.300000")
    assert database_snapshot(engine) == before


async def test_empty_current_account_set_is_not_confirmed_no_historical_ownership(
    engine: Engine, source: dict[str, Any]
) -> None:
    event = manual()
    submit(engine, event)
    before = database_snapshot(engine)
    value = preview.RevisionProposal.model_validate(
        {
            "ex_date": "2026-01-08",
            "as_of": "2026-01-08",
            "eligibility_rule": "ORDINARY_PRE_EX_DATE",
            "dps": "1.1",
            "currency": "USD",
            "adjustment_basis": "AS_DECLARED",
            "accounts": [],
        }
    )
    with unit_of_work(engine) as con:
        result = preview.read(con, OWNER, event.event_id, value)
    assert result is not None and result.accounts == ()
    assert (
        result.history_completeness == "UNVERIFIED"
        and result.eligibility_scope == "NOT_ESTABLISHED"
    )
    assert database_snapshot(engine) == before


async def test_preview_remains_one_wal_snapshot_during_new_receipt(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    prepared = await prepare(client, engine, graph)
    value = proposal(graph)
    with unit_of_work(engine) as con:
        first = preview.read(con, prepared["owner"], prepared["event"].event_id, value)
        assert item(first, graph).state == "NO_RECORDED_ACTIVE_RECEIPTS"
        direct_receipt(engine, prepared, received_micro=6_000_000)
        assert preview.read(con, prepared["owner"], prepared["event"].event_id, value) == first
    latest = item(calculate(engine, prepared, value), graph)
    assert latest.recorded_received == Decimal("6.000000")
    assert latest.recorded_outstanding == Decimal("5.000000")
