"""Managed source-context migration preserves all facts and enforces exact current DB provenance."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

import spm_api.providers.profiles as profiles
from spm_api.audit import canonical, digest
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert, seed_evidence
from tests.test_action_opening_migration import schema
from tests.test_provider_profile_migration import AT, declared, values
from tests.test_schedule_migration import upgrade_to
from tests.test_transfer_migration import dimensions, seed_buy

HEAD = "0013_managed_source_contexts"
ACTION = "MIGRATE_MANAGED_SOURCE_CONTEXTS"


def definition(con: Connection, *, owner: str | None = None) -> dict[str, Any]:
    owner = owner or str(uuid4())
    if con.exec_driver_sql("SELECT 1 FROM owners WHERE id=?", (owner,)).first() is None:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic-hash',?,?)",
            (owner, AT, AT),
        )
    first = values(owner_id=owner, provider_id="yahoo")
    second = {**first, "id": str(uuid4()), **declared()}
    insert(con, "provider_profile_revisions", first)
    insert(con, "provider_profile_revisions", second)
    return second


def context(con: Connection, template: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    selected = profiles.latest(con, UUID(record["owner_id"]), "yahoo")
    assert selected is not None
    wire = canonical(profiles.policy_snapshot(selected).model_dump(mode="json"))
    return {
        **template,
        "id": str(uuid4()),
        "owner_id": record["owner_id"],
        "profile_json": wire,
        "profile_sha256": digest(wire),
        "context_sha256": digest("managed-context-fixture"),
    }


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0008_provider_evidence")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
            seed_evidence(con, "owner", "stock")
        upgrade_to(engine, "0012_provider_profiles")
        with engine.begin() as con:
            definition(con)
        yield engine
    finally:
        engine.dispose()


def test_upgrade_keeps_every_fact_profile_legacy_context_and_key_boundary(
    old_engine: Engine, settings: Settings
) -> None:
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    present = settings.provider_secret_dir.exists()
    upgrade_to(old_engine, HEAD)
    after = database_snapshot(old_engine)
    assert set(before) == set(after)
    assert {
        name: rows
        for name, rows in before.items()
        if name not in {"audit_events", "alembic_version"}
    } == {
        name: rows
        for name, rows in after.items()
        if name not in {"audit_events", "alembic_version"}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
            ).scalar_one()
            == 3
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            ).scalar_one()
            == 51
        )
    changed = {row[1] for row in definitions if row not in schema(old_engine)}
    assert changed == {"trg_provider_context_insert"}
    assert not (settings.data_dir / "provider-config").exists()
    assert settings.provider_secret_dir.exists() is present
    assert settings.managed_provider_secret_dir is None and not settings.backup_dir.exists()
    accepted = database_snapshot(old_engine)
    upgrade_to(old_engine, HEAD)
    assert database_snapshot(old_engine) == accepted


@pytest.mark.parametrize("failure", ["ddl", "audit", "missing_guard", "unexpected_guard"])
def test_failed_upgrade_rolls_back_original_ddl_head_and_every_owner_audit(
    old_engine: Engine, settings: Settings, failure: str
) -> None:
    with old_engine.begin() as con:
        if failure == "ddl":
            con.exec_driver_sql(
                "CREATE TRIGGER trg_provider_configuration_context_insert "
                "BEFORE INSERT ON settings BEGIN SELECT 1; END"
            )
        elif failure == "audit":
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_reject_managed_context_audit BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
        else:
            installed = con.exec_driver_sql(
                "SELECT sql FROM sqlite_schema WHERE name='trg_provider_context_insert'"
            ).scalar_one()
            con.exec_driver_sql("DROP TRIGGER trg_provider_context_insert")
            if failure == "unexpected_guard":
                con.exec_driver_sql(
                    installed.replace(
                        "'credential_version','raw_payload_storage'",
                        "'credential_version', 'raw_payload_storage'",
                    )
                )
    before, original = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises((RuntimeError, OperationalError, IntegrityError)):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == original
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0012_provider_profiles"
        )


@pytest.fixture
def current(engine: Engine) -> dict[str, Any]:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        seed_buy(con)
        evidence = seed_evidence(con, "owner", "stock")
        record = definition(con)
        managed = context(con, evidence["context"], record)
    return {"context": managed, "record": record, "legacy": evidence["context"]}


def test_raw_sql_accepts_exact_current_owned_provenance_without_adopting_financial_data(
    engine: Engine, current: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", current["context"])
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name != "provider_fetch_contexts"} == {
        name: rows for name, rows in after.items() if name != "provider_fetch_contexts"
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"configuration_source": None},
        {"configuration_source": "OPERATOR"},
        {"configuration_source": "MANAGED_PROFILE\x00hidden"},
        {"configuration_source": True},
        {"revision_id": str(uuid4())},
        {"revision_id": None},
        {"revision_id": 123},
        {"owner_id": str(uuid4())},
        {"owner_id": "other"},
        {"owner_id": None},
        {"provider_id": "massive"},
        {"version": 1},
        {"version": 3},
        {"version": "2"},
        {"version": True},
        {"enabled": False},
        {"enabled": 1},
        {"terms_acknowledged": False},
        {"terms_reference": "Different rights reference"},
        {"terms_reference": None},
        {"terms_reference": 123},
        {"terms_reason": "Different explicit owner reason."},
        {"terms_reason": None},
        {"requests_per_minute": 2},
        {"requests_per_minute": 5.0},
        {"requests_per_day": 49},
        {"requests_per_day": None},
        {"credential_artifact_id": str(uuid4())},
        {"credential_artifact_id": {}},
        {"raw_payload_storage": True},
        {"credential_source": None},
        {"credential_version": 1},
        {"api_key": "synthetic-never-allowed-context-input"},
        {"change_reason": "Unexpected non-policy field."},
    ],
)
def test_managed_context_requires_exact_types_fields_owner_current_revision_and_terms(
    engine: Engine, current: dict[str, Any], changes: dict[str, Any]
) -> None:
    values = dict(current["context"])
    values["profile_json"] = canonical({**json.loads(values["profile_json"]), **changes})
    before = database_snapshot(engine)
    with (
        pytest.raises(IntegrityError, match="INVALID_PROVIDER_CONTEXT"),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, "provider_fetch_contexts", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field",
    [
        "configuration_source",
        "revision_id",
        "owner_id",
        "credential_artifact_id",
        "requests_per_day",
        "terms_reason",
    ],
)
def test_managed_snapshot_cannot_omit_nullable_or_required_identity_fields(
    engine: Engine, current: dict[str, Any], field: str
) -> None:
    values = dict(current["context"])
    body = json.loads(values["profile_json"])
    del body[field]
    values["profile_json"] = canonical(body)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["version", "configuration_source", "revision_id"])
def test_duplicate_json_keys_and_nul_suffixed_revision_identity_are_not_provenance(
    engine: Engine, current: dict[str, Any], field: str
) -> None:
    values = dict(current["context"])
    body = json.loads(values["profile_json"])
    values["profile_json"] = (
        values["profile_json"][:-1] + "," + canonical(field) + ":" + canonical(body[field]) + "}"
    )
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", values)
    assert database_snapshot(engine) == before
    if field == "revision_id":
        values["profile_json"] = canonical(
            {**body, "revision_id": body["revision_id"] + "\x00hidden"}
        )
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            insert(con, "provider_fetch_contexts", values)
        assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["inactive", "new_version", "outer_owner"])
def test_managed_context_cannot_use_inactive_foreign_or_superseded_authority(
    engine: Engine, current: dict[str, Any], failure: str
) -> None:
    value = dict(current["context"])
    with unit_of_work(engine, write=True) as con:
        if failure == "inactive":
            con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (value["owner_id"],))
        elif failure == "new_version":
            insert(
                con,
                "provider_profile_revisions",
                {**current["record"], "id": str(uuid4()), "version": 3},
            )
        else:
            value["owner_id"] = "other"
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", value)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("source", ["legacy", "context"])
def test_context_insert_or_replace_never_rewrites_unreferenced_history(
    engine: Engine, current: dict[str, Any], source: str
) -> None:
    value = {**current[source], "id": str(uuid4())}
    with unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", value)
    before = database_snapshot(engine)
    columns = ",".join(value)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO provider_fetch_contexts({columns}) "
            f"VALUES ({','.join('?' for _ in value)})",
            tuple(value.values()),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "field", ["configuration_source", "owner_id", "revision_id", "credential_artifact_id"]
)
def test_legacy_snapshot_cannot_smuggle_managed_identity_without_its_source_kind(
    engine: Engine, current: dict[str, Any], field: str
) -> None:
    values = {**current["legacy"], "id": str(uuid4())}
    values["profile_json"] = canonical({**json.loads(values["profile_json"]), field: None})
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "provider_fetch_contexts", values)
    assert database_snapshot(engine) == before
