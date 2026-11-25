import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { body as legacyBody, combinedReceipt, inputs as legacyInputs, voided as legacyVoided } from "./receipt-void-test-data";

type Schemas = components["schemas"];
export const receipt: Schemas["CombinedRecordedReceipt"] = { ...combinedReceipt, receipt_basis: "DIRECT_EVENT_ACCOUNT", entitlement_id: null };
export const current: Schemas["DirectReceiptInputs"] = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, workflow_status: event.workflow_status,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: account.status,
  account_version: legacyInputs.account_version, portfolio_version: legacyInputs.portfolio_version, broker_version: legacyInputs.broker_version,
  portfolio_data_revision: legacyInputs.portfolio_data_revision, rejection_state: "NOT_REJECTED", rejection_id: null,
  receipt_basis: "DIRECT_EVENT_ACCOUNT", scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
export const inputs: Schemas["DirectReceiptVoidInputs"] = { current, receipt, cash_entry_id: legacyInputs.cash_entry_id!,
  origin_portfolio_id: account.portfolio_id, scope: "DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY" };
export const target = { event, account, receipt, inputs };
export const body: Schemas["DirectReceiptVoidWrite"] = {
  event_id: current.event_id, observed_revision_id: current.revision_id, observed_revision_no: current.revision_no,
  original_revision_id: receipt.revision_id, original_revision_no: receipt.revision_no, account_id: current.account_id,
  portfolio_id: current.portfolio_id, broker_id: current.broker_id, account_version: current.account_version,
  portfolio_version: current.portfolio_version, broker_version: current.broker_version, expected_data_revision: current.portfolio_data_revision,
  cash_entry_id: inputs.cash_entry_id, reason: legacyBody.reason, confirmation: "VOID_RECORDED_CASH",
};
export const voided: Schemas["DirectReceiptVoided"] = {
  id: receipt.id, event_id: body.event_id, observed_revision_id: body.observed_revision_id, observed_revision_no: body.observed_revision_no,
  original_revision_id: body.original_revision_id, original_revision_no: body.original_revision_no, account_id: body.account_id,
  portfolio_id: body.portfolio_id, origin_portfolio_id: inputs.origin_portfolio_id, broker_id: body.broker_id,
  account_version: body.account_version, portfolio_version: body.portfolio_version, broker_version: body.broker_version,
  portfolio_data_revision: body.expected_data_revision, cash_entry_id: inputs.cash_entry_id, reversal_entry_id: legacyVoided.reversal_entry_id,
  receipt_basis: "DIRECT_EVENT_ACCOUNT", status: "VOID", version: "2", created_at: receipt.created_at, voided_at: legacyVoided.voided_at,
  audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", scope: "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
