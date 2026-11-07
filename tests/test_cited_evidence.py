"""Cited source assertions retain exact terms without financial adoption."""

from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, text

import spm_api.providers.cited as service
from spm_api.audit import now
from spm_api.cited_models import CitedDividendCreate, CitedSourceInput, CitedSplitCreate
from spm_api.config import Settings
from spm_api.db import unit_of_work
from spm_api.providers.base import DividendObservation
from spm_api.providers.cited import CitedEvidenceError
from spm_api.providers.manual import EvidenceProvider
from spm_api.providers.profiles import latest
from spm_api.providers.profiles import save as save_profile
from spm_api.providers.selection import OwnerConfigurationStore
from tests.helpers import create, database_snapshot, initialize, version_headers

pytestmark = pytest.mark.anyio
# Mypy does not yet model the union type form accepted by Pydantic's adapter.
BODY: TypeAdapter[CitedDividendCreate | CitedSplitCreate] = TypeAdapter(cast(Any, CitedSourceInput))
REASON = "Explicit synthetic citation of source terms, not financial verification."


@pytest.fixture
async def book(client: httpx.AsyncClient, engine: Engine, settings: Settings) -> dict[str, Any]:
    owner = await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Cited source isolation"})
    broker = await create(client, "brokers", {"name": "Cited source broker"})
    account = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Cited source cash",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-01",
            "opening_fx_rate_hkd": "7.7",
        },
    )
    securities = []
    for exchange, ticker in (("HKG", "0005"), ("NYSE", "BRK.B")):
        securities.append(
            await create(
                client,
                "securities",
                {
                    "exchange_code": exchange,
                    "display_ticker": ticker,
                    "name": f"Cited {ticker}",
                    "quote_currency": "USD",
                    "identifier_effective_from": "2020-01-01",
                },
            )
        )
    for provider, stock in (
        ("manual", securities[0]),
        ("hkex_evidence", securities[0]),
        ("sec_evidence", securities[1]),
    ):
        await create(
            client,
            f"securities/{stock['id']}/provider-symbols",
            {
                "provider_id": provider,
                "provider_symbol": f"{stock['display_ticker']}.CITED",
                "effective_from": "2020-01-01",
            },
        )
        declaration = {
            "provider_id": provider,
            "enabled": False,
            "terms_acknowledged": True,
            "terms_reference": "Synthetic non-network source citation permission",
            "terms_reason": REASON,
            "requests_per_minute": None,
            "requests_per_day": None,
            "raw_payload_storage": False,
        }
        await create(client, "provider-profiles", {"policy": declaration, "reason": REASON})
        response = await client.patch(
            f"/api/v1/provider-profiles/{provider}",
            headers=version_headers(client, 1),
            json={
                "policy": {**declaration, "enabled": True},
                "clear_credential": False,
                "reason": REASON,
            },
        )
        assert response.status_code == 200
    await create(
        client,
        "trades",
        {
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "security_id": securities[0]["id"],
            "side": "BUY",
            "trade_date": "2026-01-02",
            "quantity": "10",
            "price": "10",
            "currency": "USD",
            "fx_rate_hkd": "7.8",
        },
    )
    return {
        "owner": UUID(owner["id"]),
        "hk": securities[0],
        "us": securities[1],
        "settings": settings,
        "account": account,
    }


def payload(
    book: dict[str, Any], provider: EvidenceProvider = "manual", *, split: bool = False
) -> dict[str, Any]:
    stock = book["us"] if provider == "sec_evidence" else book["hk"]
    url = {
        "manual": None,
        "hkex_evidence": "https://www.hkexnews.hk/synthetic-reference.pdf",
        "sec_evidence": "https://www.sec.gov/Archives/synthetic-reference.htm",
    }[provider]
    return {
        "operation": "SPLITS" if split else "DIVIDENDS",
        "provider_id": provider,
        "security_id": stock["id"],
        "provider_symbol": f"{stock['display_ticker']}.CITED",
        "citation": {
            "citation": "Synthetic document page 3, not fetched",
            "reason": REASON,
            "url": url,
        },
        **(
            {"effective_date": "2026-01-08", "ratio_numerator": "1", "ratio_denominator": "3"}
            if split
            else {"ex_date": "2026-01-08", "dps": "0.1", "currency": None, "source_currency": "EUR"}
        ),
    }


def save(engine: Engine, actor: UUID, prepared: service.PreparedCitedEvidence) -> Any:
    with unit_of_work(engine, write=True) as con:
        return service.save(con, actor, prepared, request_id=uuid4())


def unchanged_except_sources(before: Any, after: Any) -> None:
    changed = {
        "provider_fetch_contexts",
        "provider_runs",
        "dividend_observations",
        "split_observations",
        "provider_observation_sightings",
        "audit_events",
    }
    assert {key: rows for key, rows in before.items() if key not in changed} == {
        key: rows for key, rows in after.items() if key not in changed
    }


@pytest.mark.parametrize("provider", ["manual", "hkex_evidence", "sec_evidence"])
@pytest.mark.parametrize("split", [False, True])
async def test_exact_owned_capture_has_no_http_grant_or_financial_adoption(
    book: dict[str, Any],
    engine: Engine,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    provider: EvidenceProvider,
    split: bool,
) -> None:
    body = BODY.validate_python(payload(book, provider, split=split))
    before = database_snapshot(engine)
    with monkeypatch.context() as local:

        def no_http(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Cited evidence must never send HTTP")

        local.setattr(httpx.AsyncClient, "request", no_http)
        prepared = await service.prepare(book["settings"], engine, book["owner"], body)
        local.setattr(OwnerConfigurationStore, "load", no_http)
        local.setattr(OwnerConfigurationStore, "unchanged", no_http)
        result = save(engine, book["owner"], prepared)
    assert result.scope == "SOURCE_ONLY" and result.canonical_adoption == "NOT_ADOPTED"
    assert result.document_verification == "NOT_VERIFIED"
    after = database_snapshot(engine)
    unchanged_except_sources(before, after)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql("SELECT * FROM provider_runs").mappings().one()
        assert row["owner_id"] == str(book["owner"]) and row["job_id"] is None
        assert (
            row["attempted"] == 0
            and row["http_status"] is None
            and row["raw_sha256"] is None
            and row["source_url"] is None
        )
        assert row["requested_start"] == row["requested_end"] == "2026-01-08"
        authored = (
            con.exec_driver_sql(
                "SELECT * FROM audit_events WHERE action='USER_CITED_SOURCE_ASSERTED'"
            )
            .mappings()
            .one()
        )
        assert (
            authored["owner_id"] == str(book["owner"])
            and authored["entity_id"] == str(result.citation_id)
            and authored["request_id"] is not None
        )
        context = con.exec_driver_sql(
            "SELECT profile_json,owner_id FROM provider_fetch_contexts"
        ).one()
        assert '"configuration_source":"MANAGED_PROFILE"' in context[0] and context[1] == str(
            book["owner"]
        )
    path = "providers/split-observations" if split else "dividends/observations"
    response = await client.get(f"/api/v1/{path}")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    record = response.json()["items"][0]
    assert record["id"] == str(result.observation_id) and record["evidence"]["id"] == str(
        result.citation_id
    )
    assert "owner_id" not in record and "context_id" not in record
    if split:
        assert (
            record["ratio_numerator"] == "1"
            and record["ratio_denominator"] == "3"
            and record["effect_scope"] == "PROVIDER_ADJUSTMENT_ONLY"
        )
    else:
        assert (
            record["dps"] == "0.10000000"
            and record["currency"] is None
            and record["source_currency"] == "EUR"
        )
        assert record["event_type"] == "UNKNOWN" and record["adjustment_basis"] == "UNKNOWN"
        assert record["incomplete_reasons"] == ["MISSING_CURRENCY", "UNRESOLVED_ADJUSTMENT"]


@pytest.mark.parametrize("term", [None, "0", "92233720368.54775807"])
async def test_nullable_known_zero_and_int64_dps_never_use_quote_currency(
    book: dict[str, Any], engine: Engine, term: str | None
) -> None:
    body = BODY.validate_python({**payload(book), "dps": term})
    prepared = await service.prepare(book["settings"], engine, book["owner"], body)
    result = save(engine, book["owner"], prepared)
    with unit_of_work(engine) as con:
        row = (
            con.execute(
                text("SELECT * FROM dividend_observations WHERE id=:id"),
                {"id": str(result.observation_id)},
            )
            .mappings()
            .one()
        )
        assert row["dps_e8"] == (
            None if term is None else 0 if term == "0" else 9223372036854775807
        )
        assert row["currency"] is None and row["adjustment_basis"] == "UNKNOWN"


async def test_maximum_and_small_rational_terms_remain_exact_provider_only(
    book: dict[str, Any], engine: Engine
) -> None:
    for numerator, denominator in (("9223372036854775807", "9223372036854775806"), ("1", "3")):
        body = BODY.validate_python(
            {
                **payload(book, split=True),
                "ratio_numerator": numerator,
                "ratio_denominator": denominator,
            }
        )
        prepared = await service.prepare(book["settings"], engine, book["owner"], body)
        result = save(engine, book["owner"], prepared)
        with unit_of_work(engine) as con:
            assert con.execute(
                text(
                    "SELECT ratio_num,ratio_den,effect_scope FROM split_observations WHERE id=:id"
                ),
                {"id": str(result.observation_id)},
            ).one() == (int(numerator), int(denominator), "PROVIDER_ADJUSTMENT_ONLY")


async def test_as_declared_is_only_an_assertion_not_canonical_verification(
    book: dict[str, Any], engine: Engine
) -> None:
    body = BODY.validate_python(
        {
            **payload(book),
            "adjustment_basis": "AS_DECLARED",
            "event_type": "SPECIAL",
            "currency": "USD",
        }
    )
    before = database_snapshot(engine)
    prepared = await service.prepare(book["settings"], engine, book["owner"], body)
    save(engine, book["owner"], prepared)
    unchanged_except_sources(before, database_snapshot(engine))
    assert isinstance(prepared.page.rows[0], DividendObservation)
    assert prepared.page.rows[0].incomplete_reasons == frozenset()
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_events").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://www.hkexnews.hk/test.pdf",
        "https://hkexnews.hk.evil.example/test.pdf",
        "https://www.sec.gov/test.htm",
        "https://www.hkexnews.hk:444/test.pdf",
    ],
)
async def test_official_attribution_rejects_wrong_host_or_tls_before_private_loading(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    data = payload(book, "hkex_evidence")
    data["citation"]["url"] = url
    body = BODY.validate_python(data)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("No private loading for invalid attribution")

    monkeypatch.setattr(OwnerConfigurationStore, "load", forbidden)
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^INVALID_CITED_ASSERTION$"):
        await service.prepare(book["settings"], engine, book["owner"], body)
    assert database_snapshot(engine) == before


async def test_sensitive_citation_url_is_withheld_in_reads_without_rehashing(
    book: dict[str, Any], engine: Engine, client: httpx.AsyncClient
) -> None:
    data = payload(book)
    data["citation"]["url"] = "https://example.test/document?id=synthetic-document#page3"
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(data)
    )
    save(engine, book["owner"], prepared)
    original_hash = prepared.page.rows[0].normalized_sha256
    response = await client.get("/api/v1/dividends/observations")
    record = response.json()["items"][0]
    assert record["evidence"]["url_withheld"] and record["evidence"]["url"] is None
    assert record["normalized_sha256"] == original_hash
    assert prepared.page.rows[0].evidence is not None
    assert prepared.page.rows[0].evidence.url == data["citation"]["url"]


@pytest.mark.parametrize("change", ["symbol", "security", "market", "date"])
async def test_no_guessed_dated_identity_or_market(
    book: dict[str, Any], engine: Engine, change: str
) -> None:
    data = payload(book, "hkex_evidence")
    if change == "symbol":
        data["provider_symbol"] = "5.CITED"
    if change == "security":
        data["security_id"] = str(uuid4())
    if change == "market":
        data["security_id"] = book["us"]["id"]
    if change == "date":
        data["ex_date"] = "2019-01-01"
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^CITED_SOURCE_IDENTITY_UNRESOLVED$"):
        await service.prepare(book["settings"], engine, book["owner"], BODY.validate_python(data))
    assert database_snapshot(engine) == before


async def test_disabled_current_owned_policy_never_uses_operator_fallback(
    book: dict[str, Any], engine: Engine
) -> None:
    with unit_of_work(engine, write=True) as con:
        current = latest(con, book["owner"], "manual")
        assert current is not None
        save_profile(
            con,
            book["owner"],
            current.policy.model_copy(update={"enabled": False}),
            expected_version=current.version,
            at=now(),
            reason=REASON,
            request_id=uuid4(),
        )
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^CITED_SOURCE_UNCONFIGURED$"):
        await service.prepare(
            book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["profile", "alias", "actor"])
async def test_writer_fences_every_new_assertion_after_preparation(
    book: dict[str, Any], engine: Engine, change: str
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    with unit_of_work(engine, write=True) as con:
        if change == "profile":
            current = latest(con, book["owner"], "manual")
            assert current is not None
            save_profile(
                con,
                book["owner"],
                current.policy.model_copy(update={"enabled": False}),
                expected_version=current.version,
                at=now(),
                reason=REASON,
                request_id=uuid4(),
            )
        if change == "alias":
            con.exec_driver_sql(
                "UPDATE provider_symbols SET version=version+1 WHERE provider_id='manual'"
            )
        if change == "actor":
            con.execute(
                text("UPDATE owners SET is_active=0 WHERE id=:id"), {"id": str(book["owner"])}
            )
    before = database_snapshot(engine)
    with pytest.raises(ValueError):
        save(engine, book["owner"], prepared)
    assert database_snapshot(engine) == before


async def test_committed_replay_after_policy_and_alias_change_is_zero_write(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    original = save(engine, book["owner"], prepared)
    with unit_of_work(engine, write=True) as con:
        current = latest(con, book["owner"], "manual")
        assert current is not None
        save_profile(
            con,
            book["owner"],
            current.policy.model_copy(update={"enabled": False}),
            expected_version=current.version,
            at=now(),
            reason=REASON,
            request_id=uuid4(),
        )
        con.exec_driver_sql(
            "UPDATE provider_symbols SET version=version+1 WHERE provider_id='manual'"
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("No private loads or repeated authorship audit on committed replay")

    monkeypatch.setattr(OwnerConfigurationStore, "load", forbidden)
    monkeypatch.setattr(service, "audit", forbidden)
    before = database_snapshot(engine)
    assert save(engine, book["owner"], prepared) == original
    assert database_snapshot(engine) == before


async def test_second_authorship_audit_failure_rolls_back_every_table_and_sequence(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Synthetic late authorship failure")

    monkeypatch.setattr(service, "audit", fail)
    before = database_snapshot(engine)
    with pytest.raises(RuntimeError, match="Synthetic late authorship"):
        save(engine, book["owner"], prepared)
    assert database_snapshot(engine) == before


async def test_another_actor_or_borrowed_unscoped_configuration_cannot_write(
    book: dict[str, Any], engine: Engine
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^CITED_ACTOR_CHANGED$"):
        save(engine, uuid4(), prepared)
    unscoped = replace(
        prepared,
        fetch=replace(
            prepared.fetch,
            configuration=replace(prepared.fetch.configuration, selection_owner_id=None),
        ),
    )
    with pytest.raises(CitedEvidenceError, match="^CITED_ACTOR_CHANGED$"):
        save(engine, book["owner"], unscoped)
    assert database_snapshot(engine) == before


async def test_changed_prepared_terms_are_not_a_grant_for_another_source_assertion(
    book: dict[str, Any], engine: Engine
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    altered = replace(
        prepared,
        page=prepared.page.model_copy(
            update={"rows": (prepared.page.rows[0].model_copy(update={"dps": "0.20000000"}),)}
        ),
    )
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^CITED_ASSERTION_CHANGED$"):
        save(engine, book["owner"], altered)
    assert database_snapshot(engine) == before


async def test_independent_citations_append_instead_of_overwriting_old_assertions(
    book: dict[str, Any], engine: Engine
) -> None:
    body = BODY.validate_python(payload(book))
    first = await service.prepare(book["settings"], engine, book["owner"], body)
    second = await service.prepare(book["settings"], engine, book["owner"], body)
    one, two = save(engine, book["owner"], first), save(engine, book["owner"], second)
    assert one.citation_id != two.citation_id and one.observation_id != two.observation_id
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_observations").scalar_one() == 2
        assert (
            con.exec_driver_sql("SELECT count(*) FROM provider_observation_sightings").scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "field",
    [
        "owner_id",
        "provider_event_id",
        "normalized_sha256",
        "scope",
        "http_status",
        "raw_sha256",
        "cash_amount",
        "fx_rate_hkd",
    ],
)
def test_request_cannot_supply_trusted_actor_provenance_or_financial_fields(field: str) -> None:
    data = {
        "operation": "DIVIDENDS",
        "provider_id": "manual",
        "security_id": str(uuid4()),
        "provider_symbol": "0005.CITED",
        "ex_date": "2026-01-08",
        "citation": {"citation": "Local document", "reason": REASON},
        field: "untrusted",
    }
    with pytest.raises(ValidationError):
        BODY.validate_python(data)


@pytest.mark.parametrize(
    "amount", [0.1, True, "-1", "NaN", "Infinity", "1e-8", "0.123456789", "92233720368.54775808"]
)
def test_request_financial_amounts_are_exact_strings_only(amount: Any) -> None:
    with pytest.raises(ValidationError):
        CitedDividendCreate(
            operation="DIVIDENDS",
            provider_id="manual",
            security_id=uuid4(),
            provider_symbol="0005.CITED",
            ex_date="2026-01-08",
            dps=amount,
            citation={"citation": "Local document", "reason": REASON},
        )


@pytest.mark.parametrize("term", [0, -1, True, 1.5, "0", "1e3", "9223372036854775808"])
def test_request_rational_terms_are_positive_exact_int64(term: Any) -> None:
    with pytest.raises(ValidationError):
        CitedSplitCreate(
            operation="SPLITS",
            provider_id="manual",
            security_id=uuid4(),
            provider_symbol="0005.CITED",
            effective_date="2026-01-08",
            ratio_numerator=term,
            ratio_denominator="3",
            citation={"citation": "Local document", "reason": REASON},
        )


@pytest.mark.parametrize(
    "change",
    [
        "missing operation",
        "vendor",
        "citation id",
        "citation controls",
        "reason NUL",
        "missing date",
    ],
)
def test_request_requires_explicit_cited_scope_without_client_citation_identity(
    change: str,
) -> None:
    data: dict[str, Any] = {
        "operation": "DIVIDENDS",
        "provider_id": "manual",
        "security_id": str(uuid4()),
        "provider_symbol": "0005.CITED",
        "ex_date": "2026-01-08",
        "citation": {"citation": "Local document", "reason": REASON},
    }
    if change == "missing operation":
        del data["operation"]
    if change == "vendor":
        data["provider_id"] = "eodhd"
    if change == "citation id":
        data["citation"]["id"] = str(uuid4())
    if change == "citation controls":
        data["citation"]["citation"] = "Document\0page"
    if change == "reason NUL":
        data["citation"]["reason"] = "Long synthetic\0citation reason"
    if change == "missing date":
        del data["ex_date"]
    with pytest.raises(ValidationError):
        BODY.validate_python(data)


async def test_prepared_context_cannot_broaden_the_explicit_event_day(
    book: dict[str, Any], engine: Engine
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    broader = prepared.fetch.segment.window.model_copy(
        update={"start": "2026-01-01", "end": "2026-01-31"}
    )
    altered = replace(
        prepared,
        fetch=replace(
            prepared.fetch,
            segment=prepared.fetch.segment.model_copy(update={"window": broader}),
        ),
        page=prepared.page.model_copy(update={"window": broader}),
    )
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^CITED_ASSERTION_CHANGED$"):
        save(engine, book["owner"], altered)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("alteration", ["raw", "invalid copied field"])
async def test_internal_request_revalidation_rejects_bypassed_models_without_private_io(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch, alteration: str
) -> None:
    body: Any = BODY.validate_python(payload(book))
    body = (
        payload(book) if alteration == "raw" else body.model_copy(update={"ex_date": "0000-01-01"})
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid input cannot load private profiles")

    monkeypatch.setattr(OwnerConfigurationStore, "load", forbidden)
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^INVALID_CITED_ASSERTION$"):
        await service.prepare(book["settings"], engine, book["owner"], body)
    assert database_snapshot(engine) == before


async def test_private_postparse_change_cannot_be_captured(
    book: dict[str, Any], engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(OwnerConfigurationStore, "unchanged", lambda *args: False)
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^PROVIDER_CONFIG_CHANGED$"):
        await service.prepare(
            book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
        )
    assert database_snapshot(engine) == before


async def test_invalid_attribution_is_rechecked_in_prepared_envelope(
    book: dict[str, Any], engine: Engine
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    altered = replace(
        prepared, body=prepared.body.model_copy(update={"provider_id": "hkex_evidence"})
    )
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^INVALID_CITED_ASSERTION$"):
        save(engine, book["owner"], altered)
    assert database_snapshot(engine) == before


async def test_writer_is_required_even_with_a_valid_prepared_assertion(
    book: dict[str, Any], engine: Engine
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        pytest.raises(CitedEvidenceError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        service.save(con, book["owner"], prepared, request_id=uuid4())
    assert database_snapshot(engine) == before


async def test_untyped_actor_and_request_identity_fail_with_fixed_codes(
    book: dict[str, Any], engine: Engine
) -> None:
    body = BODY.validate_python(payload(book))
    with pytest.raises(CitedEvidenceError, match="^INVALID_CITED_ACTOR$"):
        await service.prepare(book["settings"], engine, cast(Any, str(book["owner"])), body)
    prepared = await service.prepare(book["settings"], engine, book["owner"], body)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(CitedEvidenceError, match="^INVALID_CITED_ACTOR$"),
    ):
        service.save(con, book["owner"], prepared, request_id=cast(Any, "not-server-identity"))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("alteration", ["incomplete", "warning", "run", "operation"])
async def test_prepared_page_must_still_be_one_complete_explicit_source_assertion(
    book: dict[str, Any], engine: Engine, alteration: str
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    if alteration == "incomplete":
        prepared = replace(prepared, page=prepared.page.model_copy(update={"complete": False}))
    if alteration == "warning":
        prepared = replace(prepared, page=prepared.page.model_copy(update={"warnings": ()}))
    if alteration == "run":
        prepared = replace(prepared, run_id=cast(Any, "not-server-identity"))
    if alteration == "operation":
        prepared = replace(prepared, fetch=replace(prepared.fetch, operation="SPLITS"))
    before = database_snapshot(engine)
    with pytest.raises(CitedEvidenceError, match="^CITED_ASSERTION_CHANGED$"):
        save(engine, book["owner"], prepared)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("day", ["2025-02-29", "0000-01-01", "2026-1-08", "2026-01-08T00:00:00Z"])
def test_event_day_is_an_explicit_calendar_date(day: str) -> None:
    with pytest.raises(ValidationError):
        CitedDividendCreate(
            operation="DIVIDENDS",
            provider_id="manual",
            security_id=uuid4(),
            provider_symbol="0005.CITED",
            ex_date=day,
            citation={"citation": "Local document", "reason": REASON},
        )


async def test_authorship_audit_correlates_the_supplied_server_request_id(
    book: dict[str, Any], engine: Engine
) -> None:
    prepared = await service.prepare(
        book["settings"], engine, book["owner"], BODY.validate_python(payload(book))
    )
    request_id = uuid4()
    with unit_of_work(engine, write=True) as con:
        result = service.save(con, book["owner"], prepared, request_id=request_id)
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT owner_id,entity_id,request_id,reason FROM audit_events "
            "WHERE action='USER_CITED_SOURCE_ASSERTED'"
        ).one()
        assert tuple(row) == (str(book["owner"]), str(result.citation_id), str(request_id), REASON)


def test_citation_bounds_frozen_models_and_hidden_references() -> None:
    body = CitedDividendCreate(
        operation="DIVIDENDS",
        provider_id="manual",
        security_id=uuid4(),
        provider_symbol="0005.CITED",
        ex_date="2026-01-08",
        source_period="QUARTERLY",
        citation={"citation": "Private document reference", "reason": REASON},
    )
    assert body.event_type == "UNKNOWN" and "Private document reference" not in repr(body)
    assert REASON not in repr(body.citation)
    with pytest.raises(ValidationError):
        cast(Any, body).ex_date = "2026-01-09"
    for text_value in ("", "x" * 301):
        with pytest.raises(ValidationError):
            CitedDividendCreate.model_validate(
                {**body.model_dump(), "citation": {"citation": text_value, "reason": REASON}}
            )
