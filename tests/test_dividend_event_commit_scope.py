"""Protected event decisions must recheck security and retained intent at commit."""

from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_event_routes as routes
import spm_api.dividend_event_writer as writer
import spm_api.dividend_rejection_writer as rejections
from spm_api.audit import now, stamp
from spm_api.mutations import mutate
from tests.helpers import create, database_snapshot, version_headers, write_headers
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import entitlement
from tests.test_dividend_event_write_api import PATH, payload, revision, safe
from tests.test_dividend_manual_override_api import override_body
from tests.test_dividend_rejection_decision_api import body as rejection_body
from tests.test_dividend_rejection_decision_api import endpoint as rejection_endpoint
from tests.test_dividend_tax_api import book as book
from tests.test_dividend_verification_api import body as accept_body
from tests.test_dividend_withdrawal_api import withdrawal_body
from tests.test_transfers import trade_body

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("owner", 401, "UNAUTHORIZED"),
        ("revoked", 401, "UNAUTHORIZED"),
        ("csrf", 403, "CSRF_REJECTED"),
        ("reauth", 403, "REAUTH_REQUIRED"),
        ("successor", 412, "PRECONDITION_FAILED"),
        ("rejection", 412, "PRECONDITION_FAILED"),
        ("event_audit", 503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"),
        ("http_audit", 503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"),
    ],
)
async def test_event_revision_rechecks_after_idempotency_before_commit(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    change: str,
    status: int,
    code: str,
) -> None:
    body = revision(book, dps="1.1")
    headers = version_headers(client, 1, key=f"synthetic-event-commit-{change}")
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(change)
        if change == "owner":
            connection.exec_driver_sql(
                "UPDATE owners SET is_active=0 WHERE id=?", (book["owner"]["id"],)
            )
        elif change in {"revoked", "csrf", "reauth"}:
            column, value = {
                "revoked": ("revoked_at", stamp()),
                "csrf": ("csrf_sha256", "0" * 64),
                "reauth": ("reauthenticated_at", "2000-01-01T00:00:00.000000Z"),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE sessions SET {column}=? WHERE owner_id=?", (value, book["owner"]["id"])
            )
        elif change in {"successor", "rejection"}:
            head = connection.exec_driver_sql(
                "SELECT current_revision_id,version FROM dividend_events WHERE id=?",
                (str(book["event"].event_id),),
            ).one()
            if change == "successor":
                decision = book["event"].model_copy(
                    update={
                        "revision_id": uuid4(),
                        "revision_no": head.version + 1,
                        "reason": "Synthetic competing same-transaction decision.",
                    }
                )
                writer.save(
                    connection, UUID(book["owner"]["id"]), decision, at=now(), request_id=uuid4()
                )
            else:
                rejections.save(
                    connection,
                    UUID(book["owner"]["id"]),
                    rejections.RejectionIntent(
                        id=uuid4(),
                        event_id=book["event"].event_id,
                        revision_id=UUID(head.current_revision_id),
                        revision_no=head.version,
                        citation="Synthetic competing rejection reference",
                        source_url=None,
                        reason="Synthetic competing same-transaction rejection.",
                    ),
                    request_id=uuid4(),
                )
        else:
            action = (
                "DIVIDEND_EVENT_REVISED"
                if change == "event_audit"
                else "DIVIDEND_EVENT_HTTP_INTENT"
            )
            inserted = connection.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,a.owner_id,a.entity_type,a.entity_id,a.action,a.before_sha256,"
                "a.after_sha256,a.reason,a.request_id,a.created_at FROM audit_events a "
                "JOIN dividend_events e ON e.current_revision_id=a.entity_id "
                "WHERE e.id=? AND a.entity_type='dividend_event_revision' AND a.action=?",
                (str(uuid4()), str(book["event"].event_id), action),
            )
            assert inserted.rowcount == 1

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", inject)
    try:
        result = await client.patch(f"{PATH}/{book['event'].event_id}", json=body, headers=headers)
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", inject)
    assert injected == [change], (result.status_code, result.json().get("code"))
    assert (result.status_code, result.json().get("code")) == (status, code)
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.patch(f"{PATH}/{book['event'].event_id}", json=body, headers=headers)
    assert retry.status_code == 200
    after = database_snapshot(engine)
    replay = await client.patch(f"{PATH}/{book['event'].event_id}", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("owner", 401, "UNAUTHORIZED"),
        ("revoked", 401, "UNAUTHORIZED"),
        ("csrf", 403, "CSRF_REJECTED"),
        ("reauth", 403, "REAUTH_REQUIRED"),
        ("successor", 412, "PRECONDITION_FAILED"),
        ("rejection_audit", 503, "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"),
        ("http_audit", 503, "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"),
    ],
)
async def test_rejection_rechecks_after_idempotency_before_commit(
    client: httpx.AsyncClient,
    engine: Engine,
    book: dict[str, Any],
    change: str,
    status: int,
    code: str,
) -> None:
    event = book["event"]
    body = rejection_body(event.revision_id)
    path = rejection_endpoint(event.event_id)
    headers = version_headers(client, 1, key=f"synthetic-rejection-commit-{change}")
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(change)
        if change == "owner":
            connection.exec_driver_sql(
                "UPDATE owners SET is_active=0 WHERE id=?", (book["owner"]["id"],)
            )
        elif change in {"revoked", "csrf", "reauth"}:
            column, value = {
                "revoked": ("revoked_at", stamp()),
                "csrf": ("csrf_sha256", "0" * 64),
                "reauth": ("reauthenticated_at", "2000-01-01T00:00:00.000000Z"),
            }[change]
            connection.exec_driver_sql(
                f"UPDATE sessions SET {column}=? WHERE owner_id=?", (value, book["owner"]["id"])
            )
        elif change == "successor":
            rejection_id = connection.exec_driver_sql(
                "SELECT id FROM dividend_event_rejections WHERE revision_id=?",
                (str(event.revision_id),),
            ).scalar_one()
            decision = event.model_copy(
                update={
                    "revision_id": uuid4(),
                    "revision_no": event.revision_no + 1,
                    "resolve_rejection_id": UUID(rejection_id),
                    "reason": "Synthetic competing rejection resolution.",
                }
            )
            writer.save(
                connection, UUID(book["owner"]["id"]), decision, at=now(), request_id=uuid4()
            )
        else:
            entity = (
                "dividend_event_rejection"
                if change == "rejection_audit"
                else "dividend_rejection_http_intent"
            )
            inserted = connection.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,a.owner_id,a.entity_type,a.entity_id,a.action,a.before_sha256,"
                "a.after_sha256,a.reason,a.request_id,a.created_at FROM audit_events a "
                "JOIN dividend_event_rejections r ON r.id=a.entity_id "
                "WHERE r.event_id=? AND a.entity_type=?",
                (str(uuid4()), str(event.event_id), entity),
            )
            assert inserted.rowcount == 1

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", inject)
    try:
        result = await client.post(path, json=body, headers=headers)
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", inject)
    assert injected == [change], (result.status_code, result.json().get("code"))
    assert (result.status_code, result.json().get("code")) == (status, code)
    assert result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    retry = await client.post(path, json=body, headers=headers)
    assert retry.status_code == 200
    after = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["create", "override", "accept", "withdraw"])
@pytest.mark.parametrize("change", ["revoked", "http_audit"])
async def test_all_other_event_decisions_recheck_after_idempotency(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str, change: str
) -> None:
    event = book["event"]
    if operation == "create":
        path, body = PATH, payload(book)
        headers = write_headers(client, key=f"synthetic-event-{operation}-{change}")
    else:
        path = (
            f"{PATH}/{event.event_id}/withdraw"
            if operation == "withdraw"
            else rejection_endpoint(event.event_id)
        )
        body = (
            withdrawal_body(book)
            if operation == "withdraw"
            else accept_body(event.revision_id)
            if operation == "accept"
            else override_body(book)
        )
        headers = version_headers(client, 1, key=f"synthetic-event-{operation}-{change}")
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(change)
        if change == "revoked":
            connection.exec_driver_sql(
                "UPDATE sessions SET revoked_at=? WHERE owner_id=?",
                (stamp(), book["owner"]["id"]),
            )
        else:
            inserted = connection.exec_driver_sql(
                "INSERT INTO audit_events(id,owner_id,entity_type,entity_id,action,before_sha256,"
                "after_sha256,reason,request_id,created_at) "
                "SELECT ?,owner_id,entity_type,entity_id,action,before_sha256,after_sha256,reason,"
                "request_id,created_at FROM audit_events "
                "WHERE entity_type='dividend_event_revision' "
                "AND action='DIVIDEND_EVENT_HTTP_INTENT' "
                "ORDER BY rowid DESC LIMIT 1",
                (str(uuid4()),),
            )
            assert inserted.rowcount == 1

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", inject)
    try:
        result = await client.post(path, json=body, headers=headers)
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", inject)
    assert injected == [change], (result.status_code, result.json().get("code"))
    expected = (
        (401, "UNAUTHORIZED")
        if change == "revoked"
        else (503, "DIVIDEND_EVENT_HISTORY_UNAVAILABLE")
    )
    assert (result.status_code, result.json().get("code")) == expected
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.post(path, json=body, headers=headers)
    assert retry.status_code == (201 if operation == "create" else 200), retry.text
    after = database_snapshot(engine)
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == retry.status_code and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["revision", "rejection"])
@pytest.mark.parametrize(
    "change", ["portfolio", "data_revision", "marker_time", "marker_resolved", "new_portfolio"]
)
async def test_late_portfolio_and_marker_changes_roll_back_protected_decisions(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str, change: str
) -> None:
    graph = {
        "portfolio": book["portfolio"],
        "source": book["account"],
        "security": {"id": str(book["event"].terms.security_id)},
    }
    await create(client, "trades", trade_body(graph, currency="HKD", fx_rate_hkd="1"))
    destination = await create(client, "portfolios", {"name": "Synthetic new affected portfolio"})
    destination_account = await create(
        client,
        "accounts",
        {
            "portfolio_id": destination["id"],
            "broker_id": book["broker"]["id"],
            "name": "Synthetic new affected account",
            "default_currency": "USD",
        },
    )
    event = book["event"]
    path = (
        f"{PATH}/{event.event_id}"
        if operation == "revision"
        else rejection_endpoint(event.event_id)
    )
    body = (
        revision(book, dps="1.1") if operation == "revision" else rejection_body(event.revision_id)
    )
    headers = version_headers(client, 1, key=f"synthetic-event-effects-{operation}-{change}")
    before = database_snapshot(engine)
    injected: list[str] = []

    def inject(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" not in " ".join(statement.lower().split()):
            return
        assert not injected
        injected.append(change)
        if change in {"portfolio", "data_revision"}:
            column = "version" if change == "portfolio" else "data_revision"
            connection.exec_driver_sql(
                f"UPDATE portfolios SET {column}={column}+1 WHERE id=?", (book["portfolio"]["id"],)
            )
        elif change == "new_portfolio":
            connection.exec_driver_sql(
                "INSERT INTO positions(portfolio_id,account_id,security_id,projection_version,"
                "shares_e8,remaining_cost_hkd_micro,realized_hkd_micro,calculated_at_utc) "
                "VALUES (?,?,?,0,0,0,0,?)",
                (destination["id"], destination_account["id"], graph["security"]["id"], stamp()),
            )
        else:
            source = connection.exec_driver_sql(
                "SELECT current_revision_id FROM dividend_events WHERE id=?"
                if operation == "revision"
                else "SELECT id FROM dividend_event_rejections WHERE event_id=?",
                (str(event.event_id),),
            ).scalar_one()
            column, value = (
                ("created_at", "2000-01-01T00:00:00.000000Z")
                if change == "marker_time"
                else ("resolved_at", stamp())
            )
            updated = connection.exec_driver_sql(
                f"UPDATE projection_invalidations SET {column}=? WHERE source_id=?", (value, source)
            )
            assert updated.rowcount == 1

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", inject)
    try:
        result = await client.request(
            "PATCH" if operation == "revision" else "POST", path, json=body, headers=headers
        )
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", inject)
    assert injected == [change], (result.status_code, result.json().get("code"))
    code = (
        "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
        if operation == "revision"
        else "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
    )
    assert (result.status_code, result.json().get("code")) == (503, code)
    assert result.headers["cache-control"] == "no-store"
    assert database_snapshot(engine) == before
    retry = await client.request(
        "PATCH" if operation == "revision" else "POST", path, json=body, headers=headers
    )
    assert retry.status_code == 200, retry.text
    after = database_snapshot(engine)
    replay = await client.request(
        "PATCH" if operation == "revision" else "POST", path, json=body, headers=headers
    )
    assert replay.status_code == 200 and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["revision", "rejection"])
@pytest.mark.parametrize("change", ["writer", "response", "request"])
async def test_substituted_decision_or_request_cannot_commit(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str, change: str
) -> None:
    event = book["event"]
    path = (
        f"{PATH}/{event.event_id}"
        if operation == "revision"
        else rejection_endpoint(event.event_id)
    )
    body = (
        revision(book, dps="1.1") if operation == "revision" else rejection_body(event.revision_id)
    )
    headers = version_headers(
        client, 1, key=f"synthetic-decision-substitution-{operation}-{change}"
    )
    before = database_snapshot(engine)
    injected: list[str] = []

    if change == "writer":
        module = writer if operation == "revision" else rejections
        original_save = module.save

        def substitute(*args: Any, **kwargs: Any) -> Any:
            saved = original_save(*args, **kwargs)
            injected.append(change)
            if operation == "revision":
                return replace(
                    saved,
                    record=saved.record.model_copy(update={"reason": "Substituted decision."}),
                )
            return saved.model_copy(update={"request_id": uuid4()})

        target = patch.object(module, "save", side_effect=substitute)
    elif change == "response":
        original_current = routes._current

        def substitute_response(*args: Any, **kwargs: Any) -> Any:
            result = original_current(*args, **kwargs)
            if not injected:
                injected.append(change)
                return result.model_copy(update={"reason": "Substituted response."})
            return result

        target = patch.object(routes, "_current", side_effect=substitute_response)
    else:
        original_mutate = mutate

        def substitute_request(*args: Any, **kwargs: Any) -> Any:
            request = args[0]
            validate = kwargs["validate_commit"]

            def check(con: Any, auth: Any, result: Any) -> None:
                original_id = request.state.request_id
                request.state.request_id = str(uuid4())
                injected.append(change)
                try:
                    validate(con, auth, result)
                finally:
                    request.state.request_id = original_id

            kwargs["validate_commit"] = check
            return original_mutate(*args, **kwargs)

        target = patch.object(routes, "mutate", side_effect=substitute_request)

    method = "PATCH" if operation == "revision" else "POST"
    with target:
        result = await client.request(method, path, json=body, headers=headers)
    assert injected == [change]
    code = (
        "DIVIDEND_EVENT_HISTORY_UNAVAILABLE"
        if operation == "revision"
        else "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE"
    )
    assert (result.status_code, result.json().get("code")) == (503, code)
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.request(method, path, json=body, headers=headers)
    assert retry.status_code == 200, retry.text
    after = database_snapshot(engine)
    replay = await client.request(method, path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == retry.json()
    assert database_snapshot(engine) == after


@pytest.mark.parametrize("operation", ["revision", "rejection"])
async def test_old_revision_entitlement_inserted_after_idempotency_cannot_escape_fence(
    client: httpx.AsyncClient, engine: Engine, book: dict[str, Any], operation: str
) -> None:
    event = book["event"]
    if operation == "rejection":
        successor = await client.patch(
            f"{PATH}/{event.event_id}", json=revision(book), headers=version_headers(client, 1)
        )
        assert successor.status_code == 200, successor.text
        path = rejection_endpoint(event.event_id)
        body = rejection_body(UUID(successor.json()["current_revision_id"]))
        version = 2
    else:
        path = f"{PATH}/{event.event_id}"
        body = revision(book, dps="1.1")
        version = 1
    headers = version_headers(client, version, key=f"synthetic-late-entitlement-{operation}")
    claim = entitlement({"id": str(event.revision_id), "currency": "USD"}, book["account"]["id"])
    claim["created_at"] = stamp()
    before = database_snapshot(engine)
    injected: list[str] = []

    def add_old_entitlement(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "insert into idempotency_keys" in " ".join(statement.lower().split()):
            assert not injected
            insert(connection, "dividend_entitlements", claim)
            injected.append(operation)

    transport: Any = client._transport
    app_engine = transport.app.state.engine
    sqlalchemy_event.listen(app_engine, "after_cursor_execute", add_old_entitlement)
    method = "PATCH" if operation == "revision" else "POST"
    try:
        result = await client.request(method, path, json=body, headers=headers)
    finally:
        sqlalchemy_event.remove(app_engine, "after_cursor_execute", add_old_entitlement)
    assert injected == [operation], (result.status_code, result.json().get("code"))
    assert (result.status_code, result.json().get("code")) == (
        409,
        "DIVIDEND_EVENT_PROJECTION_REQUIRED",
    )
    safe(result)
    assert database_snapshot(engine) == before
    retry = await client.request(method, path, json=body, headers=headers)
    assert retry.status_code == 200, retry.text
    after = database_snapshot(engine)
    replay = await client.request(method, path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == retry.json()
    assert database_snapshot(engine) == after
