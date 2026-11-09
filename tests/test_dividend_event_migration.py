"""Managed dividend events retain explicit ownership, revisions and source lineage."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from spm_api.audit import audit, canonical
from spm_api.cli import migrate
from spm_api.config import Settings
from spm_api.db import make_engine, migration_config, schema_head, unit_of_work
from spm_api.ledger_service import post_cash, reverse_cash
from spm_api.providers.base import DividendObservation, SplitObservation
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_action_opening_migration import schema
from tests.test_dividend_basis_migration import SECURITY, cloned_source, seeded
from tests.test_dividend_candidate_migration import candidate, match, with_matches
from tests.test_schedule_migration import upgrade_to

HEAD = "0016_dividend_event_controls"
OWNER = UUID("c0000000-0000-4000-8000-000000000001")
OTHER = UUID("c0000000-0000-4000-8000-000000000002")
AT = "2026-11-09T12:00:00.000000Z"
REASON = "Explicit synthetic event decision with retained source lineage."


@pytest.fixture
def source(engine: Engine) -> dict[str, Any]:
    with unit_of_work(engine, write=True) as con:
        observation = seeded(con)
        for owner in (OWNER, OTHER):
            con.exec_driver_sql(
                "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
                "VALUES (?,'synthetic-internal-actor-not-http-auth',?,?)",
                (str(owner), AT, AT),
            )
        decision = candidate(observation, owner_id=str(OWNER), created_at=AT)
        insert(con, "dividend_candidate_revisions", decision)
    return {"observation": observation, "candidate": decision}


@pytest.fixture
def old_engine(settings: Settings) -> Iterator[Engine]:
    engine = make_engine(settings.database_path)
    try:
        upgrade_to(engine, "0015_dividend_candidates")
        with engine.begin() as con:
            seeded(con)
            insert(
                con,
                "dividend_events",
                {
                    "id": "legacy-event",
                    "security_id": str(SECURITY),
                    "event_identity_key": "legacy-external-id",
                    "workflow_status": "VERIFIED",
                    "created_at": AT,
                },
            )
            insert(
                con,
                "dividend_event_revisions",
                {
                    "id": "legacy-revision",
                    "event_id": "legacy-event",
                    "revision_no": 1,
                    "event_type": "REGULAR",
                    "ex_date": "2026-01-08",
                    "dps_e8": 100000000,
                    "currency": "USD",
                    "adjustment_basis": "AS_DECLARED",
                    "review_status": "VERIFIED",
                    "manual_locked": 1,
                    "created_at": AT,
                },
            )
            con.exec_driver_sql(
                "UPDATE dividend_events SET current_revision_id='legacy-revision' "
                "WHERE id='legacy-event'"
            )
            insert(
                con,
                "dividend_entitlements",
                {
                    "id": "legacy-entitlement",
                    "revision_id": "legacy-revision",
                    "account_id": "A",
                    "eligible_shares_e8": 1000000000,
                    "gross_micro": 10000000,
                    "expected_net_micro": 10000000,
                    "currency": "USD",
                    "quality": "ACCRUED",
                    "projection_version": 0,
                    "created_at": AT,
                },
            )
            insert(
                con,
                "dividend_receipts",
                {
                    "id": "legacy-receipt",
                    "entitlement_id": "legacy-entitlement",
                    "account_id": "A",
                    "received_date": "2026-01-09",
                    "received_micro": 6000000,
                    "currency": "USD",
                    "fx_rate_hkd_e12": 7800000000000,
                    "received_hkd_micro": 46123456,
                    "created_at": AT,
                },
            )
        yield engine
    finally:
        engine.dispose()


def event(source: dict[str, Any]) -> dict[str, Any]:
    identifier = str(uuid4())
    return {
        "id": identifier,
        "security_id": str(SECURITY),
        "event_identity_key": identifier,
        "current_revision_id": None,
        "workflow_status": "INCOMPLETE",
        "version": 1,
        "created_at": AT,
        "provenance_version": 1,
    }


def control(parent: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "event_id": parent["id"],
        "owner_id": str(OWNER),
        "request_id": str(uuid4()),
        "created_at": AT,
        **changes,
    }


def revision(parent: dict[str, Any], source: dict[str, Any], **changes: Any) -> dict[str, Any]:
    proposal = source["candidate"]
    return {
        "id": str(uuid4()),
        "event_id": parent["id"],
        "revision_no": 1,
        "event_type": proposal["event_type"],
        "ex_date": proposal["ex_date"],
        "announce_date": proposal["announce_date"],
        "record_date": proposal["record_date"],
        "pay_date": proposal["pay_date"],
        "dps_e8": proposal["dps_e8"],
        "currency": proposal["currency"],
        "adjustment_basis": proposal["adjustment_basis"],
        "review_status": "INCOMPLETE",
        "manual_locked": 0,
        "reason": REASON,
        "created_at": AT,
        **changes,
    }


def decision(
    parent: dict[str, Any], rev: dict[str, Any], source: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    return {
        "revision_id": rev["id"],
        "event_id": parent["id"],
        "owner_id": str(OWNER),
        "decision_kind": "CANDIDATE",
        "candidate_revision_id": source["candidate"]["id"],
        "evidence_count": 1,
        "citation": None,
        "source_url": None,
        "request_id": str(uuid4()),
        "created_at": AT,
        **changes,
    }


def link(rev: dict[str, Any], source: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "revision_id": rev["id"],
        "observation_id": source["observation"]["observation_id"],
        "field_names_json": canonical(["currency", "ex_date"]),
        **changes,
    }


def audited(con: Any, rev: dict[str, Any], source_decision: dict[str, Any]) -> None:
    audit(
        con,
        source_decision["owner_id"],
        "dividend_event_revision",
        rev["id"],
        "DIVIDEND_EVENT_REVISED",
        request_id=source_decision["request_id"],
        before={"revision": rev["revision_no"] - 1} if rev["revision_no"] > 1 else None,
        after={"revision": rev["revision_no"]},
        reason=rev["reason"],
    )


def publish(con: Any, parent: dict[str, Any], rev: dict[str, Any], version: int = 1) -> None:
    con.exec_driver_sql(
        "UPDATE dividend_events SET current_revision_id=?,workflow_status=?,version=? WHERE id=?",
        (rev["id"], rev["review_status"], version, parent["id"]),
    )


def staged(
    con: Any,
    source: dict[str, Any],
    *,
    manual: bool = False,
    status: str = "INCOMPLETE",
    locked: int = 0,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    parent = event(source)
    rev = revision(
        parent,
        source,
        review_status=status,
        manual_locked=locked,
        adjustment_basis="AS_DECLARED" if manual else source["candidate"]["adjustment_basis"],
    )
    provenance = decision(
        parent,
        rev,
        source,
        **(
            {
                "decision_kind": "MANUAL",
                "candidate_revision_id": None,
                "evidence_count": 0,
                "citation": "Synthetic explicit owner reference",
            }
            if manual
            else {}
        ),
    )
    insert(con, "dividend_events", parent)
    insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
    insert(con, "dividend_event_revisions", rev)
    insert(con, "dividend_revision_decisions", provenance)
    if not manual:
        insert(con, "dividend_evidence_links", link(rev, source))
    audited(con, rev, provenance)
    return parent, rev, provenance


def manual_revision(
    con: Any,
    parent: dict[str, Any],
    source: dict[str, Any],
    *,
    number: int,
    kind: str = "MANUAL",
    status: str = "PROVISIONAL",
    locked: int = 0,
    **terms: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rev = revision(
        parent,
        source,
        revision_no=number,
        adjustment_basis="AS_DECLARED",
        review_status=status,
        manual_locked=locked,
        **terms,
    )
    provenance = decision(
        parent,
        rev,
        source,
        decision_kind=kind,
        candidate_revision_id=None,
        evidence_count=0,
        citation="Synthetic explicit owner reference",
    )
    insert(con, "dividend_event_revisions", rev)
    insert(con, "dividend_revision_decisions", provenance)
    audited(con, rev, provenance)
    return rev, provenance


def owned_account(con: Any) -> str:
    portfolio, broker, account = (str(uuid4()) for _ in range(3))
    insert(
        con,
        "portfolios",
        {
            "id": portfolio,
            "owner_id": str(OWNER),
            "name": f"Synthetic portfolio {portfolio}",
            "created_at": AT,
        },
    )
    insert(
        con, "brokers", {"id": broker, "owner_id": str(OWNER), "name": f"Synthetic broker {broker}"}
    )
    insert(
        con,
        "accounts",
        {
            "id": account,
            "portfolio_id": portfolio,
            "broker_id": broker,
            "name": "Synthetic owned account",
            "default_currency": "USD",
            "created_at": AT,
        },
    )
    return account


def entitlement(rev: dict[str, Any], account: str) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "revision_id": rev["id"],
        "account_id": account,
        "eligible_shares_e8": 1000000000,
        "gross_micro": 10000000,
        "expected_net_micro": 10000000,
        "currency": rev["currency"],
        "quality": "ACCRUED",
        "projection_version": 0,
        "created_at": AT,
    }


def add_posted_receipt(con: Any, claim: dict[str, Any], *, fx: int | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": str(uuid4()),
        "entitlement_id": claim["id"],
        "account_id": claim["account_id"],
        "received_date": "2026-01-09",
        "received_micro": 6000000,
        "currency": "USD",
        "fx_rate_hkd_e12": fx,
        "received_hkd_micro": 46800000 if fx is not None else None,
        "created_at": AT,
    }
    insert(con, "dividend_receipts", values)
    post_cash(
        con,
        account=claim["account_id"],
        day=values["received_date"],
        currency="USD",
        amount=6000000,
        fx=fx,
        booked=values["received_hkd_micro"],
        kind="DIVIDEND_RECEIPT",
        source_type="DIVIDEND_RECEIPT",
        source_id=values["id"],
    )
    return values


def posted_receipt(con: Any, source: dict[str, Any], *, fx: int | None = None) -> dict[str, Any]:
    parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
    publish(con, parent, rev)
    claim = entitlement(rev, owned_account(con))
    insert(con, "dividend_entitlements", claim)
    return {
        "event": parent,
        "revision": rev,
        "entitlement": claim,
        "receipt": add_posted_receipt(con, claim, fx=fx),
    }


def reverse_receipt(con: Any, values: dict[str, Any], *, owner: UUID = OWNER) -> None:
    row = (
        con.exec_driver_sql(
            "SELECT * FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' "
            "AND source_id=? AND entry_role='PRIMARY'",
            (values["id"],),
        )
        .mappings()
        .one()
    )
    reverse_cash(con, row, reason=REASON)
    audit(
        con,
        str(owner),
        "dividend_receipt",
        values["id"],
        "DIVIDEND_RECEIPT_VOIDED",
        request_id=str(uuid4()),
        before={"status": "ACTIVE"},
        after={"status": "VOID"},
        reason=REASON,
    )


def test_schema_adds_empty_managed_provenance_without_adopting_events(engine: Engine) -> None:
    with unit_of_work(engine) as con:
        assert schema_head() == HEAD
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_event_controls").scalar_one() == 0
        assert (
            con.exec_driver_sql("SELECT count(*) FROM dividend_revision_decisions").scalar_one()
            == 0
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            ).scalar_one()
            == 55
        )
        plan = con.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT account_id FROM dividend_receipts "
            "WHERE entitlement_id=? AND status='ACTIVE'",
            (str(uuid4()),),
        ).all()
        assert any("idx_dividend_receipt_entitlement" in row[3] for row in plan)


def test_explicit_candidate_revision_and_evidence_publish_together(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(rev, source))
        audited(con, rev, provenance)
        publish(con, parent, rev)
    after = database_snapshot(engine)
    changed = {
        "dividend_events",
        "dividend_event_controls",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "dividend_evidence_links",
        "audit_events",
    }
    assert {key: value for key, value in before.items() if key not in changed} == {
        key: value for key, value in after.items() if key not in changed
    }
    with unit_of_work(engine) as con:
        row = con.exec_driver_sql(
            "SELECT current_revision_id,workflow_status,version FROM dividend_events WHERE id=?",
            (parent["id"],),
        ).one()
        assert tuple(row) == (rev["id"], "INCOMPLETE", 1)
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_entitlements").scalar_one() == 0


def test_missing_source_link_cannot_publish_a_partial_candidate_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        audited(con, rev, provenance)
        publish(con, parent, rev)
    assert database_snapshot(engine) == before


def test_managed_revision_cannot_publish_without_correlated_audit(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(rev, source))
        publish(con, parent, rev)
    assert database_snapshot(engine) == before


def test_legacy_evidence_cannot_be_moved_onto_a_managed_revision(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    legacy = {
        **event(source),
        "id": "legacy",
        "event_identity_key": "legacy",
        "provenance_version": 0,
    }
    legacy_revision = {**revision(legacy, source), "id": "legacy-revision"}
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", decision(parent, rev, source))
        insert(con, "dividend_events", legacy)
        insert(con, "dividend_event_revisions", legacy_revision)
        insert(con, "dividend_evidence_links", link(legacy_revision, source))
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE dividend_evidence_links SET revision_id=? WHERE revision_id='legacy-revision'",
            (rev["id"],),
        )
    assert database_snapshot(engine) == before


def test_managed_event_cannot_gain_another_owners_entitlement(
    engine: Engine, source: dict[str, Any]
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(rev, source))
        audited(con, rev, provenance)
        publish(con, parent, rev)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(
            con,
            "dividend_entitlements",
            {
                "id": str(uuid4()),
                "revision_id": rev["id"],
                "account_id": "A",
                "eligible_shares_e8": 100000000,
                "gross_micro": 1000000,
                "expected_net_micro": 1000000,
                "currency": "USD",
                "quality": "ACCRUED",
                "projection_version": 0,
                "created_at": AT,
            },
        )
    assert database_snapshot(engine) == before


def test_upgrade_preserves_legacy_events_receipts_fx_and_adds_only_empty_controls(
    old_engine: Engine, settings: Settings
) -> None:
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    migrate(settings)
    after = database_snapshot(old_engine)
    added = {"dividend_event_controls", "dividend_revision_decisions"}
    assert set(after) == set(before) | added and all(after[name] == [] for name in added)
    assert {
        name: rows
        for name, rows in before.items()
        if name not in {"alembic_version", "audit_events", "dividend_events"}
    } == {
        name: rows
        for name, rows in after.items()
        if name not in {"alembic_version", "audit_events", "dividend_events", *added}
    }
    assert [row[:-1] for row in after["dividend_events"]] == before["dividend_events"]
    assert all(row[-1] == 0 for row in after["dividend_events"])
    assert after["audit_events"][: len(before["audit_events"])] == before["audit_events"]
    assert len(after["audit_events"]) == len(before["audit_events"]) + 2
    assert all(
        row in schema(old_engine)
        for row in definitions
        if row[1] not in {"alembic_version", "dividend_events"}
    )
    with unit_of_work(old_engine) as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE action='MIGRATE_DIVIDEND_EVENT_CONTROLS'"
            ).scalar_one()
            == 2
        )
        assert con.exec_driver_sql(
            "SELECT current_revision_id,workflow_status,provenance_version FROM dividend_events "
            "WHERE id='legacy-event'"
        ).one() == ("legacy-revision", "VERIFIED", 0)
        assert con.exec_driver_sql(
            "SELECT received_micro,fx_rate_hkd_e12,received_hkd_micro FROM dividend_receipts "
            "WHERE id='legacy-receipt'"
        ).one() == (6000000, 7800000000000, 46123456)
        assert con.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    migrate(settings)
    assert database_snapshot(old_engine) == after


@pytest.mark.parametrize("failure", ["ddl", "second-owner-audit"])
def test_late_upgrade_failure_restores_old_schema_facts_and_audits(
    old_engine: Engine, settings: Settings, failure: str
) -> None:
    with old_engine.begin() as con:
        if failure == "ddl":
            con.exec_driver_sql(
                "CREATE INDEX idx_dividend_decision_candidate ON settings(owner_id)"
            )
        else:
            con.exec_driver_sql("""
                CREATE TRIGGER test_fail_event_schema_audit BEFORE INSERT ON audit_events
                WHEN NEW.action='MIGRATE_DIVIDEND_EVENT_CONTROLS'
                  AND EXISTS(SELECT 1 FROM audit_events WHERE action=NEW.action)
                BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECOND_OWNER_FAILURE'); END
            """)
    before, definitions = database_snapshot(old_engine), schema(old_engine)
    with pytest.raises((OperationalError, IntegrityError)):
        migrate(settings)
    assert database_snapshot(old_engine) == before and schema(old_engine) == definitions
    with old_engine.connect() as con:
        assert (
            con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0015_dividend_candidates"
        )


def test_downgrade_refuses_to_remove_managed_event_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
    before, definitions = database_snapshot(engine), schema(engine)
    with engine.connect() as con:
        config = migration_config()
        config.attributes["connection"] = con
        with pytest.raises(RuntimeError, match="validated"):
            command.downgrade(config, "0015_dividend_candidates")
    assert database_snapshot(engine) == before and schema(engine) == definitions


def test_two_competing_revision_writers_publish_only_one_version(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, first, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, first)

    def revise(dps: int) -> bool:
        try:
            with unit_of_work(engine, write=True) as con:
                next_revision, _ = manual_revision(con, parent, source, number=2, dps_e8=dps)
                publish(con, parent, next_revision, 2)
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(revise, (70000000, 80000000))) == [False, True]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_event_revisions WHERE event_id=?", (parent["id"],)
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_revision_decisions WHERE event_id=?", (parent["id"],)
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM audit_events WHERE entity_type='dividend_event_revision' "
                "AND entity_id IN (SELECT id FROM dividend_event_revisions WHERE event_id=?)",
                (parent["id"],),
            ).scalar_one()
            == 2
        )
        assert (
            con.exec_driver_sql(
                "SELECT version FROM dividend_events WHERE id=?", (parent["id"],)
            ).scalar_one()
            == 2
        )


def test_legacy_event_cannot_be_silently_adopted_or_marked_managed(
    old_engine: Engine, settings: Settings
) -> None:
    migrate(settings)
    before = database_snapshot(old_engine)
    with pytest.raises(IntegrityError), unit_of_work(old_engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE dividend_events SET provenance_version=1 WHERE id='legacy-event'"
        )
    with pytest.raises(IntegrityError), unit_of_work(old_engine, write=True) as con:
        insert(
            con,
            "dividend_event_controls",
            {
                "event_id": "legacy-event",
                "owner_id": "owner",
                "request_id": str(uuid4()),
                "created_at": AT,
            },
        )
    assert database_snapshot(old_engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "id",
        "security",
        "identity",
        "current",
        "status",
        "version",
        "time",
        "time-nul",
        "owner",
        "control-time",
        "request",
    ],
)
def test_managed_event_control_rejects_invalid_owner_identity_and_initial_state(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    parent, changes = event(source), {}
    if problem == "id":
        parent["id"] = parent["event_identity_key"] = "private-invalid-uuid"
    elif problem == "security":
        parent["security_id"] = "stock"
    elif problem == "identity":
        parent["event_identity_key"] = "ex-date-is-not-identity"
    elif problem == "current":
        parent["current_revision_id"] = str(uuid4())
    elif problem == "status":
        parent["workflow_status"] = "VERIFIED"
    elif problem == "version":
        parent["version"] = 2
    elif problem == "time":
        parent["created_at"] = "2026-02-30T12:00:00.000000Z"
    elif problem == "time-nul":
        parent["created_at"] = AT + "\0private"
    elif problem == "owner":
        changes["owner_id"] = str(uuid4())
    elif problem == "control-time":
        changes["created_at"] = "2026-11-09T12:00:01.000000Z"
    else:
        changes["request_id"] = "private-invalid-request"
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, **changes))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "gap",
        "duplicate-id",
        "duplicate-version",
        "negative",
        "real",
        "date",
        "date-nul",
        "reason",
        "reason-nul",
        "clock",
        "verified-unlocked",
        "verified-unknown-basis",
    ],
)
def test_managed_revision_rejects_noncanonical_or_unfenced_terms(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, original, _ = staged(con, source)
        publish(con, parent, original)
    values = revision(parent, source, revision_no=2)
    if problem == "gap":
        values["revision_no"] = 3
    elif problem == "duplicate-id":
        values["id"] = original["id"]
    elif problem == "duplicate-version":
        values["revision_no"] = 1
    elif problem == "negative":
        values["dps_e8"] = -1
    elif problem == "real":
        values["dps_e8"] = 60000000.5
    elif problem == "date":
        values["ex_date"] = "2026-02-30"
    elif problem == "date-nul":
        values["ex_date"] = "2026-01-08\0private"
    elif problem == "reason":
        values["reason"] = "short"
    elif problem == "reason-nul":
        values["reason"] = "private-invalid\0reason"
    elif problem == "clock":
        values["created_at"] = "2026-11-09T11:59:59.000000Z"
    elif problem == "verified-unlocked":
        values.update(review_status="VERIFIED", manual_locked=0, adjustment_basis="AS_DECLARED")
    else:
        values.update(review_status="VERIFIED", manual_locked=1, adjustment_basis="UNKNOWN")
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_event_revisions", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "owner",
        "candidate",
        "count",
        "terms",
        "verified-source",
        "manual-missing-citation",
        "manual-with-candidate",
        "verify-without-current",
        "withdraw-without-current",
    ],
)
def test_revision_decision_requires_explicit_owned_current_candidate_or_manual_citation(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    if problem == "terms":
        rev["dps_e8"] += 1
    if problem == "verified-source":
        with unit_of_work(engine, write=True) as con:
            alternate = candidate(
                source["observation"],
                owner_id=str(OWNER),
                created_at=AT,
                adjustment_basis="AS_DECLARED",
            )
            insert(con, "dividend_candidate_revisions", alternate)
        source = {**source, "candidate": alternate}
        rev = revision(parent, source, review_status="VERIFIED", manual_locked=1)
    provenance = decision(parent, rev, source)
    if problem == "owner":
        provenance["owner_id"] = str(OTHER)
    elif problem == "candidate":
        provenance["candidate_revision_id"] = str(uuid4())
    elif problem == "count":
        provenance["evidence_count"] = 0
    elif problem == "manual-missing-citation":
        provenance.update(decision_kind="MANUAL", candidate_revision_id=None, evidence_count=0)
    elif problem == "manual-with-candidate":
        provenance.update(decision_kind="MANUAL", citation="Synthetic owner reference")
    elif problem == "verify-without-current":
        provenance.update(decision_kind="VERIFY", citation="Synthetic verification reference")
        rev.update(review_status="VERIFIED", manual_locked=1, adjustment_basis="AS_DECLARED")
    elif problem == "withdraw-without-current":
        provenance.update(
            decision_kind="WITHDRAW",
            candidate_revision_id=None,
            evidence_count=0,
            citation="Synthetic withdrawal reference",
        )
        rev["review_status"] = "WITHDRAWN"
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
    assert database_snapshot(engine) == before


def test_a_candidate_already_bound_to_one_event_cannot_silently_create_another(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        first, rev, _ = staged(con, source)
        publish(con, first, rev)
    other, candidate_revision = event(source), None
    candidate_revision = revision(other, source)
    provenance = decision(other, candidate_revision, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", other)
        insert(con, "dividend_event_controls", control(other, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", candidate_revision)
        insert(con, "dividend_revision_decisions", provenance)
    assert database_snapshot(engine) == before


def test_new_candidate_decision_cannot_use_a_superseded_candidate_version(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        previous = source["candidate"]
        insert(
            con,
            "dividend_candidate_revisions",
            {**previous, "id": str(uuid4()), "revision_no": 2, "request_id": str(uuid4())},
        )
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "malformed",
        "object",
        "unknown",
        "duplicate",
        "nontext",
        "too-many",
        "foreign-observation",
        "wrong-source",
    ],
)
def test_evidence_links_are_explicit_unique_and_cannot_substitute_other_sources(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    evidence = link(rev, source)
    if problem == "malformed":
        evidence["field_names_json"] = "[private-invalid"
    elif problem == "object":
        evidence["field_names_json"] = "{}"
    elif problem == "unknown":
        evidence["field_names_json"] = canonical(["private-invalid"])
    elif problem == "duplicate":
        evidence["field_names_json"] = canonical(["ex_date", "ex_date"])
    elif problem == "nontext":
        evidence["field_names_json"] = canonical([1])
    elif problem == "too-many":
        evidence["field_names_json"] = canonical(["ex_date"] * 9)
    elif problem == "foreign-observation":
        evidence["observation_id"] = str(uuid4())
    else:
        evidence["observation_id"] = "obs"
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", evidence)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem", ["owner", "request", "reason", "before", "after", "duplicate", "creation-request"]
)
def test_publication_requires_one_matching_actor_request_reason_and_hash_shape(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    parent = event(source)
    rev = revision(parent, source)
    provenance = decision(parent, rev, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        ctl = control(parent, request_id=provenance["request_id"])
        if problem == "creation-request":
            ctl["request_id"] = str(uuid4())
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", ctl)
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(rev, source))
        row = {
            "id": str(uuid4()),
            "owner_id": provenance["owner_id"],
            "entity_type": "dividend_event_revision",
            "entity_id": rev["id"],
            "action": "DIVIDEND_EVENT_REVISED",
            "request_id": provenance["request_id"],
            "reason": rev["reason"],
            "before_sha256": None,
            "after_sha256": "a" * 64,
            "created_at": AT,
        }
        if problem == "owner":
            row["owner_id"] = str(OTHER)
        elif problem == "request":
            row["request_id"] = str(uuid4())
        elif problem == "reason":
            row["reason"] = "A different synthetic audit reason."
        elif problem == "before":
            row["before_sha256"] = "b" * 64
        elif problem == "after":
            row["after_sha256"] = "invalid"
        insert(con, "audit_events", row)
        if problem == "duplicate":
            insert(con, "audit_events", {**row, "id": str(uuid4())})
        publish(con, parent, rev)
    assert database_snapshot(engine) == before


def test_manual_lock_blocks_candidate_and_preserves_manual_history(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, first, _ = staged(con, source, manual=True, status="VERIFIED", locked=1)
        publish(con, parent, first)
    before = database_snapshot(engine)
    candidate_revision = revision(parent, source, revision_no=2)
    provenance = decision(parent, candidate_revision, source)
    with (
        pytest.raises(IntegrityError, match="INVALID_MANAGED_DIVIDEND_TRANSITION"),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, "dividend_event_revisions", candidate_revision)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(candidate_revision, source))
        audited(con, candidate_revision, provenance)
        publish(con, parent, candidate_revision, 2)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        second, _ = manual_revision(
            con, parent, source, number=2, status="VERIFIED", locked=1, dps_e8=70000000
        )
        publish(con, parent, second, 2)
        third, _ = manual_revision(
            con,
            parent,
            source,
            number=3,
            kind="WITHDRAW",
            status="WITHDRAWN",
            locked=1,
            dps_e8=70000000,
        )
        publish(con, parent, third, 3)
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT revision_no,dps_e8,review_status,manual_locked FROM dividend_event_revisions "
            "WHERE event_id=? ORDER BY revision_no",
            (parent["id"],),
        ).all()
        assert [tuple(row) for row in rows] == [
            (1, 60000000, "VERIFIED", 1),
            (2, 70000000, "VERIFIED", 1),
            (3, 70000000, "WITHDRAWN", 1),
        ]
        assert con.exec_driver_sql(
            "SELECT current_revision_id,workflow_status,version FROM dividend_events WHERE id=?",
            (parent["id"],),
        ).one() == (third["id"], "WITHDRAWN", 3)
        assert con.exec_driver_sql("SELECT count(*) FROM dividend_receipts").scalar_one() == 0
    before = database_snapshot(engine)
    future = revision(parent, source, revision_no=4)
    future_decision = decision(parent, future, source)
    with (
        pytest.raises(IntegrityError, match="INVALID_MANAGED_DIVIDEND_TRANSITION"),
        unit_of_work(engine, write=True) as con,
    ):
        insert(con, "dividend_event_revisions", future)
        insert(con, "dividend_revision_decisions", future_decision)
        insert(con, "dividend_evidence_links", link(future, source))
        audited(con, future, future_decision)
        publish(con, parent, future, 4)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("conflict", ["id", "event-version"])
def test_legacy_dividend_revisions_cannot_be_replaced(
    engine: Engine, source: dict[str, Any], conflict: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent = {
            **event(source),
            "id": "legacy-event",
            "event_identity_key": "legacy-event",
            "provenance_version": 0,
        }
        rev = {**revision(parent, source), "id": "legacy-revision"}
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_revisions", rev)
    before = database_snapshot(engine)
    replacement = {
        **rev,
        "id": str(uuid4()) if conflict == "event-version" else rev["id"],
        "dps_e8": 70000000,
    }
    columns = ",".join(replacement)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO dividend_event_revisions ({columns}) "
            f"VALUES ({','.join('?' for _ in replacement)})",
            tuple(replacement.values()),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("conflict", ["id", "source-hash"])
def test_unlinked_dividend_observation_cannot_be_replaced(
    engine: Engine, source: dict[str, Any], conflict: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        old = dict(
            con.exec_driver_sql(
                "SELECT * FROM dividend_observations WHERE id=?",
                (source["observation"]["observation_id"],),
            )
            .mappings()
            .one()
        )
        original = DividendObservation.model_validate(
            {**json.loads(old["normalized_json"]), "dps": "0.70000000"}
        )
        observation = {
            **old,
            "id": str(uuid4()),
            "dps_e8": 70000000,
            "normalized_json": canonical(original.model_dump(mode="json")),
            "normalized_sha256": original.normalized_sha256,
        }
        insert(con, "dividend_observations", observation)
    before = database_snapshot(engine)
    if conflict == "id":
        alternate = DividendObservation.model_validate(
            {**json.loads(old["normalized_json"]), "dps": "0.80000000"}
        )
        replacement = {
            **observation,
            "dps_e8": 80000000,
            "normalized_json": canonical(alternate.model_dump(mode="json")),
            "normalized_sha256": alternate.normalized_sha256,
        }
    else:
        replacement = {
            **observation,
            "id": str(uuid4()),
            "source_metadata_json": canonical({"synthetic": "replaced"}),
        }
    columns = ",".join(replacement)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO dividend_observations ({columns}) "
            f"VALUES ({','.join('?' for _ in replacement)})",
            tuple(replacement.values()),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("conflict", ["id", "source-hash"])
def test_unlinked_split_observation_cannot_be_replaced(
    engine: Engine, source: dict[str, Any], conflict: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        original = (
            con.exec_driver_sql(
                "SELECT * FROM dividend_observations WHERE id=?",
                (source["observation"]["observation_id"],),
            )
            .mappings()
            .one()
        )
        old_run = dict(
            con.exec_driver_sql(
                "SELECT * FROM provider_runs WHERE id=?", (original["provider_run_id"],)
            )
            .mappings()
            .one()
        )
        old_context = dict(
            con.exec_driver_sql(
                "SELECT * FROM provider_fetch_contexts WHERE id=?", (old_run["context_id"],)
            )
            .mappings()
            .one()
        )
        context = {**old_context, "id": str(uuid4()), "operation": "SPLITS"}
        run = {**old_run, "id": str(uuid4()), "context_id": context["id"], "operation": "SPLITS"}
        insert(con, "provider_fetch_contexts", context)
        insert(con, "provider_runs", run)
        factor = SplitObservation(
            provider_id="yahoo",
            security_id=SECURITY,
            provider_symbol="SYNTH",
            effective_date="2026-01-08",
            ratio_numerator=2,
            ratio_denominator=1,
        )
        observation = {
            "id": str(uuid4()),
            "provider_run_id": run["id"],
            "provider_id": "yahoo",
            "origin_group": "YAHOO",
            "security_id": str(SECURITY),
            "provider_symbol": "SYNTH",
            "normalized_sha256": factor.normalized_sha256,
            "raw_sha256": run["raw_sha256"],
            "source_url": run["source_url"],
            "effective_date": "2026-01-08",
            "ratio_num": 2,
            "ratio_den": 1,
            "effect_scope": "PROVIDER_ADJUSTMENT_ONLY",
            "normalized_json": canonical(factor.model_dump(mode="json")),
            "retrieved_at": run["finished_at"],
        }
        insert(con, "split_observations", observation)
    before = database_snapshot(engine)
    if conflict == "id":
        alternate = factor.model_copy(update={"ratio_numerator": 3})
        replacement = {
            **observation,
            "ratio_num": 3,
            "normalized_sha256": alternate.normalized_sha256,
            "normalized_json": canonical(alternate.model_dump(mode="json")),
        }
    else:
        replacement = {**observation, "id": str(uuid4())}
    columns = ",".join(replacement)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO split_observations ({columns}) "
            f"VALUES ({','.join('?' for _ in replacement)})",
            tuple(replacement.values()),
        )
    assert database_snapshot(engine) == before


def test_published_event_audit_cannot_be_replaced(engine: Engine, source: dict[str, Any]) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
        original = dict(
            con.exec_driver_sql(
                "SELECT * FROM audit_events WHERE entity_type='dividend_event_revision' "
                "AND entity_id=?",
                (rev["id"],),
            )
            .mappings()
            .one()
        )
    before = database_snapshot(engine)
    changed = {**original, "reason": "Synthetic replaced audit reason.", "after_sha256": "a" * 64}
    columns = ",".join(changed)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO audit_events ({columns}) "
            f"VALUES ({','.join('?' for _ in changed)})",
            tuple(changed.values()),
        )
    assert database_snapshot(engine) == before


def test_explicit_verification_appends_identical_terms_and_requires_citation(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, first, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, first)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        manual_revision(
            con,
            parent,
            source,
            number=2,
            kind="VERIFY",
            status="VERIFIED",
            locked=1,
            dps_e8=first["dps_e8"] + 1,
        )
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        verified, _ = manual_revision(
            con, parent, source, number=2, kind="VERIFY", status="VERIFIED", locked=1
        )
        publish(con, parent, verified, 2)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT current_revision_id,workflow_status,version FROM dividend_events WHERE id=?",
            (parent["id"],),
        ).one() == (verified["id"], "VERIFIED", 2)
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_event_revisions WHERE event_id=?", (parent["id"],)
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "target",
    [
        "event-update",
        "event-delete",
        "event-replace",
        "revision-update",
        "revision-delete",
        "revision-replace",
        "control-update",
        "control-delete",
        "control-replace",
        "decision-update",
        "decision-delete",
        "decision-replace",
        "link-update",
        "link-delete",
        "link-replace",
    ],
)
def test_managed_event_history_and_evidence_reject_update_delete_and_replace(
    engine: Engine, source: dict[str, Any], target: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, provenance = staged(con, source)
        publish(con, parent, rev)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if target == "event-update":
            con.exec_driver_sql(
                "UPDATE dividend_events SET event_identity_key='changed' WHERE id=?",
                (parent["id"],),
            )
        elif target == "event-delete":
            con.exec_driver_sql("DELETE FROM dividend_events WHERE id=?", (parent["id"],))
        elif target == "event-replace":
            columns = ",".join(parent)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_events ({columns}) VALUES "
                f"({','.join('?' for _ in parent)})",
                tuple(parent.values()),
            )
        elif target == "revision-update":
            con.exec_driver_sql(
                "UPDATE dividend_event_revisions SET dps_e8=dps_e8+1 WHERE id=?", (rev["id"],)
            )
        elif target == "revision-delete":
            con.exec_driver_sql("DELETE FROM dividend_event_revisions WHERE id=?", (rev["id"],))
        elif target == "revision-replace":
            columns = ",".join(rev)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_event_revisions ({columns}) VALUES "
                f"({','.join('?' for _ in rev)})",
                tuple(rev.values()),
            )
        elif target == "control-update":
            con.exec_driver_sql(
                "UPDATE dividend_event_controls SET request_id=? WHERE event_id=?",
                (str(uuid4()), parent["id"]),
            )
        elif target == "control-delete":
            con.exec_driver_sql(
                "DELETE FROM dividend_event_controls WHERE event_id=?", (parent["id"],)
            )
        elif target == "control-replace":
            values = control(parent, request_id=provenance["request_id"])
            columns = ",".join(values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_event_controls ({columns}) VALUES "
                f"({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        elif target == "decision-update":
            con.exec_driver_sql(
                "UPDATE dividend_revision_decisions SET citation='changed' WHERE revision_id=?",
                (rev["id"],),
            )
        elif target == "decision-delete":
            con.exec_driver_sql(
                "DELETE FROM dividend_revision_decisions WHERE revision_id=?", (rev["id"],)
            )
        elif target == "decision-replace":
            columns = ",".join(provenance)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_revision_decisions ({columns}) VALUES "
                f"({','.join('?' for _ in provenance)})",
                tuple(provenance.values()),
            )
        elif target == "link-update":
            con.exec_driver_sql(
                "UPDATE dividend_evidence_links SET field_names_json='[]' WHERE revision_id=?",
                (rev["id"],),
            )
        elif target == "link-delete":
            con.exec_driver_sql(
                "DELETE FROM dividend_evidence_links WHERE revision_id=?", (rev["id"],)
            )
        else:
            values = link(rev, source)
            columns = ",".join(values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_evidence_links ({columns}) VALUES "
                f"({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["entitlement", "receipt"])
@pytest.mark.parametrize("operation", ["update", "replace"])
def test_legacy_cash_facts_cannot_be_reassigned_to_a_managed_event(
    engine: Engine, source: dict[str, Any], kind: str, operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, managed, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, managed)
        account = owned_account(con)
        old_event = {
            **event(source),
            "id": "legacy-event",
            "event_identity_key": "legacy-event",
            "provenance_version": 0,
        }
        old_revision = {
            **revision(old_event, source),
            "id": "legacy-revision",
            "adjustment_basis": "AS_DECLARED",
        }
        insert(con, "dividend_events", old_event)
        insert(con, "dividend_event_revisions", old_revision)
        old_entitlement = entitlement(old_revision, account)
        insert(con, "dividend_entitlements", old_entitlement)
        current_entitlement = entitlement(managed, account)
        old_receipt = {
            "id": "legacy-receipt",
            "entitlement_id": old_entitlement["id"],
            "account_id": account,
            "received_date": "2026-01-09",
            "received_micro": 6000000,
            "currency": "USD",
            "created_at": AT,
        }
        if kind == "receipt":
            insert(con, "dividend_entitlements", current_entitlement)
            insert(con, "dividend_receipts", old_receipt)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "replace":
            table = "dividend_entitlements" if kind == "entitlement" else "dividend_receipts"
            values = (
                {**old_entitlement, "revision_id": managed["id"]}
                if kind == "entitlement"
                else {**old_receipt, "entitlement_id": current_entitlement["id"]}
            )
            columns = ",".join(values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {table} ({columns}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        elif kind == "entitlement":
            con.exec_driver_sql(
                "UPDATE dividend_entitlements SET revision_id=? WHERE id=?",
                (managed["id"], old_entitlement["id"]),
            )
        else:
            con.exec_driver_sql(
                "UPDATE dividend_receipts SET entitlement_id=? WHERE id='legacy-receipt'",
                (current_entitlement["id"],),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("kind", ["entitlement", "receipt"])
@pytest.mark.parametrize("operation", ["update", "replace"])
def test_managed_cash_facts_cannot_be_reassigned_to_a_legacy_event(
    engine: Engine, source: dict[str, Any], kind: str, operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        record = posted_receipt(con, source)
        legacy_event = {
            **event(source),
            "id": "legacy-event",
            "event_identity_key": "legacy-event",
            "provenance_version": 0,
        }
        legacy_revision = {
            **revision(legacy_event, source),
            "id": "legacy-revision",
            "adjustment_basis": "AS_DECLARED",
        }
        insert(con, "dividend_events", legacy_event)
        insert(con, "dividend_event_revisions", legacy_revision)
        legacy_claim = entitlement(legacy_revision, record["receipt"]["account_id"])
        insert(con, "dividend_entitlements", legacy_claim)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        values = record["entitlement"] if kind == "entitlement" else record["receipt"]
        table = "dividend_entitlements" if kind == "entitlement" else "dividend_receipts"
        column = "revision_id" if kind == "entitlement" else "entitlement_id"
        target = legacy_revision["id"] if kind == "entitlement" else legacy_claim["id"]
        if operation == "replace":
            replacement = {**values, column: target}
            columns = ",".join(replacement)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO {table} ({columns}) "
                f"VALUES ({','.join('?' for _ in replacement)})",
                tuple(replacement.values()),
            )
        else:
            con.exec_driver_sql(f"UPDATE {table} SET {column}=? WHERE id=?", (target, values["id"]))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "invalid-uuid"),
        ("account_id", "A"),
        ("currency", "HKD"),
        ("eligible_shares_e8", 0),
        ("eligible_shares_e8", 1.5),
        ("gross_micro", -1),
        ("gross_micro", 1.5),
        ("tax_micro", -1),
        ("tax_micro", 1.5),
        ("fee_micro", -1),
        ("fee_micro", 1.5),
        ("expected_net_micro", 1.5),
        ("fx_rate_hkd_e12", 0),
        ("fx_rate_hkd_e12", 1.5),
        ("expected_hkd_micro", 1.5),
        ("tax_rate_snapshot_ppm", 1000001),
        ("tax_rate_snapshot_ppm", 1.5),
        ("projection_version", -1),
        ("projection_version", 1.5),
        ("created_at", "2026-02-30T12:00:00.000000Z"),
        ("created_at", AT + "\0private"),
    ],
)
def test_managed_entitlement_rejects_invalid_scope_and_scaled_values(
    engine: Engine, source: dict[str, Any], field: str, value: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
        claim = entitlement(rev, owned_account(con))
    claim[field] = value
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_entitlements", claim)
    assert database_snapshot(engine) == before


def test_managed_entitlement_preserves_exact_extremes_and_negative_net(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
        claim = entitlement(rev, owned_account(con))
        claim.update(
            eligible_shares_e8=9223372036854775807,
            gross_micro=9223372036854775807,
            expected_net_micro=-9223372036854775808,
            quality="NEEDS_REVIEW",
        )
        insert(con, "dividend_entitlements", claim)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT eligible_shares_e8,gross_micro,expected_net_micro,quality "
            "FROM dividend_entitlements WHERE id=?",
            (claim["id"],),
        ).one() == (9223372036854775807, 9223372036854775807, -9223372036854775808, "NEEDS_REVIEW")


@pytest.mark.parametrize("operation", ["update", "delete", "replace-id", "replace-scope"])
def test_managed_entitlement_history_cannot_be_rewritten(
    engine: Engine, source: dict[str, Any], operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
        claim = entitlement(rev, owned_account(con))
        insert(con, "dividend_entitlements", claim)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "update":
            con.exec_driver_sql(
                "UPDATE dividend_entitlements SET gross_micro=gross_micro+1 WHERE id=?",
                (claim["id"],),
            )
        elif operation == "delete":
            con.exec_driver_sql("DELETE FROM dividend_entitlements WHERE id=?", (claim["id"],))
        else:
            values = {**claim, "id": str(uuid4())} if operation == "replace-scope" else claim
            columns = ",".join(values)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_entitlements ({columns}) VALUES "
                f"({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "invalid-uuid"),
        ("account_id", "A"),
        ("received_date", "2026-02-30"),
        ("received_date", "2026-01-09\0private"),
        ("received_micro", 1.5),
        ("received_micro", -9223372036854775808),
        ("tax_micro", -1),
        ("tax_micro", 1.5),
        ("fee_micro", -1),
        ("fee_micro", 1.5),
        ("fx_rate_hkd_e12", 0),
        ("fx_rate_hkd_e12", 1.5),
        ("received_hkd_micro", 1.5),
        ("received_hkd_micro", -9223372036854775808),
        ("status", "VOID"),
        ("version", 2),
        ("voided_at", AT),
        ("created_at", "2026-02-30T12:00:00.000000Z"),
        ("created_at", AT + "\0private"),
    ],
)
def test_managed_receipt_rejects_invalid_scope_and_scaled_values(
    engine: Engine, source: dict[str, Any], field: str, value: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
        claim = entitlement(rev, owned_account(con))
        insert(con, "dividend_entitlements", claim)
    values = {
        "id": str(uuid4()),
        "entitlement_id": claim["id"],
        "account_id": claim["account_id"],
        "received_date": "2026-01-09",
        "received_micro": 6000000,
        "currency": "USD",
        "created_at": AT,
        field: value,
    }
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_receipts", values)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("currency", "amount", "fx", "booked"),
    [("USD", -1, None, None), ("USD", 0, None, None), ("HKD", 6000000, 1000000000000, 6000000)],
)
def test_managed_receipt_retains_explicit_signed_zero_and_cross_currency_cash(
    engine: Engine,
    source: dict[str, Any],
    currency: Literal["HKD", "USD"],
    amount: int,
    fx: int | None,
    booked: int | None,
) -> None:
    with unit_of_work(engine, write=True) as con:
        parent, rev, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, parent, rev)
        claim = entitlement(rev, owned_account(con))
        insert(con, "dividend_entitlements", claim)
        values = {
            "id": str(uuid4()),
            "entitlement_id": claim["id"],
            "account_id": claim["account_id"],
            "received_date": "2026-01-09",
            "received_micro": amount,
            "currency": currency,
            "fx_rate_hkd_e12": fx,
            "received_hkd_micro": booked,
            "created_at": AT,
        }
        insert(con, "dividend_receipts", values)
        post_cash(
            con,
            account=claim["account_id"],
            day="2026-01-09",
            currency=currency,
            amount=amount,
            fx=fx,
            booked=booked,
            kind="DIVIDEND_RECEIPT",
            source_type="DIVIDEND_RECEIPT",
            source_id=values["id"],
        )
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql(
            "SELECT currency,received_micro,fx_rate_hkd_e12,received_hkd_micro "
            "FROM dividend_receipts WHERE id=?",
            (values["id"],),
        ).one() == (currency, amount, fx, booked)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", str(uuid4())),
        ("received_date", "2026-01-10"),
        ("received_micro", 7000000),
        ("currency", "HKD"),
        ("tax_micro", 1),
        ("fee_micro", 1),
        ("fx_rate_hkd_e12", 7900000000000),
        ("received_hkd_micro", 47400000),
        ("note", "rewritten note"),
        ("source", "IMPORT"),
        ("created_at", "2026-11-10T12:00:00.000000Z"),
    ],
)
def test_void_cannot_rewrite_managed_receipt_facts(
    engine: Engine, source: dict[str, Any], field: str, value: Any
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = posted_receipt(con, source)["receipt"]
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        reverse_receipt(con, receipt)
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=?,"
            f"{field}=? WHERE id=?",
            (AT, value, receipt["id"]),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "problem",
    [
        "no-reversal",
        "wrong-reversal-day",
        "no-audit",
        "foreign-audit",
        "wrong-reason",
        "no-request",
        "no-before",
        "no-after",
        "duplicate-audit",
        "bad-void-time",
        "wrong-version",
    ],
)
def test_void_requires_audited_exact_cash_reversal(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = posted_receipt(con, source)["receipt"]
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if problem != "no-reversal":
            original = (
                con.exec_driver_sql(
                    "SELECT * FROM cash_ledger_entries WHERE source_id=? AND entry_role='PRIMARY'",
                    (receipt["id"],),
                )
                .mappings()
                .one()
            )
            if problem == "wrong-reversal-day":
                insert(
                    con,
                    "cash_ledger_entries",
                    {
                        "id": str(uuid4()),
                        "account_id": original["account_id"],
                        "effective_date": "2026-01-10",
                        "currency": original["currency"],
                        "amount_micro": -original["amount_micro"],
                        "fx_rate_hkd_e12": original["fx_rate_hkd_e12"],
                        "booked_hkd_micro": None,
                        "kind": "REVERSAL",
                        "entry_role": "REVERSAL",
                        "source_type": original["source_type"],
                        "source_id": original["source_id"],
                        "external_flow": original["external_flow"],
                        "note": REASON,
                        "created_at": AT,
                        "reverses_entry_id": original["id"],
                    },
                )
            else:
                reverse_cash(con, original, reason=REASON)
        if problem != "no-audit":
            audit(
                con,
                str(OTHER if problem == "foreign-audit" else OWNER),
                "dividend_receipt",
                receipt["id"],
                "DIVIDEND_RECEIPT_VOIDED",
                request_id=None if problem == "no-request" else str(uuid4()),
                before=None if problem == "no-before" else {"status": "ACTIVE"},
                after=None if problem == "no-after" else {"status": "VOID"},
                reason="A different explicit reason." if problem == "wrong-reason" else REASON,
            )
        if problem == "duplicate-audit":
            audit(
                con,
                str(OWNER),
                "dividend_receipt",
                receipt["id"],
                "DIVIDEND_RECEIPT_VOIDED",
                request_id=str(uuid4()),
                before={"status": "ACTIVE"},
                after={"status": "VOID"},
                reason=REASON,
            )
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=?,voided_at=? WHERE id=?",
            (
                3 if problem == "wrong-version" else 2,
                "2026-02-30T12:00:00.000000Z" if problem == "bad-void-time" else AT,
                receipt["id"],
            ),
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("fx", [None, 7800000000000])
def test_void_retains_actual_receipt_and_appends_exact_reversal(
    engine: Engine, source: dict[str, Any], fx: int | None
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = posted_receipt(con, source, fx=fx)["receipt"]
        reverse_receipt(con, receipt)
        con.exec_driver_sql(
            "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
            (AT, receipt["id"]),
        )
    with unit_of_work(engine) as con:
        actual = con.exec_driver_sql(
            "SELECT status,version,received_micro,fx_rate_hkd_e12,received_hkd_micro "
            "FROM dividend_receipts WHERE id=?",
            (receipt["id"],),
        ).one()
        assert tuple(actual) == ("VOID", 2, 6000000, fx, 46800000 if fx is not None else None)
        rows = con.exec_driver_sql(
            "SELECT entry_role,amount_micro,fx_rate_hkd_e12,booked_hkd_micro,reverses_entry_id "
            "FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' AND source_id=? "
            "ORDER BY entry_role",
            (receipt["id"],),
        ).all()
        assert len(rows) == 2
        assert tuple(rows[0][:4]) == ("PRIMARY", 6000000, fx, 46800000 if fx is not None else None)
        assert tuple(rows[1][:4]) == (
            "REVERSAL",
            -6000000,
            fx,
            -46800000 if fx is not None else None,
        )
        assert rows[1][4] is not None


@pytest.mark.parametrize("operation", ["delete", "replace"])
def test_managed_receipts_cannot_be_deleted_or_replaced(
    engine: Engine, source: dict[str, Any], operation: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = posted_receipt(con, source)["receipt"]
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        if operation == "delete":
            con.exec_driver_sql("DELETE FROM dividend_receipts WHERE id=?", (receipt["id"],))
        else:
            columns = ",".join(receipt)
            con.exec_driver_sql(
                f"INSERT OR REPLACE INTO dividend_receipts ({columns}) VALUES "
                f"({','.join('?' for _ in receipt)})",
                tuple(receipt.values()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("conflict", ["primary-id", "source-identity", "reversal-source"])
def test_managed_receipt_cash_cannot_be_replaced(
    engine: Engine, source: dict[str, Any], conflict: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        receipt = posted_receipt(con, source)["receipt"]
        if conflict == "reversal-source":
            reverse_receipt(con, receipt)
            con.exec_driver_sql(
                "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
                (AT, receipt["id"]),
            )
        role = "REVERSAL" if conflict == "reversal-source" else "PRIMARY"
        cash = dict(
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE source_type='DIVIDEND_RECEIPT' "
                "AND source_id=? AND entry_role=?",
                (receipt["id"], role),
            )
            .mappings()
            .one()
        )
    before = database_snapshot(engine)
    if conflict == "primary-id":
        replacement = {
            "id": cash["id"],
            "account_id": cash["account_id"],
            "effective_date": cash["effective_date"],
            "currency": "USD",
            "amount_micro": 1,
            "kind": "ADJUSTMENT",
            "source_type": "MANUAL_CASH",
            "source_id": str(uuid4()),
            "created_at": AT,
        }
    else:
        replacement = {**cash, "id": str(uuid4()), "note": "Synthetic replaced cash entry"}
    columns = ",".join(replacement)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO cash_ledger_entries ({columns}) "
            f"VALUES ({','.join('?' for _ in replacement)})",
            tuple(replacement.values()),
        )
    assert database_snapshot(engine) == before


def test_cash_correction_successor_cannot_be_replaced_across_owned_accounts(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        first, second = owned_account(con), owned_account(con)
        original_id = post_cash(
            con,
            account=first,
            day="2026-01-09",
            currency="USD",
            amount=100,
            fx=None,
            booked=None,
            kind="ADJUSTMENT",
            source_type="MANUAL_CASH",
            source_id=str(uuid4()),
        )
        original = (
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries WHERE id=?", (original_id,))
            .mappings()
            .one()
        )
        reverse_cash(con, original, reason=REASON)
        post_cash(
            con,
            account=first,
            day="2026-01-09",
            currency="USD",
            amount=200,
            fx=None,
            booked=None,
            kind="ADJUSTMENT",
            source_type="CASH_CORRECTION",
            source_id=original_id,
        )
    before = database_snapshot(engine)
    replacement = {
        "id": str(uuid4()),
        "account_id": second,
        "effective_date": "2026-01-09",
        "currency": "USD",
        "amount_micro": 300,
        "kind": "ADJUSTMENT",
        "source_type": "CASH_CORRECTION",
        "source_id": original_id,
        "created_at": AT,
    }
    columns = ",".join(replacement)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            f"INSERT OR REPLACE INTO cash_ledger_entries ({columns}) "
            f"VALUES ({','.join('?' for _ in replacement)})",
            tuple(replacement.values()),
        )
    assert database_snapshot(engine) == before


def review_item(
    con: Any,
    parent: dict[str, Any],
    previous: dict[str, Any],
    next_revision: dict[str, Any],
    account: str,
    **changes: Any,
) -> dict[str, Any]:
    portfolio = con.exec_driver_sql(
        "SELECT portfolio_id FROM accounts WHERE id=?", (account,)
    ).scalar_one()
    values = {
        "id": str(uuid4()),
        "event_id": parent["id"],
        "account_id": account,
        "portfolio_id": portfolio,
        "kind": "DIVIDEND_REVISION",
        "severity": "WARNING",
        "status": "OPEN",
        "reason_code": "RECEIPT_RECONCILIATION_REQUIRED",
        "detail_json": canonical(
            {"previous_revision_id": previous["id"], "new_revision_id": next_revision["id"]}
        ),
        "created_at": AT,
        **changes,
    }
    insert(con, "review_items", values)
    return values


@pytest.mark.parametrize(
    "problem",
    ["missing", "event", "account", "portfolio", "kind", "status", "reason", "previous", "next"],
)
def test_event_change_requires_matching_review_for_every_receipt_account(
    engine: Engine, source: dict[str, Any], problem: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        first = posted_receipt(con, source, fx=7800000000000)
        parent, previous = first["event"], first["revision"]
        second_claim = entitlement(previous, owned_account(con))
        insert(con, "dividend_entitlements", second_claim)
        add_posted_receipt(con, second_claim)
        unrelated, unrelated_revision, _ = staged(con, source, manual=True, status="PROVISIONAL")
        publish(con, unrelated, unrelated_revision)
        first_portfolio = con.exec_driver_sql(
            "SELECT portfolio_id FROM accounts WHERE id=?", (first["receipt"]["account_id"],)
        ).scalar_one()
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        updated, _ = manual_revision(con, parent, source, number=2, dps_e8=70000000)
        review_item(con, parent, previous, updated, first["receipt"]["account_id"])
        changes: dict[str, Any] = {}
        if problem == "event":
            changes["event_id"] = unrelated["id"]
        elif problem == "account":
            changes["account_id"] = first["receipt"]["account_id"]
        elif problem == "portfolio":
            changes["portfolio_id"] = first_portfolio
        elif problem == "kind":
            changes["kind"] = "OTHER"
        elif problem == "status":
            changes["status"] = "RESOLVED"
        elif problem == "reason":
            changes["reason_code"] = "OTHER"
        elif problem == "previous":
            changes["detail_json"] = canonical(
                {"previous_revision_id": unrelated_revision["id"], "new_revision_id": updated["id"]}
            )
        elif problem == "next":
            changes["detail_json"] = canonical(
                {
                    "previous_revision_id": previous["id"],
                    "new_revision_id": unrelated_revision["id"],
                }
            )
        if problem != "missing":
            review_item(con, parent, previous, updated, second_claim["account_id"], **changes)
        publish(con, parent, updated, 2)
    assert database_snapshot(engine) == before


def test_review_follows_old_receipts_across_revisions_and_preserves_actual_cash(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        first = posted_receipt(con, source, fx=7800000000000)
        parent, previous = first["event"], first["revision"]
        add_posted_receipt(con, first["entitlement"])
        second_claim = entitlement(previous, owned_account(con))
        insert(con, "dividend_entitlements", second_claim)
        add_posted_receipt(con, second_claim)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        second, _ = manual_revision(con, parent, source, number=2, dps_e8=70000000)
        for account in (first["receipt"]["account_id"], second_claim["account_id"]):
            review_item(con, parent, previous, second, account)
        publish(con, parent, second, 2)
    middle = database_snapshot(engine)
    for table in ("dividend_receipts", "dividend_entitlements", "cash_ledger_entries"):
        assert middle[table] == before[table]
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        third, _ = manual_revision(con, parent, source, number=3, dps_e8=80000000)
        publish(con, parent, third, 3)
    assert database_snapshot(engine) == middle
    with unit_of_work(engine, write=True) as con:
        third, _ = manual_revision(con, parent, source, number=3, dps_e8=80000000)
        for account in (first["receipt"]["account_id"], second_claim["account_id"]):
            review_item(con, parent, second, third, account)
        publish(con, parent, third, 3)
    after = database_snapshot(engine)
    for table in ("dividend_receipts", "dividend_entitlements", "cash_ledger_entries"):
        assert after[table] == before[table]
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM review_items WHERE event_id=?", (parent["id"],)
            ).scalar_one()
            == 4
        )
        assert con.exec_driver_sql(
            "SELECT current_revision_id,version FROM dividend_events WHERE id=?", (parent["id"],)
        ).one() == (third["id"], 3)


@pytest.mark.parametrize("case", ["unchanged-verify", "void-receipt"])
def test_unchanged_verification_and_void_receipts_do_not_require_reconciliation(
    engine: Engine, source: dict[str, Any], case: str
) -> None:
    with unit_of_work(engine, write=True) as con:
        record = posted_receipt(con, source)
        if case == "void-receipt":
            reverse_receipt(con, record["receipt"])
            con.exec_driver_sql(
                "UPDATE dividend_receipts SET status='VOID',version=2,voided_at=? WHERE id=?",
                (AT, record["receipt"]["id"]),
            )
        if case == "unchanged-verify":
            next_revision, _ = manual_revision(
                con, record["event"], source, number=2, kind="VERIFY", status="VERIFIED", locked=1
            )
        else:
            next_revision, _ = manual_revision(
                con, record["event"], source, number=2, dps_e8=70000000
            )
        publish(con, record["event"], next_revision, 2)
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM review_items").scalar_one() == 0
        assert (
            con.exec_driver_sql(
                "SELECT current_revision_id FROM dividend_events WHERE id=?",
                (record["event"]["id"],),
            ).scalar_one()
            == next_revision["id"]
        )


def test_withdrawal_with_existing_cash_requires_a_correlated_account_review(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        record = posted_receipt(con, source)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        withdrawn, _ = manual_revision(
            con, record["event"], source, number=2, kind="WITHDRAW", status="WITHDRAWN"
        )
        publish(con, record["event"], withdrawn, 2)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        withdrawn, _ = manual_revision(
            con, record["event"], source, number=2, kind="WITHDRAW", status="WITHDRAWN"
        )
        review_item(
            con, record["event"], record["revision"], withdrawn, record["receipt"]["account_id"]
        )
        publish(con, record["event"], withdrawn, 2)
    after = database_snapshot(engine)
    for table in ("dividend_receipts", "dividend_entitlements", "cash_ledger_entries"):
        assert after[table] == before[table]


def test_candidate_revision_requires_every_explicitly_selected_source_link(
    engine: Engine, source: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        other = cloned_source(con, source["observation"], dps="0.70000000")
        selected = with_matches(
            candidate(source["observation"], owner_id=str(OWNER), created_at=AT),
            [match(source["observation"]), match(other)],
        )
        insert(con, "dividend_candidate_revisions", selected)
    source = {**source, "candidate": selected}
    parent, rev = event(source), None
    rev = revision(parent, source)
    provenance = decision(parent, rev, source, evidence_count=2)
    before = database_snapshot(engine)
    with pytest.raises(IntegrityError), unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(rev, source))
        audited(con, rev, provenance)
        publish(con, parent, rev)
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        insert(con, "dividend_events", parent)
        insert(con, "dividend_event_controls", control(parent, request_id=provenance["request_id"]))
        insert(con, "dividend_event_revisions", rev)
        insert(con, "dividend_revision_decisions", provenance)
        insert(con, "dividend_evidence_links", link(rev, source))
        insert(con, "dividend_evidence_links", link(rev, {"observation": other}))
        audited(con, rev, provenance)
        publish(con, parent, rev)
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT count(*) FROM dividend_evidence_links WHERE revision_id=?", (rev["id"],)
            ).scalar_one()
            == 2
        )
