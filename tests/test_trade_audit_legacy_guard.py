"""Non-destructive synthetic legacy-trade review tests on a verified temporary database."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.audit import audit, stamp
from spm_api.db import unit_of_work
from spm_api.ledger_service import allocate_sequence
from tests.helpers import database_snapshot, version_headers
from tests.provider_evidence_helpers import insert
from tests.test_trades import book as book
from tests.test_trades import payload

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def exclusive_test_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


def synthetic_legacy_draft(engine: Engine, book: dict[str, Any], *, unrelated_audit: bool) -> str:
    """Insert a new synthetic draft; never alter existing audits, triggers or records."""
    identifier, at = str(uuid4()), stamp()
    with unit_of_work(engine, write=True) as con:
        sequence = allocate_sequence(con, "TRADE", identifier)
        insert(
            con,
            "trades",
            {
                "id": identifier,
                "root_id": identifier,
                "created_sequence": sequence,
                "portfolio_id": book["portfolio"]["id"],
                "account_id": book["account"]["id"],
                "security_id": book["security"]["id"],
                "side": "BUY",
                "trade_date": "2999-01-02",
                "quantity_e8": 1000000000,
                "price_e8": 10000000000,
                "currency": "USD",
                "gross_micro": 1000000000,
                "settlement_micro": 1000000000,
                "status": "DRAFT_SCHEDULED",
                "created_at": at,
                "updated_at": at,
            },
        )
        if unrelated_audit:
            audit(
                con,
                book["owner"]["id"],
                "trade",
                identifier,
                "CREATE",
                request_id=str(uuid4()),
                after={"synthetic": "deliberately unrelated assertion"},
            )
    return identifier


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("unrelated_audit", "status", "code"),
    [(False, 409, "NEEDS_REVIEW"), (True, 503, "TRADE_AUDIT_UNAVAILABLE")],
)
async def test_legacy_draft_without_matching_original_requires_review(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    method: str,
    unrelated_audit: bool,
    status: int,
    code: str,
) -> None:
    identifier = synthetic_legacy_draft(engine, book, unrelated_audit=unrelated_audit)
    before = database_snapshot(engine)
    body = (
        payload(
            book,
            trade_date="2999-01-02",
            fx_rate_hkd=None,
            quantity="11",
            reason="Review this synthetic legacy trade",
        )
        if method == "PATCH"
        else {"reason": "Review this synthetic legacy trade"}
    )
    response = await client.request(
        method,
        f"/api/v1/trades/{identifier}",
        json=body,
        headers=version_headers(client, 1),
    )
    assert response.status_code == status and response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert identifier not in response.text
    assert database_snapshot(engine) == before
    with unit_of_work(engine) as con:
        assert con.exec_driver_sql("SELECT count(*) FROM trade_audit_origins").scalar_one() == 0
