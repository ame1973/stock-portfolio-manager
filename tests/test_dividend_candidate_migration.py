"""Candidate decisions are inert, append-only source associations, not canonical income."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.audit import canonical, digest
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_basis_migration import SECURITY, cloned_source, seeded
from tests.test_schedule_migration import upgrade_to

HEAD = "0015_dividend_candidates"
TABLE = "dividend_candidate_revisions"
ACTION = "MIGRATE_DIVIDEND_CANDIDATES"
AT = "2026-11-09T00:00:00.000000Z"
REASON = "Explicit candidate association for later human review."


@pytest.fixture
def source(engine: Engine) -> dict[str, Any]:
    with unit_of_work(engine, write=True) as con:
        original = seeded(con)
        insert(con, "dividend_basis_assertions", original)
    return original


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0014_dividend_basis_assertions")
        with engine.begin() as con:
            original = seeded(con)
            insert(con, "dividend_basis_assertions", original)
        yield engine
    finally:
        engine.dispose()


def match(source: dict[str, Any], *, basis: bool = False) -> dict[str, Any]:
    return {
        "observation_id": source["observation_id"],
        "source_sha256": source["source_sha256"],
        "basis_assertion_id": source["id"] if basis else None,
        "reason": REASON,
    }


def candidate(source: dict[str, Any], *, basis: bool = False, **changes: Any) -> dict[str, Any]:
    wire = canonical([match(source, basis=basis)])
    return {
        "id": str(uuid4()),
        "candidate_id": str(uuid4()),
        "owner_id": "owner",
        "security_id": str(SECURITY),
        "revision_no": 1,
        "state": "OPEN",
        "event_type": "UNKNOWN",
        "ex_date": "2026-01-08",
        "announce_date": None,
        "record_date": None,
        "pay_date": None,
        "dps_e8": 60000000,
        "currency": "USD",
        "adjustment_basis": "SPLIT_ADJUSTED",
        "matches_json": wire,
        "matches_sha256": digest(wire),
        "change_reason": REASON,
        "request_id": str(uuid4()),
        "created_at": AT,
        **changes,
    }


def with_matches(values: dict[str, Any], matches: Any) -> dict[str, Any]:
    wire = canonical(matches)
    return {**values, "matches_json": wire, "matches_sha256": digest(wire)}


def rejected(engine: Engine, values: dict[str, Any], *, replace: bool = False) -> None:
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if replace:
            columns = ",".join(values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {TABLE} ({columns}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        else:
            insert(con, TABLE, values)
    assert database_snapshot(engine) == before


def test_new_current_schema_exposes_only_an_empty_candidate_revision_store(engine: Engine) -> None:
    with unit_of_work(engine) as con:
        assert schema_head() == HEAD
        assert con.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            ).scalar_one()
            == 53
        )


def test_upgrade_preserves_all_old_facts_adds_one_empty_table_and_audits_each_owner(
    old_engine: Engine, settings: Settings
) -> None:
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    secret_dir_present = settings.provider_secret_dir.exists()
    migrate(settings)
    after = database_snapshot(old_engine)
    assert set(after) == {*before, TABLE} and after[TABLE] == []
    assert {
        name: rows
        for name, rows in before.items()
        if name not in {"audit_events", "alembic_version"}
    } == {
        name: rows
        for name, rows in after.items()
        if name not in {"audit_events", "alembic_version", TABLE}
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
    assert settings.provider_secret_dir.exists() is secret_dir_present
    assert not settings.backup_dir.exists()
    migrate(settings)
    assert database_snapshot(old_engine) == after


@pytest.mark.parametrize("failure", ["ddl", "second-owner-audit"])
def test_failed_upgrade_rolls_back_schema_facts_and_audits(
    old_engine: Engine, settings: Settings, failure: str
) -> None:
    with old_engine.begin() as con:
        if failure == "ddl":
            con.exec_driver_sql("CREATE INDEX idx_dividend_candidate_owner ON settings(owner_id)")
        else:
            con.exec_driver_sql(f"""
                CREATE TRIGGER test_candidate_audit_failure BEFORE INSERT ON audit_events
                WHEN NEW.action='{ACTION}'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action='{ACTION}')
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_AUDIT_FAILURE'); END
            """)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0014_dividend_basis_assertions"
        )


def test_exact_source_and_basis_are_snapshotted_without_any_other_table_change(
    engine: Engine, source: dict[str, Any]
) -> None:
    values = candidate(source, basis=True)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    after = database_snapshot(engine)
    assert {name: rows for name, rows in before.items() if name != TABLE} == {
        name: rows for name, rows in after.items() if name != TABLE
    }
    with unit_of_work(engine) as con:
        assert dict(con.exec_driver_sql(f"SELECT * FROM {TABLE}").mappings().one()) == values
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_events").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0


def test_same_day_distinct_candidates_and_dismissal_revisions_retain_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = candidate(source, event_type="REGULAR")
    second = candidate(source, event_type="SPECIAL")
    dismissed = {
        **first,
        "id": str(uuid4()),
        "revision_no": 2,
        "state": "DISMISSED",
        "change_reason": "Explicitly dismiss this synthetic candidate.",
    }
    with unit_of_work(engine, write=True) as con:
        for row in (first, second, dismissed):
            insert(con, TABLE, row)
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            f"SELECT candidate_id,revision_no,state,event_type FROM {TABLE} ORDER BY rowid"
        ).all()
        assert [tuple(row) for row in rows] == [
            (first["candidate_id"], 1, "OPEN", "REGULAR"),
            (second["candidate_id"], 1, "OPEN", "SPECIAL"),
            (first["candidate_id"], 2, "DISMISSED", "REGULAR"),
        ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-uuid"),
        ("id", str(uuid4()) + "\0tail"),
        ("candidate_id", "not-a-uuid"),
        ("candidate_id", str(uuid4()) + "\0tail"),
        ("owner_id", "missing-owner"),
        ("security_id", str(uuid4())),
        ("revision_no", 0),
        ("revision_no", 2),
        ("revision_no", 1.5),
        ("revision_no", 2147483648),
        ("state", "VERIFIED"),
        ("event_type", "invented"),
        ("ex_date", "2026-02-30"),
        ("ex_date", "0000-01-01"),
        ("announce_date", "2026-02-30"),
        ("record_date", "2026-1-1"),
        ("pay_date", "2026-02-30"),
        ("dps_e8", -1),
        ("dps_e8", 0.5),
        ("currency", "EUR"),
        ("adjustment_basis", "VERIFIED"),
        ("matches_sha256", "0" * 63),
        ("matches_sha256", "F" * 64),
        ("change_reason", "short"),
        ("change_reason", " x " + "a" * 10),
        ("change_reason", "Synthetic\0change reason"),
        ("request_id", "not-uuid"),
        ("created_at", "2026-11-09T00:00:00Z"),
        ("created_at", "2026-02-30T00:00:00.000000Z"),
        ("created_at", "2026-11-09T24:00:00.000000Z"),
    ],
)
def test_invalid_identity_terms_and_metadata_never_persist(
    engine: Engine, source: dict[str, Any], field: str, value: Any
) -> None:
    rejected(engine, candidate(source, **{field: value}))


def test_nullable_terms_zero_and_max_int64_are_distinct_and_preserved(
    engine: Engine, source: dict[str, Any]
) -> None:
    rows = [
        candidate(source, ex_date=None, dps_e8=None, currency=None, adjustment_basis="UNKNOWN"),
        candidate(source, dps_e8=0, currency="HKD", adjustment_basis="AS_DECLARED"),
        candidate(source, dps_e8=9223372036854775807, currency=None),
    ]
    with unit_of_work(engine, write=True) as con:
        for row in rows:
            insert(con, TABLE, row)
    with unit_of_work(engine) as con:
        actual = con.exec_driver_sql(
            f"SELECT dps_e8,currency,adjustment_basis FROM {TABLE} ORDER BY rowid"
        ).all()
        assert [tuple(row) for row in actual] == [
            (None, None, "UNKNOWN"),
            (0, "HKD", "AS_DECLARED"),
            (9223372036854775807, None, "SPLIT_ADJUSTED"),
        ]


@pytest.mark.parametrize(
    "change", ["gap", "owner", "security", "clock", "replace-id", "replace-version"]
)
def test_version_fences_prevent_cross_owner_identity_hijack_and_replacement(
    engine: Engine, source: dict[str, Any], change: str
) -> None:
    initial = candidate(source)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, initial)
    next_row = {**initial, "id": str(uuid4()), "revision_no": 2}
    if change == "gap":
        next_row["revision_no"] = 3
    elif change == "owner":
        next_row["owner_id"] = "other"
    elif change == "security":
        next_row["security_id"] = "stock"
    elif change == "clock":
        next_row["created_at"] = "2026-11-08T23:59:59.999999Z"
    elif change == "replace-id":
        next_row["id"] = initial["id"]
    else:
        next_row["revision_no"] = 1
    rejected(engine, next_row, replace=change.startswith("replace"))


@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_candidate_history_is_immutable(
    engine: Engine, source: dict[str, Any], action: str
) -> None:
    initial = candidate(source)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, initial)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        statement = (
            f"UPDATE {TABLE} SET state='DISMISSED' WHERE id=?"
            if action == "UPDATE"
            else f"DELETE FROM {TABLE} WHERE id=?"
        )
        con.exec_driver_sql(statement, (initial["id"],))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "object",
        "scalar",
        "malformed",
        "duplicate-id",
        "unknown-key",
        "missing-key",
        "null-entry",
        "bad-id",
        "foreign-source",
        "hash",
        "basis",
        "wrong-basis-source",
        "reason",
        "reason-nul",
        "oversized",
    ],
)
def test_invalid_source_associations_fail_without_partial_rows(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    values, item = candidate(source), match(source)
    if problem == "object":
        values = with_matches(values, item)
    elif problem == "scalar":
        values = with_matches(values, "invalid")
    elif problem == "malformed":
        values["matches_json"] = "[broken"
    elif problem == "duplicate-id":
        values = with_matches(values, [item, item])
    elif problem == "null-entry":
        values = with_matches(values, [None])
    elif problem == "oversized":
        values = with_matches(values, [{**item, "reason": "x" * 131073}])
    else:
        if problem == "unknown-key":
            item["extra"] = "untrusted"
        elif problem == "missing-key":
            del item["basis_assertion_id"]
        elif problem == "bad-id":
            item["observation_id"] = "bad-id"
        elif problem == "foreign-source":
            item["observation_id"] = str(uuid4())
        elif problem == "hash":
            item["source_sha256"] = "0" * 64
        elif problem == "basis":
            item["basis_assertion_id"] = str(uuid4())
        elif problem == "wrong-basis-source":
            with unit_of_work(engine, write=True) as con:
                other = cloned_source(con, source, dps="0.70000000")
            item.update(
                observation_id=other["observation_id"],
                source_sha256=other["source_sha256"],
                basis_assertion_id=source["id"],
            )
        elif problem == "reason":
            item["reason"] = "short"
        else:
            item["reason"] = "Synthetic\0source association"
        values = with_matches(values, [item])
    rejected(engine, values)


def test_basis_from_another_owner_or_security_never_becomes_an_association(
    engine: Engine, source: dict[str, Any]
) -> None:
    values = candidate(source, basis=True, owner_id="other")
    rejected(engine, values)
    values = candidate(source, basis=True, security_id="stock")
    rejected(engine, values)


def test_explicit_empty_and_up_to_one_hundred_matches_are_bounded(
    engine: Engine, source: dict[str, Any]
) -> None:
    empty = with_matches(candidate(source), [])
    matches = [match(source)]
    with unit_of_work(engine, write=True) as con:
        for i in range(1, 101):
            other = cloned_source(con, source, dps=f"0.{i:08d}")
            matches.append(match(other))
    hundred = with_matches(candidate(source), matches[:100])
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, empty)
        insert(con, TABLE, hundred)
    rejected(engine, with_matches(candidate(source), matches))
    unicode_values = with_matches(
        candidate(source), [{**item, "reason": "中" * 1000} for item in matches[:44]]
    )
    assert len(unicode_values["matches_json"]) < 131072
    assert len(unicode_values["matches_json"].encode()) > 131072
    rejected(engine, unicode_values)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            f"SELECT json_array_length(matches_json) FROM {TABLE} ORDER BY rowid"
        ).scalars().all() == [0, 100]


def test_action_correction_does_not_rewrite_historical_candidate_or_basis(
    engine: Engine, source: dict[str, Any]
) -> None:
    row = candidate(source, basis=True)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, row)
    before = database_snapshot(engine)[TABLE]
    with unit_of_work(engine, write=True) as con:
        identifier = json.loads(source["assertion_json"])["splits"][0]["id"]
        con.exec_driver_sql(
            "UPDATE corporate_actions SET status='VOID',version=version+1 WHERE id=?", (identifier,)
        )
    assert database_snapshot(engine)[TABLE] == before


@pytest.mark.parametrize("corruption", ["legacy", "missing-first-sighting"])
def test_non_v1_or_unproven_source_cannot_be_newly_referenced(
    engine: Engine, source: dict[str, Any], corruption: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        trigger = (
            "trg_dividend_observation_no_update"
            if corruption == "legacy"
            else "trg_provider_provider_observation_sightings_delete"
        )
        guard = con.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE name=?", (trigger,)
        ).scalar_one()
        con.exec_driver_sql(f"DROP TRIGGER {trigger}")
        if corruption == "legacy":
            con.exec_driver_sql(
                "UPDATE dividend_observations SET provenance_version=0 WHERE id=?",
                (source["observation_id"],),
            )
        else:
            con.exec_driver_sql(
                "DELETE FROM provider_observation_sightings WHERE dividend_observation_id=?",
                (source["observation_id"],),
            )
        con.exec_driver_sql(guard)
    rejected(engine, candidate(source))


def test_source_for_another_real_security_is_not_matched_by_a_date_or_symbol(
    engine: Engine, source: dict[str, Any]
) -> None:
    other = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO securities(id,exchange_code,display_ticker,name,"
            "quote_currency,created_at) "
            "VALUES (?,'NASDAQ','OTHER','Other synthetic security','USD',?)",
            (other, AT),
        )
    rejected(engine, candidate(source, security_id=other))


def test_duplicate_and_escaped_json_object_keys_do_not_override_a_match(
    engine: Engine, source: dict[str, Any]
) -> None:
    values = candidate(source)
    for key in ('"reason"', '"reas\\u006fn"'):
        wire = values["matches_json"].replace(
            '"reason":', f'{key}:"untrusted duplicate reason","reason":', 1
        )
        rejected(engine, {**values, "matches_json": wire, "matches_sha256": digest(wire)})


def test_inactive_owner_cannot_create_a_new_candidate_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id='owner'")
    rejected(engine, candidate(source))


def test_two_concurrent_writers_cannot_create_the_same_candidate_version(
    engine: Engine, source: dict[str, Any]
) -> None:
    first = candidate(source)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, first)
    revisions = [{**first, "id": str(uuid4()), "revision_no": 2} for _ in range(2)]

    def save(values: dict[str, Any]) -> bool:
        try:
            with unit_of_work(engine, write=True) as con:
                insert(con, TABLE, values)
        except IntegrityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, revisions)) == [False, True]
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            f"SELECT revision_no FROM {TABLE} WHERE candidate_id=? ORDER BY revision_no",
            (first["candidate_id"],),
        ).scalars().all() == [1, 2]


@pytest.mark.parametrize("day", ["0001-01-01", "2024-02-29", "9999-12-31"])
def test_valid_calendar_boundaries_are_preserved(
    engine: Engine, source: dict[str, Any], day: str
) -> None:
    values = candidate(source, ex_date=day, announce_date=day, record_date=day, pay_date=day)
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, values)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            f"SELECT ex_date,announce_date,record_date,pay_date FROM {TABLE}"
        ).one() == (day, day, day, day)


def test_downgrade_refuses_to_delete_candidate_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        insert(con, TABLE, candidate(source))
    before, definitions = database_snapshot(engine), schema(engine)
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated"):
            command.downgrade(config, "0014_dividend_basis_assertions")
    assert database_snapshot(engine) == before and schema(engine) == definitions
