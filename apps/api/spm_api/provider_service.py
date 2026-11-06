"""Read immutable source history without probing providers or trusting legacy status payloads."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from spm_api.job_models import SourceOperation
from spm_api.master_models import ProviderId
from spm_api.provider_models import (
    ProviderCoverage,
    ProviderHealth,
    ProviderInfo,
    RecordedSourcePage,
)
from spm_api.providers.adapters import AdapterHealth
from spm_api.providers.base import FetchState, availability, registry
from spm_api.providers.symbols import FetchWindow


def as_provider(health: AdapterHealth) -> ProviderInfo:
    definition = registry()[health.provider_id]
    return ProviderInfo(
        **definition.model_dump(),
        configured=health.configured,
        capabilities=definition.operations,
        configuration=health,
    )


def _as_page(row: RowMapping) -> RecordedSourcePage:
    return RecordedSourcePage(
        id=UUID(row["id"]),
        security_id=UUID(row["security_id"]),
        operation=row["operation"],
        state=row["status"],
        provider_symbol=row["provider_symbol"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        attempted=bool(row["attempted"]),
        http_status=row["http_status"],
        error_code=row["sanitized_error"],
        window=FetchWindow(start=row["requested_start"], end=row["requested_end"]),
        parse_complete=bool(row["parse_complete"]),
        has_more=row["next_cursor"] is not None,
        observations_count=row["observations_count"],
        issue_count=row["issue_count"],
        filtered_count=row["filtered_count"],
        source_url=row["source_url"],
    )


def _history(
    con: Connection,
    provider_id: ProviderId,
    *,
    security_id: UUID | None = None,
    operation: SourceOperation | None = None,
) -> tuple[RecordedSourcePage | None, datetime | None, int]:
    params = {
        "provider": provider_id,
        "security": str(security_id) if security_id is not None else None,
        "operation": operation,
    }
    row = (
        con.execute(
            text("""
        SELECT r.*,json_extract(c.alias_json,'$.alias.provider_symbol') AS provider_symbol
        FROM provider_runs r LEFT JOIN provider_fetch_contexts c ON c.id=r.context_id
        WHERE r.provider_id=:provider AND r.provenance_version=1
          AND (:security IS NULL OR r.security_id=:security)
          AND (:operation IS NULL OR r.operation=:operation)
        ORDER BY r.finished_at DESC,r.id DESC LIMIT 1
    """),
            params,
        )
        .mappings()
        .first()
    )
    success, legacy = con.execute(
        text("""
        SELECT max(CASE WHEN provenance_version=1 AND parse_complete=1
            AND status IN ('OK_WITH_DATA','NO_DATA')
            AND (:operation IS NULL OR operation=:operation) THEN finished_at END),
          coalesce(sum(CASE WHEN provenance_version=0 THEN 1 ELSE 0 END),0)
        FROM provider_runs WHERE provider_id=:provider
          AND (:security IS NULL OR security_id=:security)
    """),
        params,
    ).one()
    return (
        _as_page(row) if row is not None else None,
        datetime.fromisoformat(success) if success is not None else None,
        legacy,
    )


def as_health(con: Connection, configuration: AdapterHealth) -> ProviderHealth:
    last, success, legacy = _history(con, configuration.provider_id)
    return ProviderHealth(
        provider_id=configuration.provider_id,
        configured=configuration.configured,
        state=configuration.state,
        reason=configuration.reason,
        last_result=last,
        last_success_at=success,
        last_error_code=last.error_code if last is not None else None,
        legacy_runs_omitted=legacy,
    )


def as_coverage(
    con: Connection,
    row: RowMapping,
    configuration: AdapterHealth,
    operation: SourceOperation,
) -> ProviderCoverage:
    definition = registry()[configuration.provider_id]
    security_id = UUID(row["id"])
    last, success, legacy = _history(
        con, configuration.provider_id, security_id=security_id, operation=operation
    )
    column = "dividend_observation_id" if operation == "DIVIDENDS" else "split_observation_id"
    observations = con.execute(
        text(f"""
        SELECT count(DISTINCT s.{column}) FROM provider_observation_sightings s
        JOIN provider_runs r ON r.id=s.run_id
        WHERE r.provenance_version=1 AND r.provider_id=:provider
          AND r.security_id=:security AND r.operation=:operation
    """),
        {
            "provider": configuration.provider_id,
            "security": str(security_id),
            "operation": operation,
        },
    ).scalar_one()
    states = [FetchState.OK_WITH_DATA] if observations else [last.state] if last is not None else []
    return ProviderCoverage(
        security_id=security_id,
        provider_id=configuration.provider_id,
        origin_group=definition.origin_group,
        provider_symbol=last.provider_symbol if last is not None else None,
        operation=operation,
        status=last.state if last is not None else "NOT_TESTED",
        configuration=configuration,
        supported_market=row["market"] in definition.markets,
        supported_operation=operation in definition.operations,
        availability=availability(states),
        observations_count=observations,
        last_result=last,
        last_success_at=success,
        legacy_runs_omitted=legacy,
        legacy_status_ignored=bool(row["legacy_status_ignored"]),
    )
