import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { created, inputs as createInputs } from "./receipt-test-data";
import type { ReceiptVoidTarget } from "./receipt-void-validation";

type Schemas = components["schemas"];
export const receipt: Schemas["RecordedReceipt"] = {
  id: created.id, event_id: event.id, account_id: account.id, entitlement_id: created.entitlement_id,
  revision_id: created.original_revision_id, revision_no: created.original_revision_no, current_revision_id: event.current_revision_id,
  received_date: created.received_date, recorded_amount: created.received_amount, currency: created.currency,
  recorded_tax: created.tax, recorded_fee: created.fee, fx_rate_hkd: created.fx_rate_hkd, received_hkd: created.received_hkd,
  source: "MANUAL", status: "ACTIVE", version: "1", created_at: created.created_at, voided_at: null,
  cash_scope: "MATCHING_CASH_RECORDED_UNVERIFIED", audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED", history_completeness: "UNVERIFIED",
  scope: "UNVERIFIED_RECEIPT_HISTORY_ONLY", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED",
};
export const combinedReceipt: Schemas["CombinedRecordedReceipt"] = { ...receipt, receipt_basis: "LEGACY_ENTITLEMENT", current_revision_no: event.version,
  scope: "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY", audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" };
export const inputs: Schemas["ReceiptVoidInputs"] = {
  receipt_id: receipt.id, event_id: event.id, observed_revision_id: event.current_revision_id, observed_revision_no: event.version,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE",
  account_version: createInputs.account_version, portfolio_version: createInputs.portfolio_version, broker_version: createInputs.broker_version,
  portfolio_data_revision: createInputs.portfolio_data_revision, entitlement_id: receipt.entitlement_id, original_revision_id: receipt.revision_id,
  cash_entry_id: created.cash_entry_id, origin_portfolio_id: account.portfolio_id, received_date: receipt.received_date,
  received_amount: receipt.recorded_amount, currency: receipt.currency, tax: receipt.recorded_tax, fee: receipt.recorded_fee,
  fx_rate_hkd: receipt.fx_rate_hkd, received_hkd: receipt.received_hkd, status: "ACTIVE", receipt_version: "1", created_at: receipt.created_at,
  voided_at: null, audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", scope: "RECORDED_RECEIPT_VOID_INPUTS_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED",
  payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
export const target: ReceiptVoidTarget = { event, account, receipt, inputs };
export const body: Schemas["ReceiptVoidWrite"] = {
  event_id: inputs.event_id, observed_revision_id: inputs.observed_revision_id, observed_revision_no: inputs.observed_revision_no,
  entitlement_id: inputs.entitlement_id, original_revision_id: inputs.original_revision_id, account_id: inputs.account_id,
  portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id, account_version: inputs.account_version,
  portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
  cash_entry_id: created.cash_entry_id, reason: "Synthetic receipt correction reason", confirmation: "VOID_RECORDED_CASH",
};
export const voided: Schemas["ReceiptVoided"] = {
  id: receipt.id, event_id: event.id, observed_revision_id: event.current_revision_id, observed_revision_no: event.version,
  entitlement_id: receipt.entitlement_id, original_revision_id: receipt.revision_id, account_id: account.id,
  portfolio_id: account.portfolio_id, origin_portfolio_id: account.portfolio_id, broker_id: account.broker_id,
  account_version: inputs.account_version, portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version,
  portfolio_data_revision: inputs.portfolio_data_revision, cash_entry_id: created.cash_entry_id,
  reversal_entry_id: "66666666-6666-4666-8666-666666666666", status: "VOID", version: "2", created_at: receipt.created_at,
  voided_at: "2026-11-05T00:00:00Z", audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", scope: "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED",
  payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
