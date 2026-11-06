"""Human evidence profiles retain exact cited assertions without fetching or booking anything."""

import hashlib
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

from spm_api.db import unit_of_work
from spm_api.providers.adapters import FetchPage, ProviderAdapter
from spm_api.providers.base import (
    DividendObservation,
    EvidenceCitation,
    FetchState,
    independent_vendor_origins,
)
from spm_api.providers.http import ProviderAccess, ProviderHTTP
from spm_api.providers.manual import (
    DividendEvidence,
    EvidenceBatch,
    ManualEvidenceAdapter,
    SplitEvidence,
)
from spm_api.providers.symbols import AliasPeriod, AliasSegment, FetchWindow, resolve_symbol
from tests.helpers import create, database_snapshot, initialize

SID = UUID("00000000-0000-4000-8000-000000000001")
OTHER = UUID("00000000-0000-4000-8000-000000000009")
WINDOW = FetchWindow(start="2026-01-01", end="2026-01-31")
URLS = {
    "manual": None,
    "hkex_evidence": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/synthetic.pdf",
    "sec_evidence": "https://www.sec.gov/Archives/edgar/data/0/synthetic.htm",
}


def citation(provider: Any = "manual", **changes: Any) -> dict[str, Any]:
    return {
        "id": "00000000-0000-4000-8000-000000000003",
        "url": URLS[provider],
        "citation": "Synthetic document, page 2, distribution table",
        "reason": "Transcribed the explicit terms from the cited synthetic document.",
        **changes,
    }


def dividend(provider: Any = "manual", **changes: Any) -> DividendEvidence:
    return DividendEvidence.model_validate(
        {
            "provider_id": provider,
            "security_id": SID,
            "provider_symbol": "MAPPED",
            "ex_date": "2026-01-08",
            "dps": "0.3",
            "currency": "USD",
            "evidence": citation(provider),
            **changes,
        }
    )


def split(provider: Any = "manual", **changes: Any) -> SplitEvidence:
    return SplitEvidence.model_validate(
        {
            "provider_id": provider,
            "security_id": SID,
            "provider_symbol": "MAPPED",
            "effective_date": "2026-01-08",
            "ratio_numerator": "3",
            "ratio_denominator": "2",
            "evidence": citation(provider),
            **changes,
        }
    )


def segment(provider: Any = "manual", market: Any = "HK", **changes: Any) -> AliasSegment:
    return AliasSegment(
        alias=AliasPeriod.model_validate(
            {
                "id": "00000000-0000-4000-8000-000000000002",
                "security_id": SID,
                "provider_id": provider,
                "provider_symbol": "MAPPED",
                "effective_from": "2020-01-01",
                "verified": True,
                **changes,
            }
        ),
        market=market,
        window=WINDOW,
    )


def access() -> ProviderAccess:
    return ProviderAccess(enabled=True, terms_acknowledged=True)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider,market", [("manual", "HK"), ("hkex_evidence", "HK"), ("sec_evidence", "US")]
)
async def test_manual_profiles_default_disabled_and_health_never_claims_live_coverage(
    provider: Any, market: Any
) -> None:
    source: ProviderAdapter = ManualEvidenceAdapter(provider)
    health = await source.healthcheck()
    result = await source.fetch_dividends(segment(provider, market))
    factor = await source.fetch_splits(segment(provider, market))
    assert health.state == result.state == factor.state == FetchState.UNCONFIGURED
    assert result.error_code == health.reason == "PROVIDER_DISABLED" and not health.configured
    assert health.scope == "CONFIGURATION_ONLY" and health.live_validation == "NOT_TESTED"
    assert health.last_success_at is health.quota_remaining is None
    enabled = ManualEvidenceAdapter(provider, access())
    configured = await enabled.healthcheck()
    caps = enabled.get_capabilities()
    assert configured.configured and configured.state == FetchState.STALE
    assert configured.reason == "MANUAL_INPUT_ONLY" and caps.access_mode == "MANUAL_EVIDENCE"
    assert caps.operations == ("DIVIDENDS", "SPLITS") and caps.raw_payload_storage is False
    assert caps.coverage_start is None and caps.has_unadjusted_dividend is None
    assert caps.data_rights == "MANUAL_EVIDENCE_ONLY" and caps.live_validation == "NOT_TESTED"
    assert independent_vendor_origins([provider]) == frozenset()


@pytest.mark.anyio
async def test_rights_acknowledgment_is_not_implied_by_enabling_a_manual_profile() -> None:
    source = ManualEvidenceAdapter("manual", ProviderAccess(enabled=True))
    result = await source.fetch_dividends(segment())
    health = await source.healthcheck()
    assert result.state == health.state == FetchState.UNCONFIGURED
    assert result.error_code == health.reason == "RIGHTS_NOT_ACKNOWLEDGED"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider,market", [("manual", "HK"), ("hkex_evidence", "HK"), ("sec_evidence", "US")]
)
async def test_exact_cited_input_survives_base_typed_page_serialization_without_http(
    provider: Any, market: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Manual document evidence must not create a network client")

    monkeypatch.setattr(ProviderHTTP, "__init__", no_http)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", no_http)
    original = dividend(provider, adjustment_basis="AS_DECLARED", pay_date=None)
    source: ProviderAdapter = ManualEvidenceAdapter(
        provider, access(), batch=EvidenceBatch(dividends=(original,), splits=(split(provider),))
    )
    result = await source.fetch_dividends(segment(provider, market))
    factor = await source.fetch_splits(segment(provider, market))
    assert result.state == factor.state == FetchState.OK_WITH_DATA and result.complete
    (observed,) = result.rows
    assert observed.dps == "0.30000000" and observed.currency == "USD"
    assert observed.pay_date is observed.record_date is observed.announce_date is None
    assert observed.adjustment_basis == "AS_DECLARED" and observed.evidence == original.evidence
    assert observed.provider_event_id is None and observed.incomplete_reasons == frozenset()
    assert all(
        value is None
        for value in (result.http_status, result.raw_sha256, result.source_url, result.retry_at)
    )
    assert {
        "USER_SUPPLIED_ASSERTION",
        "DOCUMENT_NOT_FETCHED",
        "NOT_AN_INDEPENDENT_VENDOR_VOTE",
    } <= set(result.warnings)
    assert factor.rows[0].effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    assert factor.rows[0].model_dump(mode="json")["ratio_numerator"] == "3"
    serialized = result.model_dump(mode="json")
    assert serialized["rows"][0]["evidence"] == original.evidence.model_dump(mode="json")
    reread = FetchPage[DividendObservation].model_validate_json(result.model_dump_json())
    assert reread.rows[0].normalized_sha256 == observed.normalized_sha256
    assert (
        "receipt_state" not in serialized["rows"][0]
        and "review_status" not in serialized["rows"][0]
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider,market,alias_provider,cursor,reason",
    [
        ("hkex_evidence", "US", "hkex_evidence", None, "UNSUPPORTED_MARKET"),
        ("sec_evidence", "HK", "sec_evidence", None, "UNSUPPORTED_MARKET"),
        ("manual", None, "manual", None, "MARKET_CONTEXT_REQUIRED"),
        ("manual", "HK", "yahoo", None, "ALIAS_PROVIDER_MISMATCH"),
        ("manual", "HK", "manual", "invented-cursor", "UNEXPECTED_PROVIDER_CURSOR"),
    ],
)
async def test_manual_context_does_not_infer_market_or_accept_another_provider(
    provider: Any, market: Any, alias_provider: Any, cursor: str | None, reason: str
) -> None:
    result = await ManualEvidenceAdapter(provider, access()).fetch_dividends(
        segment(alias_provider, market), cursor
    )
    assert result.error_code == reason and not result.complete and result.rows == ()


@pytest.mark.anyio
async def test_empty_or_outside_window_assertions_are_no_data_not_authoritative_empty() -> None:
    old = dividend(ex_date="2025-12-31")
    source = ManualEvidenceAdapter("manual", access(), batch=EvidenceBatch(dividends=(old,)))
    outside = await source.fetch_dividends(segment())
    empty = await source.fetch_splits(segment())
    assert (
        outside.state == empty.state == FetchState.NO_DATA and outside.complete and empty.complete
    )
    assert outside.filtered_count == 1 and empty.filtered_count == 0
    assert outside.issue_count == 0 and "OUTSIDE_MAPPING_WINDOW_IGNORED" in outside.warnings
    assert outside.rows == () and empty.rows == ()


@pytest.mark.anyio
async def test_foreign_profiles_and_securities_do_not_leak_into_a_requested_mapping() -> None:
    batch = EvidenceBatch(
        dividends=(
            dividend(),
            dividend("hkex_evidence"),
            dividend(security_id=OTHER),
        )
    )
    result = await ManualEvidenceAdapter("manual", access(), batch=batch).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1 and result.complete
    assert result.rows[0].provider_id == "manual" and result.rows[0].security_id == SID


@pytest.mark.anyio
async def test_bad_alias_and_duplicate_evidence_keep_valid_citations_partial() -> None:
    current = dividend()
    bad = dividend(provider_symbol="MAPPED.OTHER")
    batch = EvidenceBatch(dividends=(current, current, bad))
    result = await ManualEvidenceAdapter("manual", access(), batch=batch).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and len(result.rows) == 1 and not result.complete
    assert result.issue_count == 2 and result.error_code == "PARTIAL_PROVIDER_PAGE"
    assert {"MALFORMED_PROVIDER_ROW", "DUPLICATE_SOURCE_OBSERVATION"} <= set(result.warnings)
    invalid = await ManualEvidenceAdapter(
        "manual", access(), batch=EvidenceBatch(dividends=(bad,))
    ).fetch_dividends(segment())
    assert invalid.state == FetchState.PARSE_ERROR and invalid.rows == () and not invalid.complete


@pytest.mark.anyio
async def test_unknown_basis_currency_and_amount_remain_unknown_with_cited_input() -> None:
    batch = EvidenceBatch(dividends=(dividend(dps=None, currency=None),))
    result = await ManualEvidenceAdapter("manual", access(), batch=batch).fetch_dividends(segment())
    assert result.state == FetchState.OK_WITH_DATA and result.complete
    assert result.rows[0].dps is result.rows[0].currency is None
    assert result.rows[0].adjustment_basis == "UNKNOWN"
    assert {"MISSING_DPS", "MISSING_CURRENCY", "UNRESOLVED_ADJUSTMENT"} <= set(result.warnings)


@pytest.mark.parametrize(
    "value", [0.3, 1, True, "1e3", "NaN", "-1", "0.000000001", "92233720368.54775808"]
)
def test_manual_amounts_are_strict_owner_strings_without_rounding(value: Any) -> None:
    with pytest.raises(ValidationError):
        dividend(dps=value)


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence": None},
        {"ex_date": None},
        {"ex_date": "2026-02-30"},
        {"pay_date": "2026-01-08T00:00:00Z"},
        {"provider_id": "eodhd"},
        {"origin_group": "EODHD"},
        {"review_status": "VERIFIED"},
        {"receipt_state": "RECEIVED"},
        {"raw_payload": "not-a-retention-grant"},
    ],
)
def test_manual_boundary_does_not_accept_vendor_or_financial_verification_claims(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        dividend(**changes)
    missing = dividend().model_dump(mode="json")
    del missing["evidence"]
    with pytest.raises(ValidationError):
        DividendEvidence.model_validate(missing)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"citation": " "},
        {"citation": "bad\nreference"},
        {"citation": "a" * 301},
        {"reason": "too short"},
        {"reason": " " * 20},
        {"reason": "a" * 1001},
        {"url": "file:///private/document.pdf"},
        {"url": "javascript:alert(1)"},
        {"url": "https://user:synthetic-password@example.invalid/doc"},
        {"url": "https://example.invalid/doc with spaces"},
        {"url": "https://example.invalid:invalid/doc"},
    ],
)
def test_citation_requires_bounded_reference_and_reason_without_unsafe_url_schemes(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceCitation.model_validate(citation(**changes))


@pytest.mark.parametrize(
    "provider,url",
    [
        ("hkex_evidence", None),
        ("hkex_evidence", "https://www.sec.gov/synthetic.htm"),
        ("hkex_evidence", "https://hkexnews.hk.evil.invalid/synthetic.pdf"),
        ("hkex_evidence", "http://www.hkexnews.hk/synthetic.pdf"),
        ("hkex_evidence", "https://www.hkexnews.hk:444/synthetic.pdf"),
        ("sec_evidence", None),
        ("sec_evidence", "https://www.hkexnews.hk/synthetic.pdf"),
        ("sec_evidence", "https://www.sec.gov.evil.invalid/synthetic.htm"),
        ("sec_evidence", "http://www.sec.gov/synthetic.htm"),
        ("sec_evidence", "https://www.sec.gov:444/synthetic.htm"),
    ],
)
def test_official_attribution_requires_the_explicit_https_host_without_asserting_document_truth(
    provider: Any, url: str | None
) -> None:
    with pytest.raises(ValidationError, match="Official attribution"):
        dividend(provider, evidence=citation(provider, url=url))
    with pytest.raises(ValidationError, match="Official attribution"):
        split(provider, evidence=citation(provider, url=url))


@pytest.mark.parametrize(
    "provider,url",
    [
        ("hkex_evidence", "https://hkexnews.hk/synthetic.pdf"),
        ("hkex_evidence", "https://www.hkexnews.hk/synthetic.pdf"),
        ("hkex_evidence", "https://www1.hkexnews.hk:443/synthetic.pdf#page=2"),
        ("sec_evidence", "https://sec.gov/Archives/synthetic.htm"),
        ("sec_evidence", "https://www.sec.gov/Archives/synthetic.htm?output=1#dividend"),
        ("manual", None),
        ("manual", "https://issuer.example.invalid/synthetic.pdf"),
    ],
)
def test_official_links_and_local_manual_citations_are_preserved_but_not_fetched(
    provider: Any, url: str | None
) -> None:
    item = dividend(provider, evidence=citation(provider, url=url))
    assert item.evidence.url == url
    assert item.provider_event_id is None and item.origin_group not in independent_vendor_origins(
        [provider]
    )


@pytest.mark.parametrize(
    "new,old",
    [(True, 1), (0, 1), (1, 0), ("9223372036854775808", 1), ("1e3", 1), (1.5, 1), (None, 1)],
)
def test_manual_split_terms_are_exact_positive_int64_not_rounded_or_inferred(
    new: Any, old: Any
) -> None:
    with pytest.raises(ValidationError):
        split(ratio_numerator=new, ratio_denominator=old)


@pytest.mark.anyio
async def test_manual_int64_terms_and_known_zero_preserve_every_digit() -> None:
    batch = EvidenceBatch(
        dividends=(dividend(dps="0"),),
        splits=(
            split(ratio_numerator="9223372036854775807", ratio_denominator="9223372036854775806"),
        ),
    )
    source = ManualEvidenceAdapter("manual", access(), batch=batch)
    cash = await source.fetch_dividends(segment())
    factor = await source.fetch_splits(segment())
    assert cash.rows[0].dps == "0.00000000" and "MISSING_DPS" not in cash.warnings
    payload = factor.model_dump(mode="json")["rows"][0]
    assert payload["ratio_numerator"] == "9223372036854775807"
    assert payload["ratio_denominator"] == "9223372036854775806"
    assert payload["effect_scope"] == "PROVIDER_ADJUSTMENT_ONLY"


def test_citation_and_terms_revisions_are_frozen_distinct_assertions_not_rewritten() -> None:
    original = dividend()
    revised = dividend(
        dps="0.4",
        evidence=citation(reason="Corrected the transcription after checking the same document."),
    )
    changed_reference = dividend(
        evidence=citation(citation="Synthetic document, page 3, corrected table")
    )
    assert original.evidence.id == revised.evidence.id == changed_reference.evidence.id
    assert (
        len(
            {
                original.normalized_sha256,
                revised.normalized_sha256,
                changed_reference.normalized_sha256,
            }
        )
        == 3
    )
    assert original.dps == "0.30000000" and revised.dps == "0.40000000"
    batch = EvidenceBatch(dividends=(original,))
    for target, field, value in [
        (original, "dps", "0.40000000"),
        (original.evidence, "reason", "Attempted to replace historical evidence."),
        (batch, "dividends", (revised,)),
    ]:
        with pytest.raises(ValidationError):
            setattr(target, field, value)


def test_absent_citation_does_not_change_existing_vendor_normalized_hashes() -> None:
    vendor = DividendObservation(
        provider_id="eodhd",
        security_id=SID,
        provider_symbol="0005.HK",
        ex_date="2026-01-08",
        dps="0.3",
        currency="USD",
    )
    baseline = {**vendor.model_dump(mode="json", exclude={"evidence"}), "origin_group": "EODHD"}
    expected = hashlib.sha256(
        json.dumps(baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    assert vendor.evidence is None and vendor.normalized_sha256 == expected
    assert "evidence=" not in repr(dividend())


def test_manual_profile_and_batch_limits_do_not_accept_keys_or_unbounded_inputs() -> None:
    invalid: Any = "yahoo"
    with pytest.raises(ValueError, match="manual evidence profiles"):
        ManualEvidenceAdapter(invalid)
    with pytest.raises(ValueError, match="does not accept provider credentials"):
        ManualEvidenceAdapter("manual", ProviderAccess(credential=SecretStr("synthetic-key")))
    with pytest.raises(ValidationError):
        EvidenceBatch(dividends=(dividend(),) * 10001)
    with pytest.raises(ValidationError):
        EvidenceBatch(splits=(split(),) * 10001)
    source_rows = [dividend()]
    batch = EvidenceBatch.model_validate({"dividends": source_rows})
    source_rows.clear()
    assert len(batch.dividends) == 1


@pytest.mark.anyio
async def test_real_http_ledger_and_explicit_evidence_mapping_are_unchanged_by_normalization(
    client: httpx.AsyncClient, engine: Engine
) -> None:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic manual evidence isolation"})
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
            "name": "Synthetic cited security",
            "quote_currency": "USD",
            "identifier_effective_from": "2020-01-01",
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
    for provider in ("hkex_evidence", "manual"):
        await create(
            client,
            f"securities/{security['id']}/provider-symbols",
            {
                "provider_id": provider,
                "provider_symbol": "MAPPED",
                "effective_from": "2020-01-01",
            },
        )
    batch = EvidenceBatch(
        dividends=(dividend("hkex_evidence", security_id=security["id"]),),
        splits=(split("hkex_evidence", security_id=security["id"]),),
    )
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        resolved = resolve_symbol(con, UUID(security["id"]), "hkex_evidence", WINDOW)
        other = resolve_symbol(con, UUID(security["id"]), "manual", WINDOW)
    source = ManualEvidenceAdapter("hkex_evidence", access(), batch=batch)
    cash = await source.fetch_dividends(resolved.segments[0])
    factor = await source.fetch_splits(resolved.segments[0])
    refused = await source.fetch_dividends(other.segments[0])
    assert (
        cash.state == factor.state == FetchState.OK_WITH_DATA
        and refused.state == FetchState.UNSUPPORTED
    )
    assert cash.rows[0].evidence is not None and cash.rows[0].evidence.url == URLS["hkex_evidence"]
    assert factor.rows[0].effect_scope == "PROVIDER_ADJUSTMENT_ONLY"
    assert database_snapshot(engine) == before
