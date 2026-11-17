"""Protected projection history is owner-scoped, exact and never an income grant."""

import base64
import json
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
import spm_api.dividend_projection_history as history
import spm_api.dividend_projection_models as models
import spm_api.dividend_projection_routes as routes
import tests.test_dividend_projection_history as legacy
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.fixture
async def book(client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]) -> dict[str, Any]:
    return await prepare(client, engine, graph)


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/projections"
    )


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "synthetic-private" not in response.text


async def test_missing_owned_projection_history_is_not_zero_or_a_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    assert response.json() == {"items": [], "next_cursor": None}
    assert database_snapshot(engine) == before


async def test_recorded_projection_history_is_exact_paged_and_not_current_authority(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    tax, fee = settings(engine, book)
    first_intent = intent(engine, book, tax, fee, as_of="2026-01-07")
    first = save(engine, book, first_intent, uuid4())
    second_intent = intent(
        engine,
        book,
        tax,
        fee,
        as_of="2026-01-08",
        expected_projection_no=1,
        expected_projection_id=first.version.id,
    )
    second = save(engine, book, second_intent, uuid4())
    before = database_snapshot(engine)
    response = await client.get(path(book), params={"limit": 1})
    assert response.status_code == 200, response.text
    safe(response)
    page = response.json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    latest = page["items"][0]
    assert latest["id"] == str(second.version.id) and latest["projection_no"] == "2"
    assert latest["previous_id"] == str(first.version.id)
    assert latest["event_id"] == str(book["event"].event_id)
    assert latest["revision_id"] == str(book["event"].revision_id) and latest["revision_no"] == 1
    assert latest["account_id"] == str(book["account_id"])
    assert latest["account_version"] == str(book["account_version"])
    assert latest["broker_version"] == str(book["broker_version"])
    assert latest["portfolio_data_revision"] == str(second.portfolio_data_revision)
    assert latest["state"] == "EXPECTED" and latest["as_of"] == latest["ex_date"] == "2026-01-08"
    assert latest["timing_scope"] == "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY"
    assert latest["recorded_shares"] == "10.00000000"
    assert latest["estimated_amounts"] == {
        "currency": "USD",
        "gross_local": "10.000000",
        "tax_local": "2.000000",
        "fee_local": "0.500000",
        "net_local": "7.500000",
        "rounding_delta_local": "0.000000",
        "estimated_hkd": "58.500000",
        "quality": "OK",
    }
    assert latest["tax_provenance"] == {
        "effective_on": "2026-01-08",
        "selected_setting_id": str(tax.id),
        "observed_head_no": "1",
        "observed_head_id": str(tax.id),
        "status": "ESTIMATED",
        "scope": "OWNER_ESTIMATE_ONLY",
    }
    assert latest["fee_provenance"] == {
        "effective_on": "2026-01-08",
        "selected_setting_id": str(fee.id),
        "observed_head_no": "1",
        "observed_head_id": str(fee.id),
        "status": "ESTIMATED",
        "scope": "OWNER_ESTIMATE_ONLY",
    }
    assert latest["history_completeness"] == "UNVERIFIED"
    assert latest["scope"] == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    assert (
        latest["current_use_scope"] == "REVALIDATION_REQUIRED"
        and latest["eligibility_scope"] == "NOT_ESTABLISHED"
    )
    older = await client.get(path(book), params={"limit": 1, "cursor": page["next_cursor"]})
    assert older.status_code == 200
    safe(older)
    assert older.json()["next_cursor"] is None and older.json()["items"][0]["id"] == str(
        first.version.id
    )
    assert older.json()["items"][0]["timing_scope"] == "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY"
    assert database_snapshot(engine) == before


async def test_missing_estimates_and_no_computation_do_not_turn_into_zero(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    record = save(engine, book, intent(engine, book), uuid4())
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == str(record.version.id) and item["state"] == "NEEDS_REVIEW"
    assert item["recorded_shares"] is None and item["estimated_amounts"] is None
    assert item["tax_provenance"]["status"] == item["fee_provenance"]["status"] == "MISSING"
    assert (
        item["tax_provenance"]["observed_head_no"]
        == item["fee_provenance"]["observed_head_no"]
        == "0"
    )
    assert item["tax_rate"] is None and item["fee_original"] is None and item["fx_rate_hkd"] is None
    assert database_snapshot(engine) == before


async def test_unknown_estimates_keep_their_explicit_provenance(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    tax, fee = settings(engine, book, rate=None, fee=None, fee_currency="USD")
    saved = save(engine, book, intent(engine, book, tax, fee), uuid4())
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    record = response.json()["items"][0]
    assert record["id"] == str(saved.version.id) and record["state"] == "NEEDS_REVIEW"
    assert record["tax_provenance"]["status"] == record["fee_provenance"]["status"] == "UNKNOWN"
    assert record["tax_provenance"]["selected_setting_id"] == str(tax.id)
    assert record["fee_provenance"]["selected_setting_id"] == str(fee.id)
    assert record["estimated_amounts"] is None and record["fee_original"] is None
    assert database_snapshot(engine) == before


async def test_missing_fx_remains_unknown_and_a_computational_quality_is_not_a_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    tax, fee = settings(engine, book)
    save(engine, book, intent(engine, book, tax, fee, fx_rate_hkd=None, fx_source=None), uuid4())
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path(book))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["estimated_amounts"]["quality"] == "MISSING_FX"
    assert item["estimated_amounts"]["estimated_hkd"] is None and item["fx_rate_hkd"] is None
    assert item["eligibility_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_recorded_no_position_never_becomes_a_confirmed_zero_entitlement(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph, holdings=False)
    save(engine, book, intent(engine, book), uuid4())
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["state"] == "NO_POSITION" and item["recorded_shares"] is None
    assert item["estimated_amounts"] is None and item["history_completeness"] == "UNVERIFIED"
    assert item["eligibility_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_legacy_projection_keeps_absent_provenance_without_backfill(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with patch.object(legacy, "OWNER", book["owner"]), unit_of_work(engine, write=True) as con:
        old = legacy.append(con, book["event"], str(book["account_id"]))
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["id"] == old["id"] and item["tax_provenance"] is None
    assert item["fee_provenance"] is None and item["scope"] == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    assert item["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


async def test_unrelated_search_cannot_hide_a_corrupt_later_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, book, intent(engine, book), uuid4())
    second = save(
        engine,
        book,
        intent(engine, book, expected_projection_no=1, expected_projection_id=first.version.id),
        uuid4(),
    )
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(book["owner"]),
            "dividend_projection",
            str(second.version.id),
            "DIVIDEND_PROJECTION_CREATED",
            after={"synthetic": "duplicate"},
        )
    before = database_snapshot(engine)
    response = await client.get(path(book), params={"q": str(first.version.id), "limit": 1})
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    )
    safe(response)
    assert database_snapshot(engine) == before


async def test_projection_page_uses_one_wal_snapshot_during_a_concurrent_append(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = save(engine, book, intent(engine, book), uuid4())
    next_intent = intent(
        engine, book, expected_projection_no=1, expected_projection_id=first.version.id
    )
    original = history.history
    appended: list[Any] = []

    def read_then_append(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            appended.append(
                pool.submit(save, engine, book, next_intent, uuid4()).result(timeout=10)
            )
        return result

    with patch.object(history, "history", side_effect=read_then_append):
        response = await client.get(path(book))
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(first.version.id)]
    latest = await client.get(path(book))
    assert latest.status_code == 200
    assert [item["id"] for item in latest.json()["items"]] == [
        str(appended[0].version.id),
        str(first.version.id),
    ]


async def test_exact_int64_versions_are_strings_and_malformed_model_values_fail(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    save(engine, book, intent(engine, book), uuid4())
    response = await client.get(path(book))
    item = response.json()["items"][0]
    item.update(
        account_version="9007199254740993",
        broker_version="9223372036854775807",
        portfolio_data_revision="9007199254740993",
    )
    parsed = models.ProjectionRecord.model_validate(item).model_dump(mode="json")
    assert parsed["account_version"] == parsed["portfolio_data_revision"] == "9007199254740993"
    assert parsed["broker_version"] == "9223372036854775807"
    for field, value in (
        ("account_version", 1),
        ("broker_version", "01"),
        ("projection_no", "9223372036854775808"),
        ("portfolio_data_revision", "-1"),
    ):
        with pytest.raises(ValidationError):
            models.ProjectionRecord.model_validate({**item, field: value})


async def test_response_models_reject_inconsistent_scope_provenance_and_values(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    tax, fee = settings(engine, book)
    save(engine, book, intent(engine, book, tax, fee), uuid4())
    item = (await client.get(path(book))).json()["items"][0]
    for changed in (
        {"previous_id": item["id"]},
        {"timing_scope": "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY"},
        {"state": "NO_POSITION"},
        {"estimated_amounts": None},
        {"estimated_amounts": {**item["estimated_amounts"], "currency": "HKD"}},
        {"recorded_shares": None},
        {"tax_rate": None},
        {"fee_original": None},
        {"fee_rate_source": None},
        {"fx_source": None},
        {"scope": "GRANTED"},
        {"current_use_scope": "GRANTED"},
        {"eligibility_scope": "ESTABLISHED"},
    ):
        with pytest.raises(ValidationError):
            models.ProjectionRecord.model_validate({**item, **changed})
    for changed in (
        {"observed_head_no": "0"},
        {"observed_head_id": None},
        {"selected_setting_id": None},
        {"status": "MISSING"},
    ):
        with pytest.raises(ValidationError):
            models.ProjectionProvenance.model_validate({**item["tax_provenance"], **changed})


async def test_projection_conversion_and_event_failures_do_not_disclose_or_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    record = save(engine, book, intent(engine, book), uuid4())
    before = database_snapshot(engine)
    with patch.object(routes, "_public", side_effect=ValueError("synthetic-private")):
        response = await client.get(path(book))
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    )
    safe(response)
    with unit_of_work(engine) as con:
        for value in (
            None,
            SimpleNamespace(record=SimpleNamespace(revision_id=uuid4())),
            SimpleNamespace(
                record=SimpleNamespace(
                    revision_id=record.version.revision_id,
                    terms=SimpleNamespace(ex_date=None, currency="USD"),
                )
            ),
        ):
            with patch.object(events, "get", return_value=value), pytest.raises(ValueError):
                routes._public(con, book["owner"], record)
    assert database_snapshot(engine) == before


async def test_executable_contract_marks_exact_versions_and_boundaries_as_required(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    schema = (await client.get("/openapi.json")).json()
    template = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections"
    assert set(schema["paths"][template]) == {"get", "post"}
    assert schema["paths"][template]["get"]["security"] == [{"cookieAuth": []}]
    projection = schema["components"]["schemas"]["ProjectionRecord"]
    for field in ("account_version", "broker_version", "portfolio_data_revision", "projection_no"):
        assert (
            projection["properties"][field]["type"] == "string" and field in projection["required"]
        )
    for field in (
        "scope",
        "current_use_scope",
        "eligibility_scope",
        "history_completeness",
        "timing_scope",
    ):
        assert field in projection["required"]
    provenance = schema["components"]["schemas"]["ProjectionProvenance"]
    assert provenance["properties"]["observed_head_no"]["type"] == "string"
    assert "scope" in provenance["required"]


@pytest.mark.parametrize("value", ["0", "01", "-1", "1e3", "9223372036854775808", "9" * 100])
async def test_noncanonical_or_out_of_range_projection_cursors_fail_safely(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], value: str
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    response = await client.get(path(book), params={"cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    safe(response)
    assert database_snapshot(engine) == before


async def test_missing_and_foreign_scopes_are_not_exposed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    event, account = book["event"], book["account_id"]
    for url in (
        f"/api/v1/dividends/events/{uuid4()}/accounts/{account}/projections",
        f"/api/v1/dividends/events/{event.event_id}/accounts/{uuid4()}/projections",
    ):
        response = await client.get(url)
        assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
        safe(response)
    assert database_snapshot(engine) == before
    other = uuid4()
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(other),),
        )
        token, csrf, _ = issue_session(con, str(other), "synthetic-foreign-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    safe(response)
    assert database_snapshot(engine) == before
    client.cookies.clear()
    response = await client.get(path(book))
    assert response.status_code == 401
    safe(response)
    assert database_snapshot(engine) == before


async def test_projection_search_is_literal_and_cannot_cross_an_owned_account(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    first = save(engine, book, intent(engine, book), uuid4())
    before = database_snapshot(engine)
    literal = await client.get(path(book), params={"q": "%_\\\\"})
    assert literal.status_code == 200 and literal.json()["items"] == []
    match = await client.get(path(book), params={"q": str(first.version.id)})
    assert match.status_code == 200 and [row["id"] for row in match.json()["items"]] == [
        str(first.version.id)
    ]
    other = await client.get(
        f"/api/v1/dividends/events/{book['event'].event_id}/accounts/{graph['target']['id']}/projections"
    )
    assert other.status_code == 200 and other.json()["items"] == []
    assert database_snapshot(engine) == before


async def test_unavailable_history_is_private_and_never_repaired(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with patch.object(
        history, "history", side_effect=history.ProjectionHistoryError("synthetic-private")
    ):
        response = await client.get(path(book))
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    )
    safe(response)
    assert database_snapshot(engine) == before
