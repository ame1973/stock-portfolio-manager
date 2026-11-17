"""Preserve legacy receipts while allowing explicit unknown tax and fee amounts."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0027_receipt_unknowns"
down_revision = "0026_dividend_projection_fee"
branch_labels = None
depends_on = None

_TEMP = "_spm_receipt_unknowns_0027"
_REQUIRED = {
    "idx_dividend_receipt_entitlement",
    "idx_receipts_account_date",
    "trg_dividend_receipt_account_insert",
    "trg_dividend_receipt_account_update",
    "trg_managed_dividend_receipt_delete",
    "trg_managed_dividend_receipt_insert",
    "trg_managed_dividend_receipt_update",
    "trg_scaled_dividend_receipts_insert",
    "trg_scaled_dividend_receipts_update",
}


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def upgrade() -> None:
    con = op.get_bind()
    sql = con.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='dividend_receipts'"
    ).scalar_one()
    prefix = "CREATE TABLE dividend_receipts"
    if not isinstance(sql, str) or not sql.startswith(prefix):
        raise RuntimeError("Unexpected receipt table definition; migration refused")
    for column in ("tax_micro", "fee_micro"):
        original = f"{column} INTEGER NOT NULL DEFAULT 0"
        if sql.count(original) != 1:
            raise RuntimeError("Unexpected receipt component definition; migration refused")
        sql = sql.replace(original, f"{column} INTEGER", 1)
    temporary_sql = sql.replace(prefix, f"CREATE TABLE {_quote(_TEMP)}", 1)
    objects = con.exec_driver_sql("""
        SELECT type,name,sql FROM sqlite_schema
        WHERE tbl_name='dividend_receipts' COLLATE NOCASE
          AND type IN ('index','trigger') AND sql IS NOT NULL
        ORDER BY type,name
    """).all()
    if not {row.name for row in objects} >= _REQUIRED:
        raise RuntimeError("Missing receipt safety guard; migration refused")
    definitions: list[str] = []
    for row in objects:
        definition = row.sql
        if row.name == "trg_managed_dividend_receipt_insert":
            for column in ("tax_micro", "fee_micro"):
                original = f"typeof(NEW.{column}) IS NOT 'integer' OR NEW.{column}<0"
                if definition.count(original) != 1:
                    raise RuntimeError("Unexpected receipt safety guard; migration refused")
                replacement = (
                    f"(NEW.{column} IS NOT NULL AND "
                    f"(typeof(NEW.{column}) IS NOT 'integer' OR NEW.{column}<0))"
                )
                definition = definition.replace(original, replacement, 1)
        definitions.append(definition)
    tables = con.exec_driver_sql(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).scalars()
    for table in tables:
        foreign_keys = con.exec_driver_sql(f"PRAGMA foreign_key_list({_quote(table)})")
        if any(str(row[2]).lower() == "dividend_receipts" for row in foreign_keys):
            raise RuntimeError("Inbound receipt foreign key prevents safe migration")
    all_columns = con.exec_driver_sql("PRAGMA table_xinfo(dividend_receipts)").all()
    if any(str(row[1]).lower() in {"rowid", "_rowid_", "oid"} for row in all_columns):
        raise RuntimeError("A receipt column shadows rowid; migration refused")
    expected_columns = []
    for row in all_columns:
        expected = list(row)
        if row[1] in ("tax_micro", "fee_micro"):
            if row[2] != "INTEGER" or row[3] != 1 or row[4] != "0":
                raise RuntimeError("Unexpected receipt column metadata; migration refused")
            expected[3], expected[4] = 0, None
        expected_columns.append(tuple(expected))
    columns = [row[1] for row in con.exec_driver_sql("PRAGMA table_info(dividend_receipts)")]
    names = ",".join(_quote(column) for column in columns)
    previous_legacy = con.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one()
    try:
        # External event/cash triggers must keep their original receipt table reference.
        con.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        con.exec_driver_sql(temporary_sql)
        con.exec_driver_sql(
            f"INSERT INTO {_quote(_TEMP)} (rowid,{names}) "
            f"SELECT rowid,{names} FROM dividend_receipts ORDER BY rowid"
        )
        original_count = con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one()
        copied_count = con.exec_driver_sql(f"SELECT count(*) FROM {_quote(_TEMP)}").scalar_one()
        differences = " OR ".join(
            f"(typeof(original.{_quote(row[1])}) IS NOT typeof(copied.{_quote(row[1])}) OR "
            f"(original.{_quote(row[1])} COLLATE BINARY) IS NOT "
            f"(copied.{_quote(row[1])} COLLATE BINARY))"
            for row in all_columns
        )
        mismatch = con.exec_driver_sql(
            f"SELECT 1 FROM dividend_receipts original LEFT JOIN {_quote(_TEMP)} copied "
            f"ON copied.rowid=original.rowid WHERE copied.rowid IS NULL OR {differences} LIMIT 1"
        ).first()
        if original_count != copied_count or mismatch is not None:
            raise RuntimeError("Receipt copy mismatch; migration refused")
        con.exec_driver_sql("DROP TABLE dividend_receipts")
        con.exec_driver_sql(f"ALTER TABLE {_quote(_TEMP)} RENAME TO dividend_receipts")
        actual_columns = con.exec_driver_sql("PRAGMA table_xinfo(dividend_receipts)").all()
        if [tuple(row) for row in actual_columns] != expected_columns:
            raise RuntimeError("Receipt column metadata changed; migration refused")
        for definition in definitions:
            con.exec_driver_sql(definition)
        if con.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("Receipt foreign key check failed; migration refused")
        for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
            con.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,created_at) VALUES (?,?,'schema',? ,?,?,?, ?,?)",
                (
                    str(uuid4()),
                    owner,
                    revision,
                    "MIGRATE_DIVIDEND_RECEIPT_UNKNOWNS",
                    sha256(down_revision.encode()).hexdigest(),
                    sha256(revision.encode()).hexdigest(),
                    "Local operator upgrade; preserve unknown receipt tax and fee",
                    datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                ),
            )
    finally:
        con.exec_driver_sql(f"PRAGMA legacy_alter_table={1 if previous_legacy else 0}")


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of removing receipt history")
