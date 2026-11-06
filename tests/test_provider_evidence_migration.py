"""Evidence upgrades retain legacy/factual bytes and enforce immutable relational provenance."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.audit import canonical, digest
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from tests.helpers import database_snapshot as snapshot
from tests.provider_evidence_helpers import NOW, insert, seed_evidence
from tests.test_action_opening_migration import schema
from tests.test_transfer_migration import dimensions, seed_buy


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0007_actions_opening")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
            insert(
                con,
                "provider_runs",
                {
                    "id": "legacy-run",
                    "provider_id": "eodhd",
                    "origin_group": "EODHD",
                    "security_id": "stock",
                    "started_at": NOW,
                    "status": "STALE",
                    "observations_count": 1,
                },
            )
            insert(
                con,
                "dividend_observations",
                {
                    "id": "legacy-obs",
                    "provider_run_id": "legacy-run",
                    "provider_id": "eodhd",
                    "origin_group": "EODHD",
                    "security_id": "stock",
                    "provider_symbol": "OLD.US",
                    "normalized_sha256": "legacy-unresolved-hash",
                    "ex_date": "2026-01-08",
                    "dps_e8": 9223372036854775807,
                    "currency": None,
                    "adjustment_basis": "UNKNOWN",
                    "retrieved_at": NOW,
                    "source_metadata_json": '{"legacy":"retained exactly"}',
                },
            )
        yield engine
    finally:
        engine.dispose()


def test_upgrade_preserves_all_original_values_and_does_not_invent_provenance(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.connect() as con:
        names = (
            con.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
            .scalars()
            .all()
        )
        columns = {
            name: [row[1] for row in con.exec_driver_sql(f'PRAGMA table_info("{name}")')]
            for name in names
        }
    before = snapshot(old_engine)
    migrate(settings)
    with unit_of_work(old_engine) as con:
        for table, fields in columns.items():
            if table in {"audit_events", "alembic_version"}:
                continue
            assert [
                tuple(row)
                for row in con.exec_driver_sql(
                    f'SELECT {",".join(fields)} FROM "{table}" ORDER BY rowid'
                )
            ] == before[table]
        assert con.exec_driver_sql(
            "SELECT provenance_version,owner_id,context_id FROM provider_runs"
        ).one() == (0, None, None)
        assert con.exec_driver_sql(
            "SELECT provenance_version,normalized_json,dps_e8,currency FROM dividend_observations"
        ).one() == (0, None, 9223372036854775807, None)
        assert con.exec_driver_sql("SELECT count(*) FROM provider_fetch_contexts").scalar_one() == 0
        assert (
            con.exec_driver_sql("SELECT count(*) FROM provider_observation_sightings").scalar_one()
            == 0
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_PROVIDER_EVIDENCE'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
    accepted = snapshot(old_engine)
    migrate(settings)
    assert snapshot(old_engine) == accepted


@pytest.mark.parametrize("problem", ["scaled-real", "late-ddl"])
def test_failed_upgrade_restores_schema_head_and_every_retained_value(
    old_engine: Engine, settings: Settings, problem: str
) -> None:
    with old_engine.begin() as con:
        if problem == "scaled-real":
            con.exec_driver_sql("DROP TRIGGER trg_dividend_observation_no_update")
            con.exec_driver_sql("DROP TRIGGER trg_scaled_dividend_observations_update")
            con.exec_driver_sql("UPDATE dividend_observations SET dps_e8=0.1 WHERE id='legacy-obs'")
        else:
            con.exec_driver_sql(
                "CREATE INDEX idx_split_observations_security ON provider_status(provider_id)"
            )
    before, original_schema = snapshot(old_engine), schema(old_engine)
    with pytest.raises((RuntimeError, OperationalError)):
        migrate(settings)
    assert snapshot(old_engine) == before and schema(old_engine) == original_schema
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0007_actions_opening"
        )


@pytest.fixture
def evidence(engine: Engine) -> dict[str, dict[str, Any]]:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        seed_buy(con)
        return seed_evidence(con, "owner", "stock")


def split_fixture(con: Connection, evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    context = {**evidence["context"], "id": str(uuid4()), "operation": "SPLITS"}
    insert(con, "provider_fetch_contexts", context)
    run = {
        **evidence["run"],
        "id": str(uuid4()),
        "context_id": context["id"],
        "operation": "SPLITS",
    }
    insert(con, "provider_runs", run)
    normalized = {
        "provider_id": "yahoo",
        "security_id": "stock",
        "provider_symbol": "SYNTH",
        "provider_event_id": None,
        "evidence": None,
        "effective_date": "2026-01-08",
        "ratio_numerator": "9223372036854775807",
        "ratio_denominator": "9223372036854775806",
        "source_adjustment_type": None,
        "effect_scope": "PROVIDER_ADJUSTMENT_ONLY",
    }
    values = {
        "id": str(uuid4()),
        "provider_run_id": run["id"],
        "provider_id": "yahoo",
        "origin_group": "YAHOO",
        "security_id": "stock",
        "provider_symbol": "SYNTH",
        "normalized_sha256": digest("factor"),
        "raw_sha256": run["raw_sha256"],
        "source_url": run["source_url"],
        "effective_date": "2026-01-08",
        "ratio_num": 9223372036854775807,
        "ratio_den": 9223372036854775806,
        "effect_scope": "PROVIDER_ADJUSTMENT_ONLY",
        "normalized_json": canonical(normalized),
        "retrieved_at": NOW,
    }
    insert(con, "split_observations", values)
    insert(
        con,
        "provider_observation_sightings",
        {
            "run_id": run["id"],
            "ordinal": 0,
            "split_observation_id": values["id"],
        },
    )
    return values


@pytest.mark.parametrize(
    "table",
    [
        "provider_fetch_contexts",
        "provider_runs",
        "dividend_observations",
        "split_observations",
        "provider_observation_sightings",
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_all_source_history_is_immutable_including_noop_updates(
    engine: Engine, evidence: Any, table: str, operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        split_fixture(con, evidence)
    before = snapshot(engine)
    column = "ordinal" if table == "provider_observation_sightings" else "id"
    with pytest.raises(IntegrityError, match="IMMUTABLE"), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"UPDATE {table} SET {column}={column}"
            if operation == "UPDATE"
            else f"DELETE FROM {table}"
        )
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"origin_group": "EODHD"},
        {"alias_version": 2},
        {"security_id": "foreign"},
        {"window_start": "2025-12-31"},
        {"window_end": "2026-02-30"},
        {"profile_sha256": "private-not-a-hash"},
        {"normalization_version": 0},
        {"profile_json": '{"api_key":"never allowed"}'},
    ],
)
def test_context_requires_current_identity_and_nonsecret_valid_policy(
    engine: Engine, evidence: Any, changes: dict[str, Any]
) -> None:
    values = {**evidence["context"], "id": str(uuid4()), **changes}
    before = snapshot(engine)
    with pytest.raises((IntegrityError, OperationalError)), unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", values)
    assert snapshot(engine) == before


@pytest.mark.parametrize("day", ["2026-02-30", "2026-04-31", "2025-02-29"])
def test_context_dates_cannot_use_sqlite_unnormalized_calendar_days(
    engine: Engine, evidence: Any, day: str
) -> None:
    values = {**evidence["context"], "id": str(uuid4()), "window_start": day, "window_end": day}
    alias = json.loads(values["alias_json"])
    alias["window"] = {"start": day, "end": day}
    values["alias_json"] = canonical(alias)
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", values)
    assert snapshot(engine) == before


def test_context_accepts_a_real_leap_day_without_rounding(engine: Engine, evidence: Any) -> None:
    day = "2024-02-29"
    values = {**evidence["context"], "id": str(uuid4()), "window_start": day, "window_end": day}
    alias = json.loads(values["alias_json"])
    alias["window"] = {"start": day, "end": day}
    values["alias_json"] = canonical(alias)
    with unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", values)
        assert con.exec_driver_sql(
            "SELECT window_start,window_end FROM provider_fetch_contexts WHERE id=?",
            (values["id"],),
        ).one() == (day, day)


@pytest.mark.parametrize(
    "changes",
    [
        {"provenance_version": 0},
        {"owner_id": "other"},
        {"observations_count": 0},
        {"status": "NO_DATA"},
        {"status": "EMPTY_CONFIRMED"},
        {"duration_ms": 1.5},
        {"issue_count": 1},
        {"coverage_start": "2026-01-01"},
        {"coverage_end": "2026-01-31"},
        {"requested_start": "bad-date"},
        {"http_status": 999},
        {"http_status": None},
        {"raw_sha256": None},
        {"source_url": None},
        {"source_url": "https://foreign.invalid/data"},
        {"response_sha256": "bad"},
        {"raw_sha256": "bad"},
        {"source_url": "https://provider.invalid/?api_key=private"},
        {"sanitized_error": "private error body"},
        {"next_cursor": "https://foreign.invalid/page"},
        {"attempted": 0},
        {"context_id": None},
    ],
)
def test_run_counts_states_coverage_privacy_and_owner_scope_are_enforced(
    engine: Engine, evidence: Any, changes: dict[str, Any]
) -> None:
    values = {**evidence["run"], "id": str(uuid4()), **changes}
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_runs", values)
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"provenance_version": 0},
        {"raw_permitted": 1},
        {"provider_id": "manual"},
        {"origin_group": "MASSIVE"},
        {"normalized_sha256": "bad"},
        {"normalized_json": None},
        {"dps_e8": 1.5},
        {"dps_e8": 30000000},
        {"currency": "USD"},
        {"provider_event_id": "another event"},
        {"provider_symbol": "ANOTHER"},
        {"retrieved_at": "2026-11-05T00:00:00Z"},
        {"source_url": "https://different.invalid/data"},
    ],
)
def test_observation_values_must_match_immutable_run_and_exact_normalized_terms(
    engine: Engine, evidence: Any, changes: dict[str, Any]
) -> None:
    values = {**evidence["observation"], "id": str(uuid4()), **changes}
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_observations", values)
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "field,value",
    [("review_status", "VERIFIED"), ("receipt_state", "RECEIVED"), ("api_key", "private")],
)
def test_normalized_observation_cannot_smuggle_financial_status_or_secret_fields(
    engine: Engine, evidence: Any, field: str, value: str
) -> None:
    values = {**evidence["observation"], "id": str(uuid4())}
    payload = json.loads(values["normalized_json"])
    payload[field] = value
    values["normalized_json"] = canonical(payload)
    before = snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_PROVIDER_OBSERVATION"),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, "dividend_observations", values)
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"ordinal": 1},
        {"ordinal": -1},
        {"split_observation_id": "other"},
        {"dividend_observation_id": None},
    ],
)
def test_sightings_cannot_multiply_or_reassign_source_assertions(
    engine: Engine, evidence: Any, changes: dict[str, Any]
) -> None:
    values = {
        "run_id": evidence["run"]["id"],
        "ordinal": 0,
        "dividend_observation_id": evidence["observation"]["id"],
        **changes,
    }
    before = snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_observation_sightings", values)
    assert snapshot(engine) == before


def test_stale_alias_blocks_even_a_direct_new_run_and_split_maxima_are_integers(
    engine: Engine, evidence: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        factor = split_fixture(con, evidence)
        assert con.exec_driver_sql(
            "SELECT typeof(ratio_num),typeof(ratio_den) FROM split_observations WHERE id=?",
            (factor["id"],),
        ).one() == ("integer", "integer")
        con.exec_driver_sql("UPDATE provider_symbols SET version=version+1")
    before = snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_PROVIDER_RUN"),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, "provider_runs", {**evidence["run"], "id": str(uuid4())})
    assert snapshot(engine) == before
