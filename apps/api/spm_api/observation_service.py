"""Bounded immutable source reads validate exact assertions and never repair or adopt facts."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from spm_api.decimal_types import from_scaled
from spm_api.errors import ApiError, Model
from spm_api.job_models import SourceOperation
from spm_api.master_models import ProviderId
from spm_api.master_service import security_row
from spm_api.observation_models import (
    DividendObservationRecord,
    ObservationPage,
    PublicCitation,
    SplitObservationRecord,
)
from spm_api.pagination import PageQuery, paginate, validate_time_page
from spm_api.providers.base import DividendObservation, SourceObservation, SplitObservation
from spm_api.providers.manual import DividendEvidence, SplitEvidence


def _source[S: SourceObservation](row: RowMapping, model: type[S], operation: SourceOperation) -> S:
    raw = row["normalized_json"]
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ValueError("Invalid normalized source assertion")
    source = model.model_validate_json(raw)
    expected = {
        "provider_id": source.provider_id,
        "origin_group": source.origin_group,
        "security_id": str(source.security_id),
        "provider_symbol": source.provider_symbol,
        "provider_event_id": source.provider_event_id,
        "normalized_sha256": source.normalized_sha256,
        "first_run_provenance": 1,
        "first_run_operation": operation,
        "first_run_state": "OK_WITH_DATA",
        "first_run_security": str(source.security_id),
        "first_run_provider": source.provider_id,
        "first_finished_at": row["retrieved_at"],
        "first_source_url": row["source_url"],
    }
    if any(row[name] != value for name, value in expected.items()):
        raise ValueError("Inconsistent immutable source provenance")
    pointer = urlsplit(row["source_url"]) if row["source_url"] is not None else None
    if pointer is not None and (pointer.query or pointer.fragment or pointer.username is not None):
        raise ValueError("Unsafe source pointer")
    return source


def _values(row: RowMapping, source: SourceObservation) -> dict[str, Any]:
    evidence = None
    if source.evidence is not None:
        citation = source.evidence
        link = urlsplit(citation.url) if citation.url is not None else None
        withheld = link is not None and bool(link.query or link.fragment)
        evidence = PublicCitation(
            **{**citation.model_dump(), "url": None if withheld else citation.url},
            url_withheld=withheld,
        )
    return {
        **source.model_dump(mode="json"),
        "id": row["id"],
        "provider_run_id": row["provider_run_id"],
        "origin_group": source.origin_group,
        "normalized_sha256": row["normalized_sha256"],
        "source_url": row["source_url"],
        "retrieved_at": row["retrieved_at"],
        "sightings_count": row["sightings_count"],
        "last_seen_at": row["last_seen_at"],
        "evidence": evidence,
    }


def as_dividend(row: RowMapping) -> DividendObservationRecord:
    try:
        model: type[DividendObservation] = (
            DividendEvidence
            if row["provider_id"] in {"manual", "hkex_evidence", "sec_evidence"}
            else DividendObservation
        )
        source = _source(row, model, "DIVIDENDS")
        fields = (
            "ex_date",
            "announce_date",
            "record_date",
            "pay_date",
            "event_type",
            "currency",
            "adjustment_basis",
        )
        dps = from_scaled(row["dps_e8"], 8) if row["dps_e8"] is not None else None
        if dps != source.dps or any(row[name] != getattr(source, name) for name in fields):
            raise ValueError("Inconsistent exact dividend assertion")
        return DividendObservationRecord.model_validate(
            {
                **_values(row, source),
                "incomplete_reasons": sorted(source.incomplete_reasons),
            }
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ApiError(
            503, "SOURCE_EVIDENCE_UNAVAILABLE", "The source assertion cannot be read safely"
        ) from None


def as_split(row: RowMapping) -> SplitObservationRecord:
    try:
        model: type[SplitObservation] = (
            SplitEvidence
            if row["provider_id"] in {"manual", "hkex_evidence", "sec_evidence"}
            else SplitObservation
        )
        source = _source(row, model, "SPLITS")
        expected = {
            "effective_date": source.effective_date,
            "ratio_num": source.ratio_numerator,
            "ratio_den": source.ratio_denominator,
            "effect_scope": source.effect_scope,
        }
        if (
            type(row["ratio_num"]) is not int
            or type(row["ratio_den"]) is not int
            or any(row[name] != value for name, value in expected.items())
        ):
            raise ValueError("Inconsistent exact split assertion")
        return SplitObservationRecord.model_validate(_values(row, source))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ApiError(
            503, "SOURCE_EVIDENCE_UNAVAILABLE", "The source assertion cannot be read safely"
        ) from None


def observations[T: Model](
    con: Connection,
    page: PageQuery,
    operation: SourceOperation,
    convert: Callable[[RowMapping], T],
    *,
    security_id: UUID | None,
    provider_id: ProviderId | None,
    from_date: str | None,
    to_date: str | None,
) -> ObservationPage[T]:
    validate_time_page(page)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ApiError(422, "VALIDATION_ERROR", "Date range end precedes start")
    if security_id is not None:
        security_row(con, str(security_id))
    dividend = operation == "DIVIDENDS"
    table = "dividend_observations" if dividend else "split_observations"
    date = "ex_date" if dividend else "effective_date"
    column = "dividend_observation_id" if dividend else "split_observation_id"
    provenance = "o.provenance_version=1" if dividend else "1=1"
    where = rf"""
        (:security IS NULL OR o.security_id=:security)
        AND (:provider IS NULL OR o.provider_id=:provider)
        AND (:start IS NULL OR o.{date}>=:start) AND (:end IS NULL OR o.{date}<=:end)
        AND (o.provider_id LIKE :search ESCAPE '\' OR o.provider_symbol LIKE :search ESCAPE '\'
          OR o.provider_event_id LIKE :search ESCAPE '\'
          OR s.display_ticker LIKE :search ESCAPE '\')
    """
    params = {
        **page.params(),
        "security": str(security_id) if security_id is not None else None,
        "provider": provider_id,
        "start": from_date,
        "end": to_date,
    }
    rows = (
        con.execute(
            text(f"""
        WITH page AS (
          SELECT o.* FROM {table} o JOIN securities s ON s.id=o.security_id
          WHERE {provenance} AND {where}
            AND (:cursor_value IS NULL OR (o.retrieved_at,o.id)<(:cursor_value,:cursor_id))
          ORDER BY o.retrieved_at DESC,o.id DESC LIMIT :limit
        ), seen AS (
          SELECT s.{column} AS observation_id,count(*) AS sightings_count,
            max(r.finished_at) AS last_seen_at
          FROM provider_observation_sightings s JOIN provider_runs r ON r.id=s.run_id
          WHERE r.provenance_version=1 AND s.{column} IN (SELECT id FROM page)
          GROUP BY s.{column}
        )
        SELECT p.*,seen.sightings_count,seen.last_seen_at,
          r.provenance_version AS first_run_provenance,r.operation AS first_run_operation,
          r.status AS first_run_state,
          r.security_id AS first_run_security,r.provider_id AS first_run_provider,
          r.finished_at AS first_finished_at,r.source_url AS first_source_url
        FROM page p LEFT JOIN seen ON seen.observation_id=p.id
        LEFT JOIN provider_runs r ON r.id=p.provider_run_id
        ORDER BY p.retrieved_at DESC,p.id DESC
    """),
            params,
        )
        .mappings()
        .all()
    )
    result = paginate(rows, page, "retrieved_at", convert)
    legacy = (
        con.execute(
            text(f"""
        SELECT count(*) FROM dividend_observations o JOIN securities s ON s.id=o.security_id
        WHERE o.provenance_version=0 AND {where}
    """),
            params,
        ).scalar_one()
        if dividend
        else 0
    )
    return ObservationPage(
        items=result.items, next_cursor=result.next_cursor, legacy_records_omitted=legacy
    )
