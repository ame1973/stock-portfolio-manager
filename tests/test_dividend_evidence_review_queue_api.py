"""Owner-scoped cross-event evidence-review queue without financial authority."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_writer as writer
import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_routes as routes
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.errors import ApiError
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.test_dividend_evidence_review_api import safe, successor
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
QUEUE = "/api/v1/dividends/evidence-reviews"


def create_incomplete(engine: Engine, book: dict[str, Any], owner: UUID) -> writer.EventIntent:
    original = book["event"]
    assert original.terms is not None
    intent = writer.EventIntent.model_validate(
        {
            "event_id": uuid4(),
            "revision_id": uuid4(),
            "revision_no": 1,
            "kind": "MANUAL",
            "terms": {**original.terms.model_dump(), "adjustment_basis": "UNKNOWN"},
            "citation": "Synthetic queue evidence reference",
            "reason": "Synthetic incomplete event for review queue.",
        }
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(con, owner, intent, at=now(), request_id=uuid4())
    return intent


async def test_cross_event_queue_filters_and_pages_retained_reviews(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    resolution = successor(engine, book, first, "AS_DECLARED")
    current = successor(engine, book, resolution, "UNKNOWN")
    other = create_incomplete(engine, book, UUID(book["owner"]["id"]))
    before = database_snapshot(engine)

    all_rows = await client.get(QUEUE, params={"limit": 100})
    assert all_rows.status_code == 200
    safe(all_rows)
    body = all_rows.json()
    assert len(body["items"]) == 3 and body["next_cursor"] is None
    assert body["scope"] == "RETAINED_EVIDENCE_REVIEW_QUEUE_ONLY"
    assert body["history_completeness"] == "UNVERIFIED"
    assert body["eligibility_scope"] == body["document_verification"] == "NOT_ESTABLISHED"
    assert {row["event_id"] for row in body["items"]} == {
        str(current.event_id),
        str(other.event_id),
    }
    assert all(row["severity"] == "WARNING" for row in body["items"])
    assert all(row["acknowledgement_scope"] == "NOT_ESTABLISHED" for row in body["items"])
    old = next(row for row in body["items"] if row["revision_id"] == str(first.revision_id))
    assert old["status"] == "RESOLVED"
    assert (old["current_revision_id"], old["current_revision_no"]) == (
        str(current.revision_id),
        current.revision_no,
    )
    assert old["resolved_by_revision_id"] == str(resolution.revision_id)

    first_page = await client.get(
        QUEUE, params={"status": "OPEN", "severity": "WARNING", "limit": 1}
    )
    assert first_page.status_code == 200
    safe(first_page)
    page = first_page.json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    second_page = await client.get(
        QUEUE,
        params={"status": "OPEN", "severity": "WARNING", "limit": 1, "cursor": page["next_cursor"]},
    )
    assert second_page.status_code == 200
    safe(second_page)
    second = second_page.json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert {page["items"][0]["event_id"], second["items"][0]["event_id"]} == {
        str(current.event_id),
        str(other.event_id),
    }
    by_event = await client.get(QUEUE, params={"q": str(current.event_id)})
    assert by_event.status_code == 200
    assert len(by_event.json()["items"]) == 2
    by_reason = await client.get(QUEUE, params={"q": "DIVIDEND_INCOMPLETE"})
    assert by_reason.status_code == 200 and len(by_reason.json()["items"]) == 3
    literal_percent = await client.get(QUEUE, params={"q": "%"})
    assert literal_percent.status_code == 200 and literal_percent.json()["items"] == []
    literal_slash = await client.get(QUEUE, params={"q": "\\"})
    assert literal_slash.status_code == 200 and literal_slash.json()["items"] == []
    empty = await client.get(QUEUE, params={"severity": "INFO"})
    assert empty.status_code == 200 and empty.json()["items"] == []
    assert empty.json()["history_completeness"] == "UNVERIFIED"
    assert database_snapshot(engine) == before


async def test_queue_hides_other_owners_and_requires_a_session(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    own = successor(engine, book, book["event"], "UNKNOWN")
    with unit_of_work(engine, write=True) as con:
        other = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (other, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, other, "synthetic-other-owner")
    foreign = create_incomplete(engine, book, UUID(other))
    before = database_snapshot(engine)
    own_response = await client.get(QUEUE)
    assert own_response.status_code == 200
    assert {row["event_id"] for row in own_response.json()["items"]} == {str(own.event_id)}
    assert str(foreign.event_id) not in own_response.text
    client.cookies.clear()
    anonymous = await client.get(QUEUE)
    assert anonymous.status_code == 401 and str(own.event_id) not in anonymous.text
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    foreign_response = await client.get(QUEUE)
    assert foreign_response.status_code == 200
    safe(foreign_response)
    assert {row["event_id"] for row in foreign_response.json()["items"]} == {str(foreign.event_id)}
    assert str(own.event_id) not in foreign_response.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "params", [{"status": "DISMISSED"}, {"severity": "URGENT"}, {"limit": 0}, {"cursor": "bad"}]
)
async def test_invalid_queue_filters_fail_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], params: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(QUEUE, params=params)
    assert response.status_code == 422
    safe(response)
    assert database_snapshot(engine) == before


async def test_selected_event_corruption_fails_but_an_empty_filter_is_not_completeness(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event = successor(engine, book, book["event"], "UNKNOWN")
    with unit_of_work(engine, write=True) as con:
        review_id = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=?", (str(event.event_id),)
        ).scalar_one()
        audit(
            con, book["owner"]["id"], "review_item", review_id, "DIVIDEND_EVIDENCE_REVIEW_CREATED"
        )
    before = database_snapshot(engine)
    unavailable = await client.get(QUEUE, params={"severity": "WARNING"})
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(unavailable)
    empty = await client.get(QUEUE, params={"severity": "INFO"})
    assert empty.status_code == 200 and empty.json()["items"] == []
    assert empty.json()["history_completeness"] == "UNVERIFIED"
    assert database_snapshot(engine) == before


async def test_queue_rejects_a_late_same_connection_write_and_rolls_it_back(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    original = routes._checked_rows

    def changed(con: Any, retained: reviews.EvidenceReviewHistory) -> dict[str, Any]:
        records = original(con, retained)
        assert (
            con.exec_driver_sql(
                "UPDATE review_items SET status='ACKNOWLEDGED' WHERE event_id=?",
                (str(retained.event_id),),
            ).rowcount
            == 1
        )
        return records

    before = database_snapshot(engine)
    with patch.object(routes, "_checked_rows", side_effect=changed):
        response = await client.get(QUEUE, params={"status": "OPEN"})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_selected_event_checks_corrupt_history_hidden_by_filter(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    resolved = successor(engine, book, first, "AS_DECLARED")
    successor(engine, book, resolved, "UNKNOWN")
    with unit_of_work(engine, write=True) as con:
        review_id = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=? AND status='RESOLVED'",
            (str(first.event_id),),
        ).scalar_one()
        audit(
            con, book["owner"]["id"], "review_item", review_id, "DIVIDEND_EVIDENCE_REVIEW_CREATED"
        )
    before = database_snapshot(engine)
    response = await client.get(QUEUE, params={"status": "OPEN"})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_unreturned_next_row_is_validated_before_offering_a_cursor(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    older = successor(engine, book, book["event"], "UNKNOWN")
    create_incomplete(engine, book, UUID(book["owner"]["id"]))
    with unit_of_work(engine, write=True) as con:
        review_id = con.exec_driver_sql(
            "SELECT id FROM review_items WHERE event_id=?", (str(older.event_id),)
        ).scalar_one()
        audit(
            con, book["owner"]["id"], "review_item", review_id, "DIVIDEND_EVIDENCE_REVIEW_CREATED"
        )
    before = database_snapshot(engine)
    response = await client.get(QUEUE, params={"limit": 1})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "ACKNOWLEDGED"),
        ("severity", "INFO"),
        ("created_at", datetime(2099, 1, 1, tzinfo=UTC)),
    ],
)
async def test_queue_selected_row_must_match_its_verified_history(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str, value: object
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    original = routes._checked_rows

    def forged(con: Any, retained: reviews.EvidenceReviewHistory) -> dict[str, Any]:
        records = original(con, retained)
        return {key: item.model_copy(update={field: value}) for key, item in records.items()}

    before = database_snapshot(engine)
    with patch.object(routes, "_checked_rows", side_effect=forged):
        response = await client.get(QUEUE)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("code", [404, 403])
async def test_queue_handles_unavailable_selected_events_without_leaking_rows(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], code: int
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    before = database_snapshot(engine)
    with patch.object(routes, "_owned", side_effect=ApiError(code, "SYNTHETIC", "Unavailable")):
        response = await client.get(QUEUE)
    assert response.status_code == (503 if code == 404 else 403)
    safe(response)
    assert str(book["event"].event_id) not in response.text
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "connection", [None, SimpleNamespace(), SimpleNamespace(driver_connection=None)]
)
async def test_queue_rejects_an_unsupported_read_connection(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], connection: object
) -> None:
    @contextmanager
    def unsupported(*args: Any, **kwargs: Any) -> Iterator[Any]:
        yield SimpleNamespace(connection=connection)

    before = database_snapshot(engine)
    with patch.object(routes, "unit_of_work", unsupported):
        response = await client.get(QUEUE)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_queue_keeps_one_wal_snapshot_when_an_event_changes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    original = routes._owned
    later: list[writer.EventIntent] = []

    def raced(*args: Any, **kwargs: Any) -> reviews.EvidenceReviewHistory:
        if not later:
            with ThreadPoolExecutor(max_workers=1) as pool:
                later.append(pool.submit(successor, engine, book, first, "AS_DECLARED").result(30))
        return original(*args, **kwargs)

    with patch.object(routes, "_owned", side_effect=raced):
        response = await client.get(QUEUE)
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["status"] == "OPEN" and row["current_revision_id"] == str(first.revision_id)
    after = await client.get(QUEUE)
    assert after.status_code == 200
    row = after.json()["items"][0]
    assert row["status"] == "RESOLVED" and row["current_revision_id"] == str(later[0].revision_id)
