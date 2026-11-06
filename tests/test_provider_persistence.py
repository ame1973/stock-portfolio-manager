"""Real HTTP/SQLite source capture stays append-only and never adopts financial facts."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

import spm_api.providers.persistence as storage
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.master_models import ProviderId
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import DividendObservation, FetchState
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.manual import EvidenceBatch
from spm_api.providers.persistence import PreparedFetch, capture_page, prepare_fetch
from spm_api.providers.runtime import configured_adapter
from spm_api.providers.symbols import FetchWindow, resolve_symbol
from tests.helpers import create, database_snapshot, initialize
from tests.test_provider_configuration import KEY, policy, provision
from tests.test_provider_manual import dividend as cited_dividend

pytestmark = pytest.mark.anyio
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")
START = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
FINISH = START + timedelta(milliseconds=12)


@pytest.fixture
async def book(
    client: httpx.AsyncClient, engine: Engine, settings: Settings, tmp_path: Path
) -> dict[str, Any]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic persisted evidence"})
    broker = await create(client, "brokers", {"name": "Synthetic evidence broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic evidence cash",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-01",
            "opening_fx_rate_hkd": "7.8",
        },
    )
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "HKG",
            "display_ticker": "0005",
            "name": "Synthetic persisted security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        f"securities/{security['id']}/provider-symbols",
        {
            "provider_id": "eodhd",
            "provider_symbol": "0005.HK",
            "effective_from": "2020-01-01",
        },
    )
    await create(
        client,
        "trades",
        {
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "security_id": security["id"],
            "side": "BUY",
            "trade_date": "2026-01-02",
            "quantity": "10",
            "price": "10",
            "currency": "USD",
            "fx_rate_hkd": "7.8",
        },
    )
    local = settings.model_copy(update={"provider_secret_dir": tmp_path / "provider-keys"})
    path, key = provision(local)
    store = ProviderConfigurationStore(local)
    loaded = store.load("eodhd")
    assert loaded.error_code is None
    with unit_of_work(engine) as con:
        resolution = resolve_symbol(con, UUID(security["id"]), "eodhd", WINDOW)
    prepared = prepare_fetch(UUID(owner["id"]), loaded, resolution.segments[0], "DIVIDENDS")
    return {
        "owner": owner,
        "security": security,
        "account": account,
        "prepared": prepared,
        "store": store,
        "profile": path,
        "key": key,
        "settings": local,
    }


async def fetched(book: dict[str, Any], *, payload: Any = None, status: int = 200) -> Any:
    prepared: PreparedFetch = book["prepared"]
    if payload is None:
        payload = [
            {
                "date": "2026-01-08",
                "unadjustedValue": "0.9",
                "currency": "USD",
                "period": "Quarterly",
            }
        ]
    async with configured_adapter(
        prepared.configuration,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(json.dumps(payload).encode()),
            )
        ),
    ) as adapter:
        return (
            await adapter.fetch_dividends(prepared.segment)
            if prepared.operation == "DIVIDENDS"
            else await adapter.fetch_splits(prepared.segment)
        )


def save(
    engine: Engine, book: dict[str, Any], page: Any, *, run: UUID | None = None, **changes: Any
) -> Any:
    arguments = {
        "run_id": run or uuid4(),
        "started_at": START,
        "finished_at": FINISH,
        "duration_ms": 12,
        **changes,
    }
    with unit_of_work(engine, write=True) as con:
        return capture_page(con, book["prepared"], page, **arguments)


def assert_financial_unchanged(before: Any, after: Any) -> None:
    allowed = {
        "provider_fetch_contexts",
        "provider_runs",
        "dividend_observations",
        "split_observations",
        "provider_observation_sightings",
        "audit_events",
    }
    assert {name: rows for name, rows in before.items() if name not in allowed} == {
        name: rows for name, rows in after.items() if name not in allowed
    }


async def test_actual_adapter_capture_repeat_sighting_and_same_attempt_replay(
    book: dict[str, Any], engine: Engine
) -> None:
    before = database_snapshot(engine)
    page = await fetched(book)
    run = uuid4()
    first = save(engine, book, page, run=run)
    assert first.observations_new == 1 and len(first.observation_ids) == 1
    committed = database_snapshot(engine)
    assert save(engine, book, page, run=run) == first
    assert database_snapshot(engine) == committed
    second = save(engine, book, await fetched(book))
    assert second.observations_new == 0 and second.observation_ids == first.observation_ids
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql("SELECT * FROM dividend_observations").mappings().one()
        assert row["dps_e8"] == 90000000 and row["currency"] == "USD"
        assert row["adjustment_basis"] == "UNKNOWN" and row["pay_date"] is None
        assert row["provider_run_id"] == str(first.run_id) and row["raw_permitted"] == 0
        restored = DividendObservation.model_validate_json(row["normalized_json"])
        assert restored.dps == "0.90000000" and restored.source_period == "QUARTERLY"
        assert restored.normalized_sha256 == row["normalized_sha256"]
        assert con.exec_driver_sql("SELECT count(*) FROM provider_fetch_contexts").scalar_one() == 1
        assert (
            con.exec_driver_sql("SELECT count(*) FROM provider_observation_sightings").scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM provider_runs "
                "WHERE coverage_start IS NOT NULL OR coverage_end IS NOT NULL"
            ).scalar_one()
            == 0
        )
        assert con.exec_driver_sql("SELECT count(*) FROM provider_status").scalar_one() == 0
        metadata = con.exec_driver_sql(
            "SELECT profile_json FROM provider_fetch_contexts"
        ).scalar_one()
        assert KEY.decode() not in metadata and '"credential"' not in metadata
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='PROVIDER_EVIDENCE_CAPTURE'"
            ).scalar_one()
            == 2
        )
    assert_financial_unchanged(before, database_snapshot(engine))


async def test_changed_terms_append_and_same_date_regular_special_remain_separate(
    book: dict[str, Any], engine: Engine
) -> None:
    original = await fetched(book)
    old = original.rows[0].model_dump(mode="json")
    rows = tuple(
        DividendObservation.model_validate({**old, **changes})
        for changes in [
            {"provider_event_id": "event-1", "event_type": "REGULAR", "dps": "1"},
            {"provider_event_id": "event-1", "event_type": "REGULAR", "dps": "1.1"},
            {"provider_event_id": "event-2", "event_type": "SPECIAL", "dps": "1"},
        ]
    )
    page = FetchPage[DividendObservation].model_validate(
        {**original.model_dump(mode="json"), "rows": rows}
    )
    before = database_snapshot(engine)
    receipt = save(engine, book, page)
    assert receipt.observations_new == 3 and len(set(receipt.observation_ids)) == 3
    with unit_of_work(engine) as con:
        assert [
            tuple(row)
            for row in con.exec_driver_sql(
                "SELECT provider_event_id,event_type,dps_e8 "
                "FROM dividend_observations ORDER BY rowid"
            )
        ] == [
            ("event-1", "REGULAR", 100000000),
            ("event-1", "REGULAR", 110000000),
            ("event-2", "SPECIAL", 100000000),
        ]
    assert_financial_unchanged(before, database_snapshot(engine))


@pytest.mark.parametrize(
    "amount,expected", [(None, None), ("0", 0), ("92233720368.54775807", 9223372036854775807)]
)
async def test_nullable_zero_and_maximum_dps_are_exact_in_sql(
    book: dict[str, Any], engine: Engine, amount: Any, expected: Any
) -> None:
    page = await fetched(
        book, payload=[{"date": "2026-01-08", "unadjustedValue": amount, "currency": None}]
    )
    save(engine, book, page)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT dps_e8,currency,pay_date,normalized_json FROM dividend_observations"
        ).one()
    assert row[0] == expected and row[1] is row[2] is None
    assert DividendObservation.model_validate_json(row[3]).dps == (
        None if amount is None else "0.00000000" if amount == "0" else amount
    )


@pytest.mark.parametrize(
    "ratio,expected",
    [
        ("3/2", (3, 2)),
        ("1/3", (1, 3)),
        ("9223372036854775807/9223372036854775806", (9223372036854775807, 9223372036854775806)),
    ],
)
async def test_split_evidence_keeps_every_ratio_digit_without_adopting_shares(
    book: dict[str, Any], engine: Engine, ratio: str, expected: tuple[int, int]
) -> None:
    prepared = book["prepared"]
    book["prepared"] = prepare_fetch(
        prepared.owner_id, prepared.configuration, prepared.segment, "SPLITS"
    )
    page = await fetched(book, payload=[{"date": "2026-01-08", "split": ratio}])
    before = database_snapshot(engine)
    receipt = save(engine, book, page)
    assert receipt.observations_new == 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT ratio_num,ratio_den,effect_scope,normalized_json FROM split_observations"
        ).one()
    assert tuple(row[:2]) == expected and row[2] == "PROVIDER_ADJUSTMENT_ONLY"
    assert json.loads(row[3])["ratio_numerator"] == str(expected[0])
    assert_financial_unchanged(before, database_snapshot(engine))


@pytest.mark.parametrize(
    "status,state",
    [
        (200, FetchState.NO_DATA),
        (401, FetchState.AUTH_ERROR),
        (429, FetchState.RATE_LIMITED),
        (503, FetchState.TRANSIENT_ERROR),
    ],
)
async def test_empty_and_failed_fetch_records_preserve_distinct_states_not_zero_income(
    book: dict[str, Any], engine: Engine, status: int, state: FetchState
) -> None:
    page = await fetched(book, payload=[], status=status)
    before = database_snapshot(engine)
    receipt = save(engine, book, page)
    assert receipt.observation_ids == () and receipt.observations_new == 0
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT status FROM provider_runs").scalar_one() == state.value
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 0
    assert_financial_unchanged(before, database_snapshot(engine))


async def test_partial_page_retains_valid_evidence_and_never_claims_complete(
    book: dict[str, Any], engine: Engine
) -> None:
    page = await fetched(
        book, payload=[{"date": "2026-01-08", "value": "0.9"}, {"date": "invalid", "value": "0.4"}]
    )
    receipt = save(engine, book, page)
    assert receipt.observations_new == 1
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT parse_complete,issue_count,status FROM provider_runs"
        ).one()
    assert tuple(row) == (0, 1, "OK_WITH_DATA")


async def test_alias_cas_blocks_new_capture_but_historical_retry_still_replays(
    book: dict[str, Any], engine: Engine
) -> None:
    page = await fetched(book)
    run = uuid4()
    first = save(engine, book, page, run=run)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE provider_symbols SET version=version+1 WHERE id=?",
            (str(book["prepared"].segment.alias.id),),
        )
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_ALIAS_CHANGED"):
        save(engine, book, page)
    assert database_snapshot(engine) == before
    assert save(engine, book, page, run=run) == first and database_snapshot(engine) == before


async def test_same_attempt_changed_payload_or_context_rolls_back_every_value(
    book: dict[str, Any], engine: Engine
) -> None:
    page = await fetched(book)
    run = uuid4()
    save(engine, book, page, run=run)
    changed = await fetched(book, payload=[{"date": "2026-01-08", "value": "0.8"}])
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_ATTEMPT_CONFLICT"):
        save(engine, book, changed, run=run)
    assert database_snapshot(engine) == before
    current = book["prepared"]
    book["profile"].write_text(json.dumps(policy(version=2)))
    loaded = book["store"].load("eodhd")
    book["prepared"] = prepare_fetch(
        current.owner_id, loaded, current.segment, "DIVIDENDS", context_id=current.id
    )
    with pytest.raises(ValueError, match="PROVIDER_CONTEXT_CONFLICT"):
        save(engine, book, page)
    assert database_snapshot(engine) == before


async def test_late_audit_failure_rolls_back_context_run_observations_and_sightings(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await fetched(book)
    before = database_snapshot(engine)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Injected capture failure")

    monkeypatch.setattr(storage, "audit", fail)
    with pytest.raises(RuntimeError, match="Injected capture failure"):
        save(engine, book, page)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "credential",
        "query",
        "foreign-url",
        "empty-confirmed",
        "wrong-window",
        "no-attempt",
        "duplicate",
    ],
)
async def test_untrusted_scope_privacy_and_empty_claims_write_nothing(
    book: dict[str, Any], engine: Engine, problem: str
) -> None:
    page = await fetched(book)
    data = page.model_dump(mode="json")
    if problem == "credential":
        data["rows"][0]["provider_event_id"] = KEY.decode()
    elif problem == "query":
        data["source_url"] += "?api_token=" + KEY.decode()
    elif problem == "foreign-url":
        data["source_url"] = "https://foreign.invalid/data"
    elif problem == "empty-confirmed":
        data.update(state="EMPTY_CONFIRMED", rows=[], complete=False)
    elif problem == "wrong-window":
        data["window"] = {"start": "2025-01-01", "end": "2025-01-31"}
    elif problem == "no-attempt":
        data.update(source_url=None, http_status=None, raw_sha256=None)
    else:
        data["rows"] *= 2
    invalid = FetchPage[DividendObservation].model_validate(data)
    before = database_snapshot(engine)
    with pytest.raises(ValueError):
        save(engine, book, invalid)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("credential", ['synthetic-"quoted-key', r"synthetic-\backslash-key"])
@pytest.mark.parametrize("location", ["profile", "observation"])
async def test_json_escaped_credentials_are_not_persisted_in_metadata_or_observations(
    book: dict[str, Any], engine: Engine, credential: str, location: str
) -> None:
    book["key"].write_text(credential)
    if location == "profile":
        book["profile"].write_text(json.dumps(policy(terms_reference=credential)))
    loaded = book["store"].load("eodhd")
    assert loaded.error_code is None
    observed: PreparedFetch = book["prepared"]
    before = database_snapshot(engine)
    if location == "profile":
        with pytest.raises(ValueError, match="CREDENTIAL_IN_EVIDENCE"):
            prepare_fetch(observed.owner_id, loaded, observed.segment, "DIVIDENDS")
    else:
        book["prepared"] = prepare_fetch(observed.owner_id, loaded, observed.segment, "DIVIDENDS")
        page = await fetched(book)
        payload = page.model_dump(mode="json")
        payload["rows"][0]["provider_event_id"] = credential
        with pytest.raises(ValueError, match="CREDENTIAL_IN_EVIDENCE"):
            save(engine, book, FetchPage[DividendObservation].model_validate(payload))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"duration_ms": True},
        {"duration_ms": -1},
        {"started_at": datetime(2026, 1, 10)},
        {"finished_at": START - timedelta(seconds=1)},
    ],
)
async def test_metadata_times_and_duration_are_explicit_not_invented(
    book: dict[str, Any], engine: Engine, change: dict[str, Any]
) -> None:
    page = await fetched(book)
    before = database_snapshot(engine)
    with pytest.raises(ValueError):
        save(engine, book, page, **change)
    assert database_snapshot(engine) == before


async def test_read_transaction_cannot_capture_and_financial_values_still_reject_floats(
    book: dict[str, Any], engine: Engine
) -> None:
    page = await fetched(book)
    with (
        unit_of_work(engine) as con,
        pytest.raises(ValueError, match="WRITE_UNIT_OF_WORK_REQUIRED"),
    ):
        capture_page(
            con,
            book["prepared"],
            page,
            run_id=uuid4(),
            started_at=START,
            finished_at=FINISH,
            duration_ms=12,
        )
    value = page.rows[0].model_dump(mode="json")
    with pytest.raises(ValidationError):
        DividendObservation.model_validate({**value, "dps": 0.9})


async def test_legacy_unresolved_hash_cannot_be_silently_linked_or_rewritten(
    book: dict[str, Any], engine: Engine
) -> None:
    page = await fetched(book)
    save(engine, book, page)
    # Reproduce retained pre-0008 metadata, then restore immutability before exercising the writer.
    with unit_of_work(engine, write=True) as con:
        trigger = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name='trg_dividend_observation_no_update'"
        ).scalar_one()
        con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
        con.exec_driver_sql(
            "UPDATE dividend_observations SET provenance_version=0,normalized_json=NULL"
        )
        con.exec_driver_sql(trigger)
    before = database_snapshot(engine)
    with pytest.raises(ValueError, match="PROVIDER_OBSERVATION_HASH_CONFLICT"):
        save(engine, book, page)
    assert database_snapshot(engine) == before


async def test_manual_citation_survives_capture_without_claiming_a_fetched_official_document(
    book: dict[str, Any], engine: Engine, client: httpx.AsyncClient
) -> None:
    provider: ProviderId = "hkex_evidence"
    await create(
        client,
        f"securities/{book['security']['id']}/provider-symbols",
        {
            "provider_id": provider,
            "provider_symbol": "0005",
            "effective_from": "2020-01-01",
        },
    )
    provision(book["settings"], provider)
    loaded = book["store"].load(provider)
    with unit_of_work(engine) as con:
        segment = resolve_symbol(con, UUID(book["security"]["id"]), provider, WINDOW).segments[0]
    book["prepared"] = prepare_fetch(UUID(book["owner"]["id"]), loaded, segment, "DIVIDENDS")
    evidence = cited_dividend(
        provider,
        security_id=book["security"]["id"],
        provider_symbol="0005",
        adjustment_basis="AS_DECLARED",
    )
    async with configured_adapter(loaded, evidence=EvidenceBatch(dividends=(evidence,))) as adapter:
        page = await adapter.fetch_dividends(segment)
    before = database_snapshot(engine)
    save(engine, book, page)
    with unit_of_work(engine) as con:
        normalized = con.exec_driver_sql(
            "SELECT normalized_json FROM dividend_observations"
        ).scalar_one()
        record = con.exec_driver_sql(
            "SELECT attempted,http_status,raw_sha256,source_url FROM provider_runs"
        ).one()
    assert json.loads(normalized)["evidence"] == evidence.evidence.model_dump(mode="json")
    assert tuple(record) == (0, None, None, None)
    assert "DOCUMENT_NOT_FETCHED" in page.warnings
    assert_financial_unchanged(before, database_snapshot(engine))
    invalid = page.model_dump(mode="json")
    invalid["rows"][0]["evidence"]["url"] = "https://foreign.invalid/not-official.pdf"
    before = database_snapshot(engine)
    with pytest.raises(ValidationError):
        save(engine, book, FetchPage[DividendObservation].model_validate(invalid))
    assert database_snapshot(engine) == before
