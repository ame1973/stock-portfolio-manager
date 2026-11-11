"""Retain additive, unverified dividend projection-version snapshots."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0020_dividend_projection_history"
down_revision = "0019_trade_correction_scope"
branch_labels = None
depends_on = None


def _uuid(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=36
        AND substr({value},9,1)='-' AND substr({value},14,1)='-'
        AND substr({value},19,1)='-' AND substr({value},24,1)='-'
        AND length(replace({value},'-',''))=32
        AND replace({value},'-','') NOT GLOB '*[^0-9a-f]*')"""


def _hash(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0
        AND length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"""


def _day(value: str) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=10
        AND substr({value},1,4)!='0000' AND date({value},'+0 days') IS {value})"""


def _utc(value: str) -> str:
    day = f"substr({value},1,10)"
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=27
        AND {_day(day)} AND substr({value},11,1)='T'
        AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _text(value: str, minimum: int, maximum: int) -> str:
    return f"""(typeof({value})='text' AND instr({value},char(0))=0
        AND length({value}) BETWEEN {minimum} AND {maximum}
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' '))"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_projection_versions (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT
                CHECK({_uuid("revision_id")}),
            portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT
                CHECK({_uuid("portfolio_id")}),
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT
                CHECK({_uuid("account_id")}),
            broker_id TEXT NOT NULL REFERENCES brokers(id) ON DELETE RESTRICT
                CHECK({_uuid("broker_id")}),
            account_version INTEGER NOT NULL
                CHECK(typeof(account_version)='integer' AND account_version>0),
            broker_version INTEGER NOT NULL
                CHECK(typeof(broker_version)='integer' AND broker_version>0),
            projection_no INTEGER NOT NULL
                CHECK(typeof(projection_no)='integer' AND projection_no>0),
            previous_id TEXT REFERENCES dividend_projection_versions(id) ON DELETE RESTRICT
                CHECK(previous_id IS NULL OR {_uuid("previous_id")}),
            state TEXT NOT NULL
                CHECK(state IN ('EXPECTED','NO_POSITION','NEEDS_REVIEW','CANCELLED')),
            as_of TEXT NOT NULL CHECK({_day("as_of")}),
            eligibility_rule TEXT NOT NULL
                CHECK(eligibility_rule IN ('ORDINARY_PRE_EX_DATE','REQUIRES_REVIEW')),
            portfolio_data_revision INTEGER NOT NULL
                CHECK(typeof(portfolio_data_revision)='integer' AND portfolio_data_revision>=0),
            retired_trade_scope TEXT NOT NULL CHECK(retired_trade_scope IN
                ('NO_RETIRED_TRADES','RETIRED_TRADE_HISTORY_UNVERIFIED','RETIRED_TRADE_HASHES_MATCHED')),
            creation_audit_scope TEXT NOT NULL CHECK(creation_audit_scope IN
                ('NO_RECORDED_CREATION_GAP_REVIEW','CREATION_GAP_REVIEWED_UNVERIFIED')),
            history_completeness TEXT NOT NULL CHECK(history_completeness='UNVERIFIED'),
            eligible_shares_e8 INTEGER,
            gross_micro INTEGER,
            tax_micro INTEGER,
            fee_micro INTEGER,
            expected_net_micro INTEGER,
            rounding_delta_micro INTEGER,
            currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
            fx_rate_hkd_e12 INTEGER,
            fx_source TEXT,
            expected_hkd_micro INTEGER,
            tax_rate_snapshot_ppm INTEGER,
            fee_original_micro INTEGER,
            fee_original_currency TEXT,
            fee_rate_event_e12 INTEGER,
            fee_rate_source TEXT,
            quality TEXT,
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(event_id,account_id,projection_no),
            UNIQUE(event_id,account_id,request_id),
            CHECK(COALESCE(CASE WHEN state='EXPECTED' THEN
                eligibility_rule='ORDINARY_PRE_EX_DATE'
                AND typeof(eligible_shares_e8)='integer' AND eligible_shares_e8>0
                AND typeof(gross_micro)='integer' AND gross_micro>=0
                AND typeof(tax_micro)='integer' AND tax_micro>=0
                AND typeof(fee_micro)='integer' AND fee_micro>=0
                AND typeof(expected_net_micro)='integer'
                AND typeof(rounding_delta_micro)='integer'
                AND typeof(tax_rate_snapshot_ppm)='integer'
                    AND tax_rate_snapshot_ppm BETWEEN 0 AND 1000000
                AND typeof(fee_original_micro)='integer' AND fee_original_micro>=0
                AND fee_original_currency IN ('HKD','USD')
                AND ((fee_original_currency=currency
                      AND typeof(fee_rate_event_e12)='integer'
                      AND fee_rate_event_e12=1000000000000 AND fee_rate_source='IDENTITY')
                     OR (fee_original_currency!=currency AND (
                          (fee_original_micro=0 AND fee_micro=0
                           AND fee_rate_event_e12 IS NULL AND fee_rate_source IS NULL)
                          OR (typeof(fee_rate_event_e12)='integer' AND fee_rate_event_e12>0
                              AND {_text("fee_rate_source", 1, 100)}
                              AND fee_rate_source!='IDENTITY'))))
                AND quality IN ('OK','MISSING_QUOTE','MISSING_FX','NOT_COMPUTABLE','NEEDS_REVIEW')
                AND (expected_net_micro>=0 OR quality='NEEDS_REVIEW')
                AND ((currency='HKD' AND fx_rate_hkd_e12=1000000000000
                      AND fx_source='IDENTITY' AND typeof(expected_hkd_micro)='integer'
                      AND quality!='MISSING_FX')
                  OR (currency='USD' AND (
                       (fx_rate_hkd_e12 IS NULL AND fx_source IS NULL AND expected_hkd_micro IS NULL
                         AND quality IN ('MISSING_FX','NEEDS_REVIEW'))
                    OR (typeof(fx_rate_hkd_e12)='integer' AND fx_rate_hkd_e12>0
                         AND {_text("fx_source", 1, 100)} AND fx_source!='IDENTITY'
                         AND typeof(expected_hkd_micro)='integer' AND quality!='MISSING_FX'))))
              ELSE eligible_shares_e8 IS NULL AND gross_micro IS NULL AND tax_micro IS NULL
                AND fee_micro IS NULL AND expected_net_micro IS NULL
                AND rounding_delta_micro IS NULL AND fx_rate_hkd_e12 IS NULL
                AND fx_source IS NULL AND expected_hkd_micro IS NULL
                AND tax_rate_snapshot_ppm IS NULL AND fee_original_micro IS NULL
                AND fee_original_currency IS NULL AND fee_rate_event_e12 IS NULL
                AND fee_rate_source IS NULL AND quality IS NULL END,0))
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_projection_owner_event
        ON dividend_projection_versions(owner_id,event_id,account_id,projection_no)
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_projection_versions_insert
        BEFORE INSERT ON dividend_projection_versions
        WHEN EXISTS(SELECT 1 FROM dividend_projection_versions old
                    WHERE old.id=NEW.id OR old.audit_id=NEW.audit_id
                       OR (old.event_id=NEW.event_id AND old.account_id=NEW.account_id
                           AND (old.projection_no=NEW.projection_no
                                OR old.request_id=NEW.request_id)))
          OR NOT EXISTS(
              SELECT 1 FROM dividend_events event
              JOIN dividend_event_controls ctl ON ctl.event_id=event.id
              JOIN owners owner ON owner.id=ctl.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions rev ON rev.id=NEW.revision_id AND rev.event_id=event.id
              JOIN dividend_revision_decisions decision ON decision.revision_id=rev.id
                   AND decision.event_id=event.id AND decision.owner_id=ctl.owner_id
              JOIN audit_events source_audit ON source_audit.entity_type='dividend_event_revision'
                   AND source_audit.entity_id=rev.id
                   AND source_audit.action='DIVIDEND_EVENT_REVISED'
                   AND source_audit.owner_id=ctl.owner_id
                   AND source_audit.request_id=decision.request_id
              JOIN accounts account ON account.id=NEW.account_id
                   AND account.portfolio_id=NEW.portfolio_id AND account.broker_id=NEW.broker_id
                   AND account.version=NEW.account_version
              JOIN portfolios portfolio ON portfolio.id=NEW.portfolio_id
                   AND portfolio.owner_id=ctl.owner_id
              JOIN brokers broker ON broker.id=NEW.broker_id AND broker.owner_id=ctl.owner_id
                   AND broker.version=NEW.broker_version
              JOIN audit_events projection_audit ON projection_audit.id=NEW.audit_id
              WHERE event.id=NEW.event_id AND event.provenance_version=1
                AND event.current_revision_id=rev.id AND event.version=rev.revision_no
                AND event.workflow_status=rev.review_status
                AND ctl.owner_id=NEW.owner_id
                AND portfolio.data_revision=NEW.portfolio_data_revision
                AND rev.currency=NEW.currency AND {_hash("source_audit.after_sha256")}
                AND {_utc("source_audit.created_at")}
                AND NEW.created_at>=rev.created_at AND NEW.created_at>=source_audit.created_at
                AND source_audit.reason=rev.reason
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=rev.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
                AND (NEW.state!='EXPECTED' OR rev.review_status IN
                     ('PROVISIONAL','CORROBORATED','VERIFIED'))
                AND projection_audit.owner_id=NEW.owner_id
                AND projection_audit.entity_type='dividend_projection'
                AND projection_audit.entity_id=NEW.id
                AND projection_audit.action='DIVIDEND_PROJECTION_CREATED'
                AND projection_audit.request_id=NEW.request_id
                AND projection_audit.created_at=NEW.created_at
                AND projection_audit.before_sha256 IS NULL
                AND {_hash("projection_audit.after_sha256")}
                AND {_text("projection_audit.reason", 10, 1000)}
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_projection' AND a.entity_id=NEW.id
                       AND a.action='DIVIDEND_PROJECTION_CREATED')=1)
          OR NOT ((NEW.projection_no=1 AND NEW.previous_id IS NULL) OR EXISTS(
              SELECT 1 FROM dividend_projection_versions previous
              JOIN dividend_event_revisions old_revision ON old_revision.id=previous.revision_id
              JOIN dividend_event_revisions new_revision ON new_revision.id=NEW.revision_id
              WHERE previous.id=NEW.previous_id AND previous.owner_id=NEW.owner_id
                AND previous.event_id=NEW.event_id AND previous.account_id=NEW.account_id
                AND previous.projection_no=NEW.projection_no-1
                AND old_revision.revision_no<=new_revision.revision_no
                AND previous.created_at<=NEW.created_at))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_PROJECTION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_projection_blocks_revision
        BEFORE UPDATE OF current_revision_id ON dividend_events
        WHEN NEW.current_revision_id IS NOT OLD.current_revision_id
          AND EXISTS(SELECT 1 FROM dividend_projection_versions history
                     WHERE history.event_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'DIVIDEND_PROJECTION_REBUILD_REQUIRED'); END
    """)
    for operation in ("UPDATE", "DELETE"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_dividend_projection_versions_{operation.lower()}
            BEFORE {operation} ON dividend_projection_versions
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_PROJECTION'); END
        """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_PROJECTION_HISTORY',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; preserve dividend facts without automatic adoption",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a validated pre-upgrade backup instead of deleting projection history"
    )
