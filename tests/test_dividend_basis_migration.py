"""Basis declarations are inert, append-only and fenced against real source/action rows."""

import json
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.audit import canonical, digest
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, unit_of_work
from spm_api.decimal_types import to_scaled
from spm_api.dividend_basis import SplitBasisAssertion, SplitReference
from spm_api.providers.base import DividendObservation
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert, seed_evidence
from tests.test_action_opening_migration import action, schema
from tests.test_schedule_migration import upgrade_to
from tests.test_transfer_migration import dimensions, seed_buy

HEAD = "0014_dividend_basis_assertions"
TABLE = "dividend_basis_assertions"
ACTION = "MIGRATE_DIVIDEND_BASIS"
AT = "2026-11-07T00:00:00.000000Z"
SECURITY = UUID("b0000000-0000-4000-8000-000000000001")


def source(con: Connection, *, basis: str = "SPLIT_ADJUSTED", **changes: Any) -> dict[str, Any]:
    evidence = seed_evidence(con, "owner", str(SECURITY), identifier=str(uuid4()))
    run = {**evidence["run"], "id": str(uuid4())}
    insert(con, "provider_runs", run)
    original = DividendObservation.model_validate(
        {
            "provider_id": "yahoo",
            "security_id": SECURITY,
            "provider_symbol": "SYNTH",
            "ex_date": "2026-01-08",
            "dps": "0.60000000",
            "currency": "USD",
            "adjustment_basis": basis,
        }
    )
    observation = {
        **evidence["observation"],
        "id": str(uuid4()),
        "provider_run_id": run["id"],
        "dps_e8": 60000000,
        "currency": "USD",
        "adjustment_basis": basis,
        "normalized_json": canonical(original.model_dump(mode="json")),
        "normalized_sha256": original.normalized_sha256,
        **changes,
    }
    insert(con, "dividend_observations", observation)
    insert(
        con,
        "provider_observation_sightings",
        {"run_id": run["id"], "ordinal": 0, "dividend_observation_id": observation["id"]},
    )
    return observation


def cloned_source(con: Connection, observed: dict[str, Any], **terms: Any) -> dict[str, Any]:
    old = dict(
        con.exec_driver_sql(
            "SELECT * FROM dividend_observations WHERE id=?", (observed["observation_id"],)
        )
        .mappings()
        .one()
    )
    run = dict(
        con.exec_driver_sql("SELECT * FROM provider_runs WHERE id=?", (old["provider_run_id"],))
        .mappings()
        .one()
    )
    run["id"] = str(uuid4())
    insert(con, "provider_runs", run)
    original = DividendObservation.model_validate({**json.loads(old["normalized_json"]), **terms})
    newer = {
        **old,
        "id": str(uuid4()),
        "provider_run_id": run["id"],
        "dps_e8": to_scaled(original.dps, 8) if original.dps is not None else None,
        "currency": original.currency,
        "adjustment_basis": original.adjustment_basis,
        "normalized_json": canonical(original.model_dump(mode="json")),
        "normalized_sha256": original.normalized_sha256,
    }
    insert(con, "dividend_observations", newer)
    insert(
        con,
        "provider_observation_sightings",
        {
            "run_id": run["id"],
            "ordinal": 0,
            "dividend_observation_id": newer["id"],
        },
    )
    return {**observed, "observation_id": newer["id"], "source_sha256": newer["normalized_sha256"]}


def declaration(con: Connection, observation: dict[str, Any]) -> dict[str, Any]:
    rows = con.exec_driver_sql(
        "SELECT * FROM corporate_actions WHERE security_id=? AND is_current=1 "
        "AND status='CONFIRMED' AND effective_date BETWEEN '2026-01-08' AND '2026-01-10' "
        "AND action_type IN ('SPLIT','REVERSE_SPLIT') ORDER BY effective_date,id",
        (str(SECURITY),),
    ).mappings()
    references = tuple(
        SplitReference(
            id=UUID(row["id"]),
            root_id=UUID(row["root_id"]),
            security_id=SECURITY,
            version=row["version"],
            action_type=row["action_type"],
            effective_date=row["effective_date"],
            ratio_numerator=row["ratio_num"],
            ratio_denominator=row["ratio_den"],
            status="CONFIRMED",
            lifecycle_status="CURRENT",
        )
        for row in rows
    )
    assertion = SplitBasisAssertion(
        id=uuid4(),
        observation_id=UUID(observation["id"]),
        source_sha256=observation["normalized_sha256"],
        security_id=SECURITY,
        ex_date=observation["ex_date"],
        share_basis_date="2026-01-10",
        source_basis="AFTER_EFFECTIVE_DATE_ACTIONS",
        target_basis="PRE_EX_DATE_EVENTS",
        complete=True,
        split_only=True,
        splits=references,
        citation="Synthetic declared split-only scope, not verified market history",
        reason="The synthetic owner explicitly declares the source share-unit scope.",
    )
    wire = canonical(assertion.model_dump(mode="json"))
    return {
        "id": str(assertion.id),
        "owner_id": "owner",
        "observation_id": str(assertion.observation_id),
        "source_sha256": assertion.source_sha256,
        "security_id": str(SECURITY),
        "ex_date": assertion.ex_date,
        "share_basis_date": assertion.share_basis_date,
        "assertion_json": wire,
        "assertion_sha256": digest(wire),
        "request_id": str(uuid4()),
        "created_at": AT,
    }


def seeded(con: Connection) -> dict[str, Any]:
    dimensions(con)
    seed_buy(con)
    insert(
        con,
        "securities",
        {
            "id": str(SECURITY),
            "exchange_code": "NASDAQ",
            "display_ticker": "BASIS",
            "name": "Synthetic basis security",
            "quote_currency": "USD",
            "created_at": AT,
        },
    )
    action(con, str(uuid4()), security_id=str(SECURITY), effective_date="2026-01-08")
    return declaration(con, source(con))


@pytest.fixture
def basis(engine: Engine) -> dict[str, Any]:
    with unit_of_work(engine, write=True) as con:
        return seeded(con)


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0013_managed_source_contexts")
        with engine.begin() as con:
            seeded(con)
        yield engine
    finally:
        engine.dispose()


def body(values: dict[str, Any], **changes: Any) -> dict[str, Any]:
    parsed = {**json.loads(values["assertion_json"]), **changes}
    wire = canonical(parsed)
    return {**values, "assertion_json": wire, "assertion_sha256": digest(wire)}


def rejected(engine: Engine, values: dict[str, Any]) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_upgrade_adds_only_an_empty_declaration_store_and_schema_audits(
    old_engine: Engine, settings: Settings
) -> None:
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    present = settings.provider_secret_dir.exists()
    upgrade_to(old_engine, HEAD)
    after = database_snapshot(old_engine)
    assert set(after) == {*before, TABLE}
    assert after[TABLE] == []
    assert {
        name: after[name] for name in before if name not in {"audit_events", "alembic_version"}
    } == {
        name: rows
        for name, rows in before.items()
        if name not in {"audit_events", "alembic_version"}
    }
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert all(row in schema(old_engine) for row in definitions if row[1] != "alembic_version")
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action=?", (ACTION,)
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            ).scalar_one()
            == 52
        )
    assert settings.provider_secret_dir.exists() is present
    assert not (settings.data_dir / "provider-config").exists()
    assert not settings.backup_dir.exists() and settings.managed_provider_secret_dir is None
    upgrade_to(old_engine, HEAD)
    assert database_snapshot(old_engine) == after


@pytest.mark.parametrize("failure", ["ddl", "second_owner_audit"])
def test_failed_upgrade_restores_all_schema_head_facts_and_audits(
    old_engine: Engine, failure: str
) -> None:
    with old_engine.begin() as con:
        if failure == "ddl":
            con.exec_driver_sql("CREATE INDEX idx_dividend_basis_source ON settings(owner_id)")
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_reject_basis_audit BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        upgrade_to(old_engine, HEAD)
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0013_managed_source_contexts"
        )


def test_exact_current_source_and_complete_action_snapshot_are_retained_without_adoption(
    engine: Engine, basis: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, basis)
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name != TABLE} == {
        name: rows for name, rows in after.items() if name != TABLE
    }
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()
        assert dict(row) == basis
        parsed = SplitBasisAssertion.model_validate_json(row["assertion_json"])
        assert parsed.source_sha256 == basis["source_sha256"] and len(parsed.splits) == 1
    assert len(after[TABLE]) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-uuid"),
        ("id", str(uuid4()) + "\0tail"),
        ("owner_id", "missing-owner"),
        ("observation_id", str(uuid4())),
        ("source_sha256", "0" * 64),
        ("source_sha256", "F" * 64),
        ("source_sha256", "0" * 64 + "\0tail"),
        ("security_id", "stock"),
        ("ex_date", "2026-01-09"),
        ("ex_date", "2026-02-30"),
        ("share_basis_date", "2026-02-30"),
        ("share_basis_date", "0000-01-01"),
        ("share_basis_date", "2026-01-10\0tail"),
        ("assertion_sha256", "G" * 64),
        ("assertion_sha256", "0" * 63),
        ("request_id", "request-not-uuid"),
        ("request_id", str(uuid4()) + "\0tail"),
        ("created_at", "2026-11-07T00:00:00Z"),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2026-11-07T24:00:00.000000Z"),
        ("created_at", "2026-11-07T00:00:00.000000Z\0tail"),
    ],
)
def test_invalid_or_mismatched_row_metadata_is_atomic(
    engine: Engine, basis: dict[str, Any], field: str, value: Any
) -> None:
    rejected(engine, {**basis, field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", str(uuid4())),
        ("observation_id", str(uuid4())),
        ("source_sha256", "0" * 64),
        ("security_id", str(uuid4())),
        ("ex_date", "2026-01-09"),
        ("share_basis_date", "2026-01-09"),
        ("source_basis", "BEFORE_EFFECTIVE_DATE_ACTIONS"),
        ("target_basis", "EX_DATE_AFTER_ACTIONS"),
        ("complete", False),
        ("complete", 1),
        ("complete", "true"),
        ("split_only", False),
        ("split_only", 1),
        ("citation", ""),
        ("citation", "bad\0reference"),
        ("citation", "bad\nreference"),
        ("reason", "short"),
        ("reason", "Synthetic bad\0reason"),
        ("reason", "x" * 1001),
        ("url", 7),
        ("url", "https://example.com/\0tail"),
        ("splits", None),
        ("splits", {}),
        ("splits", ["malformed-json-atom"]),
        ("splits", [None]),
        ("extra", "unknown key"),
    ],
)
def test_invalid_json_mirrors_authority_flags_or_references_fail_closed(
    engine: Engine, basis: dict[str, Any], field: str, value: Any
) -> None:
    rejected(engine, body(basis, **{field: value}))


@pytest.mark.parametrize("key", ["id", "splits", "complete", "source_basis", "url"])
def test_missing_or_duplicate_json_keys_are_rejected(
    engine: Engine, basis: dict[str, Any], key: str
) -> None:
    payload = json.loads(basis["assertion_json"])
    del payload[key]
    rejected(engine, {**basis, "assertion_json": canonical(payload)})
    payload = basis["assertion_json"][:-1] + f',"{key}":null}}'
    rejected(engine, {**basis, "assertion_json": payload})


@pytest.mark.parametrize("payload", ["not-json", "[]", "null", "7", "{}", '{"complete":NaN}'])
def test_malformed_or_non_object_json_is_rejected_without_partial_work(
    engine: Engine, basis: dict[str, Any], payload: str
) -> None:
    rejected(engine, {**basis, "assertion_json": payload})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", str(uuid4())),
        ("root_id", str(uuid4())),
        ("security_id", str(uuid4())),
        ("version", 2),
        ("version", True),
        ("version", "1"),
        ("action_type", "REVERSE_SPLIT"),
        ("effective_date", "2026-01-09"),
        ("ratio_numerator", 4),
        ("ratio_numerator", "3"),
        ("ratio_numerator", 3.0),
        ("ratio_numerator", 9223372036854775808),
        ("ratio_denominator", 0),
        ("ratio_denominator", 2.0),
        ("status", "NEEDS_REVIEW"),
        ("lifecycle_status", "SUPERSEDED"),
        ("extra", "unknown key"),
    ],
)
def test_declared_action_fields_must_match_actual_current_confirmed_rows(
    engine: Engine, basis: dict[str, Any], field: str, value: Any
) -> None:
    payload = json.loads(basis["assertion_json"])
    payload["splits"][0][field] = value
    rejected(engine, body(basis, splits=payload["splits"]))


@pytest.mark.parametrize("change", ["void", "supersede", "inactive_owner"])
def test_post_observation_action_or_owner_changes_fence_a_new_declaration(
    engine: Engine, basis: dict[str, Any], change: str
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    with unit_of_work(engine, write=True) as con:
        if change == "inactive_owner":
            con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id='owner'")
        else:
            assignments = {
                "void": "status='VOID',version=version+1",
                "supersede": "is_current=0,version=version+1",
            }
            con.exec_driver_sql(
                f"UPDATE corporate_actions SET {assignments[change]} WHERE id=?", (reference["id"],)
            )
    rejected(engine, basis)


@pytest.mark.parametrize("kind", ["SPLIT", "REVERSE_SPLIT", "CASH_IN_LIEU", "MERGER", "SPIN_OFF"])
def test_known_omitted_or_unallocated_quantity_actions_block_a_complete_claim(
    engine: Engine, basis: dict[str, Any], kind: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        action(
            con,
            str(uuid4()),
            security_id=str(SECURITY),
            effective_date="2026-01-09",
            action_type=kind,
            ratio_num=1 if kind == "REVERSE_SPLIT" else 3 if kind == "SPLIT" else None,
            ratio_den=2 if kind in {"SPLIT", "REVERSE_SPLIT"} else None,
            status="NEEDS_REVIEW" if kind not in {"SPLIT", "REVERSE_SPLIT"} else "CONFIRMED",
        )
    rejected(engine, basis)


def test_known_unconfirmed_split_is_not_silently_ignored(
    engine: Engine, basis: dict[str, Any]
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?",
            (reference["id"],),
        )
        action(
            con,
            str(uuid4()),
            security_id=str(SECURITY),
            root_id=reference["root_id"],
            supersedes_id=reference["id"],
            revision=2,
            effective_date=reference["effective_date"],
            status="NEEDS_REVIEW",
        )
    rejected(engine, body(basis, splits=[]))


def test_duplicate_physical_or_root_references_and_overlong_chain_are_rejected(
    engine: Engine, basis: dict[str, Any]
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    for references in (
        [reference, reference],
        [reference, {**reference, "id": str(uuid4())}],
        [reference] * 101,
    ):
        rejected(engine, body(basis, splits=references))


@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
def test_retained_declarations_cannot_be_changed_deleted_or_replaced(
    engine: Engine, basis: dict[str, Any], operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, basis)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "replace":
            columns = ",".join(basis)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) "
                f"VALUES ({','.join('?' for _ in basis)})",
                tuple(basis.values()),
            )
        else:
            con.exec_driver_sql(
                f"UPDATE {TABLE} SET id=id" if operation == "update" else f"DELETE FROM {TABLE}"
            )
    assert database_snapshot(engine) == before


def test_later_action_revision_preserves_historical_assertion_but_blocks_new_use(
    engine: Engine, basis: dict[str, Any]
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, basis)
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?",
            (reference["id"],),
        )
        action(
            con,
            str(uuid4()),
            security_id=str(SECURITY),
            root_id=reference["root_id"],
            supersedes_id=reference["id"],
            revision=2,
            effective_date=reference["effective_date"],
            ratio_num=2,
            ratio_den=1,
        )
    new_id = str(uuid4())
    rejected(engine, body({**basis, "id": new_id}, id=new_id))
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(f"SELECT assertion_json FROM {TABLE}").scalar_one()
            == basis["assertion_json"]
        )


def test_current_physical_version_is_not_the_retired_root_version(
    engine: Engine, basis: dict[str, Any]
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    identifier = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id=?",
            (reference["id"],),
        )
        action(
            con,
            identifier,
            security_id=str(SECURITY),
            root_id=reference["root_id"],
            supersedes_id=reference["id"],
            revision=2,
            effective_date=reference["effective_date"],
        )
    stale = {**reference, "id": identifier, "version": 2}
    rejected(engine, body(basis, splits=[stale]))
    accepted = body(basis, splits=[{**stale, "version": 1}])
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, accepted)


@pytest.mark.parametrize(
    ("basis_day", "action_day", "included"),
    [
        ("2026-01-10", "2026-01-07", False),
        ("2026-01-10", "2026-01-08", True),
        ("2026-01-10", "2026-01-10", True),
        ("2026-01-10", "2026-01-11", False),
        ("2026-01-05", "2026-01-05", False),
        ("2026-01-05", "2026-01-06", True),
        ("2026-01-05", "2026-01-07", True),
        ("2026-01-05", "2026-01-08", False),
        ("2026-01-08", "2026-01-07", False),
        ("2026-01-08", "2026-01-08", True),
        ("2026-01-08", "2026-01-09", False),
        ("0001-01-01", "2026-01-08", False),
        ("9999-12-31", "2026-01-08", True),
    ],
)
def test_sql_uses_the_same_exact_pre_ex_date_interval_as_the_pure_core(
    engine: Engine, basis: dict[str, Any], basis_day: str, action_day: str, included: bool
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    identifier = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET status='VOID',version=version+1 WHERE id=?",
            (reference["id"],),
        )
        action(con, identifier, security_id=str(SECURITY), effective_date=action_day)
    reference = {**reference, "id": identifier, "root_id": identifier, "effective_date": action_day}
    observed = {**basis, "share_basis_date": basis_day}
    rejected(
        engine, body(observed, share_basis_date=basis_day, splits=[] if included else [reference])
    )
    accepted = body(observed, share_basis_date=basis_day, splits=[reference] if included else [])
    assert SplitBasisAssertion.model_validate_json(accepted["assertion_json"])
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, accepted)


@pytest.mark.parametrize("kind", ["outside", "foreign", "display_only", "void"])
def test_unrelated_or_non_quantity_actions_do_not_invent_a_blocker(
    engine: Engine, basis: dict[str, Any], kind: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        identifier = str(uuid4())
        if kind == "display_only":
            action(
                con,
                identifier,
                security_id=str(SECURITY),
                action_type="SYMBOL_CHANGE",
                effective_date="2026-01-09",
                ratio_num=None,
                ratio_den=None,
                status="NEEDS_REVIEW",
                new_symbol="EXPLICIT",
            )
        else:
            action(
                con,
                identifier,
                security_id="stock" if kind == "foreign" else str(SECURITY),
                action_type="MERGER",
                effective_date="2026-01-11" if kind == "outside" else "2026-01-09",
                ratio_num=None,
                ratio_den=None,
                status="NEEDS_REVIEW",
            )
            if kind == "void":
                con.exec_driver_sql(
                    "UPDATE corporate_actions SET status='VOID',version=version+1 WHERE id=?",
                    (identifier,),
                )
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, basis)


@pytest.mark.parametrize("adjustment_basis", ["UNKNOWN", "AS_DECLARED"])
def test_original_basis_must_really_be_split_adjusted(
    engine: Engine, basis: dict[str, Any], adjustment_basis: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        observed = cloned_source(con, basis, adjustment_basis=adjustment_basis)
    rejected(
        engine,
        body(
            observed,
            observation_id=observed["observation_id"],
            source_sha256=observed["source_sha256"],
        ),
    )


@pytest.mark.parametrize("missing", ["dps", "currency", "both"])
def test_retaining_a_declaration_never_fills_missing_source_terms(
    engine: Engine, basis: dict[str, Any], missing: str
) -> None:
    terms = {name: None for name in ("dps", "currency") if missing in {name, "both"}}
    with unit_of_work(engine, write=True) as con:
        observed = cloned_source(con, basis, **terms)
    before = database_snapshot(engine)
    accepted = body(
        observed, observation_id=observed["observation_id"], source_sha256=observed["source_sha256"]
    )
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, accepted)
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name != TABLE} == {
        name: rows for name, rows in after.items() if name != TABLE
    }


def test_one_hundred_exact_refs_fit_without_truncation(
    engine: Engine, basis: dict[str, Any]
) -> None:
    reference = json.loads(basis["assertion_json"])["splits"][0]
    references = [reference]
    last = date(2026, 1, 8)
    with unit_of_work(engine, write=True) as con:
        for number in range(1, 100):
            last = date(2026, 1, 8) + timedelta(days=number)
            identifier = str(uuid4())
            action(con, identifier, security_id=str(SECURITY), effective_date=last.isoformat())
            references.append(
                {
                    **reference,
                    "id": identifier,
                    "root_id": identifier,
                    "effective_date": last.isoformat(),
                }
            )
        accepted = body(
            {**basis, "share_basis_date": last.isoformat()},
            share_basis_date=last.isoformat(),
            splits=references,
        )
        assert (
            len(SplitBasisAssertion.model_validate_json(accepted["assertion_json"]).splits) == 100
        )
        insert(con, TABLE, accepted)


def test_payload_limit_is_in_bytes_even_for_multibyte_citations(
    engine: Engine, basis: dict[str, Any]
) -> None:
    wire = body(basis, citation="\U0001f642" * 300)["assertion_json"]
    payload = wire + " " * (131000 - len(wire))
    assert len(payload) < 131072 < len(payload.encode())
    rejected(engine, {**basis, "assertion_json": payload})


def test_missing_and_duplicate_action_fields_and_escaped_keys_fail_closed(
    engine: Engine, basis: dict[str, Any]
) -> None:
    payload = json.loads(basis["assertion_json"])
    reference = payload["splits"][0]
    for key in reference:
        fewer = {name: value for name, value in reference.items() if name != key}
        rejected(engine, body(basis, splits=[fewer]))
    for raw in (
        basis["assertion_json"].replace('"version":1', '"version":1,"version":1'),
        basis["assertion_json"].replace('"complete":true', '"complete":true,"compl\\u0065te":true'),
        basis["assertion_json"].replace('"security_id":', '"security_id\\u0000":', 1),
    ):
        assert raw != basis["assertion_json"]
        rejected(engine, {**basis, "assertion_json": raw})


def test_legacy_evidence_stays_unadmitted_after_upgrade(settings: Settings) -> None:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0007_actions_opening")
        with engine.begin() as con:
            dimensions(con)
            insert(
                con,
                "securities",
                {
                    "id": str(SECURITY),
                    "exchange_code": "NASDAQ",
                    "display_ticker": "BASIS",
                    "name": "Synthetic legacy basis security",
                    "quote_currency": "USD",
                    "created_at": AT,
                },
            )
            action(con, str(uuid4()), security_id=str(SECURITY), effective_date="2026-01-08")
            run_id = str(uuid4())
            insert(
                con,
                "provider_runs",
                {
                    "id": run_id,
                    "provider_id": "yahoo",
                    "origin_group": "YAHOO",
                    "security_id": str(SECURITY),
                    "started_at": AT,
                    "finished_at": AT,
                    "status": "OK_WITH_DATA",
                    "observations_count": 1,
                },
            )
            original = DividendObservation(
                provider_id="yahoo",
                security_id=SECURITY,
                provider_symbol="SYNTH",
                ex_date="2026-01-08",
                dps="0.60000000",
                currency="USD",
                adjustment_basis="SPLIT_ADJUSTED",
            )
            legacy = {
                "id": str(uuid4()),
                "provider_run_id": run_id,
                "provider_id": "yahoo",
                "origin_group": "YAHOO",
                "security_id": str(SECURITY),
                "provider_symbol": "SYNTH",
                "normalized_sha256": original.normalized_sha256,
                "ex_date": "2026-01-08",
                "dps_e8": 60000000,
                "currency": "USD",
                "adjustment_basis": "SPLIT_ADJUSTED",
                "retrieved_at": AT,
            }
            insert(con, "dividend_observations", legacy)
            proposed = declaration(con, legacy)
        migrate(settings)
        rejected(engine, proposed)
        with unit_of_work(engine) as con:
            assert con.exec_driver_sql(
                "SELECT provenance_version,normalized_json FROM dividend_observations WHERE id=?",
                (legacy["id"],),
            ).one() == (0, None)
    finally:
        engine.dispose()


def test_downgrade_refuses_to_delete_retained_declarations(old_engine: Engine) -> None:
    upgrade_to(old_engine, HEAD)
    with old_engine.begin() as con:
        observed = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_observations WHERE security_id=? AND dps_e8=60000000",
                (str(SECURITY),),
            )
            .mappings()
            .one()
        )
        insert(con, TABLE, declaration(con, observed))
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with old_engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated"):
            command.downgrade(config, "0013_managed_source_contexts")
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions
