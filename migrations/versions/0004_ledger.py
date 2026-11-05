"""Add durable replay identities, immutable ledger facts, and exact cash reversals."""

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from hashlib import sha256
from uuid import uuid4

from alembic import op
from sqlalchemy import Connection
from sqlalchemy.engine import RowMapping

revision = "0004_ledger"
down_revision = "0003_master_data"
branch_labels = None
depends_on = None

MAX = 2**63 - 1
DATES = {
    "trades": "trade_date",
    "cash_ledger_entries": "effective_date",
    "expenses": "date",
    "security_transfers": "transfer_date",
    "corporate_actions": "effective_date",
}


def _decimal(value: int, scale: int) -> Decimal:
    if type(value) is not int:
        raise RuntimeError("Ledger migration requires integer scaled values")
    return Decimal(value).scaleb(-scale)


def _micro(value: Decimal) -> int:
    result = int(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).scaleb(6))
    if not -MAX <= result <= MAX:
        raise RuntimeError("Ledger amount must fit INT64 in both reversal directions")
    return result


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError("Timezone required")
        return result.astimezone(UTC)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            "Cannot infer a ledger creation sequence from an invalid timestamp"
        ) from exc


def _rows(con: Connection, table: str) -> list[RowMapping]:
    return list(con.exec_driver_sql(f'SELECT * FROM "{table}"').mappings())


def _validate_dates(con: Connection) -> None:
    for table, column in DATES.items():
        scaled = [
            info[1]
            for info in con.exec_driver_sql(f"PRAGMA table_info({table})")
            if info[1].endswith(("_micro", "_e8", "_e12", "_ppm"))
        ]
        invalid = " OR ".join(
            f'("{key}" IS NOT NULL AND typeof("{key}")!=\'integer\')' for key in scaled
        )
        if scaled and con.exec_driver_sql(f"SELECT 1 FROM {table} WHERE {invalid} LIMIT 1").first():
            raise RuntimeError("Ledger migration requires integer scaled values")
        for row in _rows(con, table):
            try:
                value = row[column]
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("Noncanonical calendar date")
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid ledger date in {table}; migration refused") from exc
    expenses = {row["id"]: row for row in _rows(con, "expenses")}
    owners = {
        row[0]: row[1]
        for row in con.exec_driver_sql(
            "SELECT a.id,p.owner_id FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id"
        )
    }
    checked: set[str] = set()
    for identifier in expenses:
        current = identifier
        seen: set[str] = set()
        while current not in checked:
            if current in seen or current not in expenses:
                raise RuntimeError("Invalid expense revision lineage")
            seen.add(current)
            row = expenses[current]
            parent = row["supersedes_id"]
            if parent is None:
                break
            previous = expenses.get(parent)
            if (
                previous is None
                or previous["status"] != "SUPERSEDED"
                or owners[row["account_id"]] != owners[previous["account_id"]]
            ):
                raise RuntimeError("Invalid expense revision lineage")
            current = parent
        checked.update(seen)
    for row in _rows(con, "corporate_actions"):
        if any(
            row[key] is not None and type(row[key]) is not int for key in ("ratio_num", "ratio_den")
        ):
            raise RuntimeError("Rational action terms must be integers; migration refused")


def _sequences(con: Connection) -> None:
    trades = {row["id"]: row for row in _rows(con, "trades")}
    currencies = {
        row[0]: row[1] for row in con.exec_driver_sql("SELECT id,quote_currency FROM securities")
    }
    owners = {row[0]: row[1] for row in con.exec_driver_sql("SELECT id,owner_id FROM portfolios")}
    roots: dict[str, str] = {}

    def root(identifier: str) -> str:
        chain: list[str] = []
        seen: set[str] = set()
        current = identifier
        while current not in roots:
            if current in seen or current not in trades:
                raise RuntimeError("Invalid trade revision lineage; migration refused")
            chain.append(current)
            seen.add(current)
            row = trades[current]
            parent = row["supersedes_id"]
            if parent is None:
                if row["revision"] != 1:
                    raise RuntimeError("A trade root must have revision one")
                roots[current] = current
                break
            if parent not in trades:
                raise RuntimeError("Missing trade predecessor")
            previous = trades[parent]
            if (
                row["revision"] != previous["revision"] + 1
                or previous["status"] != "SUPERSEDED"
                or owners[row["portfolio_id"]] != owners[previous["portfolio_id"]]
            ):
                raise RuntimeError("Invalid trade revision lineage; migration refused")
            current = parent
        for member in chain:
            roots[member] = roots[current]
        return roots[identifier]

    ordered: list[tuple[datetime, str, str]] = []
    for identifier, row in trades.items():
        if root(identifier) == identifier:
            ordered.append((_timestamp(row["created_at"]), "TRADE", identifier))
        if row["executed_at_utc"] is not None:
            _timestamp(row["executed_at_utc"])
    for kind, table in (("TRANSFER", "security_transfers"), ("ACTION", "corporate_actions")):
        ordered.extend(
            (_timestamp(row["created_at"]), kind, row["id"]) for row in _rows(con, table)
        )
    sequences: dict[tuple[str, str], int] = {}
    for _, kind, identifier in sorted(ordered):
        result = con.exec_driver_sql(
            "INSERT INTO ledger_sequences(kind,fact_id) VALUES (?,?)", (kind, identifier)
        ).lastrowid
        assert result is not None
        sequences[kind, identifier] = result
    for identifier, root_id in roots.items():
        row = trades[identifier]
        if row["currency"] != currencies[row["security_id"]]:
            raise RuntimeError("Legacy trade currency disagrees with its security")
        if (
            row["currency"] == "HKD"
            and row["status"] == "ACTIVE"
            and row["booked_hkd_micro"] != row["settlement_micro"]
        ):
            raise RuntimeError("HKD booked cash must preserve identity conversion")
        gross = _decimal(row["quantity_e8"], 8) * _decimal(row["price_e8"], 8)
        if _micro(gross) != row["gross_micro"]:
            raise RuntimeError("Stored trade gross disagrees with exact quantity/price")
        fees = _decimal(row["fee_micro"], 6) + _decimal(row["tax_micro"], 6)
        theoretical = gross + fees if row["side"] == "BUY" else gross - fees
        settlement = _decimal(row["settlement_micro"], 6)
        if row["side"] == "BUY" and settlement < 0:
            raise RuntimeError("A buy cannot have negative settlement cost")
        actual_local = settlement != theoretical.quantize(Decimal("0.01"))
        actual_hkd = False
        fx = row["fx_rate_hkd_e12"]
        if row["currency"] == "HKD" and fx not in (None, 10**12):
            raise RuntimeError("HKD historical FX must equal identity one")
        if row["booked_hkd_micro"] is not None:
            if fx is None and row["currency"] == "USD":
                actual_hkd = True
            else:
                rate = Decimal(1) if row["currency"] == "HKD" else _decimal(fx, 12)
                actual_hkd = row["booked_hkd_micro"] != _micro(settlement * rate)
        con.exec_driver_sql(
            "UPDATE trades SET root_id=?,created_sequence=?,rounding_delta_micro=?,"
            "settlement_override=?,hkd_override=?,override_reason=? WHERE id=?",
            (
                root_id,
                sequences["TRADE", root_id],
                _micro(settlement - theoretical),
                int(actual_local),
                int(actual_hkd),
                "Preserved pre-ledger persisted amounts; no broker verification asserted"
                if actual_local or actual_hkd
                else "",
                identifier,
            ),
        )
    for kind, table in (("TRANSFER", "security_transfers"), ("ACTION", "corporate_actions")):
        for row in _rows(con, table):
            con.exec_driver_sql(
                f"UPDATE {table} SET created_sequence=? WHERE id=?",
                (sequences[kind, row["id"]], row["id"]),
            )
            if kind == "ACTION":
                con.exec_driver_sql(
                    "UPDATE corporate_actions SET root_id=id WHERE id=?", (row["id"],)
                )


def _cash_backfill(con: Connection) -> None:
    entries = _rows(con, "cash_ledger_entries")
    primary: dict[tuple[str, str, str], RowMapping] = {}
    for row in entries:
        if (row["kind"] == "REVERSAL") != (row["entry_role"] == "REVERSAL"):
            raise RuntimeError("Legacy cash reversal role is inconsistent")
        if row["entry_role"] == "PRIMARY":
            key = row["account_id"], row["source_type"], row["source_id"]
            if key in primary:
                raise RuntimeError("Ambiguous original cash entries")
            primary[key] = row
    booked: dict[str, int | None] = {}
    for row in entries:
        if row["entry_role"] == "REVERSAL":
            continue
        amount = _decimal(row["amount_micro"], 6)
        value = None
        if row["currency"] == "HKD":
            if row["fx_rate_hkd_e12"] not in (None, 10**12):
                raise RuntimeError("HKD cash FX must equal identity one")
            value = _micro(amount)
        elif row["fx_rate_hkd_e12"] is not None:
            value = _micro(amount * _decimal(row["fx_rate_hkd_e12"], 12))
        for source, table, field, amount_field, day_field, kind, sign in (
            (
                "TRADE",
                "trades",
                "booked_hkd_micro",
                "settlement_micro",
                "trade_date",
                "TRADE_SETTLEMENT",
                None,
            ),
            ("EXPENSE", "expenses", "hkd_micro", "amount_micro", "date", "EXPENSE", -1),
            (
                "DIVIDEND_RECEIPT",
                "dividend_receipts",
                "received_hkd_micro",
                "received_micro",
                "received_date",
                "DIVIDEND_RECEIPT",
                1,
            ),
        ):
            if row["source_type"] == source:
                fact = (
                    con.exec_driver_sql(f"SELECT * FROM {table} WHERE id=?", (row["source_id"],))
                    .mappings()
                    .first()
                )
                if (
                    fact is None
                    or fact["account_id"] != row["account_id"]
                    or fact["currency"] != row["currency"]
                ):
                    raise RuntimeError("Cash entry has no matching financial source")
                direction = (1 if fact["side"] == "SELL" else -1) if sign is None else sign
                entry_fx, source_fx = row["fx_rate_hkd_e12"], fact["fx_rate_hkd_e12"]
                if row["currency"] == "HKD":
                    entry_fx = entry_fx if entry_fx is not None else 10**12
                    source_fx = source_fx if source_fx is not None else 10**12
                if (
                    row["amount_micro"] != direction * fact[amount_field]
                    or row["effective_date"] != fact[day_field]
                    or row["kind"] != kind
                    or row["external_flow"]
                    or entry_fx != source_fx
                ):
                    raise RuntimeError("Cash entry conflicts with its financial source")
                if fact[field] is not None:
                    value = direction * fact[field]
        if row["amount_micro"] == -(2**63) or (value is not None and not -MAX <= value <= MAX):
            raise RuntimeError("Legacy cash cannot be exactly reversed within INT64")
        booked[row["id"]] = value
        con.exec_driver_sql(
            "UPDATE cash_ledger_entries SET booked_hkd_micro=? WHERE id=?", (value, row["id"])
        )
    for row in entries:
        if row["entry_role"] != "REVERSAL":
            continue
        original = primary.get((row["account_id"], row["source_type"], row["source_id"]))
        if original is None:
            raise RuntimeError("Legacy reversal has no unique original cash entry")
        if (
            row["kind"] != "REVERSAL"
            or row["amount_micro"] != -original["amount_micro"]
            or any(
                row[key] != original[key]
                for key in ("currency", "fx_rate_hkd_e12", "external_flow")
            )
        ):
            raise RuntimeError("Legacy reversal does not exactly offset its original")
        value = booked[original["id"]]
        con.exec_driver_sql(
            "UPDATE cash_ledger_entries SET reverses_entry_id=?,booked_hkd_micro=? WHERE id=?",
            (original["id"], -value if value is not None else None, row["id"]),
        )


def _guards(con: Connection) -> None:
    mutable = {
        "trades": {"status", "version", "updated_at"},
        "cash_ledger_entries": {"version"},
        "expenses": {"status", "version"},
        "security_transfers": {"status", "version"},
        "corporate_actions": {"status", "version", "is_current"},
    }
    for table, allowed in mutable.items():
        columns = [row[1] for row in con.exec_driver_sql(f"PRAGMA table_info({table})")]
        changed = " OR ".join(
            f'NEW."{col}" IS NOT OLD."{col}"' for col in columns if col not in allowed
        )
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_{table}_immutable BEFORE UPDATE ON {table}
            WHEN ({changed}) OR NEW.version!=OLD.version+1
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FINANCIAL_FACT'); END
        """)
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_{table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_FINANCIAL_FACT'); END
        """)
        day = DATES[table]
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_{table}_date BEFORE INSERT ON {table}
            WHEN length(NEW.{day})!=10 OR NEW.{day} NOT GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                 OR substr(NEW.{day},1,4)='0000' OR date(NEW.{day},'+0 days') IS NOT NEW.{day}
            BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_DATE'); END
        """)
        scaled = [col for col in columns if col.endswith(("_micro", "_e8", "_e12", "_ppm"))]
        invalid = " OR ".join(
            f'(NEW."{col}" IS NOT NULL AND typeof(NEW."{col}")!=\'integer\')' for col in scaled
        )
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_{table}_scaled BEFORE INSERT ON {table}
            WHEN ({invalid or "0"}) OR typeof(NEW.version)!='integer'
            BEGIN SELECT RAISE(ABORT,'SCALED_VALUE_MUST_BE_INTEGER'); END
        """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_sequences_{operation.lower()}
            BEFORE {operation} ON ledger_sequences
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_LEDGER_SEQUENCE'); END
        """)
    for table, kind, identity in (
        ("trades", "TRADE", "root_id"),
        ("security_transfers", "TRANSFER", "id"),
        ("corporate_actions", "ACTION", "root_id"),
    ):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_{table}_sequence BEFORE INSERT ON {table}
            WHEN typeof(NEW.created_sequence)!='integer' OR NOT EXISTS(
                SELECT 1 FROM ledger_sequences WHERE sequence=NEW.created_sequence
                AND kind='{kind}' AND fact_id=NEW.{identity})
            BEGIN SELECT RAISE(ABORT,'INVALID_LEDGER_SEQUENCE'); END
        """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_trade_revision BEFORE INSERT ON trades
        WHEN NEW.root_id IS NULL OR typeof(NEW.revision)!='integer' OR
          (NEW.supersedes_id IS NULL AND (NEW.root_id!=NEW.id OR NEW.revision!=1)) OR
          (NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM trades p JOIN portfolios old ON old.id=p.portfolio_id
            JOIN portfolios current ON current.id=NEW.portfolio_id
            WHERE p.id=NEW.supersedes_id AND p.root_id=NEW.root_id
              AND p.created_sequence=NEW.created_sequence AND p.status='SUPERSEDED'
              AND NEW.revision=p.revision+1 AND old.owner_id=current.owner_id))
        BEGIN SELECT RAISE(ABORT,'INVALID_TRADE_REVISION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_trade_terms BEFORE INSERT ON trades
        WHEN NEW.currency IS NOT (SELECT quote_currency FROM securities WHERE id=NEW.security_id)
          OR (NEW.currency='HKD' AND NEW.fx_rate_hkd_e12 IS NOT NULL
              AND NEW.fx_rate_hkd_e12!=1000000000000)
          OR (NEW.currency='HKD' AND NEW.status='ACTIVE'
              AND NEW.booked_hkd_micro IS NOT NEW.settlement_micro)
          OR ((NEW.settlement_override=1 OR NEW.hkd_override=1)
              AND length(trim(NEW.override_reason))<10)
        BEGIN SELECT RAISE(ABORT,'INVALID_TRADE_CURRENCY_OR_OVERRIDE'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_action_revision BEFORE INSERT ON corporate_actions
        WHEN NEW.root_id IS NULL OR
          (NEW.supersedes_id IS NULL AND (NEW.root_id!=NEW.id OR NEW.revision!=1)) OR
          (NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM corporate_actions p WHERE p.id=NEW.supersedes_id
            AND p.root_id=NEW.root_id AND p.created_sequence=NEW.created_sequence
            AND p.is_current=0 AND NEW.revision=p.revision+1)) OR
          (NEW.ratio_num IS NOT NULL AND typeof(NEW.ratio_num)!='integer') OR
          (NEW.ratio_den IS NOT NULL AND typeof(NEW.ratio_den)!='integer')
        BEGIN SELECT RAISE(ABORT,'INVALID_ACTION_REVISION_OR_RATIO'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_expense_revision BEFORE INSERT ON expenses
        WHEN NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM expenses e JOIN accounts a ON a.id=e.account_id
          JOIN portfolios old ON old.id=a.portfolio_id
          JOIN accounts b ON b.id=NEW.account_id
          JOIN portfolios current ON current.id=b.portfolio_id
          WHERE e.id=NEW.supersedes_id AND e.status='SUPERSEDED' AND old.owner_id=current.owner_id)
        BEGIN SELECT RAISE(ABORT,'INVALID_EXPENSE_REVISION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_cash_reversal BEFORE INSERT ON cash_ledger_entries
        BEGIN
          SELECT CASE WHEN typeof(NEW.amount_micro)!='integer' OR
              (NEW.booked_hkd_micro IS NOT NULL AND typeof(NEW.booked_hkd_micro)!='integer')
            THEN RAISE(ABORT,'SCALED_VALUE_MUST_BE_INTEGER')
            WHEN NEW.amount_micro=-9223372036854775808 OR NEW.booked_hkd_micro=-9223372036854775808
            THEN RAISE(ABORT,'CASH_REVERSAL_OVERFLOW')
            WHEN (NEW.entry_role='REVERSAL' OR NEW.kind='REVERSAL'
                  OR NEW.reverses_entry_id IS NOT NULL)
              AND NOT (NEW.entry_role='REVERSAL' AND NEW.kind='REVERSAL' AND EXISTS(
                SELECT 1 FROM cash_ledger_entries p WHERE p.id=NEW.reverses_entry_id
                  AND p.entry_role='PRIMARY' AND p.account_id=NEW.account_id
                  AND p.currency=NEW.currency AND p.source_type=NEW.source_type
                  AND p.source_id=NEW.source_id AND p.fx_rate_hkd_e12 IS NEW.fx_rate_hkd_e12
                  AND p.external_flow=NEW.external_flow AND NEW.amount_micro=-p.amount_micro
                  AND NEW.booked_hkd_micro IS -p.booked_hkd_micro))
            THEN RAISE(ABORT,'INVALID_CASH_REVERSAL')
            WHEN NEW.currency='HKD' AND NEW.fx_rate_hkd_e12 IS NOT NULL
                 AND NEW.fx_rate_hkd_e12!=1000000000000
            THEN RAISE(ABORT,'INVALID_HKD_IDENTITY_FX')
            WHEN (NEW.currency='HKD' OR NEW.fx_rate_hkd_e12 IS NOT NULL)
                 AND NEW.booked_hkd_micro IS NULL
            THEN RAISE(ABORT,'MISSING_BOOKED_CASH') END;
        END
    """)

    for source, table, amount, booked, day, kind, active in (
        (
            "TRADE",
            "trades",
            "CASE p.side WHEN 'BUY' THEN -p.settlement_micro ELSE p.settlement_micro END",
            "CASE p.side WHEN 'BUY' THEN -p.booked_hkd_micro ELSE p.booked_hkd_micro END",
            "trade_date",
            "TRADE_SETTLEMENT",
            "p.status='ACTIVE'",
        ),
        (
            "EXPENSE",
            "expenses",
            "-p.amount_micro",
            "-p.hkd_micro",
            "date",
            "EXPENSE",
            "p.status='ACTIVE'",
        ),
        (
            "DIVIDEND_RECEIPT",
            "dividend_receipts",
            "p.received_micro",
            "p.received_hkd_micro",
            "received_date",
            "DIVIDEND_RECEIPT",
            "p.status='ACTIVE'",
        ),
    ):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_cash_source_{source.lower()}
            BEFORE INSERT ON cash_ledger_entries
            WHEN NEW.entry_role='PRIMARY' AND NEW.source_type='{source}' AND NOT EXISTS(
                SELECT 1 FROM {table} p WHERE p.id=NEW.source_id AND {active}
                AND p.account_id=NEW.account_id AND p.currency=NEW.currency
                AND p.{day}=NEW.effective_date AND NEW.kind='{kind}' AND NEW.external_flow=0
                AND NEW.amount_micro={amount} AND NEW.booked_hkd_micro IS {booked}
                AND NEW.fx_rate_hkd_e12 IS p.fx_rate_hkd_e12)
            BEGIN SELECT RAISE(ABORT,'CASH_SOURCE_MISMATCH'); END
        """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_cash_opening BEFORE INSERT ON cash_ledger_entries
        WHEN NEW.source_type='OPENING_BALANCE' AND NEW.entry_role='PRIMARY' AND NOT EXISTS(
          SELECT 1 FROM accounts a WHERE a.id=NEW.account_id AND a.id=NEW.source_id
          AND a.default_currency=NEW.currency AND a.opening_cash_date=NEW.effective_date
          AND a.opening_cash_micro=NEW.amount_micro
          AND NEW.kind='ADJUSTMENT' AND NEW.external_flow=0)
        BEGIN SELECT RAISE(ABORT,'OPENING_CASH_SOURCE_MISMATCH'); END
    """)
    for table in ("trades", "expenses", "security_transfers"):
        terminal = "('SUPERSEDED','VOID')" if table != "security_transfers" else "('VOID')"
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_ledger_{table}_state BEFORE UPDATE ON {table}
            WHEN OLD.status IN {terminal} OR NEW.status NOT IN {terminal}
            BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_STATE_TRANSITION'); END
        """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_action_state BEFORE UPDATE ON corporate_actions
        WHEN NOT (OLD.is_current=1 AND OLD.status!='VOID' AND (
          (NEW.is_current=0 AND NEW.status=OLD.status) OR
          (NEW.is_current=1 AND NEW.status='VOID')))
        BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_STATE_TRANSITION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_ledger_cash_state BEFORE UPDATE ON cash_ledger_entries
        WHEN OLD.entry_role!='PRIMARY' OR NOT EXISTS(
          SELECT 1 FROM cash_ledger_entries r WHERE r.reverses_entry_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'INVALID_FINANCIAL_STATE_TRANSITION'); END
    """)


def upgrade() -> None:
    con = op.get_bind()
    with localcontext(prec=64, rounding=ROUND_HALF_UP):
        _validate_dates(con)
        con.exec_driver_sql("""
            CREATE TABLE ledger_sequences(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('TRADE','TRANSFER','ACTION')),
                fact_id TEXT NOT NULL, UNIQUE(kind,fact_id))
        """)
        additions = {
            "trades": [
                "version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1)",
                "created_sequence INTEGER REFERENCES ledger_sequences(sequence)",
                "root_id TEXT REFERENCES trades(id)",
                "rounding_delta_micro INTEGER NOT NULL DEFAULT 0",
                "settlement_override INTEGER NOT NULL DEFAULT 0 "
                "CHECK(settlement_override IN (0,1))",
                "hkd_override INTEGER NOT NULL DEFAULT 0 CHECK(hkd_override IN (0,1))",
                "override_reason TEXT NOT NULL DEFAULT ''",
            ],
            "cash_ledger_entries": [
                "booked_hkd_micro INTEGER",
                "reverses_entry_id TEXT REFERENCES cash_ledger_entries(id)",
            ],
            "security_transfers": [
                "version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1)",
                "created_sequence INTEGER REFERENCES ledger_sequences(sequence)",
                "note TEXT NOT NULL DEFAULT ''",
            ],
            "corporate_actions": [
                "created_sequence INTEGER REFERENCES ledger_sequences(sequence)",
                "root_id TEXT REFERENCES corporate_actions(id)",
                "supersedes_id TEXT REFERENCES corporate_actions(id)",
                "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1)",
                "is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1))",
            ],
        }
        for table, columns in additions.items():
            for column in columns:
                con.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column}")
        _sequences(con)
        _cash_backfill(con)
        for table in DATES:
            if con.exec_driver_sql(
                f"SELECT 1 FROM {table} WHERE typeof(version)!='integer' LIMIT 1"
            ).first():
                raise RuntimeError("Financial versions must be integers")
        for name, table, key_columns, condition in (
            ("trade_current_root", "trades", "root_id", "status NOT IN ('SUPERSEDED','VOID')"),
            ("trade_predecessor", "trades", "supersedes_id", "supersedes_id IS NOT NULL"),
            ("action_current_root", "corporate_actions", "root_id", "is_current=1"),
            (
                "action_predecessor",
                "corporate_actions",
                "supersedes_id",
                "supersedes_id IS NOT NULL",
            ),
            ("expense_predecessor", "expenses", "supersedes_id", "supersedes_id IS NOT NULL"),
            (
                "cash_exact_reversal",
                "cash_ledger_entries",
                "reverses_entry_id",
                "reverses_entry_id IS NOT NULL",
            ),
        ):
            con.exec_driver_sql(
                f"CREATE UNIQUE INDEX uq_{name} ON {table}({key_columns}) WHERE {condition}"
            )
        con.exec_driver_sql(
            "CREATE INDEX idx_ledger_trade_order "
            "ON trades(portfolio_id,trade_date,executed_at_utc,created_sequence,id)"
        )
        _guards(con)
        for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
            con.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,"
                "before_sha256,after_sha256,reason,created_at) "
                "VALUES (?,?,'schema',?,'MIGRATE_LEDGER',?,?,?,?)",
                (
                    str(uuid4()),
                    owner,
                    revision,
                    sha256(down_revision.encode()).hexdigest(),
                    sha256(revision.encode()).hexdigest(),
                    "Local operator schema upgrade; original financial values preserved",
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ),
            )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of removing ledger guards")
