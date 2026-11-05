"""Explicit opening assertion corrections, exact historical reversal and honest coverage reset."""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine

from spm_api.audit import stamp
from spm_api.cash_models import OpeningCashRecord
from spm_api.db import unit_of_work
from spm_api.ledger_service import post_cash, reverse_cash
from tests.helpers import (
    create,
    initialize,
    version_headers,
    write_headers,
)
from tests.helpers import (
    database_snapshot as snapshot,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def account(client: httpx.AsyncClient, engine: Engine) -> dict[str, Any]:
    await initialize(client, engine)
    portfolio = await create(client, "portfolios", {"name": "Synthetic opening portfolio"})
    broker = await create(client, "brokers", {"name": "Synthetic opening broker"})
    created = await create(
        client,
        "accounts",
        {
            "portfolio_id": portfolio["id"],
            "broker_id": broker["id"],
            "name": "Synthetic opening account",
            "default_currency": "USD",
            "opening_cash": "1000",
            "opening_cash_date": "2026-01-10",
            "opening_fx_rate_hkd": "7.7",
            "cash_completeness": "COMPLETE",
        },
    )
    return {**created, "broker": broker, "portfolio": portfolio}


def body(**changes: Any) -> dict[str, Any]:
    return {
        "opening_cash": "1500",
        "opening_cash_date": "2026-01-15",
        "opening_fx_rate_hkd": "7.9",
        "reason": "Synthetic reviewed opening assertion",
        **changes,
    }


async def balances(client: httpx.AsyncClient, identifier: str, **query: str) -> dict[str, Any]:
    result = await client.get(f"/api/v1/accounts/{identifier}/cash-balances", params=query)
    assert result.status_code == 200, result.text
    return dict(result.json()["balances"][0])


async def test_opening_correction_is_cas_idempotent_exact_and_retains_each_original_snapshot(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    url = f"/api/v1/accounts/{account['id']}/opening-cash"
    initial = await client.get(url)
    assert initial.status_code == 200 and initial.headers["ETag"] == '"1"'
    assert initial.headers["Cache-Control"] == "no-store"
    original = initial.json()
    assert original["quality"] == "OK" and original["booked_hkd"] == "7700.000000"
    headers = version_headers(client, 1)
    changed = await client.patch(url, json=body(), headers=headers)
    assert changed.status_code == 200, changed.text
    current = changed.json()
    assert changed.headers["ETag"] == '"2"' and current["version"] == 2
    assert (
        current["predecessor_id"] == original["entry_id"]
        and current["entry_id"] != original["entry_id"]
    )
    assert current["opening_cash"] == "1500.000000" and current["fx_rate_hkd"] == "7.900000000000"
    assert current["booked_hkd"] == "11850.000000" and current["quality"] == "OK"
    assert (await balances(client, account["id"]))["balance"] == "1500.000000"
    before = snapshot(engine)
    retry = await client.patch(url, json=body(), headers=headers)
    assert retry.status_code == 200 and retry.json() == current and snapshot(engine) == before
    conflict = await client.patch(url, json=body(opening_cash="1501"), headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    stale = await client.patch(url, json=body(), headers=version_headers(client, 1))
    assert stale.status_code == 412 and snapshot(engine) == before
    second = await client.patch(
        url,
        json=body(opening_cash="1700", opening_cash_date="2026-01-20", opening_fx_rate_hkd="8"),
        headers=version_headers(client, 2),
    )
    assert second.status_code == 200 and second.json()["predecessor_id"] == current["entry_id"]
    with unit_of_work(engine) as con:
        rows = list(
            con.exec_driver_sql("SELECT * FROM cash_ledger_entries ORDER BY rowid").mappings()
        )
        assert len(rows) == 5
        assert rows[0]["amount_micro"] == 1000000000 and rows[0]["fx_rate_hkd_e12"] == 7700000000000
        assert rows[0]["booked_hkd_micro"] == 7700000000 and rows[0]["version"] == 2
        assert rows[1]["reverses_entry_id"] == rows[0]["id"]
        assert rows[1]["booked_hkd_micro"] == -7700000000
        assert all(row["external_flow"] == 0 for row in rows)
        assert sum(row["amount_micro"] for row in rows) == 1700000000


async def test_relocated_opening_rebases_at_new_fx_and_can_be_cleared_or_reintroduced(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    security = await create(
        client,
        "securities",
        {
            "exchange_code": "NASDAQ",
            "display_ticker": "OPN",
            "name": "Opening test stock",
            "quote_currency": "USD",
        },
    )
    trade = {
        "portfolio_id": account["portfolio_id"],
        "account_id": account["id"],
        "security_id": security["id"],
        "side": "BUY",
        "trade_date": "2026-01-02",
        "quantity": "10",
        "price": "100",
        "fee": "1",
        "currency": "USD",
        "fx_rate_hkd": "7.8",
    }
    await create(client, "trades", trade)
    with unit_of_work(engine) as con:
        old = list(
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries "
                "WHERE source_type IN ('OPENING_BALANCE','OPENING_REBASE')"
            ).mappings()
        )
        assert len(old) == 2
    url = f"/api/v1/accounts/{account['id']}/opening-cash"
    changed = await client.patch(url, json=body(), headers=version_headers(client, 1))
    assert changed.status_code == 200, changed.text
    assert (await balances(client, account["id"]))["balance"] == "1500.000000"
    prior = await balances(client, account["id"], as_of="2026-01-12")
    assert prior["recorded_balance"] == "-1001.000000" and prior["balance"] is None
    assert prior["balance_quality"] == "BEFORE_OPENING"
    await create(
        client,
        "trades",
        {
            **trade,
            "side": "SELL",
            "trade_date": "2026-01-03",
            "quantity": "4",
            "price": "120",
            "fee": "2",
        },
    )
    with unit_of_work(engine) as con:
        latest = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries WHERE source_type='OPENING_REBASE' "
                "ORDER BY rowid DESC LIMIT 1"
            )
            .mappings()
            .one()
        )
        assert (
            latest["effective_date"] == "2026-01-15" and latest["fx_rate_hkd_e12"] == 7900000000000
        )
        assert latest["amount_micro"] == -478000000 and latest["booked_hkd_micro"] == -3776200000
    cleared = await client.patch(
        url,
        json={"opening_cash": None, "reason": "Withdraw unsupported starting assertion"},
        headers=version_headers(client, 2),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["quality"] == "UNSET" and cleared.json()["opening_cash"] is None
    assert cleared.json()["fx_rate_hkd"] is cleared.json()["booked_hkd"] is None
    assert cleared.json()["cash_completeness"] == "UNKNOWN"
    current = await balances(client, account["id"])
    assert current["recorded_balance"] == "-523.000000" and current["balance"] is None
    assert current["balance_quality"] == "UNKNOWN"
    known_zero = await client.patch(
        url,
        json=body(opening_cash="0", opening_cash_date="2026-01-20", opening_fx_rate_hkd="8"),
        headers=version_headers(client, 3),
    )
    assert known_zero.status_code == 200, known_zero.text
    assert known_zero.json()["opening_cash"] == known_zero.json()["booked_hkd"] == "0.000000"
    assert (
        known_zero.json()["quality"] == "OK" and known_zero.json()["cash_completeness"] == "UNKNOWN"
    )
    assert known_zero.json()["predecessor_id"] == changed.json()["entry_id"]
    assert (await balances(client, account["id"]))["balance"] is None
    asserted = await client.patch(
        f"/api/v1/accounts/{account['id']}",
        json={"cash_completeness": "COMPLETE"},
        headers=version_headers(client, 4),
    )
    assert (
        asserted.status_code == 200
        and (await balances(client, account["id"]))["balance"] == "0.000000"
    )
    with unit_of_work(engine) as con:
        for original in old:
            actual = (
                con.exec_driver_sql(
                    "SELECT * FROM cash_ledger_entries WHERE id=?", (original["id"],)
                )
                .mappings()
                .one()
            )
            assert all(actual[key] == original[key] for key in original if key != "version")
            assert actual["version"] == 2
        assert con.exec_driver_sql("PRAGMA foreign_key_check").all() == []


async def test_existing_account_can_adopt_first_dated_assertion_without_double_counting_prior_cash(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    new = await create(
        client,
        "accounts",
        {
            "portfolio_id": account["portfolio_id"],
            "broker_id": account["broker_id"],
            "name": "Previously unknown opening",
            "default_currency": "USD",
        },
    )
    await create(
        client,
        "cash-entries",
        {
            "account_id": new["id"],
            "effective_date": "2026-01-02",
            "kind": "DEPOSIT",
            "currency": "USD",
            "amount": "100",
        },
    )
    url = f"/api/v1/accounts/{new['id']}/opening-cash"
    assert (await client.get(url)).json()["quality"] == "UNSET"
    patched = await client.patch(
        url,
        json=body(opening_cash="1000", opening_cash_date="2026-01-10", opening_fx_rate_hkd="7.8"),
        headers=version_headers(client, 1),
    )
    assert patched.status_code == 200
    result = await balances(client, new["id"])
    assert result["recorded_balance"] == "1000.000000" and result["balance"] is None
    assert result["historical_booked_hkd_net"] is None
    with unit_of_work(engine) as con:
        initial = (
            con.exec_driver_sql(
                "SELECT * FROM cash_ledger_entries "
                "WHERE account_id=? AND source_type='OPENING_BALANCE'",
                (new["id"],),
            )
            .mappings()
            .one()
        )
        assert initial["source_id"] == new["id"] and initial["amount_micro"] == 1000000000


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"opening_cash": 1000}, "VALIDATION_ERROR"),
        ({"opening_cash": 0.1}, "VALIDATION_ERROR"),
        ({"opening_cash": True}, "VALIDATION_ERROR"),
        ({"opening_cash": "-1"}, "VALIDATION_ERROR"),
        ({"opening_cash": "1e3"}, "VALIDATION_ERROR"),
        ({"opening_cash": "NaN"}, "VALIDATION_ERROR"),
        ({"opening_cash": "1000.0000001"}, "VALIDATION_ERROR"),
        ({"opening_cash": "9223372036854.775808"}, "VALIDATION_ERROR"),
        ({"opening_cash_date": None}, "VALIDATION_ERROR"),
        ({"opening_cash_date": "2050-01-02"}, "VALIDATION_ERROR"),
        ({"opening_cash_date": "2026-02-30"}, "VALIDATION_ERROR"),
        ({"opening_fx_rate_hkd": "0"}, "VALIDATION_ERROR"),
        ({"opening_fx_rate_hkd": "7.8000000000001"}, "VALIDATION_ERROR"),
        ({"opening_fx_rate_hkd": None}, "MISSING_FX"),
        ({"opening_cash": None}, "VALIDATION_ERROR"),
        ({"reason": "short"}, "VALIDATION_ERROR"),
        ({"currency": "HKD"}, "VALIDATION_ERROR"),
        ({"opening_cash": "2000000", "opening_fx_rate_hkd": "9000000"}, "FINANCIAL_OVERFLOW"),
    ],
)
async def test_invalid_opening_correction_rolls_back_all_tables_and_control_metadata(
    client: httpx.AsyncClient,
    engine: Engine,
    account: dict[str, Any],
    changes: dict[str, Any],
    code: str,
) -> None:
    before = snapshot(engine)
    result = await client.patch(
        f"/api/v1/accounts/{account['id']}/opening-cash",
        json=body(**changes),
        headers=version_headers(client, 1),
    )
    assert result.status_code == 422 and result.json()["code"] == code, result.text
    assert snapshot(engine) == before


async def test_rebase_overflow_after_replacement_restores_old_opening_all_versions_and_audit(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    # The old low-rate assertion can accommodate this native history; the replacement cannot.
    low = await client.patch(
        f"/api/v1/accounts/{account['id']}/opening-cash",
        json=body(opening_fx_rate_hkd="0.000000000001"),
        headers=version_headers(client, 1),
    )
    assert low.status_code == 200
    await create(
        client,
        "cash-entries",
        {
            "account_id": account["id"],
            "effective_date": "2026-01-02",
            "kind": "DEPOSIT",
            "currency": "USD",
            "amount": "2000000000000",
            "fx_rate_hkd": "0.000000000001",
        },
    )
    before = snapshot(engine)
    rejected = await client.patch(
        f"/api/v1/accounts/{account['id']}/opening-cash",
        json=body(),
        headers=version_headers(client, 2),
    )
    assert rejected.status_code == 422 and rejected.json()["code"] == "FINANCIAL_OVERFLOW"
    assert snapshot(engine) == before


async def test_opening_serialization_failure_restores_account_original_and_all_cash(
    client: httpx.AsyncClient,
    engine: Engine,
    account: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(self: OpeningCashRecord, **kwargs: Any) -> str:
        raise ValueError("Synthetic opening serialization failure")

    monkeypatch.setattr(OpeningCashRecord, "model_dump_json", broken)
    before = snapshot(engine)
    with pytest.raises(ValueError, match="Synthetic opening serialization failure"):
        await client.patch(
            f"/api/v1/accounts/{account['id']}/opening-cash",
            json=body(),
            headers=version_headers(client, 1),
        )
    assert snapshot(engine) == before


async def test_hkd_identity_source_protection_and_archive_do_not_allow_metadata_shortcuts(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    hkd = await create(
        client,
        "accounts",
        {
            "portfolio_id": account["portfolio_id"],
            "broker_id": account["broker_id"],
            "name": "HKD opening",
            "default_currency": "HKD",
        },
    )
    response = await client.patch(
        f"/api/v1/accounts/{hkd['id']}/opening-cash",
        json=body(opening_cash="12.345001", opening_fx_rate_hkd=None),
        headers=version_headers(client, 1),
    )
    assert response.status_code == 200 and response.json()["fx_rate_hkd"] == "1.000000000000"
    assert response.json()["booked_hkd"] == "12.345001"
    before = snapshot(engine)
    bad = await client.patch(
        f"/api/v1/accounts/{hkd['id']}/opening-cash",
        json=body(),
        headers=version_headers(client, 2),
    )
    assert (
        bad.status_code == 422
        and bad.json()["code"] == "CURRENCY_MISMATCH"
        and snapshot(engine) == before
    )
    indirect = await client.patch(
        f"/api/v1/accounts/{hkd['id']}",
        json={"opening_cash": "100"},
        headers=version_headers(client, 2),
    )
    assert indirect.status_code == 422 and snapshot(engine) == before
    guarded = await client.request(
        "DELETE",
        f"/api/v1/cash-entries/{response.json()['entry_id']}",
        json={"reason": "Disallowed direct opening removal"},
        headers=version_headers(client, 1),
    )
    assert guarded.status_code == 409 and guarded.json()["code"] == "SOURCE_MANAGED"
    archived = await client.delete(
        f"/api/v1/accounts/{account['id']}", headers=version_headers(client, 1)
    )
    assert archived.status_code == 200
    before = snapshot(engine)
    blocked = await client.patch(
        f"/api/v1/accounts/{account['id']}/opening-cash",
        json=body(),
        headers=version_headers(client, 2),
    )
    assert (
        blocked.status_code == 409
        and blocked.json()["code"] == "ARCHIVED"
        and snapshot(engine) == before
    )


async def test_opening_scope_auth_csrf_and_account_version_are_required_without_writes(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    owner, portfolio, broker, foreign = (str(uuid4()) for _ in range(4))
    with unit_of_work(engine, write=True) as con:
        con.exec_driver_sql(
            "INSERT INTO owners VALUES (?,'synthetic-hash',?,?,1)", (owner, stamp(), stamp())
        )
        con.exec_driver_sql(
            "INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (?,?,'Other',?)",
            (portfolio, owner, stamp()),
        )
        con.exec_driver_sql(
            "INSERT INTO brokers(id,owner_id,name) VALUES (?,?,'Other')", (broker, owner)
        )
        con.exec_driver_sql(
            "INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,created_at) "
            "VALUES (?,?,?,'Other','USD',?)",
            (foreign, portfolio, broker, stamp()),
        )
    before = snapshot(engine)
    for identifier in (foreign, str(uuid4())):
        url = f"/api/v1/accounts/{identifier}/opening-cash"
        assert (await client.get(url)).status_code == 404
        rejected = await client.patch(url, json=body(), headers=version_headers(client, 1))
        assert rejected.status_code == 404 and snapshot(engine) == before
    url = f"/api/v1/accounts/{account['id']}/opening-cash"
    for headers, code in (
        (write_headers(client), 422),
        ({**version_headers(client, 1), "If-Match": "1"}, 422),
        ({**version_headers(client, 1), "X-CSRF-Token": "synthetic-wrong-token"}, 403),
        ({"If-Match": '"1"', "X-CSRF-Token": write_headers(client)["X-CSRF-Token"]}, 422),
    ):
        rejected = await client.patch(url, json=body(), headers=headers)
        assert rejected.status_code == code and snapshot(engine) == before
    renamed = await client.patch(
        f"/api/v1/accounts/{account['id']}",
        json={"name": "Reviewed account"},
        headers=version_headers(client, 1),
    )
    assert renamed.status_code == 200
    before = snapshot(engine)
    stale = await client.patch(url, json=body(), headers=version_headers(client, 1))
    assert stale.status_code == 412 and snapshot(engine) == before
    client.cookies.clear()
    for method in ("GET", "PATCH"):
        rejected = await client.request(
            method,
            url,
            json=body() if method == "PATCH" else None,
            headers=version_headers(client, 2),
        )
        assert rejected.status_code == 401 and rejected.headers["Cache-Control"] == "no-store"
        assert snapshot(engine) == before


@pytest.mark.parametrize("provenance", ["missing", "retired"])
async def test_explicit_assertion_repairs_missing_or_retired_provenance_without_rewriting_facts(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any], provenance: str
) -> None:
    if provenance == "missing":
        account = await create(
            client,
            "accounts",
            {
                "portfolio_id": account["portfolio_id"],
                "broker_id": account["broker_id"],
                "name": "Retained metadata-only assertion",
                "default_currency": "USD",
                "cash_completeness": "COMPLETE",
            },
        )
    url = f"/api/v1/accounts/{account['id']}/opening-cash"
    original = (await client.get(url)).json()
    with unit_of_work(engine, write=True) as con:
        if provenance == "retired":
            entry = (
                con.exec_driver_sql(
                    "SELECT * FROM cash_ledger_entries WHERE id=?",
                    (original["entry_id"],),
                )
                .mappings()
                .one()
            )
            reverse_cash(con, entry, reason="Synthetic unsupported historical assertion")
        else:
            # Retained metadata has no invented provenance or deleted financial fact.
            con.exec_driver_sql(
                "UPDATE accounts SET opening_cash_micro=1000000000,"
                "opening_cash_date='2026-01-10' WHERE id=?",
                (account["id"],),
            )
    assert (await client.get(url)).json()["quality"] == "NEEDS_REVIEW"
    assert (await balances(client, account["id"]))["balance"] is None
    before = snapshot(engine)["cash_ledger_entries"]
    corrected = await client.patch(url, json=body(), headers=version_headers(client, 1))
    assert corrected.status_code == 200 and corrected.json()["quality"] == "OK"
    assert corrected.json()["predecessor_id"] == original["entry_id"]
    assert snapshot(engine)["cash_ledger_entries"][: len(before)] == before
    assert (await balances(client, account["id"]))["balance"] == "1500.000000"


async def test_ambiguous_opening_chain_is_review_and_never_guessed_or_silently_repaired(
    client: httpx.AsyncClient, engine: Engine, account: dict[str, Any]
) -> None:
    with unit_of_work(engine, write=True) as con:
        # Deliberate corruption models old operator data; public writes cannot create this fork.
        con.exec_driver_sql("DROP TRIGGER trg_ledger_cash_opening")
        post_cash(
            con,
            account=account["id"],
            day="2026-01-10",
            currency="USD",
            amount=1,
            fx=7700000000000,
            booked=8,
            kind="ADJUSTMENT",
            source_type="OPENING_BALANCE",
            source_id=str(uuid4()),
        )
    before = snapshot(engine)
    url = f"/api/v1/accounts/{account['id']}/opening-cash"
    for method in ("GET", "PATCH"):
        response = await client.request(
            method,
            url,
            json=body() if method == "PATCH" else None,
            headers=version_headers(client, 1),
        )
        assert response.status_code == 409 and response.json()["code"] == "NEEDS_REVIEW"
        assert snapshot(engine) == before
