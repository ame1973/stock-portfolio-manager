"""Combined receipt HTTP history must never omit either retained store."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_combined_receipt_routes as routes
import spm_api.dividend_event_service as events
import tests.test_dividend_direct_receipt_history as direct_fixture
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_direct_receipt_history import RecordedDirectReceipt
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import receipt
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/recorded-receipts"
    )


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "synthetic-private" not in response.text
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_combined_http_history_retains_distinct_basis_and_audit_labels(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    old = receipt(engine, book, older=True)
    new = direct_receipt(engine, book, received_micro=2_000_000, received_date="2026-01-10")
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    data = response.json()
    assert data["next_cursor"] is None
    assert [item["id"] for item in data["items"]] == [new["id"], old["id"]]
    direct, legacy = data["items"]
    assert direct["receipt_basis"] == "DIRECT_EVENT_ACCOUNT"
    assert direct["entitlement_id"] is None and direct["revision_no"] == 2
    assert direct["recorded_amount"] == "2.000000"
    assert direct["audit_scope"] == "RECORDED_AUDIT_MATCHED_ONLY"
    assert legacy["receipt_basis"] == "LEGACY_ENTITLEMENT"
    assert legacy["entitlement_id"] == book["older"]["id"] and legacy["revision_no"] == 1
    assert legacy["recorded_amount"] == "6.000000"
    assert legacy["audit_scope"] == "RECEIPT_AUDIT_NOT_VERIFIED"
    for item in data["items"]:
        assert item["event_id"] == str(book["event"].event_id)
        assert item["account_id"] == str(book["account_id"])
        assert item["current_revision_id"] == str(book["event"].revision_id)
        assert item["current_revision_no"] == 2
        assert item["scope"] == "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY"
        assert item["eligibility_scope"] == "NOT_ESTABLISHED"
        assert item["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
        assert item["history_completeness"] == "UNVERIFIED"
        assert item["current_use_scope"] == "REVALIDATION_REQUIRED"
        assert item["fx_rate_hkd"] is item["received_hkd"] is None
    assert database_snapshot(engine) == before


async def test_empty_history_is_private_and_does_not_confirm_zero(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    assert response.json()["items"] == [] and response.json()["next_cursor"] is None
    assert database_snapshot(engine) == before


async def test_cross_store_keyset_pages_do_not_duplicate_receipts(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    old = receipt(engine, book)
    new = direct_receipt(engine, book, received_date="2026-01-10")
    before = database_snapshot(engine)
    first = await client.get(path(book), params={"limit": 1})
    assert first.status_code == 200
    safe(first)
    page = first.json()
    assert [item["id"] for item in page["items"]] == [new["id"]]
    assert page["next_cursor"] is not None
    second = await client.get(path(book), params={"limit": 1, "cursor": page["next_cursor"]})
    assert second.status_code == 200
    safe(second)
    assert [item["id"] for item in second.json()["items"]] == [old["id"]]
    assert second.json()["next_cursor"] is None
    assert database_snapshot(engine) == before


async def test_unsealed_direct_record_fails_the_whole_read_even_when_filtered_out(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    old = receipt(engine, book)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(book["event"], str(book["account_id"])))
    before = database_snapshot(engine)
    response = await client.get(path(book), params={"q": old["id"], "limit": 1})
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "COMBINED_RECEIPT_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["event", "account"])
async def test_missing_scope_is_not_an_empty_owned_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    target = path(book).replace(
        str(book["event"].event_id if field == "event" else book["account_id"]), str(uuid4())
    )
    before = database_snapshot(engine)
    response = await client.get(target)
    assert response.status_code == 404
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["event_id", "owner_id"])
async def test_substituted_current_event_scope_cannot_label_another_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    direct_receipt(engine, book)
    with unit_of_work(engine) as con:
        current = events.get(con, book["owner"], book["event"].event_id)
        retained = combined.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert current is not None and retained
    forged = replace(current, record=current.record.model_copy(update={field: uuid4()}))
    before = database_snapshot(engine)
    with (
        patch.object(events, "get", return_value=forged),
        patch.object(combined, "history", return_value=retained),
    ):
        response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_private_scope_requires_the_owner_session(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    direct_receipt(engine, book)
    with unit_of_work(engine, write=True) as con:
        other = uuid4()
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
    assert response.status_code == 404
    safe(response)
    client.cookies.clear()
    response = await client.get(path(book))
    assert response.status_code == 401
    safe(response)
    assert database_snapshot(engine) == before


async def test_other_owned_account_never_receives_this_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    receipt(engine, book)
    direct_receipt(engine, book)
    before = database_snapshot(engine)
    response = await client.get(path({**book, "account_id": graph["target"]["id"]}))
    assert response.status_code == 200 and response.json()["items"] == []
    safe(response)
    assert database_snapshot(engine) == before


async def test_historical_direct_void_remains_visible_without_provider_access(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    record = direct_receipt(engine, book)
    with patch.object(direct_fixture, "OWNER", book["owner"]):
        direct_fixture.void_receipt(engine, record)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    value = response.json()["items"][0]
    assert value["status"] == "VOID" and value["version"] == "2"
    assert value["cash_scope"] == "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"
    assert value["receipt_basis"] == "DIRECT_EVENT_ACCOUNT" and value["entitlement_id"] is None
    assert database_snapshot(engine) == before


async def test_int64_amount_limits_survive_http_as_exact_decimal_strings(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    direct_receipt(engine, book, received_micro=INT64_MAX)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    assert response.json()["items"][0]["recorded_amount"] == "9223372036854.775807"
    assert database_snapshot(engine) == before


async def test_page_query_and_complete_validation_share_one_wal_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    old = receipt(engine, book)
    read = combined.history

    def append(*args: Any, **kwargs: Any) -> Any:
        result = read(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(direct_receipt, engine, book, received_date="2026-01-10").result(timeout=30)
        return result

    with patch.object(combined, "history", side_effect=append):
        first = await client.get(path(book))
    before = database_snapshot(engine)
    second = await client.get(path(book))
    assert first.status_code == second.status_code == 200
    safe(first)
    safe(second)
    assert [value["id"] for value in first.json()["items"]] == [old["id"]]
    assert len(second.json()["items"]) == 2
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["duplicate", "invalid", "revision", "amount", "date", "error"])
async def test_corrupt_retained_outputs_are_unavailable_without_disclosing_values(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    direct_receipt(engine, book)
    with unit_of_work(engine) as con:
        retained = combined.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert retained is not None and len(retained) == 1
    value = retained[0]
    assert isinstance(value, RecordedDirectReceipt)
    options: dict[str, Any]
    if problem == "duplicate":
        options = {"return_value": (*retained, *retained)}
    elif problem == "invalid":
        options = {"return_value": (object(),)}
    elif problem == "revision":
        options = {"return_value": (replace(value, revision_no=1),)}
    elif problem == "amount":
        options = {
            "return_value": (
                replace(value, receipt=replace(value.receipt, received=cast(Any, 2.0))),
            )
        }
    elif problem == "date":
        options = {
            "return_value": (
                replace(
                    value,
                    receipt=replace(
                        value.receipt, received_date=value.receipt.received_date.replace(day=11)
                    ),
                ),
            )
        }
    else:
        options = {"side_effect": combined.CombinedReceiptHistoryError("synthetic-private")}
    before = database_snapshot(engine)
    with patch.object(combined, "history", **options):
        response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "COMBINED_RECEIPT_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "101"}, {"cursor": "invalid"}])
async def test_invalid_pagination_is_private_and_read_only(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], params: dict[str, str]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book), params=params)
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


async def test_literal_search_does_not_expand_sql_wildcards(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    old = receipt(engine, book)
    direct_receipt(engine, book)
    before = database_snapshot(engine)
    selected = await client.get(path(book), params={"q": old["id"]})
    assert selected.status_code == 200
    assert [item["id"] for item in selected.json()["items"]] == [old["id"]]
    for query in ("%", "_", "\\"):
        response = await client.get(path(book), params={"q": query})
        assert response.status_code == 200 and response.json()["items"] == []
        safe(response)
    assert database_snapshot(engine) == before


def test_public_conversion_rejects_unknown_record_types() -> None:
    with pytest.raises(ValueError):
        routes._public(cast(Any, None), uuid4(), cast(Any, object()), {})


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), 2.0])
def test_nonfinite_or_binary_amounts_are_not_formatted_as_wire_money(bad: Any) -> None:
    with pytest.raises(ValueError):
        routes._amount(bad)
