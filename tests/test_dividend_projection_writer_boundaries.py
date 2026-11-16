"""Projection persistence preserves intent, review states, and exact historical replay."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_projection_writer as writer
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot
from tests.test_dividend_eligibility_service import split_body
from tests.test_dividend_event_migration import REASON
from tests.test_dividend_event_writer import next_intent
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


async def test_withdrawn_event_produces_cancelled_not_an_amount(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    withdrawn = next_intent(book["event"], "WITHDRAW")
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con,
            book["owner"],
            withdrawn,
            at=datetime(2026, 11, 10, 12, tzinfo=UTC),
            request_id=uuid4(),
        )
    book["event"] = withdrawn
    result = save(engine, book, intent(engine, book), uuid4())
    assert result.version.state == "CANCELLED" and result.version.accrual is None
    assert result.eligible_shares is None and result.history_completeness == "UNVERIFIED"


async def test_unresolved_action_requires_review_even_with_estimates(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    await create(client, "trades", trade_body(graph, trade_date="2026-01-06", quantity="1"))
    await create(
        client,
        "corporate-actions",
        split_body(
            graph,
            action_type="MERGER",
            effective_date="2026-01-07",
            status="NEEDS_REVIEW",
            ratio_numerator=None,
            ratio_denominator=None,
        ),
    )
    tax_setting, fee_setting = settings(engine, book)
    result = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    assert result.version.state == "NEEDS_REVIEW" and result.version.accrual is None
    assert result.tax_provenance is not None and result.tax_provenance.status == "ESTIMATED"
    assert result.fee_provenance is not None and result.fee_provenance.status == "ESTIMATED"


@pytest.mark.parametrize(
    ("field", "scope"),
    [
        ("trade_history_audit_scope", "RETIRED_TRADE_HISTORY_UNVERIFIED"),
        ("trade_creation_audit_scope", "CREATION_GAP_REVIEWED_UNVERIFIED"),
    ],
)
async def test_unverified_trade_evidence_does_not_become_a_projection_grant(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    field: str,
    scope: str,
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    value = intent(engine, book, tax_setting, fee_setting)
    real_get = eligibility.get

    def unverified(*args: Any, **kwargs: Any) -> Any:
        result = real_get(*args, **kwargs)
        assert result is not None
        if field == "trade_history_audit_scope":
            return replace(result, trade_history_audit_scope="RETIRED_TRADE_HISTORY_UNVERIFIED")
        return replace(result, trade_creation_audit_scope="CREATION_GAP_REVIEWED_UNVERIFIED")

    with patch.object(eligibility, "get", side_effect=unverified):
        result = save(engine, book, value, uuid4())
    assert result.version.state == "NEEDS_REVIEW" and result.version.accrual is None
    assert (
        getattr(
            result,
            "retired_trade_scope" if field.startswith("trade_history") else "creation_audit_scope",
        )
        == scope
    )


async def test_hkd_dividend_uses_event_currency_and_only_identity_conversion(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, currency="HKD")
    tax_setting, fee_setting = settings(engine, book, fee="0.5", fee_currency="HKD")
    bad = intent(engine, book, tax_setting, fee_setting)
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^INVALID_DIVIDEND_PROJECTION_FX$"):
        save(engine, book, bad, uuid4())
    assert database_snapshot(engine) == before
    value = intent(engine, book, tax_setting, fee_setting, fx_rate_hkd=None, fx_source=None)
    result = save(engine, book, value, uuid4())
    assert result.version.accrual is not None and result.version.accrual.currency == "HKD"
    assert result.version.accrual.net_local == Decimal("7.5")
    assert result.version.accrual.booked_hkd == Decimal("7.5")
    assert result.fx_rate_hkd == Decimal(1) and result.fx_source == "IDENTITY"
    assert result.fee_rate_event == Decimal(1) and result.fee_rate_source == "IDENTITY"


async def test_foreign_fee_keeps_precision_until_net_and_hkd_rounding(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book, fee="0.000001", fee_currency="HKD")
    value = intent(
        engine,
        book,
        tax_setting,
        fee_setting,
        fee_rate_event="0.5",
        fee_rate_source="MANUAL:fee-to-usd",
    )
    result = save(engine, book, value, uuid4())
    assert result.version.accrual is not None
    assert result.version.accrual.fee_local == Decimal("0.000001")
    assert result.version.accrual.net_local == Decimal("8.000000")
    assert result.version.accrual.rounding_delta_local == Decimal("0.000001")
    assert result.version.accrual.booked_hkd == Decimal("62.399996")


async def test_negative_expected_net_is_retained_for_review_not_clamped(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book, fee="20")
    result = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    assert result.version.accrual is not None
    assert result.version.accrual.net_local == Decimal("-12")
    assert result.version.accrual.quality == "NEEDS_REVIEW"


async def test_backdated_tax_setting_does_not_relabel_or_rewrite_a_replay(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    first_tax, fee_setting = settings(engine, book)
    value, request = intent(engine, book, first_tax, fee_setting), uuid4()
    first = save(engine, book, value, request)
    later = tax.SettingIntent(
        id=uuid4(),
        event_id=book["event"].event_id,
        revision_id=book["event"].revision_id,
        revision_no=book["event"].revision_no,
        account_id=book["account_id"],
        portfolio_id=book["portfolio_id"],
        broker_id=book["broker_id"],
        account_version=book["account_version"],
        broker_version=book["broker_version"],
        expected_setting_no=1,
        effective_from="2026-01-01",
        rate="0.3",
        reason=REASON,
    )
    with unit_of_work(engine, write=True) as con:
        second_tax = tax.save(con, book["owner"], later, request_id=uuid4())
    before = database_snapshot(engine)
    assert save(engine, book, value, request) == first
    stale = intent(
        engine,
        book,
        first_tax,
        fee_setting,
        expected_projection_no=1,
        expected_projection_id=first.version.id,
    )
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_TAX_SETTING_CHANGED$"):
        save(engine, book, stale, uuid4())
    assert database_snapshot(engine) == before
    revised = intent(
        engine,
        book,
        second_tax,
        fee_setting,
        expected_projection_no=1,
        expected_projection_id=first.version.id,
    )
    current = save(engine, book, revised, uuid4())
    assert current.version.accrual is not None
    assert current.version.accrual.tax_local == Decimal("3")
    assert first.version.accrual is not None and first.version.accrual.tax_local == Decimal("2")


async def test_replay_rejects_reused_request_and_duplicate_intent_audit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value, request = intent(engine, book), uuid4()
    original = save(engine, book, value, request)
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_CONFLICT$"):
        save(engine, book, value, uuid4())
    other = intent(
        engine,
        book,
        expected_projection_no=1,
        expected_projection_id=original.version.id,
    )
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_CONFLICT$"):
        save(engine, book, other, request)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_projection_intent",
            str(value.id),
            "DIVIDEND_PROJECTION_INTENT_RECORDED",
            request_id=str(request),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    duplicate = database_snapshot(engine)
    with pytest.raises(
        writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
    ):
        save(engine, book, value, request)
    assert database_snapshot(engine) == duplicate


async def test_orphan_intent_audit_and_invalid_currency_rate_fail_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_projection_intent",
            str(value.id),
            "DIVIDEND_PROJECTION_INTENT_RECORDED",
            request_id=str(uuid4()),
            after={"synthetic": "orphan"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    with pytest.raises(
        writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
    ):
        save(engine, book, value, uuid4())
    invalid = intent(engine, book, fx_rate_hkd="1", fx_source="IDENTITY")
    with pytest.raises(writer.ProjectionWriteError, match="^INVALID_DIVIDEND_PROJECTION_FX$"):
        save(engine, book, invalid, uuid4())
    assert database_snapshot(engine) == before


async def test_audit_timestamp_normalizes_non_utc_aware_input(
    engine: Engine, graph: dict[str, Any]
) -> None:
    local = datetime(2026, 11, 10, 13, tzinfo=timezone(timedelta(hours=8)))
    with unit_of_work(engine, write=True) as con:
        audit(con, graph["owner"]["id"], "synthetic", "clock-offset", "CLOCK_TEST", at=local)
        stored = con.exec_driver_sql(
            "SELECT created_at FROM audit_events WHERE entity_type='synthetic' "
            "AND entity_id='clock-offset'"
        ).scalar_one()
    assert stored == stamp(local.astimezone(UTC)) == "2026-11-10T05:00:00.000000Z"
