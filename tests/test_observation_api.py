"""Read-only immutable observations retain exact source terms without financial adoption."""

import base64
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.audit import stamp
from spm_api.db import unit_of_work
from spm_api.job_models import SourceOperation
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import DividendObservation
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.providers.manual import EvidenceBatch, EvidenceProvider
from spm_api.providers.persistence import prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.symbols import resolve_symbol
from tests.helpers import create, database_snapshot
from tests.test_provider_configuration import KEY, provision
from tests.test_provider_manual import citation
from tests.test_provider_manual import dividend as cited_dividend
from tests.test_provider_manual import split as cited_split
from tests.test_provider_persistence import FINISH, START, WINDOW, fetched, save
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("path", ["dividends/observations", "providers/split-observations"])
async def test_observation_lists_require_an_owner(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(f"/api/v1/{path}")
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


async def test_dividend_observation_returns_exact_source_only_terms_and_first_retrieval(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    page = await fetched(book)
    receipt = save(engine, book, page)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["scope"] == "IMMUTABLE_SOURCE_V1"
    assert result["legacy_records_omitted"] == 0 and result["next_cursor"] is None
    assert len(result["items"]) == 1
    row = result["items"][0]
    assert row["id"] == receipt.observation_ids[0] and row["provider_run_id"] == str(receipt.run_id)
    assert row["provider_id"] == "eodhd" and row["provider_symbol"] == "0005.HK"
    assert row["origin_group"] == "EODHD" and row["security_id"] == book["security"]["id"]
    assert row["dps"] == "0.90000000" and row["currency"] == "USD"
    assert row["source_period"] == "QUARTERLY" and row["event_type"] == "UNKNOWN"
    assert row["ex_date"] == "2026-01-08" and row["pay_date"] is None
    assert row["adjustment_basis"] == "UNKNOWN"
    assert row["incomplete_reasons"] == ["UNRESOLVED_ADJUSTMENT"]
    assert row["provenance_version"] == 1 and row["canonical_adoption"] == "NOT_ADOPTED"
    assert row["sightings_count"] == 1 and row["last_seen_at"] == row["retrieved_at"]
    assert row["normalized_sha256"] == page.rows[0].normalized_sha256
    assert row["source_url"] == "https://eodhd.com/api/div/0005.HK"
    assert database_snapshot(engine) == before


async def test_unsupported_event_currency_is_not_filled_from_the_security_quote(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    save(
        engine,
        book,
        await fetched(
            book,
            payload=[
                {"date": "2026-01-08", "unadjustedValue": "0.9", "currency": "EUR"},
            ],
        ),
    )
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    row = response.json()["items"][0]
    assert book["security"]["quote_currency"] == "USD"
    assert row["currency"] is None and row["source_currency"] == "EUR"
    assert row["dps"] == "0.90000000" and "MISSING_CURRENCY" in row["incomplete_reasons"]
    assert row["canonical_adoption"] == "NOT_ADOPTED"
    assert database_snapshot(engine) == before


@pytest.fixture(params=["DIVIDENDS", "SPLITS"])
async def observed(
    request: pytest.FixtureRequest, book: dict[str, Any], engine: Engine
) -> dict[str, Any]:
    prepared = book["prepared"]
    local = {
        **book,
        "prepared": prepare_fetch(
            prepared.owner_id, prepared.configuration, prepared.segment, request.param
        ),
    }
    page = await fetched(
        local,
        payload=None
        if request.param == "DIVIDENDS"
        else [
            {"date": "2026-01-08", "split": "3/2"},
        ],
    )
    receipt = save(engine, local, page)
    return {
        **local,
        "operation": request.param,
        "page": page,
        "receipt": receipt,
        "path": "/api/v1/dividends/observations"
        if request.param == "DIVIDENDS"
        else "/api/v1/providers/split-observations",
    }


def assert_private_free(response: httpx.Response) -> None:
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert KEY.decode() not in response.text
    for private in (
        "source_metadata_json",
        "normalized_json",
        "raw_sha256",
        "profile_json",
        "profile_sha256",
        "credential_source",
        "credential_version",
        "owner_id",
        "job_id",
        "context_sha256",
        "alias_json",
        "terms_reference",
        "terms_reason",
    ):
        assert private not in response.text


async def test_source_reads_ignore_current_credentials_and_never_probe_or_load_private_files(
    observed: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed["profile"].unlink()
    observed["key"].unlink()

    def no_files(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An immutable evidence read loaded private configuration")

    async def no_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An immutable evidence read sent upstream HTTP")

    monkeypatch.setattr(ProviderConfigurationStore, "load", no_files)
    monkeypatch.setattr(ProviderHTTP, "get_json", no_http)
    before = database_snapshot(engine)
    response = await client.get(observed["path"])
    assert_private_free(response)
    assert response.json()["items"][0]["id"] == observed["receipt"].observation_ids[0]
    assert database_snapshot(engine) == before


async def test_repeated_sightings_preserve_first_run_time_fingerprint_and_one_observation(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    original = observed["receipt"]
    repeat = save(
        engine,
        observed,
        observed["page"],
        started_at=START + timedelta(days=1),
        finished_at=FINISH + timedelta(days=1),
    )
    assert repeat.observations_new == 0 and repeat.observation_ids == original.observation_ids
    before = database_snapshot(engine)
    response = await client.get(observed["path"])
    assert_private_free(response)
    assert len(response.json()["items"]) == 1
    row = response.json()["items"][0]
    assert row["provider_run_id"] == str(original.run_id) and row["retrieved_at"] == stamp(FINISH)
    assert row["sightings_count"] == 2 and row["last_seen_at"] == stamp(FINISH + timedelta(days=1))
    assert row["normalized_sha256"] == observed["page"].rows[0].normalized_sha256
    assert row["canonical_adoption"] == "NOT_ADOPTED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("ratio", ["3/2", "1/3", "9223372036854775807/9223372036854775806"])
async def test_provider_only_split_ratios_keep_every_int64_digit_as_json_strings(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, ratio: str
) -> None:
    prepared = book["prepared"]
    local = {
        **book,
        "prepared": prepare_fetch(
            prepared.owner_id, prepared.configuration, prepared.segment, "SPLITS"
        ),
    }
    page = await fetched(local, payload=[{"date": "2026-01-08", "split": ratio}])
    save(engine, local, page)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/providers/split-observations")
    assert_private_free(response)
    row = response.json()["items"][0]
    numerator, denominator = ratio.split("/")
    assert row["ratio_numerator"] == numerator and row["ratio_denominator"] == denominator
    assert row["effect_scope"] == "PROVIDER_ADJUSTMENT_ONLY"
    assert row["scope"] == "SOURCE_ONLY" and row["canonical_adoption"] == "NOT_ADOPTED"
    assert row["effective_date"] == "2026-01-08"
    assert (await client.get("/api/v1/dividends/observations")).json()["items"] == []
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("dps", [None, "0", "0.00000001", "92233720368.54775807"])
async def test_missing_and_known_zero_dps_are_distinct_exact_source_values(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, dps: str | None
) -> None:
    template = await fetched(book)
    assertion = DividendObservation(
        provider_id="eodhd",
        security_id=UUID(book["security"]["id"]),
        provider_symbol="0005.HK",
        ex_date=None,
        dps=dps,
        currency=None,
        adjustment_basis="UNKNOWN",
    )
    page = FetchPage[DividendObservation].model_validate(
        {
            **template.model_dump(mode="json"),
            "rows": [assertion.model_dump(mode="json")],
        }
    )
    save(engine, book, page)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert_private_free(response)
    row = response.json()["items"][0]
    assert row["dps"] == assertion.dps and row["ex_date"] is row["currency"] is None
    assert (
        "MISSING_EX_DATE" in row["incomplete_reasons"]
        and "MISSING_CURRENCY" in row["incomplete_reasons"]
    )
    assert ("MISSING_DPS" in row["incomplete_reasons"]) is (dps is None)
    assert row["pay_date"] is row["announce_date"] is row["record_date"] is None
    assert row["canonical_adoption"] == "NOT_ADOPTED"
    filtered = await client.get(
        "/api/v1/dividends/observations", params={"from_date": "2026-01-01"}
    )
    assert_private_free(filtered)
    assert filtered.json()["items"] == []
    assert database_snapshot(engine) == before


async def test_changed_source_terms_append_separate_rows_without_summing_economic_income(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    first = save(engine, book, await fetched(book))
    second = save(
        engine,
        book,
        await fetched(
            book,
            payload=[
                {"date": "2026-01-08", "unadjustedValue": "1.1", "currency": "USD"},
            ],
        ),
        started_at=START + timedelta(seconds=1),
        finished_at=FINISH + timedelta(seconds=1),
    )
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert_private_free(response)
    rows = response.json()["items"]
    assert [row["id"] for row in rows] == [second.observation_ids[0], first.observation_ids[0]]
    assert [row["dps"] for row in rows] == ["1.10000000", "0.90000000"]
    assert rows[0]["normalized_sha256"] != rows[1]["normalized_sha256"]
    assert all(row["canonical_adoption"] == "NOT_ADOPTED" for row in rows)
    assert database_snapshot(engine) == before


async def test_both_operation_lists_keep_their_own_exact_sighting_counts_and_source_ids(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    operation: SourceOperation = "SPLITS" if observed["operation"] == "DIVIDENDS" else "DIVIDENDS"
    prepared = observed["prepared"]
    other = {
        **observed,
        "prepared": prepare_fetch(
            prepared.owner_id, prepared.configuration, prepared.segment, operation
        ),
    }
    save(
        engine,
        other,
        await fetched(
            other,
            payload=None if operation == "DIVIDENDS" else [{"date": "2026-01-08", "split": "1/3"}],
        ),
    )
    before = database_snapshot(engine)
    dividends = await client.get("/api/v1/dividends/observations")
    splits = await client.get("/api/v1/providers/split-observations")
    assert_private_free(dividends)
    assert_private_free(splits)
    assert len(dividends.json()["items"]) == len(splits.json()["items"]) == 1
    assert (
        dividends.json()["items"][0]["sightings_count"]
        == splits.json()["items"][0]["sightings_count"]
        == 1
    )
    assert dividends.json()["items"][0]["id"] != splits.json()["items"][0]["id"]
    assert database_snapshot(engine) == before


async def test_event_date_filters_are_inclusive_and_are_not_retrieval_time_filters(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    response = await client.get(
        observed["path"], params={"from_date": "2026-01-08", "to_date": "2026-01-08"}
    )
    assert_private_free(response)
    assert len(response.json()["items"]) == 1
    for params in ({"from_date": "2026-01-09"}, {"to_date": "2026-01-07"}):
        result = await client.get(observed["path"], params=params)
        assert_private_free(result)
        assert result.json()["items"] == []
    filtered = await client.get(observed["path"], params={"provider_id": "yahoo"})
    assert_private_free(filtered)
    assert filtered.json()["items"] == []
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", ["%", "eodhd%", "_"])
async def test_source_search_treats_wildcards_literally(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    before = database_snapshot(engine)
    response = await client.get(observed["path"], params={"q": value})
    assert_private_free(response)
    assert response.json()["items"] == [] and response.json()["next_cursor"] is None
    assert database_snapshot(engine) == before


async def test_immutable_retrieval_keyset_is_bounded_stable_and_not_sorted_by_ex_date(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    payload = [
        {"date": f"2026-01-{day:02d}", "unadjustedValue": "0.1", "currency": "USD"}
        for day in range(1, 6)
    ]
    receipt = save(engine, book, await fetched(book, payload=payload))
    before = database_snapshot(engine)
    params: dict[str, Any] = {
        "limit": 2,
        "security_id": book["security"]["id"],
        "provider_id": "eodhd",
        "q": "0005.HK",
    }
    seen: list[str] = []
    while True:
        response = await client.get("/api/v1/dividends/observations", params=params)
        assert_private_free(response)
        result = response.json()
        assert 0 < len(result["items"]) <= 2
        seen.extend(row["id"] for row in result["items"])
        if result["next_cursor"] is None:
            break
        assert json.loads(base64.urlsafe_b64decode(result["next_cursor"])) == [
            stamp(FINISH),
            seen[-1],
        ]
        params["cursor"] = result["next_cursor"]
        assert len(seen) < 5
    assert seen == sorted(receipt.observation_ids, reverse=True) and len(set(seen)) == 5
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-10",
        "2026-01-10T00:00:00",
        "2026-01-10T00:00:00+00:00",
        "2026-01-10T00:00:00.000000+08:00",
        "eodhd",
        "99999999999999999999999999",
    ],
)
async def test_other_resource_or_noncanonical_timestamp_cursors_reject_without_writes(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine, value: str
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps([value, str(uuid4())]).encode()).decode()
    before = database_snapshot(engine)
    response = await client.get(observed["path"], params={"cursor": cursor})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_CURSOR"
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"q": "x" * 101},
        {"cursor": "bad cursor"},
        {"security_id": "not-a-uuid"},
        {"provider_id": "unknown"},
        {"from_date": "2026-02-30"},
        {"to_date": "2026-1-2"},
        {"from_date": "2026-01-31", "to_date": "2026-01-01"},
    ],
)
async def test_source_scope_date_and_pagination_inputs_are_bounded_and_explicit(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine, params: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(observed["path"], params=params)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert KEY.decode() not in response.text
    assert database_snapshot(engine) == before


async def test_unknown_security_is_404_not_successfully_empty_source_history(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    before = database_snapshot(engine)
    response = await client.get(observed["path"], params={"security_id": str(uuid4())})
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "hash",
        "private-extra",
        "float-json",
        "financial-mirror",
        "pointer",
        "first-time",
        "oversized-json",
    ],
)
async def test_corrupt_v1_assertions_fail_closed_without_echo_repair_or_financial_writes(
    observed: dict[str, Any], client: httpx.AsyncClient, engine: Engine, problem: str
) -> None:
    dividend = observed["operation"] == "DIVIDENDS"
    table = "dividend_observations" if dividend else "split_observations"
    guard_name = (
        "trg_dividend_observation_no_update"
        if dividend
        else "trg_provider_split_observations_update"
    )
    identifier = observed["receipt"].observation_ids[0]
    with unit_of_work(engine, write=True) as con:
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name=?", (guard_name,)
        ).scalar_one()
        con.exec_driver_sql(f"DROP TRIGGER {guard_name}")
        if problem == "oversized-json":
            raw = con.exec_driver_sql(
                f"SELECT normalized_json FROM {table} WHERE id=?", (identifier,)
            ).scalar_one()
            con.exec_driver_sql(
                f"UPDATE {table} SET normalized_json=? WHERE id=?", (" " * 16385 + raw, identifier)
            )
        elif problem in {"private-extra", "float-json"}:
            value = json.loads(
                con.exec_driver_sql(
                    f"SELECT normalized_json FROM {table} WHERE id=?", (identifier,)
                ).scalar_one()
            )
            if problem == "private-extra":
                value["api_key"] = KEY.decode()
            else:
                value["dps" if dividend else "ratio_numerator"] = 0.1
            con.exec_driver_sql(
                f"UPDATE {table} SET normalized_json=? WHERE id=?", (json.dumps(value), identifier)
            )
        elif problem == "financial-mirror":
            column = "dps_e8" if dividend else "ratio_num"
            con.exec_driver_sql(f"UPDATE {table} SET {column}={column}+1 WHERE id=?", (identifier,))
        else:
            column, value = {
                "hash": ("normalized_sha256", "0" * 64),
                "pointer": (
                    "source_url",
                    "https://eodhd.com/private?api_token=private-query-canary",
                ),
                "first-time": ("retrieved_at", stamp(START)),
            }[problem]
            con.exec_driver_sql(f"UPDATE {table} SET {column}=? WHERE id=?", (value, identifier))
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    response = await client.get(observed["path"])
    assert response.status_code == 503 and response.json()["code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert KEY.decode() not in response.text and "private-query-canary" not in response.text
    assert database_snapshot(engine) == before


async def test_retained_legacy_rows_are_reported_omitted_and_never_dump_private_payloads(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    receipt = save(engine, book, await fetched(book))
    with unit_of_work(engine, write=True) as con:
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_dividend_observation_no_update'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
        con.exec_driver_sql(
            """UPDATE dividend_observations SET provenance_version=0, normalized_json=NULL,
            source_url=?,source_metadata_json=? WHERE id=?""",
            (
                f"https://private.invalid/legacy?api_key={KEY.decode()}",
                json.dumps({"private_key": KEY.decode()}),
                receipt.observation_ids[0],
            ),
        )
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert_private_free(response)
    assert response.json() == {
        "items": [],
        "next_cursor": None,
        "legacy_records_omitted": 1,
        "scope": "IMMUTABLE_SOURCE_V1",
    }
    assert database_snapshot(engine) == before


async def test_unselected_metadata_json_is_not_exposed_or_repaired_by_a_valid_source_read(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    save(engine, book, await fetched(book))
    with unit_of_work(engine, write=True) as con:
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_dividend_observation_no_update'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_observations SET source_metadata_json=?",
            (json.dumps({"private_key": KEY.decode()}),),
        )
        con.exec_driver_sql(guard)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert_private_free(response)
    assert len(response.json()["items"]) == 1
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("provider", ["manual", "hkex_evidence", "sec_evidence"])
@pytest.mark.parametrize("operation", ["DIVIDENDS", "SPLITS"])
async def test_cited_manual_and_official_assertions_keep_identity_without_document_verification(
    book: dict[str, Any],
    client: httpx.AsyncClient,
    engine: Engine,
    provider: EvidenceProvider,
    operation: SourceOperation,
) -> None:
    stock = book["security"]
    if provider == "sec_evidence":
        stock = await create(
            client,
            "securities",
            {
                "exchange_code": "NYSE",
                "display_ticker": "BRK.B",
                "name": "Synthetic cited US evidence",
                "quote_currency": "USD",
                "identifier_effective_from": "2020-01-01",
            },
        )
    await create(
        client,
        f"securities/{stock['id']}/provider-symbols",
        {"provider_id": provider, "provider_symbol": "MAPPED", "effective_from": "2020-01-01"},
    )
    provision(book["settings"], provider)
    loaded = ProviderConfigurationStore(book["settings"]).load(provider)
    with unit_of_work(engine) as con:
        resolved = resolve_symbol(con, UUID(stock["id"]), provider, WINDOW)
    prepared = prepare_fetch(UUID(book["owner"]["id"]), loaded, resolved.segments[0], operation)
    source = (cited_dividend if operation == "DIVIDENDS" else cited_split)(
        provider, security_id=stock["id"], provider_symbol="MAPPED"
    )
    batch = (
        EvidenceBatch(dividends=(source,))
        if operation == "DIVIDENDS"
        else EvidenceBatch(splits=(source,))
    )
    async with configured_adapter(loaded, evidence=batch) as adapter:
        page = (
            await adapter.fetch_dividends(prepared.segment)
            if operation == "DIVIDENDS"
            else await adapter.fetch_splits(prepared.segment)
        )
    save(engine, {**book, "prepared": prepared}, page)
    before = database_snapshot(engine)
    response = await client.get(
        "/api/v1/dividends/observations"
        if operation == "DIVIDENDS"
        else "/api/v1/providers/split-observations"
    )
    assert_private_free(response)
    row = response.json()["items"][0]
    assert row["evidence"]["id"] == str(source.evidence.id)
    assert (
        row["evidence"]["citation"] == source.evidence.citation
        and row["evidence"]["reason"] == source.evidence.reason
    )
    assert (
        row["evidence"]["url"] == source.evidence.url and row["evidence"]["url_withheld"] is False
    )
    assert row["normalized_sha256"] == source.normalized_sha256 and row["source_url"] is None
    assert row["canonical_adoption"] == "NOT_ADOPTED" and row["provenance_version"] == 1
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("suffix", ["?api_key=private-link-canary", "#private-link-canary"])
async def test_citation_query_links_are_withheld_without_rewriting_the_original_assertion_hash(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine, suffix: str
) -> None:
    await create(
        client,
        f"securities/{book['security']['id']}/provider-symbols",
        {"provider_id": "manual", "provider_symbol": "MAPPED", "effective_from": "2020-01-01"},
    )
    provision(book["settings"], "manual")
    loaded = ProviderConfigurationStore(book["settings"]).load("manual")
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(book["security"]["id"]), "manual", WINDOW).segments[0]
    source = cited_dividend(
        "manual",
        security_id=book["security"]["id"],
        evidence=citation("manual", url="https://example.invalid/document" + suffix),
    )
    prepared = prepare_fetch(UUID(book["owner"]["id"]), loaded, segment, "DIVIDENDS")
    async with configured_adapter(loaded, evidence=EvidenceBatch(dividends=(source,))) as adapter:
        page = await adapter.fetch_dividends(segment)
    save(engine, {**book, "prepared": prepared}, page)
    before = database_snapshot(engine)
    response = await client.get("/api/v1/dividends/observations")
    assert_private_free(response)
    row = response.json()["items"][0]
    assert row["evidence"]["url"] is None and row["evidence"]["url_withheld"] is True
    assert row["evidence"]["id"] == str(source.evidence.id)
    assert row["normalized_sha256"] == source.normalized_sha256
    assert "private-link-canary" not in response.text
    assert database_snapshot(engine) == before
