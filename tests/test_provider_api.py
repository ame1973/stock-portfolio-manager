"""Authenticated provider diagnostics distinguish local readiness, source history and coverage."""

import base64
import json
from datetime import timedelta
from importlib.metadata import PackageNotFoundError
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.providers.yfinance as sdk
from spm_api.audit import stamp
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import FetchState, registry
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.symbols import resolve_symbol
from spm_worker.dispatcher import dispatch_once
from tests.helpers import create, database_snapshot, version_headers, write_headers
from tests.test_provider_configuration import KEY, policy, provision
from tests.test_provider_persistence import FINISH, START, WINDOW, fetched, save
from tests.test_provider_persistence import book as book
from tests.test_sync_api import unchanged_finances

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sources(book: dict[str, Any], settings: Settings) -> dict[str, Any]:
    settings.provider_secret_dir = book["settings"].provider_secret_dir
    return book


@pytest.mark.parametrize("path", ["providers", "providers/eodhd/health", "providers/coverage"])
async def test_provider_reads_require_an_owner(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(f"/api/v1/{path}")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


async def test_candidate_registry_and_local_health_are_not_a_live_probe(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A provider diagnostic tried upstream HTTP")

    monkeypatch.setattr(ProviderHTTP, "get_json", no_http)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["next_cursor"] is None and len(result["items"]) == 8
    rows = {row["provider_id"]: row for row in result["items"]}
    assert set(rows) == set(registry())
    assert rows["eodhd"]["configured"] is True
    assert rows["eodhd"]["configuration"]["state"] == "STALE"
    assert rows["eodhd"]["data_rights"] == "UNVERIFIED"
    assert rows["yahoo"]["origin_group"] == rows["yfinance"]["origin_group"] == "YAHOO"
    assert all(row["live_validation"] == "NOT_TESTED" for row in rows.values())
    assert all(row["raw_payload_storage"] is False for row in rows.values())
    assert rows["manual"]["data_rights"] == "MANUAL_EVIDENCE_ONLY"
    health = await client.get("/api/v1/providers/eodhd/health")
    assert health.status_code == 200
    value = health.json()
    assert value["state"] == "STALE" and value["configured"] is True
    assert value["last_result"] is None and value["last_success_at"] is None
    assert value["last_error_code"] is None and value["quota_remaining"] is None
    assert value["live_validation"] == "NOT_TESTED"
    assert KEY.decode() not in response.text + health.text
    coverage = await client.get("/api/v1/providers/coverage")
    assert coverage.status_code == 200 and len(coverage.json()["items"]) == 8
    assert KEY.decode() not in coverage.text
    assert database_snapshot(engine) == before


def assert_private_free(response: httpx.Response) -> None:
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert KEY.decode() not in response.text
    for hidden in (
        "profile_json",
        "profile_sha256",
        "alias_json",
        "request_json",
        "result_json",
        "credential_source",
        "credential_version",
        "terms_reference",
        "terms_reason",
        "context_sha256",
        "owner_id",
        "job_id",
        'next_cursor":"/stocks',
        "warnings_json",
    ):
        assert hidden not in response.text


async def read_coverage(
    client: httpx.AsyncClient, sources: dict[str, Any], **params: Any
) -> dict[str, Any]:
    response = await client.get(
        "/api/v1/providers/coverage",
        params={
            "security_id": sources["security"]["id"],
            "provider_id": "eodhd",
            **params,
        },
    )
    assert_private_free(response)
    rows = response.json()["items"]
    assert len(rows) == 1 and response.json()["next_cursor"] is None
    return dict(rows[0])


async def test_no_attempt_has_unknown_coverage_even_with_an_explicit_alias(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["provider_symbol"] is None
    assert row["status"] == "NOT_TESTED" and row["configuration"]["configured"] is True
    assert row["supported_market"] is True and row["supported_operation"] is True
    assert row["availability"] == "DATA_UNAVAILABLE" and row["observations_count"] == 0
    assert row["coverage_start"] is row["coverage_end"] is row["last_result"] is None
    assert row["last_success_at"] is None and row["coverage_basis"] == "NOT_ESTABLISHED"
    assert row["live_validation"] == "NOT_TESTED" and row["legacy_runs_omitted"] == 0
    assert row["legacy_status_ignored"] is False
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "payload, expected, availability",
    [
        ([], "NO_DATA", "DATA_NOT_CONFIRMED"),
        (
            [{"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "USD"}],
            "OK_WITH_DATA",
            "OBSERVATIONS_AVAILABLE",
        ),
    ],
)
async def test_complete_recorded_page_is_not_authoritative_coverage(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    payload: Any,
    expected: str,
    availability: str,
) -> None:
    receipt = save(engine, sources, await fetched(sources, payload=payload))
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["status"] == expected and row["availability"] == availability
    assert row["observations_count"] == len(payload)
    assert row["provider_symbol"] == "0005.HK"
    assert row["coverage_start"] is row["coverage_end"] is None
    assert row["coverage_basis"] == "NOT_ESTABLISHED"
    recorded = row["last_result"]
    assert recorded["id"] == str(receipt.run_id) and recorded["scope"] == "RECORDED_SOURCE_PAGE"
    assert recorded["window"] == WINDOW.model_dump(mode="json")
    assert recorded["parse_complete"] is True and recorded["has_more"] is False
    assert recorded["attempted"] is True and recorded["http_status"] == 200
    assert recorded["source_url"] == "https://eodhd.com/api/div/0005.HK"
    assert recorded["observations_count"] == len(payload) and recorded["issue_count"] == 0
    assert row["last_success_at"] == stamp(FINISH)
    health = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(health)
    assert health.json()["state"] == "STALE" and health.json()["configured"] is True
    assert health.json()["last_result"] == recorded
    assert health.json()["last_success_at"] == stamp(FINISH)
    assert health.json()["quota_remaining"] is None
    assert health.json()["live_validation"] == "NOT_TESTED"
    assert database_snapshot(engine) == before


async def test_partial_evidence_does_not_gain_a_success_timestamp_or_coverage(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(
        sources,
        payload=[
            {"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "USD"},
            {"date": "not-a-calendar-date", "unadjustedValue": "0.1"},
        ],
    )
    assert page.complete is False and page.issue_count == 1
    save(engine, sources, page)
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["status"] == "OK_WITH_DATA" and row["observations_count"] == 1
    assert row["availability"] == "OBSERVATIONS_AVAILABLE"
    assert row["last_result"]["parse_complete"] is False
    assert row["last_result"]["issue_count"] == 1 and row["last_success_at"] is None
    assert row["coverage_start"] is row["coverage_end"] is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "status, expected", [(401, "AUTH_ERROR"), (429, "RATE_LIMITED"), (503, "TRANSIENT_ERROR")]
)
async def test_new_failure_does_not_hide_retained_assertions_or_erase_parsed_success(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    status: int,
    expected: str,
) -> None:
    save(engine, sources, await fetched(sources))
    failed = await fetched(sources, status=status)
    receipt = save(
        engine,
        sources,
        failed,
        started_at=START + timedelta(seconds=1),
        finished_at=FINISH + timedelta(seconds=1),
    )
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["status"] == expected and row["observations_count"] == 1
    assert row["availability"] == "OBSERVATIONS_AVAILABLE"
    assert row["last_result"]["id"] == str(receipt.run_id)
    assert row["last_result"]["http_status"] == status
    assert row["last_success_at"] == stamp(FINISH)
    assert row["coverage_start"] is row["coverage_end"] is None
    health = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(health)
    assert health.json()["state"] == "STALE"
    assert health.json()["last_result"]["state"] == expected
    assert health.json()["last_error_code"] == failed.error_code
    assert health.json()["quota_remaining"] is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "state, availability",
    [
        (FetchState.UNSUPPORTED, "PROVIDER_UNSUPPORTED"),
        (FetchState.UNCONFIGURED, "DATA_UNAVAILABLE"),
        (FetchState.PARSE_ERROR, "DATA_UNAVAILABLE"),
        (FetchState.STALE, "DATA_UNAVAILABLE"),
    ],
)
async def test_recorded_non_http_failure_is_not_counted_as_an_upstream_response(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    state: FetchState,
    availability: str,
) -> None:
    save(engine, sources, FetchPage(state=state, window=WINDOW, error_code="NOT_PROBED"))
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["status"] == state.value and row["availability"] == availability
    assert row["last_result"]["attempted"] is False
    assert row["last_result"]["http_status"] is row["last_result"]["source_url"] is None
    assert row["last_success_at"] is row["coverage_start"] is row["coverage_end"] is None
    assert row["observations_count"] == 0
    assert database_snapshot(engine) == before


async def test_later_empty_page_and_repeat_sighting_never_reset_unique_evidence_count(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(sources)
    first = save(engine, sources, page)
    repeated = save(
        engine,
        sources,
        page,
        started_at=START + timedelta(seconds=1),
        finished_at=FINISH + timedelta(seconds=1),
    )
    assert first.observations_new == 1 and repeated.observations_new == 0
    empty = save(
        engine,
        sources,
        await fetched(sources, payload=[]),
        started_at=START + timedelta(seconds=2),
        finished_at=FINISH + timedelta(seconds=2),
    )
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["status"] == "NO_DATA" and row["observations_count"] == 1
    assert row["availability"] == "OBSERVATIONS_AVAILABLE"
    assert row["last_result"]["id"] == str(empty.run_id)
    assert row["last_result"]["observations_count"] == 0
    assert row["coverage_start"] is row["coverage_end"] is None
    assert database_snapshot(engine) == before


async def test_current_profile_loss_does_not_relabel_old_success_as_live_health(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    receipt = save(engine, sources, await fetched(sources))
    sources["profile"].unlink()
    before = database_snapshot(engine)
    health = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(health)
    result = health.json()
    assert result["state"] == "UNCONFIGURED" and result["configured"] is False
    assert result["reason"] == "CONFIG_NOT_FOUND"
    assert result["last_result"]["id"] == str(receipt.run_id)
    assert result["last_success_at"] == stamp(FINISH) and result["quota_remaining"] is None
    row = await read_coverage(client, sources)
    assert row["status"] == "OK_WITH_DATA" and row["configuration"]["state"] == "UNCONFIGURED"
    assert row["coverage_start"] is row["coverage_end"] is None
    assert row["observations_count"] == 1
    assert database_snapshot(engine) == before


async def test_capability_market_and_operation_do_not_claim_symbol_coverage(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    alpha = await read_coverage(client, sources, provider_id="alphavantage", operation="SPLITS")
    assert alpha["supported_market"] is False and alpha["supported_operation"] is False
    assert alpha["status"] == "NOT_TESTED" and alpha["provider_symbol"] is None
    assert alpha["coverage_start"] is alpha["coverage_end"] is None
    yahoo = await read_coverage(client, sources, provider_id="yahoo")
    sdk_row = await read_coverage(client, sources, provider_id="yfinance")
    assert yahoo["origin_group"] == sdk_row["origin_group"] == "YAHOO"
    assert yahoo["supported_market"] is sdk_row["supported_market"] is True
    assert yahoo["observations_count"] == sdk_row["observations_count"] == 0
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("provider", ["manual", "hkex_evidence", "sec_evidence"])
async def test_enabled_manual_profile_is_only_input_readiness_not_document_verification(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine, provider: str
) -> None:
    provision(sources["settings"], provider)
    before = database_snapshot(engine)
    response = await client.get(f"/api/v1/providers/{provider}/health")
    assert_private_free(response)
    value = response.json()
    assert value["configured"] is True and value["state"] == "STALE"
    assert value["reason"] == "MANUAL_INPUT_ONLY" and value["last_result"] is None
    assert value["quota_remaining"] is value["last_success_at"] is None
    assert value["live_validation"] == "NOT_TESTED"
    assert database_snapshot(engine) == before


async def test_missing_optional_sdk_stays_unconfigured_despite_ready_policy_files(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision(sources["settings"], "yfinance")

    def missing(_: str) -> Any:
        raise PackageNotFoundError("private SDK dependency detail")

    monkeypatch.setattr(sdk, "version", missing)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/yfinance/health")
    assert_private_free(response)
    assert response.json()["configured"] is False
    assert (
        response.json()["state"] == "UNCONFIGURED"
        and response.json()["reason"] == "SDK_NOT_INSTALLED"
    )
    assert "private SDK dependency detail" not in response.text
    listed = await client.get("/api/v1/providers")
    assert_private_free(listed)
    item = next(row for row in listed.json()["items"] if row["provider_id"] == "yfinance")
    assert item["configured"] is False and item["configuration"]["reason"] == "SDK_NOT_INSTALLED"
    assert item["capabilities"] == ["DIVIDENDS", "SPLITS"]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"enabled": False}, "PROVIDER_DISABLED"),
        ({"terms_acknowledged": False}, "RIGHTS_NOT_ACKNOWLEDGED"),
        ({"raw_payload_storage": True}, "CONFIG_INVALID"),
        ({"requests_per_minute": 0}, "CONFIG_INVALID"),
    ],
)
async def test_invalid_or_disabled_policy_reports_safe_local_code_without_provider_io(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    change: dict[str, Any],
    expected: str,
) -> None:
    provision(sources["settings"], body=policy(**change))
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(response)
    assert response.json()["state"] == "UNCONFIGURED" and response.json()["configured"] is False
    assert response.json()["reason"] == expected and response.json()["last_result"] is None
    assert database_snapshot(engine) == before


async def test_coverage_keyset_spans_unique_provider_security_pairs_in_server_order(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    for ticker in ("0006", "0007"):
        await create(
            client,
            "securities",
            {
                "exchange_code": "HKG",
                "display_ticker": ticker,
                "name": "Synthetic coverage page",
                "quote_currency": "HKD",
                "identifier_effective_from": "2020-01-01",
            },
        )
    before = database_snapshot(engine)
    params: dict[str, Any] = {"limit": 5}
    seen: list[tuple[str, str]] = []
    while True:
        response = await client.get("/api/v1/providers/coverage", params=params)
        assert_private_free(response)
        page = response.json()
        assert 0 < len(page["items"]) <= 5
        seen.extend((row["provider_id"], row["security_id"]) for row in page["items"])
        if page["next_cursor"] is None:
            break
        key = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
        assert key == list(seen[-1])
        params["cursor"] = page["next_cursor"]
        assert len(seen) < 24
    assert len(seen) == len(set(seen)) == 24 and seen == sorted(seen)
    filtered = await client.get(
        "/api/v1/providers/coverage",
        params={
            "security_id": sources["security"]["id"],
            "q": "eodhd",
            "limit": 1,
        },
    )
    assert_private_free(filtered)
    assert [(row["provider_id"], row["security_id"]) for row in filtered.json()["items"]] == [
        ("eodhd", sources["security"]["id"]),
    ]
    assert filtered.json()["next_cursor"] is None
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("query", ["%", "eodhd%", "\\"])
async def test_coverage_search_treats_wildcards_and_backslash_as_literal_text(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine, query: str
) -> None:
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/coverage", params={"q": query})
    assert_private_free(response)
    assert response.json() == {"items": [], "next_cursor": None}
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00.000000Z",
        "eodhd ",
        "EODHD",
        "-1",
        "99999999999999999999",
        "unknown",
    ],
)
async def test_coverage_rejects_noncanonical_or_other_resource_cursor_domains(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/coverage", params={"cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": -1},
        {"limit": "not-an-integer"},
        {"cursor": "not-base64"},
        {"cursor": "x" * 1501},
        {"q": "x" * 101},
        {"provider_id": "not-a-provider"},
        {"security_id": "not-a-uuid"},
        {"operation": "QUOTES"},
    ],
)
async def test_coverage_invalid_bounded_inputs_do_not_write_or_echo_values(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine, params: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/coverage", params=params)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert response.json()["code"] in {"VALIDATION_ERROR", "INVALID_CURSOR"}
    assert "not-a-provider" not in response.text and "not-a-uuid" not in response.text
    assert KEY.decode() not in response.text
    assert database_snapshot(engine) == before


async def test_unknown_catalogue_security_returns_404_not_fabricated_coverage(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/coverage", params={"security_id": str(uuid4())})
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("path", ["providers", "providers/eodhd/health", "providers/coverage"])
async def test_authentication_precedes_every_private_file_read(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def no_files(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An unauthenticated diagnostic read private operator files")

    monkeypatch.setattr(ProviderConfigurationStore, "load", no_files)
    response = await client.get(f"/api/v1/{path}")
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["providers", "providers/eodhd/health", "providers/coverage"])
async def test_private_configuration_reads_are_outside_database_units_of_work(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    original = ProviderConfigurationStore.load
    reads = []

    def observed(store: ProviderConfigurationStore, provider: Any) -> Any:
        with unit_of_work(engine, write=True) as con:
            assert con.in_transaction()
        reads.append(provider)
        return original(store, provider)

    monkeypatch.setattr(ProviderConfigurationStore, "load", observed)
    before = database_snapshot(engine)
    response = await client.get(f"/api/v1/{path}")
    assert_private_free(response)
    assert len(reads) == (1 if path.endswith("/health") else 8)
    assert database_snapshot(engine) == before


async def test_retained_legacy_status_and_run_are_counted_but_never_adopted_or_dumped(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            """
            INSERT INTO provider_status(provider_id,security_id,provider_symbol,origin_group,status,
                last_success_at,coverage_start,coverage_end,last_error_code)
            VALUES ('eodhd',?,'untrusted-private-symbol','EODHD','EMPTY_CONFIRMED',
                '9999-01-01T00:00:00.000000Z','1900-01-01','9999-01-01',?)
        """,
            (sources["security"]["id"], KEY.decode()),
        )
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_provider_run_insert'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_provider_run_insert")
        con.exec_driver_sql(
            """
            INSERT INTO provider_runs(id,provider_id,origin_group,security_id,started_at,
                status,coverage_start,coverage_end,sanitized_error)
            VALUES (?,'eodhd','EODHD',?,?,'EMPTY_CONFIRMED','1900-01-01','9999-01-01',?)
        """,
            (str(uuid4()), sources["security"]["id"], stamp(START), KEY.decode()),
        )
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["legacy_runs_omitted"] == 1 and row["legacy_status_ignored"] is True
    assert row["status"] == "NOT_TESTED" and row["provider_symbol"] is None
    assert row["coverage_start"] is row["coverage_end"] is row["last_success_at"] is None
    assert row["last_result"] is None and row["observations_count"] == 0
    health = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(health)
    assert health.json()["state"] == "STALE" and health.json()["legacy_runs_omitted"] == 1
    assert health.json()["last_result"] is health.json()["last_success_at"] is None
    assert "untrusted-private-symbol" not in health.text
    assert database_snapshot(engine) == before


async def test_split_history_never_becomes_dividend_processing_or_a_second_vendor_vote(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    prepared = sources["prepared"]
    split = prepare_fetch(prepared.owner_id, prepared.configuration, prepared.segment, "SPLITS")
    split_book = {**sources, "prepared": split}
    page = await fetched(split_book, payload=[{"date": "2026-01-08", "split": "1/3"}])
    receipt = save(engine, split_book, page)
    before = database_snapshot(engine)
    dividends = await read_coverage(client, sources)
    assert dividends["status"] == "NOT_TESTED" and dividends["last_result"] is None
    assert dividends["observations_count"] == 0 and dividends["last_success_at"] is None
    splits = await read_coverage(client, sources, operation="SPLITS")
    assert splits["status"] == "OK_WITH_DATA" and splits["observations_count"] == 1
    assert splits["last_result"]["id"] == str(receipt.run_id)
    assert splits["last_result"]["operation"] == "SPLITS"
    assert splits["coverage_start"] is splits["coverage_end"] is None
    health = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(health)
    assert (
        health.json()["state"] == "STALE" and health.json()["last_result"]["operation"] == "SPLITS"
    )
    assert database_snapshot(engine) == before


async def test_complete_page_with_a_continuation_remains_only_a_processed_page(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    provision(sources["settings"], "massive")
    stock = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "BRK.B",
            "name": "Synthetic paged diagnostics",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{stock['id']}/provider-symbols",
        {
            "provider_id": "massive",
            "provider_symbol": "BRK.B",
            "effective_from": "2020-01-01",
        },
    )
    loaded = ProviderConfigurationStore(sources["settings"]).load("massive")
    with unit_of_work(engine) as con:
        resolved = resolve_symbol(con, UUID(stock["id"]), "massive", WINDOW)
    prepared = prepare_fetch(
        UUID(sources["owner"]["id"]), loaded, resolved.segments[0], "DIVIDENDS"
    )
    local = {**sources, "prepared": prepared, "security": stock}
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {KEY.decode()}"
        body = {
            "status": "OK",
            "results": [
                {
                    "ticker": "BRK.B",
                    "ex_dividend_date": "2026-01-08",
                    "cash_amount": "0.1",
                    "currency": "USD",
                }
            ],
            "next_url": "https://api.massive.com/stocks/v1/dividends?cursor=private-page-token",
        }
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(body).encode()),
            headers={"content-type": "application/json"},
        )

    async with configured_adapter(loaded, transport=httpx.MockTransport(respond)) as adapter:
        page = await adapter.fetch_dividends(prepared.segment)
    assert page.complete is True and page.next_cursor is not None
    save(engine, local, page)
    before = database_snapshot(engine)
    row = await read_coverage(client, local, provider_id="massive")
    assert row["last_result"]["parse_complete"] is True and row["last_result"]["has_more"] is True
    assert row["last_result"]["window"] == WINDOW.model_dump(mode="json")
    assert row["last_success_at"] == stamp(FINISH) and row["observations_count"] == 1
    assert row["coverage_start"] is row["coverage_end"] is None
    assert "private-page-token" not in json.dumps(row) and "next_cursor" not in row["last_result"]
    assert calls == ["/stocks/v1/dividends"]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("tie", [False, True])
async def test_latest_result_orders_by_completed_time_and_stable_run_id_not_started_time(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine, tie: bool
) -> None:
    page = await fetched(sources)
    first_id = UUID("00000000-0000-4000-8000-000000000001")
    second_id = UUID("00000000-0000-4000-8000-000000000002")
    save(
        engine,
        sources,
        page,
        run=first_id,
        started_at=START - timedelta(seconds=20),
        finished_at=FINISH + timedelta(seconds=30),
    )
    save(
        engine,
        sources,
        page,
        run=second_id,
        started_at=START + timedelta(seconds=1),
        finished_at=FINISH + timedelta(seconds=30 if tie else 2),
    )
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(response)
    assert response.json()["last_result"]["id"] == str(second_id if tie else first_id)
    assert response.json()["last_success_at"] == stamp(FINISH + timedelta(seconds=30))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "status, payload, expected",
    [
        (401, {"private": "discarded provider detail"}, "AUTH_ERROR"),
        (429, {"private": "discarded provider detail"}, "RATE_LIMITED"),
        (503, {"private": "discarded provider detail"}, "TRANSIENT_ERROR"),
        (200, {"private": "discarded provider detail"}, "PARSE_ERROR"),
    ],
)
async def test_failure_without_assertions_is_unavailable_not_confirmed_zero_income(
    sources: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    status: int,
    payload: Any,
    expected: str,
) -> None:
    save(engine, sources, await fetched(sources, status=status, payload=payload))
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["status"] == expected and row["availability"] == "DATA_UNAVAILABLE"
    assert row["observations_count"] == 0 and row["last_success_at"] is None
    assert row["coverage_start"] is row["coverage_end"] is None
    assert "discarded provider detail" not in json.dumps(row)
    assert database_snapshot(engine) == before


async def test_source_job_api_actual_dispatch_and_recorded_diagnostics_leave_financial_facts_alone(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    baseline = database_snapshot(engine)
    queued = await client.post(
        "/api/v1/dividends/sync",
        headers=write_headers(client),
        json={
            "security_ids": [sources["security"]["id"]],
            "provider_ids": ["eodhd"],
            "from_date": WINDOW.start,
            "to_date": WINDOW.end,
        },
    )
    assert queued.status_code == 202
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        with unit_of_work(engine, write=True) as con:
            assert con.exec_driver_sql("SELECT count(*) FROM upstream_attempts").scalar_one() == 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(
                b'[{"date":"2026-01-08","unadjustedValue":"0.9","currency":"USD"}]'
            ),
        )

    assert await dispatch_once(
        engine, sources["settings"], uuid4(), transport=httpx.MockTransport(respond)
    )
    before = database_snapshot(engine)
    unchanged_finances(baseline, before)
    job = await client.get(f"/api/v1/jobs/{queued.json()['job_id']}")
    assert job.status_code == 200 and job.json()["status"] == "SUCCEEDED"
    row = await read_coverage(client, sources)
    assert row["status"] == "OK_WITH_DATA" and row["observations_count"] == 1
    assert row["last_result"]["attempted"] is True
    assert row["last_result"]["http_status"] == 200 and row["last_result"]["parse_complete"] is True
    assert row["coverage_start"] is row["coverage_end"] is None
    health = await client.get("/api/v1/providers/eodhd/health")
    assert_private_free(health)
    assert health.json()["state"] == "STALE" and health.json()["quota_remaining"] is None
    assert health.json()["last_success_at"] == row["last_success_at"]
    assert health.json()["live_validation"] == "NOT_TESTED"
    assert calls == ["/api/div/0005.HK"]
    assert database_snapshot(engine) == before


async def test_recorded_symbol_stays_frozen_after_dated_alias_period_correction(
    sources: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    save(engine, sources, await fetched(sources))
    prepared = sources["prepared"]
    alias = prepared.segment.alias
    fixed = await client.patch(
        f"/api/v1/securities/{alias.security_id}/provider-symbols/{alias.id}",
        headers=version_headers(client, alias.version),
        json={
            "effective_from": "2020-01-01",
            "effective_to": "2026-01-31",
            "reason": "Synthetic dated provider identity correction",
        },
    )
    assert fixed.status_code == 200
    await create(
        client,
        f"securities/{alias.security_id}/provider-symbols",
        {
            "provider_id": "eodhd",
            "provider_symbol": "0005X.HK",
            "effective_from": "2026-02-01",
        },
    )
    before = database_snapshot(engine)
    row = await read_coverage(client, sources)
    assert row["provider_symbol"] == row["last_result"]["provider_symbol"] == "0005.HK"
    assert row["last_result"]["window"] == WINDOW.model_dump(mode="json")
    assert row["coverage_start"] is row["coverage_end"] is None
    assert database_snapshot(engine) == before
