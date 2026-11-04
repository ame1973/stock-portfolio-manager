"""API contracts for owner-scoped master data and installation security identities."""

import re
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney
from spm_api.errors import Model

Currency = Literal["HKD", "USD"]
Completeness = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
ArchiveStatus = Literal["ACTIVE", "ARCHIVED"]
SecurityStatus = Literal["ACTIVE", "SUSPENDED", "DELISTED", "UNKNOWN"]
Name = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]
Ticker = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ProviderId = Literal[
    "eodhd",
    "alphavantage",
    "massive",
    "yahoo",
    "yfinance",
    "hkex_evidence",
    "sec_evidence",
    "manual",
]
ProviderCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9^][A-Za-z0-9.^=_-]*$",
    ),
]
Reason = Annotated[str, Field(max_length=1000)]


class PortfolioCreate(Model):
    name: Name
    base_currency: Literal["HKD"] = "HKD"


class Portfolio(Model):
    id: UUID
    name: str
    base_currency: Literal["HKD"]
    cash_completeness: Completeness
    data_revision: int
    active_projection_version: int
    version: int


class PortfolioUpdate(Model):
    name: Name | None = None
    cash_completeness: Completeness | None = None


class BrokerCreate(Model):
    name: Name
    dividend_fee: NonNegativeMoney = "0.000000"
    dividend_fee_currency: Currency = "HKD"


class Broker(Model):
    id: UUID
    name: str
    dividend_fee: Money
    dividend_fee_currency: Currency
    status: ArchiveStatus
    version: int


class BrokerUpdate(Model):
    name: Name | None = None
    dividend_fee: NonNegativeMoney | None = None
    dividend_fee_currency: Currency | None = None
    status: ArchiveStatus | None = None


class AccountCreate(Model):
    portfolio_id: UUID
    broker_id: UUID
    name: Name
    default_currency: Currency
    opening_cash: NonNegativeMoney | None = None
    opening_cash_date: IsoDate | None = None
    opening_fx_rate_hkd: FxRate | None = None
    cash_completeness: Completeness = "UNKNOWN"

    @model_validator(mode="after")
    def opening_terms(self) -> "AccountCreate":
        if self.opening_cash is None:
            if self.opening_cash_date is not None or self.opening_fx_rate_hkd is not None:
                raise ValueError("Opening date/FX requires an opening cash amount")
        else:
            if self.opening_cash_date is None or self.opening_cash_date > date.today().isoformat():
                raise ValueError("Opening cash requires a non-future calendar date")
        if self.default_currency == "HKD" and self.opening_fx_rate_hkd not in {
            None,
            "1.000000000000",
        }:
            raise ValueError("HKD FX must equal mathematical identity 1")
        return self


class Account(Model):
    id: UUID
    portfolio_id: UUID
    broker_id: UUID
    name: str
    default_currency: Currency
    status: ArchiveStatus
    opening_cash: Money | None
    opening_cash_date: IsoDate | None
    cash_completeness: Completeness
    version: int


class AccountUpdate(Model):
    name: Name | None = None
    status: ArchiveStatus | None = None
    cash_completeness: Completeness | None = None


class Exchange(Model):
    code: str
    market: Literal["HK", "US"]
    timezone: str
    default_currency: Currency
    display_name: str


class SecurityCreate(Model):
    exchange_code: Annotated[str, Field(strict=True, min_length=1, max_length=20)]
    display_ticker: Ticker
    name: Name
    security_type: Literal["EQUITY", "ETF", "REIT", "ADR", "FUND", "OTHER"] = "EQUITY"
    quote_currency: Currency
    isin: Annotated[str, Field(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")] | None = None
    identifier_effective_from: IsoDate | None = None

    @field_validator("display_ticker", "exchange_code")
    @classmethod
    def uppercase(cls, value: str) -> str:
        return value.strip().upper()


class Security(Model):
    id: UUID
    exchange_code: str
    display_ticker: str
    name: str
    security_type: str
    quote_currency: Currency
    isin: str | None
    status: SecurityStatus
    verified: bool
    version: int


class SecurityUpdate(Model):
    name: Name | None = None
    status: SecurityStatus | None = None
    verified: Annotated[bool, Field(strict=True)] | None = None
    reason: Reason = ""

    @model_validator(mode="after")
    def verification_reason(self) -> "SecurityUpdate":
        if self.verified is True and len(self.reason.strip()) < 10:
            raise ValueError("Manual verification requires an explanation")
        return self


class IdentifierCreate(Model):
    exchange_code: str
    identifier_type: Literal["TICKER", "ISIN", "MIC", "OTHER"]
    identifier_value: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=100)
    ]
    effective_from: IsoDate
    effective_to: IsoDate | None = None

    @model_validator(mode="after")
    def period(self) -> "IdentifierCreate":
        self.exchange_code = self.exchange_code.strip().upper()
        if self.identifier_type in {"TICKER", "ISIN", "MIC"}:
            self.identifier_value = self.identifier_value.upper()
        if self.identifier_type == "TICKER" and not re.fullmatch(
            r"[A-Z0-9][A-Z0-9._-]{0,31}", self.identifier_value
        ):
            raise ValueError("Invalid ticker identifier")
        if self.identifier_type == "ISIN" and not re.fullmatch(
            r"[A-Z]{2}[A-Z0-9]{9}[0-9]", self.identifier_value
        ):
            raise ValueError("Invalid ISIN format")
        if self.identifier_type == "MIC" and not re.fullmatch(
            r"[A-Z0-9]{4}", self.identifier_value
        ):
            raise ValueError("MIC identifiers have four alphanumeric characters")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("Effective end precedes start")
        return self


class Identifier(IdentifierCreate):
    id: UUID
    security_id: UUID
    version: int


class MappingPeriodUpdate(Model):
    effective_from: IsoDate
    effective_to: IsoDate | None = None
    reason: Annotated[str, Field(min_length=10, max_length=1000)]

    @model_validator(mode="after")
    def period(self) -> "MappingPeriodUpdate":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("Effective end precedes start")
        return self


class ProviderSymbolCreate(Model):
    provider_id: ProviderId
    provider_symbol: ProviderCode
    effective_from: IsoDate
    effective_to: IsoDate | None = None
    verified: Annotated[bool, Field(strict=True)] = False
    reason: Reason = ""

    @model_validator(mode="after")
    def terms(self) -> "ProviderSymbolCreate":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("Effective end precedes start")
        if self.verified and len(self.reason.strip()) < 10:
            raise ValueError("Manual verification requires an explanation")
        return self


class ProviderSymbol(Model):
    id: UUID
    security_id: UUID
    provider_id: str
    provider_symbol: str
    effective_from: IsoDate
    effective_to: IsoDate | None
    verified: bool
    version: int
