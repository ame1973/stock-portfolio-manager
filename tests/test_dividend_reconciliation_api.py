"""Protected recorded reconciliation is private history, not a payment grant."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_projection_history as projections
import spm_api.dividend_reconciliation_history as history
import spm_api.dividend_reconciliation_models as models
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_projection_writer import intent, save, settings
from tests.test_dividend_receipt_void_inputs_api import create as create_recorded_receipt
from tests.test_dividend_receipt_void_write_api import body as void_body
from tests.test_dividend_receipt_void_write_api import inputs as void_inputs
from tests.test_dividend_receipt_void_write_api import path as void_path
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import projection, read, receipt
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/reconciliation"
    )


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "owner_id" not in response.text and "audit_id" not in response.text
    assert "synthetic-private" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_cross_revision_comparison_is_exact_private_and_read_only(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    latest = projection(engine, book)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    result = response.json()
    assert result == {
        "event_id": str(book["event"].event_id),
        "revision_id": str(book["second"].revision_id),
        "revision_no": 2,
        "account_id": str(book["account_id"]),
        "projection_id": str(latest.version.id),
        "projection_no": "1",
        "projection_as_of": "2026-01-08",
        "projection_state": "EXPECTED",
        "projection_quality": "OK",
        "comparison_quality": "OK",
        "currency": "USD",
        "recorded_expected": "11.000000",
        "recorded_received": "6.000000",
        "recorded_outstanding": "5.000000",
        "active_receipts": "1",
        "void_receipts": "0",
        "unverified_origins": "1",
        "unavailable_cash_links": "0",
        "state": "RECORDED_PARTIAL",
        "scope": "UNVERIFIED_RECORDED_RECONCILIATION_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
    }
    assert database_snapshot(engine) == before


async def test_missing_projection_and_empty_receipt_are_not_confirmed_zero(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    missing = await client.get(path(book))
    assert missing.status_code == 200
    safe(missing)
    value = missing.json()
    assert value["state"] == "NO_RECORDED_PROJECTION"
    assert value["projection_id"] is value["projection_no"] is value["projection_as_of"] is None
    assert value["recorded_expected"] is value["recorded_received"] is None
    assert value["recorded_outstanding"] is None and value["active_receipts"] == "0"
    assert database_snapshot(engine) == before
    projection(engine, book)
    before = database_snapshot(engine)
    empty = await client.get(path(book))
    assert empty.status_code == 200
    safe(empty)
    value = empty.json()
    assert value["state"] == "NO_RECORDED_ACTIVE_RECEIPTS"
    assert value["recorded_expected"] == "11.000000" and value["recorded_received"] is None
    assert value["recorded_outstanding"] is None and value["comparison_quality"] is None
    assert value["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert database_snapshot(engine) == before


async def test_matched_origin_and_missing_fx_remain_unverified_without_upstream_access(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    await create_recorded_receipt(client, book)
    tax, fee = settings(engine, book, rate="0", fee="0")
    save(engine, book, intent(engine, book, tax, fee, fx_rate_hkd=None, fx_source=None), uuid4())
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    value = response.json()
    assert value["state"] == "RECORDED_PARTIAL" and value["unverified_origins"] == "0"
    assert value["projection_quality"] == value["comparison_quality"] == "MISSING_FX"
    assert value["recorded_received"] == "6.000000" and value["recorded_outstanding"] == "5.000000"
    assert value["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert value["eligibility_scope"] == "NOT_ESTABLISHED"
    assert value["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert database_snapshot(engine) == before


async def test_voided_receipt_leaves_comparison_without_deleting_original_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create_recorded_receipt(client, book)
    projection(engine, book)
    before_void = await client.get(path(book))
    assert before_void.status_code == 200 and before_void.json()["recorded_received"] == "6.000000"
    observed = await void_inputs(client, book, created["id"])
    response = await client.post(
        void_path(created["id"]), json=void_body(observed), headers=version_headers(client, 1)
    )
    assert response.status_code == 200, response.text
    before = database_snapshot(engine)
    result = await client.get(path(book))
    assert result.status_code == 200
    safe(result)
    value = result.json()
    assert value["state"] == "NO_RECORDED_ACTIVE_RECEIPTS"
    assert value["active_receipts"] == "0" and value["void_receipts"] == "1"
    assert value["recorded_received"] is value["recorded_outstanding"] is None
    assert value["unverified_origins"] == value["unavailable_cash_links"] == "0"
    assert database_snapshot(engine) == before


async def test_other_owned_account_does_not_receive_this_accounts_amounts(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], graph: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    before = database_snapshot(engine)
    other = {**book, "account_id": graph["target"]["id"]}
    response = await client.get(path(other))
    assert response.status_code == 200
    safe(response)
    value = response.json()
    assert value["account_id"] == graph["target"]["id"]
    assert value["state"] == "NO_RECORDED_PROJECTION" and value["active_receipts"] == "0"
    assert value["recorded_received"] is value["recorded_outstanding"] is None
    assert database_snapshot(engine) == before


async def test_currency_mismatch_and_missing_cash_are_never_reported_as_received(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, currency="HKD")
    projection(engine, book)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    value = response.json()
    assert value["state"] == "CURRENCY_REVIEW" and value["comparison_quality"] == "NEEDS_REVIEW"
    assert value["recorded_received"] is value["recorded_outstanding"] is None
    assert database_snapshot(engine) == before
    receipt(engine, book, cash=False)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    value = response.json()
    assert value["state"] == "CASH_LINK_UNAVAILABLE" and value["unavailable_cash_links"] == "1"
    assert value["recorded_received"] is value["recorded_outstanding"] is None
    assert database_snapshot(engine) == before


async def test_int64_money_is_serialized_as_exact_decimal_strings(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=INT64_MAX)
    projection(engine, book)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    value = response.json()
    assert value["recorded_received"] == "9223372036854.775807"
    assert value["recorded_outstanding"] == "-9223372036843.775807"
    assert value["state"] == "RECORDED_OVER_RECEIVED"
    assert value["comparison_quality"] == "NEEDS_REVIEW"
    assert database_snapshot(engine) == before


async def test_overflow_returns_fixed_private_error_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=INT64_MAX)
    receipt(engine, book, amount=1)
    projection(engine, book)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "DIVIDEND_RECONCILIATION_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_foreign_missing_and_unauthenticated_scopes_are_private(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    base = "/api/v1/dividends/events"
    bad = (
        f"{base}/{uuid4()}/accounts/{book['account_id']}/reconciliation",
        f"{base}/{book['event'].event_id}/accounts/{uuid4()}/reconciliation",
    )
    before = database_snapshot(engine)
    for url in bad:
        response = await client.get(url)
        assert response.status_code == 404
        safe(response)
    assert database_snapshot(engine) == before
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


async def test_read_errors_and_substituted_scope_fail_closed_without_private_values(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    retained = read(engine, book)
    assert retained is not None
    before = database_snapshot(engine)
    for supplied in (
        history.ReconciliationHistoryError("synthetic-private"),
        object(),
        replace(retained, event_id=uuid4()),
        replace(retained, account_id=uuid4()),
        replace(retained, recorded_received=Decimal("7")),
        replace(retained, recorded_received=cast(Any, 1.0)),
        replace(retained, active_receipts=cast(Any, True)),
        replace(retained, projection_as_of=cast(Any, "2026-01-08")),
        replace(retained, projection_no=INT64_MAX + 1),
    ):
        with patch.object(
            history,
            "read",
            **(
                {"side_effect": supplied}
                if isinstance(supplied, Exception)
                else {"return_value": supplied}
            ),
        ):
            response = await client.get(path(book))
        assert response.status_code == 503
        safe(response)
        assert response.json()["code"] == "DIVIDEND_RECONCILIATION_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_api_read_uses_one_wal_snapshot_during_concurrent_append(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    receipt(engine, book)
    projection(engine, book)
    original = projections.history

    def concurrent(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(receipt, engine, book, amount=2_000_000, older=False).result()
        return result

    with patch.object(projections, "history", side_effect=concurrent):
        first = await client.get(path(book))
    before = database_snapshot(engine)
    second = await client.get(path(book))
    assert first.status_code == second.status_code == 200
    safe(first)
    safe(second)
    assert first.json()["recorded_received"] == "6.000000"
    assert second.json()["recorded_received"] == "8.000000"
    assert first.json()["active_receipts"] == "1" and second.json()["active_receipts"] == "2"
    assert database_snapshot(engine) == before


def sample() -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "revision_no": 2,
        "account_id": str(uuid4()),
        "projection_id": str(uuid4()),
        "projection_no": "1",
        "projection_as_of": "2026-01-08",
        "projection_state": "EXPECTED",
        "projection_quality": "OK",
        "comparison_quality": "OK",
        "currency": "USD",
        "recorded_expected": "11.000000",
        "recorded_received": "6.000000",
        "recorded_outstanding": "5.000000",
        "active_receipts": "1",
        "void_receipts": "0",
        "unverified_origins": "1",
        "unavailable_cash_links": "0",
        "state": "RECORDED_PARTIAL",
        "scope": "UNVERIFIED_RECORDED_RECONCILIATION_ONLY",
        "history_completeness": "UNVERIFIED",
        "current_use_scope": "REVALIDATION_REQUIRED",
        "eligibility_scope": "NOT_ESTABLISHED",
        "payment_scope": "OWNER_RECORDED_NOT_VERIFIED",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"projection_id": None},
        {"projection_no": None},
        {"projection_no": "0"},
        {"projection_no": "01"},
        {"projection_no": str(INT64_MAX + 1)},
        {"projection_as_of": None},
        {"projection_state": None},
        {"projection_quality": None},
        {"projection_state": "NEEDS_REVIEW"},
        {"state": "NO_RECORDED_PROJECTION"},
        {"state": "STALE_RECORDED_PROJECTION"},
        {"state": "PROJECTION_NOT_COMPARABLE"},
        {"unverified_origins": "2"},
        {"unavailable_cash_links": "2"},
        {"currency": None},
        {"recorded_expected": None},
        {"recorded_received": None},
        {"recorded_outstanding": None},
        {"active_receipts": "0", "unverified_origins": "0"},
        {"unavailable_cash_links": "1"},
        {"comparison_quality": None},
        {"comparison_quality": "MISSING_FX"},
        {"recorded_received": "6.000001"},
        {"recorded_received": "6.0000001"},
        {"recorded_received": 6.0},
        {"recorded_received": "9223372036854.775808"},
        {"recorded_expected": "-1", "recorded_received": "0", "recorded_outstanding": "-1"},
        {"recorded_received": "-1", "recorded_outstanding": "12"},
        {"recorded_received": "11", "recorded_outstanding": "0"},
        {"state": "RECORDED_RECEIVED"},
        {"state": "RECORDED_OVER_RECEIVED"},
        {"state": "RECORDED_NEEDS_REVIEW"},
        {"state": "CURRENCY_REVIEW"},
        {"state": "CASH_LINK_UNAVAILABLE"},
        {"state": "NO_RECORDED_ACTIVE_RECEIPTS"},
        {"scope": "CONFIRMED"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "VERIFIED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"payment_scope": "VERIFIED"},
        {"extra_private_data": "synthetic-private"},
    ],
)
def test_model_rejects_inconsistent_or_unsafe_outputs(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        models.RecordedReconciliation.model_validate({**sample(), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {
            "state": "NO_RECORDED_PROJECTION",
            "projection_id": None,
            "projection_no": None,
            "projection_as_of": None,
            "projection_state": None,
            "projection_quality": None,
            "comparison_quality": None,
            "currency": None,
            "recorded_expected": None,
            "recorded_received": None,
            "recorded_outstanding": None,
        },
        {
            "state": "STALE_RECORDED_PROJECTION",
            "comparison_quality": None,
            "currency": None,
            "recorded_expected": None,
            "recorded_received": None,
            "recorded_outstanding": None,
        },
        {
            "state": "PROJECTION_NOT_COMPARABLE",
            "projection_state": "NEEDS_REVIEW",
            "projection_quality": None,
            "comparison_quality": None,
            "currency": None,
            "recorded_expected": None,
            "recorded_received": None,
            "recorded_outstanding": None,
        },
        {
            "state": "CASH_LINK_UNAVAILABLE",
            "unavailable_cash_links": "1",
            "comparison_quality": None,
            "recorded_received": None,
            "recorded_outstanding": None,
        },
        {
            "state": "NO_RECORDED_ACTIVE_RECEIPTS",
            "active_receipts": "0",
            "unverified_origins": "0",
            "comparison_quality": None,
            "recorded_received": None,
            "recorded_outstanding": None,
        },
        {
            "state": "CURRENCY_REVIEW",
            "comparison_quality": "NEEDS_REVIEW",
            "recorded_received": None,
            "recorded_outstanding": None,
        },
        {
            "state": "RECORDED_RECEIVED",
            "recorded_expected": "0",
            "recorded_received": "0",
            "recorded_outstanding": "0",
        },
        {
            "state": "RECORDED_OVER_RECEIVED",
            "recorded_received": "12",
            "recorded_outstanding": "-1",
            "comparison_quality": "NEEDS_REVIEW",
        },
        {
            "state": "RECORDED_NEEDS_REVIEW",
            "recorded_received": "-1",
            "recorded_outstanding": "12",
            "comparison_quality": "NEEDS_REVIEW",
        },
    ],
)
def test_model_accepts_distinct_recorded_states(changes: dict[str, Any]) -> None:
    result = models.RecordedReconciliation.model_validate({**sample(), **changes})
    assert result.state == changes["state"]
    assert result.payment_scope == "OWNER_RECORDED_NOT_VERIFIED"
