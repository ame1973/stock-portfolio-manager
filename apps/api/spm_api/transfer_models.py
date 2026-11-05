"""Exact paired cash and account-specific security-transfer revision contracts."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import model_validator

from spm_api.cash_models import CashDate
from spm_api.decimal_types import FxRate, IsoDate, Money, NonNegativeMoney, PositiveQuantity
from spm_api.errors import Model
from spm_api.master_models import ChangeReason, Currency, Note


class TransferAccounts(Model):
    portfolio_id: UUID
    source_account_id: UUID
    target_account_id: UUID
    transfer_date: IsoDate
    note: Note = ""


class CashTransferCreate(TransferAccounts):
    transfer_date: CashDate
    source_currency: Currency
    target_currency: Currency
    source_amount: NonNegativeMoney
    target_amount: NonNegativeMoney
    fx_rate_hkd: FxRate | None = None
    actual_hkd: NonNegativeMoney | None = None
    reason: ChangeReason

    @model_validator(mode="after")
    def conserved_principal(self) -> "CashTransferCreate":
        if Decimal(self.source_amount) == 0 or Decimal(self.target_amount) == 0:
            raise ValueError("Transfer principal must be positive")
        if self.source_currency == self.target_currency:
            if self.source_account_id == self.target_account_id:
                raise ValueError("Select a different account or explicit currency conversion")
            if self.source_amount != self.target_amount:
                raise ValueError(
                    "Same-currency principal must match; record actual fees separately"
                )
        if self.actual_hkd is not None and (
            Decimal(self.actual_hkd) == 0
            or (self.source_currency == "HKD" and self.target_currency == "HKD")
        ):
            raise ValueError("Actual HKD must be positive and apply to the USD leg")
        return self


class SecurityTransferCreate(TransferAccounts):
    security_id: UUID
    quantity: PositiveQuantity

    @model_validator(mode="after")
    def separate_accounts(self) -> "SecurityTransferCreate":
        if self.source_account_id == self.target_account_id:
            raise ValueError("Security transfer accounts must differ")
        return self


class SecurityTransferRevision(SecurityTransferCreate):
    reason: ChangeReason


class TransferRecord(Model):
    id: UUID
    root_id: UUID
    supersedes_id: UUID | None
    portfolio_id: UUID
    source_account_id: UUID
    target_account_id: UUID
    transfer_date: IsoDate
    status: Literal["ACTIVE", "SUPERSEDED", "VOID"]
    revision: int
    version: int
    note: str
    created_at: str


class CashTransfer(TransferRecord):
    source_currency: Currency
    target_currency: Currency
    source_amount: NonNegativeMoney
    target_amount: NonNegativeMoney
    fx_rate_hkd: FxRate | None
    actual_hkd: NonNegativeMoney | None
    source_booked_hkd: NonNegativeMoney | None
    target_booked_hkd: NonNegativeMoney | None
    booking_delta_hkd: Money | None
    quality: Literal["OK", "MISSING_FX", "NEEDS_REVIEW"]
    reason: str
    updated_at: str


class SecurityTransfer(TransferRecord):
    security_id: UUID
    quantity: PositiveQuantity
    cost_moved_hkd: NonNegativeMoney
    cost_scope: Literal["BOOKED_AT_CREATION"] = "BOOKED_AT_CREATION"
    created_sequence: int
    is_current: bool
