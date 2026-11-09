"""Manual-cash source guards preserve old facts and refuse ambiguous corrections atomically."""

from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from spm_api.ledger_service import reverse_cash

NOW = "2026-01-02T00:00:00Z"


def dimensions(con: Connection) -> None:
    for owner in ("owner", "other"):
        con.exec_driver_sql(
            "INSERT INTO owners VALUES (?,'synthetic-hash',?,?,1)", (owner, NOW, NOW)
        )
        con.exec_driver_sql(
            "INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (?,?,?,?)",
            (owner, owner, owner, NOW),
        )
        con.exec_driver_sql(
            "INSERT INTO brokers(id,owner_id,name) VALUES (?,?,?)", (owner, owner, owner)
        )
    for account, owner in (("A", "owner"), ("B", "owner"), ("foreign", "other")):
        con.exec_driver_sql(
            "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
            "VALUES (?,?,?,?,'HKD',?)",
            (account, owner, owner, account, NOW),
        )


def cash(
    con: Connection,
    identifier: str,
    *,
    account: str = "A",
    amount: int = 1000000,
    kind: str = "DEPOSIT",
    external: int = 1,
    source: str = "MANUAL_CASH",
    previous: str | None = None,
) -> None:
    con.exec_driver_sql(
        "INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,"
        "fx_rate_hkd_e12,booked_hkd_micro,kind,source_type,source_id,external_flow,created_at) "
        "VALUES (?,?,'2026-01-02','HKD',?,1000000000000,?,?,?,?,?,?)",
        (identifier, account, amount, amount, kind, source, previous or identifier, external, NOW),
    )


def reverse(con: Connection, identifier: str) -> None:
    row = (
        con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (identifier,))
        .mappings()
        .one()
    )
    reverse_cash(con, row, reason="Synthetic pre-upgrade cash reversal")


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0004_ledger")
        with engine.begin() as con:
            dimensions(con)
        yield engine
    finally:
        engine.dispose()


def rows(con: Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row) for row in con.exec_driver_sql("SELECT * FROM cash_ledger_entries ORDER BY id")
    ]


def test_upgrade_preserves_cash_fx_reversals_and_appends_one_schema_audit(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        cash(con, "original")
        reverse(con, "original")
        cash(
            con,
            "corrected",
            account="B",
            source="CASH_CORRECTION",
            previous="original",
            amount=2000000,
        )
        con.exec_driver_sql(
            "INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,"
            "kind,source_type,source_id,external_flow,created_at) "
            "VALUES ('unknown','A','2026-01-02','USD',1,'DEPOSIT','MANUAL_CASH','unknown',1,?)",
            (NOW,),
        )
        before = rows(con)
    migrate(settings)
    with unit_of_work(old_engine) as con:
        assert rows(con) == before
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_CASH'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    migrate(settings)
    with unit_of_work(old_engine) as con:
        assert rows(con) == before
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_CASH'"
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "problem", ["direction", "external", "missing", "active", "foreign", "managed", "fork"]
)
def test_invalid_old_cash_refuses_upgrade_with_head_schema_and_facts_intact(
    old_engine: Engine, settings: Settings, problem: str
) -> None:
    with old_engine.begin() as con:
        if problem == "direction":
            cash(con, "invalid", amount=-1)
        elif problem == "external":
            cash(con, "invalid", external=0)
        elif problem == "missing":
            cash(con, "invalid", source="CASH_CORRECTION", previous="missing")
        else:
            cash(
                con,
                "original",
                account="foreign" if problem == "foreign" else "A",
                source="IMPORT" if problem == "managed" else "MANUAL_CASH",
            )
            if problem != "active":
                reverse(con, "original")
            cash(con, "invalid", source="CASH_CORRECTION", previous="original")
            if problem == "fork":
                cash(con, "second", account="B", source="CASH_CORRECTION", previous="original")
        before = rows(con)
    with pytest.raises(RuntimeError, match="cash correction|cash manual_terms"):
        migrate(settings)
    with old_engine.connect() as con:
        assert rows(con) == before
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0004_ledger"
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE name IN "
                "('uq_cash_correction_successor','idx_cash_source',"
                "'trg_cash_manual_terms','trg_cash_correction_source')"
            ).scalar_one()
            == 0
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_CASH'"
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("kind", "amount", "external"),
    [
        ("DEPOSIT", -1, 1),
        ("DEPOSIT", 1, 0),
        ("WITHDRAW", 1, 1),
        ("ADJUSTMENT", 0, 0),
        ("ADJUSTMENT", 1, 1),
        ("TRANSFER_IN", 1, 0),
    ],
)
def test_sql_cannot_bypass_manual_cash_sign_flow_or_paired_transfer_rules(
    engine: Engine, kind: str, amount: int, external: int
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
    with (
        pytest.raises(IntegrityError, match="INVALID_CASH_MANUAL_TERMS"),
        unit_of_work(engine, write=True) as con,
    ):
        cash(con, "invalid", kind=kind, amount=amount, external=external)
    with unit_of_work(engine) as con:
        assert rows(con) == []


def test_sql_corrections_need_a_reversed_manual_parent_same_owner_and_one_successor(
    engine: Engine,
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        cash(con, "original")
        cash(con, "foreign-parent", account="foreign")
        reverse(con, "foreign-parent")
    for predecessor in ("original", "foreign-parent", "missing"):
        with (
            pytest.raises(IntegrityError, match="INVALID_CASH_CORRECTION_SOURCE"),
            unit_of_work(engine, write=True) as con,
        ):
            cash(con, "invalid", source="CASH_CORRECTION", previous=predecessor)
    with unit_of_work(engine, write=True) as con:
        reverse(con, "original")
        cash(con, "corrected", account="B", source="CASH_CORRECTION", previous="original")
    with (
        pytest.raises(IntegrityError, match="IMMUTABLE_CASH_ENTRY"),
        unit_of_work(engine, write=True) as con,
    ):
        cash(con, "fork", source="CASH_CORRECTION", previous="original")
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM cash_ledger_entries WHERE source_type='CASH_CORRECTION'"
            ).scalar_one()
            == 1
        )


def test_cash_guard_downgrade_is_explicitly_non_destructive(old_engine: Engine) -> None:
    with old_engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        command.upgrade(config, "0005_cash")
        with pytest.raises(RuntimeError, match="validated pre-upgrade backup"):
            command.downgrade(config, "0004_ledger")
    with unit_of_work(old_engine) as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0005_cash"
        )
