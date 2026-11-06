"""Direct-SQL synthetic provider evidence fixtures for schema tests, including legacy short IDs."""

from typing import Any
from uuid import uuid4

from sqlalchemy import Connection

from spm_api.audit import canonical, digest
from spm_api.providers.configuration import ProviderFile

NOW = "2026-11-04T00:00:00Z"


def insert(con: Connection, table: str, values: dict[str, Any]) -> None:
    columns = ",".join(values)
    marks = ",".join("?" for _ in values)
    con.exec_driver_sql(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))


def seed_evidence(
    con: Connection, owner: str, security: str, *, identifier: str = "obs"
) -> dict[str, dict[str, Any]]:
    exchange = con.exec_driver_sql(
        "SELECT e.market,e.timezone FROM securities s JOIN exchanges e ON e.code=s.exchange_code "
        "WHERE s.id=?",
        (security,),
    ).one()
    alias_id, context_id, run_id = (str(uuid4()) for _ in range(3))
    alias = {
        "id": alias_id,
        "security_id": security,
        "provider_id": "yahoo",
        "provider_symbol": "SYNTH",
        "effective_from": "2000-01-01",
        "effective_to": None,
        "verified": True,
        "version": 1,
    }
    insert(con, "provider_symbols", {**alias, "verified": 1})
    policy = ProviderFile(
        provider_id="yahoo",
        version=1,
        enabled=True,
        terms_acknowledged=True,
        terms_reference="Synthetic schema fixture terms",
        terms_reason="Accepted the synthetic schema fixture terms for software tests.",
        requests_per_minute=5,
        requests_per_day=50,
    )
    snapshot = canonical(
        {
            "alias": alias,
            "window": {"start": "2026-01-01", "end": "2026-01-31"},
            "market": exchange[0],
            "exchange_timezone": exchange[1],
        }
    )
    context = {
        "id": context_id,
        "owner_id": owner,
        "security_id": security,
        "provider_id": "yahoo",
        "origin_group": "YAHOO",
        "alias_id": alias_id,
        "alias_version": 1,
        "operation": "DIVIDENDS",
        "window_start": "2026-01-01",
        "window_end": "2026-01-31",
        "alias_json": snapshot,
        "profile_json": canonical(policy.model_dump(mode="json")),
        "profile_sha256": digest("profile"),
        "context_sha256": digest("context"),
        "parser_version": "synthetic-schema-v1",
        "normalization_version": 1,
        "created_at": NOW,
    }
    insert(con, "provider_fetch_contexts", context)
    run = {
        "id": run_id,
        "provider_id": "yahoo",
        "origin_group": "YAHOO",
        "security_id": security,
        "started_at": NOW,
        "finished_at": NOW,
        "status": "OK_WITH_DATA",
        "observations_count": 1,
        "http_status": 200,
        "provenance_version": 1,
        "owner_id": owner,
        "context_id": context_id,
        "operation": "DIVIDENDS",
        "requested_start": "2026-01-01",
        "requested_end": "2026-01-31",
        "response_sha256": digest("response"),
        "raw_sha256": digest("decoded payload"),
        "source_url": "https://query1.finance.yahoo.com/v8/finance/chart/SYNTH",
        "parse_complete": 1,
        "duration_ms": 0,
        "attempted": 1,
    }
    insert(con, "provider_runs", run)
    normalized = {
        "provider_id": "yahoo",
        "security_id": security,
        "provider_symbol": "SYNTH",
        "provider_event_id": None,
        "evidence": None,
        "ex_date": "2026-01-08",
        "announce_date": None,
        "record_date": None,
        "pay_date": None,
        "event_type": "UNKNOWN",
        "dps": None,
        "currency": None,
        "source_currency": None,
        "source_period": None,
        "source_frequency": None,
        "adjustment_basis": "UNKNOWN",
    }
    observation = {
        "id": identifier,
        "provider_run_id": run_id,
        "provider_id": "yahoo",
        "origin_group": "YAHOO",
        "security_id": security,
        "provider_symbol": "SYNTH",
        "normalized_sha256": digest("terms"),
        "raw_sha256": run["raw_sha256"],
        "source_url": run["source_url"],
        "ex_date": "2026-01-08",
        "event_type": "UNKNOWN",
        "adjustment_basis": "UNKNOWN",
        "retrieved_at": NOW,
        "provenance_version": 1,
        "normalized_json": canonical(normalized),
    }
    insert(con, "dividend_observations", observation)
    insert(
        con,
        "provider_observation_sightings",
        {
            "run_id": run_id,
            "ordinal": 0,
            "dividend_observation_id": identifier,
        },
    )
    return {"context": context, "run": run, "observation": observation}
