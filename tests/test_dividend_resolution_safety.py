"""Resolution pins, writer isolation and invalidation remain owner-scoped."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OTHER, OWNER, add_posted_receipt, entitlement
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent, owned_account
from tests.test_dividend_rejection_resolution import reject, resolve
from tests.test_dividend_rejection_service import case as case
from tests.test_dividend_rejection_writer import affected_portfolio
from tests.test_dividend_rejection_writer import intent as rejection_intent


def test_resolution_invalidates_only_affected_owned_portfolios(engine: Engine, case: Any) -> None:
    portfolio, _ = affected_portfolio(engine, case)
    foreign, _ = affected_portfolio(engine, case, OTHER)
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    successor, _, _, _ = resolve(engine, case, rejected.id)
    after = database_snapshot(engine)
    changed = {
        "dividend_events",
        "dividend_event_revisions",
        "dividend_revision_decisions",
        "dividend_rejection_resolutions",
        "audit_events",
        "portfolios",
        "projection_invalidations",
    }
    assert {key: value for key, value in before.items() if key not in changed} == {
        key: value for key, value in after.items() if key not in changed
    }
    with unit_of_work(engine) as con:
        rows = con.exec_driver_sql(
            "SELECT id,data_revision FROM portfolios WHERE id IN (?,?) ORDER BY id",
            (portfolio, foreign),
        ).all()
        invalidation = con.exec_driver_sql(
            "SELECT portfolio_id,source_id,reason,from_date FROM projection_invalidations "
            "WHERE source_id=?",
            (str(successor.revision_id),),
        ).one()
    revisions = {row.id: row.data_revision for row in rows}
    assert revisions[portfolio] == 2 and revisions[foreign] == 0
    assert tuple(invalidation) == (
        portfolio,
        str(successor.revision_id),
        "DIVIDEND_EVENT_CHANGED",
        case.terms.ex_date,
    )


def test_foreign_owner_cannot_resolve_or_replay_an_owned_resolution(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    attempted = next_intent(case, "VERIFY", resolve_rejection_id=rejected.id)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_NOT_FOUND$"),
    ):
        event_writer.save(con, OTHER, attempted, at=datetime.now(UTC), request_id=uuid4())
    assert database_snapshot(engine) == before
    successor, _, at, request = resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_REVISION_CONFLICT$"),
    ):
        event_writer.save(con, OTHER, successor, at=at, request_id=request)
    assert database_snapshot(engine) == before


def test_resolution_cannot_predate_the_correlated_rejection_audit(
    engine: Engine, case: Any
) -> None:
    created = datetime.now(UTC)
    value = rejection_intent(case)
    with (
        unit_of_work(engine, write=True) as con,
        patch.object(rejection_writer, "now", return_value=created),
        patch("spm_api.audit.now", return_value=created + timedelta(seconds=2)),
    ):
        rejected = rejection_writer.save(con, OWNER, value, request_id=uuid4())
    before = database_snapshot(engine)
    with pytest.raises(events.EventError, match="^DIVIDEND_EVENT_TIME_CONFLICT$"):
        resolve(engine, case, rejected.id, at=created + timedelta(seconds=1))
    assert database_snapshot(engine) == before


def test_uncertain_resolution_savepoint_release_aborts_unrelated_outer_work(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "synthetic_outer_work",
            str(uuid4()),
            "SYNTHETIC_OUTER_WORK",
            request_id=str(uuid4()),
            after={"synthetic": True},
            reason="Synthetic outer work",
        )
        with (
            patch.object(
                engine.dialect,
                "do_release_savepoint",
                side_effect=SQLAlchemyError("synthetic uncertain release"),
            ),
            pytest.raises(SQLAlchemyError, match="synthetic uncertain release"),
        ):
            event_writer.save(
                con,
                OWNER,
                next_intent(case, "VERIFY", resolve_rejection_id=rejected.id),
                at=datetime.now(UTC),
                request_id=uuid4(),
            )
        assert not con.in_transaction()
    assert database_snapshot(engine) == before


def test_older_entitlement_and_receipt_fence_resolution_at_both_layers(
    engine: Engine, case: Any
) -> None:
    next_revision = next_intent(case, "VERIFY")
    with unit_of_work(engine, write=True) as con:
        event_writer.save(con, OWNER, next_revision, at=datetime.now(UTC), request_id=uuid4())
    _, account = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        claim = entitlement({"id": str(case.revision_id), "currency": "USD"}, account)
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        add_posted_receipt(con, claim, fx=7_800_000_000_000)
    rejected = reject(engine, next_revision)
    successor = next_intent(next_revision, "WITHDRAW", number=3, resolve_rejection_id=rejected.id)
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_PROJECTION_REQUIRED$"),
    ):
        event_writer.save(con, OWNER, successor, at=datetime.now(UTC), request_id=uuid4())
    assert database_snapshot(engine) == before
    with unit_of_work(engine, write=True) as con:
        execute = con.execute

        def bypass_application_check(*args: Any, **kwargs: Any) -> Any:
            if "SELECT 1 FROM dividend_entitlements ent" in str(args[0]):
                result = MagicMock()
                result.first.return_value = None
                return result
            return execute(*args, **kwargs)

        with (
            patch.object(con, "execute", side_effect=bypass_application_check),
            pytest.raises(IntegrityError, match="DIVIDEND_EVENT_REJECTION_PENDING"),
        ):
            event_writer.save(con, OWNER, successor, at=datetime.now(UTC), request_id=uuid4())
    assert database_snapshot(engine) == before
