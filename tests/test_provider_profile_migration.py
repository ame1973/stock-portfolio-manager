"""Managed provider policy history is inert, append-only, owned and version fenced."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.runtime import check_configuration
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import NOW, insert, seed_evidence
from tests.test_action_opening_migration import schema
from tests.test_provider_persistence import book as book
from tests.test_schedule_migration import upgrade_to
from tests.test_transfer_migration import dimensions, seed_buy

TABLE = "provider_profile_revisions"
AT = "2026-11-07T00:00:00.000000Z"
PROVIDERS = (
    "eodhd",
    "alphavantage",
    "massive",
    "yahoo",
    "yfinance",
    "hkex_evidence",
    "sec_evidence",
    "manual",
)


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0008_provider_evidence")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
            seed_evidence(con, "owner", "stock")
            insert(
                con,
                "settings",
                {
                    "owner_id": "owner",
                    "key": "provider_profiles",
                    "value_json": '{"enabled":true,"api_key":"synthetic-retained-not-a-key"}',
                    "updated_at": NOW,
                },
            )
            insert(
                con,
                "jobs",
                {
                    "id": "legacy-provider-job",
                    "owner_id": "owner",
                    "kind": "DIVIDEND_SYNC",
                    "dedupe_key": "legacy-provider-profile",
                    "status": "QUEUED",
                    "request_json": '{"enabled":true}',
                    "created_at": NOW,
                },
            )
        upgrade_to(engine, "0011_source_schedules")
        yield engine
    finally:
        engine.dispose()


def test_upgrade_preserves_all_old_facts_and_does_not_adopt_settings_or_credentials(
    old_engine: Engine, settings: Settings
) -> None:
    before = database_snapshot(old_engine)
    secret_directory_existed = settings.provider_secret_dir.exists()
    migrate(settings)
    after = database_snapshot(old_engine)
    assert after[TABLE] == []
    assert {
        key: rows for key, rows in before.items() if key not in {"audit_events", "alembic_version"}
    } == {key: after[key] for key in before if key not in {"audit_events", "alembic_version"}}
    with unit_of_work(old_engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_PROVIDER_PROFILES'"
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert not (settings.data_dir / "provider-config").exists()
    assert settings.provider_secret_dir.exists() == secret_directory_existed
    migrate(settings)
    assert database_snapshot(old_engine) == after


def test_late_ddl_failure_rolls_back_entire_schema_head_and_facts(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        con.exec_driver_sql(
            "CREATE INDEX idx_provider_profile_credential ON provider_status(provider_id)"
        )
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises(OperationalError):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0011_source_schedules"
        )


def test_late_audit_failure_rolls_back_all_ddl_and_existing_owners_audits(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        con.exec_driver_sql("""
            CREATE TRIGGER test_reject_profile_upgrade BEFORE INSERT ON audit_events
            WHEN NEW.action='MIGRATE_PROVIDER_PROFILES' AND NEW.owner_id='other'
            BEGIN SELECT RAISE(ABORT,'SYNTHETIC_AUDIT_FAILURE'); END
        """)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises(IntegrityError, match="SYNTHETIC_AUDIT_FAILURE"):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions


@pytest.fixture
def profiles(engine: Engine) -> Engine:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        seed_buy(con)
    return engine


def values(**changes: Any) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "owner_id": "owner",
        "provider_id": "eodhd",
        "version": 1,
        "enabled": 0,
        "terms_acknowledged": 0,
        "terms_reference": "",
        "terms_reason": None,
        "requests_per_minute": None,
        "requests_per_day": None,
        "credential_artifact_id": None,
        "raw_payload_storage": 0,
        "change_reason": "Explicit synthetic owner policy change.",
        "request_id": str(uuid4()),
        "created_at": AT,
        **changes,
    }


def append(engine: Engine, **changes: Any) -> dict[str, Any]:
    result = values(**changes)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, result)
    return result


def declared(**changes: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "enabled": 1,
        "terms_acknowledged": 1,
        "terms_reference": "Synthetic owner-supplied rights reference",
        "terms_reason": "Synthetic permission assertion, not verified licensing.",
        "requests_per_minute": 5,
        "requests_per_day": 50,
        **changes,
    }


@pytest.mark.parametrize("provider", PROVIDERS)
def test_new_profile_is_explicitly_disabled_and_changes_only_its_inert_history(
    profiles: Engine, provider: str
) -> None:
    before = database_snapshot(profiles)
    row = append(profiles, provider_id=provider)
    after = database_snapshot(profiles)
    assert {key: rows for key, rows in before.items() if key != TABLE} == {
        key: rows for key, rows in after.items() if key != TABLE
    }
    with unit_of_work(profiles) as con:
        assert con.exec_driver_sql(
            f"SELECT enabled,version,credential_artifact_id FROM {TABLE} WHERE id=?", (row["id"],)
        ).one() == (0, 1, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"id": str(uuid4()) + "\x00hidden"},
        {"owner_id": "unknown"},
        {"provider_id": "../eodhd"},
        {"provider_id": "unknown"},
        {"version": 0},
        {"version": 2},
        {"version": 1.5},
        {"version": 2147483648},
        {"enabled": 1},
        {"enabled": 2},
        {"enabled": 0.5},
        {"terms_acknowledged": 2},
        {"terms_acknowledged": 1},
        {"terms_reason": "short"},
        {"terms_reference": "x" * 1001},
        {"terms_reference": "private\x00hidden"},
        {"requests_per_minute": 0},
        {"requests_per_minute": -1},
        {"requests_per_minute": 1.5},
        {"requests_per_minute": 2147483648},
        {"requests_per_day": 0},
        {"requests_per_day": 2147483648},
        {"raw_payload_storage": 1},
        {"credential_artifact_id": "../private.key"},
        {"credential_artifact_id": str(uuid4()) + "\x00hidden"},
        {"provider_id": "yahoo", "credential_artifact_id": str(uuid4())},
        {"provider_id": "yfinance", "credential_artifact_id": str(uuid4())},
        {"provider_id": "manual", "requests_per_minute": 1},
        {"provider_id": "hkex_evidence", "requests_per_day": 1},
        {"provider_id": "sec_evidence", "credential_artifact_id": str(uuid4())},
        {"change_reason": ""},
        {"change_reason": "short"},
        {"change_reason": "\t" * 10},
        {"change_reason": "   long enough but not normalized   "},
        {"change_reason": "x" * 1001},
        {"change_reason": "valid reason\x00hidden"},
        {"request_id": "not-a-minted-uuid"},
        {"request_id": str(uuid4()) + "\x00hidden"},
        {"created_at": AT + "\x00hidden"},
        {"created_at": "2026-02-30T00:00:00.000000Z"},
        {"created_at": "0000-01-01T00:00:00.000000Z"},
        {"created_at": "2026-11-07T24:00:00.000000Z"},
        {"created_at": "2026-11-07T00:00:00.000000+00:00"},
        {"created_at": "2026-11-07T00:00:00.00000xZ"},
    ],
)
def test_invalid_initial_metadata_fails_atomically(
    profiles: Engine, changes: dict[str, Any]
) -> None:
    before = database_snapshot(profiles)
    with pytest.raises(IntegrityError):
        append(profiles, **changes)
    assert database_snapshot(profiles) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"terms_acknowledged": 0},
        {"terms_reference": ""},
        {"terms_reference": "   "},
        {"terms_reference": "\t\r\n"},
        {"terms_reason": None},
        {"requests_per_minute": None},
        {"requests_per_day": None},
        {"credential_artifact_id": None},
    ],
)
def test_enabling_key_provider_requires_explicit_rights_budgets_and_bound_edition(
    profiles: Engine, changes: dict[str, Any]
) -> None:
    append(profiles)
    before = database_snapshot(profiles)
    fields = declared(credential_artifact_id=str(uuid4()))
    fields.update(changes)
    with pytest.raises(IntegrityError):
        append(profiles, **fields)
    assert database_snapshot(profiles) == before


@pytest.mark.parametrize("provider", PROVIDERS)
def test_explicit_later_policy_can_enable_only_its_supported_access_mode(
    profiles: Engine, provider: str
) -> None:
    append(profiles, provider_id=provider)
    fields = declared(provider_id=provider)
    if provider in {"eodhd", "alphavantage", "massive"}:
        fields["credential_artifact_id"] = str(uuid4())
    elif provider in {"hkex_evidence", "sec_evidence", "manual"}:
        fields.update(requests_per_minute=None, requests_per_day=None)
    append(profiles, **fields)
    with unit_of_work(profiles) as con:
        rows = con.exec_driver_sql(
            f"SELECT version,enabled FROM {TABLE} WHERE provider_id=? ORDER BY version", (provider,)
        )
        assert [tuple(row) for row in rows] == [(1, 0), (2, 1)]


def test_rotation_disable_and_clear_append_without_changing_old_selection_or_finance(
    profiles: Engine,
) -> None:
    first, second = str(uuid4()), str(uuid4())
    before = database_snapshot(profiles)
    append(profiles, credential_artifact_id=first)
    append(profiles, **declared(credential_artifact_id=first))
    retained = database_snapshot(profiles)[TABLE]
    append(profiles, **declared(version=3, credential_artifact_id=second))
    append(profiles, **declared(version=4, enabled=0, credential_artifact_id=second))
    append(profiles, version=5)
    after = database_snapshot(profiles)
    assert after[TABLE][:2] == retained
    assert {key: rows for key, rows in before.items() if key != TABLE} == {
        key: rows for key, rows in after.items() if key != TABLE
    }
    with unit_of_work(profiles) as con:
        rows = con.exec_driver_sql(
            f"SELECT version,enabled,credential_artifact_id FROM {TABLE} ORDER BY version"
        )
        assert [tuple(row) for row in rows] == [
            (1, 0, first),
            (2, 1, first),
            (3, 1, second),
            (4, 0, second),
            (5, 0, None),
        ]


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_old_policies_cannot_be_changed_deleted_or_replaced(
    profiles: Engine, operation: str
) -> None:
    row = append(profiles)
    before = database_snapshot(profiles)
    with pytest.raises(IntegrityError), unit_of_work(profiles, write=True) as con:
        if operation == "UPDATE":
            con.exec_driver_sql(f"UPDATE {TABLE} SET enabled=enabled")
        elif operation == "DELETE":
            con.exec_driver_sql(f"DELETE FROM {TABLE}")
        else:
            next_row = {**row, "version": 2}
            columns, marks = ",".join(next_row), ",".join("?" for _ in next_row)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE}({columns}) VALUES ({marks})",
                tuple(next_row.values()),
            )
    assert database_snapshot(profiles) == before


@pytest.mark.parametrize("other", [{"owner_id": "other"}, {"provider_id": "massive"}])
def test_bound_artifact_cannot_be_reassigned_to_another_owner_or_provider(
    profiles: Engine, other: dict[str, Any]
) -> None:
    reference = str(uuid4())
    append(profiles, credential_artifact_id=reference)
    before = database_snapshot(profiles)
    with pytest.raises(IntegrityError):
        append(profiles, credential_artifact_id=reference, **other)
    assert database_snapshot(profiles) == before


@pytest.mark.parametrize("version", [1, 3])
def test_stale_or_skipped_version_cannot_replace_the_latest_policy(
    profiles: Engine, version: int
) -> None:
    append(profiles)
    before = database_snapshot(profiles)
    with pytest.raises(IntegrityError):
        append(profiles, version=version)
    assert database_snapshot(profiles) == before


def test_clock_regression_or_inactive_owner_cannot_append_a_revision(profiles: Engine) -> None:
    append(profiles)
    before = database_snapshot(profiles)
    with pytest.raises(IntegrityError):
        append(profiles, version=2, created_at="2026-11-06T00:00:00.000000Z")
    assert database_snapshot(profiles) == before
    with unit_of_work(profiles, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id='owner'")
    before = database_snapshot(profiles)
    with pytest.raises(IntegrityError):
        append(profiles, version=2)
    assert database_snapshot(profiles) == before


def test_competing_writers_commit_exactly_one_next_version(profiles: Engine) -> None:
    append(profiles)

    def attempt(_: int) -> bool:
        try:
            append(profiles, version=2)
        except IntegrityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]
    with unit_of_work(profiles) as con:
        assert con.exec_driver_sql(
            f"SELECT version FROM {TABLE} ORDER BY version"
        ).scalars().all() == [1, 2]


def test_independent_owner_and_provider_versions_do_not_share_policy_state(
    profiles: Engine,
) -> None:
    append(profiles)
    append(profiles, version=2)
    append(profiles, owner_id="other")
    append(profiles, provider_id="massive")
    append(
        profiles, provider_id="yahoo", requests_per_minute=2147483647, requests_per_day=2147483647
    )
    with unit_of_work(profiles) as con:
        rows = con.exec_driver_sql(
            f"SELECT owner_id,provider_id,max(version) FROM {TABLE} "
            "GROUP BY owner_id,provider_id ORDER BY owner_id,provider_id"
        )
        assert [tuple(row) for row in rows] == [
            ("other", "eodhd", 1),
            ("owner", "eodhd", 2),
            ("owner", "massive", 1),
            ("owner", "yahoo", 1),
        ]


@pytest.mark.anyio
async def test_real_financial_and_operator_configuration_are_not_adopted_or_replaced(
    book: dict[str, Any], engine: Engine
) -> None:
    settings = book["settings"]
    operator = ProviderConfigurationStore(settings)
    old_eodhd = operator.load("eodhd")
    before_health = await check_configuration(settings, "massive")
    assert before_health.state == "UNCONFIGURED"
    before = database_snapshot(engine)
    fields = {"owner_id": book["owner"]["id"], "provider_id": "massive"}
    append(engine, **fields)
    append(engine, **declared(**fields, credential_artifact_id=str(uuid4())))
    after = database_snapshot(engine)
    assert {key: rows for key, rows in before.items() if key != TABLE} == {
        key: rows for key, rows in after.items() if key != TABLE
    }
    assert await check_configuration(settings, "massive") == before_health
    assert operator.unchanged(old_eodhd)
    assert not (settings.data_dir / "provider-config" / "massive.json").exists()


def test_downgrade_refuses_to_delete_retained_provider_history(profiles: Engine) -> None:
    append(profiles)
    before, definitions = database_snapshot(profiles), schema(profiles)
    with profiles.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated.*backup"):
            command.downgrade(config, "0011_source_schedules")
    assert database_snapshot(profiles) == before and schema(profiles) == definitions
