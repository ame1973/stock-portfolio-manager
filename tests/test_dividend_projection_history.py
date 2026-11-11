"""Exercise retained-projection reads with isolated, explicitly synthetic evidence."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

import spm_api.dividend_eligibility_service as eligibility
import spm_api.dividend_event_service as events
import spm_api.dividend_event_writer as event_writer
from spm_api.audit import audit, canonical, digest, stamp
from spm_api.db import unit_of_work
from spm_api.dividend_event_writer import EventIntent
from spm_api.dividend_projection_arithmetic import _AMOUNT_FIELDS
from spm_api.dividend_projection_history import ProjectionHistoryError, _money, _payload, history
from tests.conftest import engine as engine
from tests.conftest import settings as settings
from tests.helpers import database_snapshot
from tests.provider_evidence_helpers import insert
from tests.test_dividend_event_migration import OTHER, OWNER, REASON
from tests.test_dividend_event_migration import source as source
from tests.test_dividend_event_writer import AT, manual, next_intent, owned_account, submit, terms
from tests.test_dividend_projection_migration import TABLE, snapshot_values

D = Decimal


@pytest.fixture(autouse=True)
def private_database(engine: Engine, tmp_path: Path) -> None:
    assert engine.url.database is not None
    assert Path(engine.url.database).resolve().is_relative_to(tmp_path.resolve())


@pytest.fixture
def scenario(engine: Engine, source: dict[str, Any]) -> tuple[EventIntent, str]:
    _, account = owned_account(engine)
    intent = manual(terms=terms(dps="1"))
    submit(engine, intent)
    return intent, account


def append(
    con: Connection,
    intent: EventIntent,
    account: str,
    *,
    previous: dict[str, Any] | None = None,
    false_audit: bool = False,
    **changes: Any,
) -> dict[str, Any]:
    scope = con.exec_driver_sql(
        "SELECT p.id AS portfolio_id,b.id AS broker_id,a.version AS account_version,"
        "b.version AS broker_version,p.data_revision FROM accounts a "
        "JOIN portfolios p ON p.id=a.portfolio_id JOIN brokers b ON b.id=a.broker_id "
        "WHERE a.id=?",
        (account,),
    ).one()
    created = datetime.now(UTC) + timedelta(seconds=1)
    row = {
        "id": str(uuid4()),
        "owner_id": str(OWNER),
        "event_id": str(intent.event_id),
        "revision_id": str(intent.revision_id),
        "portfolio_id": scope.portfolio_id,
        "account_id": account,
        "broker_id": scope.broker_id,
        "account_version": scope.account_version,
        "broker_version": scope.broker_version,
        "projection_no": 1 if previous is None else previous["projection_no"] + 1,
        "previous_id": None if previous is None else previous["id"],
        "state": "EXPECTED",
        "as_of": "2026-01-08",
        "eligibility_rule": "ORDINARY_PRE_EX_DATE",
        "portfolio_data_revision": scope.data_revision,
        "retired_trade_scope": "NO_RETIRED_TRADES",
        "creation_audit_scope": "NO_RECORDED_CREATION_GAP_REVIEW",
        "history_completeness": "UNVERIFIED",
        "eligible_shares_e8": 100_000_000,
        "gross_micro": 1_000_000,
        "tax_micro": 0,
        "fee_micro": 1,
        "expected_net_micro": 1_000_000,
        "rounding_delta_micro": 1,
        "currency": "USD",
        "fx_rate_hkd_e12": 7_800_000_000_000,
        "fx_source": "MANUAL:synthetic",
        "expected_hkd_micro": 7_799_996,
        "tax_rate_snapshot_ppm": 0,
        "fee_original_micro": 1,
        "fee_original_currency": "HKD",
        "fee_rate_event_e12": 500_000_000_000,
        "fee_rate_source": "MANUAL:synthetic",
        "quality": "OK",
        "request_id": str(uuid4()),
        "created_at": stamp(created),
        **changes,
    }
    with patch("spm_api.audit.now", return_value=created):
        audit(
            con,
            str(OWNER),
            "dividend_projection",
            row["id"],
            "DIVIDEND_PROJECTION_CREATED",
            request_id=row["request_id"],
            after={"synthetic": "different"}
            if false_audit
            else _payload(row, intent.revision_no, REASON),
            reason=REASON,
        )
    row["audit_id"] = con.exec_driver_sql(
        "SELECT id FROM audit_events WHERE entity_type='dividend_projection' AND entity_id=?",
        (row["id"],),
    ).scalar_one()
    insert(con, TABLE, row)
    return row


def read(engine: Engine, intent: EventIntent, account: str, owner: UUID = OWNER) -> Any:
    with unit_of_work(engine) as con:
        return history(con, owner, intent.event_id, UUID(account))


def read_with_simulated_projection(
    engine: Engine, intent: EventIntent, account: str, changes: dict[str, Any]
) -> Any:
    """Simulate corrupt query results without changing stored rows or guards."""
    with unit_of_work(engine) as con:
        execute = con.execute
        altered: dict[str, Any] = {}

        def simulate(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = execute(statement, *args, **kwargs)
            sql = str(statement)
            if "FROM dividend_projection_versions v" in sql:
                altered.update(dict(result.mappings().one()))
                altered.update(changes)
                replacement = MagicMock()
                replacement.mappings.return_value.__iter__.return_value = iter((altered,))
                return replacement
            if "entity_type='dividend_projection'" in sql:
                audit_row = dict(result.mappings().one())
                audit_row["created_at"] = altered["created_at"]
                audit_row["after_sha256"] = digest(
                    canonical(_payload(altered, altered["revision_no"], audit_row["reason"]))
                )
                replacement = MagicMock()
                replacement.mappings.return_value.all.return_value = [audit_row]
                return replacement
            return result

        with patch.object(con, "execute", side_effect=simulate):
            return history(con, OWNER, intent.event_id, UUID(account))


def test_one_projection_recomputes_cross_currency_components_without_granting_use(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        raw = append(con, intent, account)
    before = database_snapshot(engine)
    rows = read(engine, intent, account)
    assert rows is not None and len(rows) == 1
    result = rows[0]
    assert result.version.id == UUID(raw["id"])
    assert result.version.accrual is not None
    assert result.version.accrual.fee_local == D("0.000001")
    assert result.version.accrual.net_local == D("1.000000")
    assert result.version.accrual.booked_hkd == D("7.799996")
    assert result.version.accrual.rounding_delta_local == D("0.000001")
    assert result.eligible_shares == D("1") and result.tax_rate == D("0")
    assert result.fee_original == D("0.000001") and result.fee_rate_event == D("0.5")
    assert result.history_completeness == "UNVERIFIED"
    assert result.scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    assert REASON not in repr(result) and account not in repr(result)
    assert database_snapshot(engine) == before


def test_matching_audit_and_amount_do_not_prove_any_recorded_shares(
    engine: Engine, source: dict[str, Any]
) -> None:
    _, account = owned_account(engine)
    security = uuid4()
    with unit_of_work(engine, write=True) as con:
        insert(
            con,
            "securities",
            {
                "id": str(security),
                "exchange_code": "NASDAQ",
                "display_ticker": "CLEAN",
                "name": "Synthetic security without actions",
                "quote_currency": "USD",
                "created_at": "2026-01-01T00:00:00.000000Z",
            },
        )
    intent = manual(terms=terms(security_id=security, dps="1"))
    submit(engine, intent)
    with unit_of_work(engine, write=True) as con:
        row = append(con, intent, account)
    with unit_of_work(engine) as con:
        retained = history(con, OWNER, intent.event_id, UUID(account))
        book = eligibility.get(
            con,
            OWNER,
            UUID(row["portfolio_id"]),
            security,
            ex_date=date(2026, 1, 8),
            as_of=date(2026, 1, 8),
            rule="ORDINARY_PRE_EX_DATE",
        )
    assert retained is not None and retained[0].eligible_shares == D("1")
    assert book is not None and book.accounts[0].recorded_quantity == "0.00000000"
    assert retained[0].history_completeness == "UNVERIFIED"
    assert retained[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"


def test_zero_foreign_fee_and_missing_hkd_fx_remain_distinct(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(
            con,
            intent,
            account,
            fee_original_micro=0,
            fee_micro=0,
            fee_rate_event_e12=None,
            fee_rate_source=None,
            rounding_delta_micro=0,
            fx_rate_hkd_e12=None,
            fx_source=None,
            expected_hkd_micro=None,
            quality="MISSING_FX",
        )
    rows = read(engine, intent, account)
    assert rows is not None and rows[0].version.accrual is not None
    assert rows[0].fee_currency == "HKD" and rows[0].fee_rate_event is None
    assert rows[0].version.accrual.net_local == D("1")
    assert rows[0].version.accrual.booked_hkd is None
    assert rows[0].version.accrual.quality == "MISSING_FX"


@pytest.mark.parametrize("quality", ["OK", "NEEDS_REVIEW"])
def test_negative_raw_net_cannot_hide_behind_a_rounded_zero(
    engine: Engine, scenario: tuple[EventIntent, str], quality: str
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(
            con,
            intent,
            account,
            eligible_shares_e8=100,
            gross_micro=1,
            fee_rate_event_e12=1_000_001_000_000,
            expected_net_micro=0,
            rounding_delta_micro=0,
            expected_hkd_micro=0,
            quality=quality,
        )
    if quality == "OK":
        with pytest.raises(
            ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
        ):
            read(engine, intent, account)
    else:
        rows = read(engine, intent, account)
        assert rows is not None and rows[0].version.accrual is not None
        assert rows[0].version.accrual.net_local == 0
        assert rows[0].version.accrual.quality == "NEEDS_REVIEW"


def test_reads_the_complete_chain_without_summing_old_versions(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        first = append(con, intent, account)
        second = append(
            con,
            intent,
            account,
            previous=first,
            eligible_shares_e8=200_000_000,
            gross_micro=2_000_000,
            expected_net_micro=2_000_000,
            expected_hkd_micro=15_599_996,
        )
    rows = read(engine, intent, account)
    assert rows is not None and [str(row.version.id) for row in rows] == [first["id"], second["id"]]
    assert rows[1].version.previous_id == rows[0].version.id
    assert rows[1].version.accrual is not None
    assert rows[1].version.accrual.net_local == D("2.000000")


@pytest.mark.parametrize("state", ["NO_POSITION", "NEEDS_REVIEW", "CANCELLED"])
def test_non_amount_states_are_preserved_not_reclassified_as_zero(
    engine: Engine, scenario: tuple[EventIntent, str], state: str
) -> None:
    intent, account = scenario
    changes: dict[str, Any] = {name: None for name in _AMOUNT_FIELDS}
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account, state=state, eligibility_rule="REQUIRES_REVIEW", **changes)
    rows = read(engine, intent, account)
    assert rows is not None and rows[0].version.state == state
    assert rows[0].version.accrual is None and rows[0].eligible_shares is None


def test_projection_fingerprint_covers_every_retained_field_and_hides_the_reason(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        row = append(con, intent, account)
    payload = _payload(row, 1, REASON)
    assert set(payload) == (set(row) - {"audit_id"}) | {
        "revision_no",
        "reason_sha256",
        "scope",
    }
    assert payload["reason_sha256"] == digest(canonical(REASON))
    assert "reason" not in payload and REASON not in canonical(payload)
    original = digest(canonical(payload))
    for name in set(row) - {"audit_id"}:
        changed = {**row, name: "synthetic changed value"}
        assert digest(canonical(_payload(changed, 1, REASON))) != original
    assert digest(canonical(_payload(row, 2, REASON))) != original
    assert digest(canonical(_payload(row, 1, "A different synthetic reason"))) != original


@pytest.mark.parametrize(
    ("revision", "reason"),
    [
        (0, REASON),
        (True, REASON),
        (2_147_483_648, REASON),
        (1, "short"),
        (1, " synthetic reason with outer whitespace "),
        (1, "synthetic reason\x00with null"),
    ],
)
def test_projection_fingerprint_rejects_invalid_revision_and_reason(
    revision: Any, reason: str
) -> None:
    with pytest.raises(ValueError):
        _payload({}, revision, reason)


def test_sql_shaped_audit_is_not_a_verified_projection_fingerprint(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        values = snapshot_values(
            con,
            {"id": str(intent.event_id)},
            {"id": str(intent.revision_id), "currency": "USD"},
            account,
        )
        insert(con, TABLE, values)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, intent, account)


@pytest.mark.parametrize(
    "changes",
    [
        {"gross_micro": 1_000_001},
        {"expected_net_micro": 999_999, "rounding_delta_micro": 0, "expected_hkd_micro": 7_799_992},
        {"expected_hkd_micro": 7_799_995},
        {"quality": "MISSING_QUOTE", "fee_original_micro": 2},
    ],
)
def test_matching_fingerprint_cannot_substitute_for_recomputed_amounts(
    engine: Engine, scenario: tuple[EventIntent, str], changes: dict[str, Any]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account, **changes)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, intent, account)


def test_a_false_audit_on_any_later_version_fails_the_whole_chain(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        first = append(con, intent, account)
        append(con, intent, account, previous=first, false_audit=True)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, intent, account)


def test_later_duplicate_audit_cannot_make_an_old_snapshot_trusted(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        row = append(con, intent, account)
    assert read(engine, intent, account)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_projection",
            row["id"],
            "DIVIDEND_PROJECTION_CREATED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, intent, account)


def test_later_duplicate_event_audit_invalidates_historical_source(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account)
    with unit_of_work(engine, write=True) as con:
        audit(
            con,
            str(OWNER),
            "dividend_event_revision",
            str(intent.revision_id),
            "DIVIDEND_EVENT_REVISED",
            request_id=str(uuid4()),
            after={"synthetic": "duplicate"},
            reason=REASON,
        )
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read(engine, intent, account)


def test_missing_and_foreign_scope_are_not_economic_zero(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    _, foreign = owned_account(engine, OTHER)
    assert read(engine, intent, account) == ()
    assert read(engine, intent, account, OTHER) is None
    assert read(engine, intent, foreign) is None
    assert read(engine, intent, str(uuid4())) is None
    with unit_of_work(engine) as con:
        assert history(con, OWNER, uuid4(), UUID(account)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": str(OTHER)},
        {"event_id": str(uuid4())},
        {"account_id": str(uuid4())},
        {"retained_portfolio_owner": str(OTHER)},
        {"retained_broker_owner": str(OTHER)},
        {"projection_no": 2},
        {"previous_id": str(uuid4())},
        {"revision_no": 0},
        {"revision_no": 99},
        {"revision_id": str(uuid4())},
        {"created_at": "2000-01-01T00:00:00.000000Z"},
        {"as_of": "20260108"},
        {"as_of": "not-a-date"},
        {"as_of": None},
        {"account_version": 0},
        {"broker_version": -1},
        {"portfolio_data_revision": -1},
        {"eligibility_rule": "SYNTHETIC_INVALID"},
        {"retired_trade_scope": "SYNTHETIC_INVALID"},
        {"creation_audit_scope": "SYNTHETIC_INVALID"},
        {"history_completeness": "VERIFIED"},
    ],
)
def test_simulated_corrupt_read_fails_closed_without_touching_database(
    engine: Engine, scenario: tuple[EventIntent, str], changes: dict[str, Any]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account)
    before = database_snapshot(engine)
    with pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"):
        read_with_simulated_projection(engine, intent, account, changes)
    assert database_snapshot(engine) == before


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_projection_integer_decoder_rejects_non_integer_values(value: Any) -> None:
    with pytest.raises(ValueError):
        _money(value, 6)


def test_simulated_cross_owner_broker_cannot_make_history_owned(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account)
    before = database_snapshot(engine)
    with unit_of_work(engine) as con:
        execute = con.execute

        def simulate(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = execute(statement, *args, **kwargs)
            if "p.owner_id AS portfolio_owner,b.owner_id AS broker_owner" in str(statement):
                replacement = MagicMock()
                replacement.first.return_value = SimpleNamespace(
                    portfolio_owner=str(OWNER), broker_owner=str(OTHER)
                )
                return replacement
            return result

        with (
            patch.object(con, "execute", side_effect=simulate),
            pytest.raises(
                ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"
            ),
        ):
            history(con, OWNER, intent.event_id, UUID(account))
    assert database_snapshot(engine) == before


def test_simulated_older_revision_remains_historical_not_current_use(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    first, account = scenario
    second = next_intent(first, "VERIFY")
    submit(engine, second, at=AT + timedelta(seconds=1))
    with unit_of_work(engine, write=True) as con:
        append(con, second, account)
    before = database_snapshot(engine)
    rows = read_with_simulated_projection(
        engine, second, account, {"revision_id": str(first.revision_id), "revision_no": 1}
    )
    assert rows is not None and rows[0].version.revision_id == first.revision_id
    assert rows[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    assert database_snapshot(engine) == before


def test_inactive_owner_can_read_history_but_cannot_use_it_as_a_current_grant(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account)
        con.exec_driver_sql("UPDATE owners SET is_active=0 WHERE id=?", (str(OWNER),))
    before = database_snapshot(engine)
    rows = read(engine, intent, account)
    assert rows is not None and rows[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"
    with (
        unit_of_work(engine, write=True) as con,
        pytest.raises(events.EventError, match="^DIVIDEND_EVENT_OWNER_UNAVAILABLE$"),
    ):
        event_writer.require_current_source(
            con, OWNER, intent.event_id, revision_id=intent.revision_id, version=1
        )
    assert database_snapshot(engine) == before


@pytest.mark.parametrize(
    ("target", "failure"),
    [
        ("events.get", SQLAlchemyError("private synthetic source failure")),
        ("checked_accrual", ValueError("private synthetic amount failure")),
    ],
)
def test_private_reader_failures_are_fixed_errors_without_writes(
    engine: Engine, scenario: tuple[EventIntent, str], target: str, failure: Exception
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        append(con, intent, account)
    before = database_snapshot(engine)
    with (
        patch(f"spm_api.dividend_projection_history.{target}", side_effect=failure),
        pytest.raises(ProjectionHistoryError, match="^DIVIDEND_PROJECTION_HISTORY_UNAVAILABLE$"),
    ):
        read(engine, intent, account)
    assert database_snapshot(engine) == before


def test_retained_portfolio_and_broker_are_not_rebound_after_account_move(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    _, destination = owned_account(engine)
    with unit_of_work(engine, write=True) as con:
        row = append(con, intent, account)
        target = con.exec_driver_sql(
            "SELECT portfolio_id,broker_id FROM accounts WHERE id=?", (destination,)
        ).one()
        con.exec_driver_sql(
            "UPDATE accounts SET portfolio_id=?,broker_id=?,version=version+1 WHERE id=?",
            (target.portfolio_id, target.broker_id, account),
        )
    rows = read(engine, intent, account)
    assert rows is not None
    assert str(rows[0].portfolio_id) == row["portfolio_id"] != target.portfolio_id
    assert str(rows[0].broker_id) == row["broker_id"] != target.broker_id
    assert rows[0].account_version == row["account_version"]


def test_changed_broker_configuration_does_not_rewrite_history_or_grant_current_use(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        row = append(con, intent, account)
        con.exec_driver_sql(
            "UPDATE brokers SET dividend_fee_micro=1000000,version=version+1 WHERE id=?",
            (row["broker_id"],),
        )
    rows = read(engine, intent, account)
    assert rows is not None and rows[0].broker_version == row["broker_version"]
    assert rows[0].scope == "UNVERIFIED_PROJECTION_HISTORY_ONLY"


def test_later_event_revision_is_independently_verified_before_snapshot(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    first, account = scenario
    second = next_intent(first, "VERIFY")
    submit(engine, second, at=AT + timedelta(seconds=1))
    with unit_of_work(engine, write=True) as con:
        raw = append(con, second, account)
    rows = read(engine, second, account)
    assert rows is not None and rows[0].version.revision_no == 2
    assert str(rows[0].version.revision_id) == raw["revision_id"]


def test_read_requires_explicit_transaction_and_typed_scope(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with (
        engine.connect() as con,
        pytest.raises(ProjectionHistoryError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"),
    ):
        history(con, OWNER, intent.event_id, UUID(account))
    with unit_of_work(engine) as con:
        for bad in (str(OWNER), None, True):
            with pytest.raises(ProjectionHistoryError, match="^INVALID_DIVIDEND_PROJECTION_SCOPE$"):
                history(con, bad, intent.event_id, UUID(account))  # type: ignore[arg-type]


def test_a_read_transaction_retains_its_snapshot_across_a_new_committed_projection(
    engine: Engine, scenario: tuple[EventIntent, str]
) -> None:
    intent, account = scenario
    with unit_of_work(engine, write=True) as con:
        first = append(con, intent, account)
    with unit_of_work(engine) as reader:
        before = history(reader, OWNER, intent.event_id, UUID(account))
        with unit_of_work(engine, write=True) as writer:
            append(writer, intent, account, previous=first)
        during = history(reader, OWNER, intent.event_id, UUID(account))
    after = read(engine, intent, account)
    assert before is not None and during == before
    assert after is not None and len(after) == 2
