"""Owner-scoped correction scope is private, complete, and never a financial grant."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api import trade_history_review, trade_service
from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.security import COOKIE, CSRF_COOKIE, issue_session
from spm_api.trade_codec import as_trade
from tests.helpers import create, database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_trade_audit_legacy_guard import synthetic_legacy_draft
from tests.test_trade_correction_scope import decision, second_auditless_revision
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_temporary_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def path(trade: UUID | str) -> str:
    return f"/api/v1/trades/{trade}/unverified-corrections/scope"


async def test_status_lists_only_root_gaps_without_grant_or_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store" and "etag" not in result.headers
    assert result.json() == {
        "trade_id": str(trade),
        "root_id": str(trade),
        "trade_version": 1,
        "trade_status": "DRAFT_SCHEDULED",
        "gaps": [
            {
                "trade_id": str(trade),
                "trade_version": 1,
                "review_revision": "0",
                "current_gap": "MISSING_CREATION_AUDIT",
                "state": "NOT_REVIEWED",
            }
        ],
        "scope": "UNVERIFIED_TRADE_CORRECTION_ONLY",
        "history_completeness": "UNVERIFIED",
    }
    assert database_snapshot(engine) == before
    decision(engine, UUID(book["owner"]["id"]), trade)
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 200
    assert result.json()["gaps"][0] == {
        "trade_id": str(trade),
        "trade_version": 1,
        "review_revision": "1",
        "current_gap": "MISSING_CREATION_AUDIT",
        "state": "ACKNOWLEDGED_UNVERIFIED",
    }
    assert "reason" not in result.text and database_snapshot(engine) == before


async def test_each_missing_physical_revision_appears_in_order(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, first)
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, first, trade_version=2)
    decision(engine, owner, current)
    before = database_snapshot(engine)
    result = await client.get(path(current))
    assert result.status_code == 200
    assert [
        (item["trade_id"], item["trade_version"], item["review_revision"], item["state"])
        for item in result.json()["gaps"]
    ] == [
        (str(first), 2, "1", "ACKNOWLEDGED_UNVERIFIED"),
        (str(current), 1, "1", "ACKNOWLEDGED_UNVERIFIED"),
    ]
    assert result.json()["root_id"] == str(first)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("withdrawn", [False, True])
async def test_late_matching_evidence_is_changed_not_a_new_scope_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], withdrawn: bool
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    if withdrawn:
        decision(engine, owner, trade, revision=1, action="WITHDRAW")
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(trade),)).mappings().one()
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json"),
        )
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 200
    item = result.json()["gaps"][0]
    assert item["current_gap"] is None
    assert item["state"] == "EVIDENCE_CHANGED"
    assert database_snapshot(engine) == before


async def test_conflicting_audit_fails_closed_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(owner),
            "trade",
            str(trade),
            "CREATE",
            request_id=str(uuid4()),
            after={"synthetic": "contradictory evidence"},
        )
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 503 and result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_verified_root_has_empty_scope_and_retired_target_is_not_current(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = (
        await create(client, "trades", payload(book, trade_date="2999-01-02", fx_rate_hkd=None))
    )["id"]
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 200 and result.json()["gaps"] == []
    assert result.json()["history_completeness"] == "UNVERIFIED"
    assert database_snapshot(engine) == before
    missing = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    second_auditless_revision(engine, missing)
    before = database_snapshot(engine)
    result = await client.get(path(missing))
    assert result.status_code == 409 and database_snapshot(engine) == before


async def test_foreign_or_signed_out_read_is_denied_without_writes(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    foreign = str(uuid4())
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners(id,password_hash,password_changed_at,created_at) "
            "VALUES (?,'synthetic','2026-01-01','2026-01-01')",
            (foreign,),
        )
        token, csrf, _ = issue_session(con, foreign, "synthetic-other-owner")
    client.cookies.clear()
    client.cookies.set(COOKIE, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 404 and result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    client.cookies.clear()
    result = await client.get(path(trade))
    assert result.status_code == 401 and result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


async def test_withdrawn_current_review_is_not_a_correction_grant(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    from tests.test_trade_unverified_correction_api import body as correction_body
    from tests.test_trade_unverified_correction_api import path as correction_path

    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    observed = await client.get(path(trade))
    assert observed.status_code == 200
    assert observed.json()["gaps"][0]["state"] == "ACKNOWLEDGED_UNVERIFIED"
    decision(engine, owner, trade, revision=1, action="WITHDRAW")
    before = database_snapshot(engine)
    current = await client.get(path(trade))
    assert current.status_code == 200
    assert current.json()["gaps"][0]["state"] == "WITHDRAWN"
    assert current.json()["gaps"][0]["review_revision"] == "2"
    assert database_snapshot(engine) == before
    stale = await client.post(
        correction_path(trade, "void"),
        json=correction_body(trade, "void", book),
        headers=version_headers(client, 1),
    )
    assert stale.status_code == 409 and stale.json()["code"] == "TRADE_REVIEW_REQUIRED"
    assert database_snapshot(engine) == before


async def test_later_retired_origin_review_does_not_validate_old_missing_creation_gap(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, first)
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, first, trade_version=2)
    with unit_of_work(engine, write=True) as con:
        row = con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(first),)).mappings().one()
        audit(
            con,
            str(owner),
            "trade",
            str(first),
            "CREATE",
            request_id=str(uuid4()),
            after=as_trade(row).model_dump(mode="json"),
        )
    decision(engine, owner, first, trade_version=2, revision=1, action="WITHDRAW")
    decision(engine, owner, first, trade_version=2, revision=2)
    before = database_snapshot(engine)
    individual = await client.get(f"/api/v1/trades/{first}/history-review")
    assert individual.status_code == 200
    assert individual.json()["state"] == "ACKNOWLEDGED_UNVERIFIED"
    assert individual.json()["current_gap"] == "MISSING_RETIRED_ORIGIN"
    scope = await client.get(path(current))
    assert scope.status_code == 200
    gap = scope.json()["gaps"][0]
    assert gap["trade_id"] == str(first) and gap["review_revision"] == "3"
    assert gap["current_gap"] == "MISSING_RETIRED_ORIGIN"
    assert gap["state"] == "EVIDENCE_CHANGED"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("count", [100, 101])
async def test_scope_never_silently_truncates_more_than_the_supported_pins(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], count: int
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = first
    with unit_of_work(engine, write=True) as con:
        for _ in range(count - 1):
            old = (
                con.exec_driver_sql("SELECT * FROM trades WHERE id=?", (str(current),))
                .mappings()
                .one()
            )
            trade_service._retire(con, old, "SUPERSEDED")
            successor, at = uuid4(), stamp()
            insert(
                con,
                "trades",
                {
                    **dict(old),
                    "id": str(successor),
                    "revision": old["revision"] + 1,
                    "supersedes_id": str(current),
                    "created_at": at,
                    "updated_at": at,
                },
            )
            current = successor
    before = database_snapshot(engine)
    result = await client.get(path(current))
    if count == 100:
        assert result.status_code == 200
        assert len(result.json()["gaps"]) == 100
        assert result.json()["gaps"][0]["trade_id"] == str(first)
        assert result.json()["gaps"][-1]["trade_id"] == str(current)
    else:
        assert result.status_code == 409
        assert result.json()["code"] == "TRADE_CORRECTION_SCOPE_TOO_LARGE"
        assert "gaps" not in result.json()
    assert database_snapshot(engine) == before


async def test_scope_read_uses_one_snapshot_even_if_review_is_concurrently_withdrawn(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_trade_unverified_correction_api import body as correction_body
    from tests.test_trade_unverified_correction_api import path as correction_path

    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    owner = UUID(book["owner"]["id"])
    decision(engine, owner, trade)
    history = trade_history_review.history
    invoked = False

    def withdraw_during_snapshot(*args: Any, **kwargs: Any) -> Any:
        nonlocal invoked
        if not invoked:
            invoked = True
            decision(engine, owner, trade, revision=1, action="WITHDRAW")
        return history(*args, **kwargs)

    monkeypatch.setattr(trade_history_review, "history", withdraw_during_snapshot)
    observed = await client.get(path(trade))
    assert invoked and observed.status_code == 200
    assert observed.json()["gaps"][0]["state"] == "ACKNOWLEDGED_UNVERIFIED"
    assert observed.json()["gaps"][0]["review_revision"] == "1"
    after = database_snapshot(engine)
    current = await client.get(path(trade))
    assert current.status_code == 200 and current.json()["gaps"][0]["state"] == "WITHDRAWN"
    rejected = await client.post(
        correction_path(trade, "void"),
        json=correction_body(trade, "void", book),
        headers=version_headers(client, 1),
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "TRADE_REVIEW_REQUIRED"
    assert database_snapshot(engine) == after


async def test_corrupt_later_revision_is_not_hidden_by_an_earlier_missing_audit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    first = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    current = second_auditless_revision(engine, first)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            book["owner"]["id"],
            "trade",
            str(current),
            "REVISE",
            request_id=str(uuid4()),
            before={"synthetic": "unrelated"},
            after={"synthetic": "unrelated"},
        )
    before = database_snapshot(engine)
    result = await client.get(path(current))
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("failure", ["sql", "value", "history", "gap", "state"])
async def test_unreadable_or_inconsistent_scope_fails_closed_without_private_details(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    import spm_api.trade_routes as routes

    trade = UUID(synthetic_legacy_draft(engine, book, unrelated_audit=False))
    history = trade_history_review.history
    calls = 0

    def failed_scope(*args: Any, **kwargs: Any) -> Any:
        if failure == "sql":
            raise SQLAlchemyError("private-untrusted")
        raise ValueError("private-untrusted")

    def missing_history(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return history(*args, **kwargs) if calls == 1 else None

    if failure in ("sql", "value"):
        monkeypatch.setattr(trade_history_review, "_correction_gaps", failed_scope)
    elif failure == "history":
        monkeypatch.setattr(trade_history_review, "history", missing_history)
    elif failure == "gap":
        monkeypatch.setattr(routes, "_current_review_gap", lambda *args: None)
    else:
        monkeypatch.setattr(routes, "_review_state", lambda *args: "NOT_REQUIRED")
    before = database_snapshot(engine)
    result = await client.get(path(trade))
    assert result.status_code == 503 and result.json()["code"] == "TRADE_REVIEW_UNAVAILABLE"
    assert result.headers["cache-control"] == "no-store"
    assert "private-untrusted" not in result.text
    assert database_snapshot(engine) == before
