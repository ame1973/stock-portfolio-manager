"""Projection writes derive recorded shares and retain explicit estimated provenance."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_fee_writer as fee_writer
import spm_api.dividend_projection_writer as writer
import spm_api.dividend_tax_service as tax
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot
from tests.test_dividend_event_migration import REASON
from tests.test_dividend_event_writer import manual, terms
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


async def prepare(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    *,
    holdings: bool = True,
    currency: str = "USD",
) -> dict[str, Any]:
    if holdings:
        await create(client, "trades", trade_body(graph, trade_date="2026-01-07", quantity="10"))
        await create(
            client,
            "trades",
            trade_body(graph, trade_date="2026-01-08", side="SELL", quantity="2"),
        )
    owner = UUID(graph["owner"]["id"])
    event = manual(terms=terms(security_id=graph["security"]["id"], dps="1", currency=currency))
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con, owner, event, at=datetime(2026, 11, 9, 12, tzinfo=UTC), request_id=uuid4()
        )
    with unit_of_work(engine) as con:
        account = con.exec_driver_sql(
            "SELECT a.version AS account_version,b.version AS broker_version,a.broker_id "
            "FROM accounts a JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (graph["source"]["id"],),
        ).one()
    return {
        "owner": owner,
        "event": event,
        "account_id": UUID(graph["source"]["id"]),
        "portfolio_id": UUID(graph["portfolio"]["id"]),
        "broker_id": UUID(account.broker_id),
        "account_version": account.account_version,
        "broker_version": account.broker_version,
    }


def settings(
    engine: Engine,
    book: dict[str, Any],
    *,
    rate: str | None = "0.2",
    fee: str | None = "0.5",
    fee_currency: str | None = "USD",
) -> tuple[tax.TaxSetting, Any]:
    event = book["event"]
    scope = {
        key: book[key]
        for key in ("account_id", "portfolio_id", "broker_id", "account_version", "broker_version")
    }
    common = {
        **scope,
        "id": uuid4(),
        "event_id": event.event_id,
        "revision_id": event.revision_id,
        "revision_no": event.revision_no,
        "expected_setting_no": 0,
        "effective_from": "2026-01-01",
        "reason": REASON,
    }
    with unit_of_work(engine, write=True) as con:
        tax_setting = tax.save(
            con, book["owner"], tax.SettingIntent(**common, rate=rate), request_id=uuid4()
        )
    with unit_of_work(engine, write=True) as con:
        fee_setting = fee_writer.save(
            con,
            book["owner"],
            fee_writer.FeeIntent(
                **{**common, "id": uuid4()},
                expected_head_id=None,
                fee_status="ESTIMATED" if fee is not None else "UNKNOWN",
                fee=fee,
                fee_currency=fee_currency,
            ),
            request_id=uuid4(),
        )
    return tax_setting, fee_setting


def intent(
    engine: Engine,
    book: dict[str, Any],
    tax_setting: tax.TaxSetting | None = None,
    fee_setting: Any = None,
    **changes: Any,
) -> writer.ProjectionIntent:
    with unit_of_work(engine) as con:
        revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (str(book["portfolio_id"]),)
        ).scalar_one()
    event = book["event"]
    return writer.ProjectionIntent.model_validate(
        {
            "id": uuid4(),
            "event_id": event.event_id,
            "revision_id": event.revision_id,
            "revision_no": event.revision_no,
            "account_id": book["account_id"],
            "portfolio_id": book["portfolio_id"],
            "broker_id": book["broker_id"],
            "account_version": book["account_version"],
            "broker_version": book["broker_version"],
            "expected_data_revision": revision,
            "expected_projection_no": 0,
            "expected_projection_id": None,
            "as_of": "2026-01-08",
            "eligibility_rule": "ORDINARY_PRE_EX_DATE",
            "tax_effective_on": "2026-01-08",
            "expected_tax_head_no": tax_setting.setting_no if tax_setting else 0,
            "expected_tax_head_id": tax_setting.id if tax_setting else None,
            "expected_tax_selected_id": tax_setting.id if tax_setting else None,
            "fee_effective_on": "2026-01-08",
            "expected_fee_head_no": fee_setting.setting_no if fee_setting else 0,
            "expected_fee_head_id": fee_setting.id if fee_setting else None,
            "expected_fee_selected_id": fee_setting.id if fee_setting else None,
            "fx_rate_hkd": "7.8",
            "fx_source": "MANUAL:synthetic",
            "fee_rate_event": None,
            "fee_rate_source": None,
            "reason": REASON,
            **changes,
        }
    )


def save(
    engine: Engine, book: dict[str, Any], value: writer.ProjectionIntent, request: UUID
) -> Any:
    with unit_of_work(engine, write=True) as con:
        return writer.save(con, book["owner"], value, request_id=request)


async def test_projection_uses_real_pre_ex_date_shares_and_audited_estimates(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax_setting, fee_setting = settings(engine, book)
    value = intent(engine, book, tax_setting, fee_setting)
    before = database_snapshot(engine)
    result = save(engine, book, value, uuid4())
    assert result.version.state == "EXPECTED"
    assert result.eligible_shares == Decimal("10")
    assert result.version.accrual is not None
    assert result.version.accrual.gross_local == Decimal("10")
    assert result.version.accrual.tax_local == Decimal("2")
    assert result.version.accrual.fee_local == Decimal("0.5")
    assert result.version.accrual.net_local == Decimal("7.5")
    assert result.version.accrual.booked_hkd == Decimal("58.5")
    assert (
        result.tax_provenance is not None
        and result.tax_provenance.selected_setting_id == tax_setting.id
    )
    assert (
        result.fee_provenance is not None
        and result.fee_provenance.selected_setting_id == fee_setting.id
    )
    assert result.history_completeness == "UNVERIFIED"
    after = database_snapshot(engine)
    allowed = {"audit_events", "dividend_projection_versions"}
    assert {key: value for key, value in before.items() if key not in allowed} == {
        key: value for key, value in after.items() if key not in allowed
    }


async def test_missing_estimates_remain_review_not_zero_and_retry_is_read_only(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    value, request = intent(engine, book), uuid4()
    result = save(engine, book, value, request)
    assert result.version.state == "NEEDS_REVIEW" and result.version.accrual is None
    assert result.tax_provenance is not None and result.tax_provenance.status == "MISSING"
    assert result.fee_provenance is not None and result.fee_provenance.status == "MISSING"
    before = database_snapshot(engine)
    assert save(engine, book, value, request) == result
    assert database_snapshot(engine) == before
    changed = value.model_copy(update={"reason": "A different synthetic intent reason"})
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_CONFLICT$"):
        save(engine, book, changed, request)
    assert database_snapshot(engine) == before
