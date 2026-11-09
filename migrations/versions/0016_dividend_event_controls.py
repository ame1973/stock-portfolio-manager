"""Fence owned dividend-event revisions without adopting legacy or source data."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op

revision = "0016_dividend_event_controls"
down_revision = "0015_dividend_candidates"
branch_labels = None
depends_on = None

_FIELDS = (
    "'ex_date','announce_date','record_date','pay_date',"
    "'event_type','currency','dps','adjustment_basis'"
)
_TERMS = (
    "event_type",
    "ex_date",
    "announce_date",
    "record_date",
    "pay_date",
    "dps_e8",
    "currency",
    "adjustment_basis",
)


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
        AND {_day(f"substr({value},1,10)")}
        AND substr({value},11,1)='T' AND substr({value},20,1)='.' AND substr({value},27,1)='Z'
        AND substr({value},21,6) NOT GLOB '*[^0-9]*'
        AND time(substr({value},12,8)) IS substr({value},12,8)
        AND substr({value},12,2) BETWEEN '00' AND '23')"""


def _text(value: str, minimum: int, maximum: int) -> str:
    return f"""(typeof({value})='text' AND length({value}) BETWEEN {minimum} AND {maximum}
        AND instr({value},char(0))=0
        AND {value}=trim({value},char(9)||char(10)||char(11)||char(12)||char(13)||' '))"""


def _same(left: str, right: str) -> str:
    return " AND ".join(f"{left}.{field} IS {right}.{field}" for field in _TERMS)


def upgrade() -> None:
    con = op.get_bind()
    con.exec_driver_sql("""
        ALTER TABLE dividend_events ADD COLUMN provenance_version INTEGER NOT NULL DEFAULT 0
            CHECK(typeof(provenance_version)='integer' AND provenance_version IN (0,1))
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_event_controls (
            event_id TEXT NOT NULL PRIMARY KEY REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TABLE dividend_revision_decisions (
            revision_id TEXT NOT NULL PRIMARY KEY REFERENCES dividend_event_revisions(id)
                ON DELETE RESTRICT CHECK({_uuid("revision_id")}),
            event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT
                CHECK({_uuid("event_id")}),
            owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT
                CHECK({_uuid("owner_id")}),
            decision_kind TEXT NOT NULL
                CHECK(decision_kind IN ('CANDIDATE','MANUAL','VERIFY','WITHDRAW')),
            candidate_revision_id TEXT REFERENCES dividend_candidate_revisions(id)
                ON DELETE RESTRICT
                CHECK(candidate_revision_id IS NULL OR {_uuid("candidate_revision_id")}),
            evidence_count INTEGER NOT NULL CHECK(typeof(evidence_count)='integer'
                AND evidence_count BETWEEN 0 AND 100),
            citation TEXT CHECK(citation IS NULL OR {_text("citation", 1, 300)}),
            source_url TEXT CHECK(source_url IS NULL OR {_text("source_url", 1, 2048)}),
            request_id TEXT NOT NULL CHECK({_uuid("request_id")}),
            created_at TEXT NOT NULL CHECK({_utc("created_at")})
        )
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_event_insert BEFORE INSERT ON dividend_events
        WHEN EXISTS(SELECT 1 FROM dividend_events e WHERE e.provenance_version=1
              AND (e.id=NEW.id OR (e.security_id=NEW.security_id
                    AND e.event_identity_key=NEW.event_identity_key)))
          OR (NEW.provenance_version=1 AND (
                NOT {_uuid("NEW.id")} OR NOT {_uuid("NEW.security_id")}
                OR NEW.event_identity_key IS NOT NEW.id OR NEW.current_revision_id IS NOT NULL
                OR NEW.workflow_status IS NOT 'INCOMPLETE' OR NEW.version IS NOT 1
                OR NOT {_utc("NEW.created_at")}))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_DIVIDEND_EVENT'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_event_control_insert BEFORE INSERT ON dividend_event_controls
        WHEN EXISTS(SELECT 1 FROM dividend_event_controls WHERE event_id=NEW.event_id)
          OR NOT EXISTS(SELECT 1 FROM owners WHERE id=NEW.owner_id AND is_active=1)
          OR NOT EXISTS(SELECT 1 FROM dividend_events e WHERE e.id=NEW.event_id
                AND e.provenance_version=1 AND e.current_revision_id IS NULL AND e.version=1
                AND e.workflow_status='INCOMPLETE' AND e.event_identity_key=e.id
                AND e.created_at=NEW.created_at AND {_uuid("e.id")} AND {_uuid("e.security_id")}
                AND NOT EXISTS(SELECT 1 FROM dividend_event_revisions r WHERE r.event_id=e.id))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_EVENT_CONTROL'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_revision_insert
        BEFORE INSERT ON dividend_event_revisions
        WHEN EXISTS(SELECT 1 FROM dividend_event_revisions r
                    JOIN dividend_events e ON e.id=r.event_id
                    WHERE e.provenance_version=1 AND r.id=NEW.id)
          OR ((SELECT provenance_version FROM dividend_events WHERE id=NEW.event_id)=1 AND (
                NOT {_uuid("NEW.id")} OR NOT {_day("NEW.ex_date")}
                OR (NEW.announce_date IS NOT NULL AND NOT {_day("NEW.announce_date")})
                OR (NEW.record_date IS NOT NULL AND NOT {_day("NEW.record_date")})
                OR (NEW.pay_date IS NOT NULL AND NOT {_day("NEW.pay_date")})
                OR typeof(NEW.revision_no) IS NOT 'integer'
                OR NEW.revision_no NOT BETWEEN 1 AND 2147483647
                OR typeof(NEW.dps_e8) IS NOT 'integer' OR NEW.dps_e8<0
                OR typeof(NEW.manual_locked) IS NOT 'integer' OR NEW.manual_locked NOT IN (0,1)
                OR (NEW.review_status='VERIFIED' AND
                    (NEW.manual_locked!=1 OR NEW.adjustment_basis!='AS_DECLARED'))
                OR NOT {_text("NEW.reason", 10, 1000)} OR NOT {_utc("NEW.created_at")}
                OR EXISTS(SELECT 1 FROM dividend_event_revisions r
                    WHERE r.event_id=NEW.event_id AND r.revision_no=NEW.revision_no)
                OR NOT EXISTS(
                    SELECT 1 FROM dividend_events e
                    JOIN dividend_event_controls c ON c.event_id=e.id
                    JOIN owners o ON o.id=c.owner_id AND o.is_active=1
                    WHERE e.id=NEW.event_id AND NEW.created_at>=e.created_at
                      AND ((e.current_revision_id IS NULL AND e.version=1 AND NEW.revision_no=1
                            AND NOT EXISTS(SELECT 1 FROM dividend_event_revisions r
                                  WHERE r.event_id=e.id))
                        OR (e.current_revision_id IS NOT NULL AND NEW.revision_no=e.version+1
                            AND (SELECT count(*) FROM dividend_event_revisions r
                                  WHERE r.event_id=e.id)=e.version
                            AND EXISTS(SELECT 1 FROM dividend_event_revisions r
                                  WHERE r.id=e.current_revision_id AND r.event_id=e.id
                                    AND r.revision_no=e.version
                                    AND r.created_at<=NEW.created_at))))))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_DIVIDEND_REVISION'); END
    """)
    candidate_terms = _same("r", "c")
    previous_terms = _same("r", "previous")
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_dividend_revision_decision_insert
        BEFORE INSERT ON dividend_revision_decisions
        WHEN EXISTS(SELECT 1 FROM dividend_revision_decisions WHERE revision_id=NEW.revision_id)
          OR NOT EXISTS(
                SELECT 1 FROM dividend_event_controls ctl
                JOIN owners o ON o.id=ctl.owner_id AND o.is_active=1
                JOIN dividend_event_revisions r ON r.id=NEW.revision_id AND r.event_id=ctl.event_id
                WHERE ctl.event_id=NEW.event_id AND ctl.owner_id=NEW.owner_id
                  AND r.created_at=NEW.created_at)
          OR (NEW.candidate_revision_id IS NULL AND NEW.evidence_count!=0)
          OR (NEW.decision_kind='CANDIDATE' AND (
                NEW.candidate_revision_id IS NULL OR EXISTS(
                    SELECT 1 FROM dividend_event_revisions r WHERE r.id=NEW.revision_id
                      AND (r.manual_locked!=0 OR r.review_status IN ('VERIFIED','WITHDRAWN')))))
          OR (NEW.decision_kind IN ('MANUAL','WITHDRAW') AND NEW.candidate_revision_id IS NOT NULL)
          OR (NEW.decision_kind IN ('MANUAL','VERIFY','WITHDRAW') AND NEW.citation IS NULL)
          OR (NEW.candidate_revision_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM dividend_candidate_revisions c
                JOIN dividend_event_revisions r ON r.id=NEW.revision_id
                JOIN dividend_events e ON e.id=NEW.event_id
                WHERE c.id=NEW.candidate_revision_id AND c.owner_id=NEW.owner_id
                  AND c.security_id=e.security_id AND c.state='OPEN' AND {candidate_terms}
                  AND c.revision_no=(SELECT max(v.revision_no) FROM dividend_candidate_revisions v
                                      WHERE v.candidate_id=c.candidate_id)
                  AND c.revision_no=(SELECT count(*) FROM dividend_candidate_revisions v
                                      WHERE v.candidate_id=c.candidate_id)
                  AND NOT EXISTS(SELECT 1 FROM dividend_candidate_revisions v
                        WHERE v.candidate_id=c.candidate_id
                          AND (v.owner_id IS NOT c.owner_id OR v.security_id IS NOT c.security_id))
                  AND NEW.evidence_count=json_array_length(c.matches_json)
                  AND NOT EXISTS(SELECT 1 FROM dividend_revision_decisions d
                        JOIN dividend_candidate_revisions other ON other.id=d.candidate_revision_id
                        WHERE other.candidate_id=c.candidate_id AND d.event_id!=NEW.event_id)))
          OR (NEW.decision_kind='MANUAL' AND EXISTS(
                SELECT 1 FROM dividend_event_revisions r WHERE r.id=NEW.revision_id
                  AND r.review_status='WITHDRAWN'))
          OR (NEW.decision_kind IN ('VERIFY','WITHDRAW') AND NOT EXISTS(
                SELECT 1 FROM dividend_events e
                JOIN dividend_event_revisions previous ON previous.id=e.current_revision_id
                JOIN dividend_event_revisions r ON r.id=NEW.revision_id
                WHERE e.id=NEW.event_id AND {previous_terms}
                  AND ((NEW.decision_kind='VERIFY' AND r.review_status='VERIFIED'
                        AND r.manual_locked=1 AND previous.review_status!='WITHDRAWN')
                    OR (NEW.decision_kind='WITHDRAW' AND r.review_status='WITHDRAWN'
                        AND r.manual_locked=previous.manual_locked))))
        BEGIN SELECT RAISE(ABORT,'INVALID_DIVIDEND_REVISION_DECISION'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_evidence_insert BEFORE INSERT ON dividend_evidence_links
        WHEN EXISTS(SELECT 1 FROM dividend_evidence_links l JOIN dividend_event_revisions r
                    ON r.id=l.revision_id JOIN dividend_events e ON e.id=r.event_id
                    WHERE l.revision_id=NEW.revision_id AND l.observation_id=NEW.observation_id
                      AND e.provenance_version=1)
          OR (EXISTS(SELECT 1 FROM dividend_event_revisions r
                     JOIN dividend_events e ON e.id=r.event_id
                     WHERE r.id=NEW.revision_id AND e.provenance_version=1) AND (
              NOT EXISTS(
                  SELECT 1 FROM dividend_revision_decisions d
                  JOIN dividend_candidate_revisions c ON c.id=d.candidate_revision_id
                  JOIN dividend_events e ON e.id=d.event_id
                  JOIN dividend_observations o ON o.id=NEW.observation_id
                  JOIN provider_runs run ON run.id=o.provider_run_id
                  JOIN json_each(c.matches_json) m
                    ON json_extract(m.value,'$.observation_id')=o.id
                  WHERE d.revision_id=NEW.revision_id AND o.provenance_version=1
                    AND o.security_id=e.security_id
                    AND o.normalized_sha256=json_extract(m.value,'$.source_sha256')
                    AND run.provenance_version=1 AND run.status='OK_WITH_DATA'
                    AND run.operation='DIVIDENDS' AND run.security_id=o.security_id
                    AND run.provider_id=o.provider_id AND run.origin_group=o.origin_group
                    AND EXISTS(SELECT 1 FROM provider_observation_sightings s
                          WHERE s.run_id=run.id AND s.dividend_observation_id=o.id))
              OR CASE WHEN typeof(NEW.field_names_json) IS NOT 'text'
                        OR length(CAST(NEW.field_names_json AS BLOB))>4096
                        OR json_valid(NEW.field_names_json) IS NOT 1 THEN 1
                  WHEN json_type(NEW.field_names_json) IS NOT 'array' THEN 1
                  WHEN json_array_length(NEW.field_names_json)>8 THEN 1
                  ELSE (SELECT count(DISTINCT value) FROM json_each(NEW.field_names_json))
                            !=json_array_length(NEW.field_names_json)
                    OR EXISTS(SELECT 1 FROM json_each(NEW.field_names_json)
                              WHERE type IS NOT 'text' OR value NOT IN ({_FIELDS}))
                  END))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_DIVIDEND_EVIDENCE'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_event_update BEFORE UPDATE ON dividend_events
        WHEN (OLD.provenance_version=1 OR NEW.provenance_version=1) AND (
              OLD.provenance_version IS NOT NEW.provenance_version
              OR OLD.id IS NOT NEW.id OR OLD.security_id IS NOT NEW.security_id
              OR OLD.event_identity_key IS NOT NEW.event_identity_key
              OR OLD.created_at IS NOT NEW.created_at OR NEW.current_revision_id IS NULL
              OR NEW.current_revision_id IS OLD.current_revision_id
              OR NEW.version IS NOT CASE WHEN OLD.current_revision_id IS NULL THEN 1
                                         ELSE OLD.version+1 END
              OR NOT EXISTS(
                  SELECT 1 FROM dividend_revision_decisions d
                  JOIN dividend_event_controls ctl ON ctl.event_id=d.event_id
                    AND ctl.owner_id=d.owner_id
                  JOIN owners o ON o.id=ctl.owner_id AND o.is_active=1
                  JOIN dividend_event_revisions r ON r.id=d.revision_id AND r.event_id=d.event_id
                  WHERE d.event_id=OLD.id AND d.revision_id=NEW.current_revision_id
                    AND r.revision_no=NEW.version AND r.review_status=NEW.workflow_status
                    AND (r.revision_no!=1 OR ctl.request_id=d.request_id)
                    AND d.evidence_count=(SELECT count(*) FROM dividend_evidence_links l
                                          WHERE l.revision_id=r.id)
                    AND (SELECT count(*) FROM audit_events a
                         WHERE a.entity_type='dividend_event_revision' AND a.entity_id=r.id
                           AND a.action='DIVIDEND_EVENT_REVISED')=1
                    AND EXISTS(SELECT 1 FROM audit_events a
                         WHERE a.entity_type='dividend_event_revision' AND a.entity_id=r.id
                           AND a.action='DIVIDEND_EVENT_REVISED' AND a.owner_id=ctl.owner_id
                           AND a.request_id=d.request_id AND a.reason=r.reason
                           AND {_hash("a.after_sha256")}
                           AND ((r.revision_no=1 AND a.before_sha256 IS NULL)
                             OR (r.revision_no>1 AND {_hash("a.before_sha256")})))
                    AND NOT (d.decision_kind='CANDIDATE' AND EXISTS(
                          SELECT 1 FROM dividend_event_revisions previous
                          WHERE previous.id=OLD.current_revision_id
                            AND (previous.manual_locked=1 OR previous.review_status='WITHDRAWN'))))
              OR EXISTS(
                  SELECT 1 FROM dividend_event_revisions previous
                  JOIN dividend_event_revisions next ON next.id=NEW.current_revision_id
                  JOIN dividend_event_revisions old_receipt ON old_receipt.event_id=OLD.id
                  JOIN dividend_entitlements ent ON ent.revision_id=old_receipt.id
                  JOIN dividend_receipts receipt ON receipt.entitlement_id=ent.id
                  JOIN accounts account ON account.id=receipt.account_id
                  WHERE previous.id=OLD.current_revision_id AND receipt.status='ACTIVE'
                    AND (NOT ({_same("previous", "next")})
                         OR (next.review_status='WITHDRAWN'
                             AND previous.review_status!='WITHDRAWN'))
                    AND NOT EXISTS(SELECT 1 FROM review_items item
                        WHERE item.event_id=OLD.id AND item.account_id=receipt.account_id
                          AND item.portfolio_id=account.portfolio_id
                          AND item.kind='DIVIDEND_REVISION' AND item.status='OPEN'
                          AND item.reason_code='RECEIPT_RECONCILIATION_REQUIRED'
                          AND json_extract(item.detail_json,'$.previous_revision_id')
                              =OLD.current_revision_id
                          AND json_extract(item.detail_json,'$.new_revision_id')
                              =NEW.current_revision_id)))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_DIVIDEND_TRANSITION'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_managed_dividend_event_delete BEFORE DELETE ON dividend_events
        WHEN OLD.provenance_version=1
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_MANAGED_DIVIDEND_EVENT'); END
    """)
    managed_revision = """EXISTS(SELECT 1 FROM dividend_event_revisions rev
        JOIN dividend_events event ON event.id=rev.event_id
        WHERE rev.id={revision_id} AND event.provenance_version=1)"""
    old_revision = managed_revision.format(revision_id="OLD.revision_id")
    new_revision = managed_revision.format(revision_id="NEW.revision_id")
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_entitlement_insert
        BEFORE INSERT ON dividend_entitlements
        WHEN EXISTS(SELECT 1 FROM dividend_entitlements previous
                JOIN dividend_event_revisions rev ON rev.id=previous.revision_id
                JOIN dividend_events event ON event.id=rev.event_id
                WHERE event.provenance_version=1 AND (previous.id=NEW.id
                  OR (previous.revision_id=NEW.revision_id AND previous.account_id=NEW.account_id)))
          OR ({new_revision} AND (NOT {_uuid("NEW.id")}
              OR EXISTS(SELECT 1 FROM dividend_entitlements WHERE id=NEW.id)
              OR NOT EXISTS(SELECT 1 FROM dividend_event_revisions r
                JOIN dividend_event_controls ctl ON ctl.event_id=r.event_id
                JOIN accounts a ON a.id=NEW.account_id
                JOIN portfolios p ON p.id=a.portfolio_id
                WHERE r.id=NEW.revision_id AND p.owner_id=ctl.owner_id
                  AND r.currency=NEW.currency
                  AND typeof(NEW.eligible_shares_e8)='integer' AND NEW.eligible_shares_e8>0
                  AND typeof(NEW.gross_micro)='integer' AND NEW.gross_micro>=0
                  AND typeof(NEW.tax_micro)='integer' AND NEW.tax_micro>=0
                  AND typeof(NEW.fee_micro)='integer' AND NEW.fee_micro>=0
                  AND typeof(NEW.expected_net_micro)='integer'
                  AND (NEW.fx_rate_hkd_e12 IS NULL OR
                       (typeof(NEW.fx_rate_hkd_e12)='integer' AND NEW.fx_rate_hkd_e12>0))
                  AND (NEW.expected_hkd_micro IS NULL OR typeof(NEW.expected_hkd_micro)='integer')
                  AND (NEW.tax_rate_snapshot_ppm IS NULL OR
                       (typeof(NEW.tax_rate_snapshot_ppm)='integer'
                        AND NEW.tax_rate_snapshot_ppm BETWEEN 0 AND 1000000))
                  AND typeof(NEW.projection_version)='integer' AND NEW.projection_version>=0
                  AND {_utc("NEW.created_at")})))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_DIVIDEND_ENTITLEMENT'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_entitlement_update
        BEFORE UPDATE ON dividend_entitlements
        WHEN {old_revision} OR {new_revision}
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_MANAGED_DIVIDEND_ENTITLEMENT'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_entitlement_delete
        BEFORE DELETE ON dividend_entitlements WHEN {old_revision}
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_MANAGED_DIVIDEND_ENTITLEMENT'); END
    """)
    receipt_scope = """EXISTS(
        SELECT 1 FROM dividend_entitlements ent
        JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
        JOIN dividend_events event ON event.id=rev.event_id
        WHERE ent.id={entitlement_id} AND event.provenance_version=1)"""
    old_scope = receipt_scope.format(entitlement_id="OLD.entitlement_id")
    new_scope = receipt_scope.format(entitlement_id="NEW.entitlement_id")
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_receipt_insert BEFORE INSERT ON dividend_receipts
        WHEN EXISTS(SELECT 1 FROM dividend_receipts old
                  JOIN dividend_entitlements ent ON ent.id=old.entitlement_id
                  JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                  JOIN dividend_events event ON event.id=rev.event_id
                  WHERE old.id=NEW.id AND event.provenance_version=1)
          OR ({new_scope} AND (
                EXISTS(SELECT 1 FROM dividend_receipts WHERE id=NEW.id)
                OR NOT {_uuid("NEW.id")} OR NOT {_day("NEW.received_date")}
                OR NOT {_utc("NEW.created_at")} OR NEW.status IS NOT 'ACTIVE'
                OR NEW.version IS NOT 1 OR NEW.voided_at IS NOT NULL
                OR typeof(NEW.received_micro) IS NOT 'integer'
                OR NEW.received_micro=-9223372036854775808
                OR typeof(NEW.tax_micro) IS NOT 'integer' OR NEW.tax_micro<0
                OR typeof(NEW.fee_micro) IS NOT 'integer' OR NEW.fee_micro<0
                OR (NEW.fx_rate_hkd_e12 IS NOT NULL AND
                    (typeof(NEW.fx_rate_hkd_e12) IS NOT 'integer' OR NEW.fx_rate_hkd_e12<=0))
                OR (NEW.received_hkd_micro IS NOT NULL AND
                    (typeof(NEW.received_hkd_micro) IS NOT 'integer'
                     OR NEW.received_hkd_micro=-9223372036854775808))))
        BEGIN SELECT RAISE(ABORT,'INVALID_MANAGED_DIVIDEND_RECEIPT'); END
    """)
    receipt_facts = (
        "id",
        "entitlement_id",
        "account_id",
        "received_date",
        "received_micro",
        "currency",
        "tax_micro",
        "fee_micro",
        "fx_rate_hkd_e12",
        "received_hkd_micro",
        "note",
        "source",
        "created_at",
    )
    changed_receipt = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in receipt_facts)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_receipt_update BEFORE UPDATE ON dividend_receipts
        WHEN ({old_scope} OR {new_scope}) AND (
              {changed_receipt} OR OLD.status IS NOT 'ACTIVE' OR NEW.status IS NOT 'VOID'
              OR NEW.version IS NOT OLD.version+1 OR NOT {_utc("NEW.voided_at")}
              OR NEW.voided_at<OLD.created_at
              OR NOT EXISTS(
                  SELECT 1 FROM dividend_entitlements ent
                  JOIN dividend_event_revisions rev ON rev.id=ent.revision_id
                  JOIN dividend_event_controls ctl ON ctl.event_id=rev.event_id
                  JOIN cash_ledger_entries original ON original.source_type='DIVIDEND_RECEIPT'
                    AND original.source_id=OLD.id AND original.entry_role='PRIMARY'
                  JOIN cash_ledger_entries reversal ON reversal.reverses_entry_id=original.id
                  WHERE ent.id=OLD.entitlement_id AND ent.account_id=OLD.account_id
                    AND original.account_id=OLD.account_id AND original.currency=OLD.currency
                    AND original.effective_date=OLD.received_date
                    AND original.kind='DIVIDEND_RECEIPT' AND original.external_flow=0
                    AND original.amount_micro=OLD.received_micro
                    AND original.fx_rate_hkd_e12 IS OLD.fx_rate_hkd_e12
                    AND original.booked_hkd_micro IS OLD.received_hkd_micro
                    AND reversal.entry_role='REVERSAL' AND reversal.kind='REVERSAL'
                    AND reversal.source_type=original.source_type
                    AND reversal.source_id=original.source_id
                    AND reversal.account_id=original.account_id
                    AND reversal.effective_date=original.effective_date
                    AND reversal.currency=original.currency
                    AND reversal.external_flow=original.external_flow
                    AND reversal.amount_micro=-original.amount_micro
                    AND reversal.fx_rate_hkd_e12 IS original.fx_rate_hkd_e12
                    AND reversal.booked_hkd_micro IS -original.booked_hkd_micro
                    AND (SELECT count(*) FROM audit_events a
                         WHERE a.entity_type='dividend_receipt' AND a.entity_id=OLD.id
                           AND a.action='DIVIDEND_RECEIPT_VOIDED')=1
                    AND EXISTS(SELECT 1 FROM audit_events a
                        WHERE a.entity_type='dividend_receipt' AND a.entity_id=OLD.id
                          AND a.action='DIVIDEND_RECEIPT_VOIDED' AND a.owner_id=ctl.owner_id
                          AND a.reason=reversal.note AND {_text("a.reason", 10, 1000)}
                          AND {_uuid("a.request_id")} AND {_hash("a.before_sha256")}
                          AND {_hash("a.after_sha256")})))
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_MANAGED_DIVIDEND_RECEIPT'); END
    """)
    con.exec_driver_sql(f"""
        CREATE TRIGGER trg_managed_dividend_receipt_delete BEFORE DELETE ON dividend_receipts
        WHEN {old_scope}
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_MANAGED_DIVIDEND_RECEIPT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_cash_entry_no_replace BEFORE INSERT ON cash_ledger_entries
        WHEN EXISTS(SELECT 1 FROM cash_ledger_entries old WHERE old.id=NEW.id
            OR (old.account_id=NEW.account_id AND old.source_type=NEW.source_type
                AND old.source_id=NEW.source_id AND old.entry_role=NEW.entry_role)
            OR (NEW.reverses_entry_id IS NOT NULL
                AND old.reverses_entry_id=NEW.reverses_entry_id)
            OR (NEW.entry_role='PRIMARY'
                AND NEW.source_type IN ('CASH_CORRECTION','OPENING_BALANCE_CORRECTION')
                AND old.entry_role='PRIMARY' AND old.source_type=NEW.source_type
                AND old.source_id=NEW.source_id))
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_CASH_ENTRY'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_audit_event_no_replace BEFORE INSERT ON audit_events
        WHEN EXISTS(SELECT 1 FROM audit_events old WHERE old.id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_AUDIT_EVENT'); END
    """)
    con.exec_driver_sql("""
        CREATE TRIGGER trg_dividend_revision_no_replace BEFORE INSERT ON dividend_event_revisions
        WHEN EXISTS(SELECT 1 FROM dividend_event_revisions old WHERE old.id=NEW.id
            OR (old.event_id=NEW.event_id AND old.revision_no=NEW.revision_no))
        BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_EVENT_REVISION'); END
    """)
    for table in ("dividend_observations", "split_observations"):
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_{table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS(SELECT 1 FROM {table} old WHERE old.id=NEW.id
                OR (old.provider_id=NEW.provider_id AND old.security_id=NEW.security_id
                    AND old.normalized_sha256=NEW.normalized_sha256))
            BEGIN SELECT RAISE(ABORT,'INVALID_PROVIDER_OBSERVATION'); END
        """)
    for table in ("dividend_event_controls", "dividend_revision_decisions"):
        for operation in ("UPDATE", "DELETE"):
            con.exec_driver_sql(f"""
                CREATE TRIGGER trg_{table}_no_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_PROVENANCE'); END
            """)
    for operation in ("UPDATE", "DELETE"):
        target = "OR r.id=NEW.revision_id" if operation == "UPDATE" else ""
        con.exec_driver_sql(f"""
            CREATE TRIGGER trg_managed_dividend_evidence_no_{operation.lower()}
            BEFORE {operation} ON dividend_evidence_links
            WHEN EXISTS(SELECT 1 FROM dividend_event_revisions r
                        JOIN dividend_events e ON e.id=r.event_id
                        WHERE (r.id=OLD.revision_id {target}) AND e.provenance_version=1)
            BEGIN SELECT RAISE(ABORT,'IMMUTABLE_DIVIDEND_EVIDENCE'); END
        """)
    con.exec_driver_sql(
        "CREATE INDEX idx_dividend_event_control_owner "
        "ON dividend_event_controls(owner_id,event_id)"
    )
    con.exec_driver_sql(
        "CREATE INDEX idx_dividend_decision_candidate "
        "ON dividend_revision_decisions(candidate_revision_id,event_id)"
    )
    con.exec_driver_sql(
        "CREATE INDEX idx_dividend_receipt_entitlement "
        "ON dividend_receipts(entitlement_id,status,account_id)"
    )
    for owner in con.exec_driver_sql("SELECT id FROM owners").scalars():
        con.exec_driver_sql(
            "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
            "after_sha256,reason,created_at) "
            "VALUES (?,?,'schema',?,'MIGRATE_DIVIDEND_EVENT_CONTROLS',?,?,?,?)",
            (
                str(uuid4()),
                owner,
                revision,
                sha256(down_revision.encode()).hexdigest(),
                sha256(revision.encode()).hexdigest(),
                "Local operator upgrade; retain legacy dividend facts without automatic adoption",
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def downgrade() -> None:
    raise RuntimeError("Restore a validated pre-upgrade backup instead of deleting event history")
