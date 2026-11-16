"""Recorded-share input timestamps track relevant validated history, not ownership proof."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_eligibility_service as eligibility
from spm_api.db import unit_of_work
from tests.helpers import create, version_headers
from tests.test_dividend_eligibility_service import split_body
from tests.test_transfers import graph as graph
from tests.test_transfers import stock_body, trade_body

pytestmark = pytest.mark.anyio


def load(engine: Engine, graph: dict[str, Any]) -> eligibility.RecordedEligibilityBook:
    with unit_of_work(engine) as con:
        result = eligibility.get(
            con,
            UUID(graph["owner"]["id"]),
            UUID(graph["portfolio"]["id"]),
            UUID(graph["security"]["id"]),
            ex_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
            as_of=datetime(2026, 1, 5, tzinfo=UTC).date(),
            rule="ORDINARY_PRE_EX_DATE",
        )
    assert result is not None
    return result


async def test_latest_input_covers_audited_trade_transfer_and_all_action_revisions(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    empty = load(engine, graph)
    assert empty.latest_input_at is None
    trade = await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    transfer = await create(
        client, "security-transfers", stock_body(graph, transfer_date="2026-01-02")
    )
    action = await create(
        client, "corporate-actions", split_body(graph, effective_date="2026-01-03")
    )
    response = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}",
        json=split_body(graph, effective_date="2026-01-03", ratio_numerator=2, ratio_denominator=1),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    revised = response.json()
    observed = load(engine, graph)
    with unit_of_work(engine) as con:
        times = con.exec_driver_sql(
            "SELECT created_at FROM audit_events WHERE "
            "(entity_type='trade' AND entity_id=?) OR "
            "(entity_type='security_transfer' AND entity_id=?) OR "
            "(entity_type='corporate_action' AND entity_id IN (?,?)) "
            "UNION ALL SELECT captured_at FROM trade_audit_origins WHERE trade_id=?",
            (trade["id"], transfer["id"], action["id"], revised["id"], trade["id"]),
        ).scalars()
        latest = max(datetime.fromisoformat(item.replace("Z", "+00:00")) for item in times)
    assert observed.latest_input_at is not None and observed.latest_input_at >= latest
    assert all(item.history_completeness == "UNVERIFIED" for item in observed.accounts)

    other = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHER",
            "name": "Synthetic unrelated security",
            "quote_currency": "USD",
        },
    )
    await create(
        client, "trades", trade_body(graph, security_id=other["id"], trade_date="2026-01-01")
    )
    after_other = load(engine, graph)
    assert after_other.latest_input_at == observed.latest_input_at
    assert after_other.data_revision > observed.data_revision
