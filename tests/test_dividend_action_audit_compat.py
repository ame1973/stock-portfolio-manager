"""Action audit reads retain compatibility with exact-version response additions."""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.action_service as actions
import spm_api.dividend_eligibility_service as eligibility
from spm_api.audit import audit, canonical, digest
from spm_api.db import unit_of_work
from tests.helpers import create, database_snapshot, version_headers
from tests.test_dividend_eligibility_service import load, quantities, split_body
from tests.test_transfers import graph as graph
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("legacy_create", [False, True])
@pytest.mark.parametrize("legacy_revision", [False, True])
async def test_recorded_action_audit_survives_legacy_and_exact_versions(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    legacy_create: bool,
    legacy_revision: bool,
) -> None:
    await create(client, "trades", trade_body(graph, trade_date="2026-01-01"))

    def legacy_audit(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("after") is not None and args[2] == "corporate_action":
            kwargs["after"] = {k: v for k, v in kwargs["after"].items() if k != "version_exact"}
        audit(*args, **kwargs)

    with monkeypatch.context() as context:
        if legacy_create:
            context.setattr(actions, "audit", legacy_audit)
        first = await create(client, "corporate-actions", split_body(graph))
    before = database_snapshot(engine)
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "15.00000000"
    assert database_snapshot(engine) == before

    with monkeypatch.context() as context:
        if legacy_revision:
            context.setattr(actions, "audit", legacy_audit)
        response = await client.patch(
            f"/api/v1/corporate-actions/{first['id']}",
            json=split_body(graph, ratio_numerator=2, ratio_denominator=1),
            headers=version_headers(client, 1),
        )
    assert response.status_code == 200, response.text
    second = response.json()
    with unit_of_work(engine) as con:
        for record, legacy in ((first, legacy_create), (second, legacy_revision)):
            retained_hash = con.exec_driver_sql(
                "SELECT after_sha256 FROM audit_events "
                "WHERE entity_type='corporate_action' AND entity_id=?",
                (record["id"],),
            ).scalar_one()
            expected = {k: v for k, v in record.items() if not legacy or k != "version_exact"}
            assert retained_hash == digest(canonical(expected))
    before = database_snapshot(engine)
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "20.00000000"
    assert database_snapshot(engine) == before

    voided = await client.request(
        "DELETE",
        f"/api/v1/corporate-actions/{second['id']}",
        json={"reason": "Void this synthetic action"},
        headers=version_headers(client, 1),
    )
    assert voided.status_code == 200, voided.text
    before = database_snapshot(engine)
    assert quantities(load(engine, graph))[graph["source"]["id"]] == "10.00000000"
    assert database_snapshot(engine) == before


async def test_action_audit_with_inconsistent_exact_version_is_not_accepted(
    client: httpx.AsyncClient,
    engine: Engine,
    graph: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = await create(client, "corporate-actions", split_body(graph))
    original_entry = eligibility._audit_entry

    def mismatched_entry(*args: Any, **kwargs: Any) -> Any:
        entry = original_entry(*args, **kwargs)
        if args[2] == "ACTION" and args[3] == "CREATE":
            return {**entry, "after_sha256": digest(canonical({**action, "version_exact": "2"}))}
        return entry

    before = database_snapshot(engine)
    monkeypatch.setattr(eligibility, "_audit_entry", mismatched_entry)
    with pytest.raises(
        eligibility.EligibilityHistoryError, match="^ELIGIBILITY_HISTORY_UNAVAILABLE$"
    ):
        load(engine, graph)
    assert database_snapshot(engine) == before
