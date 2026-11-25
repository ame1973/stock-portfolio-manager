"""Protected hypothetical revision previews are private, read-only and not grants."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_revision_preview as preview
import spm_api.dividend_revision_preview_routes as routes
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session, read_auth
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_projection_writer import intent, prepare, save, settings
from tests.test_dividend_reconciliation_direct import direct_receipt
from tests.test_dividend_reconciliation_history import book as book
from tests.test_dividend_reconciliation_history import receipt
from tests.test_dividend_revision_preview import calculate, proposal
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any]) -> str:
    return f"/api/v1/dividends/events/{book['event'].event_id}/revision-preview"


def body(book: dict[str, Any], graph: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "revision_id": str(book["event"].revision_id),
        "proposal": proposal(graph).model_dump(mode="json"),
        "limit": 25,
        "after_account_id": None,
        "snapshot_token": None,
        **changes,
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "synthetic private source" not in response.text


async def test_preview_is_owner_scoped_exact_and_does_not_write_financial_facts(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    save(engine, book, intent(engine, book, tax, fee), uuid4())
    direct_receipt(engine, book, received_micro=6_000_000)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("network")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("network")),
    ):
        response = await client.post(
            path(book), json=body(book, graph), headers=version_headers(client, 1)
        )
    assert response.status_code == 200, response.text
    safe(response)
    result = response.json()
    assert result["scope"] == "HYPOTHETICAL_RECORDED_COMPARISON_ONLY"
    assert result["eligibility_scope"] == "NOT_ESTABLISHED"
    assert result["payment_scope"] == "OWNER_RECORDED_NOT_VERIFIED"
    assert result["revision_id"] == str(book["event"].revision_id)
    assert result["revision_no"] == 1 and result["next_after_account_id"] is None
    by_account = {value["account_id"]: value for value in result["items"]}
    held, empty = by_account[graph["source"]["id"]], by_account[graph["target"]["id"]]
    assert held["previous_expected"] == "10.000000" and held["expected_delta"] == "1.000000"
    assert held["proposed_amounts"]["net_local"] == "11.000000"
    assert held["recorded_received"] == "6.000000" and held["recorded_outstanding"] == "5.000000"
    assert held["state"] == "RECORDED_PARTIAL" and empty["state"] == "NO_RECORDED_POSITION"
    assert empty["proposed_amounts"] is None and empty["recorded_received"] is None
    assert database_snapshot(engine) == before


async def test_cursor_pages_reject_changed_inputs_and_changed_financial_scope(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    request = body(book, graph, limit=1)
    before = database_snapshot(engine)
    headers = version_headers(client, 1)
    first = await client.post(path(book), json=request, headers=headers)
    assert first.status_code == 200, first.text
    safe(first)
    first_page = first.json()
    assert len(first_page["items"]) == 1 and first_page["next_after_account_id"] is not None
    next_request = {
        **request,
        "after_account_id": first_page["next_after_account_id"],
        "snapshot_token": first_page["snapshot_token"],
    }
    second = await client.post(path(book), json=next_request, headers=headers)
    assert second.status_code == 200, second.text
    safe(second)
    assert len(second.json()["items"]) == 1 and second.json()["next_after_account_id"] is None
    assert second.json()["snapshot_token"] == first_page["snapshot_token"]
    assert second.json()["items"][0]["account_id"] != first_page["items"][0]["account_id"]
    assert database_snapshot(engine) == before
    changed = deepcopy(next_request)
    changed["proposal"]["dps"] = "2"
    mismatch = await client.post(path(book), json=changed, headers=headers)
    assert mismatch.status_code == 409
    safe(mismatch)
    assert mismatch.json()["code"] == "DIVIDEND_PREVIEW_SNAPSHOT_CHANGED"
    assert database_snapshot(engine) == before
    direct_receipt(engine, book, received_micro=6_000_000)
    before = database_snapshot(engine)
    stale = await client.post(path(book), json=next_request, headers=headers)
    assert stale.status_code == 409
    safe(stale)
    assert stale.json()["code"] == "DIVIDEND_PREVIEW_SNAPSHOT_CHANGED"
    assert database_snapshot(engine) == before


async def test_current_version_physical_pin_and_account_inputs_are_required(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    before = database_snapshot(engine)
    for data, version, expected in (
        (body(book, graph), 2, 412),
        (body(book, graph, revision_id=str(uuid4())), 1, 412),
    ):
        response = await client.post(
            path(book), json=data, headers=version_headers(client, version)
        )
        assert response.status_code == expected
        safe(response)
    missing = body(book, graph)
    missing["proposal"]["accounts"] = missing["proposal"]["accounts"][:1]
    response = await client.post(path(book), json=missing, headers=version_headers(client, 1))
    assert response.status_code == 409
    safe(response)
    assert database_snapshot(engine) == before


async def test_session_origin_csrf_and_foreign_events_fail_closed(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    request, headers = body(book, graph), version_headers(client, 1)
    before = database_snapshot(engine)
    wrong_origin = await client.post(
        path(book), json=request, headers={**headers, "Origin": "https://foreign.invalid"}
    )
    assert wrong_origin.status_code == 403
    safe(wrong_origin)
    wrong_csrf = await client.post(
        path(book), json=request, headers={**headers, "X-CSRF-Token": "x" * 40}
    )
    assert wrong_csrf.status_code == 403
    safe(wrong_csrf)
    missing = await client.post(
        f"/api/v1/dividends/events/{uuid4()}/revision-preview", json=request, headers=headers
    )
    assert missing.status_code == 404
    safe(missing)
    client.cookies.clear()
    unauthenticated = await client.post(path(book), json=request, headers=headers)
    assert unauthenticated.status_code == 401
    safe(unauthenticated)
    assert database_snapshot(engine) == before


async def test_malformed_private_values_never_echo_and_do_not_write(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    request = body(book, graph)
    request["proposal"]["accounts"][0]["fx_source"] = " synthetic private source "
    before = database_snapshot(engine)
    response = await client.post(path(book), json=request, headers=version_headers(client, 1))
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("alter", ["cash", "projection"])
async def test_consistent_substituted_result_cannot_override_retained_history(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], alter: str
) -> None:
    book = await prepare(client, engine, graph)
    tax, fee = settings(engine, book, rate="0", fee="0")
    save(engine, book, intent(engine, book, tax, fee), uuid4())
    direct_receipt(engine, book, received_micro=6_000_000)
    actual = calculate(engine, book, proposal(graph))
    held = next(value for value in actual.accounts if value.account_id == book["account_id"])
    if alter == "cash":
        forged = replace(held, recorded_received=Decimal("7"), recorded_outstanding=Decimal("4"))
    else:
        forged = replace(held, previous_expected=Decimal("9"), expected_delta=Decimal("2"))
    changed = replace(
        actual,
        accounts=tuple(
            forged if value.account_id == held.account_id else value for value in actual.accounts
        ),
    )
    before = database_snapshot(engine)
    with patch.object(preview, "read", return_value=changed):
        response = await client.post(
            path(book), json=body(book, graph), headers=version_headers(client, 1)
        )
    assert response.status_code == 503
    safe(response)
    assert response.json()["code"] == "DIVIDEND_REVISION_PREVIEW_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_cross_revision_legacy_and_direct_receipts_are_counted_once(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], book: dict[str, Any]
) -> None:
    receipt(engine, book, amount=6_000_000, older=True)
    direct_receipt(engine, book, received_micro=2_000_000)
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json=body(book, graph), headers=version_headers(client, 2)
    )
    assert response.status_code == 200, response.text
    safe(response)
    held = next(
        row for row in response.json()["items"] if row["account_id"] == graph["source"]["id"]
    )
    assert held["recorded_received"] == "8.000000"
    assert held["recorded_outstanding"] == "3.000000"
    assert held["active_receipts"] == "2" and held["unverified_origins"] == "1"
    assert held["state"] == "RECORDED_PARTIAL"
    assert database_snapshot(engine) == before


async def test_same_key_is_not_persisted_and_equivalent_input_preserves_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    request = body(book, graph)
    headers = version_headers(client, 1)
    before = database_snapshot(engine)
    first = await client.post(path(book), json=request, headers=headers)
    assert first.status_code == 200
    alternative = deepcopy(request)
    alternative["proposal"]["accounts"].reverse()
    alternative["proposal"]["dps"] = "1.1000"
    second = await client.post(path(book), json=alternative, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert database_snapshot(engine) == before
    direct_receipt(engine, book, received_micro=6_000_000)
    before = database_snapshot(engine)
    fresh = await client.post(path(book), json=request, headers=headers)
    assert fresh.status_code == 200
    assert fresh.json()["snapshot_token"] != first.json()["snapshot_token"]
    assert database_snapshot(engine) == before


async def test_snapshot_token_is_session_bound_and_cursor_is_checked(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    first = await client.post(
        path(book), json=body(book, graph, limit=1), headers=version_headers(client, 1)
    )
    assert first.status_code == 200
    value = first.json()
    continued = body(
        book,
        graph,
        limit=1,
        after_account_id=value["next_after_account_id"],
        snapshot_token=value["snapshot_token"],
    )
    before = database_snapshot(engine)
    invalid = await client.post(
        path(book),
        json={**continued, "after_account_id": str(uuid4())},
        headers=version_headers(client, 1),
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_CURSOR"
    safe(invalid)
    modified = await client.post(
        path(book),
        json={**continued, "snapshot_token": "0" * 64},
        headers=version_headers(client, 1),
    )
    assert modified.status_code == 409
    safe(modified)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        token, csrf, _ = issue_session(con, str(book["owner"]), "second-synthetic-session")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    different_session = await client.post(
        path(book), json=continued, headers=version_headers(client, 1)
    )
    assert different_session.status_code == 409
    assert different_session.json()["code"] == "DIVIDEND_PREVIEW_SNAPSHOT_CHANGED"
    safe(different_session)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_preview_another_owners_event(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    with unit_of_work(engine, write=True) as con:
        other = uuid4()
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (str(other),),
        )
        token, csrf, _ = issue_session(con, str(other), "foreign-synthetic-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json=body(book, graph), headers=version_headers(client, 1)
    )
    assert response.status_code == 404
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("changed", ["session", "owner", "csrf"])
async def test_session_and_csrf_are_rechecked_within_the_read_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], changed: str
) -> None:
    book = await prepare(client, engine, graph)
    with unit_of_work(engine) as con:
        auth = read_auth(con, client.cookies.get(COOKIE))
    if changed == "session":
        other = replace(auth, session_id=str(uuid4()))
    elif changed == "owner":
        other = replace(auth, owner=auth.owner.model_copy(update={"id": uuid4()}))
    else:
        other = replace(auth, csrf_sha256="0" * 64)
    before = database_snapshot(engine)
    with patch.object(routes, "read_auth", return_value=other):
        response = await client.post(
            path(book), json=body(book, graph), headers=version_headers(client, 1)
        )
    assert response.status_code == (403 if changed == "csrf" else 401)
    safe(response)
    assert database_snapshot(engine) == before


async def test_actual_wal_append_cannot_mix_one_page_snapshot(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    original = preview.read

    def concurrent(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(direct_receipt, engine, book, received_micro=6_000_000).result()
        return result

    headers = version_headers(client, 1)
    with patch.object(preview, "read", side_effect=concurrent):
        first = await client.post(path(book), json=body(book, graph), headers=headers)
    assert first.status_code == 200, first.text
    before = database_snapshot(engine)
    second = await client.post(path(book), json=body(book, graph), headers=headers)
    assert second.status_code == 200, second.text
    for response in (first, second):
        safe(response)
    old = next(row for row in first.json()["items"] if row["account_id"] == graph["source"]["id"])
    new = next(row for row in second.json()["items"] if row["account_id"] == graph["source"]["id"])
    assert old["state"] == "NO_RECORDED_ACTIVE_RECEIPTS" and old["recorded_received"] is None
    assert new["state"] == "RECORDED_PARTIAL" and new["recorded_received"] == "6.000000"
    assert first.json()["snapshot_token"] != second.json()["snapshot_token"]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "exception",
        "event",
        "revision",
        "revision-number",
        "security",
        "ex-date",
        "as-of",
        "rule",
        "proposal",
        "omitted-account",
        "duplicate-account",
        "duplicate-history",
        "foreign-history",
        "portfolio-owner",
        "portfolio-id",
        "portfolio-security",
        "portfolio-revision",
        "portfolio-date",
        "portfolio-rule",
        "pin-version",
        "no-portfolio",
        "unverified-label",
    ],
)
async def test_corrupt_internal_scope_never_returns_a_partial_preview(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], corruption: str
) -> None:
    book = await prepare(client, engine, graph)
    direct_receipt(engine, book, received_micro=6_000_000)
    value = proposal(graph)
    result = calculate(engine, book, value)
    snapshot, retained = result.snapshot, result.snapshot.retained
    portfolio = snapshot.portfolios[0]
    history = retained.accounts[0]
    pin = portfolio.accounts[0]
    if corruption == "missing":
        result = None
    elif corruption == "exception":
        result = preview.RevisionPreviewError("synthetic private source")
    elif corruption == "event":
        result = replace(
            result, snapshot=replace(snapshot, retained=replace(retained, event_id=uuid4()))
        )
    elif corruption == "revision":
        result = replace(
            result, snapshot=replace(snapshot, retained=replace(retained, revision_id=uuid4()))
        )
    elif corruption == "revision-number":
        result = replace(
            result, snapshot=replace(snapshot, retained=replace(retained, revision_no=2))
        )
    elif corruption == "security":
        result = replace(result, snapshot=replace(snapshot, security_id=uuid4()))
    elif corruption == "ex-date":
        result = replace(result, snapshot=replace(snapshot, proposed_ex_date=date(2026, 1, 9)))
    elif corruption == "as-of":
        result = replace(result, snapshot=replace(snapshot, as_of=date(2026, 1, 9)))
    elif corruption == "rule":
        result = replace(result, snapshot=replace(snapshot, rule="REQUIRES_REVIEW"))
    elif corruption == "proposal":
        result = replace(result, proposal=proposal(graph, dps="2"))
    elif corruption == "omitted-account":
        result = replace(result, accounts=result.accounts[:-1])
    elif corruption == "duplicate-account":
        result = replace(result, accounts=result.accounts + result.accounts[:1])
    elif corruption == "duplicate-history":
        altered = replace(retained, accounts=retained.accounts + (history,))
        result = replace(result, snapshot=replace(snapshot, retained=altered))
    elif corruption == "foreign-history":
        altered = replace(retained, accounts=(replace(history, account_id=uuid4()),))
        result = replace(result, snapshot=replace(snapshot, retained=altered))
    elif corruption.startswith("portfolio-"):
        altered_book = portfolio.book
        if corruption == "portfolio-owner":
            altered_book = replace(altered_book, owner_id=uuid4())
        elif corruption == "portfolio-id":
            altered_book = replace(altered_book, portfolio_id=uuid4())
        elif corruption == "portfolio-security":
            altered_book = replace(altered_book, security_id=uuid4())
        elif corruption == "portfolio-revision":
            altered_book = replace(altered_book, data_revision=altered_book.data_revision + 1)
        elif corruption == "portfolio-date":
            altered_book = replace(altered_book, as_of=date(2026, 1, 9))
        else:
            altered_book = replace(altered_book, rule="REQUIRES_REVIEW")
        altered = replace(portfolio, book=altered_book)
        result = replace(result, snapshot=replace(snapshot, portfolios=(altered,)))
    elif corruption == "pin-version":
        altered_pin = replace(pin, account_version=pin.account_version + 1)
        altered = replace(portfolio, accounts=(altered_pin, *portfolio.accounts[1:]))
        result = replace(result, snapshot=replace(snapshot, portfolios=(altered,)))
    elif corruption == "no-portfolio":
        result = replace(result, snapshot=replace(snapshot, portfolios=()))
    else:
        result = replace(result, eligibility_scope=cast(Any, "GRANTED"))
    before = database_snapshot(engine)
    with patch.object(
        preview,
        "read",
        **({"side_effect": result} if isinstance(result, Exception) else {"return_value": result}),
    ):
        response = await client.post(
            path(book), json=body(book, graph), headers=version_headers(client, 1)
        )
    assert response.status_code == 503, corruption
    safe(response)
    assert response.json()["code"] == "DIVIDEND_REVISION_PREVIEW_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("bad", [0.5, "1", Decimal("NaN"), Decimal("Infinity")])
def test_amount_codec_rejects_non_decimal_or_nonfinite_values(bad: object) -> None:
    with pytest.raises(ValueError):
        routes._amount(cast(Any, bad))
    assert routes._amount(None) is None and routes._amount(Decimal("1.000000")) == "1.000000"


@pytest.mark.parametrize("bad", [True, "1", 1.0, None])
def test_ordinal_codec_requires_an_actual_integer(bad: object) -> None:
    with pytest.raises(ValueError):
        routes._ordinal(bad)
    assert routes._ordinal(1) == "1"


def test_date_codec_rejects_a_date_string() -> None:
    with pytest.raises(ValueError):
        routes._day("2026-01-08")
    assert routes._day(None) is None and routes._day(date(2026, 1, 8)) == "2026-01-08"


async def test_account_converter_rejects_misaligned_identity_and_state(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    result = calculate(engine, book, proposal(graph))
    portfolio = result.snapshot.portfolios[0]
    pin, observation = portfolio.accounts[0], portfolio.book.accounts[0]
    estimate = next(
        value for value in result.proposal.accounts if value.account_id == pin.account_id
    )
    current = next(value for value in result.accounts if value.account_id == pin.account_id)
    variants = [
        (cast(Any, object()), estimate, observation),
        (replace(current, account_id=uuid4()), estimate, observation),
        (current, estimate.model_copy(update={"account_id": uuid4()}), observation),
        (current, estimate, replace(observation, account_id=str(uuid4()))),
        (current, estimate, replace(observation, state="NEEDS_REVIEW")),
    ]
    for record, supplied, observed in variants:
        with pytest.raises(ValueError):
            routes._account(portfolio, pin, observed, supplied, record)
