"""Retired trade hashes are matched only with retained, validated origin evidence."""

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Connection, Engine

from spm_api import trade_service
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.dividend_eligibility_service import (
    EligibilityHistoryError,
    RecordedEligibilityBook,
    get,
)
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_origin_migration import capture_values, origin_values
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def read(con: Connection, graph: dict[str, Any]) -> RecordedEligibilityBook | None:
    return get(
        con,
        UUID(graph["owner"]["id"]),
        UUID(graph["portfolio"]["id"]),
        UUID(graph["security"]["id"]),
        ex_date=date(2026, 1, 5),
        as_of=date(2026, 1, 5),
        rule="ORDINARY_PRE_EX_DATE",
    )


@pytest.mark.parametrize("end", ["active", "void"])
async def test_matching_origins_and_retirement_hashes_are_reported_without_adoption(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], end: str
) -> None:
    original = await create(client, "trades", trade_body(graph))
    revision = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=trade_body(graph, quantity="11", reason="Correct the synthetic trade quantity"),
        headers=version_headers(client, 1),
    )
    assert revision.status_code == 200
    if end == "void":
        void = await client.request(
            "DELETE",
            f"/api/v1/trades/{revision.json()['id']}",
            json={"reason": "Void the synthetic revised trade"},
            headers=version_headers(client, 1),
        )
        assert void.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = read(con, graph)
        assert result is not None
        assert result.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED"
        quantities = {row.account_id: row.recorded_quantity for row in result.accounts}
        assert quantities[graph["source"]["id"]] == (
            "11.00000000" if end == "active" else "0.00000000"
        )
        assert all(row.history_completeness == "UNVERIFIED" for row in result.accounts)
    assert database_snapshot(engine) == before


async def test_old_history_without_origins_remains_explicitly_unverified(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
    ):
        trade = await create(client, "trades", trade_body(graph))
        revised = await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=trade_body(graph, quantity="11", reason="Correct the synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
    assert revised.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 0
        result = read(con, graph)
        assert result is not None
        assert result.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
    assert database_snapshot(engine) == before


async def test_one_missing_legacy_origin_keeps_the_whole_snapshot_unverified(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
    ):
        legacy = await create(client, "trades", trade_body(graph))
        response = await client.patch(
            f"/api/v1/trades/{legacy['id']}",
            json=trade_body(graph, quantity="11", reason="Correct the synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
    assert response.status_code == 200
    current = await create(client, "trades", trade_body(graph, account_id=graph["target"]["id"]))
    response = await client.patch(
        f"/api/v1/trades/{current['id']}",
        json=trade_body(
            graph,
            account_id=graph["target"]["id"],
            quantity="12",
            reason="Correct another synthetic trade",
        ),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    with unit_of_work(engine) as con:
        result = read(con, graph)
        assert (
            result is not None
            and result.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
        )


@pytest.mark.parametrize("retirement", ["REVISE", "VOID"])
async def test_new_audit_with_unrelated_before_hash_is_not_accepted_as_a_chain(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], retirement: str
) -> None:
    trade = await create(client, "trades", trade_body(graph))

    def synthetic_unrelated_before(*args: Any, **kwargs: Any) -> None:
        if args[2] == "trade" and args[4] == retirement:
            kwargs["before"] = {"synthetic": "not the predecessor"}
        audit(*args, **kwargs)

    with patch.object(trade_service, "audit", side_effect=synthetic_unrelated_before):
        if retirement == "REVISE":
            response = await client.patch(
                f"/api/v1/trades/{trade['id']}",
                json=trade_body(graph, quantity="11", reason="Correct synthetic recorded trade"),
                headers=version_headers(client, 1),
            )
        else:
            response = await client.request(
                "DELETE",
                f"/api/v1/trades/{trade['id']}",
                json={"reason": "Void synthetic recorded trade"},
                headers=version_headers(client, 1),
            )
    assert response.status_code == 200
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, graph)
    assert database_snapshot(engine) == before


async def test_new_sql_claim_cannot_hide_a_mismatched_creation_fingerprint(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    def synthetic_unrelated_after(*args: Any, **kwargs: Any) -> None:
        if args[2] == "trade" and args[4] == "CREATE":
            kwargs["after"] = {"synthetic": "not the original trade"}
        audit(*args, **kwargs)

    with (
        patch.object(trade_service, "audit", side_effect=synthetic_unrelated_after),
        patch.object(trade_service, "_remember_trade_origin"),
    ):
        trade = await create(client, "trades", trade_body(graph))
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, graph["owner"]["id"], trade["id"])
        insert(con, "trade_audit_origins", values)
    with patch.object(trade_service, "_capture_trade_origin"):
        revised = await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=trade_body(graph, quantity="11", reason="Correct synthetic recorded trade"),
            headers=version_headers(client, 1),
        )
    assert revised.status_code == 200
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, graph)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("retire", [False, True])
async def test_new_sql_capture_claim_cannot_replace_a_canonical_capture_audit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], retire: bool
) -> None:
    with patch.object(trade_service, "_remember_trade_origin"):
        trade = await create(client, "trades", trade_body(graph))
    with unit_of_work(engine, write=True) as con:
        values = origin_values(con, graph["owner"]["id"], trade["id"])
        values = capture_values(con, values, "BEFORE_RETIRE")
        insert(con, "trade_audit_origins", values)
    if retire:
        with patch.object(trade_service, "_capture_trade_origin"):
            revised = await client.patch(
                f"/api/v1/trades/{trade['id']}",
                json=trade_body(graph, quantity="11", reason="Correct synthetic recorded trade"),
                headers=version_headers(client, 1),
            )
        assert revised.status_code == 200
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, graph)
    assert database_snapshot(engine) == before


async def test_read_snapshot_does_not_mix_a_concurrent_origin_revision(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with unit_of_work(engine) as con:
        first = read(con, graph)
        assert first is not None and first.trade_history_audit_scope == "NO_RETIRED_TRADES"
        revised = await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=trade_body(graph, quantity="11", reason="Correct synthetic recorded trade"),
            headers=version_headers(client, 1),
        )
        assert revised.status_code == 200
        assert read(con, graph) == first
    with unit_of_work(engine) as con:
        latest = read(con, graph)
        assert (
            latest is not None
            and latest.trade_history_audit_scope == "RETIRED_TRADE_HASHES_MATCHED"
        )
        assert latest.data_revision > first.data_revision


async def test_missing_legacy_origin_does_not_hide_another_mismatched_chain(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
    ):
        legacy = await create(client, "trades", trade_body(graph))
        changed = await client.patch(
            f"/api/v1/trades/{legacy['id']}",
            json=trade_body(graph, quantity="11", reason="Correct synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
    assert changed.status_code == 200
    other = await create(client, "trades", trade_body(graph, account_id=graph["target"]["id"]))

    def synthetic_unrelated_before(*args: Any, **kwargs: Any) -> None:
        if args[2] == "trade" and args[4] == "REVISE":
            kwargs["before"] = {"synthetic": "not the predecessor"}
        audit(*args, **kwargs)

    with patch.object(trade_service, "audit", side_effect=synthetic_unrelated_before):
        revised = await client.patch(
            f"/api/v1/trades/{other['id']}",
            json=trade_body(
                graph,
                account_id=graph["target"]["id"],
                quantity="12",
                reason="Correct another synthetic trade",
            ),
            headers=version_headers(client, 1),
        )
    assert revised.status_code == 200
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, graph)
    assert database_snapshot(engine) == before


async def test_present_origin_must_resolve_inside_the_same_read_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    before = database_snapshot(engine)
    with (
        patch("spm_api.dividend_eligibility_service.trade_audit.get_origin", return_value=None),
        unit_of_work(engine) as con,
        pytest.raises(EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"),
    ):
        read(con, graph)
    assert database_snapshot(engine) == before


async def test_legacy_current_without_origin_uses_its_direct_audit_fingerprint(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    with patch.object(trade_service, "_remember_trade_origin"):
        await create(client, "trades", trade_body(graph))
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = read(con, graph)
        assert result is not None and result.trade_history_audit_scope == "NO_RETIRED_TRADES"
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 0
    assert database_snapshot(engine) == before


async def test_old_retired_trade_without_origin_does_not_gain_new_timestamp_requirements(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    with (
        patch.object(trade_service, "_remember_trade_origin"),
        patch.object(trade_service, "_capture_trade_origin"),
        patch.object(trade_service, "stamp", return_value="2020-01-01T00:00:00Z"),
    ):
        trade = await create(client, "trades", trade_body(graph))
        revised = await client.patch(
            f"/api/v1/trades/{trade['id']}",
            json=trade_body(graph, quantity="11", reason="Correct synthetic legacy trade"),
            headers=version_headers(client, 1),
        )
    assert revised.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        result = read(con, graph)
        assert (
            result is not None
            and result.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
        )
    assert database_snapshot(engine) == before
