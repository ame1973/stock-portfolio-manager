"""Bind explicit tax-setting provenance to immutable dividend projections."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0022_dividend_projection_tax"
down_revision = "0021_dividend_tax_settings"
branch_labels = None
depends_on = None


def _uuid(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=36
        AND substr({value},9,1)='-' AND substr({value},14,1)='-'
        AND substr({value},19,1)='-' AND substr({value},24,1)='-'
        AND length(replace({value},'-',''))=32
        AND replace({value},'-','') NOT GLOB '*[^0-9a-f]*')"""


def _day(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=10
        AND substr({value},1,4)!='0000' AND date({value},'+0 days') IS {value})"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql("""
        ALTER TABLE dividend_projection_versions ADD COLUMN tax_provenance_version INTEGER
        CHECK(tax_provenance_version IS NULL OR
              (typeof(tax_provenance_version)='integer' AND tax_provenance_version=1))
    """)
    con.exec_driver_sql(f"""
        ALTER TABLE dividend_projection_versions ADD COLUMN tax_effective_on TEXT
        CHECK(tax_effective_on IS NULL OR {_day("tax_effective_on")})
    """)
    con.exec_driver_sql("""
        ALTER TABLE dividend_projection_versions ADD COLUMN tax_observed_head_no INTEGER
        CHECK(tax_observed_head_no IS NULL OR
              (typeof(tax_observed_head_no)='integer' AND tax_observed_head_no>=0))
    """)
    for column in ("tax_observed_head_id", "tax_selected_setting_id"):
        con.exec_driver_sql(f"""
            ALTER TABLE dividend_projection_versions ADD COLUMN {column} TEXT
            REFERENCES dividend_tax_settings(id) ON DELETE RESTRICT
            CHECK({column} IS NULL OR {_uuid(column)})
        """)
    con.exec_driver_sql("""
        ALTER TABLE dividend_projection_versions ADD COLUMN tax_estimate_status TEXT
        CHECK(tax_estimate_status IS NULL OR tax_estimate_status IN
              ('MISSING','UNKNOWN','ESTIMATED'))
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_projection_tax_insert
        BEFORE INSERT ON dividend_projection_versions
        WHEN (NEW.tax_provenance_version IS NULL AND NOT (
                 NEW.tax_effective_on IS NULL AND NEW.tax_observed_head_no IS NULL
                 AND NEW.tax_observed_head_id IS NULL AND NEW.tax_selected_setting_id IS NULL
                 AND NEW.tax_estimate_status IS NULL))
          OR (NEW.tax_provenance_version=1 AND NOT COALESCE((
              NEW.tax_effective_on IS NOT NULL AND NEW.tax_observed_head_no IS NOT NULL
              AND NEW.tax_estimate_status IS NOT NULL
              AND ((NEW.tax_observed_head_no=0 AND NEW.tax_observed_head_id IS NULL)
                OR (NEW.tax_observed_head_no>0 AND EXISTS(
                    SELECT 1 FROM dividend_tax_settings head
                    WHERE head.id=NEW.tax_observed_head_id AND head.owner_id=NEW.owner_id
                      AND head.event_id=NEW.event_id AND head.revision_id=NEW.revision_id
                      AND head.account_id=NEW.account_id
                      AND head.setting_no=NEW.tax_observed_head_no
                      AND head.created_at<=NEW.created_at)))
              AND NOT EXISTS(
                  SELECT 1 FROM dividend_tax_settings later
                  WHERE later.event_id=NEW.event_id AND later.revision_id=NEW.revision_id
                    AND later.account_id=NEW.account_id
                    AND later.setting_no>NEW.tax_observed_head_no)
              AND NEW.tax_selected_setting_id IS (
                  SELECT selected.id FROM dividend_tax_settings selected
                  WHERE selected.owner_id=NEW.owner_id AND selected.event_id=NEW.event_id
                    AND selected.revision_id=NEW.revision_id AND selected.account_id=NEW.account_id
                    AND selected.setting_no<=NEW.tax_observed_head_no
                    AND selected.effective_from<=NEW.tax_effective_on
                  ORDER BY selected.effective_from DESC,selected.setting_no DESC LIMIT 1)
              AND ((NEW.tax_selected_setting_id IS NULL AND NEW.tax_estimate_status='MISSING'
                    AND NEW.state!='EXPECTED') OR EXISTS(
                  SELECT 1 FROM dividend_tax_settings selected
                  WHERE selected.id=NEW.tax_selected_setting_id
                    AND selected.owner_id=NEW.owner_id AND selected.event_id=NEW.event_id
                    AND selected.revision_id=NEW.revision_id AND selected.account_id=NEW.account_id
                    AND selected.portfolio_id=NEW.portfolio_id AND selected.broker_id=NEW.broker_id
                    AND selected.account_version=NEW.account_version
                    AND selected.broker_version=NEW.broker_version
                    AND selected.tax_status=NEW.tax_estimate_status
                    AND (NEW.state!='EXPECTED' OR
                         (selected.tax_status='ESTIMATED'
                          AND selected.rate_ppm=NEW.tax_rate_snapshot_ppm))))
          ),0))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_PROJECTION_TAX'); END
    """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_PROJECTION_TAX',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; preserve explicit projection tax provenance",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of removing tax provenance")
