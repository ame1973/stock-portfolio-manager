"""Corrupted resolution linkages cannot be adopted or silently repaired."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_resolution as resolutions
from spm_api.audit import audit
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import next_intent
from tests.test_dividend_rejection_resolution import reject, resolve
from tests.test_dividend_rejection_service import case as case


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", str(uuid4())),
        ("owner_id", str(uuid4())),
        ("event_id", str(uuid4())),
        ("rejection_id", str(uuid4())),
        ("rejected_revision_id", str(uuid4())),
        ("successor_revision_id", str(uuid4())),
        ("successor_revision_no", 3),
        ("reason", "A different synthetic resolution reason"),
        ("request_id", str(uuid4())),
        ("audit_id", str(uuid4())),
        ("created_at", "2030-01-01T00:00:00.000000Z"),
    ],
)
def test_corrupt_linkage_fails_the_full_event_history_closed(
    engine: Engine, case: Any, field: str, value: Any
) -> None:
    rejected = reject(engine, case)
    resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        execute = con.execute

        def corrupt(*args: Any, **kwargs: Any) -> Any:
            result = execute(*args, **kwargs)
            if "SELECT * FROM dividend_rejection_resolutions" in str(args[0]):
                rows = result.mappings().all()
                assert len(rows) == 1
                values = dict(rows[0])
                values[field] = value
                replacement = MagicMock()
                replacement.mappings.return_value.all.return_value = [values]
                return replacement
            return result

        with (
            patch.object(con, "execute", side_effect=corrupt),
            pytest.raises(events.EventError, match="^DIVIDEND_EVENT_HISTORY_UNAVAILABLE$"),
        ):
            events.get(con, OWNER, case.event_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("missing", ["link", "rejection", "rejection_audit", "successor_audit"])
def test_missing_resolution_component_is_not_interpreted_as_a_valid_link(
    engine: Engine, case: Any, missing: str
) -> None:
    rejected = reject(engine, case)
    resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        previous = events.get(con, OWNER, case.event_id, version=1)
        successor = events.get(con, OWNER, case.event_id, version=2)
        assert previous is not None and successor is not None
        marker = (
            con.exec_driver_sql(
                "SELECT * FROM audit_events WHERE entity_type='dividend_event_revision' "
                "AND entity_id=? AND action='DIVIDEND_EVENT_REJECTION_RESOLUTION'",
                (str(successor.record.revision_id),),
            )
            .mappings()
            .one()
        )
        execute = con.execute
        revision_audit_reads = 0

        def omit(*args: Any, **kwargs: Any) -> Any:
            nonlocal revision_audit_reads
            query = str(args[0])
            if "SELECT created_at FROM audit_events" in query and "DIVIDEND_EVENT_REVISED" in query:
                revision_audit_reads += 1
            target = (
                (missing == "link" and "SELECT * FROM dividend_rejection_resolutions" in query)
                or (missing == "rejection" and "SELECT * FROM dividend_event_rejections" in query)
                or (
                    missing == "rejection_audit"
                    and "SELECT created_at FROM audit_events WHERE id=" in query
                )
                or (
                    missing == "successor_audit"
                    and revision_audit_reads == 2
                    and "SELECT created_at FROM audit_events" in query
                )
            )
            if target:
                result = MagicMock()
                result.mappings.return_value.all.return_value = []
                result.all.return_value = []
                return result
            return execute(*args, **kwargs)

        with patch.object(con, "execute", side_effect=omit), pytest.raises(ValueError):
            resolutions.checked_link(
                con,
                previous,
                successor.record,
                manual_override=successor.manual_override,
                marker=marker,
            )
    assert database_snapshot(engine) == before


def test_resolution_append_rejects_an_unrelated_rejection_pin_without_writes(
    engine: Engine, case: Any
) -> None:
    rejected = reject(engine, case)
    resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        previous = events.get(con, OWNER, case.event_id, version=1)
        successor = events.get(con, OWNER, case.event_id, version=2)
        assert previous is not None and successor is not None
        with pytest.raises(ValueError, match="Invalid rejection resolution scope"):
            resolutions.append(
                con,
                previous,
                rejected,
                successor.record,
                manual_override=False,
                link=events.ResolutionLink(uuid4(), uuid4()),
            )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_duplicate_new_resolution_marker_rolls_back_all_staged_rows(
    engine: Engine, case: Any, duplicate: bool
) -> None:
    rejected = reject(engine, case)
    before = database_snapshot(engine)

    def incorrect_audit(*args: Any, **kwargs: Any) -> None:
        if duplicate:
            audit(*args, **kwargs)
            audit(*args, **kwargs)

    with (
        unit_of_work(engine, write=True) as con,
        patch.object(resolutions, "audit", side_effect=incorrect_audit),
        pytest.raises(ValueError, match="Missing or duplicate rejection-resolution audit"),
    ):
        event_writer.save(
            con,
            OWNER,
            next_intent(case, "VERIFY", resolve_rejection_id=rejected.id),
            at=datetime.now(UTC),
            request_id=uuid4(),
        )
    assert database_snapshot(engine) == before
