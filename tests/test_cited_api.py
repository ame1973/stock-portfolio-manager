"""Protected cited source writes retain atomic ownership, replay and financial isolation."""

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.cited_routes as routes
import spm_api.providers.cited as core
from spm_api.audit import now, stamp
from spm_api.cited_models import CitedSourceCreated
from spm_api.db import unit_of_work
from spm_api.mutations import mutate
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.profiles import latest
from spm_api.providers.profiles import save as save_profile
from tests.helpers import database_snapshot, write_headers
from tests.test_cited_evidence import book as book
from tests.test_cited_evidence import payload

pytestmark = pytest.mark.anyio
PATH = "/api/v1/providers/cited-observations"
REASON = "Explicit synthetic source-access revision for the citation HTTP boundary."


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    for private in (
        "owner_id",
        "context_id",
        "profile_json",
        "profile_sha256",
        "credential_artifact_id",
        "alias_json",
        "request_json",
    ):
        assert private not in response.text


def source_only(before: Any, after: Any) -> None:
    allowed = {
        "provider_fetch_contexts",
        "provider_runs",
        "dividend_observations",
        "split_observations",
        "provider_observation_sightings",
        "audit_events",
        "idempotency_keys",
    }
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }


def pause(engine: Engine, owner: UUID) -> None:
    with unit_of_work(engine, write=True) as con:
        record = latest(con, owner, "manual")
        assert record is not None
        save_profile(
            con,
            owner,
            record.policy.model_copy(update={"enabled": False}),
            expected_version=record.version,
            at=now(),
            request_id=uuid4(),
            reason=REASON,
        )


@pytest.mark.parametrize("provider", ["manual", "hkex_evidence", "sec_evidence"])
@pytest.mark.parametrize("split", [False, True])
async def test_actual_protected_create_retains_one_source_and_server_authorship(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    split: bool,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Cited HTTP never constructs a provider HTTP client")

    monkeypatch.setattr("spm_api.providers.runtime.ProviderHTTP", forbidden)
    before = database_snapshot(engine)
    response = await client.post(
        PATH,
        json=payload(book, provider, split=split),
        headers={**write_headers(client), "X-Request-ID": "untrusted-client-request"},
    )
    assert response.status_code == 201
    safe(response)
    assert "location" not in response.headers and "etag" not in response.headers
    created = response.json()
    assert set(created) == {
        "provider_id",
        "security_id",
        "operation",
        "observation_id",
        "provider_run_id",
        "citation_id",
        "scope",
        "canonical_adoption",
        "canonical_adoption_scope",
        "document_verification",
    }
    assert (
        created["scope"] == "SOURCE_ONLY"
        and created["canonical_adoption"] == "NOT_ADOPTED"
        and created["canonical_adoption_scope"] == "RESPONSE_ONLY_NOT_LINKAGE"
        and created["document_verification"] == "NOT_VERIFIED"
    )
    assert UUID(created["citation_id"]).version == UUID(created["provider_run_id"]).version == 4
    source_only(before, database_snapshot(engine))
    with unit_of_work(engine) as con:
        authored = con.exec_driver_sql(
            "SELECT owner_id,entity_id,request_id FROM audit_events "
            "WHERE action='USER_CITED_SOURCE_ASSERTED'"
        ).one()
        assert tuple(authored) == (
            str(book["owner"]),
            created["citation_id"],
            response.headers["x-request-id"],
        )
        assert authored[2] != "untrusted-client-request"
        assert con.exec_driver_sql(
            "SELECT response_status,response_json FROM idempotency_keys WHERE route=?",
            (f"POST {PATH}",),
        ).one() == (201, json.dumps(created, separators=(",", ":")))
        assert con.exec_driver_sql(
            "SELECT attempted,http_status,job_id FROM provider_runs"
        ).one() == (0, None, None)
    endpoint = "providers/split-observations" if split else "dividends/observations"
    read = await client.get(f"/api/v1/{endpoint}")
    assert read.status_code == 200
    row = read.json()["items"][0]
    assert (
        row["id"] == created["observation_id"]
        and row["provider_run_id"] == created["provider_run_id"]
    )
    assert row["evidence"]["id"] == created["citation_id"] and row["sightings_count"] == 1


async def test_committed_http_replay_skips_private_preflight_after_disable_and_alias_change(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = write_headers(client, key="cited-owner-uncertain-intent")
    body = payload(book)
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    pause(engine, book["owner"])
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE provider_symbols SET version=version+1 WHERE provider_id='manual'"
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Committed replay must not load private files, prepare, resolve or save"
        )

    monkeypatch.setattr(core, "prepare", forbidden)
    monkeypatch.setattr(core, "save", forbidden)
    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    before = database_snapshot(engine)
    replay = await client.post(PATH, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == original.json()
    safe(replay)
    assert database_snapshot(engine) == before


async def test_changed_input_conflicts_before_any_private_preparation(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, body = write_headers(client), payload(book)
    assert (await client.post(PATH, json=body, headers=headers)).status_code == 201

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Conflicting intent must fail before preparation")

    monkeypatch.setattr(core, "prepare", forbidden)
    before = database_snapshot(engine)
    rejected = await client.post(PATH, json={**body, "dps": "0.2"}, headers=headers)
    assert rejected.status_code == 409 and rejected.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_snapshot(engine) == before


async def test_normalized_decimal_and_ratio_inputs_replay_the_same_intent(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    for split in (False, True):
        headers, body = write_headers(client), payload(book, split=split)
        response = await client.post(PATH, json=body, headers=headers)
        assert response.status_code == 201
        changed = (
            {**body, "ratio_numerator": 1, "ratio_denominator": 3}
            if split
            else {**body, "dps": "0.10000000"}
        )
        before = database_snapshot(engine)
        replay = await client.post(PATH, json=changed, headers=headers)
        assert replay.status_code == 201 and replay.json() == response.json()
        assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["cookie", "csrf", "origin", "key"])
async def test_security_dependencies_reject_before_private_preparation(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unauthorized input must not prepare private evidence")

    monkeypatch.setattr(core, "prepare", forbidden)
    headers = write_headers(client)
    if failure == "cookie":
        client.cookies.clear()
    if failure == "csrf":
        headers["X-CSRF-Token"] = "incorrect-synthetic-csrf"
    if failure == "origin":
        headers["Origin"] = "https://untrusted.example"
    if failure == "key":
        headers["Idempotency-Key"] = "short"
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(book), headers=headers)
    assert response.status_code == {"cookie": 401, "csrf": 403, "origin": 403, "key": 422}[failure]
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("failure", ["revoked", "expired", "csrf"])
async def test_current_authorization_is_rechecked_inside_writer_before_write_or_cached_result(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    failure: str,
) -> None:
    headers, body = write_headers(client), payload(book)
    if replay:
        assert (await client.post(PATH, json=body, headers=headers)).status_code == 201
    checkpoint: dict[str, Any] = {}

    def changed_after_preflight(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            if failure == "revoked":
                con.exec_driver_sql("UPDATE sessions SET revoked_at=?", (stamp(),))
            if failure == "expired":
                con.exec_driver_sql("UPDATE sessions SET expires_at='2000-01-01T00:00:00.000000Z'")
            if failure == "csrf":
                con.exec_driver_sql("UPDATE sessions SET csrf_sha256=?", ("0" * 64,))
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed_after_preflight)
    response = await client.post(PATH, json=body, headers=headers)
    assert response.status_code == (403 if failure == "csrf" else 401)
    assert database_snapshot(engine) == checkpoint["before"]


async def test_removed_advisory_receipt_fails_closed_without_writer_preparation(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, body = write_headers(client), payload(book)
    assert (await client.post(PATH, json=body, headers=headers)).status_code == 201
    checkpoint: dict[str, Any] = {}

    def removed_after_preflight(*args: Any, **kwargs: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(
                "DELETE FROM idempotency_keys WHERE idem_key=?", (headers["Idempotency-Key"],)
            )
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("An advisory replay cannot become a new preparation")

    monkeypatch.setattr(routes, "mutate", removed_after_preflight)
    monkeypatch.setattr(core, "prepare", forbidden)
    response = await client.post(PATH, json=body, headers=headers)
    assert (
        response.status_code == 409 and response.json()["code"] == "CITED_ASSERTION_RETRY_REQUIRED"
    )
    assert database_snapshot(engine) == checkpoint["before"]


@pytest.mark.parametrize("change", ["policy", "alias"])
async def test_new_capture_current_selection_and_alias_races_roll_back_the_whole_request(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    checkpoint: dict[str, Any] = {}

    def changed_after_preparation(*args: Any, **kwargs: Any) -> Any:
        if change == "policy":
            pause(engine, book["owner"])
        else:
            with unit_of_work(engine, write=True) as con:
                con.exec_driver_sql(
                    "UPDATE provider_symbols SET version=version+1 WHERE provider_id='manual'"
                )
        checkpoint["before"] = database_snapshot(engine)
        return mutate(*args, **kwargs)

    monkeypatch.setattr(routes, "mutate", changed_after_preparation)
    response = await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert response.status_code == 409 and response.json()["code"] == "CITED_SOURCE_CHANGED"
    safe(response)
    assert database_snapshot(engine) == checkpoint["before"]


@pytest.mark.parametrize("failure", ["audit", "serialization"])
async def test_late_audit_and_serialization_failures_cannot_commit_source_or_idempotency(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic late cited request failure")

    monkeypatch.setattr(core, "audit", fail) if failure == "audit" else monkeypatch.setattr(
        CitedSourceCreated, "model_dump_json", fail
    )
    before = database_snapshot(engine)
    with pytest.raises(RuntimeError, match="Synthetic late cited request failure"):
        await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert database_snapshot(engine) == before


async def test_another_prepared_actor_is_not_authorized_by_the_current_session(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_prepare = core.prepare

    async def swapped(*args: Any, **kwargs: Any) -> Any:
        prepared = await real_prepare(*args, **kwargs)
        return replace(prepared, fetch=replace(prepared.fetch, owner_id=uuid4()))

    monkeypatch.setattr(core, "prepare", swapped)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert response.status_code == 401 and response.json()["code"] == "UNAUTHORIZED"
    assert database_snapshot(engine) == before


async def test_unconfigured_source_is_not_an_accepted_quote_or_canonical_event(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    pause(engine, book["owner"])
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert response.status_code == 503 and response.json()["code"] == "CITED_SOURCE_UNCONFIGURED"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["symbol", "date", "official host", "security"])
async def test_invalid_attribution_and_dated_identity_do_not_persist_or_echo_inputs(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    change: str,
) -> None:
    body = payload(book, "hkex_evidence")
    if change == "symbol":
        body["provider_symbol"] = "unmapped-private-symbol"
    if change == "date":
        body["ex_date"] = "2019-01-01"
    if change == "security":
        body["security_id"] = str(uuid4())
    if change == "official host":
        body["citation"]["url"] = "https://private-document.example/query?private-marker=not-public"
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 422
    assert body["provider_symbol"] not in response.text and "private-marker" not in response.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field",
    [
        "owner_id",
        "citation_id",
        "provider_event_id",
        "normalized_sha256",
        "http_status",
        "raw_sha256",
        "scope",
        "canonical_adoption_scope",
        "cash_amount",
    ],
)
async def test_trusted_and_financial_extra_fields_are_rejected_before_preflight(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Untrusted identity and adoption input must not prepare")

    monkeypatch.setattr(core, "prepare", forbidden)
    before = database_snapshot(engine)
    response = await client.post(
        PATH, json={**payload(book), field: "private-invalid-marker"}, headers=write_headers(client)
    )
    assert response.status_code == 422 and "private-invalid-marker" not in response.text
    assert database_snapshot(engine) == before


async def test_unknown_private_value_error_is_safely_unavailable_not_echoed_or_repaired(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("private-configuration-or-reference-marker")

    monkeypatch.setattr(core, "prepare", unavailable)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert response.status_code == 503 and response.json()["code"] == "CITED_SOURCE_UNAVAILABLE"
    assert "private-configuration" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("scope", ["missing", "forged"])
async def test_retained_citation_scope_is_explicit_without_new_private_work(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    headers, body = write_headers(client), payload(book)
    original = await client.post(PATH, json=body, headers=headers)
    assert original.status_code == 201
    retained = original.json()
    if scope == "missing":
        del retained["canonical_adoption_scope"]
    else:
        retained["canonical_adoption_scope"] = "LINKAGE_CHECKED"
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            (json.dumps(retained), headers["Idempotency-Key"]),
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A retained response must not prepare private evidence")

    monkeypatch.setattr(core, "prepare", forbidden)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=headers)
    if scope == "missing":
        assert response.status_code == 201 and response.json() == original.json()
    else:
        assert response.status_code == 503 and response.json()["code"] == "CITED_SOURCE_UNAVAILABLE"
    assert database_snapshot(engine) == before


async def test_corrupt_cached_response_fails_closed_without_private_reads_or_repair(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, body = write_headers(client), payload(book)
    assert (await client.post(PATH, json=body, headers=headers)).status_code == 201
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE idempotency_keys SET response_json=? WHERE idem_key=?",
            ('{"private-corrupt-marker":"invalid"}', headers["Idempotency-Key"]),
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Retained replay corruption is not permission for private work")

    monkeypatch.setattr(core, "prepare", forbidden)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=headers)
    assert response.status_code == 503 and response.json()["code"] == "CITED_SOURCE_UNAVAILABLE"
    assert "private-corrupt-marker" not in response.text
    assert database_snapshot(engine) == before


async def test_simultaneous_identical_intents_commit_only_one_citation_and_receipt(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    headers, body = write_headers(client), payload(book)
    results = await asyncio.gather(
        *(client.post(PATH, json=body, headers=headers) for _ in range(2))
    )
    assert all(response.status_code == 201 for response in results)
    assert results[0].json() == results[1].json()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 1
        assert con.exec_driver_sql("SELECT count(*) FROM provider_runs").scalar_one() == 1
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='USER_CITED_SOURCE_ASSERTED'"
            ).scalar_one()
            == 1
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM idempotency_keys WHERE route=?", (f"POST {PATH}",)
            ).scalar_one()
            == 1
        )


async def test_citation_is_an_ordinary_guarded_write_not_a_password_or_key_import(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE sessions SET reauthenticated_at=NULL")
    response = await client.post(PATH, json=payload(book), headers=write_headers(client))
    assert (
        response.status_code == 201 and response.json()["document_verification"] == "NOT_VERIFIED"
    )


@pytest.mark.parametrize("amount", [None, "0", "92233720368.54775807"])
async def test_exact_nullable_and_boundary_dps_survive_the_actual_http_wire(
    book: dict[str, Any], client: httpx.AsyncClient, amount: str | None
) -> None:
    body = {**payload(book), "dps": amount}
    body["citation"]["url"] = "https://example.test/synthetic?private-link-marker=fixture#page"
    created = await client.post(PATH, json=body, headers=write_headers(client))
    assert created.status_code == 201
    read = await client.get("/api/v1/dividends/observations")
    row = read.json()["items"][0]
    assert row["dps"] == (None if amount is None else "0.00000000" if amount == "0" else amount)
    assert row["currency"] is None and row["source_currency"] == "EUR"
    assert row["evidence"]["url"] is None and row["evidence"]["url_withheld"]
    assert "private-link-marker" not in created.text and "private-link-marker" not in read.text


@pytest.mark.parametrize(
    "numerator,denominator", [("1", "3"), ("9223372036854775807", "9223372036854775806")]
)
async def test_exact_provider_only_ratio_digits_survive_the_actual_http_wire(
    book: dict[str, Any], client: httpx.AsyncClient, numerator: str, denominator: str
) -> None:
    body = {
        **payload(book, split=True),
        "ratio_numerator": numerator,
        "ratio_denominator": denominator,
    }
    assert (await client.post(PATH, json=body, headers=write_headers(client))).status_code == 201
    read = await client.get("/api/v1/providers/split-observations")
    row = read.json()["items"][0]
    assert (row["ratio_numerator"], row["ratio_denominator"]) == (numerator, denominator)
    assert row["effect_scope"] == "PROVIDER_ADJUSTMENT_ONLY"


@pytest.mark.parametrize(
    "change",
    [
        "float",
        "overprecision",
        "missing operation",
        "unknown operation",
        "vendor",
        "citation id",
        "reason NUL",
    ],
)
async def test_discriminated_json_rejects_untrusted_or_inexact_values_without_preparing(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    body = payload(book)
    if change == "float":
        body["dps"] = 0.1
    if change == "overprecision":
        body["dps"] = "0.100000001"
    if change == "missing operation":
        del body["operation"]
    if change == "unknown operation":
        body["operation"] = "ADOPT_DIVIDEND"
    if change == "vendor":
        body["provider_id"] = "eodhd"
    if change == "citation id":
        body["citation"]["id"] = str(uuid4())
    if change == "reason NUL":
        body["citation"]["reason"] = "Synthetic\0private reason"

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid JSON cannot begin private preparation")

    monkeypatch.setattr(core, "prepare", forbidden)
    before = database_snapshot(engine)
    response = await client.post(PATH, json=body, headers=write_headers(client))
    assert response.status_code == 422
    assert database_snapshot(engine) == before
