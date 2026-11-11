"""Protected HTTP proofs for owner event-decision reads."""

import base64
import json
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, RowMapping, text

import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_writer as writer
from spm_api.audit import audit, now, stamp
from spm_api.db import unit_of_work
from spm_api.providers.configuration import ProviderConfigurationStore
from spm_api.providers.http import ProviderHTTP
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from tests.helpers import database_snapshot
from tests.test_dividend_tax_api import book as book

pytestmark = pytest.mark.anyio
PATH = "/api/v1/dividends/events"
REASON = "Synthetic decision; not document verification or income."


def append(
    engine: Engine,
    book: dict[str, Any],
    *,
    kind: str = "MANUAL",
    number: int = 1,
    event_id: UUID | None = None,
    day: str = "2026-01-08",
    url: str | None = None,
) -> writer.EventIntent:
    terms = book["event"].terms.model_copy(update={"ex_date": day}) if kind == "MANUAL" else None
    intent = writer.EventIntent.model_validate(
        {
            "event_id": str(event_id or uuid4()),
            "revision_id": str(uuid4()),
            "revision_no": number,
            "kind": kind,
            "terms": terms,
            "citation": "Synthetic reference",
            "source_url": url,
            "reason": REASON,
        }
    )
    with unit_of_work(engine, write=True) as con:
        writer.save(con, UUID(book["owner"]["id"]), intent, at=now(), request_id=uuid4())
    return intent


def safe(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert '"owner_id"' not in response.text
    if response.is_error:
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert REASON not in response.text
    else:
        assert '"request_id"' not in response.text


async def test_current_manual_event_is_owned_audited_and_never_an_eligibility_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    before = database_snapshot(engine)
    result = await client.get(f"{PATH}/{book['event'].event_id}")
    assert result.status_code == 200, result.text
    safe(result)
    record = result.json()
    assert result.headers["etag"] == '"1"'
    assert record["id"] == str(book["event"].event_id)
    assert record["current_revision_id"] == str(book["event"].revision_id)
    assert record["version"] == 1
    assert record["dps"] == "0.90000000" and record["currency"] == "USD"
    assert record["workflow_status"] == "PROVISIONAL" and record["manual_locked"] is False
    assert record["independent_origin_groups"] is None and record["assessment"] is None
    assert record["scope"] == "OWNER_EVENT_DECISION_ONLY"
    assert record["current_use_scope"] == "REVALIDATION_REQUIRED"
    assert record["document_verification"] == record["eligibility_scope"] == "NOT_ESTABLISHED"
    assert database_snapshot(engine) == before


async def test_distinct_same_day_events_filters_and_keyset_pages_never_merge(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = book["event"]
    second = append(engine, book)
    third = append(engine, book, day="2026-01-09")
    other = append(engine, book, day="2026-02-01")
    before = database_snapshot(engine)
    all_ids: list[str] = []
    cursor = None
    while True:
        params = {"limit": "1", "from_date": "2026-01-01", "to_date": "2026-01-31"}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(PATH, params=params)
        assert response.status_code == 200, response.text
        safe(response)
        all_ids.extend(item["id"] for item in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert all_ids == [
        str(third.event_id),
        *sorted([str(first.event_id), str(second.event_id)], reverse=True),
    ]
    assert str(other.event_id) not in all_ids
    filtered = await client.get(
        PATH,
        params={
            "security_id": str(first.terms.security_id),
            "q": "0005",
            "workflow_status": "PROVISIONAL",
        },
    )
    assert filtered.status_code == 200 and len(filtered.json()["items"]) == 4
    assert database_snapshot(engine) == before


async def test_verified_and_withdrawn_revisions_keep_history_and_withhold_sensitive_urls(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    initial = book["event"]
    verified = append(
        engine,
        book,
        kind="VERIFY",
        number=2,
        event_id=initial.event_id,
        url="https://example.test/document?private=synthetic#section",
    )
    withdrawn = append(
        engine,
        book,
        kind="WITHDRAW",
        number=3,
        event_id=initial.event_id,
        url="https://example.test/public.pdf",
    )
    before = database_snapshot(engine)
    current = await client.get(f"{PATH}/{initial.event_id}")
    assert current.status_code == 200 and current.headers["etag"] == '"3"'
    assert current.json()["workflow_status"] == "WITHDRAWN"
    assert current.json()["reference"]["url"] == "https://example.test/public.pdf"
    assert current.json()["reference"]["url_withheld"] is False
    first = await client.get(f"{PATH}/{initial.event_id}/revisions", params={"limit": "2"})
    assert first.status_code == 200
    records = first.json()["items"]
    assert [record["revision_id"] for record in records] == [
        str(withdrawn.revision_id),
        str(verified.revision_id),
    ]
    assert records[1]["workflow_status"] == "VERIFIED" and records[1]["manual_locked"] is True
    assert records[1]["document_verification"] == "NOT_ESTABLISHED"
    assert (
        records[1]["reference"]["url"] is None and records[1]["reference"]["url_withheld"] is True
    )
    assert "private=synthetic" not in first.text and "#section" not in first.text
    second = await client.get(
        f"{PATH}/{initial.event_id}/revisions",
        params={"limit": "2", "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200 and second.json()["items"][0]["revision_no"] == 1
    assert second.json()["next_cursor"] is None
    safe(current)
    safe(first)
    safe(second)
    assert database_snapshot(engine) == before


async def test_foreign_owner_cannot_list_or_read_another_event(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        owner = str(uuid4())
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) VALUES (?,?,?,?)",
            (owner, "synthetic-owner", stamp(), stamp()),
        )
        token, csrf, _ = issue_session(con, owner, "synthetic-other-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    listed = await client.get(PATH)
    assert listed.status_code == 200 and listed.json()["items"] == []
    for suffix in ("", "/revisions"):
        response = await client.get(f"{PATH}/{book['event'].event_id}{suffix}")
        assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
        safe(response)
    assert database_snapshot(engine) == before


async def test_late_duplicate_audit_fails_closed_without_read_repairs(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "dividend_event_revision",
            str(book["event"].revision_id),
            "DIVIDEND_EVENT_REVISED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    before = database_snapshot(engine)
    for url in (
        PATH,
        f"{PATH}/{book['event'].event_id}",
        f"{PATH}/{book['event'].event_id}/revisions",
    ):
        response = await client.get(url)
        assert response.status_code == 503
        assert response.json()["code"] == "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
        safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "path_suffix",
    [
        "",
        "/11111111-1111-4111-8111-111111111111",
        "/11111111-1111-4111-8111-111111111111/revisions",
    ],
)
async def test_unauthenticated_reads_return_no_event_data(
    client: httpx.AsyncClient, engine: Engine, path_suffix: str
) -> None:
    before = database_snapshot(engine)
    result = await client.get(PATH + path_suffix)
    assert result.status_code == 401
    safe(result)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "params",
    [
        {"from_date": "2026-02-01", "to_date": "2026-01-01"},
        {"from_date": "2026-02-30"},
        {"workflow_status": "unexpected-private-value"},
        {"cursor": "not-a-cursor"},
        {"limit": "101"},
    ],
)
async def test_invalid_query_is_bounded_and_does_not_echo_private_input(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], params: dict[str, str]
) -> None:
    before = database_snapshot(engine)
    result = await client.get(PATH, params=params)
    assert result.status_code == 422
    assert "unexpected-private-value" not in result.text
    safe(result)
    assert database_snapshot(engine) == before


async def test_cursor_type_and_executable_contract_are_explicit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    bad = base64.urlsafe_b64encode(
        json.dumps(["9223372036854775807", str(uuid4())]).encode()
    ).decode()
    before = database_snapshot(engine)
    result = await client.get(f"{PATH}/{book['event'].event_id}/revisions", params={"cursor": bad})
    assert result.status_code == 422
    schema = (await client.get("/openapi.json")).json()
    for path in (PATH, PATH + "/{event_id}", PATH + "/{event_id}/revisions"):
        assert schema["paths"][path]["get"]["security"] == [{"cookieAuth": []}]
    assert database_snapshot(engine) == before


async def test_reading_events_never_loads_provider_configuration_or_probes_http(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Event reads must not contact a provider")

    monkeypatch.setattr(ProviderConfigurationStore, "load", forbidden)
    monkeypatch.setattr(ProviderHTTP, "__init__", forbidden)
    before = database_snapshot(engine)
    for url in (
        PATH,
        f"{PATH}/{book['event'].event_id}",
        f"{PATH}/{book['event'].event_id}/revisions",
    ):
        assert (await client.get(url)).status_code == 200
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    "corruption",
    ["identity", "joined-security", "revision", "version", "security", "status", "day"],
)
async def test_list_converter_rejects_corrupt_selection_without_touching_database(
    engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    before = database_snapshot(engine)
    owner = UUID(book["owner"]["id"])
    with unit_of_work(engine) as con:
        original = (
            con.execute(
                text("""
                SELECT e.id,e.security_id,e.version,e.workflow_status,
                       r.id AS revision_id,r.ex_date,s.id AS joined_security_id
                FROM dividend_events e JOIN dividend_event_revisions r ON r.id=e.current_revision_id
                JOIN securities s ON s.id=e.security_id WHERE e.id=:id
            """),
                {"id": str(book["event"].event_id)},
            )
            .mappings()
            .one()
        )
        row = dict(original)
        if corruption == "identity":
            row["id"] = "{" + row["id"] + "}"
        elif corruption == "joined-security":
            row["joined_security_id"] = str(uuid4())
        elif corruption == "revision":
            row["revision_id"] = str(uuid4())
        elif corruption == "version":
            row["version"] = 2
        elif corruption == "security":
            row["security_id"] = row["joined_security_id"] = str(uuid4())
        elif corruption == "status":
            row["workflow_status"] = "INCOMPLETE"
        else:
            row["ex_date"] = "2026-01-09"
        with pytest.raises(ValueError):
            routes._current_row(con, owner, cast(RowMapping, row))
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["number", "identity"])
async def test_history_converter_rejects_wrong_revision_identity(
    engine: Engine, book: dict[str, Any], corruption: str
) -> None:
    before = database_snapshot(engine)
    row: dict[str, Any] = {"id": str(book["event"].revision_id), "revision_no": 1}
    if corruption == "number":
        row["revision_no"] = "1"
    else:
        row["id"] = str(uuid4())
    with unit_of_work(engine) as con, pytest.raises(ValueError):
        routes._history_row(
            con, UUID(book["owner"]["id"]), book["event"].event_id, cast(RowMapping, row)
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("converter", "suffix"),
    [("_current_row", ""), ("_current", "/{event}"), ("_history_row", "/{event}/revisions")],
)
async def test_corrupt_conversion_yields_fixed_private_error_without_repair(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    converter: str,
    suffix: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("private=synthetic-cannot-be-disclosed")

    monkeypatch.setattr(routes, converter, fail)
    before = database_snapshot(engine)
    response = await client.get(PATH + suffix.format(event=book["event"].event_id))
    assert response.status_code == 503
    assert response.json()["code"] == "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
    assert "private=synthetic" not in response.text
    safe(response)
    assert database_snapshot(engine) == before


async def test_unmanaged_legacy_event_is_not_silently_assigned_to_owner(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    legacy = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO dividend_events(id,security_id,event_identity_key,workflow_status,"
            "version,created_at,provenance_version) VALUES (?,?,?,'INCOMPLETE',1,?,0)",
            (legacy, str(book["event"].terms.security_id), "synthetic-legacy", stamp()),
        )
    before = database_snapshot(engine)
    listed = await client.get(PATH)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [str(book["event"].event_id)]
    for suffix in ("", "/revisions"):
        response = await client.get(f"{PATH}/{legacy}{suffix}")
        assert response.status_code == 404
        safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("query", ["%", "_", "\\\\"])
async def test_search_characters_are_literal_not_wildcards(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], query: str
) -> None:
    before = database_snapshot(engine)
    result = await client.get(PATH, params={"q": query})
    assert result.status_code == 200 and result.json()["items"] == []
    assert database_snapshot(engine) == before
