"""Current projection checks cannot promote later source evidence into old snapshots."""

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_projection_current as current_service
import spm_api.dividend_projection_writer as writer
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_projection_source_time import add_candidate_revision, prepared_source
from tests.test_dividend_projection_writer import intent, save
from tests.test_provider_persistence import book as book

pytestmark = pytest.mark.anyio


async def test_later_candidate_audit_stales_a_projection_without_changing_portfolio_revision(
    engine: Engine, book: dict[str, Any]
) -> None:
    scope, candidate, _ = await prepared_source(engine, book)
    value, request = intent(engine, scope), uuid4()
    record = save(engine, scope, value, request)

    def current() -> Any:
        with unit_of_work(engine, write=True) as con:
            return current_service.require_current(
                con,
                scope["owner"],
                scope["event"].event_id,
                scope["account_id"],
                projection_id=record.version.id,
                projection_no=record.version.projection_no,
                as_of=date(2026, 1, 8),
            )

    assert current() == record
    before = database_snapshot(engine)
    add_candidate_revision(engine, scope["owner"], candidate)
    after = database_snapshot(engine)
    assert after["portfolios"] == before["portfolios"]
    assert save(engine, scope, value, request) == record
    with pytest.raises(writer.ProjectionWriteError, match="^DIVIDEND_PROJECTION_STALE$"):
        current()
    assert database_snapshot(engine) == after
