"""A prospective rebuild must pin the current security without granting market validity."""

from dataclasses import FrozenInstanceError, replace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
from spm_api.db import unit_of_work
from spm_api.master_service import as_security
from tests.helpers import create, database_snapshot, version_headers
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "DELISTED"},
        {"name": "Synthetic corrected security name"},
        {"verified": True, "reason": "Synthetic explicit verification decision"},
    ],
)
async def test_real_security_update_stales_revision_observation_without_changing_holdings(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    scope: dict[str, Any],
    changes: dict[str, Any],
) -> None:
    response = await client.patch(
        f"/api/v1/securities/{graph['security']['id']}",
        json=changes,
        headers=version_headers(client, graph["security"]["version"]),
    )
    assert response.status_code == 200
    assert response.json()["version"] == graph["security"]["version"] + 1
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


async def test_same_transaction_security_update_requires_fresh_observation_and_rolls_back(
    engine: Engine, graph: dict[str, Any], scope: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
        con.exec_driver_sql(
            "UPDATE securities SET status='SUSPENDED',version=version+1 WHERE id=?",
            (graph["security"]["id"],),
        )
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("exchange_code", "NYSE"),
        ("display_ticker", "TRNX"),
        ("name", "Synthetic changed name"),
        ("security_type", "ETF"),
        ("quote_currency", "HKD"),
        ("isin", "US0378331005"),
        ("status", "SUSPENDED"),
        ("verified", 1),
        ("created_at", "2026-01-01T00:00:00.000000Z"),
    ],
)
async def test_unversioned_catalogue_changes_cannot_hide_in_the_same_transaction(
    engine: Engine, graph: dict[str, Any], scope: dict[str, Any], column: str, value: str | int
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        con.exec_driver_sql(
            f"UPDATE securities SET {column}=? WHERE id=?", (value, graph["security"]["id"])
        )
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


async def test_missing_catalogue_row_has_no_security_pin(engine: Engine) -> None:
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(ValueError, match="^Invalid current security scope$"),
    ):
        rebuild._security_scope(con, uuid4())
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("malformed", ["version", "created_at"])
async def test_malformed_security_values_cannot_be_current_pins(
    engine: Engine, graph: dict[str, Any], scope: dict[str, Any], malformed: str
) -> None:
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
        unit_of_work(engine, write=True) as con,
    ):
        statement = (
            "UPDATE securities SET version=1.5 WHERE id=?"
            if malformed == "version"
            else "UPDATE securities SET created_at=X'01' WHERE id=?"
        )
        con.exec_driver_sql(statement, (graph["security"]["id"],))
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["name", "id", "version", "verified", "extra", "object"])
async def test_substituted_security_model_is_rejected_without_disclosing_values(
    engine: Engine, graph: dict[str, Any], scope: dict[str, Any], change: str
) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        row = (
            con.exec_driver_sql("SELECT * FROM securities WHERE id=?", (graph["security"]["id"],))
            .mappings()
            .one()
        )
        security = as_security(row)
        changes: dict[str, Any] = {
            "name": {"name": "Synthetic substituted value"},
            "id": {"id": uuid4()},
            "version": {"version": True},
            "verified": {"verified": 1},
            "extra": {"private_extra": "synthetic"},
        }
        forged: Any = (
            object() if change == "object" else security.model_copy(update=changes[change])
        )
        with (
            patch.object(rebuild, "as_security", return_value=forged),
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_OBSERVATION_UNAVAILABLE$"),
        ):
            rebuild.observe(
                con,
                scope["book"]["owner"],
                scope["decision"].event_id,
                scope["decision"],
                scope["proposal"],
            )
    assert database_snapshot(engine) == before


async def test_security_pin_is_immutable_private_and_not_a_verification_grant(
    engine: Engine, graph: dict[str, Any], scope: dict[str, Any]
) -> None:
    observed = scope["observed"]
    security = observed.security
    assert str(security.id) == graph["security"]["id"]
    assert security.version == graph["security"]["version"]
    assert security.verified is False and observed.eligibility_scope == "NOT_ESTABLISHED"
    assert graph["security"]["id"] not in repr(security)
    assert graph["security"]["name"] not in repr(security)
    assert graph["security"]["display_ticker"] not in repr(security)
    with pytest.raises(FrozenInstanceError):
        attribute = "version"
        setattr(security, attribute, security.version + 1)
    forged = replace(observed, security=replace(security, version=True))
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], forged
        )


async def test_security_pin_and_preview_share_the_same_wal_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], scope: dict[str, Any]
) -> None:
    owner, decision, proposed = scope["book"]["owner"], scope["decision"], scope["proposal"]
    with unit_of_work(engine) as reader:
        observed = rebuild.observe(reader, owner, decision.event_id, decision, proposed)
        assert observed is not None
        changed = await client.patch(
            f"/api/v1/securities/{graph['security']['id']}",
            json={"status": "DELISTED"},
            headers=version_headers(client, graph["security"]["version"]),
        )
        assert changed.status_code == 200
        assert rebuild.observe(reader, owner, decision.event_id, decision, proposed) == observed
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        fresh = rebuild.observe(con, owner, decision.event_id, decision, proposed)
        assert fresh is not None and fresh.preview == observed.preview
        assert fresh.security.status == "DELISTED" and fresh.security != observed.security
        with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
            current.require_current(con, owner, decision, proposed, observed)
    assert database_snapshot(engine) == before


async def test_unrelated_security_update_does_not_stale_revision_scope(
    client: httpx.AsyncClient,
    engine: Engine,
    scope: dict[str, Any],
) -> None:
    other = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OTHR",
            "name": "Synthetic unrelated security",
            "quote_currency": "USD",
        },
    )
    changed = await client.patch(
        f"/api/v1/securities/{other['id']}",
        json={"status": "DELISTED"},
        headers=version_headers(client, other["version"], key=str(uuid4())),
    )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        result = current.require_current(
            con, scope["book"]["owner"], scope["decision"], scope["proposal"], scope["observed"]
        )
    assert result == scope["observed"]
    assert database_snapshot(engine) == before
