"""Add inert, completion-bound storage for future dividend revision rebuilds."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0031_dividend_rebuild"
down_revision = "0030_direct_receipts"
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
    return f"""(typeof({value})='text' AND instr({value},char(0))=0 AND length({value})=27
        AND {_day(f"substr({value},1,10)")} AND substr({value},11,1)='T'
        AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_revision_rebuild_operations (
            id TEXT NOT NULL PRIMARY KEY CHECK({_uuid("id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            previous_revision_id TEXT NOT NULL
                REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT
                CHECK({_uuid("previous_revision_id")}),
            previous_revision_no INTEGER NOT NULL
                CHECK(typeof(previous_revision_no)='integer'
                      AND previous_revision_no BETWEEN 1 AND 2147483646),
            new_revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id)
                DEFERRABLE INITIALLY DEFERRED CHECK({_uuid("new_revision_id")}),
            new_revision_no INTEGER NOT NULL
                CHECK(typeof(new_revision_no)='integer'
                      AND new_revision_no=previous_revision_no+1),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            intent_sha256 TEXT NOT NULL CHECK({_hash("intent_sha256")}),
            scope_sha256 TEXT NOT NULL CHECK({_hash("scope_sha256")}),
            as_of TEXT NOT NULL CHECK({_day("as_of")}),
            eligibility_rule TEXT NOT NULL
                CHECK(eligibility_rule IN ('ORDINARY_PRE_EX_DATE','REQUIRES_REVIEW')),
            account_count INTEGER NOT NULL
                CHECK(typeof(account_count)='integer' AND account_count>=0),
            portfolio_count INTEGER NOT NULL
                CHECK(typeof(portfolio_count)='integer' AND portfolio_count>=0),
            completion_id TEXT NOT NULL REFERENCES
                dividend_revision_rebuild_completions(operation_id)
                DEFERRABLE INITIALLY DEFERRED CHECK(completion_id=id),
            created_at TEXT NOT NULL CHECK({_utc("created_at")}),
            UNIQUE(owner_id,request_id),
            UNIQUE(event_id,new_revision_id),
            CHECK(previous_revision_id!=new_revision_id)
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_revision_rebuild_accounts (
            operation_id TEXT NOT NULL REFERENCES dividend_revision_rebuild_operations(id)
                ON DELETE RESTRICT CHECK({_uuid("operation_id")}),
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT
                CHECK({_uuid("account_id")}),
            portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT
                CHECK({_uuid("portfolio_id")}),
            broker_id TEXT NOT NULL REFERENCES brokers(id) ON DELETE RESTRICT
                CHECK({_uuid("broker_id")}),
            account_version INTEGER NOT NULL
                CHECK(typeof(account_version)='integer' AND account_version>0),
            portfolio_version INTEGER NOT NULL
                CHECK(typeof(portfolio_version)='integer' AND portfolio_version>0),
            portfolio_data_revision INTEGER NOT NULL
                CHECK(typeof(portfolio_data_revision)='integer' AND portfolio_data_revision>=0),
            broker_version INTEGER NOT NULL
                CHECK(typeof(broker_version)='integer' AND broker_version>0),
            account_status TEXT NOT NULL CHECK(account_status IN ('ACTIVE','ARCHIVED')),
            broker_status TEXT NOT NULL CHECK(broker_status IN ('ACTIVE','ARCHIVED')),
            eligibility_state TEXT NOT NULL CHECK(eligibility_state IN
                ('RECORDED_PRE_EX_DATE','UPCOMING_RECORDED_HOLDINGS','NEEDS_REVIEW')),
            recorded_shares_e8 INTEGER,
            disposition TEXT NOT NULL CHECK(disposition IN
                ('REBUILD_REQUIRED','NO_RECORDED_IMPACT')),
            previous_projection_id TEXT REFERENCES dividend_projection_versions(id) ON DELETE
                RESTRICT
                CHECK(previous_projection_id IS NULL OR {_uuid("previous_projection_id")}),
            projection_id TEXT REFERENCES dividend_projection_versions(id)
                DEFERRABLE INITIALLY DEFERRED CHECK(projection_id IS NULL OR
                    {_uuid("projection_id")}),
            review_id TEXT REFERENCES review_items(id)
                DEFERRABLE INITIALLY DEFERRED CHECK(review_id IS NULL OR {_uuid("review_id")}),
            review_audit_id TEXT REFERENCES audit_events(id)
                DEFERRABLE INITIALLY DEFERRED
                CHECK(review_audit_id IS NULL OR {_uuid("review_audit_id")}),
            PRIMARY KEY(operation_id,account_id),
            UNIQUE(projection_id), UNIQUE(review_id), UNIQUE(review_audit_id),
            CHECK(COALESCE((
                (eligibility_state='NEEDS_REVIEW' AND recorded_shares_e8 IS NULL)
                OR (eligibility_state IN ('RECORDED_PRE_EX_DATE','UPCOMING_RECORDED_HOLDINGS')
                    AND typeof(recorded_shares_e8)='integer' AND recorded_shares_e8>=0)
            ),0)),
            CHECK(COALESCE((
                (disposition='NO_RECORDED_IMPACT' AND eligibility_state='RECORDED_PRE_EX_DATE'
                    AND recorded_shares_e8=0 AND previous_projection_id IS NULL
                    AND projection_id IS NULL AND review_id IS NULL AND review_audit_id IS NULL)
                OR (disposition='REBUILD_REQUIRED' AND projection_id IS NOT NULL
                    AND review_id IS NOT NULL AND review_audit_id IS NOT NULL)
            ),0))
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_revision_rebuild_portfolios (
            operation_id TEXT NOT NULL REFERENCES dividend_revision_rebuild_operations(id)
                ON DELETE RESTRICT CHECK({_uuid("operation_id")}),
            portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT
                CHECK({_uuid("portfolio_id")}),
            portfolio_version INTEGER NOT NULL
                CHECK(typeof(portfolio_version)='integer' AND portfolio_version>0),
            initial_data_revision INTEGER NOT NULL
                CHECK(typeof(initial_data_revision)='integer' AND initial_data_revision>=0),
            final_data_revision INTEGER NOT NULL
                CHECK(typeof(final_data_revision)='integer'
                      AND final_data_revision>initial_data_revision),
            invalidation_id TEXT NOT NULL REFERENCES projection_invalidations(id)
                DEFERRABLE INITIALLY DEFERRED CHECK({_uuid("invalidation_id")}),
            PRIMARY KEY(operation_id,portfolio_id), UNIQUE(invalidation_id)
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_revision_rebuild_completions (
            operation_id TEXT NOT NULL PRIMARY KEY
                REFERENCES dividend_revision_rebuild_operations(id) ON DELETE RESTRICT
                CHECK({_uuid("operation_id")}),
            audit_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id) ON DELETE RESTRICT
                CHECK({_uuid("audit_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    con.exec_driver_sql("""
        CREATE INDEX idx_dividend_rebuild_event
        ON dividend_revision_rebuild_operations(owner_id,event_id,new_revision_no)
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_rebuild_operation_insert
        BEFORE INSERT ON dividend_revision_rebuild_operations
        WHEN EXISTS(SELECT 1 FROM dividend_revision_rebuild_operations existing
                    WHERE existing.id=NEW.id
                       OR (existing.owner_id=NEW.owner_id AND existing.request_id=NEW.request_id)
                       OR (existing.event_id=NEW.event_id AND
                           existing.new_revision_id=NEW.new_revision_id))
          OR EXISTS(SELECT 1 FROM dividend_event_revisions WHERE id=NEW.new_revision_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_events event
              JOIN dividend_event_controls control ON control.event_id=event.id
              JOIN owners owner ON owner.id=control.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions previous ON previous.id=event.current_revision_id
                   AND previous.event_id=event.id
              JOIN dividend_revision_decisions previous_decision
                   ON previous_decision.revision_id=previous.id
                  AND previous_decision.event_id=event.id
                  AND previous_decision.owner_id=control.owner_id
              JOIN audit_events previous_audit
                   ON previous_audit.entity_type='dividend_event_revision'
                  AND previous_audit.entity_id=previous.id
                  AND previous_audit.action='DIVIDEND_EVENT_REVISED'
                  AND previous_audit.owner_id=control.owner_id
                  AND previous_audit.request_id=previous_decision.request_id
              WHERE event.id=NEW.event_id AND control.owner_id=NEW.owner_id
                AND event.provenance_version=1 AND
                    event.current_revision_id=NEW.previous_revision_id
                AND event.version=NEW.previous_revision_no AND previous.revision_no=event.version
                AND previous.created_at<=previous_audit.created_at
                AND previous_audit.created_at<=NEW.created_at
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_event_revision' AND a.entity_id=previous.id
                       AND a.action='DIVIDEND_EVENT_REVISED')=1
          )
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_REBUILD_OPERATION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_rebuild_account_insert
        BEFORE INSERT ON dividend_revision_rebuild_accounts
        WHEN EXISTS(SELECT 1 FROM dividend_revision_rebuild_accounts existing
                    WHERE (existing.operation_id=NEW.operation_id AND
                        existing.account_id=NEW.account_id)
                       OR (NEW.projection_id IS NOT NULL AND
                           existing.projection_id=NEW.projection_id)
                       OR (NEW.review_id IS NOT NULL AND existing.review_id=NEW.review_id)
                       OR (NEW.review_audit_id IS NOT NULL AND
                           existing.review_audit_id=NEW.review_audit_id))
          OR EXISTS(SELECT 1 FROM dividend_revision_rebuild_completions done
                    WHERE done.operation_id=NEW.operation_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_revision_rebuild_operations operation
              JOIN accounts account ON account.id=NEW.account_id
              JOIN portfolios portfolio ON portfolio.id=account.portfolio_id
              JOIN brokers broker ON broker.id=account.broker_id
              WHERE operation.id=NEW.operation_id AND portfolio.owner_id=operation.owner_id
                AND broker.owner_id=operation.owner_id AND account.portfolio_id=NEW.portfolio_id
                AND account.broker_id=NEW.broker_id AND account.version=NEW.account_version
                AND portfolio.version=NEW.portfolio_version
                AND portfolio.data_revision=NEW.portfolio_data_revision
                AND broker.version=NEW.broker_version AND account.status=NEW.account_status
                AND broker.status=NEW.broker_status
                AND NEW.previous_projection_id IS (
                    SELECT p.id FROM dividend_projection_versions p
                    WHERE p.event_id=operation.event_id AND p.account_id=NEW.account_id
                    ORDER BY p.projection_no DESC LIMIT 1)
          )
          OR (NEW.disposition='NO_RECORDED_IMPACT' AND EXISTS(
              SELECT 1 FROM dividend_revision_rebuild_operations operation
              WHERE operation.id=NEW.operation_id AND (
                  EXISTS(SELECT 1 FROM dividend_entitlements ent
                         JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                         WHERE rev.event_id=operation.event_id AND ent.account_id=NEW.account_id)
                  OR EXISTS(SELECT 1 FROM dividend_projection_versions p
                            WHERE p.event_id=operation.event_id AND p.account_id=NEW.account_id)
                  OR EXISTS(SELECT 1 FROM dividend_direct_receipts r
                            WHERE r.event_id=operation.event_id AND r.account_id=NEW.account_id)
                  OR EXISTS(SELECT 1 FROM dividend_direct_receipt_origins origin
                            WHERE origin.event_id=operation.event_id AND
                                origin.account_id=NEW.account_id)
                  OR EXISTS(SELECT 1 FROM dividend_receipt_origins origin
                            WHERE origin.event_id=operation.event_id AND
                                origin.account_id=NEW.account_id)
              )))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_REBUILD_ACCOUNT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_rebuild_portfolio_insert
        BEFORE INSERT ON dividend_revision_rebuild_portfolios
        WHEN EXISTS(SELECT 1 FROM dividend_revision_rebuild_portfolios existing
                    WHERE (existing.operation_id=NEW.operation_id
                           AND existing.portfolio_id=NEW.portfolio_id)
                       OR existing.invalidation_id=NEW.invalidation_id)
          OR EXISTS(SELECT 1 FROM dividend_revision_rebuild_completions done
                    WHERE done.operation_id=NEW.operation_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_revision_rebuild_operations operation
              JOIN portfolios portfolio ON portfolio.id=NEW.portfolio_id
              WHERE operation.id=NEW.operation_id AND portfolio.owner_id=operation.owner_id
                AND portfolio.version=NEW.portfolio_version
                AND portfolio.data_revision=NEW.final_data_revision)
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_REBUILD_PORTFOLIO'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_rebuild_completion_insert
        BEFORE INSERT ON dividend_revision_rebuild_completions
        WHEN EXISTS(SELECT 1 FROM dividend_revision_rebuild_completions existing
                    WHERE existing.operation_id=NEW.operation_id OR existing.audit_id=NEW.audit_id)
          OR NOT EXISTS(
              SELECT 1 FROM dividend_revision_rebuild_operations operation
              JOIN dividend_events event ON event.id=operation.event_id
              JOIN dividend_event_controls control ON control.event_id=event.id
                   AND control.owner_id=operation.owner_id
              JOIN owners owner ON owner.id=control.owner_id AND owner.is_active=1
              JOIN dividend_event_revisions previous ON previous.id=operation.previous_revision_id
                   AND previous.event_id=event.id AND
                       previous.revision_no=operation.previous_revision_no
              JOIN dividend_event_revisions successor ON successor.id=operation.new_revision_id
                   AND successor.event_id=event.id AND
                       successor.revision_no=operation.new_revision_no
              JOIN dividend_revision_decisions decision ON decision.revision_id=successor.id
                   AND decision.owner_id=operation.owner_id AND decision.event_id=event.id
                   AND decision.request_id=operation.request_id
              JOIN audit_events completion_audit ON completion_audit.id=NEW.audit_id
              WHERE operation.id=NEW.operation_id AND event.current_revision_id=successor.id
                AND event.version=successor.revision_no AND
                    event.workflow_status=successor.review_status
                AND operation.created_at<=successor.created_at AND
                    successor.created_at<=NEW.created_at
                AND completion_audit.owner_id=operation.owner_id
                AND completion_audit.entity_type='dividend_revision_rebuild'
                AND completion_audit.entity_id=operation.id
                AND completion_audit.action='DIVIDEND_REVISION_REBUILD_COMPLETED'
                AND completion_audit.request_id=operation.request_id
                AND completion_audit.reason=successor.reason
                AND completion_audit.before_sha256 IS NULL AND
                    {_hash("completion_audit.after_sha256")}
                AND completion_audit.created_at=NEW.created_at
                AND EXISTS(SELECT 1 FROM audit_events successor_audit
                    WHERE successor_audit.entity_type='dividend_event_revision'
                      AND successor_audit.entity_id=successor.id
                      AND successor_audit.action='DIVIDEND_EVENT_REVISED'
                      AND successor_audit.owner_id=operation.owner_id
                      AND successor_audit.request_id=operation.request_id
                      AND successor_audit.reason=successor.reason
                      AND successor_audit.created_at>=successor.created_at
                      AND successor_audit.created_at<=NEW.created_at)
                AND NOT EXISTS(SELECT 1 FROM dividend_event_revisions history
                    WHERE history.event_id=event.id AND (SELECT count(*) FROM audit_events a
                        WHERE a.entity_type='dividend_event_revision' AND a.entity_id=history.id
                          AND a.action='DIVIDEND_EVENT_REVISED')!=1)
                AND (SELECT count(*) FROM audit_events a
                     WHERE a.entity_type='dividend_revision_rebuild' AND a.entity_id=operation.id
                       AND a.action='DIVIDEND_REVISION_REBUILD_COMPLETED')=1
                AND operation.account_count=(SELECT count(*) FROM
                    dividend_revision_rebuild_accounts a
                                             WHERE a.operation_id=operation.id)
                AND operation.account_count=(SELECT count(*) FROM accounts a
                    JOIN portfolios p ON p.id=a.portfolio_id WHERE p.owner_id=operation.owner_id)
                AND operation.portfolio_count=(SELECT count(*) FROM
                    dividend_revision_rebuild_portfolios p
                                               WHERE p.operation_id=operation.id)
                AND NOT EXISTS(
                    SELECT 1 FROM accounts account JOIN portfolios portfolio
                      ON portfolio.id=account.portfolio_id AND portfolio.owner_id=operation.owner_id
                    LEFT JOIN dividend_revision_rebuild_accounts manifest
                      ON manifest.operation_id=operation.id AND manifest.account_id=account.id
                    WHERE manifest.account_id IS NULL OR manifest.portfolio_id!=account.portfolio_id
                      OR manifest.broker_id!=account.broker_id OR
                          manifest.account_version!=account.version
                      OR manifest.account_status!=account.status)
                AND NOT EXISTS(
                    SELECT 1 FROM (
                        SELECT entitlement.account_id FROM dividend_entitlements entitlement
                        JOIN dividend_event_revisions original
                          ON original.id=entitlement.revision_id
                        WHERE original.event_id=operation.event_id
                        UNION SELECT receipt.account_id FROM dividend_receipts receipt
                        JOIN dividend_entitlements entitlement
                          ON entitlement.id=receipt.entitlement_id
                        JOIN dividend_event_revisions original
                          ON original.id=entitlement.revision_id
                        WHERE original.event_id=operation.event_id
                        UNION SELECT account_id FROM dividend_projection_versions
                          WHERE event_id=operation.event_id
                        UNION SELECT account_id FROM dividend_direct_receipts
                          WHERE event_id=operation.event_id
                        UNION SELECT account_id FROM dividend_direct_receipt_origins
                          WHERE event_id=operation.event_id
                        UNION SELECT account_id FROM dividend_receipt_origins
                          WHERE event_id=operation.event_id
                    ) linked LEFT JOIN dividend_revision_rebuild_accounts manifest
                      ON manifest.operation_id=operation.id
                     AND manifest.account_id=linked.account_id
                    WHERE manifest.account_id IS NULL OR manifest.disposition!='REBUILD_REQUIRED')
                AND NOT EXISTS(
                    SELECT 1 FROM dividend_revision_rebuild_accounts manifest
                    JOIN accounts account ON account.id=manifest.account_id
                    JOIN portfolios portfolio ON portfolio.id=account.portfolio_id
                    JOIN brokers broker ON broker.id=account.broker_id
                    WHERE manifest.operation_id=operation.id AND (
                        portfolio.owner_id!=operation.owner_id OR
                            broker.owner_id!=operation.owner_id
                        OR portfolio.version!=manifest.portfolio_version
                        OR broker.version!=manifest.broker_version OR
                            broker.status!=manifest.broker_status
                        OR (manifest.disposition='NO_RECORDED_IMPACT'
                            AND (operation.eligibility_rule!='ORDINARY_PRE_EX_DATE'
                                 OR operation.as_of<successor.ex_date
                                 OR successor.adjustment_basis!='AS_DECLARED'
                                 OR EXISTS(SELECT 1 FROM dividend_entitlements ent
                                     JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                                     WHERE rev.event_id=event.id AND
                                        ent.account_id=manifest.account_id)
                                 OR EXISTS(SELECT 1 FROM dividend_projection_versions projection
                                     WHERE projection.event_id=event.id
                                       AND projection.account_id=manifest.account_id)
                                 OR EXISTS(SELECT 1 FROM dividend_direct_receipts receipt
                                     WHERE receipt.event_id=event.id
                                       AND receipt.account_id=manifest.account_id)
                                 OR EXISTS(SELECT 1 FROM dividend_direct_receipt_origins origin
                                     WHERE origin.event_id=event.id
                                       AND origin.account_id=manifest.account_id)
                                 OR EXISTS(SELECT 1 FROM dividend_receipt_origins origin
                                     WHERE origin.event_id=event.id
                                       AND origin.account_id=manifest.account_id)))
                        OR (manifest.disposition='REBUILD_REQUIRED' AND NOT EXISTS(
                            SELECT 1 FROM dividend_projection_versions projection
                            JOIN review_items review ON review.id=manifest.review_id
                            JOIN audit_events review_audit ON
                                review_audit.id=manifest.review_audit_id
                            JOIN dividend_revision_rebuild_portfolios affected
                              ON affected.operation_id=operation.id
                             AND affected.portfolio_id=manifest.portfolio_id
                            WHERE projection.id=manifest.projection_id
                              AND projection.owner_id=operation.owner_id AND
                                  projection.event_id=event.id
                              AND projection.revision_id=successor.id
                              AND projection.request_id=operation.request_id
                              AND projection.account_id=manifest.account_id
                              AND projection.portfolio_id=manifest.portfolio_id
                              AND projection.broker_id=manifest.broker_id
                              AND projection.account_version=manifest.account_version
                              AND projection.broker_version=manifest.broker_version
                              AND projection.previous_id IS manifest.previous_projection_id
                              AND projection.id=(SELECT latest.id FROM
                                  dividend_projection_versions latest
                                  WHERE latest.event_id=event.id
                                    AND latest.account_id=manifest.account_id
                                  ORDER BY latest.projection_no DESC LIMIT 1)
                              AND projection.portfolio_data_revision=affected.final_data_revision
                              AND projection.as_of=operation.as_of
                              AND projection.eligibility_rule=operation.eligibility_rule
                              AND projection.created_at>=successor.created_at
                              AND projection.created_at<=NEW.created_at
                              AND EXISTS(SELECT 1 FROM audit_events projection_intent
                                  WHERE projection_intent.entity_type='dividend_projection_intent'
                                    AND projection_intent.entity_id=projection.id
                                    AND
            projection_intent.action='DIVIDEND_PROJECTION_INTENT_RECORDED'
                                    AND projection_intent.owner_id=operation.owner_id
                                    AND projection_intent.request_id=operation.request_id
                                    AND projection_intent.reason=successor.reason
                                    AND projection_intent.before_sha256 IS NULL
                                    AND {_hash("projection_intent.after_sha256")}
                                    AND projection_intent.created_at=projection.created_at)
                              AND (SELECT count(*) FROM audit_events projection_intent
                                   WHERE projection_intent.entity_type='dividend_projection_intent'
                                     AND projection_intent.entity_id=projection.id
                                     AND
            projection_intent.action='DIVIDEND_PROJECTION_INTENT_RECORDED')=1
                              AND ((successor.review_status='WITHDRAWN' AND
                                  projection.state='CANCELLED')
                                   OR (successor.review_status!='WITHDRAWN' AND
                                       projection.state!='CANCELLED'))
                              AND (successor.review_status='WITHDRAWN'
                                   OR (successor.review_status NOT IN ('CONFLICT','INCOMPLETE')
                                       AND successor.adjustment_basis='AS_DECLARED'
                                       AND manifest.eligibility_state!='NEEDS_REVIEW'
                                       AND
            projection.retired_trade_scope!='RETIRED_TRADE_HISTORY_UNVERIFIED'
                                       AND
            projection.creation_audit_scope!='CREATION_GAP_REVIEWED_UNVERIFIED')
                                   OR projection.state='NEEDS_REVIEW')
                              AND (projection.state!='EXPECTED'
                                   OR (manifest.recorded_shares_e8>0
                                       AND
                                        projection.eligible_shares_e8=manifest.recorded_shares_e8))
                              AND (projection.state!='NO_POSITION' OR manifest.recorded_shares_e8=0)
                              AND review.event_id=event.id AND review.account_id=manifest.account_id
                              AND review.portfolio_id=manifest.portfolio_id AND
                                  review.kind='DIVIDEND_REVISION'
                              AND review.status='OPEN' AND review.severity='WARNING'
                              AND review.created_at=successor.created_at AND review.resolved_at IS
                                  NULL
                              AND review.reason_code='RECEIPT_RECONCILIATION_REQUIRED'
                              AND
            json_extract(review.detail_json,'$.previous_revision_id')=previous.id
                              AND json_extract(review.detail_json,'$.new_revision_id')=successor.id
                              AND (SELECT count(*) FROM review_items other
                                   WHERE other.event_id=event.id
                                     AND other.account_id=manifest.account_id
                                     AND other.kind='DIVIDEND_REVISION'
                                     AND other.reason_code='RECEIPT_RECONCILIATION_REQUIRED'
                                     AND json_extract(other.detail_json,'$.new_revision_id')=
                                         successor.id)=1
                              AND review_audit.owner_id=operation.owner_id
                              AND review_audit.entity_type='review_item' AND
                                  review_audit.entity_id=review.id
                              AND review_audit.action='DIVIDEND_REVISION_RECONCILIATION_CREATED'
                              AND review_audit.request_id=operation.request_id
                              AND review_audit.reason=successor.reason
                              AND review_audit.created_at>=review.created_at
                              AND review_audit.created_at<=NEW.created_at
                              AND review_audit.before_sha256 IS NULL AND
                                  {_hash("review_audit.after_sha256")}
                              AND (SELECT count(*) FROM audit_events a
                                   WHERE a.entity_type='review_item' AND a.entity_id=review.id
                                     AND a.action='DIVIDEND_REVISION_RECONCILIATION_CREATED')=1
                        ))))
                AND NOT EXISTS(
                    SELECT 1 FROM dividend_revision_rebuild_portfolios affected
                    JOIN portfolios portfolio ON portfolio.id=affected.portfolio_id
                    LEFT JOIN projection_invalidations invalidation ON
                        invalidation.id=affected.invalidation_id
                    WHERE affected.operation_id=operation.id AND (
                        portfolio.owner_id!=operation.owner_id OR
                            portfolio.version!=affected.portfolio_version
                        OR portfolio.data_revision!=affected.final_data_revision
                        OR EXISTS(SELECT 1 FROM dividend_revision_rebuild_accounts account
                            WHERE account.operation_id=operation.id
                              AND account.portfolio_id=affected.portfolio_id
                              AND account.portfolio_data_revision!=affected.initial_data_revision)
                        OR invalidation.id IS NULL OR
                            invalidation.portfolio_id!=affected.portfolio_id
                        OR invalidation.source_id!=successor.id OR
                            invalidation.reason!='DIVIDEND_EVENT_CHANGED'
                        OR NOT {_utc("invalidation.created_at")}
                        OR invalidation.created_at<successor.created_at
                        OR invalidation.created_at>NEW.created_at
                        OR (SELECT count(*) FROM projection_invalidations other
                            WHERE other.portfolio_id=affected.portfolio_id
                              AND other.source_id=successor.id
                              AND other.reason='DIVIDEND_EVENT_CHANGED')!=1
                        OR invalidation.resolved_at IS NOT NULL
                        OR invalidation.from_date!=min(previous.ex_date,successor.ex_date)))
                AND NOT EXISTS(
                    SELECT 1 FROM (
                        SELECT portfolio_id FROM dividend_projection_versions
                          WHERE event_id=operation.event_id
                        UNION SELECT portfolio_id FROM dividend_direct_receipt_origins
                              WHERE event_id=operation.event_id
                        UNION SELECT portfolio_id FROM dividend_receipt_origins
                              WHERE event_id=operation.event_id
                    ) linked LEFT JOIN portfolios p ON p.id=linked.portfolio_id
                    LEFT JOIN dividend_revision_rebuild_portfolios affected
                      ON affected.operation_id=operation.id AND
                          affected.portfolio_id=linked.portfolio_id
                    WHERE p.id IS NULL OR p.owner_id!=operation.owner_id
                       OR affected.portfolio_id IS NULL)
                AND NOT EXISTS(
                    SELECT 1 FROM (
                        SELECT portfolio_id FROM trades WHERE security_id=event.security_id
                        UNION SELECT portfolio_id FROM security_transfers
                              WHERE security_id=event.security_id
                        UNION SELECT portfolio_id FROM positions WHERE security_id=event.security_id
                    ) linked JOIN portfolios p ON p.id=linked.portfolio_id AND
                        p.owner_id=operation.owner_id
                    LEFT JOIN dividend_revision_rebuild_portfolios affected
                      ON affected.operation_id=operation.id AND affected.portfolio_id=p.id
                    WHERE affected.portfolio_id IS NULL)
          )
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_REBUILD_COMPLETION'); END
    """)
    for table in (
        "dividend_revision_rebuild_operations",
        "dividend_revision_rebuild_accounts",
        "dividend_revision_rebuild_portfolios",
        "dividend_revision_rebuild_completions",
    ):
        for operation in ("UPDATE", "DELETE"):
            con.exec_driver_sql(f"""
                CREATE TRIGGER trg_{table}_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_REBUILD_MANIFEST'); END
            """)
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_REVISION_REBUILDS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; no financial grant or publication fence change",
                datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting rebuild history")
