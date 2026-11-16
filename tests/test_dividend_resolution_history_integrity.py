"""An inconsistent complete rejection history must fail closed."""

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_service as rejections
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_rejection_resolution import reject, resolve
from tests.test_dividend_rejection_service import case as case


@pytest.mark.parametrize(
    "failure", ["invalid_version", "missing_prior", "duplicate", "missing_successor", "wrong_link"]
)
def test_history_rejects_inconsistent_complete_scope(
    engine: Engine, case: Any, failure: str
) -> None:
    rejected = reject(engine, case)
    resolve(engine, case, rejected.id)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        execute = con.execute
        original_get = events.get

        def get(*args: Any, **kwargs: Any) -> Any:
            version = kwargs.get("version")
            if (failure == "missing_prior" and version == 1) or (
                failure == "missing_successor" and version == 2
            ):
                return None
            result = original_get(*args, **kwargs)
            if failure == "wrong_link" and version == 2 and result is not None:
                return replace(result, resolution=None)
            return result

        def rows(*args: Any, **kwargs: Any) -> Any:
            result = execute(*args, **kwargs)
            if "SELECT rejected.* FROM dividend_event_rejections" not in str(args[0]):
                return result
            values: list[Any] = list(result.mappings().all())
            assert len(values) == 1
            if failure == "invalid_version":
                values = [{**dict(values[0]), "revision_no": True}]
            elif failure == "duplicate":
                values = [values[0], values[0]]
            replacement = MagicMock()
            replacement.mappings.return_value.all.return_value = values
            return replacement

        with (
            patch.object(events, "get", side_effect=get),
            patch.object(con, "execute", side_effect=rows),
            pytest.raises(
                rejections.RejectionError, match="^DIVIDEND_REJECTION_HISTORY_UNAVAILABLE$"
            ),
        ):
            rejections.history(con, OWNER, case.event_id)
    assert database_snapshot(engine) == before
