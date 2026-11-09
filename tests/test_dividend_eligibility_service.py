"""Owner-scoped eligibility reads real immutable ledger facts without writes."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_eligibility_service as service
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from tests.helpers import create, database_snapshot, version_headers
from tests.test_transfers import graph as graph
from tests.test_transfers import stock_body, trade_body

pytestmark = pytest.mark.anyio


def arguments(graph: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "owner_id": UUID(graph["owner"]["id"]),
        "portfolio_id": UUID(graph["portfolio"]["id"]),
        "security_id": UUID(graph["security"]["id"]),
        "as_of": date(2026, 1, 5),
        "ex_date": date(2026, 1, 5),
        "rule": "ORDINARY_PRE_EX_DATE",
        **changes,
    }


def load(engine: Engine, graph: dict[str, Any], **changes: Any) -> service.RecordedEligibilityBook:
    with unit_of_work(engine) as con:
        result = service.get(con, **arguments(graph, **changes))
    assert result is not None
    return result


def quantities(result: service.RecordedEligibilityBook) -> dict[str, str | None]:
    return {row.account_id: row.recorded_quantity for row in result.accounts}


class Rollback(Exception):
    """Abort synthetic corruption so triggers and data are restored."""


@contextmanager
def corrupt(engine: Engine, table: str) -> Iterator[Connection]:
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        triggers = con.exec_driver_sql(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (table,)
        ).scalars()
        for trigger in list(triggers):
            con.exec_driver_sql(f'DROP TRIGGER "{trigger}"')
        yield con
        raise Rollback
    assert database_snapshot(engine) == before


def expect_unavailable(con: Connection, graph: dict[str, Any]) -> None:
    with pytest.raises(service.EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"):
        service.get(con, **arguments(graph))


def split_body(graph: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "security_id": graph["security"]["id"],
        "action_type": "SPLIT",
        "effective_date": "2026-01-04",
        "ratio_numerator": 3,
        "ratio_denominator": 2,
        "status": "CONFIRMED",
        "reason": "Synthetic reviewed action terms",
        **changes,
    }


async def test_real_ledger_is_read_per_account_without_writes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    await create(
        client,
        "trades",
        trade_body(graph, account_id=graph["target"]["id"], trade_date="2026-01-02", quantity="5"),
    )
    await create(client, "security-transfers", stock_body(graph, transfer_date="2026-01-03"))
    await create(client, "corporate-actions", split_body(graph))
    await create(
        client, "trades", trade_body(graph, side="SELL", trade_date="2026-01-05", quantity="1")
    )
    before = database_snapshot(engine)
    result = load(engine, graph)
    assert quantities(result) == {
        graph["source"]["id"]: "9.00000000",
        graph["target"]["id"]: "13.50000000",
    }
    assert all(row.history_completeness == "UNVERIFIED" for row in result.accounts)
    assert result.rule == "ORDINARY_PRE_EX_DATE" and result.data_revision > 0
    assert result.trade_history_audit_scope == "NO_RETIRED_TRADES"
    assert graph["source"]["id"] not in repr(result) and "13.50000000" not in repr(result)
    future = load(engine, graph, as_of=date(2026, 1, 3))
    assert quantities(future) == {
        graph["source"]["id"]: "6.00000000",
        graph["target"]["id"]: "9.00000000",
    }
    assert all(row.state == "UPCOMING_RECORDED_HOLDINGS" for row in future.accounts)
    assert database_snapshot(engine) == before


async def test_owner_and_security_scope_do_not_expose_foreign_portfolios(
    engine: Engine, graph: dict[str, Any]
) -> None:
    with unit_of_work(engine) as con:
        assert service.get(con, **arguments(graph, portfolio_id=uuid4())) is None
        with pytest.raises(
            service.EligibilityHistoryError, match="^ELIGIBILITY_SECURITY_UNAVAILABLE$"
        ):
            service.get(con, **arguments(graph, security_id=uuid4()))
        with pytest.raises(
            service.EligibilityHistoryError, match="^ELIGIBILITY_OWNER_UNAVAILABLE$"
        ):
            service.get(con, **arguments(graph, owner_id=uuid4()))


async def test_unknown_or_special_convention_requires_explicit_review(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    result = load(engine, graph, rule="REQUIRES_REVIEW")
    assert result.rule == "REQUIRES_REVIEW"
    assert all(row.recorded_quantity is None for row in result.accounts)
    assert all(row.reasons == ("ELIGIBILITY_RULE_REQUIRES_REVIEW",) for row in result.accounts)


async def test_unresolved_action_is_never_inferred_as_an_adopted_split(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    await create(client, "security-transfers", stock_body(graph, transfer_date="2026-01-03"))
    action = await create(
        client,
        "corporate-actions",
        split_body(
            graph,
            action_type="MERGER",
            effective_date="2026-01-02",
            status="NEEDS_REVIEW",
            ratio_numerator=None,
            ratio_denominator=None,
        ),
    )
    before = database_snapshot(engine)
    result = load(engine, graph)
    assert all(
        row.recorded_quantity is None and row.reasons == ("UNALLOCATED_ACTION",)
        for row in result.accounts
    )
    assert all(row.pending_action_ids == (f"action:{action['id']}",) for row in result.accounts)
    assert database_snapshot(engine) == before


async def test_read_transaction_does_not_mix_a_concurrent_wal_commit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    with unit_of_work(engine) as con:
        first = service.get(con, **arguments(graph))
        await create(
            client,
            "trades",
            trade_body(
                graph, account_id=graph["target"]["id"], trade_date="2026-01-03", quantity="2"
            ),
        )
        assert service.get(con, **arguments(graph)) == first
    latest = load(engine, graph)
    assert first is not None and latest.data_revision > first.data_revision
    assert quantities(latest)[graph["target"]["id"]] == "2.00000000"


async def test_corrected_action_and_archived_account_retain_current_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    await create(client, "security-transfers", stock_body(graph, transfer_date="2026-01-02"))
    body = split_body(graph, effective_date="2026-01-03")
    action = await create(client, "corporate-actions", body)
    changed = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}",
        json={**body, "ratio_numerator": 2, "ratio_denominator": 1},
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    archived = await client.patch(
        f"/api/v1/accounts/{graph['target']['id']}",
        json={"status": "ARCHIVED"},
        headers=version_headers(client, 1),
    )
    assert archived.status_code == 200
    assert quantities(load(engine, graph)) == {
        graph["source"]["id"]: "12.00000000",
        graph["target"]["id"]: "8.00000000",
    }


async def test_wrong_fact_sequence_cannot_fabricate_replay_order(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    first = await create(client, "trades", trade_body(graph))
    other = await create(
        client,
        "trades",
        trade_body(graph, account_id=graph["target"]["id"], trade_date="2026-01-03"),
    )
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql(
            "UPDATE trades SET created_sequence=? WHERE id=?",
            (other["created_sequence"], first["id"]),
        )
        expect_unavailable(con, graph)


async def test_corrupt_trade_portfolio_cannot_hide_owned_account_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    other = await create(client, "portfolios", {"name": "Second synthetic portfolio"})
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql(
            "UPDATE trades SET portfolio_id=? WHERE id=?", (other["id"], trade["id"])
        )
        expect_unavailable(con, graph)


async def test_corrupt_confirmed_split_direction_cannot_be_replayed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET ratio_num=1,ratio_den=3 WHERE id=?", (action["id"],)
        )
        expect_unavailable(con, graph)


async def test_superseded_without_a_successor_cannot_hide_a_trade(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql("UPDATE trades SET status='SUPERSEDED' WHERE id=?", (trade["id"],))
        expect_unavailable(con, graph)


async def test_missing_current_transfer_cannot_hide_a_movement(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    transfer = await create(client, "security-transfers", stock_body(graph))
    with corrupt(engine, "security_transfers") as con:
        con.exec_driver_sql(
            "UPDATE security_transfers SET is_current=0 WHERE id=?", (transfer["id"],)
        )
        expect_unavailable(con, graph)


async def test_missing_current_action_cannot_hide_a_split(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql("UPDATE corporate_actions SET is_current=0 WHERE id=?", (action["id"],))
        expect_unavailable(con, graph)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("owner_id", "not-a-uuid"),
        ("portfolio_id", None),
        ("security_id", True),
        ("ex_date", datetime(2026, 1, 5)),
        ("as_of", "2026-01-05"),
        ("rule", []),
    ],
)
async def test_invalid_scope_inputs_never_reach_the_ledger(
    engine: Engine, graph: dict[str, Any], field: str, invalid: Any
) -> None:
    with (
        unit_of_work(engine) as con,
        pytest.raises(service.EligibilityHistoryError, match="^INVALID_ELIGIBILITY_SCOPE$"),
    ):
        service.get(con, **arguments(graph, **{field: invalid}))


async def test_caller_must_supply_a_transaction(engine: Engine, graph: dict[str, Any]) -> None:
    with engine.connect() as con:
        assert not con.in_transaction()
        with pytest.raises(service.EligibilityHistoryError, match="^INVALID_ELIGIBILITY_SCOPE$"):
            service.get(con, **arguments(graph))


async def test_trade_revision_may_move_between_owned_portfolios_and_securities(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    original = await create(client, "trades", trade_body(graph))
    portfolio = await create(client, "portfolios", {"name": "Other owned portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Other owned account",
            "default_currency": "USD",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "ALT",
            "name": "Other synthetic security",
            "quote_currency": "USD",
        },
    )
    response = await client.patch(
        f"/api/v1/trades/{original['id']}",
        json=trade_body(
            graph,
            portfolio_id=portfolio["id"],
            account_id=account["id"],
            security_id=security["id"],
            reason="Move synthetic trade to the corrected portfolio and security",
        ),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200, response.text
    old = load(engine, graph)
    assert all(row.recorded_quantity == "0.00000000" for row in old.accounts)
    assert old.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
    corrected = load(
        engine, graph, portfolio_id=UUID(portfolio["id"]), security_id=UUID(security["id"])
    )
    assert quantities(corrected) == {account["id"]: "10.00000000"}
    assert corrected.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"


async def test_security_transfer_revision_and_void_use_only_the_terminal_fact(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    transfer = await create(client, "security-transfers", stock_body(graph))
    updated = await client.patch(
        f"/api/v1/security-transfers/{transfer['id']}",
        json=stock_body(graph, quantity="3", reason="Correct the synthetic transfer quantity"),
        headers=version_headers(client, 1),
    )
    assert updated.status_code == 200, updated.text
    assert quantities(load(engine, graph)) == {
        graph["source"]["id"]: "7.00000000",
        graph["target"]["id"]: "3.00000000",
    }
    revised = updated.json()
    voided = await client.request(
        "DELETE",
        f"/api/v1/security-transfers/{revised['id']}",
        json={"reason": "Void the synthetic transfer after correction"},
        headers=version_headers(client, revised["version"]),
    )
    assert voided.status_code == 200, voided.text
    assert quantities(load(engine, graph)) == {
        graph["source"]["id"]: "10.00000000",
        graph["target"]["id"]: "0.00000000",
    }


async def test_voided_action_and_trade_do_not_reappear_from_old_revisions(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "15.00000000"
    void_action = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{action['id']}",
        json={"reason": "Void the synthetic adopted action"},
        headers=version_headers(client, 1),
    )
    assert void_action.status_code == 200
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"
    void_trade = await client.request(
        "DELETE",
        f"/api/v1/trades/{trade['id']}",
        json={"reason": "Void the synthetic completed trade"},
        headers=version_headers(client, 1),
    )
    assert void_trade.status_code == 200
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "0.00000000"


async def test_action_revision_may_explicitly_replace_a_split_with_symbol_metadata(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    body = split_body(
        graph,
        action_type="SYMBOL_CHANGE",
        ratio_numerator=None,
        ratio_denominator=None,
        new_symbol="NEW",
        status="NEEDS_REVIEW",
    )
    revised = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}", json=body, headers=version_headers(client, 1)
    )
    assert revised.status_code == 200, revised.text
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"


async def test_read_never_constructs_provider_clients_or_reads_private_configuration(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    before = database_snapshot(engine)
    with (
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("provider I/O")),
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private I/O")),
    ):
        assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"
    assert database_snapshot(engine) == before


async def test_trade_account_from_another_portfolio_cannot_be_replayed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    other = await create(client, "portfolios", {"name": "Other portfolio for corruption"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Other account",
            "default_currency": "USD",
        },
    )
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql(
            "UPDATE trades SET account_id=? WHERE id=?", (account["id"], trade["id"])
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("field", ["portfolio_id", "source_account_id", "target_account_id"])
async def test_cross_portfolio_transfer_corruption_is_rejected_in_both_directions(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], field: str
) -> None:
    await create(client, "trades", trade_body(graph))
    transfer = await create(client, "security-transfers", stock_body(graph))
    other = await create(client, "portfolios", {"name": "Other transfer portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Other transfer account",
            "default_currency": "USD",
        },
    )
    value = other["id"] if field == "portfolio_id" else account["id"]
    with corrupt(engine, "security_transfers") as con:
        con.exec_driver_sql(
            f"UPDATE security_transfers SET {field}=? WHERE id=?", (value, transfer["id"])
        )
        expect_unavailable(con, graph)


async def test_gap_in_retained_action_versions_cannot_hide_earlier_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    revised = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}",
        json=split_body(graph, ratio_numerator=2, ratio_denominator=1),
        headers=version_headers(client, 1),
    )
    assert revised.status_code == 200
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET revision=3 WHERE id=?", (revised.json()["id"],)
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("value", ["20260102", "2026-02-30", "2026-01-02T00:00:00"])
async def test_noncanonical_retained_trade_dates_cannot_be_silently_parsed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], value: str
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql("UPDATE trades SET trade_date=? WHERE id=?", (value, trade["id"]))
        expect_unavailable(con, graph)


@pytest.mark.parametrize(("field", "value"), [("revision", 1.5), ("version", 1.5)])
async def test_noninteger_retained_control_fields_cannot_hide_a_fact(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], field: str, value: float
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql(f"UPDATE trades SET {field}=? WHERE id=?", (value, trade["id"]))
        expect_unavailable(con, graph)


async def test_uncanonical_retained_root_identity_cannot_be_replayed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        con.exec_driver_sql(
            "UPDATE trades SET root_id=? WHERE id=?",
            ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", trade["id"]),
        )
        expect_unavailable(con, graph)


async def test_inactive_owner_and_foreign_owned_portfolio_are_not_authorized(
    engine: Engine, graph: dict[str, Any]
) -> None:
    foreign_owner, foreign_portfolio = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-internal-only','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (foreign_owner,),
        )
        con.exec_driver_sql(
            "INSERT INTO portfolios(id,owner_id,name,created_at) "
            "VALUES (?,?,'Other owner portfolio','2026-01-01T00:00:00Z')",
            (foreign_portfolio, foreign_owner),
        )
    with unit_of_work(engine) as con:
        assert service.get(con, **arguments(graph, portfolio_id=UUID(foreign_portfolio))) is None
        assert service.get(con, **arguments(graph, owner_id=UUID(foreign_owner))) is None
    with corrupt(engine, "owners") as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (graph["owner"]["id"],))
        with pytest.raises(
            service.EligibilityHistoryError, match="^ELIGIBILITY_OWNER_UNAVAILABLE$"
        ):
            service.get(con, **arguments(graph))


async def test_foreign_broker_association_cannot_authorize_an_account(
    engine: Engine, graph: dict[str, Any]
) -> None:
    foreign_owner, foreign_broker = str(uuid4()), str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-internal-only','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (foreign_owner,),
        )
        con.exec_driver_sql(
            "INSERT INTO brokers(id,owner_id,name) VALUES (?,?,'Other owner broker')",
            (foreign_broker, foreign_owner),
        )
    with corrupt(engine, "accounts") as con:
        con.exec_driver_sql(
            "UPDATE accounts SET broker_id=? WHERE id=?", (foreign_broker, graph["source"]["id"])
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("field", ["transfer", "action"])
async def test_foreign_ledger_sequence_cannot_be_used_by_a_transfer_or_action(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], field: str
) -> None:
    trade = await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if field == "transfer":
        entry = await create(client, "security-transfers", stock_body(graph))
        table = "security_transfers"
    else:
        entry = await create(client, "corporate-actions", split_body(graph))
        table = "corporate_actions"
    with corrupt(engine, table) as con:
        con.exec_driver_sql(
            f"UPDATE {table} SET created_sequence=? WHERE id=?",
            (trade["created_sequence"], entry["id"]),
        )
        expect_unavailable(con, graph)


async def test_missing_fx_scheduled_trade_is_never_activated_by_a_future_as_of_date(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    draft = await create(
        client, "trades", trade_body(graph, trade_date="2099-01-01", fx_rate_hkd=None)
    )
    assert draft["status"] == "DRAFT_SCHEDULED" and draft["booked_hkd"] is None
    before = database_snapshot(engine)
    result = load(engine, graph, ex_date=date(2099, 1, 3), as_of=date(2099, 1, 2))
    assert all(row.state == "UPCOMING_RECORDED_HOLDINGS" for row in result.accounts)
    assert all(row.recorded_quantity == "0.00000000" for row in result.accounts)
    assert database_snapshot(engine) == before


async def test_unallocated_action_cannot_be_silently_relabelled_confirmed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(
        client,
        "corporate-actions",
        split_body(
            graph,
            action_type="MERGER",
            status="NEEDS_REVIEW",
            ratio_numerator=None,
            ratio_denominator=None,
        ),
    )
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET status='CONFIRMED' WHERE id=?", (action["id"],)
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("value", [1.5, 2**63 - 0.5])
async def test_fractional_stored_action_ratios_never_enter_replay(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], value: float
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET ratio_num=? WHERE id=?", (value, action["id"])
        )
        expect_unavailable(con, graph)


async def test_an_earlier_action_revision_cannot_be_reparented_to_another_security(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    changed = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}",
        json=split_body(graph, ratio_numerator=2, ratio_denominator=1),
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    other = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "HIST",
            "name": "Other historical security",
            "quote_currency": "USD",
        },
    )
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET security_id=? WHERE id=?", (other["id"], action["id"])
        )
        expect_unavailable(con, graph)


async def test_predecessor_cannot_be_voided_behind_a_later_current_revision(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    changed = await client.patch(
        f"/api/v1/corporate-actions/{action['id']}",
        json=split_body(graph, ratio_numerator=2, ratio_denominator=1),
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET status='VOID' WHERE id=?", (action["id"],)
        )
        expect_unavailable(con, graph)


async def test_unrelated_active_trade_with_a_foreign_account_fails_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    other_security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHER",
            "name": "Other synthetic security",
            "quote_currency": "USD",
        },
    )
    other = await create(client, "portfolios", {"name": "Other corrupted portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Other corrupted account",
            "default_currency": "USD",
        },
    )
    trade = await create(client, "trades", trade_body(graph, security_id=other_security["id"]))
    with corrupt(engine, "trades") as con:
        con.exec_driver_sql(
            "UPDATE trades SET account_id=? WHERE id=?", (account["id"], trade["id"])
        )
        expect_unavailable(con, graph)


async def test_unrelated_active_transfer_with_a_foreign_account_fails_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    other_security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHER2",
            "name": "Other synthetic security",
            "quote_currency": "USD",
        },
    )
    other = await create(client, "portfolios", {"name": "Other corrupted portfolio"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": other["id"],
            "broker_id": graph["broker"]["id"],
            "name": "Other corrupted account",
            "default_currency": "USD",
        },
    )
    await create(client, "trades", trade_body(graph, security_id=other_security["id"]))
    transfer = await create(
        client, "security-transfers", stock_body(graph, security_id=other_security["id"])
    )
    with corrupt(engine, "security_transfers") as con:
        con.exec_driver_sql(
            "UPDATE security_transfers SET target_account_id=? WHERE id=?",
            (account["id"], transfer["id"]),
        )
        expect_unavailable(con, graph)


async def test_fractional_portfolio_revision_does_not_become_a_freshness_token(
    engine: Engine, graph: dict[str, Any]
) -> None:
    with corrupt(engine, "portfolios") as con:
        con.exec_driver_sql(
            "UPDATE portfolios SET data_revision=1.5 WHERE id=?", (graph["portfolio"]["id"],)
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("value", [None, True, 1, "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
async def test_internal_canonical_uuid_decoder_rejects_unsafe_values(value: Any) -> None:
    with pytest.raises(ValueError):
        service._canonical_uuid(value)


@pytest.mark.parametrize("value", [None, True, 1, "20260102", "2026-02-30"])
async def test_internal_day_decoder_rejects_noncanonical_values(value: Any) -> None:
    with pytest.raises(ValueError):
        service._day(value)


async def test_owned_snapshot_can_be_reloaded_inside_a_writer_without_writes(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph))
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        assert con.get_execution_options().get("writer") is True
        result = service.get(con, **arguments(graph))
        assert result is not None and quantities(result)[graph["source"]["id"]] == "10.00000000"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["TRADE", "TRANSFER", "ACTION"])
async def test_unaudited_void_cannot_make_an_active_fact_disappear(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    trade = await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if kind == "TRADE":
        table, record = "trades", trade
    elif kind == "TRANSFER":
        table = "security_transfers"
        record = await create(client, "security-transfers", stock_body(graph))
    else:
        table = "corporate_actions"
        record = await create(client, "corporate-actions", split_body(graph))
    with corrupt(engine, table) as con:
        con.exec_driver_sql(
            f"UPDATE {table} SET status='VOID',version=version+1 WHERE id=?", (record["id"],)
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("kind", ["TRADE", "TRANSFER", "ACTION"])
async def test_tampered_current_terms_cannot_become_new_recorded_shares(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    trade = await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if kind == "TRADE":
        table, record, field, value = "trades", trade, "quantity_e8", 1100000000
    elif kind == "TRANSFER":
        table, field, value = "security_transfers", "quantity_e8", 500000000
        record = await create(client, "security-transfers", stock_body(graph))
    else:
        table, field, value = "corporate_actions", "ratio_num", 4
        record = await create(client, "corporate-actions", split_body(graph))
    with corrupt(engine, table) as con:
        con.exec_driver_sql(f"UPDATE {table} SET {field}=? WHERE id=?", (value, record["id"]))
        expect_unavailable(con, graph)


async def test_missing_current_fact_audit_never_counts_as_authorized_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "DELETE FROM audit_events WHERE entity_type='trade' AND entity_id=?", (trade["id"],)
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("kind", ["TRANSFER", "ACTION"])
async def test_later_revision_cannot_hide_a_corrupt_prior_fact_audit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if kind == "TRANSFER":
        record = await create(client, "security-transfers", stock_body(graph))
        path = "security-transfers"
        body = stock_body(graph, quantity="3", reason="Correct synthetic transfer")
        entity = "security_transfer"
    else:
        record = await create(client, "corporate-actions", split_body(graph))
        path = "corporate-actions"
        body = split_body(graph, ratio_numerator=2, ratio_denominator=1)
        entity = "corporate_action"
    response = await client.patch(
        f"/api/v1/{path}/{record['id']}", json=body, headers=version_headers(client, 1)
    )
    assert response.status_code == 200
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type=? AND entity_id=? AND action='CREATE'",
            ("0" * 64, entity, record["id"]),
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("kind", ["TRANSFER", "ACTION"])
async def test_revision_audit_must_link_its_actual_predecessor(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if kind == "TRANSFER":
        record = await create(client, "security-transfers", stock_body(graph))
        path, entity = "security-transfers", "security_transfer"
        body = stock_body(graph, quantity="3", reason="Correct synthetic transfer")
    else:
        record = await create(client, "corporate-actions", split_body(graph))
        path, entity = "corporate-actions", "corporate_action"
        body = split_body(graph, ratio_numerator=2, ratio_denominator=1)
    response = await client.patch(
        f"/api/v1/{path}/{record['id']}", json=body, headers=version_headers(client, 1)
    )
    assert response.status_code == 200
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET before_sha256=? "
            "WHERE entity_type=? AND entity_id=? AND action='REVISE'",
            ("0" * 64, entity, response.json()["id"]),
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("kind", ["TRADE", "TRANSFER", "ACTION"])
async def test_creation_audit_reason_must_match_the_retained_fact(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    trade = await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if kind == "TRADE":
        record, entity = trade, "trade"
    elif kind == "TRANSFER":
        record = await create(client, "security-transfers", stock_body(graph))
        entity = "security_transfer"
    else:
        record = await create(client, "corporate-actions", split_body(graph))
        entity = "corporate_action"
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET reason='tampered reason' "
            "WHERE entity_type=? AND entity_id=? AND action='CREATE'",
            (entity, record["id"]),
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize("kind", ["TRANSFER", "ACTION"])
async def test_void_audit_must_link_the_previously_active_fact(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], kind: str
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    if kind == "TRANSFER":
        record = await create(client, "security-transfers", stock_body(graph))
        path, entity = "security-transfers", "security_transfer"
    else:
        record = await create(client, "corporate-actions", split_body(graph))
        path, entity = "corporate-actions", "corporate_action"
    response = await client.request(
        "DELETE",
        f"/api/v1/{path}/{record['id']}",
        json={"reason": "Void the synthetic financial fact"},
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET before_sha256=? "
            "WHERE entity_type=? AND entity_id=? AND action='VOID'",
            ("0" * 64, entity, record["id"]),
        )
        expect_unavailable(con, graph)


async def test_retired_trade_audit_reconstruction_limit_is_explicit(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    changed = await client.patch(
        f"/api/v1/trades/{trade['id']}",
        json=trade_body(graph, quantity="11", reason="Correct the synthetic recorded quantity"),
        headers=version_headers(client, 1),
    )
    assert changed.status_code == 200
    original = load(engine, graph)
    assert original.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
    assert quantities(original)[graph["source"]["id"]] == "11.00000000"
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='trade' AND entity_id=? AND action='CREATE'",
            ("0" * 64, trade["id"]),
        )
        result = service.get(con, **arguments(graph))
        assert result is not None
        assert result.trade_history_audit_scope == "RETIRED_TRADE_HISTORY_UNVERIFIED"
        assert quantities(result)[graph["source"]["id"]] == "11.00000000"


async def test_audit_actor_must_match_the_owned_trade_portfolio(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    foreign_owner = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-internal','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (foreign_owner,),
        )
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET owner_id=? WHERE entity_type='trade' AND entity_id=?",
            (foreign_owner, trade["id"]),
        )
        expect_unavailable(con, graph)


async def test_audit_reason_must_be_text(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET reason=? WHERE entity_type='trade' AND entity_id=?",
            (b"synthetic-non-text", trade["id"]),
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00.000000+00:00",
        "2026-01-01T00:00:00.000000",
        "not-a-date",
        b"non-text",
    ],
)
async def test_audit_timestamp_must_be_canonical_utc(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], timestamp: Any
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET created_at=? WHERE entity_type='trade' AND entity_id=?",
            (timestamp, trade["id"]),
        )
        expect_unavailable(con, graph)


@pytest.mark.parametrize(
    ("field", "value"), [("after_sha256", "not-a-digest"), ("before_sha256", "0" * 64)]
)
async def test_audit_fingerprint_shape_and_create_before_are_checked(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], field: str, value: str
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            f"UPDATE audit_events SET {field}=? "
            "WHERE entity_type='trade' AND entity_id=? AND action='CREATE'",
            (value, trade["id"]),
        )
        expect_unavailable(con, graph)


async def test_void_after_fingerprint_must_match_the_current_version(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    trade = await create(client, "trades", trade_body(graph))
    result = await client.request(
        "DELETE",
        f"/api/v1/trades/{trade['id']}",
        json={"reason": "Void the synthetic original trade"},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200
    with corrupt(engine, "audit_events") as con:
        con.exec_driver_sql(
            "UPDATE audit_events SET after_sha256=? "
            "WHERE entity_type='trade' AND entity_id=? AND action='VOID'",
            ("0" * 64, trade["id"]),
        )
        expect_unavailable(con, graph)


async def test_confirmed_symbol_change_and_review_only_void_retain_real_audits(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    symbol = await create(
        client,
        "corporate-actions",
        split_body(
            graph,
            action_type="SYMBOL_CHANGE",
            ratio_numerator=None,
            ratio_denominator=None,
            new_symbol="SYMB",
            status="CONFIRMED",
            effective_date="2026-01-02",
        ),
    )
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"
    result = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{symbol['id']}",
        json={"reason": "Void the synthetic symbol change"},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"
    merger = await create(
        client,
        "corporate-actions",
        split_body(
            graph,
            action_type="MERGER",
            ratio_numerator=None,
            ratio_denominator=None,
            status="NEEDS_REVIEW",
            effective_date="2026-01-03",
        ),
    )
    states = {row.account_id: row.state for row in load(engine, graph).accounts}
    assert states[graph["source"]["id"]] == "NEEDS_REVIEW"
    result = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{merger['id']}",
        json={"reason": "Void the synthetic merger review"},
        headers=version_headers(client, 1),
    )
    assert result.status_code == 200
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"


async def test_invalid_action_type_cannot_be_silently_ignored(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))
    action = await create(client, "corporate-actions", split_body(graph))
    with corrupt(engine, "corporate_actions") as con:
        con.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            con.exec_driver_sql(
                "UPDATE corporate_actions SET action_type='UNKNOWN' WHERE id=?", (action["id"],)
            )
            expect_unavailable(con, graph)
        finally:
            con.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
