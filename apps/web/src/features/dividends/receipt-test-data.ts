import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import type { ReceiptTarget } from "./receipt-input-validation";

type Schemas = components["schemas"];
export const current: Schemas["ExistingReceiptBasis"] = { id: "11111111-1111-4111-8111-111111111111", revision_id: event.current_revision_id, revision_no: event.version,
  currency: "USD", recorded_shares: "10.00000000", recorded_expected_net: "11.000000", recorded_quality: "ACCRUED", created_at: "2026-11-03T00:00:00Z", scope: "LEGACY_ENTITLEMENT_NOT_VERIFIED" };
export const old: Schemas["ExistingReceiptBasis"] = { ...current, id: "22222222-2222-4222-8222-222222222222", revision_id: "33333333-3333-4333-8333-333333333333", revision_no: 1,
  recorded_expected_net: "10.000000", recorded_quality: "ESTIMATED" };
export const inputs: Schemas["ReceiptInputs"] = { event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE",
  account_version: "9007199254740993", portfolio_version: "9223372036854775807", broker_version: "2", portfolio_data_revision: "9223372036854775806",
  entitlements: { items: [current, old], next_cursor: null }, scope: "RECORDED_RECEIPT_INPUTS_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED" };
export const target: ReceiptTarget = { event, account, inputs, basis: old };
export const body: Schemas["ReceiptCreate"] = { receipt_id: "44444444-4444-4444-8444-444444444444", event_id: event.id, revision_id: event.current_revision_id,
  entitlement_id: old.id, account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: inputs.account_version,
  portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
  received_date: "2026-11-04", received_amount: "6.125", currency: "USD", tax: null, fee: "0", fx_rate_hkd: "7.8", fx_source: "Synthetic historical FX",
  actual_hkd: null, actual_hkd_reason: null, note: "Synthetic note", reason: "Synthetic recorded receipt reason", confirmation: "RECORDED_CASH_ONLY" };
export const created: Schemas["ReceiptCreated"] = { id: body.receipt_id, event_id: event.id, account_id: account.id, entitlement_id: old.id,
  original_revision_id: old.revision_id, original_revision_no: old.revision_no, observed_revision_id: event.current_revision_id, observed_revision_no: event.version,
  portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: inputs.account_version, portfolio_version: inputs.portfolio_version,
  broker_version: inputs.broker_version, portfolio_data_revision: inputs.portfolio_data_revision, cash_entry_id: "55555555-5555-4555-8555-555555555555",
  received_date: body.received_date, received_amount: "6.125000", currency: "USD", tax: null, fee: "0.000000", fx_rate_hkd: "7.800000000000",
  fx_source: "Synthetic historical FX", received_hkd: "47.775000", actual_hkd_override: false, source: "MANUAL", status: "ACTIVE", version: "1",
  created_at: "2026-11-04T00:00:00Z", audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", scope: "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" };
