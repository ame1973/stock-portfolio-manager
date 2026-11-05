"""Transfer schema upgrades preserve facts/order and enforce paired/revision consistency."""

from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from spm_api.ledger_service import allocate_sequence, post_cash, reverse_cash, validate_history
from spm_api.master_models import Currency

NOW = "2026-01-01T00:00:00Z"


def insert(con: Connection, table: str, values: dict[str, Any]) -> None:
    con.execute(
        text(
            f"INSERT INTO {table}({','.join(values)}) "
            f"VALUES ({','.join(':' + key for key in values)})"
        ),
        values,
    )


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
    for account, owner in (("A", "owner"), ("B", "owner"), ("F", "other"), ("G", "other")):
        con.exec_driver_sql(
            "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
            "VALUES (?,?,?,?,'HKD',?)",
            (account, owner, owner, account, NOW),
        )
    con.exec_driver_sql(
        "INSERT INTO securities(id,exchange_code,display_ticker,name,quote_currency,created_at) "
        "VALUES ('stock','NASDAQ','STK','Synthetic','USD',?)",
        (NOW,),
    )


def seed_buy(con: Connection) -> None:
    sequence = allocate_sequence(con, "TRADE", "buy")
    insert(
        con,
        "trades",
        {
            "id": "buy",
            "root_id": "buy",
            "created_sequence": sequence,
            "portfolio_id": "owner",
            "account_id": "A",
            "security_id": "stock",
            "side": "BUY",
            "trade_date": "2026-01-01",
            "quantity_e8": 1000000000,
            "price_e8": 10000000000,
            "fee_micro": 1000000,
            "currency": "USD",
            "fx_rate_hkd_e12": 7800000000000,
            "gross_micro": 1000000000,
            "settlement_micro": 1001000000,
            "booked_hkd_micro": 7807800000,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    post_cash(
        con,
        account="A",
        day="2026-01-01",
        currency="USD",
        amount=-1001000000,
        fx=7800000000000,
        booked=-7807800000,
        kind="TRADE_SETTLEMENT",
        source_type="TRADE",
        source_id="buy",
    )


def stock(con: Connection, identifier: str, *, current: bool = True, **changes: Any) -> None:
    values: dict[str, Any] = {
        "id": identifier,
        "portfolio_id": "owner",
        "source_account_id": "A",
        "target_account_id": "B",
        "security_id": "stock",
        "transfer_date": "2026-01-02",
        "quantity_e8": 400000000,
        "transferred_cost_hkd_micro": 3123120000,
        "created_at": NOW,
    }
    values.update(changes)
    if current:
        values.setdefault("root_id", identifier)
    if "created_sequence" not in values:
        root = values.get("root_id", identifier)
        existing = con.exec_driver_sql(
            "SELECT sequence FROM ledger_sequences WHERE kind='TRANSFER' AND fact_id=?", (root,)
        ).scalar()
        values["created_sequence"] = (
            existing if existing is not None else allocate_sequence(con, "TRANSFER", root)
        )
    insert(con, "security_transfers", values)


def cash_fact(con: Connection, identifier: str, **changes: Any) -> None:
    values = {
        "id": identifier,
        "root_id": identifier,
        "portfolio_id": "owner",
        "source_account_id": "A",
        "target_account_id": "B",
        "transfer_date": "2026-01-02",
        "source_currency": "HKD",
        "target_currency": "HKD",
        "source_amount_micro": 1000000,
        "target_amount_micro": 1000000,
        "fx_rate_hkd_e12": 1000000000000,
        "source_booked_hkd_micro": 1000000,
        "target_booked_hkd_micro": 1000000,
        "reason": "Synthetic reviewed transfer",
        "created_at": NOW,
        "updated_at": NOW,
        **changes,
    }
    insert(con, "cash_transfers", values)


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0005_cash")
        with engine.begin() as con:
            dimensions(con)
            seed_buy(con)
        yield engine
    finally:
        engine.dispose()


def test_upgrade_preserves_all_original_security_cash_cost_and_order_columns(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        stock(con, "active", current=False)
        stock(con, "voided", current=False, status="VOID", version=2)
        names = {
            table: [row[1] for row in con.exec_driver_sql(f"PRAGMA table_info({table})")]
            for table in ("security_transfers", "cash_ledger_entries", "trades", "ledger_sequences")
        }
        before = {
            table: [
                tuple(row)
                for row in con.exec_driver_sql(
                    f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid"
                )
            ]
            for table, columns in names.items()
        }
    migrate(settings)
    with unit_of_work(old_engine) as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
        for table, columns in names.items():
            assert [
                tuple(row)
                for row in con.exec_driver_sql(
                    f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid"
                )
            ] == before[table]
        transfers = list(con.exec_driver_sql("SELECT * FROM security_transfers").mappings())
        assert all(
            row["root_id"] == row["id"] and row["revision"] == 1 and row["is_current"] == 1
            for row in transfers
        )
        assert (
            con.exec_driver_sql(
                "SELECT version FROM security_transfers WHERE id='voided'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("SELECT count(*) FROM cash_transfers").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_TRANSFERS'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    migrate(settings)
    with unit_of_work(old_engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_TRANSFERS'"
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize("problem", ["sequence", "late_ddl"])
def test_failed_transfer_upgrade_restores_head_original_facts_and_old_guards(
    old_engine: Engine, settings: Settings, problem: str
) -> None:
    with old_engine.begin() as con:
        if problem == "sequence":
            con.exec_driver_sql("DROP TRIGGER trg_ledger_security_transfers_sequence")
            trade_sequence = con.exec_driver_sql(
                "SELECT created_sequence FROM trades WHERE id='buy'"
            ).scalar_one()
            stock(con, "legacy", current=False, created_sequence=trade_sequence)
        else:
            stock(con, "legacy", current=False)
            con.exec_driver_sql("CREATE TABLE cash_transfers(operator_note TEXT)")
            con.exec_driver_sql(
                "INSERT INTO cash_transfers VALUES ('Retain unexpected existing data')"
            )
        original = [tuple(row) for row in con.exec_driver_sql("SELECT * FROM security_transfers")]
        schema = list(
            con.exec_driver_sql(
                "SELECT name,sql FROM sqlite_schema WHERE name LIKE '%security_transfers%' "
                "ORDER BY name"
            )
        )
    with pytest.raises((RuntimeError, OperationalError)):
        migrate(settings)
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0005_cash"
        )
        assert [
            tuple(row) for row in con.exec_driver_sql("SELECT * FROM security_transfers")
        ] == original
        assert (
            list(
                con.exec_driver_sql(
                    "SELECT name,sql FROM sqlite_schema WHERE name LIKE '%security_transfers%' "
                    "ORDER BY name"
                )
            )
            == schema
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_TRANSFERS'"
            ).scalar_one()
            == 0
        )
        if problem == "late_ddl":
            assert (
                con.exec_driver_sql("SELECT operator_note FROM cash_transfers").scalar_one()
                == "Retain unexpected existing data"
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_amount_micro": 0},
        {"source_amount_micro": 1.5},
        {"fx_rate_hkd_e12": 1.5},
        {"source_amount_micro": 2000000},
        {"source_booked_hkd_micro": 2000000},
        {"fx_rate_hkd_e12": 7800000000000},
        {"source_account_id": "A", "target_account_id": "A"},
        {"target_account_id": "F"},
        {"transfer_date": "2026-02-30"},
        {"transfer_date": "2026-1-02"},
        {"revision": 2},
        {"status": "VOID"},
        {"version": 2},
        {"reason": "short"},
        {"source_currency": "USD", "fx_rate_hkd_e12": None},
        {"actual_hkd_micro": 1000000},
    ],
)
def test_new_cash_facts_cannot_bypass_graph_terms_dates_or_scaled_types(
    engine: Engine, changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        cash_fact(con, "invalid", **changes)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_transfers").scalar_one() == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": -2000000},
        {"booked": -2000000},
        {"fx": None},
        {"currency": "USD"},
        {"account": "B"},
        {"day": "2026-01-03"},
        {"kind": "TRANSFER_IN"},
        {"external": True},
        {"source_id": "missing"},
    ],
)
def test_cash_legs_must_exactly_match_the_active_pair_fact(
    engine: Engine, changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        cash_fact(con, "pair")
    values: dict[str, Any] = {
        "account": "A",
        "day": "2026-01-02",
        "currency": "HKD",
        "amount": -1000000,
        "fx": 1000000000000,
        "booked": -1000000,
        "kind": "TRANSFER_OUT",
        "source_type": "CASH_TRANSFER_OUT",
        "source_id": "pair",
        **changes,
    }
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        post_cash(con, **values)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 0


def test_same_account_fx_legs_are_unique_reversible_and_not_directly_rewritable(
    engine: Engine,
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        cash_fact(
            con,
            "fx",
            target_account_id="A",
            source_currency="USD",
            target_currency="HKD",
            source_amount_micro=1000000,
            target_amount_micro=7800000,
            fx_rate_hkd_e12=7800000000000,
            source_booked_hkd_micro=7800000,
            target_booked_hkd_micro=7800000,
        )
        legs: tuple[tuple[str, Currency, int, int, int], ...] = (
            ("OUT", "USD", -1000000, 7800000000000, -7800000),
            ("IN", "HKD", 7800000, 1000000000000, 7800000),
        )
        for suffix, currency, amount, fx, booked in legs:
            post_cash(
                con,
                account="A",
                day="2026-01-02",
                currency=currency,
                amount=amount,
                fx=fx,
                booked=booked,
                kind=f"TRANSFER_{suffix}",
                source_type=f"CASH_TRANSFER_{suffix}",
                source_id="fx",
            )
        entries = list(con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings())
        con.exec_driver_sql(
            "UPDATE cash_transfers SET status='VOID',version=version+1 WHERE id='fx'"
        )
        for entry in entries:
            reverse_cash(con, entry, reason="Synthetic paired cash void")
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 4
    for sql in (
        "UPDATE cash_transfers SET source_amount_micro=2,version=version+1 WHERE id='fx'",
        "UPDATE cash_transfers SET status='ACTIVE',version=version+1 WHERE id='fx'",
        "DELETE FROM cash_transfers WHERE id='fx'",
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(sql)


def test_security_revisions_keep_original_sequence_and_only_current_replays(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        seed_buy(con)
        stock(con, "original")
        original = dict(
            con.exec_driver_sql("SELECT * FROM security_transfers WHERE id='original'")
            .mappings()
            .one()
        )
        con.exec_driver_sql(
            "UPDATE security_transfers SET is_current=0,version=version+1 WHERE id='original'"
        )
        stock(
            con,
            "corrected",
            root_id="original",
            supersedes_id="original",
            revision=2,
            quantity_e8=600000000,
            transferred_cost_hkd_micro=4684680000,
        )
    with unit_of_work(engine) as con:
        current = (
            con.exec_driver_sql("SELECT * FROM security_transfers WHERE id='corrected'")
            .mappings()
            .one()
        )
        assert current["created_sequence"] == original["created_sequence"]
        positions = validate_history(con, "owner")
        assert positions["A", "stock"].quantity == 4 and positions["B", "stock"].quantity == 6
        assert (
            con.exec_driver_sql(
                "SELECT status FROM security_transfers WHERE id='original'"
            ).scalar_one()
            == "ACTIVE"
        )
    for sql in (
        "UPDATE security_transfers SET is_current=1,version=version+1 WHERE id='original'",
        "UPDATE security_transfers SET quantity_e8=1,version=version+1 WHERE id='corrected'",
        "DELETE FROM security_transfers WHERE id='original'",
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(sql)


def test_cash_revision_lineage_and_security_root_cannot_cross_owner_or_fork(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        cash_fact(con, "cash-root")
        stock(con, "stock-root")
        con.exec_driver_sql(
            "UPDATE cash_transfers SET status='SUPERSEDED',version=version+1 WHERE id='cash-root'"
        )
        con.exec_driver_sql(
            "UPDATE security_transfers SET is_current=0,version=version+1 WHERE id='stock-root'"
        )
    for kind in ("cash", "stock"):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            if kind == "cash":
                cash_fact(
                    con,
                    "foreign-cash",
                    root_id="cash-root",
                    supersedes_id="cash-root",
                    revision=2,
                    portfolio_id="other",
                    source_account_id="F",
                    target_account_id="G",
                )
            else:
                stock(
                    con,
                    "foreign-stock",
                    root_id="stock-root",
                    supersedes_id="stock-root",
                    revision=2,
                    portfolio_id="other",
                    source_account_id="F",
                    target_account_id="G",
                )
    with unit_of_work(engine, write=True) as con:
        cash_fact(con, "new-cash", root_id="cash-root", supersedes_id="cash-root", revision=2)
        stock(con, "new-stock", root_id="stock-root", supersedes_id="stock-root", revision=2)
    for kind in ("cash", "stock"):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            if kind == "cash":
                cash_fact(
                    con, "fork-cash", root_id="cash-root", supersedes_id="cash-root", revision=2
                )
            else:
                stock(
                    con, "fork-stock", root_id="stock-root", supersedes_id="stock-root", revision=2
                )


def test_transfer_downgrade_requires_a_validated_backup(engine: Engine) -> None:
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated pre-upgrade backup"):
            command.downgrade(config, "0005_cash")
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == schema_head()
        )
