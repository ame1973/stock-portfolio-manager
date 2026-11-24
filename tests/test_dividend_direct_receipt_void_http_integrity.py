"""Direct void HTTP boundaries must fail closed and roll back all financial changes."""

import json
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_void as voids
import spm_api.dividend_direct_receipt_void_write_routes as routes
from spm_api.audit import audit
from spm_api.db import unit_of_work
from spm_api.dividend_direct_receipt_void_write_models import DirectReceiptVoidWrite
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_combined_receipt_history import add_legacy
from tests.test_dividend_direct_receipt_inputs_api import book as book
from tests.test_dividend_direct_receipt_void_inputs_api import create
from tests.test_dividend_direct_receipt_void_write_api import body, move_account, path
from tests.test_dividend_receipt_write_api import safe
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "code,status",
    [
        ("DIRECT_RECEIPT_NOT_FOUND", 404),
        ("DIRECT_RECEIPT_OWNER_UNAVAILABLE", 401),
        ("INVALID_DIRECT_RECEIPT_VOID_SCOPE", 422),
        ("DIRECT_RECEIPT_VOID_VERSION_CHANGED", 412),
        ("DIRECT_RECEIPT_VOID_SCOPE_CHANGED", 409),
        ("DIRECT_RECEIPT_VOID_CONFLICT", 409),
        ("synthetic-private-diagnostic", 503),
    ],
)
def test_internal_error_mapping_never_discloses_unknown_codes(code: str, status: int) -> None:
    error = routes._failure(voids.DirectReceiptVoidError(code))
    assert error.status == status
    assert "synthetic-private" not in error.code + error.message


@pytest.mark.parametrize(
    "problem", ["missing", "legacy", "account", "event", "version", "cash", "broker", "data"]
)
async def test_stale_missing_and_legacy_targets_cannot_be_reversed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    legacy = (
        add_legacy(engine, (book["event"], str(book["account_id"])))
        if problem == "legacy"
        else None
    )
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    target, version, status = created["id"], 1, 409
    if problem == "missing":
        target, status = str(uuid4()), 404
    elif problem == "legacy":
        assert legacy is not None
        target, status = legacy["receipt"]["id"], 404
    elif problem in {"account", "event"}:
        payload[f"{problem}_id"], status = str(uuid4()), 404
    elif problem == "version":
        version, status = 2, 412
    else:
        field = {"cash": "cash_entry_id", "broker": "broker_id", "data": "expected_data_revision"}[
            problem
        ]
        payload[field] = "99" if problem == "data" else str(uuid4())
    before = database_snapshot(engine)
    response = await client.post(
        path(target), json=payload, headers=version_headers(client, version)
    )
    assert response.status_code == status, response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["origin", "csrf", "reauth", "session", "foreign"])
async def test_owner_origin_csrf_and_reauth_are_required_before_void(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    status = 403
    if problem == "origin":
        headers["Origin"] = "http://untrusted.test"
    elif problem == "csrf":
        headers["X-CSRF-Token"] = "x" * 32
    elif problem == "reauth":
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00Z'")
    elif problem == "session":
        client.cookies.clear()
        status = 401
    else:
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
        headers["X-CSRF-Token"] = csrf
        status = 404
    before = database_snapshot(engine)
    response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == status, response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "broker",
        "portfolio",
        "data",
        "old-version",
        "old-data",
        "reauth",
        "session",
        "csrf",
    ],
)
async def test_late_idempotency_scope_or_security_change_rolls_back_and_preserves_retry(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    created = await create(client, book)
    destination = await move_account(client, engine, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    original = Connection.execute

    def late(con: Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(con, statement, *args, **kwargs)
        if "INSERT INTO idempotency_keys" in str(statement):
            if change in {"reauth", "session", "csrf"}:
                sql = {
                    "reauth": "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00Z'",
                    "session": "UPDATE sessions SET revoked_at='2026-01-01T00:00:00Z'",
                    "csrf": "UPDATE sessions SET csrf_sha256='synthetic-changed'",
                }[change]
                con.exec_driver_sql(sql)
            else:
                table, column, identifier = {
                    "account": ("accounts", "version", str(book["account_id"])),
                    "broker": ("brokers", "version", str(book["broker_id"])),
                    "portfolio": ("portfolios", "version", destination),
                    "data": ("portfolios", "data_revision", destination),
                    "old-version": ("portfolios", "version", created["portfolio_id"]),
                    "old-data": ("portfolios", "data_revision", created["portfolio_id"]),
                }[change]
                con.exec_driver_sql(
                    f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (identifier,)
                )
        return result

    before = database_snapshot(engine)
    with patch.object(Connection, "execute", late):
        response = await client.post(path(created["id"]), json=payload, headers=headers)
    status = 401 if change == "session" else 403 if change in {"csrf", "reauth"} else 409
    assert response.status_code == status, response.text
    safe(response)
    assert database_snapshot(engine) == before
    retry = await client.post(path(created["id"]), json=payload, headers=headers)
    assert retry.status_code == 200, retry.text


@pytest.mark.parametrize("failure", ["error", "ignored", "duplicate"])
async def test_http_audit_failure_does_not_commit_partial_cash(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], failure: str
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    original = audit

    def fail(*args: Any, **kwargs: Any) -> Any:
        if failure == "error":
            raise ValueError("synthetic-private-audit-error")
        if failure == "duplicate":
            original(*args, **kwargs)
            original(*args, **kwargs)
        return None

    before = database_snapshot(engine)
    with patch.object(routes, "audit", side_effect=fail):
        response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before
    assert (
        await client.post(path(created["id"]), json=payload, headers=headers)
    ).status_code == 200


async def test_ignored_idempotency_cannot_commit_a_reversal(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "CREATE TRIGGER synthetic_ignore_direct_void_idem BEFORE INSERT ON idempotency_keys "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    before = database_snapshot(engine)
    response = await client.post(
        path(created["id"]), json=payload, headers=version_headers(client, 1)
    )
    assert response.status_code == 503 and response.json()["code"] == "IDEMPOTENCY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["id", "reversal_entry_id", "portfolio_data_revision"])
async def test_corrupted_cached_response_never_becomes_a_new_void(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == 200
    forged = {
        **response.json(),
        field: "99" if field == "portfolio_data_revision" else str(uuid4()),
    }
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE route=? AND idem_key=?",
            (json.dumps(forged), f"POST {path(created['id'])}", headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(path(created["id"]), json=payload, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_missing_idempotency_cannot_adopt_a_prior_http_void(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    headers = version_headers(client, 1)
    first = await client.post(path(created["id"]), json=payload, headers=headers)
    assert first.status_code == 200
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "DELETE FROM idempotency_keys WHERE route=?", (f"POST {path(created['id'])}",)
        )
    before = database_snapshot(engine)
    repeated = await client.post(path(created["id"]), json=payload, headers=headers)
    assert repeated.status_code == 409 and repeated.json()["code"] == "DIRECT_RECEIPT_VOID_CONFLICT"
    safe(repeated)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["invalid", "duplicate"])
async def test_substituted_history_cannot_be_used_as_direct_authority(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    with unit_of_work(engine) as con:
        retained = combined.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert retained is not None
    before = database_snapshot(engine)
    with patch.object(
        combined, "history", return_value=(object(),) if problem == "invalid" else retained * 2
    ):
        response = await client.post(
            path(created["id"]), json=payload, headers=version_headers(client, 1)
        )
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_public_reversal_rejects_invalid_retained_lifecycle_and_missing_links(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    intent = routes._intent(UUID(created["id"]), DirectReceiptVoidWrite.model_validate(payload), 1)
    response = await client.post(
        path(created["id"]), json=payload, headers=version_headers(client, 1)
    )
    assert response.status_code == 200
    with unit_of_work(engine) as con:
        record = routes._record(con, book["owner"], intent)
        for invalid in (
            replace(record, receipt=replace(record.receipt, status="ACTIVE")),
            replace(record, receipt=replace(record.receipt, version=1)),
            replace(record, receipt=replace(record.receipt, voided_at=None)),
            replace(record, revision_id=uuid4()),
            replace(record, revision_no=2),
            replace(record, origin=replace(record.origin, cash_entry_id=uuid4())),
        ):
            with (
                patch.object(routes, "_record", return_value=invalid),
                pytest.raises(voids.DirectReceiptVoidError),
            ):
                routes._public(con, book["owner"], intent, invalid)
        with pytest.raises(voids.DirectReceiptVoidError):
            routes._request_id(con, uuid4())
        broken = MagicMock(spec=Connection)
        broken.execute.return_value.all.return_value = []
        with (
            patch.object(routes, "_record", return_value=record),
            pytest.raises(voids.DirectReceiptVoidError),
        ):
            routes._public(broken, book["owner"], intent, record)


async def test_changed_http_binding_is_not_accepted_by_audit_shape_alone(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])

    def changed(*args: Any, **kwargs: Any) -> Any:
        return audit(*args, **{**kwargs, "after": {"scope": "synthetic-wrong-intent"}})

    before = database_snapshot(engine)
    with patch.object(routes, "audit", side_effect=changed):
        response = await client.post(
            path(created["id"]), json=payload, headers=version_headers(client, 1)
        )
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_commit_response_substitution_rolls_back_the_financial_transaction(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    created = await create(client, book)
    payload = await body(client, book, created["id"])
    public = routes._public
    calls = 0

    def changed(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = public(*args, **kwargs)
        calls += 1
        return result if calls == 1 else result.model_copy(update={"origin_portfolio_id": uuid4()})

    before = database_snapshot(engine)
    with patch.object(routes, "_public", side_effect=changed):
        response = await client.post(
            path(created["id"]), json=payload, headers=version_headers(client, 1)
        )
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"fx_rate_hkd": None, "fx_source": None, "tax": None, "fee": None},
        {"currency": "HKD", "fx_rate_hkd": None, "fx_source": None},
        {"actual_hkd": "47", "actual_hkd_reason": "Synthetic retained cash adjustment"},
        {"received_amount": "-6"},
        {"received_amount": "0"},
        {"received_amount": "9223372036854.775807", "fx_rate_hkd": None, "fx_source": None},
    ],
)
async def test_archived_account_reversal_preserves_exact_cash_fx_and_unknowns(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    created = await create(client, book, **changes)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
            (str(book["account_id"]),),
        )
    payload = await body(client, book, created["id"])
    response = await client.post(
        path(created["id"]), json=payload, headers=version_headers(client, 1)
    )
    assert response.status_code == 200, response.text
    safe(response)
    with unit_of_work(engine) as con:
        primary, reversal = con.exec_driver_sql(
            "SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12 FROM cash_ledger_entries "
            "WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=? ORDER BY entry_role",
            (created["id"],),
        ).all()
    assert tuple(reversal) == (
        -primary.amount_micro,
        -primary.booked_hkd_micro if primary.booked_hkd_micro is not None else None,
        primary.fx_rate_hkd_e12,
    )
