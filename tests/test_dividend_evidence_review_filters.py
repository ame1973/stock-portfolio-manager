"""Filter retained evidence reviews without hiding corruption or granting authority."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_routes as routes
from spm_api.audit import audit
from spm_api.db import savepoint, unit_of_work
from tests.helpers import database_snapshot
from tests.test_dividend_evidence_review_api import path, safe, successor
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio


async def test_filters_separate_recorded_status_and_severity_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    resolved = successor(engine, book, first, "AS_DECLARED")
    current = successor(engine, book, resolved, "UNKNOWN")
    with unit_of_work(engine, write=True) as con:
        changed = con.exec_driver_sql(
            "UPDATE review_items SET status='ACKNOWLEDGED' WHERE event_id=? AND status='OPEN'",
            (str(current.event_id),),
        )
        assert changed.rowcount == 1
    before = database_snapshot(engine)

    for params, expected in (
        ({}, {"ACKNOWLEDGED", "RESOLVED"}),
        ({"status": "OPEN"}, set()),
        ({"status": "ACKNOWLEDGED"}, {"ACKNOWLEDGED"}),
        ({"status": "RESOLVED"}, {"RESOLVED"}),
        ({"severity": "WARNING"}, {"ACKNOWLEDGED", "RESOLVED"}),
        ({"severity": "INFO"}, set()),
        ({"severity": "CRITICAL"}, set()),
        ({"status": "RESOLVED", "severity": "WARNING"}, {"RESOLVED"}),
        ({"status": "ACKNOWLEDGED", "severity": "CRITICAL"}, set()),
    ):
        response = await client.get(path(book), params=params)
        assert response.status_code == 200
        safe(response)
        body = response.json()
        assert {item["status"] for item in body["items"]} == expected
        assert body["history_completeness"] == "UNVERIFIED"
        assert body["eligibility_scope"] == body["document_verification"] == "NOT_ESTABLISHED"
        assert all(item["acknowledgement_scope"] == "NOT_ESTABLISHED" for item in body["items"])
    assert database_snapshot(engine) == before


async def test_filtered_pages_remain_bounded_and_ordered(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    next_event = successor(engine, book, first, "AS_DECLARED")
    second = successor(engine, book, next_event, "UNKNOWN")
    next_event = successor(engine, book, second, "AS_DECLARED")
    successor(engine, book, next_event, "UNKNOWN")
    before = database_snapshot(engine)
    first_page = await client.get(path(book), params={"status": "RESOLVED", "limit": 1})
    assert first_page.status_code == 200
    safe(first_page)
    first_body = first_page.json()
    assert len(first_body["items"]) == 1 and first_body["next_cursor"]
    assert first_body["items"][0]["revision_id"] == str(second.revision_id)
    next_page = await client.get(
        path(book),
        params={"status": "RESOLVED", "limit": 1, "cursor": first_body["next_cursor"]},
    )
    assert next_page.status_code == 200
    safe(next_page)
    next_body = next_page.json()
    assert len(next_body["items"]) == 1 and next_body["next_cursor"] is None
    assert next_body["items"][0]["revision_id"] == str(first.revision_id)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "params",
    [
        {"status": "DISMISSED"},
        {"status": "open"},
        {"status": ""},
        {"severity": "URGENT"},
        {"severity": ""},
    ],
)
async def test_invalid_filters_fail_before_reading_private_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], params: dict[str, str]
) -> None:
    before = database_snapshot(engine)
    with patch.object(reviews, "history", side_effect=AssertionError("Unexpected history read")):
        response = await client.get(path(book), params=params)
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("params", [{"status": "OPEN"}, {"severity": "INFO"}])
async def test_filtered_out_corrupt_history_still_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], params: dict[str, str]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    successor(engine, book, first, "AS_DECLARED")
    with unit_of_work(engine, write=True) as con:
        review_id = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=?", (str(first.event_id),)
        ).scalar_one()
        audit(
            con, book["owner"]["id"], "review_item", review_id, "DIVIDEND_EVIDENCE_REVIEW_CREATED"
        )
    before = database_snapshot(engine)
    response = await client.get(path(book), params=params)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(("field", "value"), [("status", "ACKNOWLEDGED"), ("severity", "INFO")])
async def test_selected_row_must_match_the_checked_record(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: str
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    original = routes._checked_rows

    def forged(con: Any, retained: reviews.EvidenceReviewHistory) -> dict[str, Any]:
        records = original(con, retained)
        return {key: item.model_copy(update={field: value}) for key, item in records.items()}

    before = database_snapshot(engine)
    with patch.object(routes, "_checked_rows", side_effect=forged):
        response = await client.get(path(book), params={"status": "OPEN", "severity": "WARNING"})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "connection", [None, SimpleNamespace(), SimpleNamespace(driver_connection=None)]
)
async def test_unsupported_review_connection_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], connection: object
) -> None:
    @contextmanager
    def unsupported(*args: Any, **kwargs: Any) -> Iterator[Any]:
        yield SimpleNamespace(connection=connection)

    before = database_snapshot(engine)
    with patch.object(routes, "unit_of_work", unsupported):
        response = await client.get(path(book))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("rolled_back", [False, True])
@pytest.mark.parametrize(
    ("column", "value", "params"),
    [
        ("status", "ACKNOWLEDGED", {"status": "OPEN"}),
        ("severity", "INFO", {"severity": "WARNING"}),
    ],
)
async def test_filter_cannot_hide_a_late_same_connection_change(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    column: str,
    value: str,
    params: dict[str, str],
    rolled_back: bool,
) -> None:
    class Rollback(Exception):
        pass

    successor(engine, book, book["event"], "UNKNOWN")
    original = routes._checked_rows

    def changed(con: Any, retained: reviews.EvidenceReviewHistory) -> dict[str, Any]:
        records = original(con, retained)
        assert column in {"status", "severity"}

        def update() -> None:
            result = con.exec_driver_sql(
                f"UPDATE review_items SET {column}=? WHERE event_id=?",
                (value, str(retained.event_id)),
            )
            assert result.rowcount == 1

        if rolled_back:
            with pytest.raises(Rollback), savepoint(con):
                update()
                raise Rollback
        else:
            update()
        return records

    before = database_snapshot(engine)
    with patch.object(routes, "_checked_rows", side_effect=changed):
        response = await client.get(path(book), params=params)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before
