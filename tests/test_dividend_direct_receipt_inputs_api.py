"""Direct receipt input reads expose recorded pins without inventing entitlement."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as receipts
import spm_api.dividend_direct_receipt_input_routes as routes
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_service as rejections
from spm_api.audit import now
from spm_api.db import unit_of_work
from spm_api.decimal_types import INT64_MAX
from spm_api.dividend_account_scope import account
from spm_api.dividend_direct_receipt_input_models import DirectReceiptInputs
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_combined_receipt_api import safe
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_event_writer import AT, next_intent
from tests.test_dividend_projection_writer import prepare
from tests.test_dividend_receipt_history import claim
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import receipt
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_rejection_writer import save as reject
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.fixture
async def book(client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]) -> dict[str, Any]:
    return await prepare(client, engine, graph, holdings=False)


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/recorded-receipts/inputs"
    )


async def test_direct_inputs_need_no_entitlement_and_do_not_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["event_id"] == str(book["event"].event_id)
    assert value["revision_id"] == str(book["event"].revision_id)
    assert value["revision_no"] == book["event"].revision_no
    assert value["account_id"] == str(book["account_id"])
    assert value["portfolio_id"] == str(book["portfolio_id"])
    assert value["broker_id"] == str(book["broker_id"])
    assert value["receipt_basis"] == "DIRECT_EVENT_ACCOUNT"
    assert value["account_status"] == "ACTIVE"
    assert value["rejection_state"] == "NOT_REJECTED" and value["rejection_id"] is None
    assert value["scope"] == "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY"
    assert value["history_completeness"] == "UNVERIFIED"
    assert value["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert value["eligibility_scope"] == "NOT_ESTABLISHED"
    assert value["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert "entitlements" not in value and "entitlement_id" not in value
    assert "currency" not in value and "received_amount" not in value
    for key in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "portfolio_data_revision",
    ):
        assert isinstance(value[key], str)
    assert database_snapshot(engine) == before


async def test_mixed_retained_receipts_are_validated_without_exposing_or_rewriting_them(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    older = claim(book["event"].revision_id, str(book["account_id"]), net=10_000_000)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", older)
    receipt(engine, {**book, "older": older})
    direct_receipt(engine, book)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    assert "entitlement_id" not in response.text and "recorded_amount" not in response.text
    assert response.json()["receipt_basis"] == "DIRECT_EVENT_ACCOUNT"
    assert database_snapshot(engine) == before


async def test_unsealed_direct_history_is_not_treated_as_a_usable_empty_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, receipt_values(book["event"], str(book["account_id"])))
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 503
    assert response.json()["code"] == "DIRECT_RECEIPT_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["account_id", "event"])
async def test_missing_scope_is_not_an_owned_input_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    identifier = book["account_id"] if field == "account_id" else book["event"].event_id
    before = database_snapshot(engine)
    response = await client.get(path(book).replace(str(identifier), str(uuid4())))
    assert response.status_code == 404
    safe(response)
    assert database_snapshot(engine) == before


async def test_foreign_owner_and_absent_session_cannot_read_pins(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
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


async def test_maximum_versions_and_archived_status_remain_readable_without_a_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET version=?,status='ARCHIVED' WHERE id=?",
            (INT64_MAX, str(book["account_id"])),
        )
        con.exec_driver_sql(
            "UPDATE portfolios SET version=?,data_revision=? WHERE id=?",
            (INT64_MAX, INT64_MAX, str(book["portfolio_id"])),
        )
        con.exec_driver_sql(
            "UPDATE brokers SET version=? WHERE id=?", (INT64_MAX, str(book["broker_id"]))
        )
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    value = response.json()
    assert value["account_status"] == "ARCHIVED"
    for key in (
        "account_version",
        "portfolio_version",
        "broker_version",
        "portfolio_data_revision",
    ):
        assert value[key] == str(INT64_MAX)
    assert database_snapshot(engine) == before


async def test_current_rejection_and_later_resolved_withdrawal_stay_explicit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    rejected = reject(engine, rejection_intent(book["event"]), owner=book["owner"])
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    assert response.json()["rejection_id"] == str(rejected.id)
    assert response.json()["rejection_state"] == "REJECTED"
    assert database_snapshot(engine) == before
    successor = next_intent(book["event"], "WITHDRAW", resolve_rejection_id=rejected.id)
    with unit_of_work(engine, write=True) as con:
        event_writer.save(
            con,
            book["owner"],
            successor,
            at=now(),
            request_id=uuid4(),
        )
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    assert response.json()["workflow_status"] == "WITHDRAWN"
    assert response.json()["revision_id"] == str(successor.revision_id)
    assert response.json()["rejection_state"] == "NOT_REJECTED"
    assert response.json()["rejection_id"] is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["account", "rejection", "event"])
async def test_event_account_and_rejection_share_one_wal_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    read = events.get
    appended = False

    def write() -> None:
        if change == "rejection":
            reject(engine, rejection_intent(book["event"]), owner=book["owner"])
        elif change == "event":
            successor = next_intent(book["event"], "VERIFY")
            with unit_of_work(engine, write=True) as con:
                event_writer.save(
                    con, book["owner"], successor, at=AT + timedelta(seconds=1), request_id=uuid4()
                )
        else:
            with unit_of_work(engine, write=True) as con:
                con.exec_driver_sql(
                    "UPDATE accounts SET version=version+1,status='ARCHIVED' WHERE id=?",
                    (str(book["account_id"]),),
                )
                con.exec_driver_sql(
                    "UPDATE portfolios SET version=version+1,data_revision=data_revision+1 "
                    "WHERE id=?",
                    (str(book["portfolio_id"]),),
                )

    def read_then_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal appended
        result = read(*args, **kwargs)
        if not appended:
            appended = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write).result(timeout=30)
        return result

    previous = (await client.get(path(book))).json()
    with patch.object(events, "get", side_effect=read_then_write):
        response = await client.get(path(book))
    assert response.status_code == 200, response.text
    safe(response)
    assert response.json() == previous
    before = database_snapshot(engine)
    latest = await client.get(path(book))
    assert latest.status_code == 200, latest.text
    safe(latest)
    assert latest.json() != previous
    if change == "account":
        assert latest.json()["account_status"] == "ARCHIVED"
        for key in ("account_version", "portfolio_version", "portfolio_data_revision"):
            assert int(latest.json()[key]) == int(previous[key]) + 1
    elif change == "rejection":
        assert latest.json()["rejection_state"] == "REJECTED"
    else:
        assert latest.json()["revision_no"] == previous["revision_no"] + 1
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["owner_id", "event_id"])
async def test_internally_substituted_event_scope_is_not_relabelled_as_owned(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    with unit_of_work(engine) as con:
        current = events.get(con, book["owner"], book["event"].event_id)
    assert current is not None
    forged = replace(current, record=current.record.model_copy(update={field: uuid4()}))
    before = database_snapshot(engine)
    with patch.object(events, "get", return_value=forged):
        response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["id", "portfolio_id", "broker_id", "account_version"])
async def test_changed_account_pins_cannot_be_combined_with_other_current_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    with unit_of_work(engine) as con:
        current = account(con, book["owner"], book["account_id"])
    assert current is not None
    forged = dict(current)
    forged[field] = current[field] + 1 if field == "account_version" else str(uuid4())
    before = database_snapshot(engine)
    with patch.object(routes, "account", return_value=forged):
        response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("store", ["receipt", "rejection"])
@pytest.mark.parametrize("unavailable", ["missing", "error"])
async def test_unavailable_history_fails_privately_without_partial_pins(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], store: str, unavailable: str
) -> None:
    module = receipts if store == "receipt" else rejections
    options: dict[str, Any] = (
        {"return_value": None}
        if unavailable == "missing"
        else {"side_effect": ValueError("synthetic-private-history")}
    )
    before = database_snapshot(engine)
    with patch.object(module, "history", **options):
        response = await client.get(path(book))
    assert response.status_code == 503
    assert response.json()["code"] == "DIRECT_RECEIPT_INPUTS_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["invalid", "duplicate", "owner", "event", "revision", "number"]
)
async def test_malformed_rejection_cannot_be_presented_as_clear_or_current(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    rejected = reject(engine, rejection_intent(book["event"]), owner=book["owner"])
    retained: tuple[Any, ...]
    if problem == "invalid":
        retained = (object(),)
    elif problem == "duplicate":
        retained = (rejected, rejected)
    else:
        key = {
            "owner": "owner_id",
            "event": "event_id",
            "revision": "revision_id",
            "number": "revision_no",
        }[problem]
        retained = (rejected.model_copy(update={key: 2 if problem == "number" else uuid4()}),)
    before = database_snapshot(engine)
    with patch.object(rejections, "history", return_value=retained):
        response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["version", "data_revision"])
async def test_noninteger_portfolio_pins_are_not_coerced_to_exact_strings(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"UPDATE portfolios SET {field}=1.5 WHERE id=?", (str(book["portfolio_id"]),)
        )
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_model_keeps_exact_pins_and_rejects_contradictory_labels(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    response = await client.get(path(book))
    assert response.status_code == 200
    value = response.json()
    model = DirectReceiptInputs.model_validate(value)
    assert model.model_dump(mode="json") == value
    for key in ("event_id", "revision_id", "account_id", "portfolio_id", "broker_id"):
        assert value[key] not in repr(model)
    for change in (
        {"account_version": 1},
        {"portfolio_version": "0"},
        {"broker_version": "01"},
        {"portfolio_data_revision": "9223372036854775808"},
        {"revision_no": "1"},
        {"rejection_state": "REJECTED", "rejection_id": None},
        {"rejection_state": "NOT_REJECTED", "rejection_id": str(uuid4())},
        {"receipt_basis": "LEGACY_ENTITLEMENT"},
        {"entitlement_id": str(uuid4())},
        {"eligibility_scope": "ESTABLISHED"},
        {"payment_scope": "VERIFIED"},
        {"current_use_scope": "GRANTED"},
        {"history_completeness": "COMPLETE"},
        {"scope": "RECORDED_RECEIPT_INPUTS_ONLY"},
        {"workflow_status": "READY_TO_PAY"},
    ):
        with pytest.raises(ValidationError):
            DirectReceiptInputs.model_validate({**value, **change})
