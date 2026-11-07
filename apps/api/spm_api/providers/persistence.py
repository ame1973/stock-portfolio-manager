"""Append source evidence in a short UoW; never fetch, reconcile or rewrite financial facts."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from spm_api.audit import audit, canonical, stamp
from spm_api.decimal_types import to_scaled
from spm_api.providers.adapters import FetchPage
from spm_api.providers.base import (
    DividendObservation,
    FetchState,
    SplitObservation,
    registry,
)
from spm_api.providers.configuration import LoadedConfiguration, ManagedPolicySnapshot
from spm_api.providers.http import _CREDENTIAL_NAMES
from spm_api.providers.manual import DividendEvidence, SplitEvidence
from spm_api.providers.selection import validate_configuration
from spm_api.providers.symbols import AliasSegment, ResolutionState, resolve_symbol
from spm_api.providers.yfinance import SDK_VERSION

Operation = Literal["DIVIDENDS", "SPLITS"]
_PARSERS = {
    "eodhd": "eodhd-json-v1",
    "alphavantage": "alpha-dividends-compat-v1",
    "massive": "massive-stocks-v1",
    "yahoo": "yahoo-chart-v8-compat-v1",
    "yfinance": f"yfinance-{SDK_VERSION}-actions-v1",
    "hkex_evidence": "manual-evidence-v1",
    "sec_evidence": "manual-evidence-v1",
    "manual": "manual-evidence-v1",
}


@dataclass(frozen=True)
class PreparedFetch:
    """Server-only request snapshot; configuration retains the memory-only credential guard."""

    id: UUID
    owner_id: UUID
    operation: Operation
    segment: AliasSegment
    configuration: LoadedConfiguration = field(repr=False)

    def metadata(self) -> dict[str, Any]:
        profile = self.configuration.profile
        revision = self.configuration.revision
        if self.configuration.error_code is not None or profile is None or revision is None:
            raise ValueError("PROVIDER_UNCONFIGURED")
        if (
            self.configuration.selection_owner_id is not None
            and self.configuration.selection_owner_id != self.owner_id
        ) or (isinstance(profile, ManagedPolicySnapshot) and profile.owner_id != self.owner_id):
            raise ValueError("PROVIDER_OWNER_MISMATCH")
        alias = self.segment.alias
        return {
            "owner_id": str(self.owner_id),
            "security_id": str(alias.security_id),
            "provider_id": alias.provider_id,
            "origin_group": registry()[alias.provider_id].origin_group,
            "alias_id": str(alias.id),
            "alias_version": alias.version,
            "operation": self.operation,
            "window_start": self.segment.window.start,
            "window_end": self.segment.window.end,
            "alias_json": canonical(self.segment.model_dump(mode="json")),
            "profile_json": canonical(profile.model_dump(mode="json")),
            "profile_sha256": revision.profile_sha256,
            "parser_version": _PARSERS[alias.provider_id],
            "normalization_version": 1,
        }


def prepare_fetch(
    owner_id: UUID,
    configuration: LoadedConfiguration,
    segment: AliasSegment,
    operation: Operation,
    *,
    context_id: UUID | None = None,
) -> PreparedFetch:
    if operation not in {"DIVIDENDS", "SPLITS"}:
        raise ValueError("UNSUPPORTED_CAPTURE_OPERATION")
    if configuration.provider_id != segment.alias.provider_id:
        raise ValueError("ALIAS_PROVIDER_MISMATCH")
    if segment.market is None or segment.exchange_timezone is None:
        raise ValueError("MISSING_EXCHANGE_CONTEXT")
    if segment.market not in registry()[configuration.provider_id].markets:
        raise ValueError("UNSUPPORTED_MARKET")
    prepared = PreparedFetch(context_id or uuid4(), owner_id, operation, segment, configuration)
    metadata = prepared.metadata()
    _guard_credential(prepared, *metadata.values())
    return prepared


@dataclass(frozen=True)
class CaptureReceipt:
    run_id: UUID
    context_id: UUID
    observation_ids: tuple[str, ...]
    observations_new: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _guard_credential(prepared: PreparedFetch, *values: object) -> None:
    credential = prepared.configuration.access.credential
    if credential is None:
        return
    secret = credential.get_secret_value()
    representations = (secret, canonical(secret)[1:-1])
    if any(
        isinstance(value, str) and token in value for token in representations for value in values
    ):
        raise ValueError("CREDENTIAL_IN_EVIDENCE")


def _pointer(prepared: PreparedFetch, page: FetchPage[Any]) -> None:
    provider = prepared.configuration.provider_id
    symbol = prepared.segment.alias.provider_symbol
    operation = prepared.operation
    expected = (
        f"https://eodhd.com/api/{'div' if operation == 'DIVIDENDS' else 'splits'}/{symbol}"
        if provider == "eodhd"
        else "https://www.alphavantage.co/query"
        if provider == "alphavantage"
        else f"https://api.massive.com/stocks/v1/{operation.lower()}"
        if provider == "massive"
        else f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        if provider in {"yahoo", "yfinance"}
        else None
    )
    if page.source_url is not None and page.source_url != expected:
        raise ValueError("UNSAFE_PROVIDER_SOURCE")
    _cursor(prepared, page.next_cursor)


def _cursor(prepared: PreparedFetch, value: str | None) -> None:
    if value is None:
        return
    if type(value) is not str or not 1 <= len(value) <= 4096:
        raise ValueError("UNSAFE_PROVIDER_CURSOR")
    parsed = urlsplit(value)
    if (
        prepared.configuration.provider_id != "massive"
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or parsed.path != f"/stocks/v1/{prepared.operation.lower()}"
    ):
        raise ValueError("UNSAFE_PROVIDER_CURSOR")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=32, errors="strict")
    if any(name.casefold() in _CREDENTIAL_NAMES for name, _ in pairs):
        raise ValueError("UNSAFE_PROVIDER_CURSOR")
    _guard_credential(prepared, value, *(name for name, _ in pairs), *(value for _, value in pairs))


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_CAPTURE_TIME_REQUIRED")
    return stamp(value.astimezone(UTC))


def _insert(con: Connection, table: str, values: dict[str, Any]) -> None:
    columns = ",".join(values)
    placeholders = ",".join(f":{key}" for key in values)
    con.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), values)


def validate_selection(con: Connection, prepared: PreparedFetch) -> None:
    """Fence owned decisions/scoped fallback using DB metadata only, never private files."""
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise ValueError("WRITE_UNIT_OF_WORK_REQUIRED")
    configuration = prepared.configuration
    profile = configuration.profile
    if configuration.selection_owner_id is None and not isinstance(profile, ManagedPolicySnapshot):
        # Unswitched operator producers retain their existing definition-only API semantics.
        return
    prepared.metadata()
    if isinstance(profile, ManagedPolicySnapshot) and not profile.enabled:
        raise ValueError("PROVIDER_CONFIG_CHANGED")
    validate_configuration(con, configuration, prepared.owner_id)


def persist_context(con: Connection, prepared: PreparedFetch) -> None:
    """Retain a checked non-secret request plan before I/O, without claiming an HTTP result."""
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise ValueError("WRITE_UNIT_OF_WORK_REQUIRED")
    metadata = prepared.metadata()
    _guard_credential(prepared, *metadata.values())
    validate_selection(con, prepared)
    context_hash = _digest(canonical(metadata))
    resolved = resolve_symbol(
        con,
        prepared.segment.alias.security_id,
        prepared.segment.alias.provider_id,
        prepared.segment.window,
    )
    if resolved.state != ResolutionState.RESOLVED or resolved.segments != (prepared.segment,):
        raise ValueError("PROVIDER_ALIAS_CHANGED")
    context = con.execute(
        text("SELECT context_sha256 FROM provider_fetch_contexts WHERE id=:id"),
        {"id": str(prepared.id)},
    ).scalar_one_or_none()
    if context is None:
        _insert(
            con,
            "provider_fetch_contexts",
            {
                "id": str(prepared.id),
                **metadata,
                "context_sha256": context_hash,
                "created_at": stamp(),
            },
        )
    elif context != context_hash:
        raise ValueError("PROVIDER_CONTEXT_CONFLICT")


def _receipt(con: Connection, run_id: str, context_id: str, operation: Operation) -> CaptureReceipt:
    column = "dividend_observation_id" if operation == "DIVIDENDS" else "split_observation_id"
    identifiers = tuple(
        con.execute(
            text(
                f"SELECT {column} FROM provider_observation_sightings "
                "WHERE run_id=:run ORDER BY ordinal"
            ),
            {"run": run_id},
        ).scalars()
    )
    table = "dividend_observations" if operation == "DIVIDENDS" else "split_observations"
    created = con.execute(
        text(f"SELECT count(*) FROM {table} WHERE provider_run_id=:run"), {"run": run_id}
    ).scalar_one()
    return CaptureReceipt(UUID(run_id), UUID(context_id), identifiers, created)


def capture_page(
    con: Connection,
    prepared: PreparedFetch,
    page: FetchPage[DividendObservation] | FetchPage[SplitObservation],
    *,
    run_id: UUID,
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    job_id: UUID | None = None,
    requested_cursor: str | None = None,
) -> CaptureReceipt:
    """Persist already-fetched data; caller must recheck private config before/after HTTP."""
    if not con.in_transaction() or not con.get_execution_options().get("writer"):
        raise ValueError("WRITE_UNIT_OF_WORK_REQUIRED")
    if page.window != prepared.segment.window or page.state == FetchState.EMPTY_CONFIRMED:
        raise ValueError("UNPROVEN_PROVIDER_WINDOW")
    if type(duration_ms) is not int or not 0 <= duration_ms <= 2147483647:
        raise ValueError("INVALID_CAPTURE_DURATION")
    started, finished = _utc(started_at), _utc(finished_at)
    if finished < started:
        raise ValueError("INVALID_CAPTURE_TIMING")
    _pointer(prepared, page)
    _cursor(prepared, requested_cursor)
    metadata = prepared.metadata()
    snapshot = canonical(metadata)
    wire = canonical(page.model_dump(mode="json"))
    _guard_credential(prepared, *metadata.values(), wire)
    context_hash = _digest(snapshot)
    response_hash = _digest(
        canonical(
            {
                "context_id": str(prepared.id),
                "context_sha256": context_hash,
                "job_id": str(job_id) if job_id else None,
                "page": page.model_dump(mode="json"),
                "started_at": started,
                "finished_at": finished,
                "duration_ms": duration_ms,
                **({"requested_cursor": requested_cursor} if requested_cursor is not None else {}),
            }
        )
    )
    existing = (
        con.execute(
            text("SELECT response_sha256,context_id,owner_id FROM provider_runs WHERE id=:id"),
            {"id": str(run_id)},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        if (
            existing["response_sha256"] != response_hash
            or existing["context_id"] != str(prepared.id)
            or existing["owner_id"] != str(prepared.owner_id)
        ):
            raise ValueError("PROVIDER_ATTEMPT_CONFLICT")
        return _receipt(con, str(run_id), str(prepared.id), prepared.operation)
    persist_context(con, prepared)
    rows: list[DividendObservation | SplitObservation] = []
    manual = registry()[prepared.configuration.provider_id].access_mode == "MANUAL_EVIDENCE"
    dividend_model: type[DividendObservation] = DividendEvidence if manual else DividendObservation
    split_model: type[SplitObservation] = SplitEvidence if manual else SplitObservation
    if (
        page.rows
        and prepared.operation not in registry()[prepared.configuration.provider_id].operations
    ):
        raise ValueError("UNSUPPORTED_CAPTURE_DATA")
    seen = set()
    observation: DividendObservation | SplitObservation
    for item in page.rows:
        if prepared.operation == "DIVIDENDS" and isinstance(item, DividendObservation):
            dividend_item = dividend_model.model_validate(item.model_dump(mode="json"))
            observation = dividend_item
            day = dividend_item.ex_date
        elif prepared.operation == "SPLITS" and isinstance(item, SplitObservation):
            split_item = split_model.model_validate(item.model_dump(mode="json"))
            observation = split_item
            day = split_item.effective_date
        else:
            raise ValueError("PROVIDER_OPERATION_MISMATCH")
        alias = prepared.segment.alias
        if (
            observation.provider_id != alias.provider_id
            or observation.security_id != alias.security_id
            or observation.provider_symbol != alias.provider_symbol
            or (day is not None and not page.window.start <= day <= page.window.end)
        ):
            raise ValueError("PROVIDER_OBSERVATION_SCOPE_MISMATCH")
        if (
            registry()[alias.provider_id].access_mode == "MANUAL_EVIDENCE"
            and observation.evidence is None
        ):
            raise ValueError("MANUAL_CITATION_REQUIRED")
        if observation.normalized_sha256 in seen:
            raise ValueError("DUPLICATE_PROVIDER_OBSERVATION")
        seen.add(observation.normalized_sha256)
        rows.append(observation)
    attempted = (
        page.http_status is not None or page.source_url is not None or page.raw_sha256 is not None
    )
    if manual and attempted:
        raise ValueError("MANUAL_EVIDENCE_CANNOT_CLAIM_HTTP")
    if (
        not manual
        and page.state in {FetchState.OK_WITH_DATA, FetchState.NO_DATA}
        and (
            not attempted
            or page.source_url is None
            or page.raw_sha256 is None
            or page.http_status is None
            or not 200 <= page.http_status < 300
        )
    ):
        raise ValueError("UNPROVEN_PROVIDER_ATTEMPT")
    _insert(
        con,
        "provider_runs",
        {
            "id": str(run_id),
            "job_id": str(job_id) if job_id else None,
            "provider_id": metadata["provider_id"],
            "origin_group": metadata["origin_group"],
            "security_id": metadata["security_id"],
            "started_at": started,
            "finished_at": finished,
            "status": page.state.value,
            "observations_count": len(rows),
            "http_status": page.http_status,
            "sanitized_error": page.error_code,
            "provenance_version": 1,
            "owner_id": str(prepared.owner_id),
            "context_id": str(prepared.id),
            "operation": prepared.operation,
            "requested_start": page.window.start,
            "requested_end": page.window.end,
            "response_sha256": response_hash,
            "raw_sha256": page.raw_sha256,
            "source_url": page.source_url,
            "parse_complete": int(page.complete),
            "issue_count": page.issue_count,
            "filtered_count": page.filtered_count,
            "warnings_json": canonical(page.warnings),
            "next_cursor": page.next_cursor,
            "requested_cursor": requested_cursor,
            "retry_at": page.retry_at,
            "duration_ms": duration_ms,
            "attempted": int(attempted),
        },
    )
    for ordinal, observation in enumerate(rows):
        dividend = isinstance(observation, DividendObservation)
        table = "dividend_observations" if dividend else "split_observations"
        normalized = canonical(observation.model_dump(mode="json"))
        found = (
            con.execute(
                text(
                    f"SELECT id,normalized_json FROM {table} WHERE provider_id=:provider "
                    "AND security_id=:security AND normalized_sha256=:hash"
                ),
                {
                    "provider": observation.provider_id,
                    "security": str(observation.security_id),
                    "hash": observation.normalized_sha256,
                },
            )
            .mappings()
            .first()
        )
        if found is not None:
            if found["normalized_json"] != normalized:
                raise ValueError("PROVIDER_OBSERVATION_HASH_CONFLICT")
            identifier = found["id"]
        else:
            identifier = str(uuid4())
            values: dict[str, Any] = {
                "id": identifier,
                "provider_run_id": str(run_id),
                "provider_id": observation.provider_id,
                "origin_group": observation.origin_group,
                "security_id": str(observation.security_id),
                "provider_symbol": observation.provider_symbol,
                "provider_event_id": observation.provider_event_id,
                "normalized_sha256": observation.normalized_sha256,
                "raw_sha256": page.raw_sha256,
                "source_url": page.source_url,
                "retrieved_at": finished,
                "normalized_json": normalized,
            }
            if isinstance(observation, DividendObservation):
                values.update(
                    ex_date=observation.ex_date,
                    announce_date=observation.announce_date,
                    record_date=observation.record_date,
                    pay_date=observation.pay_date,
                    event_type=observation.event_type,
                    dps_e8=to_scaled(observation.dps, 8) if observation.dps is not None else None,
                    currency=observation.currency,
                    adjustment_basis=observation.adjustment_basis,
                    raw_permitted=0,
                    provenance_version=1,
                    source_metadata_json=canonical({"normalization_version": 1}),
                )
            else:
                values.update(
                    effective_date=observation.effective_date,
                    ratio_num=observation.ratio_numerator,
                    ratio_den=observation.ratio_denominator,
                    effect_scope=observation.effect_scope,
                )
            _insert(con, table, values)
        _insert(
            con,
            "provider_observation_sightings",
            {
                "run_id": str(run_id),
                "ordinal": ordinal,
                "dividend_observation_id": identifier if dividend else None,
                "split_observation_id": None if dividend else identifier,
            },
        )
    receipt = _receipt(con, str(run_id), str(prepared.id), prepared.operation)
    audit(
        con,
        str(prepared.owner_id),
        "provider_run",
        str(run_id),
        "PROVIDER_EVIDENCE_CAPTURE",
        after={
            "context_id": str(prepared.id),
            "provider_id": metadata["provider_id"],
            "security_id": metadata["security_id"],
            "operation": prepared.operation,
            "state": page.state.value,
            "rows": len(rows),
            "observations_new": receipt.observations_new,
        },
        reason="Source evidence captured; no canonical dividend, receipt or ledger fact changed",
    )
    return receipt
