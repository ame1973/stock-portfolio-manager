"""Protected projection writes retain explicit recorded inputs, never receipt authority."""

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_projection_history as history
import spm_api.dividend_projection_routes as routes
import spm_api.dividend_projection_write_models as write_models
import spm_api.dividend_projection_writer as writer
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import PASSWORD, database_snapshot, version_headers, write_headers
from tests.test_dividend_projection_api import book as book
from tests.test_dividend_projection_inputs_api import QUERY
from tests.test_dividend_projection_inputs_api import path as input_path
from tests.test_dividend_projection_writer import settings
from tests.test_dividend_tax_service import intent as tax_intent
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any]) -> str:
    return (
        f"/api/v1/dividends/events/{book['event'].event_id}"
        f"/accounts/{book['account_id']}/projections"
    )


async def scope(client: httpx.AsyncClient, book: dict[str, Any]) -> dict[str, Any]:
    result = await client.get(input_path(book), params=QUERY)
    assert result.status_code == 200, result.text
    return dict(result.json())


def body(value: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "revision_id": value["revision_id"],
        "portfolio_id": value["portfolio_id"],
        "broker_id": value["broker_id"],
        "account_version": value["account_version"],
        "broker_version": value["broker_version"],
        "expected_data_revision": value["portfolio_data_revision"],
        "expected_projection_no": value["projection_head_no"],
        "expected_projection_id": value["projection_head_id"],
        "as_of": value["as_of"],
        "eligibility_rule": value["eligibility_rule"],
        "tax_effective_on": value["tax"]["effective_on"],
        "expected_tax_head_no": value["tax"]["head_no"],
        "expected_tax_head_id": value["tax"]["head_id"],
        "expected_tax_selected_id": value["tax"]["selected_id"],
        "fee_effective_on": value["fee"]["effective_on"],
        "expected_fee_head_no": value["fee"]["head_no"],
        "expected_fee_head_id": value["fee"]["head_id"],
        "expected_fee_selected_id": value["fee"]["selected_id"],
        "fx_rate_hkd": None,
        "fx_source": None,
        "fee_rate_event": None,
        "fee_rate_source": None,
        "confirmation": "RECORDED_PROJECTION_ONLY",
        "confirm_revision_fence": True,
        "reason": "Synthetic projection reviewed explicitly.",
        **changes,
    }


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert "owner_id" not in response.text and "audit_id" not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "synthetic-private" not in response.text


async def test_explicit_missing_inputs_record_review_not_zero_or_a_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    before = database_snapshot(engine)
    with (
        patch.object(ProviderConfigurationStore, "load", side_effect=AssertionError("private")),
        patch.object(ProviderHTTP, "__init__", side_effect=AssertionError("upstream")),
    ):
        response = await client.post(
            path(book),
            json=body(observed),
            headers=version_headers(client, observed["revision_no"]),
        )
    assert response.status_code == 201, response.text
    safe(response)
    result = response.json()
    assert result["state"] == "NEEDS_REVIEW" and result["projection_no"] == "1"
    assert result["recorded_shares"] is None and result["estimated_amounts"] is None
    assert result["tax_provenance"]["status"] == result["fee_provenance"]["status"] == "MISSING"
    assert result["scope"] == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    assert result["history_completeness"] == "UNVERIFIED"
    assert result["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert result["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    allowed = {"audit_events", "dividend_projection_versions", "idempotency_keys"}
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }
    assert (
        len(after["dividend_projection_versions"])
        == len(before["dividend_projection_versions"]) + 1
    )
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1


async def test_estimated_projection_excludes_ex_day_trade_and_never_defaults_fx(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    settings(engine, book)
    observed = await scope(client, book)
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json=body(observed), headers=version_headers(client, observed["revision_no"])
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["state"] == "EXPECTED" and result["recorded_shares"] == "10.00000000"
    amounts = result["estimated_amounts"]
    assert amounts["gross_local"] == "10.000000" and amounts["net_local"] == "7.500000"
    assert amounts["estimated_hkd"] is None and amounts["quality"] == "MISSING_FX"
    assert result["fx_rate_hkd"] is None and result["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    for table in ("dividend_receipts", "cash_ledger_entries", "dividend_entitlements"):
        assert after[table] == before[table]


async def test_before_ex_date_and_explicit_fx_remain_unverified_recorded_values(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    settings(engine, book)
    response = await client.get(input_path(book), params={**QUERY, "as_of": "2026-01-07"})
    assert response.status_code == 200
    observed = response.json()
    before = database_snapshot(engine)
    result = await client.post(
        path(book),
        json=body(observed, fx_rate_hkd="7.8", fx_source="MANUAL:synthetic"),
        headers=version_headers(client, 1),
    )
    assert result.status_code == 201, result.text
    record = result.json()
    assert record["timing_scope"] == "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY"
    assert record["recorded_shares"] == "10.00000000"
    assert record["fx_rate_hkd"] == "7.800000000000"
    assert record["estimated_amounts"]["estimated_hkd"] == "58.500000"
    assert record["eligibility_scope"] == "NOT_ESTABLISHED"
    after = database_snapshot(engine)
    for table in ("dividend_receipts", "cash_ledger_entries", "dividend_entitlements"):
        assert after[table] == before[table]


async def test_replay_after_account_change_is_historical_and_zero_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    headers = version_headers(
        client, observed["revision_no"], key="synthetic-projection-replay-key"
    )
    payload = body(observed)
    first = await client.post(path(book), json=payload, headers=headers)
    assert first.status_code == 201
    changed = await client.patch(
        f"/api/v1/accounts/{book['account_id']}",
        json={"name": "Synthetic renamed account"},
        headers=version_headers(client, book["account_version"]),
    )
    assert changed.status_code == 200
    before = database_snapshot(engine)
    replay = await client.post(path(book), json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    safe(replay)
    assert replay.json()["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before
    conflict = await client.post(
        path(book), json={**payload, "reason": "A different synthetic reason."}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["account", "tax", "projection"])
async def test_changed_inputs_cannot_be_silently_rebound(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: str
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    if change == "account":
        result = await client.patch(
            f"/api/v1/accounts/{book['account_id']}",
            json={"name": "Synthetic changed account"},
            headers=version_headers(client, book["account_version"]),
        )
        assert result.status_code == 200
    elif change == "tax":
        with unit_of_work(engine, write=True) as con:
            tax.save(con, book["owner"], tax_intent(book), request_id=uuid4())
    else:
        result = await client.post(path(book), json=payload, headers=version_headers(client, 1))
        assert result.status_code == 201
    before = database_snapshot(engine)
    result = await client.post(path(book), json=payload, headers=version_headers(client, 1))
    expected = {
        "account": (409, "DIVIDEND_PROJECTION_SCOPE_CHANGED"),
        "tax": (409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
        "projection": (412, "PRECONDITION_FAILED"),
    }
    assert (result.status_code, result.json()["code"]) == expected[change]
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "expected_status", "expected_code"),
    [
        ("revision_id", 412, "PRECONDITION_FAILED"),
        ("expected_data_revision", 409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
        ("expected_tax_head_id", 409, "DIVIDEND_TAX_SETTING_CHANGED"),
        ("expected_tax_selected_id", 409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
        ("expected_fee_head_id", 409, "DIVIDEND_FEE_SETTING_CHANGED"),
        ("expected_fee_selected_id", 409, "DIVIDEND_PROJECTION_INPUT_CHANGED"),
    ],
)
async def test_wrong_but_well_formed_observed_pins_do_not_write(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    field: str,
    expected_status: int,
    expected_code: str,
) -> None:
    settings(engine, book)
    observed = await scope(client, book)
    wrong = "0" if field == "expected_data_revision" else str(uuid4())
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json=body(observed, **{field: wrong}), headers=version_headers(client, 1)
    )
    assert (response.status_code, response.json()["code"]) == (expected_status, expected_code)
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmation": "CONFIRMED_INCOME"},
        {"confirm_revision_fence": False},
        {"confirm_revision_fence": 1},
        {"confirm_revision_fence": "true"},
        {"account_version": 1},
        {"broker_version": "01"},
        {"expected_data_revision": "9223372036854775808"},
        {"expected_projection_no": "9223372036854775807"},
        {"expected_projection_id": str(uuid4())},
        {"expected_tax_selected_id": str(uuid4())},
        {"expected_fee_head_id": str(uuid4())},
        {"as_of": "2026-02-30"},
        {"eligibility_rule": "ASSUMED"},
        {"fx_rate_hkd": "7.8", "fx_source": None},
        {"fx_rate_hkd": "7.8000000000001", "fx_source": "MANUAL:synthetic"},
        {"fx_rate_hkd": "0", "fx_source": "MANUAL:synthetic"},
        {"fx_rate_hkd": "1", "fx_source": " source with whitespace "},
        {"fx_rate_hkd": "1", "fx_source": "IDENTITY"},
        {"fee_rate_event": "1", "fee_rate_source": "MANUAL:synthetic"},
        {"fee_rate_event": "1", "fee_rate_source": None},
    ],
)
async def test_invalid_or_incomplete_wire_pins_never_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], changes: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json=body(observed, **changes), headers=version_headers(client, 1)
    )
    assert response.status_code == 422, response.text
    safe(response)
    assert database_snapshot(engine) == before


async def test_snapshot_creation_requires_explicit_revision_fence_acknowledgment(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    payload.pop("confirm_revision_fence", None)
    before = database_snapshot(engine)
    response = await client.post(path(book), json=payload, headers=version_headers(client, 1))
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


async def test_new_snapshot_fences_withdrawal_and_rejection_until_reconciliation(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    created = await client.post(path(book), json=body(observed), headers=version_headers(client, 1))
    assert created.status_code == 201
    event = book["event"]
    before = database_snapshot(engine)
    for url, payload in (
        (
            f"/api/v1/dividends/events/{event.event_id}/withdraw",
            {
                "revision_id": str(event.revision_id),
                "confirmation": "WITHDRAW_EVENT",
                "citation": "Synthetic withdrawal citation",
                "reason": "Synthetic withdrawal decision.",
            },
        ),
        (
            f"/api/v1/dividends/{event.event_id}/verify",
            {
                "revision_id": str(event.revision_id),
                "decision": "REJECT_REVISION",
                "citation": "Synthetic rejection citation",
                "reason": "Synthetic rejection decision.",
            },
        ),
    ):
        result = await client.post(url, json=payload, headers=version_headers(client, 1))
        assert (
            result.status_code == 409
            and result.json()["code"] == "DIVIDEND_EVENT_PROJECTION_REQUIRED"
        )
        assert database_snapshot(engine) == before


async def test_recent_password_confirmation_is_required_before_new_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    headers = version_headers(client, 1, key="synthetic-projection-reauth-key")
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'")
    before = database_snapshot(engine)
    denied = await client.post(path(book), json=payload, headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "REAUTH_REQUIRED"
    assert database_snapshot(engine) == before
    confirmed = await client.post(
        "/api/v1/auth/reauth", json={"password": PASSWORD}, headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    saved = await client.post(path(book), json=payload, headers=headers)
    assert saved.status_code == 201


async def test_rejection_race_blocks_new_projection_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    event = book["event"]
    rejected = await client.post(
        f"/api/v1/dividends/{event.event_id}/verify",
        headers=version_headers(client, event.revision_no),
        json={
            "revision_id": str(event.revision_id),
            "decision": "REJECT_REVISION",
            "citation": "Synthetic rejection citation",
            "reason": "Synthetic rejection decision.",
        },
    )
    assert rejected.status_code == 200
    before = database_snapshot(engine)
    response = await client.post(
        path(book), json=body(observed), headers=version_headers(client, 1)
    )
    assert response.status_code == 409 and response.json()["code"] == "DIVIDEND_REVISION_REJECTED"
    safe(response)
    assert database_snapshot(engine) == before


async def test_competing_projection_writes_serialize_against_one_head(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    before = database_snapshot(engine)
    first, second = await asyncio.gather(
        client.post(path(book), json=payload, headers=version_headers(client, 1)),
        client.post(path(book), json=payload, headers=version_headers(client, 1)),
    )
    assert sorted((first.status_code, second.status_code)) == [201, 412]
    after = database_snapshot(engine)
    assert (
        len(after["dividend_projection_versions"])
        == len(before["dividend_projection_versions"]) + 1
    )
    assert len(after["idempotency_keys"]) == len(before["idempotency_keys"]) + 1
    for table in ("dividend_receipts", "cash_ledger_entries", "dividend_entitlements"):
        assert after[table] == before[table]


async def test_competing_rejection_and_projection_have_only_one_winner(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    event = book["event"]
    before = database_snapshot(engine)
    projection, rejection = await asyncio.gather(
        client.post(path(book), json=body(observed), headers=version_headers(client, 1)),
        client.post(
            f"/api/v1/dividends/{event.event_id}/verify",
            headers=version_headers(client, 1),
            json={
                "revision_id": str(event.revision_id),
                "decision": "REJECT_REVISION",
                "citation": "Synthetic rejection citation",
                "reason": "Synthetic rejection race.",
            },
        ),
    )
    assert (projection.status_code, rejection.status_code) in {(201, 409), (409, 200)}
    after = database_snapshot(engine)
    count = (
        len(after["dividend_projection_versions"]) - len(before["dividend_projection_versions"])
    ) + (len(after["dividend_event_rejections"]) - len(before["dividend_event_rejections"]))
    assert count == 1
    for table in ("dividend_receipts", "cash_ledger_entries", "dividend_entitlements"):
        assert after[table] == before[table]


@pytest.mark.parametrize("failure", ["revoked", "csrf", "reauth"])
@pytest.mark.parametrize("replay", [False, True])
async def test_writer_rechecks_session_security_even_for_replay(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    replay: bool,
) -> None:
    observed = await scope(client, book)
    payload, headers = body(observed), version_headers(client, 1)
    if replay:
        result = await client.post(path(book), json=payload, headers=headers)
        assert result.status_code == 201, result.text
    checkpoint: dict[str, Any] = {}

    def changed(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            elif failure == "csrf":
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
            else:
                con.exec_driver_sql(
                    "UPDATE sessions SET reauthenticated_at='2000-01-01T00:00:00.000000Z'"
                )
        checkpoint["state"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed)
    result = await client.post(path(book), json=payload, headers=headers)
    assert result.status_code == (401 if failure == "revoked" else 403)
    safe(result)
    assert database_snapshot(engine) == checkpoint["state"]


async def test_failed_http_binding_rolls_back_projection_and_all_audits(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    before = database_snapshot(engine)
    with patch.object(routes, "audit", side_effect=ValueError("synthetic-private")):
        response = await client.post(
            path(book), json=body(observed), headers=version_headers(client, 1)
        )
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_tampered_cached_projection_cannot_replace_audited_result(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    first_body = body(observed)
    first_headers = version_headers(client, 1, key="synthetic-projection-original-key")
    first = await client.post(path(book), json=first_body, headers=first_headers)
    assert first.status_code == 201
    later = await scope(client, book)
    second = await client.post(path(book), json=body(later), headers=version_headers(client, 1))
    assert second.status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(second.json()), first_headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(path(book), json=first_body, headers=first_headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["missing_history", "cached_value", "duplicate_http_audit"])
async def test_replay_requires_complete_audited_history_and_original_response(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    observed = await scope(client, book)
    payload = body(observed)
    headers = version_headers(client, 1, key="synthetic-projection-replay-corrupt")
    first = await client.post(path(book), json=payload, headers=headers)
    assert first.status_code == 201
    if corruption == "cached_value":
        changed = {**first.json(), "reason": "Another synthetic private reason."}
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
                (json.dumps(changed), headers["Idempotency-Key"]),
            )
    elif corruption == "duplicate_http_audit":
        with unit_of_work(engine, write=True) as con:
            audit(
                con,
                str(book["owner"]),
                "dividend_projection_http_intent",
                first.json()["id"],
                "DIVIDEND_PROJECTION_HTTP_INTENT",
                after={"synthetic": "duplicate"},
            )
    before = database_snapshot(engine)
    if corruption == "missing_history":
        with patch.object(history, "history", return_value=None):
            response = await client.post(path(book), json=payload, headers=headers)
    else:
        response = await client.post(path(book), json=payload, headers=headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


async def test_forged_cached_fingerprint_fails_closed_as_unavailable(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
    headers = version_headers(client, 1, key="synthetic-projection-forged-hash")
    original = await client.post(path(book), json=body(observed), headers=headers)
    assert original.status_code == 201
    altered = body(observed, reason="A substituted synthetic decision reason.")
    fingerprint = digest(
        canonical(
            {
                "event_version": 1,
                **write_models.ProjectionWrite.model_validate(altered).model_dump(mode="json"),
            }
        )
    )
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=? WHERE idem_key=?",
            (fingerprint, headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(path(book), json=altered, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_forged_request_cache_cannot_bypass_http_key_binding(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first_scope = await scope(client, book)
    first_headers = version_headers(client, 1, key="synthetic-projection-first-key")
    first = await client.post(path(book), json=body(first_scope), headers=first_headers)
    assert first.status_code == 201, first.json().get("code")
    second_scope = await scope(client, book)
    second_body = body(second_scope, reason="A second synthetic projection decision.")
    second_headers = version_headers(client, 1, key="synthetic-projection-second-key")
    second = await client.post(path(book), json=second_body, headers=second_headers)
    assert second.status_code == 201
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE idem_key=?",
            (second_headers["Idempotency-Key"],),
        ).one()
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET request_sha256=?,response_json=? WHERE idem_key=?",
            (row.request_sha256, row.response_json, first_headers["Idempotency-Key"]),
        )
    before = database_snapshot(engine)
    response = await client.post(path(book), json=second_body, headers=first_headers)
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("DIVIDEND_PROJECTION_NOT_FOUND", 404),
        ("DIVIDEND_PROJECTION_OWNER_UNAVAILABLE", 401),
        ("INVALID_DIVIDEND_PROJECTION_SCOPE", 422),
        ("INVALID_DIVIDEND_PROJECTION_FX", 422),
        ("DIVIDEND_PROJECTION_VERSION_CONFLICT", 412),
        ("DIVIDEND_PROJECTION_INPUT_CHANGED", 409),
        ("DIVIDEND_REVISION_REJECTED", 409),
        ("synthetic-private", 503),
    ],
)
def test_projection_writer_failures_map_to_private_fixed_errors(code: str, status: int) -> None:
    error = routes._failure(writer.ProjectionWriteError(code))
    assert error.status == status and "synthetic-private" not in error.message
    if status == 503:
        assert error.code == "DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE"


async def test_writer_contract_requires_confirmation_exact_pins_and_protected_headers(
    client: httpx.AsyncClient, book: dict[str, Any]
) -> None:
    spec = (await client.get("/openapi.json")).json()
    template = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections"
    operation = spec["paths"][template]["post"]
    assert operation["security"] == [{"cookieAuth": []}]
    headers = {p["name"]: p for p in operation["parameters"] if p["in"] == "header"}
    for name in ("X-CSRF-Token", "Idempotency-Key", "If-Match"):
        assert headers[name]["required"] is True
    request = spec["components"]["schemas"]["ProjectionWrite"]
    assert request["properties"]["confirmation"]["const"] == "RECORDED_PROJECTION_ONLY"
    assert request["properties"]["confirm_revision_fence"]["const"] is True
    assert {"confirmation", "confirm_revision_fence"} <= set(request["required"])
    assert request["additionalProperties"] is False
    for field in (
        "account_version",
        "broker_version",
        "expected_data_revision",
        "expected_projection_no",
        "expected_tax_head_no",
        "expected_fee_head_no",
    ):
        assert field in request["required"] and request["properties"][field]["type"] == "string"
    for field in ("fx_rate_hkd", "fx_source", "fee_rate_event", "fee_rate_source"):
        assert field in request["required"]


async def test_foreign_and_anonymous_sessions_cannot_create_projection(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    observed = await scope(client, book)
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
    response = await client.post(
        path(book), json=body(observed), headers=version_headers(client, 1)
    )
    assert response.status_code == 404
    safe(response)
    client.cookies.clear()
    response = await client.post(
        path(book), json=body(observed), headers=version_headers(client, 1)
    )
    assert response.status_code == 401
    safe(response)
    assert database_snapshot(engine) == before
