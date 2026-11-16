"""Existing event endpoints do not silently publish a rejected revision."""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_service as events
import spm_api.dividend_rejection_writer as rejection_writer
from spm_api.db import unit_of_work
from tests.conftest import anyio_backend as anyio_backend
from tests.conftest import client as client
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot, version_headers
from tests.test_dividend_event_write_api import revision
from tests.test_dividend_rejection_writer import intent as rejection_intent
from tests.test_dividend_tax_api import book as book
from tests.test_dividend_verification_api import body as acceptance_body
from tests.test_dividend_withdrawal_api import withdrawal_body

pytestmark = pytest.mark.anyio


async def test_existing_http_writes_refuse_unpinned_rejection_without_changing_event_etag(
    book: dict[str, Any], client: httpx.AsyncClient, engine: Engine
) -> None:
    event = book["event"]
    path = f"/api/v1/dividends/events/{event.event_id}"
    original = await client.get(path)
    assert original.status_code == 200 and original.headers["etag"] == '"1"'
    with unit_of_work(engine, write=True) as con:
        rejection_writer.save(
            con, UUID(book["owner"]["id"]), rejection_intent(event), request_id=uuid4()
        )
    after_rejection = await client.get(path)
    assert after_rejection.status_code == 200
    assert after_rejection.headers["etag"] == original.headers["etag"]
    assert after_rejection.json() == original.json()
    before = database_snapshot(engine)
    operations = [
        ("PATCH", path, revision(book)),
        ("POST", f"/api/v1/dividends/{event.event_id}/verify", acceptance_body(event.revision_id)),
        ("POST", path + "/withdraw", withdrawal_body(book)),
    ]
    for method, url, body in operations:
        response = await client.request(method, url, json=body, headers=version_headers(client, 1))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "DIVIDEND_REJECTION_CONFIRMATION_REQUIRED"
        assert response.headers["cache-control"] == "no-store"
        assert "private-value" not in response.text
    assert database_snapshot(engine) == before


async def test_stale_rejection_pin_maps_to_a_precondition_failure() -> None:
    error = routes._write_failure(events.EventError("DIVIDEND_REJECTION_VERSION_CONFLICT"))
    assert error.status == 412 and error.code == "PRECONDITION_FAILED"
