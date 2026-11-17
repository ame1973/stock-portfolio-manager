import type { components } from "@spm/contracts";
import type { ProjectionQuery, ProjectionTarget } from "./projection-input-validation";

type Schemas = components["schemas"];
export const event: Schemas["DividendEventRecord"] = {
  id: "a1111111-1111-4111-8111-111111111111", current_revision_id: "b2222222-2222-4222-8222-222222222222", version: 3,
  security_id: "c3333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null,
  pay_date: null, dps: "1.00000000", currency: "USD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false,
  manual_override: false, decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic event reference", url: null, url_withheld: false }, reason: "Synthetic projection event decision",
  created_at: "2026-11-01T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
export const account: Schemas["Account"] = {
  id: "d4444444-4444-4444-8444-444444444444", portfolio_id: "e5555555-5555-4555-8555-555555555555", broker_id: "f6666666-6666-4666-8666-666666666666",
  name: "Synthetic projection account", default_currency: "USD", status: "ACTIVE", version: 1, opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN",
};
export const taxId = "a7777777-7777-4777-8777-777777777777"; export const feeId = "b8888888-8888-4888-8888-888888888888";
export const priorId = "c9999999-9999-4999-8999-999999999999";
export const query: ProjectionQuery = { as_of: "2026-11-02", eligibility_rule: "ORDINARY_PRE_EX_DATE", tax_effective_on: "2026-11-03", fee_effective_on: "2026-11-04" };
export const common = { event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id,
  portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: "9007199254740993", broker_version: "9223372036854775807" };
export const setting = { ...common, setting_no: "1", previous_id: null, effective_from: "2026-11-01", reason: "Synthetic explicit estimate",
  created_at: "2026-11-02T00:00:00Z", scope: "OWNER_ESTIMATE_ONLY" as const, current_use_scope: "REVALIDATION_REQUIRED" as const };
export const tax: Schemas["TaxSettingSelection"] = { ...common, effective_on: query.tax_effective_on, head_no: "1", head_id: taxId,
  tax_status: "ESTIMATED", selected: { ...setting, id: taxId, tax_status: "ESTIMATED", rate: "0.200000" }, selected_scope: "ACCOUNT_SCOPE_MATCH",
  scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED" };
export const fee: Schemas["FeeSettingSelection"] = { ...common, effective_on: query.fee_effective_on, head_no: "1", head_id: feeId,
  fee_status: "ESTIMATED", selected: { ...setting, id: feeId, fee_status: "ESTIMATED", fee: "0.500000", fee_currency: "USD" }, selected_scope: "ACCOUNT_SCOPE_MATCH",
  revision_rejection: "NOT_REJECTED", scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED" };
export const inputs: Schemas["ProjectionInputs"] = { ...common, ex_date: event.ex_date, currency: event.currency, portfolio_data_revision: "9007199254740993",
  projection_head_no: "9007199254740993", projection_head_id: priorId, as_of: query.as_of, eligibility_rule: query.eligibility_rule,
  tax: { effective_on: query.tax_effective_on, head_no: "1", head_id: taxId, selected_id: taxId, status: "ESTIMATED", selected_scope: "ACCOUNT_SCOPE_MATCH", scope: "OWNER_ESTIMATE_ONLY" },
  fee: { effective_on: query.fee_effective_on, head_no: "1", head_id: feeId, selected_id: feeId, status: "ESTIMATED", selected_scope: "ACCOUNT_SCOPE_MATCH", scope: "OWNER_ESTIMATE_ONLY" },
  rejection_state: "NOT_REJECTED", scope: "RECORDED_PROJECTION_INPUTS_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  source_scope: "CURRENT_SOURCE_NOT_REVALIDATED", eligibility_scope: "NOT_ESTABLISHED" };
export const target: ProjectionTarget = { event, account, inputs, tax, fee, query };
export const body: Schemas["ProjectionWrite"] = { revision_id: event.current_revision_id, portfolio_id: account.portfolio_id, broker_id: account.broker_id,
  account_version: inputs.account_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
  expected_projection_no: inputs.projection_head_no, expected_projection_id: priorId, as_of: query.as_of, eligibility_rule: query.eligibility_rule,
  tax_effective_on: query.tax_effective_on, expected_tax_head_no: "1", expected_tax_head_id: taxId, expected_tax_selected_id: taxId,
  fee_effective_on: query.fee_effective_on, expected_fee_head_no: "1", expected_fee_head_id: feeId, expected_fee_selected_id: feeId,
  fx_rate_hkd: null, fx_source: null, fee_rate_event: null, fee_rate_source: null, confirmation: "RECORDED_PROJECTION_ONLY", confirm_revision_fence: true,
  reason: "Synthetic recorded projection reason" };
export const record: Schemas["ProjectionRecord"] = { id: "daaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: inputs.account_version, broker_version: inputs.broker_version,
  portfolio_data_revision: inputs.portfolio_data_revision, projection_no: "9007199254740994", previous_id: priorId, state: "EXPECTED", as_of: query.as_of,
  ex_date: event.ex_date, timing_scope: "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY", currency: event.currency, eligibility_rule: query.eligibility_rule,
  retired_trade_scope: "NO_RETIRED_TRADES", creation_audit_scope: "NO_RECORDED_CREATION_GAP_REVIEW", recorded_shares: "10.00000000",
  estimated_amounts: { currency: "USD", gross_local: "10.000000", tax_local: "2.000000", fee_local: "0.500000", net_local: "7.500000", rounding_delta_local: "0.000000", estimated_hkd: null, quality: "MISSING_FX" },
  tax_rate: "0.200000", fee_original: "0.500000", fee_currency: "USD", fee_rate_event: "1.000000000000", fee_rate_source: "IDENTITY", fx_rate_hkd: null, fx_source: null,
  tax_provenance: { effective_on: query.tax_effective_on, selected_setting_id: taxId, observed_head_no: "1", observed_head_id: taxId, status: "ESTIMATED", scope: "OWNER_ESTIMATE_ONLY" },
  fee_provenance: { effective_on: query.fee_effective_on, selected_setting_id: feeId, observed_head_no: "1", observed_head_id: feeId, status: "ESTIMATED", scope: "OWNER_ESTIMATE_ONLY" },
  created_at: "2026-11-04T00:00:00Z", reason: body.reason, history_completeness: "UNVERIFIED", scope: "UNVERIFIED_PROJECTION_HISTORY_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED" };
