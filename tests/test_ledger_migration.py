"""Ledger upgrades preserve original facts and enforce replay/reversal invariants in SQLite."""

from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, unit_of_work

NOW = "2026-01-01T00:00:00Z"


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
            "VALUES (?,?,?,?,'USD',?)",
            (account, owner, owner, account, NOW),
        )
    con.exec_driver_sql(
        "INSERT INTO securities(id,exchange_code,display_ticker,name,quote_currency,created_at) "
        "VALUES ('stock','NASDAQ','SYN','Synthetic','USD',?)",
        (NOW,),
    )


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        with engine.connect() as con:
            config = migration_config()
            config.attributes["connection"] = con
            command.upgrade(config, "0003_master_data")
        with engine.begin() as con:
            dimensions(con)
        yield engine
    finally:
        engine.dispose()


def insert(con: Connection, table: str, values: dict[str, Any]) -> None:
    con.execute(
        text(
            f"INSERT INTO {table}({','.join(values)}) "
            f"VALUES ({','.join(':' + key for key in values)})"
        ),
        values,
    )


def sequence(con: Connection, kind: str, identifier: str) -> int:
    value = con.exec_driver_sql(
        "INSERT INTO ledger_sequences(kind,fact_id) VALUES (?,?)", (kind, identifier)
    ).lastrowid
    assert value is not None
    return value


def trade(con: Connection, identifier: str, *, current: bool = True, **changes: Any) -> None:
    values: dict[str, Any] = {
        "id": identifier,
        "portfolio_id": "owner",
        "account_id": "A",
        "security_id": "stock",
        "side": "BUY",
        "trade_date": "2026-01-01",
        "quantity_e8": 100000000,
        "price_e8": 100000000,
        "currency": "USD",
        "fx_rate_hkd_e12": 7800000000000,
        "gross_micro": 1000000,
        "settlement_micro": 1000000,
        "booked_hkd_micro": 7800000,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    if current:
        values.setdefault("root_id", identifier)
        if "created_sequence" not in values:
            existing = con.exec_driver_sql(
                "SELECT sequence FROM ledger_sequences WHERE kind='TRADE' AND fact_id=?",
                (values["root_id"],),
            ).scalar()
            values["created_sequence"] = (
                existing if existing is not None else sequence(con, "TRADE", values["root_id"])
            )
    insert(con, "trades", values)


def cash(con: Connection, identifier: str, *, current: bool = True, **changes: Any) -> None:
    values: dict[str, Any] = {
        "id": identifier,
        "account_id": "A",
        "effective_date": "2026-01-01",
        "currency": "USD",
        "amount_micro": 1000000,
        "fx_rate_hkd_e12": 7800000000000,
        "kind": "ADJUSTMENT",
        "source_type": "MANUAL",
        "source_id": identifier,
        "created_at": NOW,
    }
    if current:
        values["booked_hkd_micro"] = 7800000
    values.update(changes)
    insert(con, "cash_ledger_entries", values)


def legacy_facts(con: Connection) -> None:
    trade(con, "z-root", current=False, status="SUPERSEDED")
    cash(
        con,
        "old-cash",
        current=False,
        amount_micro=-1000000,
        kind="TRADE_SETTLEMENT",
        source_type="TRADE",
        source_id="z-root",
    )
    cash(
        con,
        "old-reversal",
        current=False,
        amount_micro=1000000,
        kind="REVERSAL",
        entry_role="REVERSAL",
        source_type="TRADE",
        source_id="z-root",
    )
    trade(
        con,
        "a-revision",
        current=False,
        status="ACTIVE",
        revision=2,
        supersedes_id="z-root",
        settlement_micro=1000001,
        booked_hkd_micro=7700000,
        created_at="2026-01-03T00:00:00Z",
    )
    cash(
        con,
        "current-cash",
        current=False,
        amount_micro=-1000001,
        kind="TRADE_SETTLEMENT",
        source_type="TRADE",
        source_id="a-revision",
    )
    cash(con, "unknown-fx", current=False, fx_rate_hkd_e12=None)
    cash(con, "identity-fx", current=False, account_id="B", currency="HKD", fx_rate_hkd_e12=None)
    con.exec_driver_sql(
        "INSERT INTO corporate_actions(id,security_id,action_type,effective_date,ratio_num,"
        "ratio_den,status,created_at) VALUES ('split','stock','SPLIT','2026-01-02',3,2,"
        "'CONFIRMED','2026-01-01T01:00:00Z')"
    )
    con.exec_driver_sql(
        "INSERT INTO security_transfers(id,portfolio_id,source_account_id,target_account_id,"
        "security_id,transfer_date,quantity_e8,transferred_cost_hkd_micro,created_at) "
        "VALUES ('transfer','owner','A','B','stock','2026-01-03',50000000,3850000,"
        "'2026-01-02T00:00:00Z')"
    )
    con.exec_driver_sql(
        "INSERT INTO expenses(id,account_id,date,category,amount_micro,currency,"
        "fx_rate_hkd_e12,hkd_micro,created_at) VALUES ('expense','A','2026-01-01',"
        "'Synthetic',1000000,'USD',7800000000000,7600000,?)",
        (NOW,),
    )
    cash(
        con,
        "expense-cash",
        current=False,
        amount_micro=-1000000,
        source_type="EXPENSE",
        source_id="expense",
        kind="EXPENSE",
    )


def original_rows(con: Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: [tuple(row) for row in con.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id")]
        for table in (
            "trades",
            "cash_ledger_entries",
            "security_transfers",
            "corporate_actions",
            "expenses",
        )
    }


def test_upgrade_preserves_locked_actuals_revision_order_and_unknown_fx(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        legacy_facts(con)
    with old_engine.connect() as con:
        before = original_rows(con)
        columns = {
            table: [info[1] for info in con.exec_driver_sql(f"PRAGMA table_info({table})")]
            for table in before
        }
    migrate(settings)
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0004_ledger"
        )
        for table, names in columns.items():
            after = [
                tuple(row)
                for row in con.exec_driver_sql(f"SELECT {','.join(names)} FROM {table} ORDER BY id")
            ]
            assert after == before[table]
        rows = {row["id"]: row for row in con.exec_driver_sql("SELECT * FROM trades").mappings()}
        assert rows["z-root"]["root_id"] == rows["a-revision"]["root_id"] == "z-root"
        assert rows["z-root"]["created_sequence"] == rows["a-revision"]["created_sequence"] == 1
        assert rows["a-revision"]["hkd_override"] == rows["a-revision"]["settlement_override"] == 1
        assert rows["a-revision"]["rounding_delta_micro"] == 1
        assert "no broker verification" in rows["a-revision"]["override_reason"]
        assert (
            con.exec_driver_sql("SELECT created_sequence FROM corporate_actions").scalar_one() == 2
        )
        assert (
            con.exec_driver_sql("SELECT created_sequence FROM security_transfers").scalar_one() == 3
        )
        cash_rows = {
            row["id"]: row
            for row in con.exec_driver_sql("SELECT * FROM cash_ledger_entries").mappings()
        }
        assert cash_rows["old-cash"]["booked_hkd_micro"] == -7800000
        assert cash_rows["old-reversal"]["booked_hkd_micro"] == 7800000
        assert cash_rows["old-reversal"]["reverses_entry_id"] == "old-cash"
        assert cash_rows["current-cash"]["booked_hkd_micro"] == -7700000
        assert cash_rows["expense-cash"]["booked_hkd_micro"] == -7600000
        assert cash_rows["unknown-fx"]["booked_hkd_micro"] is None
        assert cash_rows["identity-fx"]["booked_hkd_micro"] == 1000000
        assert cash_rows["identity-fx"]["fx_rate_hkd_e12"] is None
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_LEDGER'"
            ).scalar_one()
            == 2
        )
        allocated = list(con.exec_driver_sql("SELECT * FROM ledger_sequences ORDER BY sequence"))
    migrate(settings)
    with old_engine.connect() as con:
        assert (
            list(con.exec_driver_sql("SELECT * FROM ledger_sequences ORDER BY sequence"))
            == allocated
        )
    backups = list(settings.data_dir.glob("pre-migration-*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "corruption",
    [
        "ratio",
        "date",
        "timestamp",
        "revision",
        "gross",
        "reversal",
        "cash-source",
        "reversal-overflow",
    ],
)
def test_invalid_old_facts_abort_with_original_schema_and_facts_intact(
    old_engine: Engine, settings: Settings, corruption: str
) -> None:
    with old_engine.begin() as con:
        legacy_facts(con)
        sql = {
            "ratio": "UPDATE corporate_actions SET ratio_num=1.5",
            "date": "UPDATE trades SET trade_date='2026-02-30'",
            "timestamp": "UPDATE trades SET created_at='unknown' WHERE id='z-root'",
            "revision": "UPDATE trades SET revision=4 WHERE id='a-revision'",
            "gross": "UPDATE trades SET gross_micro=1000001 WHERE id='a-revision'",
            "reversal": "UPDATE cash_ledger_entries SET amount_micro=999999 "
            "WHERE id='old-reversal'",
            "cash-source": "UPDATE cash_ledger_entries SET amount_micro=-999999 "
            "WHERE id='current-cash'",
            "reversal-overflow": "UPDATE cash_ledger_entries SET amount_micro=-9223372036854775808 "
            "WHERE id='unknown-fx'",
        }[corruption]
        con.exec_driver_sql(sql)
    with old_engine.connect() as con:
        before = original_rows(con)
    with pytest.raises((RuntimeError, IntegrityError)):
        migrate(settings)
    with old_engine.connect() as con:
        assert original_rows(con) == before
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0003_master_data"
        )
        assert (
            con.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='ledger_sequences'"
            ).first()
            is None
        )
        assert "root_id" not in {row[1] for row in con.exec_driver_sql("PRAGMA table_info(trades)")}
        assert "booked_hkd_micro" not in {
            row[1] for row in con.exec_driver_sql("PRAGMA table_info(cash_ledger_entries)")
        }
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    assert len(list(settings.data_dir.glob("pre-migration-*.sqlite3"))) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "HKD", "fx_rate_hkd_e12": 1000000000000, "booked_hkd_micro": 1000000},
        {"settlement_override": 1, "override_reason": "short"},
        {"hkd_override": 1},
        {"created_sequence": None},
    ],
)
def test_new_fact_terms_require_currency_evidence_and_durable_sequence(
    engine: Engine, changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        trade(con, "invalid-terms", **changes)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trades").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 0


def test_revision_retains_root_sequence_and_prevents_history_rewrites(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        trade(con, "root")
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        trade(con, "unpublished-child", root_id="root", supersedes_id="root", revision=2)
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE trades SET status='SUPERSEDED',version=version+1 WHERE id='root'"
        )
        trade(
            con,
            "revision",
            root_id="root",
            supersedes_id="root",
            revision=2,
            booked_hkd_micro=7900000,
            fx_rate_hkd_e12=7900000000000,
        )
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 1
    for sql in (
        "UPDATE trades SET price_e8=200000000,status='SUPERSEDED',version=version+1 "
        "WHERE id='revision'",
        "DELETE FROM trades WHERE id='root'",
        "UPDATE trades SET status='ACTIVE',version=version+1 WHERE id='root'",
        "DELETE FROM ledger_sequences",
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(sql)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        trade(con, "branch", root_id="root", supersedes_id="root", revision=2)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT created_sequence FROM trades WHERE id='revision'"
            ).scalar_one()
            == 1
        )
        assert (
            con.exec_driver_sql("SELECT price_e8 FROM trades WHERE id='revision'").scalar_one()
            == 100000000
        )


def test_legacy_receipt_actual_hkd_is_not_recomputed_from_rate(
    old_engine: Engine, settings: Settings
) -> None:
    with old_engine.begin() as con:
        insert(
            con,
            "dividend_events",
            {
                "id": "event",
                "security_id": "stock",
                "event_identity_key": "legacy",
                "created_at": NOW,
            },
        )
        insert(
            con,
            "dividend_event_revisions",
            {
                "id": "event-revision",
                "event_id": "event",
                "revision_no": 1,
                "event_type": "REGULAR",
                "ex_date": "2026-01-01",
                "dps_e8": 100000000,
                "currency": "USD",
                "adjustment_basis": "AS_DECLARED",
                "review_status": "PROVISIONAL",
                "created_at": NOW,
            },
        )
        insert(
            con,
            "dividend_entitlements",
            {
                "id": "entitlement",
                "revision_id": "event-revision",
                "account_id": "A",
                "eligible_shares_e8": 1000000000,
                "gross_micro": 10000000,
                "expected_net_micro": 10000000,
                "currency": "USD",
                "quality": "ACCRUED",
                "projection_version": 0,
                "created_at": NOW,
            },
        )
        insert(
            con,
            "dividend_receipts",
            {
                "id": "receipt",
                "entitlement_id": "entitlement",
                "account_id": "A",
                "received_date": "2026-01-01",
                "received_micro": 6000000,
                "currency": "USD",
                "fx_rate_hkd_e12": 7800000000000,
                "received_hkd_micro": 46123456,
                "created_at": NOW,
            },
        )
        cash(
            con,
            "receipt-cash",
            current=False,
            amount_micro=6000000,
            source_type="DIVIDEND_RECEIPT",
            source_id="receipt",
            kind="DIVIDEND_RECEIPT",
        )
    migrate(settings)
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT booked_hkd_micro FROM cash_ledger_entries").scalar_one()
            == 46123456
        )
        assert (
            con.exec_driver_sql("SELECT received_hkd_micro FROM dividend_receipts").scalar_one()
            == 46123456
        )
        assert (
            con.exec_driver_sql("SELECT fx_rate_hkd_e12 FROM dividend_receipts").scalar_one()
            == 7800000000000
        )


def test_cross_owner_revision_and_wrong_sequence_kind_are_rejected(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        trade(con, "root")
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE trades SET status='SUPERSEDED',version=version+1 WHERE id='root'"
        )
        trade(
            con,
            "foreign-revision",
            root_id="root",
            supersedes_id="root",
            revision=2,
            account_id="foreign",
            portfolio_id="other",
        )
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        wrong = sequence(con, "ACTION", "wrong")
        trade(con, "wrong", created_sequence=wrong)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql("SELECT status FROM trades WHERE id='root'").scalar_one()
            == "ACTIVE"
        )
        assert con.exec_driver_sql("SELECT count(*) FROM ledger_sequences").scalar_one() == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_micro": -999999},
        {"booked_hkd_micro": -7799999},
        {"currency": "HKD"},
        {"fx_rate_hkd_e12": 7900000000000},
        {"external_flow": 1},
        {"reverses_entry_id": "missing"},
        {"entry_role": "PRIMARY"},
        {"kind": "ADJUSTMENT"},
    ],
)
def test_reversal_must_exactly_offset_original_currency_amount_fx_and_booked_hkd(
    engine: Engine, changes: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        cash(con, "original")
    values: dict[str, Any] = {
        "entry_role": "REVERSAL",
        "kind": "REVERSAL",
        "source_id": "original",
        "amount_micro": -1000000,
        "booked_hkd_micro": -7800000,
        "reverses_entry_id": "original",
    }
    values.update(changes)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        cash(con, "invalid-reversal", **values)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 1


def test_single_exact_reversal_is_append_only_and_cannot_be_reentered(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        cash(con, "original")
        cash(
            con,
            "reversal",
            entry_role="REVERSAL",
            kind="REVERSAL",
            source_id="original",
            amount_micro=-1000000,
            booked_hkd_micro=-7800000,
            reverses_entry_id="original",
        )
        con.exec_driver_sql("UPDATE cash_ledger_entries SET version=version+1 WHERE id='original'")
        assert con.exec_driver_sql(
            "SELECT sum(amount_micro),sum(booked_hkd_micro) FROM cash_ledger_entries"
        ).one() == (0, 0)
    for operation in (
        "DELETE FROM cash_ledger_entries",
        "UPDATE cash_ledger_entries SET amount_micro=2,version=version+1 WHERE id='original'",
    ):
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            con.exec_driver_sql(operation)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        cash(
            con,
            "duplicate",
            entry_role="REVERSAL",
            kind="REVERSAL",
            source_id="original",
            amount_micro=-1000000,
            booked_hkd_micro=-7800000,
            reverses_entry_id="original",
        )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 2


@pytest.mark.parametrize(
    "day", ["2026-02-30", "2026-1-01", "0000-01-01", "2026-13-01", "2026-01-01T00:00:00Z"]
)
def test_sql_path_cannot_post_invalid_financial_calendar_dates(engine: Engine, day: str) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
    with (
        pytest.raises(IntegrityError, match="INVALID_FINANCIAL_DATE"),
        unit_of_work(engine, write=True) as con,
    ):
        cash(con, "bad-day", effective_date=day)


def test_rational_action_history_requires_new_revision_not_ratio_overwrite(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        seq = sequence(con, "ACTION", "split")
        con.exec_driver_sql(
            "INSERT INTO corporate_actions(id,security_id,action_type,effective_date,ratio_num,"
            "ratio_den,status,created_at,root_id,created_sequence) VALUES ('split','stock',"
            "'SPLIT','2026-01-01',3,2,'CONFIRMED',?,'split',?)",
            (NOW, seq),
        )
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET ratio_num=4,version=version+1 WHERE id='split'"
        )
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE corporate_actions SET is_current=0,version=version+1 WHERE id='split'"
        )
        con.exec_driver_sql(
            "INSERT INTO corporate_actions(id,security_id,action_type,effective_date,ratio_num,"
            "ratio_den,status,created_at,root_id,created_sequence,supersedes_id,revision) "
            "VALUES ('new-split','stock','SPLIT','2026-01-01',4,3,'CONFIRMED',?,"
            "'split',?,'split',2)",
            (NOW, seq),
        )
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        seq = sequence(con, "ACTION", "real-ratio")
        con.exec_driver_sql(
            "INSERT INTO corporate_actions(id,security_id,action_type,effective_date,ratio_num,"
            "ratio_den,status,created_at,root_id,created_sequence) VALUES ('real-ratio','stock',"
            "'SPLIT','2026-01-01',1.5,1,'CONFIRMED',?,'real-ratio',?)",
            (NOW, seq),
        )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT ratio_num,ratio_den FROM corporate_actions WHERE id='split'"
        ).one() == (3, 2)
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM corporate_actions WHERE is_current=1"
            ).scalar_one()
            == 1
        )


def test_source_linked_cash_cannot_change_trade_settlement_or_locked_hkd(engine: Engine) -> None:
    with unit_of_work(engine, write=True) as con:
        dimensions(con)
        trade(con, "root")
    for changes in (
        {"amount_micro": -999999},
        {"booked_hkd_micro": -7799999},
        {"source_id": "missing"},
        {"external_flow": 1},
    ):
        values: dict[str, Any] = {
            "amount_micro": -1000000,
            "booked_hkd_micro": -7800000,
            "source_type": "TRADE",
            "source_id": "root",
            "kind": "TRADE_SETTLEMENT",
            **changes,
        }
        with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
            cash(con, "bad-settlement", **values)
    with unit_of_work(engine, write=True) as con:
        cash(
            con,
            "settlement",
            amount_micro=-1000000,
            booked_hkd_micro=-7800000,
            source_type="TRADE",
            source_id="root",
            kind="TRADE_SETTLEMENT",
        )
        assert con.exec_driver_sql("SELECT count(*) FROM cash_ledger_entries").scalar_one() == 1
