import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { checkedDirectReceiptSaved } from "./direct-receipt-write-validation";
import { account, event } from "./projection-test-data";
import type { DirectReceiptTarget } from "./direct-receipt-input-validation";

type Schemas = components["schemas"];
const target: DirectReceiptTarget = { event, account, inputs: {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, workflow_status: event.workflow_status,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE",
  account_version: "9007199254740993", portfolio_version: "9223372036854775807", broker_version: "2", portfolio_data_revision: "9223372036854775806",
  rejection_state: "NOT_REJECTED", rejection_id: null, receipt_basis: "DIRECT_EVENT_ACCOUNT", scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
} };
const body: Schemas["DirectReceiptCreate"] = {
  receipt_id: "44444444-4444-4444-8444-444444444444", event_id: event.id, revision_id: event.current_revision_id,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id,
  account_version: target.inputs.account_version, portfolio_version: target.inputs.portfolio_version, broker_version: target.inputs.broker_version,
  expected_data_revision: target.inputs.portfolio_data_revision, received_date: "2026-11-04", received_amount: "6.125", currency: "USD",
  tax: null, fee: "0", fx_rate_hkd: "7.8", fx_source: "Synthetic historical FX", actual_hkd: null, actual_hkd_reason: null,
  note: "Synthetic note", reason: "Synthetic recorded receipt reason", confirmation: "RECORDED_CASH_ONLY", confirm_revision_fence: true,
};
const created: Schemas["DirectReceiptCreated"] = {
  id: body.receipt_id, event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id,
  account_version: target.inputs.account_version, portfolio_version: target.inputs.portfolio_version, broker_version: target.inputs.broker_version,
  portfolio_data_revision: target.inputs.portfolio_data_revision, cash_entry_id: "55555555-5555-4555-8555-555555555555",
  received_date: body.received_date, received_amount: "6.125000", currency: "USD", tax: null, fee: "0.000000",
  fx_rate_hkd: "7.800000000000", fx_source: body.fx_source, received_hkd: "47.775000", actual_hkd: null, actual_hkd_reason: null,
  receipt_basis: "DIRECT_EVENT_ACCOUNT", source: "MANUAL", status: "ACTIVE", version: "1", created_at: "2026-11-04T00:00:00Z",
  audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", scope: "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
const other = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

describe("direct receipt creation response", () => {
  it("retains original exact cash and scope without a legacy entitlement", () => {
    expect(checkedDirectReceiptSaved(created, target, body)).toBe(created);
    expect(created.account_version).toBe("9007199254740993");
  });
  it("compares equivalent decimal spellings without altering returned strings", () => {
    expect(checkedDirectReceiptSaved(created, target, { ...body, received_amount: "006.1250", fee: "00.0", fx_rate_hkd: "07.800" })).toBe(created);
  });
  it.each([
    ["id", other], ["event_id", other], ["revision_id", other], ["revision_no", 1], ["revision_no", "2"], ["account_id", other],
    ["portfolio_id", other], ["broker_id", other], ["account_version", "1"], ["portfolio_version", "1"], ["broker_version", "1"],
    ["portfolio_data_revision", "0"], ["received_date", "2026-11-03"], ["received_amount", "6.125001"], ["received_amount", 6.125],
    ["currency", "HKD"], ["tax", "0"], ["fee", null], ["fx_rate_hkd", "7.9"], ["fx_source", "another source"],
    ["received_hkd", "47.775001"], ["received_hkd", "-47.775"], ["received_hkd", null], ["actual_hkd", "47.775"],
    ["actual_hkd_reason", "Synthetic forged override reason"], ["receipt_basis", "LEGACY_ENTITLEMENT"], ["source", "IMPORT"],
    ["status", "VOID"], ["version", "2"], ["version", 1], ["created_at", "not a timestamp"], ["audit_scope", "VERIFIED"],
    ["scope", "HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"], ["history_completeness", "COMPLETE"], ["current_use_scope", "GRANTED"],
    ["eligibility_scope", "ESTABLISHED"], ["payment_scope", "VERIFIED"], ["entitlement_id", null], ["actual_hkd_override", false],
  ])("rejects substituted %s=%s", (field, value) => {
    expect(() => checkedDirectReceiptSaved({ ...created, [field]: value } as Schemas["DirectReceiptCreated"], target, body)).toThrow(/不會假定未儲存/);
  });
  it.each(Object.keys(created))("requires explicit response field %s", field => {
    const value = { ...created } as Record<string, unknown>; delete value[field];
    expect(() => checkedDirectReceiptSaved(value as Schemas["DirectReceiptCreated"], target, body)).toThrow();
  });
  it.each([
    ["receipt_id", other], ["event_id", other], ["revision_id", other], ["account_id", other], ["portfolio_id", other], ["broker_id", other],
    ["account_version", "1"], ["portfolio_version", "1"], ["broker_version", "1"], ["expected_data_revision", "0"],
    ["confirmation", "CONFIRMED"], ["confirm_revision_fence", false], ["confirm_revision_fence", "true"], ["entitlement_id", other],
    ["received_amount", "6.1250001"], ["received_amount", "-9223372036854.775808"], ["tax", "-1"], ["fee", undefined],
    ["fx_rate_hkd", "7.8000000000001"], ["fx_source", "IDENTITY"], ["note", "bad\0note"], ["reason", "short"],
  ])("rejects an invalid or changed request field %s", (field, value) => {
    expect(() => checkedDirectReceiptSaved(created, target, { ...body, [field]: value } as Schemas["DirectReceiptCreate"])).toThrow();
  });
  it("keeps missing foreign FX and unknown tax/fees distinct from zero", () => {
    const request = { ...body, fx_rate_hkd: null, fx_source: null, fee: null };
    const result = { ...created, fx_rate_hkd: null, fx_source: null, received_hkd: null, fee: null };
    expect(checkedDirectReceiptSaved(result, target, request)).toBe(result);
    expect(() => checkedDirectReceiptSaved({ ...result, received_hkd: "0" }, target, request)).toThrow();
  });
  it("permits HKD identity only and retains the original actual-HKD reason", () => {
    const request = { ...body, currency: "HKD" as const, fx_rate_hkd: null, fx_source: null, actual_hkd: "6.125", actual_hkd_reason: " Synthetic original reason " };
    const result = { ...created, currency: "HKD" as const, fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY", received_hkd: "6.125000", actual_hkd: "6.125000", actual_hkd_reason: "Synthetic original reason" };
    expect(checkedDirectReceiptSaved(result, target, request)).toBe(result);
    expect(() => checkedDirectReceiptSaved({ ...result, received_hkd: "6.2", actual_hkd: "6.2" }, target, { ...request, actual_hkd: "6.2" })).toThrow();
  });
  it("retains a reasoned USD actual-HKD override without recalculating it", () => {
    const request = { ...body, actual_hkd: "48", actual_hkd_reason: "Synthetic actual HKD reason" };
    const result = { ...created, actual_hkd: "48.000000", actual_hkd_reason: request.actual_hkd_reason, received_hkd: "48.000000" };
    expect(checkedDirectReceiptSaved(result, target, request)).toBe(result);
    expect(() => checkedDirectReceiptSaved({ ...result, actual_hkd_reason: "Different actual HKD reason" }, target, request)).toThrow();
    expect(() => checkedDirectReceiptSaved({ ...result, received_hkd: "47.775" }, target, request)).toThrow();
  });
  it.each([
    ["0.000001", "0.499999999999", "0.000000"], ["0.000001", "0.5", "0.000001"],
    ["-0.000001", "0.5", "-0.000001"], ["0.000003", "0.5", "0.000002"], ["-0.000003", "0.5", "-0.000002"],
    ["0", "7.8", "0"], ["9223372036854.775807", "1", "9223372036854.775807"],
  ])("checks ROUND_HALF_UP at the six-decimal boundary for %s × %s", (amount, rate, hkd) => {
    const request = { ...body, received_amount: amount, fx_rate_hkd: rate };
    const result = { ...created, received_amount: amount, fx_rate_hkd: rate, received_hkd: hkd };
    expect(checkedDirectReceiptSaved(result, target, request)).toBe(result);
  });
  it("rejects derived overflow even if a small actual override was supplied", () => {
    const request = { ...body, received_amount: "9223372036854.775807", fx_rate_hkd: "2", actual_hkd: "1", actual_hkd_reason: "Synthetic actual override reason" };
    const result = { ...created, received_amount: request.received_amount, fx_rate_hkd: "2", actual_hkd: "1", actual_hkd_reason: request.actual_hkd_reason, received_hkd: "1" };
    expect(() => checkedDirectReceiptSaved(result, target, request)).toThrow();
  });
  it.each([
    { received_amount: "0", actual_hkd: "1", actual_hkd_reason: "Synthetic actual HKD reason", received_hkd: "1" },
    { received_amount: "-1", actual_hkd: "1", actual_hkd_reason: "Synthetic actual HKD reason", received_hkd: "1" },
    { received_amount: "1", actual_hkd: "-1", actual_hkd_reason: "Synthetic actual HKD reason", received_hkd: "-1" },
    { actual_hkd: "48", actual_hkd_reason: null, received_hkd: "48" },
    { actual_hkd: null, actual_hkd_reason: "Synthetic unexpected override reason" },
    { fx_rate_hkd: null, fx_source: "Synthetic orphaned source", received_hkd: null },
    { fx_rate_hkd: "7.8", fx_source: null },
    { fx_source: "IDENTITY" },
    { fx_rate_hkd: null, fx_source: null, actual_hkd: "48", actual_hkd_reason: "Synthetic missing rate reason", received_hkd: "48" },
  ])("rejects mutually consistent request/response with invalid cash components %j", changes => {
    const { received_hkd, ...requestChanges } = changes;
    const request = { ...body, ...requestChanges };
    const result = { ...created, ...requestChanges, ...(received_hkd === undefined ? {} : { received_hkd }) };
    expect(() => checkedDirectReceiptSaved(result, target, request)).toThrow();
  });
  it.each([
    { fx_rate_hkd: "2", fx_source: "IDENTITY" },
    { fx_rate_hkd: "1", fx_source: "Synthetic foreign source" },
  ])("rejects nonidentity HKD requests even when the response has identity %j", changes => {
    const request = { ...body, currency: "HKD" as const, ...changes };
    const result = { ...created, currency: "HKD" as const, fx_rate_hkd: "1", fx_source: "IDENTITY", received_hkd: "6.125" };
    expect(() => checkedDirectReceiptSaved(result, target, request)).toThrow();
  });
  it("does not depend on the current clock when validating the original response", () => {
    const request = { ...body, received_date: "2099-01-01" };
    const result = { ...created, received_date: request.received_date, created_at: "2099-01-02T00:00:00Z" };
    expect(checkedDirectReceiptSaved(result, target, request)).toBe(result);
  });
  it("requires the original frozen target, not a later rejected scope", () => {
    expect(() => checkedDirectReceiptSaved(created, { ...target, inputs: { ...target.inputs, rejection_state: "REJECTED", rejection_id: other } }, body)).toThrow();
  });
});
