"""Additional projection consistency checks across real owner and setting changes."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy import Engine

import spm_api.dividend_event_service as event_service
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_projection_writer as writer
import spm_api.dividend_rejection_writer as rejection
import spm_api.dividend_tax_service as tax
from spm_api.db import unit_of_work
from spm_api.providers.http import ProviderHTTP
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_event_writer import next_intent, terms
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_tax_service import intent as tax_intent
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


async def test_selected_none_with_a_future_tax_head_is_not_zero(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    future_intent = tax_intent(book, effective_from="2026-06-01", rate="0")
    with unit_of_work(engine, write=True) as con:
        future = tax.save(con, book["owner"], future_intent, request_id=uuid4())
    value = intent(engine, book, future, expected_tax_selected_id=None)
    result = save(engine, book, value, uuid4())
    assert result.version.state == "NEEDS_REVIEW" and result.tax_rate is None
    assert result.tax_provenance is not None
    assert result.tax_provenance.status == "MISSING"
    assert result.tax_provenance.observed_head_no == 1
    assert result.tax_provenance.observed_head_id == future.id
    assert result.tax_provenance.selected_setting_id is None


async def test_rejection_after_projection_allows_only_historical_replay(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    value, request = intent(engine, book, tax_setting, fee_setting), uuid4()
    original = save(engine, book, value, request)
    with unit_of_work(engine, write=True) as con:
        rejection.save(con, book["owner"], rejection_intent(book["event"]), request_id=uuid4())
    before = database_snapshot(engine)
    assert save(engine, book, value, request) == original
    attempted = intent(
        engine,
        book,
        tax_setting,
        fee_setting,
        expected_projection_no=1,
        expected_projection_id=original.version.id,
    )
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_REVISION_REJECTED$"):
        save(engine, book, attempted, uuid4())
    assert database_snapshot(engine) == before


async def test_stale_event_revision_and_foreign_owner_do_not_write(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    revised = next_intent(book["event"], "MANUAL", terms=terms(security_id=graph["security"]["id"]))
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con,
            book["owner"],
            revised,
            at=datetime(2026, 11, 10, 12, tzinfo=UTC),
            request_id=uuid4(),
        )
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_EVENT_VERSION_CONFLICT$"):
        save(engine, book, value, uuid4())
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_OWNER_UNAVAILABLE$"),
    ):
        writer.save(con, uuid4(), value, request_id=uuid4())
    assert database_snapshot(engine) == before


async def test_cross_account_reuse_of_a_physical_projection_id_conflicts(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book)
    save(engine, book, value, uuid4())
    before = database_snapshot(engine)
    other = value.model_copy(update={"account_id": UUID(graph["target"]["id"])})
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_CONFLICT$"):
        save(engine, book, other, uuid4())
    assert database_snapshot(engine) == before


async def test_missing_fee_currency_rejects_an_unrelated_fee_fx(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value = intent(engine, book, fee_rate_event="0.5", fee_rate_source="MANUAL:synthetic")
    before = database_snapshot(engine)
    with pytest.raises(writer.ProjectionWriteError, match="^INVALID_DIVIDEND_PROJECTION_FX$"):
        save(engine, book, value, uuid4())
    assert database_snapshot(engine) == before


async def test_existing_receipts_cash_and_locked_fx_are_never_rewritten(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    claim = entitlement(
        {"id": str(book["event"].revision_id), "currency": "USD"}, str(book["account_id"])
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
    before = database_snapshot(engine)
    result = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    after = database_snapshot(engine)
    assert result.version.state == "EXPECTED"
    for table in ("dividend_entitlements", "dividend_receipts", "cash_ledger_entries"):
        assert after[table] == before[table]
    assert receipt["received_hkd_micro"] == 46_800_000
    assert receipt["fx_rate_hkd_e12"] == 7_800_000_000_000


async def test_new_projection_does_not_bypass_event_revision_fence(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    save(engine, book, intent(engine, book), uuid4())
    before = database_snapshot(engine)
    revised = next_intent(book["event"], "MANUAL", terms=terms(security_id=graph["security"]["id"]))
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(event_service.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"),
    ):
        event_writer.save(
            con,
            book["owner"],
            revised,
            at=datetime(2026, 11, 10, 12, tzinfo=UTC),
            request_id=uuid4(),
        )
    assert database_snapshot(engine) == before


async def test_no_external_provider_or_password_hashing_occurs_in_writer(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    value = intent(engine, book, tax_setting, fee_setting)
    with (
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider HTTP")),
        patch.object(PasswordHasher, "hash", side_effect=AssertionError("password hash")),
    ):
        result = save(engine, book, value, uuid4())
    assert result.version.state == "EXPECTED"
