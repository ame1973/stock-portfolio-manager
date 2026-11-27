"""Recheck retained inputs after an event head changes without granting financial use."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy import event as sqlalchemy_event

import spm_api.dividend_event_writer as events
import spm_api.dividend_fee_writer as fees
import spm_api.dividend_projection_writer as projections
import spm_api.dividend_revision_rebuild as rebuild
import spm_api.dividend_revision_rebuild_current as current
import spm_api.dividend_tax_service as tax
from spm_api.audit import audit, now
from spm_api.db import savepoint, unit_of_work
from tests.helpers import create, database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_direct_receipt_migration import TABLE, receipt_values
from tests.test_dividend_eligibility_service import split_body
from tests.test_dividend_event_writer import manual, terms
from tests.test_dividend_projection_writer import prepare, settings
from tests.test_dividend_revision_preview import proposal
from tests.test_dividend_revision_rebuild_current import scope as scope
from tests.test_transfers import graph as graph

pytestmark = pytest.mark.anyio


async def test_preserved_scope_is_read_only_and_requires_a_writer(
    engine: Engine, scope: dict[str, Any]
) -> None:
    owner, observed = scope["book"]["owner"], scope["observed"]
    before = database_snapshot(engine)
    with unit_of_work(engine, write=True) as con:
        current.require_preserved(con, owner, observed)
        with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_SCOPE$"):
            current.require_preserved(con, cast(Any, "owner"), observed)
        with pytest.raises(rebuild.RebuildError, match="^INVALID_DIVIDEND_REBUILD_SCOPE$"):
            current.require_preserved(con, owner, cast(Any, object()))
        with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
            current.require_preserved(con, uuid4(), observed)
        forged = (
            replace(observed, event=cast(Any, object())),
            replace(observed, event=replace(observed.event, record=cast(Any, object()))),
            replace(
                observed,
                event=replace(
                    observed.event,
                    record=observed.event.record.model_copy(update={"owner_id": uuid4()}),
                ),
            ),
            replace(observed, ledger=cast(Any, object())),
        )
        for item in forged:
            with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
                current.require_preserved(con, owner, item)
    with (
        unit_of_work(engine) as con,
        pytest.raises(rebuild.RebuildError, match="^WRITE_UNIT_OF_WORK_REQUIRED$"),
    ):
        current.require_preserved(con, owner, observed)
    assert database_snapshot(engine) == before


async def test_preserved_scope_fails_closed_for_a_non_sqlite_connection(
    engine: Engine, scope: dict[str, Any]
) -> None:
    fake = SimpleNamespace(
        in_transaction=lambda: True,
        get_execution_options=lambda: {"writer": True},
        connection=SimpleNamespace(driver_connection=object()),
    )
    before = database_snapshot(engine)
    with pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"):
        current.require_preserved(cast(Connection, fake), scope["book"]["owner"], scope["observed"])
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("change", ["ledger", "receipt", "projection", "security", "owner"])
async def test_preserved_scope_detects_late_changes_and_rolls_them_back(
    engine: Engine, scope: dict[str, Any], change: str
) -> None:
    owner, observed = scope["book"]["owner"], scope["observed"]
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        current.require_preserved(con, owner, observed)
        if change == "security":
            assert (
                con.exec_driver_sql(
                    "UPDATE securities SET version=version+1 WHERE id=?",
                    (str(observed.security.id),),
                ).rowcount
                == 1
            )
        elif change == "owner":
            assert (
                con.exec_driver_sql(
                    "UPDATE owners SET is_active=0 WHERE id=?", (str(owner),)
                ).rowcount
                == 1
            )
        else:
            entity, identifier = {
                "ledger": ("trade", observed.ledger.trades[0][0]),
                "receipt": (
                    "dividend_direct_receipt_http_intent",
                    observed.receipts.direct_receipts[0][0],
                ),
                "projection": (
                    "dividend_projection_http_intent",
                    observed.projections.projections[0][0],
                ),
            }[change]
            audit(
                con,
                str(owner),
                entity,
                identifier,
                "SYNTHETIC_LATE_EVIDENCE",
                request_id=str(uuid4()),
                after={"scope": "synthetic"},
            )
        current.require_preserved(con, owner, observed)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("changed_table", ["security", "broker"])
@pytest.mark.parametrize("rolled_back", [False, True])
async def test_preserved_scope_detects_changes_during_its_last_raw_read(
    engine: Engine, scope: dict[str, Any], changed_table: str, rolled_back: bool
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    fee_id = observed.projections.fee_settings[0][0]
    before = database_snapshot(engine)
    injected: list[str] = []

    def change_during_read(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if injected or "from audit_events where entity_id=" not in statement.lower():
            return
        if fee_id not in parameters:
            return
        injected.append(changed_table)
        table, identifier = (
            ("securities", observed.security.id)
            if changed_table == "security"
            else ("brokers", book["broker_id"])
        )

        def change() -> None:
            assert (
                connection.exec_driver_sql(
                    f"UPDATE {table} SET version=version+1 WHERE id=?", (str(identifier),)
                ).rowcount
                == 1
            )

        if rolled_back:
            with pytest.raises(Rollback), savepoint(connection):
                change()
                raise Rollback
        else:
            change()

    sqlalchemy_event.listen(engine, "after_cursor_execute", change_during_read)
    try:
        with (
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
            unit_of_work(engine, write=True) as con,
        ):
            current.require_preserved(con, book["owner"], observed)
            assert injected == [changed_table]
    finally:
        sqlalchemy_event.remove(engine, "after_cursor_execute", change_during_read)
    assert injected == [changed_table]
    assert database_snapshot(engine) == before


async def test_preserved_scope_rejects_an_added_owner_portfolio(
    client: httpx.AsyncClient, engine: Engine, scope: dict[str, Any]
) -> None:
    await create(client, "portfolios", {"name": "Synthetic additional owned scope"})
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_preserved(con, scope["book"]["owner"], scope["observed"])
    assert database_snapshot(engine) == before


async def test_preserved_scope_compares_scalar_types_exactly(
    engine: Engine, scope: dict[str, Any]
) -> None:
    owner, observed = scope["book"]["owner"], scope["observed"]
    identifier, values = observed.projections.projections[0]
    row: list[Any] = list(values)
    index = next(i for i, value in enumerate(row) if type(value) is int and value != 0)
    row[index] = float(row[index])
    forged = replace(
        observed,
        projections=replace(observed.projections, projections=((identifier, tuple(row)),)),
    )
    assert forged == observed
    before = database_snapshot(engine)
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
    ):
        current.require_preserved(con, owner, forged)
    assert database_snapshot(engine) == before


async def test_preserved_scope_does_not_validate_current_account_versions(
    engine: Engine, scope: dict[str, Any]
) -> None:
    class Rollback(Exception):
        pass

    book, observed = scope["book"], scope["observed"]
    before = database_snapshot(engine)
    with pytest.raises(Rollback), unit_of_work(engine, write=True) as con:
        assert (
            con.exec_driver_sql(
                "UPDATE accounts SET status='ARCHIVED',version=version+1 WHERE id=?",
                (str(book["account_id"]),),
            ).rowcount
            == 1
        )
        current.require_preserved(con, book["owner"], observed)
        raise Rollback
    assert database_snapshot(engine) == before


async def test_preserved_scope_detects_an_action_review_change(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any], scope: dict[str, Any]
) -> None:
    action = await create(
        client,
        "corporate-actions",
        split_body(
            graph,
            action_type="MERGER",
            effective_date="2026-03-01",
            status="NEEDS_REVIEW",
            ratio_numerator=None,
            ratio_denominator=None,
        ),
    )
    book = scope["book"]
    with unit_of_work(engine) as con:
        observed = rebuild.observe(
            con, book["owner"], book["event"].event_id, scope["decision"], scope["proposal"]
        )
    assert observed is not None and observed.action_reviews.reviews
    before = database_snapshot(engine)
    with (
        pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
        unit_of_work(engine, write=True) as con,
    ):
        assert (
            con.exec_driver_sql(
                "UPDATE review_items SET status='ACKNOWLEDGED' "
                "WHERE json_extract(detail_json,'$.action_root_id')=?",
                (action["root_id"],),
            ).rowcount
            == 1
        )
        current.require_preserved(con, book["owner"], observed)
    assert database_snapshot(engine) == before


async def test_preserved_scope_survives_legitimate_later_revision_rows(
    client: httpx.AsyncClient, engine: Engine, graph: dict[str, Any]
) -> None:
    book = await prepare(client, engine, graph)
    old_tax, old_fee = settings(engine, book, rate="0", fee="0")
    previous = book["event"]
    decision = manual(
        event_id=previous.event_id,
        revision_id=uuid4(),
        revision_no=previous.revision_no + 1,
        terms=terms(security_id=graph["security"]["id"], dps="1.1", currency="USD"),
    )
    proposed = proposal(graph)
    with unit_of_work(engine) as con:
        observed = rebuild.observe(con, book["owner"], previous.event_id, decision, proposed)
    assert observed is not None
    assert len(observed.projections.tax_settings) == len(observed.projections.fee_settings) == 1
    assert not observed.projections.projections
    common = {
        key: book[key]
        for key in ("account_id", "portfolio_id", "broker_id", "account_version", "broker_version")
    }
    common.update(
        event_id=decision.event_id,
        revision_id=decision.revision_id,
        revision_no=decision.revision_no,
        reason=decision.reason,
    )
    with unit_of_work(engine, write=True) as con:
        successor = events.save(con, book["owner"], decision, at=now(), request_id=uuid4())
        assert successor.record.revision_id == decision.revision_id
        new_tax = tax.save(
            con,
            book["owner"],
            tax.SettingIntent(
                **common, id=uuid4(), expected_setting_no=0, effective_from="2026-01-01", rate="0"
            ),
            request_id=uuid4(),
        )
        new_fee = fees.save(
            con,
            book["owner"],
            fees.FeeIntent(
                **common,
                id=uuid4(),
                expected_setting_no=0,
                expected_head_id=None,
                effective_from="2026-01-01",
                fee_status="ESTIMATED",
                fee="0",
                fee_currency="USD",
            ),
            request_id=uuid4(),
        )
        data_revision = con.exec_driver_sql(
            "SELECT data_revision FROM portfolios WHERE id=?", (str(book["portfolio_id"]),)
        ).scalar_one()
        value = projections.ProjectionIntent(
            **common,
            id=uuid4(),
            expected_data_revision=data_revision,
            expected_projection_no=0,
            expected_projection_id=None,
            as_of=proposed.as_of,
            eligibility_rule=proposed.eligibility_rule,
            tax_effective_on=proposed.as_of,
            expected_tax_head_no=1,
            expected_tax_head_id=new_tax.id,
            expected_tax_selected_id=new_tax.id,
            fee_effective_on=proposed.as_of,
            expected_fee_head_no=1,
            expected_fee_head_id=new_fee.id,
            expected_fee_selected_id=new_fee.id,
            fx_rate_hkd="8",
            fx_source="Synthetic independent historical FX",
            fee_rate_event=None,
            fee_rate_source=None,
        )
        created = projections.save(con, book["owner"], value, request_id=uuid4())
        assert created.version.revision_id == decision.revision_id
        current.require_preserved(con, book["owner"], observed)
        audit(
            con,
            str(book["owner"]),
            "dividend_projection_http_intent",
            str(created.version.id),
            "SYNTHETIC_SUCCESSOR_EVIDENCE",
            request_id=str(uuid4()),
            after={"scope": "successor"},
        )
        current.require_preserved(con, book["owner"], observed)
        with (
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
            savepoint(con),
        ):
            audit(
                con,
                str(book["owner"]),
                "dividend_fee_http_intent",
                str(old_fee.id),
                "SYNTHETIC_LATE_PREDECESSOR_EVIDENCE",
                request_id=str(uuid4()),
                after={"scope": "predecessor"},
            )
            current.require_preserved(con, book["owner"], observed)
        current.require_preserved(con, book["owner"], observed)
        with (
            pytest.raises(rebuild.RebuildError, match="^DIVIDEND_REBUILD_SCOPE_CHANGED$"),
            savepoint(con),
        ):
            insert(con, TABLE, receipt_values(decision, str(book["account_id"])))
            current.require_preserved(con, book["owner"], observed)
        current.require_preserved(con, book["owner"], observed)
        with pytest.raises(rebuild.RebuildError):
            current.require_current(con, book["owner"], decision, proposed, observed)
    assert old_tax.id != new_tax.id and old_fee.id != new_fee.id
