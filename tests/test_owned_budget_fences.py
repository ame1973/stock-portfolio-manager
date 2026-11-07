"""Owned shared-origin peers must remain current in the grant/result transaction, not only I/O."""

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.providers.budgets import load_budget
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.selection import OwnerConfigurationStore, validate_configuration
from spm_api.providers.symbols import resolve_symbol
from tests.helpers import create, database_snapshot, version_headers
from tests.test_profile_api import PATH, changed
from tests.test_profile_api import source as source
from tests.test_provider_configuration import policy, provision
from tests.test_provider_persistence import WINDOW
from tests.test_provider_persistence import book as book
from tests.test_provider_selection import local as local
from tests.test_provider_selection import manage, store
from tests.test_provider_yahoo import chart, event, response
from tests.test_upstream_execution import complete, new_work, reserve

pytestmark = pytest.mark.anyio


@pytest.fixture
async def shared(
    source: dict[str, Any], local: Any, client: httpx.AsyncClient, engine: Engine
) -> dict[str, Any]:
    primary = await manage(client, "yahoo", minute=5, day=50)
    peer = await manage(client, "yfinance", minute=2, day=20)
    await create(
        client,
        f"securities/{source['security']['id']}/provider-symbols",
        {
            "provider_id": "yahoo",
            "provider_symbol": "0005.HK",
            "effective_from": "2020-01-01",
        },
    )
    selected_store = store(local, engine, source)
    loaded = selected_store.load("yahoo")
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(source["security"]["id"]), "yahoo", WINDOW).segments[0]
    prepared = prepare_fetch(UUID(source["owner"]["id"]), loaded, segment, "DIVIDENDS")
    return {
        **source,
        "primary": primary,
        "peer": peer,
        "prepared": prepared,
        "store": selected_store,
    }


async def lower_peer(client: httpx.AsyncClient, record: dict[str, Any]) -> dict[str, Any]:
    result = await client.patch(
        f"{PATH}/yfinance",
        headers=version_headers(client, record["version"]),
        json=changed(
            record, policy={**record["policy"], "requests_per_minute": 1, "requests_per_day": 10}
        ),
    )
    assert result.status_code == 200
    return dict(result.json())


async def fetched(case: dict[str, Any]) -> Any:
    async with configured_adapter(
        case["prepared"].configuration,
        transport=httpx.MockTransport(
            lambda _: response(chart({"dividends": event(currency="USD")}))
        ),
    ) as adapter:
        return await adapter.fetch_dividends(case["prepared"].segment)


async def test_peer_limit_change_between_private_check_and_grant_cannot_spend_old_limits(
    shared: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = new_work(shared, engine)
    await lower_peer(client, shared["peer"])
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        reserve(engine, case)
    assert database_snapshot(engine) == before


async def test_peer_change_after_http_rejects_the_whole_new_result_without_refunding(
    shared: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = new_work(shared, engine)
    grant = reserve(engine, case)
    page = await fetched(case)
    assert page.rows[0].dps == "0.90000000"
    await lower_peer(client, shared["peer"])
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before


async def test_unchanged_owned_peers_use_the_exact_shared_minimum_and_commit_atomically(
    shared: dict[str, Any], engine: Engine
) -> None:
    case = new_work(shared, engine)
    budget = load_budget(shared["store"], "yahoo")
    assert budget.limits.requests_per_minute == 2 and budget.limits.requests_per_day == 20
    grant = reserve(engine, case)
    page = await fetched(case)
    outcome = complete(engine, case, grant.attempt.id, page)
    assert outcome.receipt.observations_new == 1
    before = database_snapshot(engine)
    assert complete(engine, case, grant.attempt.id, page).replayed
    assert not reserve(engine, case, identifier=grant.attempt.id).attempt.created
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("baseline", ["absent", "disabled", "operator", "invalid_operator"])
@pytest.mark.parametrize("phase", ["reserve", "complete"])
async def test_inactive_or_operator_peer_cannot_change_into_a_new_owned_decision_in_the_gap(
    shared: dict[str, Any], client: httpx.AsyncClient, engine: Engine, baseline: str, phase: str
) -> None:
    record = shared["peer"]
    if baseline == "disabled":
        response = await client.patch(
            f"{PATH}/yfinance",
            headers=version_headers(client, 2),
            json=changed(record, policy={**record["policy"], "enabled": False}),
        )
        assert response.status_code == 200
        record = response.json()
    else:
        with engine.begin() as con:
            con.exec_driver_sql("DROP TRIGGER trg_provider_profile_no_delete")
            con.exec_driver_sql(
                "DELETE FROM provider_profile_revisions WHERE provider_id='yfinance'"
            )
        if baseline in ("operator", "invalid_operator"):
            provision(
                shared["settings"],
                "yfinance",
                body=(
                    policy("yfinance", requests_per_minute=3, requests_per_day=30)
                    if baseline == "operator"
                    else {"unsupported": "synthetic invalid metadata"}
                ),
            )
    case = new_work(shared, engine)
    grant = reserve(engine, case) if phase == "complete" else None
    page = await fetched(case) if grant is not None else None
    if baseline == "disabled":
        result = await client.patch(
            f"{PATH}/yfinance",
            headers=version_headers(client, record["version"]),
            json=changed(
                record,
                policy={
                    **record["policy"],
                    "enabled": True,
                    "requests_per_minute": 1,
                    "requests_per_day": 10,
                },
            ),
        )
        assert result.status_code == 200
    else:
        await manage(client, "yfinance", minute=1, day=10)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        if grant is None:
            reserve(engine, case)
        else:
            complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["reserve", "complete"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_peer",
        "duplicate_peer",
        "unscoped_peer",
        "foreign_peer",
        "foreign_origin",
        "wrong_provider",
        "primary_policy",
        "primary_revision",
        "primary_error",
        "looser_limits",
        "missing_limit_member",
        "changed_limit_hash",
    ],
)
async def test_owned_attempt_rejects_incomplete_or_substituted_budget_envelopes_before_writes(
    shared: dict[str, Any], engine: Engine, phase: str, fault: str
) -> None:
    case = new_work(shared, engine)
    grant = reserve(engine, case) if phase == "complete" else None
    page = await fetched(case) if grant is not None else None
    budget = case["budget"]
    primary = budget.primary
    peer = next(value for value in budget.configurations if value.provider_id == "yfinance")
    if fault == "missing_peer":
        forged = replace(budget, configurations=(primary,))
    elif fault == "duplicate_peer":
        forged = replace(budget, configurations=(primary, primary))
    elif fault in ("unscoped_peer", "foreign_peer"):
        forged = replace(
            budget,
            configurations=(
                primary,
                replace(peer, selection_owner_id=None if fault == "unscoped_peer" else uuid4()),
            ),
        )
    elif fault == "foreign_origin":
        forged = replace(budget, configurations=(primary, shared["store"].load("eodhd")))
    elif fault == "wrong_provider":
        forged = replace(budget, provider_id="eodhd")
    elif fault.startswith("primary_"):
        if fault == "primary_policy":
            altered = replace(primary, profile=primary.profile.model_copy(update={"version": 3}))
        elif fault == "primary_revision":
            altered = replace(primary, revision=replace(primary.revision, profile_sha256="f" * 64))
        else:
            altered = replace(primary, error_code="SYNTHETIC_CHANGED_STATE")
        forged = replace(budget, configurations=(altered, peer))
    else:
        limits = budget.limits
        if fault == "looser_limits":
            limits = limits.model_copy(update={"requests_per_minute": 5})
        elif fault == "missing_limit_member":
            limits = limits.model_copy(update={"members": (limits.members[0],)})
        else:
            limits = limits.model_copy(
                update={
                    "members": tuple(
                        value.model_copy(update={"profile_sha256": "f" * 64})
                        for value in limits.members
                    )
                }
            )
        forged = replace(budget, limits=limits)
    case["budget"] = forged
    if phase == "reserve":
        case["limits"] = forged.limits
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        if grant is None:
            reserve(engine, case)
        else:
            complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before


async def test_new_grant_cannot_substitute_limits_without_the_matching_snapshot(
    shared: dict[str, Any], engine: Engine
) -> None:
    case = new_work(shared, engine)
    case["limits"] = case["limits"].model_copy(update={"requests_per_minute": 5})
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        reserve(engine, case)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("phase", ["reserve", "complete"])
async def test_owned_attempt_requires_the_observed_private_budget_with_unchanged_primary_policy(
    shared: dict[str, Any], engine: Engine, phase: str
) -> None:
    case = new_work(shared, engine)
    grant = reserve(engine, case) if phase == "complete" else None
    page = await fetched(case) if grant is not None else None
    case["budget"] = None
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        if grant is None:
            reserve(engine, case)
        else:
            complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before


async def test_fresh_current_peer_snapshot_cannot_replace_the_policy_reserved_before_http(
    shared: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = new_work(shared, engine)
    grant = reserve(engine, case)
    page = await fetched(case)
    await lower_peer(client, shared["peer"])
    case["budget"] = load_budget(shared["store"], "yahoo")
    assert case["budget"].unchanged(shared["store"])
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
        complete(engine, case, grant.attempt.id, page)
    assert database_snapshot(engine) == before


async def test_committed_replays_ignore_later_peer_changes_and_need_no_private_snapshot(
    shared: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    case = new_work(shared, engine)
    grant = reserve(engine, case)
    page = await fetched(case)
    original = complete(engine, case, grant.attempt.id, page)
    await lower_peer(client, shared["peer"])
    case["budget"] = None
    before = database_snapshot(engine)
    replay = complete(engine, case, grant.attempt.id, page)
    assert replay.replayed and replay.receipt == original.receipt
    assert not reserve(engine, case, identifier=grant.attempt.id).attempt.created
    assert database_snapshot(engine) == before


async def test_peer_writers_use_database_metadata_without_private_loading(
    shared: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = new_work(shared, engine)
    page = await fetched(case)
    monkeypatch.setattr(
        OwnerConfigurationStore, "load", lambda *_: pytest.fail("Private owner read inside writer")
    )
    monkeypatch.setattr(
        ProviderConfigurationStore,
        "load",
        lambda *_: pytest.fail("Private file read inside writer"),
    )
    grant = reserve(engine, case)
    assert complete(engine, case, grant.attempt.id, page).receipt.observations_new == 1


async def test_peer_and_configuration_fences_require_an_actual_writer(
    shared: dict[str, Any], engine: Engine
) -> None:
    budget = load_budget(shared["store"], "yahoo")
    owner = UUID(shared["owner"]["id"])
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        with pytest.raises(ValueError, match="WRITE_UNIT_OF_WORK_REQUIRED"):
            budget.validate_selection(con, owner, budget.primary)
        with pytest.raises(ValueError, match="WRITE_UNIT_OF_WORK_REQUIRED"):
            validate_configuration(con, budget.primary, owner)
    assert database_snapshot(engine) == before


async def test_peer_snapshot_cannot_be_reused_for_a_foreign_or_unscoped_primary(
    shared: dict[str, Any], engine: Engine
) -> None:
    budget = load_budget(shared["store"], "yahoo")
    owner = UUID(shared["owner"]["id"])
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        for primary in (
            replace(budget.primary, selection_owner_id=None),
            replace(budget.primary, selection_owner_id=uuid4()),
        ):
            with pytest.raises(ValueError, match="PROVIDER_CONFIG_CHANGED"):
                budget.validate_selection(con, owner, primary)
        with pytest.raises(ValueError, match="PROVIDER_OWNER_MISMATCH"):
            validate_configuration(con, budget.primary, uuid4())
    assert database_snapshot(engine) == before
