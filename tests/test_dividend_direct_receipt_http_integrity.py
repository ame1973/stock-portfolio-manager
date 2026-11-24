"""Direct receipt HTTP boundaries reject forged retained outputs and cached authority."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_combined_receipt_history as combined
import spm_api.dividend_direct_receipt_write_routes as routes
import spm_api.dividend_direct_receipt_writer as writer
import spm_api.dividend_event_service as events
from spm_api.audit import audit
from spm_api.db import unit_of_work
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_direct_receipt_write_api import PATH, body
from tests.test_dividend_direct_receipt_write_api import book as book
from tests.test_dividend_receipt_write_api import safe
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "code,status",
    [
        ("DIVIDEND_DIRECT_RECEIPT_NOT_FOUND", 404),
        ("DIRECT_RECEIPT_OWNER_UNAVAILABLE", 401),
        ("INVALID_DIRECT_RECEIPT_SCOPE", 422),
        ("INVALID_DIVIDEND_RECEIPT_TERMS", 422),
        ("DIVIDEND_DIRECT_RECEIPT_DATE_INVALID", 422),
        ("DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED", 409),
        ("DIVIDEND_DIRECT_RECEIPT_ACCOUNT_ARCHIVED", 409),
        ("DIVIDEND_DIRECT_RECEIPT_EVENT_WITHDRAWN", 409),
        ("DIVIDEND_DIRECT_RECEIPT_CONFLICT", 409),
        ("DIVIDEND_REVISION_REJECTED", 409),
        ("DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE", 503),
        ("synthetic-private unexpected", 503),
    ],
)
def test_writer_errors_are_fixed_and_private(code: str, status: int) -> None:
    result = routes._failure(writer.DirectReceiptWriteError(code))
    assert result.status == status
    assert "synthetic-private" not in result.message


@pytest.mark.parametrize("value", [1.0, Decimal("NaN"), Decimal("Infinity")])
def test_nonfinite_or_binary_cash_is_not_serialized_as_decimal(value: Any) -> None:
    with pytest.raises(ValueError):
        routes._decimal(value)


@pytest.mark.parametrize("problem", ["missing", "empty", "duplicate", "wrong-type", "writer"])
async def test_cache_replay_requires_one_valid_retained_direct_receipt(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    headers = version_headers(client, 1)
    first = await client.post(PATH, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    with unit_of_work(engine) as con:
        records = combined.history(con, book["owner"], book["event"].event_id, book["account_id"])
    assert records is not None and len(records) == 1
    before = database_snapshot(engine)
    if problem == "writer":
        target = patch.object(
            writer,
            "save",
            side_effect=writer.DirectReceiptWriteError("DIVIDEND_DIRECT_RECEIPT_CONFLICT"),
        )
    else:
        values = {
            "missing": None,
            "empty": (),
            "duplicate": (*records, *records),
            "wrong-type": (SimpleNamespace(receipt=records[0].receipt),),
        }
        target = patch.object(combined, "history", return_value=values[problem])
    with target:
        response = await client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 503, response.text
    safe(response)
    assert database_snapshot(engine) == before


async def test_http_intent_metadata_mismatch_aborts_the_new_posting(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any]
) -> None:
    payload = await body(client, book)
    before = database_snapshot(engine)

    def wrong_reason(*args: Any, **kwargs: Any) -> None:
        audit(*args, reason="synthetic-private mismatched HTTP intent", **kwargs)

    with patch.object(routes, "audit", side_effect=wrong_reason):
        response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("field", ["owner_id", "event_id"])
async def test_substituted_current_event_is_not_treated_as_owned_creation_scope(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], field: str
) -> None:
    payload = await body(client, book)
    with unit_of_work(engine) as con:
        current = events.get(con, book["owner"], book["event"].event_id)
    assert current is not None
    forged = replace(current, record=current.record.model_copy(update={field: uuid4()}))
    before = database_snapshot(engine)
    with patch.object(events, "get", return_value=forged):
        response = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert response.status_code == 503
    safe(response)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("problem", ["type", "missing", "duplicate", "revision", "owner", "origin"])
async def test_public_conversion_independently_checks_the_retained_record_and_revision(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], problem: str
) -> None:
    payload = await body(client, book)
    first = await client.post(PATH, json=payload, headers=version_headers(client, 1))
    assert first.status_code == 201, first.text
    with unit_of_work(engine) as con:
        records = combined.history(con, book["owner"], book["event"].event_id, book["account_id"])
        current = events.get(con, book["owner"], book["event"].event_id)
    assert records is not None and current is not None
    record: Any = records[0]
    retained: Any = records
    if problem == "type":
        record = object()
    elif problem == "missing":
        retained = None
    elif problem == "duplicate":
        retained = (*records, *records)
    elif problem == "origin":
        record = replace(record, origin=replace(record.origin, observed_revision_no=2))
        retained = (record,)
    revision = None if problem == "revision" else current
    if problem == "owner":
        revision = replace(current, record=current.record.model_copy(update={"owner_id": uuid4()}))
    before = database_snapshot(engine)
    with (
        unit_of_work(engine) as con,
        patch.object(combined, "history", return_value=retained),
        patch.object(events, "get", return_value=revision),
        pytest.raises(writer.DirectReceiptWriteError),
    ):
        routes._public(con, book["owner"], book["event"].event_id, book["account_id"], record)
    assert database_snapshot(engine) == before
