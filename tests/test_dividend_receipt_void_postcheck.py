"""Late scope changes after receipt void audit must roll back the entire operation."""

from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine

import spm_api.dividend_receipt_void as voids
from spm_api.audit import stamp
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OWNER
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_receipt_audit import scope as scope
from tests.test_dividend_receipt_void import cancel, void_intent
from tests.test_dividend_receipt_writer import intent, save


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "current_portfolio",
        "current_data",
        "original_portfolio",
        "original_data",
        "broker",
    ],
)
def test_void_rechecks_all_portfolios_after_final_audit_verification(
    engine: Engine, scope: tuple[Any, str, dict[str, Any]], change: str
) -> None:
    record = save(engine, intent(engine, scope), uuid4())
    assert record.origin is not None
    old_portfolio = str(record.origin.portfolio_id)
    new_portfolio, new_broker = uuid4(), uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "portfolios",
            {
                "id": str(new_portfolio),
                "owner_id": str(OWNER),
                "name": str(new_portfolio),
                "created_at": stamp(),
            },
        )
        insert(
            con,
            "brokers",
            {"id": str(new_broker), "owner_id": str(OWNER), "name": str(new_broker)},
        )
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (str(new_portfolio), str(new_broker), scope[1]),
        )
    value = void_intent(engine, scope, record)
    request_id = uuid4()
    before = database_snapshot(engine)
    original = voids._verify_void
    changed = False
    statements = {
        "account": ("UPDATE accounts SET version=version+1 WHERE id=?", scope[1]),
        "current_portfolio": (
            "UPDATE portfolios SET version=version+1 WHERE id=?",
            str(new_portfolio),
        ),
        "current_data": (
            "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
            str(new_portfolio),
        ),
        "original_portfolio": ("UPDATE portfolios SET version=version+1 WHERE id=?", old_portfolio),
        "original_data": (
            "UPDATE portfolios SET data_revision=data_revision+1 WHERE id=?",
            old_portfolio,
        ),
        "broker": ("UPDATE brokers SET version=version+1 WHERE id=?", str(new_broker)),
    }

    def late(
        con: Connection,
        owner: UUID,
        submitted: voids.ReceiptVoidIntent,
        request_id: UUID,
        result: Any,
    ) -> None:
        nonlocal changed
        original(con, owner, submitted, request_id, result)
        if not changed:
            changed = True
            statement, identifier = statements[change]
            con.exec_driver_sql(statement, (identifier,))

    with (
        patch.object(voids, "_verify_void", side_effect=late),
        pytest.raises(voids.ReceiptVoidError, match="^DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED$"),
    ):
        cancel(engine, value, request_id)
    assert changed
    assert database_snapshot(engine) == before
    result = cancel(engine, value, request_id)
    assert result.receipt.status == "VOID" and result.receipt.version == 2
