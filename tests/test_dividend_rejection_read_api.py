"""Protected rejection reads expose decisions, not financial authority."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
import spm_api.dividend_rejection_models as models
import spm_api.dividend_rejection_routes as routes
import spm_api.dividend_rejection_service as rejections
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import add_posted_receipt, entitlement
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_resolution_pins_api import reject
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio


def status_path(event: Any) -> str:
    return f"/api/v1/dividends/events/{event.event_id}/rejection"


def history_path(event: Any) -> str:
    return f"/api/v1/dividends/events/{event.event_id}/rejections"


def resolve(
    engine: Engine, book: dict[str, Any], rejected: rejections.RejectionRecord
) -> tuple[event_writer.EventIntent, events.ReviewedEvent]:
    successor = event_writer.EventIntent(
        event_id=rejected.event_id,
        revision_id=uuid4(),
        revision_no=rejected.revision_no + 1,
        kind="MANUAL",
        terms=book["event"].terms,
        resolve_rejection_id=rejected.id,
        citation="Synthetic successor reference",
        reason="Synthetic explicit successor decision.",
    )
    with unit_of_work(engine, write=True) as con:
        saved = event_writer.save(
            con, UUID(book["owner"]["id"]), successor, at=datetime.now(UTC), request_id=uuid4()
        )
    return successor, saved


def chain(engine: Engine, book: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    current = book["event"]
    rejections: list[Any] = []
    successors: list[Any] = []
    for index in range(3):
        rejected = reject(engine, book, current)
        rejections.append(rejected)
        if index == 2:
            break
        current, saved = resolve(engine, book, rejected)
        successors.append(saved)
    return rejections, successors


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert '"owner_id"' not in response.text and '"request_id":' not in response.text
    assert '"audit_id"' not in response.text and "private=synthetic" not in response.text


async def test_unrejected_status_does_not_imply_eligibility_or_document_verification(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A rejection history read must not contact providers")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    response = await client.get(status_path(book["event"]))
    assert response.status_code == 200, response.text
    safe(response)
    status = response.json()
    assert status["event_id"] == str(book["event"].event_id)
    assert status["revision_id"] == str(book["event"].revision_id)
    assert status["revision_no"] == 1
    assert status["state"] == "NOT_REJECTED" and status["rejection"] is None
    assert status["scope"] == "OWNER_REJECTION_STATE_ONLY"
    assert status["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert status["eligibility_scope"] == "NOT_ESTABLISHED"
    history = await client.get(history_path(book["event"]))
    assert history.status_code == 200 and history.json()["items"] == []
    safe(history)
    assert database_snapshot(engine) == before


async def test_current_rejection_is_separate_no_store_resource_with_private_url_withheld(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    event_url = f"/api/v1/dividends/events/{book['event'].event_id}"
    original = await client.get(event_url)
    rejected = reject(engine, book, book["event"])
    before = database_snapshot(engine)
    event = await client.get(event_url)
    response = await client.get(status_path(book["event"]))
    assert original.status_code == event.status_code == 200
    assert original.headers["etag"] == event.headers["etag"] == '"1"'
    assert original.json() == event.json()
    assert response.status_code == 200
    safe(response)
    status = response.json()
    assert status["state"] == "REJECTED"
    decision = status["rejection"]
    assert decision["id"] == str(rejected.id)
    assert decision["revision_id"] == str(rejected.revision_id)
    assert decision["decision"] == "REJECT_REVISION"
    assert decision["reference"] == {
        "citation": rejected.citation,
        "url": None,
        "url_withheld": True,
    }
    assert decision["scope"] == "OWNER_REVISION_REJECTION_ONLY"
    assert decision["document_verification"] == decision["eligibility_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_history_pages_preserve_resolved_rejections_and_exact_successor_links(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    decisions, successors = chain(engine, book)
    before = database_snapshot(engine)
    first = await client.get(history_path(book["event"]), params={"limit": 2})
    assert first.status_code == 200, first.text
    safe(first)
    items = first.json()["items"]
    assert [item["decision"]["id"] for item in items] == [
        str(decisions[2].id),
        str(decisions[1].id),
    ]
    assert items[0]["resolution"] is None
    assert items[1]["resolution"] == {
        "id": str(successors[1].resolution.id),
        "successor_revision_id": str(successors[1].record.revision_id),
        "successor_revision_no": 3,
    }
    second = await client.get(
        history_path(book["event"]), params={"limit": 2, "cursor": first.json()["next_cursor"]}
    )
    assert second.status_code == 200
    safe(second)
    assert [item["decision"]["id"] for item in second.json()["items"]] == [str(decisions[0].id)]
    assert second.json()["items"][0]["resolution"]["id"] == str(successors[0].resolution.id)
    assert second.json()["next_cursor"] is None
    status = await client.get(status_path(book["event"]))
    assert status.status_code == 200 and status.json()["rejection"]["id"] == str(decisions[2].id)
    assert status.json()["revision_no"] == 3
    assert database_snapshot(engine) == before


async def test_rejection_reads_require_owner_session_and_hide_foreign_events(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    rejected = reject(engine, book, book["event"])
    with unit_of_work(engine, write=True) as con:
        other = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (other, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, other, "synthetic-other-owner")
    before = database_snapshot(engine)
    client.cookies.clear()
    for path in (status_path(book["event"]), history_path(book["event"])):
        response = await client.get(path)
        assert response.status_code == 401 and str(rejected.id) not in response.text
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    for path in (status_path(book["event"]), history_path(book["event"])):
        response = await client.get(path)
        assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
        assert response.headers["cache-control"] == "no-store"
        assert str(rejected.id) not in response.text
    assert database_snapshot(engine) == before


async def test_late_duplicate_rejection_audit_fails_every_read_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    rejected = reject(engine, book, book["event"])
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_event_rejection",
            str(rejected.id),
            "DIVIDEND_EVENT_REVISION_REJECTED",
            request_id=str(uuid4()),
            after={"synthetic": "late duplicate"},
            reason="Synthetic late duplicate audit",
        )
    before = database_snapshot(engine)
    for path in (status_path(book["event"]), history_path(book["event"])):
        response = await client.get(path)
        assert response.status_code == 503
        assert response.json()["code"] == "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
        assert response.headers["cache-control"] == "no-store"
        assert "private=synthetic" not in response.text
    assert database_snapshot(engine) == before


async def test_resolving_the_current_rejection_does_not_erase_history_or_grant_eligibility(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    decisions, _ = chain(engine, book)
    successor, saved = resolve(engine, book, decisions[-1])
    before = database_snapshot(engine)
    response = await client.get(status_path(book["event"]))
    assert response.status_code == 200
    safe(response)
    state = response.json()
    assert state["revision_id"] == str(successor.revision_id)
    assert state["revision_no"] == 4 and state["state"] == "NOT_REJECTED"
    assert state["rejection"] is None
    assert state["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert state["eligibility_scope"] == state["document_verification"] == "NOT_ESTABLISHED"
    history = await client.get(history_path(book["event"]))
    assert history.status_code == 200
    items = history.json()["items"]
    assert [item["decision"]["id"] for item in items] == [str(item.id) for item in decisions[::-1]]
    assert saved.resolution is not None
    assert items[0]["resolution"]["id"] == str(saved.resolution.id)
    assert items[0]["resolution"]["successor_revision_id"] == str(successor.revision_id)
    assert all(item["resolution"] is not None for item in items)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("url", "expected", "withheld"),
    [
        (None, None, False),
        ("https://example.test/public.pdf", "https://example.test/public.pdf", False),
        ("https://example.test/private?private=synthetic", None, True),
        ("https://example.test/private#private=synthetic", None, True),
    ],
)
async def test_reference_urls_withhold_queries_and_fragments_without_losing_the_citation(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    url: str | None,
    expected: str | None,
    withheld: bool,
) -> None:
    intent = rejection_intent(book["event"], source_url=url)
    with unit_of_work(engine, write=True) as con:
        rejection_writer.save(con, UUID(book["owner"]["id"]), intent, request_id=uuid4())
    before = database_snapshot(engine)
    response = await client.get(status_path(book["event"]))
    assert response.status_code == 200
    safe(response)
    assert response.json()["rejection"]["reference"] == {
        "citation": intent.citation,
        "url": expected,
        "url_withheld": withheld,
    }
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"cursor": "private=synthetic"},
        {"cursor": base64.urlsafe_b64encode(json.dumps(["01", str(uuid4())]).encode()).decode()},
        {
            "cursor": base64.urlsafe_b64encode(
                json.dumps(["2147483648", str(uuid4())]).encode()
            ).decode()
        },
    ],
)
async def test_history_rejects_invalid_or_unbounded_queries_without_private_echo(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], params: dict[str, str]
) -> None:
    before = database_snapshot(engine)
    response = await client.get(history_path(book["event"]), params=params)
    assert response.status_code == 422 and "private=synthetic" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("query", ["%", "_", "\\\\", "Synthetic owner rejection reference"])
async def test_history_search_is_literal_and_never_searches_private_reference(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], query: str
) -> None:
    decision = reject(engine, book, book["event"])
    before = database_snapshot(engine)
    response = await client.get(history_path(book["event"]), params={"q": query})
    assert response.status_code == 200 and response.json()["items"] == []
    selected = await client.get(history_path(book["event"]), params={"q": str(decision.id)})
    assert selected.status_code == 200
    assert [item["decision"]["id"] for item in selected.json()["items"]] == [str(decision.id)]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(("converter", "history"), [("_decision", False), ("_item", True)])
async def test_conversion_failure_is_sanitized_and_never_repairs_history(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    converter: str,
    history: bool,
) -> None:
    reject(engine, book, book["event"])
    before = database_snapshot(engine)
    with patch.object(routes, converter, side_effect=ValueError("private=synthetic")):
        path = history_path(book["event"]) if history else status_path(book["event"])
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert "private=synthetic" not in response.text
    assert database_snapshot(engine) == before


async def test_response_model_rejects_mismatched_current_rejection(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    empty = (await client.get(status_path(book["event"]))).json()
    with pytest.raises(ValidationError):
        models.RejectionState.model_validate({**empty, "state": "REJECTED"})
    reject(engine, book, book["event"])
    value = (await client.get(status_path(book["event"]))).json()
    for changes in (
        {"state": "NOT_REJECTED"},
        {"event_id": str(uuid4())},
        {"revision_id": str(uuid4())},
        {"revision_no": 2},
    ):
        with pytest.raises(ValidationError):
            models.RejectionState.model_validate({**value, **changes})


async def test_one_read_snapshot_cannot_mix_a_rejection_with_its_concurrent_successor(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    rejected = reject(engine, book, book["event"])
    original = rejections.history
    advanced = False

    def interleaved(
        con: Connection, owner: UUID, event_id: UUID
    ) -> tuple[rejections.RejectionRecord, ...] | None:
        nonlocal advanced
        result = original(con, owner, event_id)
        if not advanced:
            advanced = True
            resolve(engine, book, rejected)
        return result

    with patch.object(rejections, "history", side_effect=interleaved):
        old = await client.get(status_path(book["event"]))
    assert advanced and old.status_code == 200
    assert old.json()["revision_no"] == 1 and old.json()["state"] == "REJECTED"
    assert old.json()["rejection"]["id"] == str(rejected.id)
    current = await client.get(status_path(book["event"]))
    assert current.status_code == 200
    assert current.json()["revision_no"] == 2 and current.json()["state"] == "NOT_REJECTED"
    assert current.json()["rejection"] is None


async def test_executable_contract_secures_both_separate_rejection_resources(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    schema = (await client.get("/openapi.json")).json()
    for suffix in ("rejection", "rejections"):
        path = f"/api/v1/dividends/events/{{event_id}}/{suffix}"
        assert schema["paths"][path]["get"]["security"] == [{"cookieAuth": []}]
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("history", [False, True])
async def test_missing_current_event_after_retained_history_fails_closed(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], history: bool
) -> None:
    before = database_snapshot(engine)
    with (
        patch.object(rejections, "history", return_value=()),
        patch.object(events, "get", return_value=None),
    ):
        path = history_path(book["event"]) if history else status_path(book["event"])
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["missing", "no-link", "wrong-link", "wrong-number"])
async def test_history_converter_rejects_inconsistent_successor_or_selected_row(
    engine: Engine, book: dict[str, Any], failure: str
) -> None:
    decisions, successors = chain(engine, book)
    owner = UUID(book["owner"]["id"])
    event_id = book["event"].event_id
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        current = events.get(con, owner, event_id)
        assert current is not None and successors[0].resolution is not None
        record = decisions[0]
        row = {"id": str(record.id), "revision_no": record.revision_no}
        returned = successors[0]
        if failure == "missing":
            returned = None
        elif failure == "no-link":
            returned = replace(returned, resolution=None)
        elif failure == "wrong-link":
            returned = replace(returned, resolution=events.ResolutionLink(uuid4(), uuid4()))
        else:
            row["revision_no"] = 99
        with (
            patch.object(events, "get", return_value=returned),
            pytest.raises(ValueError),
        ):
            routes._item(con, owner, current, {str(record.id): record}, cast(RowMapping, row))
    assert database_snapshot(engine) == before


async def test_rejection_reads_do_not_change_existing_receipt_cash_or_locked_fx(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        claim = entitlement(
            {"id": str(book["event"].revision_id), "currency": "USD"}, book["account"]["id"]
        )
        claim["created_at"] = stamp()
        insert(con, "dividend_entitlements", claim)
        receipt = add_posted_receipt(con, claim, fx=7_800_000_000_000)
    rejected = reject(engine, book, book["event"])
    before = database_snapshot(engine)
    state = await client.get(status_path(book["event"]))
    history = await client.get(history_path(book["event"]))
    assert state.status_code == history.status_code == 200
    assert state.json()["rejection"]["id"] == str(rejected.id)
    assert state.json()["eligibility_scope"] == "NOT_ESTABLISHED"
    assert history.json()["items"][0]["resolution"] is None
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert (
            con.exec_driver_sql(
                "SELECT fx_rate_hkd_e12 FROM dividend_receipts WHERE id=?", (receipt["id"],)
            ).scalar_one()
            == 7_800_000_000_000
        )


async def test_filtered_page_still_validates_an_older_corrupt_resolution(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    decisions, successors = chain(engine, book)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_event_revision",
            str(successors[0].record.revision_id),
            "DIVIDEND_EVENT_REJECTION_RESOLUTION",
            request_id=str(uuid4()),
            after={"synthetic": "late duplicate"},
            reason="Synthetic late resolution audit",
        )
    before = database_snapshot(engine)
    response = await client.get(history_path(book["event"]), params={"q": str(decisions[2].id)})
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("history", [False, True])
async def test_database_failure_does_not_echo_private_diagnostics(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], history: bool
) -> None:
    before = database_snapshot(engine)
    with patch.object(routes, "_owned", side_effect=SQLAlchemyError("private=synthetic")):
        path = history_path(book["event"]) if history else status_path(book["event"])
        response = await client.get(path)
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
    assert "private=synthetic" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
