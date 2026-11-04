"""Master-data services; callers own the short database transaction."""

from datetime import date
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, RowMapping, text

from spm_api.audit import audit, now, stamp
from spm_api.decimal_types import from_scaled, to_scaled
from spm_api.errors import ApiError, Model, Success
from spm_api.master_models import (
    Account,
    AccountCreate,
    AccountUpdate,
    Broker,
    BrokerCreate,
    BrokerUpdate,
    Identifier,
    IdentifierCreate,
    MappingPeriodUpdate,
    Portfolio,
    PortfolioCreate,
    PortfolioUpdate,
    ProviderSymbol,
    ProviderSymbolCreate,
    Security,
    SecurityCreate,
    SecurityUpdate,
)
from spm_api.mutations import check_version
from spm_api.security import Auth


def owned_portfolio(con: Connection, owner: str, identifier: str) -> RowMapping:
    row = (
        con.execute(
            text("SELECT * FROM portfolios WHERE id=:id AND owner_id=:owner"),
            {
                "id": identifier,
                "owner": owner,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Portfolio not found")
    return row


def owned_broker(con: Connection, owner: str, identifier: str) -> RowMapping:
    row = (
        con.execute(
            text("SELECT * FROM brokers WHERE id=:id AND owner_id=:owner"),
            {
                "id": identifier,
                "owner": owner,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Broker not found")
    return row


def owned_account(con: Connection, owner: str, identifier: str) -> RowMapping:
    row = (
        con.execute(
            text("""
        SELECT a.* FROM accounts a JOIN portfolios p ON p.id=a.portfolio_id
        WHERE a.id=:id AND p.owner_id=:owner
    """),
            {"id": identifier, "owner": owner},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Account not found")
    return row


def security_row(con: Connection, identifier: str) -> RowMapping:
    row = (
        con.execute(text("SELECT * FROM securities WHERE id=:id"), {"id": identifier})
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Security not found")
    return row


def as_portfolio(row: RowMapping) -> Portfolio:
    return Portfolio.model_validate({key: row[key] for key in Portfolio.model_fields})


def as_broker(row: RowMapping) -> Broker:
    return Broker(
        id=row["id"],
        name=row["name"],
        dividend_fee=from_scaled(row["dividend_fee_micro"], 6),
        dividend_fee_currency=row["dividend_fee_currency"],
        status=row["status"],
        version=row["version"],
    )


def as_account(row: RowMapping) -> Account:
    fields = {key: row[key] for key in Account.model_fields if key != "opening_cash"}
    fields["opening_cash"] = (
        from_scaled(row["opening_cash_micro"], 6) if row["opening_cash_micro"] is not None else None
    )
    return Account.model_validate(fields)


def as_security(row: RowMapping) -> Security:
    return Security.model_validate({key: row[key] for key in Security.model_fields})


def as_identifier(row: RowMapping) -> Identifier:
    return Identifier.model_validate({key: row[key] for key in Identifier.model_fields})


def as_provider_symbol(row: RowMapping) -> ProviderSymbol:
    return ProviderSymbol.model_validate({key: row[key] for key in ProviderSymbol.model_fields})


def _changes(body: Model, *, omit: set[str] | None = None) -> dict[str, Any]:
    changes = body.model_dump(mode="json", exclude_unset=True, exclude=omit or set())
    if not changes or any(value is None for value in changes.values()):
        raise ApiError(422, "INVALID_PATCH", "Provide non-null fields to change")
    return changes


def _update(
    con: Connection, table: str, identifier: str, version: int, changes: dict[str, Any]
) -> None:
    # Table/column names come only from the fixed service mappings below, never request strings.
    assignments = ", ".join(f"{key}=:{key}" for key in changes)
    result = con.execute(
        text(f"UPDATE {table} SET {assignments},version=version+1 WHERE id=:id AND version=:old"),
        {
            **changes,
            "id": identifier,
            "old": version,
        },
    )
    if result.rowcount != 1:
        raise ApiError(412, "PRECONDITION_FAILED", "This record changed; reload before editing")


def invalidate_portfolio(
    con: Connection, identifier: str, source: str, reason: str, from_date: str | None = None
) -> None:
    if from_date is None:
        from_date = (
            con.execute(
                text("""
            SELECT MIN(day) FROM (
                SELECT trade_date AS day FROM trades WHERE portfolio_id=:id
                UNION ALL
                SELECT c.effective_date FROM cash_ledger_entries c
                JOIN accounts a ON a.id=c.account_id
                WHERE a.portfolio_id=:id
            )
        """),
                {"id": identifier},
            ).scalar()
            or date.today().isoformat()
        )
    con.execute(
        text("UPDATE portfolios SET data_revision=data_revision+1 WHERE id=:id"), {"id": identifier}
    )
    con.execute(
        text("""
        INSERT INTO projection_invalidations(id,portfolio_id,from_date,reason,source_id,created_at)
        VALUES (:key,:portfolio,:day,:reason,:source,:now)
    """),
        {
            "key": str(uuid4()),
            "portfolio": identifier,
            "day": from_date,
            "reason": reason,
            "source": source,
            "now": stamp(),
        },
    )


def create_portfolio(
    con: Connection, auth: Auth, body: PortfolioCreate, request_id: str
) -> Portfolio:
    identifier = str(uuid4())
    con.execute(
        text("INSERT INTO portfolios(id,owner_id,name,created_at) VALUES (:id,:owner,:name,:now)"),
        {
            "id": identifier,
            "owner": str(auth.owner.id),
            "name": body.name,
            "now": stamp(),
        },
    )
    result = as_portfolio(owned_portfolio(con, str(auth.owner.id), identifier))
    audit(
        con,
        str(auth.owner.id),
        "portfolio",
        identifier,
        "CREATE",
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def update_portfolio(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: PortfolioUpdate,
    request_id: str,
) -> Portfolio:
    before = owned_portfolio(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    changes = _changes(body)
    _update(con, "portfolios", identifier, version, changes)
    if "cash_completeness" in changes:
        invalidate_portfolio(con, identifier, identifier, "CASH_COMPLETENESS_CHANGED")
    result = as_portfolio(owned_portfolio(con, str(auth.owner.id), identifier))
    audit(
        con,
        str(auth.owner.id),
        "portfolio",
        identifier,
        "UPDATE",
        before=dict(before),
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def create_broker(con: Connection, auth: Auth, body: BrokerCreate, request_id: str) -> Broker:
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO brokers(id,owner_id,name,dividend_fee_micro,dividend_fee_currency)
        VALUES (:id,:owner,:name,:fee,:currency)
    """),
        {
            "id": identifier,
            "owner": str(auth.owner.id),
            "name": body.name,
            "fee": to_scaled(body.dividend_fee, 6),
            "currency": body.dividend_fee_currency,
        },
    )
    result = as_broker(owned_broker(con, str(auth.owner.id), identifier))
    audit(
        con,
        str(auth.owner.id),
        "broker",
        identifier,
        "CREATE",
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def update_broker(
    con: Connection, auth: Auth, identifier: str, version: int, body: BrokerUpdate, request_id: str
) -> Broker:
    before = owned_broker(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    changes = _changes(body)
    if "dividend_fee" in changes:
        changes["dividend_fee_micro"] = to_scaled(changes.pop("dividend_fee"), 6)
    _update(con, "brokers", identifier, version, changes)
    result = as_broker(owned_broker(con, str(auth.owner.id), identifier))
    audit(
        con,
        str(auth.owner.id),
        "broker",
        identifier,
        "UPDATE",
        before=dict(before),
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def archive_broker(
    con: Connection, auth: Auth, identifier: str, version: int, request_id: str
) -> Success:
    before = owned_broker(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    if before["status"] != "ARCHIVED":
        _update(con, "brokers", identifier, version, {"status": "ARCHIVED"})
        audit(
            con,
            str(auth.owner.id),
            "broker",
            identifier,
            "ARCHIVE",
            before=dict(before),
            request_id=request_id,
        )
    return Success(message="Broker archived; accounts and history retained")


def create_account(con: Connection, auth: Auth, body: AccountCreate, request_id: str) -> Account:
    owner = str(auth.owner.id)
    portfolio, broker = str(body.portfolio_id), str(body.broker_id)
    owned_portfolio(con, owner, portfolio)
    if owned_broker(con, owner, broker)["status"] != "ACTIVE":
        raise ApiError(409, "ARCHIVED", "Reopen the broker before creating an account")
    if (
        body.opening_cash is not None
        and body.default_currency == "USD"
        and body.opening_fx_rate_hkd is None
    ):
        raise ApiError(422, "MISSING_FX", "USD opening cash requires a locked historical FX rate")
    identifier = str(uuid4())
    amount = to_scaled(body.opening_cash, 6) if body.opening_cash is not None else None
    con.execute(
        text("""
        INSERT INTO accounts(id,portfolio_id,broker_id,name,default_currency,opening_cash_micro,
                             opening_cash_date,cash_completeness,created_at)
        VALUES (:id,:portfolio,:broker,:name,:currency,:amount,:day,:complete,:now)
    """),
        {
            "id": identifier,
            "portfolio": portfolio,
            "broker": broker,
            "name": body.name,
            "currency": body.default_currency,
            "amount": amount,
            "day": body.opening_cash_date,
            "complete": body.cash_completeness,
            "now": stamp(),
        },
    )
    if amount is not None:
        fx = "1.000000000000" if body.default_currency == "HKD" else body.opening_fx_rate_hkd
        assert fx is not None
        con.execute(
            text("""
            INSERT INTO cash_ledger_entries(id,account_id,effective_date,currency,amount_micro,
                fx_rate_hkd_e12,kind,source_type,source_id,note,created_at)
            VALUES (:id,:account,:day,:currency,:amount,:fx,'ADJUSTMENT','OPENING_BALANCE',
                    :account,'Owner-declared starting balance; not an external deposit',:now)
        """),
            {
                "id": str(uuid4()),
                "account": identifier,
                "day": body.opening_cash_date,
                "currency": body.default_currency,
                "amount": amount,
                "fx": to_scaled(fx, 12),
                "now": stamp(),
            },
        )
        audit(
            con,
            owner,
            "account",
            identifier,
            "OPENING_BALANCE",
            request_id=request_id,
            after={
                "amount": body.opening_cash,
                "currency": body.default_currency,
                "date": body.opening_cash_date,
                "fx": fx,
            },
        )
    invalidate_portfolio(con, portfolio, identifier, "ACCOUNT_CREATED", body.opening_cash_date)
    result = as_account(owned_account(con, owner, identifier))
    audit(
        con,
        owner,
        "account",
        identifier,
        "CREATE",
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def update_account(
    con: Connection, auth: Auth, identifier: str, version: int, body: AccountUpdate, request_id: str
) -> Account:
    before = owned_account(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    changes = _changes(body)
    _update(con, "accounts", identifier, version, changes)
    if "cash_completeness" in changes or "status" in changes:
        invalidate_portfolio(con, before["portfolio_id"], identifier, "ACCOUNT_SETTINGS_CHANGED")
    result = as_account(owned_account(con, str(auth.owner.id), identifier))
    audit(
        con,
        str(auth.owner.id),
        "account",
        identifier,
        "UPDATE",
        before=dict(before),
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def archive_account(
    con: Connection, auth: Auth, identifier: str, version: int, request_id: str
) -> Success:
    before = owned_account(con, str(auth.owner.id), identifier)
    check_version(before["version"], version)
    if before["status"] != "ARCHIVED":
        _update(con, "accounts", identifier, version, {"status": "ARCHIVED"})
        invalidate_portfolio(con, before["portfolio_id"], identifier, "ACCOUNT_ARCHIVED")
        audit(
            con,
            str(auth.owner.id),
            "account",
            identifier,
            "ARCHIVE",
            before=dict(before),
            request_id=request_id,
        )
    return Success(message="Account archived; financial history retained")


def create_security(con: Connection, auth: Auth, body: SecurityCreate, request_id: str) -> Security:
    exchange = (
        con.execute(text("SELECT * FROM exchanges WHERE code=:code"), {"code": body.exchange_code})
        .mappings()
        .first()
    )
    if exchange is None:
        raise ApiError(422, "UNSUPPORTED_MARKET", "Select a supported HK/US exchange")
    if exchange["market"] == "US" and body.quote_currency != "USD":
        raise ApiError(422, "CURRENCY_MISMATCH", "US securities use USD quotes")
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO securities(id,exchange_code,display_ticker,name,security_type,
                               quote_currency,isin,created_at)
        VALUES (:id,:exchange,:ticker,:name,:type,:currency,:isin,:now)
    """),
        {
            "id": identifier,
            "exchange": body.exchange_code,
            "ticker": body.display_ticker,
            "name": body.name,
            "type": body.security_type,
            "currency": body.quote_currency,
            "isin": body.isin,
            "now": stamp(),
        },
    )
    effective = (
        body.identifier_effective_from
        or now().astimezone(ZoneInfo(exchange["timezone"])).date().isoformat()
    )
    add_identifier(
        con,
        auth,
        identifier,
        IdentifierCreate(
            exchange_code=body.exchange_code,
            identifier_type="TICKER",
            identifier_value=body.display_ticker,
            effective_from=effective,
        ),
        request_id,
    )
    result = as_security(security_row(con, identifier))
    audit(
        con,
        str(auth.owner.id),
        "security",
        identifier,
        "CREATE",
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def update_security(
    con: Connection,
    auth: Auth,
    identifier: str,
    version: int,
    body: SecurityUpdate,
    request_id: str,
) -> Security:
    before = security_row(con, identifier)
    check_version(before["version"], version)
    changes = _changes(body, omit={"reason"})
    if "verified" in changes:
        changes["verified"] = int(changes["verified"])
    _update(con, "securities", identifier, version, changes)
    result = as_security(security_row(con, identifier))
    audit(
        con,
        str(auth.owner.id),
        "security",
        identifier,
        "UPDATE",
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
        request_id=request_id,
    )
    return result


def add_identifier(
    con: Connection, auth: Auth, security: str, body: IdentifierCreate, request_id: str
) -> Identifier:
    if security_row(con, security)["exchange_code"] != body.exchange_code:
        raise ApiError(422, "EXCHANGE_MISMATCH", "Identifier must use the security's exchange")
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO security_identifiers(id,security_id,exchange_code,identifier_type,
                                        identifier_value,effective_from,effective_to)
        VALUES (:id,:security,:exchange,:type,:value,:start,:end)
    """),
        {
            "id": identifier,
            "security": security,
            "exchange": body.exchange_code,
            "type": body.identifier_type,
            "value": body.identifier_value,
            "start": body.effective_from,
            "end": body.effective_to,
        },
    )
    row = (
        con.execute(text("SELECT * FROM security_identifiers WHERE id=:id"), {"id": identifier})
        .mappings()
        .one()
    )
    result = as_identifier(row)
    audit(
        con,
        str(auth.owner.id),
        "identifier",
        identifier,
        "CREATE",
        after=result.model_dump(mode="json"),
        request_id=request_id,
    )
    return result


def add_provider_symbol(
    con: Connection, auth: Auth, security: str, body: ProviderSymbolCreate, request_id: str
) -> ProviderSymbol:
    security_row(con, security)
    identifier = str(uuid4())
    con.execute(
        text("""
        INSERT INTO provider_symbols(id,security_id,provider_id,provider_symbol,
                                     effective_from,effective_to,verified)
        VALUES (:id,:security,:provider,:symbol,:start,:end,:verified)
    """),
        {
            "id": identifier,
            "security": security,
            "provider": body.provider_id,
            "symbol": body.provider_symbol,
            "start": body.effective_from,
            "end": body.effective_to,
            "verified": int(body.verified),
        },
    )
    row = (
        con.execute(text("SELECT * FROM provider_symbols WHERE id=:id"), {"id": identifier})
        .mappings()
        .one()
    )
    result = as_provider_symbol(row)
    audit(
        con,
        str(auth.owner.id),
        "provider_symbol",
        identifier,
        "CREATE",
        after=result.model_dump(mode="json"),
        reason=body.reason,
        request_id=request_id,
    )
    return result


def update_mapping(
    con: Connection,
    auth: Auth,
    security: str,
    identifier: str,
    version: int,
    body: MappingPeriodUpdate,
    request_id: str,
    *,
    provider: bool,
) -> Identifier | ProviderSymbol:
    security_row(con, security)
    table = "provider_symbols" if provider else "security_identifiers"
    before = (
        con.execute(
            text(f"SELECT * FROM {table} WHERE id=:id AND security_id=:security"),
            {
                "id": identifier,
                "security": security,
            },
        )
        .mappings()
        .first()
    )
    if before is None:
        raise ApiError(404, "NOT_FOUND", "Mapping not found")
    check_version(before["version"], version)
    _update(
        con,
        table,
        identifier,
        version,
        {"effective_from": body.effective_from, "effective_to": body.effective_to},
    )
    after = (
        con.execute(text(f"SELECT * FROM {table} WHERE id=:id"), {"id": identifier})
        .mappings()
        .one()
    )
    result = as_provider_symbol(after) if provider else as_identifier(after)
    audit(
        con,
        str(auth.owner.id),
        "provider_symbol" if provider else "identifier",
        identifier,
        "CORRECT_PERIOD",
        before=dict(before),
        after=result.model_dump(mode="json"),
        reason=body.reason,
        request_id=request_id,
    )
    return result
