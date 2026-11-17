"""Projection input pins are one owner-scoped snapshot, never economic authority."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_fee_service as fees
import spm_api.dividend_fee_writer as fee_writer
import spm_api.dividend_projection_history as projections
import spm_api.dividend_projection_input_models as models
import spm_api.dividend_projection_input_routes as routes
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.dividend_account_scope import account as owned_account_scope
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_fee_writer import intent as fee_intent
from tests.test_dividend_projection_api import book as book
from tests.test_dividend_projection_writer import intent, save, settings
from tests.test_dividend_tax_service import intent as tax_intent
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio
QUERY = {
    "as_of": "2026-01-08",
    "eligibility_rule": "ORDINARY_PRE_EX_DATE",
    "tax_effective_on": "2026-01-08",
    "fee_effective_on": "2026-01-08",
}


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/projection-inputs"
    )


async def test_inputs_require_explicit_dates_and_return_one_unverified_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    assert (await client.get(path(book))).status_code == 422
    for field in QUERY:
        assert (
            await client.get(path(book), params={k: v for k, v in QUERY.items() if k != field})
        ).status_code == 422
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["event_id"] == str(book["event"].event_id)
    assert data["revision_id"] == str(book["event"].revision_id) and data["revision_no"] == 1
    assert data["ex_date"] == "2026-01-08" and data["currency"] == "USD"
    assert data["account_id"] == str(book["account_id"]) and data["portfolio_id"] == str(
        book["portfolio_id"]
    )
    assert data["broker_id"] == str(book["broker_id"])
    assert data["account_version"] == str(book["account_version"])
    assert data["broker_version"] == str(book["broker_version"])
    assert isinstance(data["portfolio_data_revision"], str)
    assert data["projection_head_no"] == "0" and data["projection_head_id"] is None
    assert data["as_of"] == QUERY["as_of"] and data["eligibility_rule"] == QUERY["eligibility_rule"]
    assert data["tax"]["effective_on"] == QUERY["tax_effective_on"]
    assert data["fee"]["effective_on"] == QUERY["fee_effective_on"]
    assert data["tax"]["status"] == data["fee"]["status"] == "MISSING"
    assert data["tax"]["head_no"] == data["fee"]["head_no"] == "0"
    assert data["tax"]["selected_id"] is None and data["fee"]["selected_id"] is None
    assert data["rejection_state"] == "NOT_REJECTED"
    assert data["scope"] == "RECORDED_PROJECTION_INPUTS_ONLY"
    assert data["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert data["source_scope"] == "CURRENT_SOURCE_NOT_REVALIDATED"
    assert data["eligibility_scope"] == "NOT_ESTABLISHED"
    assert "owner_id" not in response.text and "audit_id" not in response.text
    assert database_snapshot(engine) == before


async def test_inputs_pin_current_estimates_and_projection_head_without_assuming_eligibility(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    tax_setting, fee_setting = settings(engine, book, rate="0", fee=None, fee_currency="USD")
    projection = save(engine, book, intent(engine, book, tax_setting, fee_setting), uuid4())
    before = database_snapshot(engine)
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["projection_head_no"] == "1" and data["projection_head_id"] == str(
        projection.version.id
    )
    assert data["tax"] == {
        "effective_on": "2026-01-08",
        "head_no": "1",
        "head_id": str(tax_setting.id),
        "selected_id": str(tax_setting.id),
        "status": "ESTIMATED",
        "selected_scope": "ACCOUNT_SCOPE_MATCH",
        "scope": "OWNER_ESTIMATE_ONLY",
    }
    assert data["fee"] == {
        "effective_on": "2026-01-08",
        "head_no": "1",
        "head_id": str(fee_setting.id),
        "selected_id": str(fee_setting.id),
        "status": "UNKNOWN",
        "selected_scope": "ACCOUNT_SCOPE_MATCH",
        "scope": "OWNER_ESTIMATE_ONLY",
    }
    assert data["eligibility_scope"] == "NOT_ESTABLISHED" and database_snapshot(engine) == before


async def test_input_snapshot_labels_changed_account_settings_without_rebinding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    settings(engine, book)
    response = await client.patch(
        f"/api/v1/accounts/{book['account_id']}",
        json={"name": "Synthetic renamed account"},
        headers=version_headers(client, book["account_version"]),
    )
    assert response.status_code == 200
    before = database_snapshot(engine)
    data = (await client.get(path(book), params=QUERY)).json()
    assert data["account_version"] == str(book["account_version"] + 1)
    assert data["tax"]["selected_scope"] == data["fee"]["selected_scope"] == "ACCOUNT_SCOPE_CHANGED"
    assert database_snapshot(engine) == before


async def test_rejected_current_revision_is_visible_but_never_a_clear_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = book["event"]
    rejected = await client.post(
        f"/api/v1/dividends/{event.event_id}/verify",
        headers=version_headers(client, 1),
        json={
            "revision_id": str(event.revision_id),
            "decision": "REJECT_REVISION",
            "citation": "Synthetic rejection reference",
            "reason": "Synthetic projection input rejection.",
        },
    )
    assert rejected.status_code == 200
    before = database_snapshot(engine)
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 200
    assert response.json()["rejection_state"] == "REJECTED"
    assert response.json()["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("component", ["tax", "fee", "rejection", "projection"])
async def test_unavailable_input_histories_fail_closed_without_private_details(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], component: str
) -> None:
    module, exception = {
        "tax": (tax, tax.TaxSettingError),
        "fee": (fees, fees.FeeSettingError),
        "rejection": (rejections, rejections.RejectionError),
        "projection": (projections, projections.ProjectionHistoryError),
    }[component]
    before = database_snapshot(engine)
    with patch.object(module, "history", side_effect=exception("synthetic-private")):
        response = await client.get(path(book), params=QUERY)
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_PROJECTION_INPUTS_UNAVAILABLE"
    )
    assert (
        response.headers["cache-control"] == "no-store" and "synthetic-private" not in response.text
    )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("component", ["tax", "fee", "rejection", "projection"])
async def test_missing_required_history_after_owned_scope_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], component: str
) -> None:
    module = {"tax": tax, "fee": fees, "rejection": rejections, "projection": projections}[
        component
    ]
    before = database_snapshot(engine)
    with patch.object(module, "history", return_value=None):
        response = await client.get(path(book), params=QUERY)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_PROJECTION_INPUTS_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["event_terms", "portfolio_revision"])
async def test_inconsistent_snapshot_scope_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    with unit_of_work(engine) as con:
        row = owned_account_scope(con, book["owner"], book["account_id"])
        assert row is not None
        nonexistent_portfolio = {**dict(row), "portfolio_id": str(uuid4())}
    incomplete = SimpleNamespace(
        record=SimpleNamespace(terms=SimpleNamespace(ex_date=None, currency="USD"))
    )
    target, method, result = (
        (events, "get", incomplete)
        if failure == "event_terms"
        else (routes, "account", nonexistent_portfolio)
    )
    before = database_snapshot(engine)
    with patch.object(target, method, return_value=result):
        response = await client.get(path(book), params=QUERY)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_PROJECTION_INPUTS_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_input_histories_share_one_snapshot_when_a_setting_commits_mid_read(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    original = tax.history
    results: list[Any] = []

    def append_fee() -> Any:
        with unit_of_work(engine, write=True) as con:
            return fee_writer.save(con, book["owner"], fee_intent(book), request_id=uuid4())

    def read_then_write(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            results.append(pool.submit(append_fee).result(timeout=10))
        return result

    with patch.object(tax, "history", side_effect=read_then_write):
        response = await client.get(path(book), params=QUERY)
    assert response.status_code == 200, response.text
    old = response.json()
    assert old["tax"]["status"] == old["fee"]["status"] == "MISSING"
    latest = (await client.get(path(book), params=QUERY)).json()
    assert latest["tax"]["status"] == "MISSING"
    assert latest["fee"]["selected_id"] == str(results[0].id)
    assert int(latest["portfolio_data_revision"]) > int(old["portfolio_data_revision"])


async def test_future_head_does_not_become_a_selected_setting_or_zero(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    tax_setting, fee_setting = settings(engine, book)
    before = database_snapshot(engine)
    query = {**QUERY, "tax_effective_on": "2025-12-31", "fee_effective_on": "2025-12-31"}
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path(book), params=query)
    assert response.status_code == 200
    for key, setting in (("tax", tax_setting), ("fee", fee_setting)):
        value = response.json()[key]
        assert value["head_no"] == "1" and value["head_id"] == str(setting.id)
        assert value["status"] == "MISSING" and value["selected_id"] is None
        assert value["selected_scope"] == "NO_SELECTED_SETTING"
    assert database_snapshot(engine) == before


async def test_selection_preserves_current_head_and_earlier_selected_setting(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first_tax, first_fee = settings(engine, book)
    with unit_of_work(engine, write=True) as con:
        later_tax = tax.save(
            con,
            book["owner"],
            tax_intent(book, expected_setting_no=1, effective_from="2026-02-01", rate=None),
            request_id=uuid4(),
        )
    with unit_of_work(engine, write=True) as con:
        later_fee = fee_writer.save(
            con,
            book["owner"],
            fee_intent(
                book,
                expected_setting_no=1,
                expected_head_id=first_fee.id,
                effective_from="2026-02-01",
                fee_status="UNKNOWN",
                fee=None,
            ),
            request_id=uuid4(),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 200
    for key, first, later in (("tax", first_tax, later_tax), ("fee", first_fee, later_fee)):
        selected = response.json()[key]
        assert selected["head_no"] == "2" and selected["head_id"] == str(later.id)
        assert selected["selected_id"] == str(first.id) and selected["status"] == "ESTIMATED"
    newer = await client.get(
        path(book),
        params={**QUERY, "tax_effective_on": "2026-02-01", "fee_effective_on": "2026-02-01"},
    )
    assert newer.status_code == 200
    assert newer.json()["tax"]["selected_id"] == str(later_tax.id)
    assert newer.json()["fee"]["selected_id"] == str(later_fee.id)
    assert newer.json()["tax"]["status"] == newer.json()["fee"]["status"] == "UNKNOWN"
    assert database_snapshot(engine) == before


async def test_projection_head_remains_in_snapshot_when_another_writer_appends(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    value = intent(engine, book)
    original = projections.history
    appended: list[Any] = []

    def read_then_append(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            appended.append(pool.submit(save, engine, book, value, uuid4()).result(timeout=10))
        return result

    with patch.object(projections, "history", side_effect=read_then_append):
        previous = await client.get(path(book), params=QUERY)
    assert previous.status_code == 200
    assert previous.json()["projection_head_no"] == "0"
    assert previous.json()["projection_head_id"] is None
    current = await client.get(path(book), params=QUERY)
    assert current.status_code == 200
    assert current.json()["projection_head_no"] == "1"
    assert current.json()["projection_head_id"] == str(appended[0].version.id)


async def test_duplicate_projection_audit_blocks_snapshot_without_repair(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    projected = save(engine, book, intent(engine, book), uuid4())
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_projection",
            str(projected.version.id),
            "DIVIDEND_PROJECTION_CREATED",
            after={"synthetic": "duplicate"},
        )
    before = database_snapshot(engine)
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_PROJECTION_INPUTS_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_input_scopes_require_owner_session_and_never_expose_foreign_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event, account = book["event"], book["account_id"]
    before = database_snapshot(engine)
    for url in (
        f"/api/v1/dividends/events/{uuid4()}/accounts/{account}/projection-inputs",
        f"/api/v1/dividends/events/{event.event_id}/accounts/{uuid4()}/projection-inputs",
    ):
        response = await client.get(url, params=QUERY)
        assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
        assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    foreign = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(foreign),),
        )
        token, csrf, _ = issue_session(con, str(foreign), "synthetic-foreign-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    assert response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    response = await client.get(path(book), params=QUERY)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("as_of", "2026-02-30"),
        ("tax_effective_on", "20260108"),
        ("fee_effective_on", "2026-13-01"),
        ("eligibility_rule", "ASSUMED"),
    ],
)
async def test_invalid_input_scope_never_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: str
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book), params={**QUERY, field: value})
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_large_mocked_account_versions_are_exact_in_the_http_response(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine) as con:
        row = owned_account_scope(con, book["owner"], book["account_id"])
        assert row is not None
        mocked = {
            **dict(row),
            "account_version": 9007199254740993,
            "broker_version": 9223372036854775807,
        }
    before = database_snapshot(engine)
    with patch.object(routes, "account", return_value=mocked):
        response = await client.get(path(book), params=QUERY)
    assert response.status_code == 200
    assert response.json()["account_version"] == "9007199254740993"
    assert response.json()["broker_version"] == "9223372036854775807"
    assert database_snapshot(engine) == before


async def test_input_models_reject_inconsistent_pins_and_preserve_large_exact_versions(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    data = (await client.get(path(book), params=QUERY)).json()
    exact = {
        **data,
        "account_version": "9007199254740993",
        "portfolio_data_revision": "9223372036854775807",
    }
    result = models.ProjectionInputs.model_validate(exact).model_dump(mode="json")
    assert result["account_version"] == exact["account_version"]
    assert result["portfolio_data_revision"] == exact["portfolio_data_revision"]
    for change in (
        {"account_version": 1},
        {"broker_version": "01"},
        {"portfolio_data_revision": "9223372036854775808"},
        {"projection_head_no": "1"},
        {"scope": "GRANTED"},
        {"current_use_scope": "GRANTED"},
        {"source_scope": "VERIFIED"},
        {"eligibility_scope": "ESTABLISHED"},
    ):
        with pytest.raises(ValidationError):
            models.ProjectionInputs.model_validate({**data, **change})
    for change in (
        {"head_no": "1"},
        {"head_id": str(uuid4())},
        {"selected_id": str(uuid4())},
        {"status": "UNKNOWN"},
        {"selected_scope": "ACCOUNT_SCOPE_MATCH"},
        {"scope": "VERIFIED"},
    ):
        with pytest.raises(ValidationError):
            models.ProjectionInputSelection.model_validate({**data["tax"], **change})


async def test_input_openapi_requires_explicit_pins_and_no_grant_labels(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    schema = (await client.get("/openapi.json")).json()
    template = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projection-inputs"
    operation = schema["paths"][template]["get"]
    assert operation["security"] == [{"cookieAuth": []}]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert all(parameters[name]["required"] for name in QUERY)
    record = schema["components"]["schemas"]["ProjectionInputs"]
    for field in (
        "account_version",
        "broker_version",
        "portfolio_data_revision",
        "projection_head_no",
    ):
        assert field in record["required"] and record["properties"][field]["type"] == "string"
    for field in ("scope", "current_use_scope", "source_scope", "eligibility_scope"):
        assert field in record["required"]
