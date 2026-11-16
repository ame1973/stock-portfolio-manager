"""Historical source audits cannot occur after a new projection's claimed time."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_candidate_service as candidates
import spm_api.dividend_event_writer as events
import spm_api.dividend_projection_writer as writer
from spm_api.db import unit_of_work
from spm_api.dividend_evidence import DividendTerms
from spm_api.observation_service import original_dividend
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import REASON
from tests.test_dividend_event_writer import next_intent, terms
from tests.test_dividend_projection_writer import intent, save
from tests.test_provider_persistence import book as book
from tests.test_provider_persistence import fetched
from tests.test_provider_persistence import save as capture_source

pytestmark = pytest.mark.anyio


async def test_projection_cannot_predate_a_later_verified_historical_candidate_audit(
    engine: Engine, book: dict[str, Any]
) -> None:
    receipt = capture_source(engine, book, await fetched(book))
    owner = UUID(book["owner"]["id"])
    source_id = UUID(receipt.observation_ids[0])
    with unit_of_work(engine) as con:
        source = original_dividend(con, source_id)
    assert source is not None
    candidate = candidates.CandidateDecision(
        id=uuid4(),
        candidate_id=uuid4(),
        revision_no=1,
        state="OPEN",
        terms=DividendTerms(
            security_id=source.security_id,
            ex_date=source.ex_date,
            dps=source.dps,
            currency=source.currency,
            adjustment_basis=source.adjustment_basis,
        ),
        matches=(
            candidates.CandidateMatch(
                observation_id=source_id, source_sha256=source.normalized_sha256, reason=REASON
            ),
        ),
        change_reason=REASON,
    )
    with unit_of_work(engine, write=True) as con:
        candidates.save(
            con, owner, candidate, at=datetime(2026, 11, 9, tzinfo=UTC), request_id=uuid4()
        )
    adopted = events.EventIntent(
        event_id=uuid4(),
        revision_id=uuid4(),
        revision_no=1,
        kind="CANDIDATE",
        candidate_id=candidate.candidate_id,
        candidate_version=1,
        reason=REASON,
    )
    with unit_of_work(engine, write=True) as con:
        events.save(
            con, owner, adopted, at=datetime(2026, 11, 9, 12, tzinfo=UTC), request_id=uuid4()
        )
    manual = next_intent(adopted, "MANUAL", terms=terms(security_id=source.security_id))
    with unit_of_work(engine, write=True) as con:
        events.save(
            con, owner, manual, at=datetime(2026, 11, 10, 12, tzinfo=UTC), request_id=uuid4()
        )
    account = book["account"]
    with unit_of_work(engine) as con:
        prior_times = con.exec_driver_sql(
            "SELECT created_at FROM audit_events WHERE entity_type='dividend_event_revision' "
            "AND entity_id=? UNION ALL SELECT created_at FROM projection_invalidations "
            "WHERE portfolio_id=?",
            (str(manual.revision_id), account["portfolio_id"]),
        ).scalars()
        earlier = max(datetime.fromisoformat(item.replace("Z", "+00:00")) for item in prior_times)
        account_row = con.exec_driver_sql(
            "SELECT a.version AS account_version,b.version AS broker_version,a.broker_id "
            "FROM accounts a JOIN brokers b ON b.id=a.broker_id WHERE a.id=?",
            (account["id"],),
        ).one()
    earlier += timedelta(microseconds=1)
    newer = candidate.model_copy(update={"id": uuid4(), "revision_no": 2})
    with unit_of_work(engine, write=True) as con:
        candidates.save(
            con, owner, newer, at=datetime(2026, 11, 10, 13, tzinfo=UTC), request_id=uuid4()
        )
    scope = {
        "owner": owner,
        "event": manual,
        "account_id": UUID(account["id"]),
        "portfolio_id": UUID(account["portfolio_id"]),
        "broker_id": UUID(account_row.broker_id),
        "account_version": account_row.account_version,
        "broker_version": account_row.broker_version,
    }
    value = intent(engine, scope)
    before = database_snapshot(engine)
    with (
        patch.object(writer, "now", return_value=earlier),
        pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_TIME_CONFLICT$"),
    ):
        save(engine, scope, value, uuid4())
    assert database_snapshot(engine) == before
