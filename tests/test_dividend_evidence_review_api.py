"""Private HTTP reads for retained dividend evidence-review history."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as writer
import spm_api.dividend_evidence_review_history as reviews
import spm_api.dividend_evidence_review_routes as routes
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.helpers import database_snapshot
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio


def path(book: dict[str, Any]) -> str:
    return f"/api/v1/dividends/events/{book['event'].event_id}/evidence-reviews"


def successor(
    engine: Engine, book: dict[str, Any], prior: writer.EventIntent, basis: str
) -> writer.EventIntent:
    assert prior.terms is not None
    intent = writer.EventIntent.model_validate(
        {
            "event_id": prior.event_id,
            "revision_id": uuid4(),
            "revision_no": prior.revision_no + 1,
            "kind": "MANUAL",
            "terms": {**prior.terms.model_dump(), "adjustment_basis": basis},
            "citation": "Synthetic evidence-review reference",
            "source_url": "https://example.test/private?secret=synthetic",
            "reason": "Synthetic evidence-review decision, not financial authority.",
        }
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(con, UUID(book["owner"]["id"]), intent, at=now(), request_id=uuid4())
    return intent


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert "secret=synthetic" not in response.text
    assert '"owner_id"' not in response.text
    if response.is_success:
        assert '"request_id"' not in response.text
    else:
        assert response.json()["request_id"] == response.headers["x-request-id"]
    assert '"audit_id"' not in response.text


async def test_empty_read_is_private_and_does_not_prove_no_reviews(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Evidence review reads must not contact providers")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    body = response.json()
    assert body["items"] == [] and body["next_cursor"] is None
    assert body["event_id"] == str(book["event"].event_id)
    assert body["history_completeness"] == "UNVERIFIED"
    assert body["document_verification"] == body["eligibility_scope"] == "NOT_ESTABLISHED"
    assert body["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert database_snapshot(engine) == before


async def test_pages_retain_open_and_resolved_review_revisions_without_writing(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    resolution = successor(engine, book, first, "AS_DECLARED")
    current = successor(engine, book, resolution, "UNKNOWN")
    before = database_snapshot(engine)
    first_page = await client.get(path(book), params={"limit": 1})
    assert first_page.status_code == 200
    safe(first_page)
    first_body = first_page.json()
    assert first_body["revision_id"] == str(current.revision_id) and first_body["revision_no"] == 4
    assert first_body["next_cursor"] and len(first_body["items"]) == 1
    active = first_body["items"][0]
    assert active["revision_id"] == str(current.revision_id) and active["status"] == "OPEN"
    assert active["incomplete_reasons"] == ["UNRESOLVED_ADJUSTMENT"]
    assert active["acknowledgement_scope"] == "NOT_ESTABLISHED"
    second_page = await client.get(
        path(book), params={"limit": 1, "cursor": first_body["next_cursor"]}
    )
    assert second_page.status_code == 200
    safe(second_page)
    second_body = second_page.json()
    assert second_body["next_cursor"] is None and len(second_body["items"]) == 1
    resolved = second_body["items"][0]
    assert resolved["revision_id"] == str(first.revision_id) and resolved["status"] == "RESOLVED"
    assert resolved["resolved_by_revision_id"] == str(resolution.revision_id)
    assert resolved["acknowledgement_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_reads_require_an_owner_session_and_hide_foreign_events(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    with unit_of_work(engine, write=True) as con:
        other = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (other, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, other, "synthetic-other-owner")
    before = database_snapshot(engine)
    client.cookies.clear()
    response = await client.get(path(book))
    assert response.status_code == 401 and str(book["event"].event_id) not in response.text
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    response = await client.get(path(book))
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
    safe(response)
    assert str(book["event"].event_id) not in response.text
    assert database_snapshot(engine) == before


async def test_corrupt_hidden_review_fails_the_entire_page_without_repair(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
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
    response = await client.get(path(book), params={"limit": 1, "q": "does-not-match"})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_invalid_pagination_is_bounded_and_does_not_write(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    for params in ({"limit": 0}, {"limit": 101}, {"cursor": "not-a-cursor"}):
        response = await client.get(path(book), params=params)
        assert response.status_code == 422
        safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"id": uuid4()},
        {"event_id": uuid4()},
        {"revision_id": uuid4()},
        {"revision_no": 99},
        {"portfolio_id": uuid4()},
        {"account_id": uuid4()},
        {"status": "ACKNOWLEDGED"},
        {"severity": "INFO"},
        {"reason_code": "DIVIDEND_SOURCE_CONFLICT"},
        {"conflicting_fields": ("dps",)},
        {"incomplete_reasons": ("MISSING_DPS",)},
        {"created_at": now() + timedelta(days=1)},
        {"created_at": None},
        {"created_at": "private synthetic timestamp"},
        {"resolved_at": now()},
        {"resolved_at": "private synthetic timestamp"},
        {"resolved_by_revision_id": uuid4()},
        {"acknowledgement_scope": "AUTHENTICATED"},
    ],
)
async def test_substituted_review_cannot_escape_the_database_snapshot(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: dict[str, Any]
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    original = reviews.history

    def forged(*args: Any, **kwargs: Any) -> reviews.EvidenceReviewHistory | None:
        retained = original(*args, **kwargs)
        assert retained is not None and len(retained.items) == 1
        return replace(retained, items=(replace(retained.items[0], **change),))

    before = database_snapshot(engine)
    with patch.object(reviews, "history", side_effect=forged):
        response = await client.get(path(book))
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    )
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": uuid4()},
        {"revision_id": uuid4()},
        {"revision_no": 99},
        {"scope": "VERIFIED"},
        {"history_completeness": "COMPLETE"},
        {"current_use_scope": "AUTHORIZED"},
        {"eligibility_scope": "ESTABLISHED"},
        {"items": ()},
        {"items": []},
        {"items": None},
        {"items": (object(),)},
    ],
)
async def test_substituted_history_envelope_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], change: dict[str, Any]
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    original = reviews.history

    def forged(*args: Any, **kwargs: Any) -> reviews.EvidenceReviewHistory | None:
        retained = original(*args, **kwargs)
        assert retained is not None
        return replace(retained, **change)

    before = database_snapshot(engine)
    with patch.object(reviews, "history", side_effect=forged):
        response = await client.get(path(book))
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    )
    safe(response)
    assert database_snapshot(engine) == before


async def test_substituted_hidden_active_old_revision_cannot_bypass_full_scope_checks(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    successor(engine, book, first, "AS_DECLARED")
    with unit_of_work(engine) as con:
        valid = reviews.history(con, UUID(book["owner"]["id"]), first.event_id)
    assert valid is not None and len(valid.items) == 1
    forged_item = replace(
        valid.items[0], status="OPEN", resolved_at=None, resolved_by_revision_id=None
    )
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE review_items SET status='OPEN',resolved_at=NULL WHERE id=?",
            (str(forged_item.id),),
        )
    before = database_snapshot(engine)
    with patch.object(reviews, "history", return_value=replace(valid, items=(forged_item,))):
        response = await client.get(path(book), params={"q": "does-not-match"})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_substituted_resolved_review_at_forged_head_fails_before_filtering(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    successor(engine, book, first, "AS_DECLARED")
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine) as con:
        valid = reviews.history(con, owner, first.event_id)
        current = events.get(con, owner, first.event_id)
    assert valid is not None and current is not None and len(valid.items) == 1
    item = valid.items[0]
    forged = replace(valid, revision_id=item.revision_id, revision_no=item.revision_no)
    forged_current = replace(
        current,
        record=current.record.model_copy(
            update={"revision_id": item.revision_id, "revision_no": item.revision_no}
        ),
    )
    before = database_snapshot(engine)
    with (
        patch.object(reviews, "history", return_value=forged),
        patch.object(events, "get", return_value=forged_current),
    ):
        response = await client.get(path(book), params={"q": "does-not-match"})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    safe(response)
    assert database_snapshot(engine) == before


async def test_substituted_hidden_invalid_detail_is_not_accepted_by_pagination(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    with unit_of_work(engine) as con:
        valid = reviews.history(con, UUID(book["owner"]["id"]), first.event_id)
    assert valid is not None and len(valid.items) == 1
    forged_item = replace(valid.items[0], incomplete_reasons=("PRIVATE SYNTHETIC DETAIL",))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "UPDATE review_items SET detail_json=json_set(detail_json,"
            "'$.incomplete_reasons',json(?)) WHERE id=?",
            ('["PRIVATE SYNTHETIC DETAIL"]', str(forged_item.id)),
        )
    before = database_snapshot(engine)
    with patch.object(reviews, "history", return_value=replace(valid, items=(forged_item,))):
        response = await client.get(path(book), params={"q": "does-not-match"})
    assert response.status_code == 503
    assert "PRIVATE SYNTHETIC DETAIL" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


async def test_late_same_transaction_page_change_rolls_back_without_a_mixed_response(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    successor(engine, book, book["event"], "UNKNOWN")
    original = routes._checked_rows

    def changed(con: Any, retained: reviews.EvidenceReviewHistory) -> dict[str, Any]:
        records = original(con, retained)
        con.exec_driver_sql(
            "UPDATE review_items SET created_at=? WHERE event_id=?",
            ("2099-01-01T00:00:00.000000Z", str(retained.event_id)),
        )
        return records

    before = database_snapshot(engine)
    with patch.object(routes, "_checked_rows", side_effect=changed):
        response = await client.get(path(book))
    assert (
        response.status_code == 503
        and response.json()["code"] == "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE"
    )
    safe(response)
    assert database_snapshot(engine) == before


async def test_a_real_successor_during_read_cannot_mix_review_snapshots(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = successor(engine, book, book["event"], "UNKNOWN")
    original = reviews.history
    pending: list[writer.EventIntent] = []

    def raced(*args: Any, **kwargs: Any) -> reviews.EvidenceReviewHistory | None:
        value = original(*args, **kwargs)
        if not pending:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending.append(
                    pool.submit(successor, engine, book, first, "AS_DECLARED").result(timeout=30)
                )
        return value

    with patch.object(reviews, "history", side_effect=raced):
        response = await client.get(path(book))
    assert response.status_code == 200
    safe(response)
    body = response.json()
    assert body["revision_id"] == str(first.revision_id)
    assert len(body["items"]) == 1 and body["items"][0]["status"] == "OPEN"
    latest = await client.get(path(book))
    assert latest.status_code == 200
    assert latest.json()["revision_id"] == str(pending[0].revision_id)
    assert latest.json()["items"][0]["status"] == "RESOLVED"
