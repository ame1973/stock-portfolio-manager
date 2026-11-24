import { describe, expect, it, vi } from "vitest";
import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { body as legacyBody, combinedReceipt, inputs as legacyInputs, voided as legacyVoided } from "./receipt-void-test-data";
import { checkedDirectReceiptVoidInputs, checkedDirectReceiptVoided, validDirectReceiptVoidTarget } from "./direct-receipt-void-validation";

type Schemas = components["schemas"];
const other = "77777777-7777-4777-8777-777777777777";
const receipt: Schemas["CombinedRecordedReceipt"] = { ...combinedReceipt, receipt_basis: "DIRECT_EVENT_ACCOUNT", entitlement_id: null };
const current: Schemas["DirectReceiptInputs"] = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, workflow_status: event.workflow_status,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: account.status,
  account_version: legacyInputs.account_version, portfolio_version: legacyInputs.portfolio_version, broker_version: legacyInputs.broker_version,
  portfolio_data_revision: legacyInputs.portfolio_data_revision, rejection_state: "NOT_REJECTED", rejection_id: null,
  receipt_basis: "DIRECT_EVENT_ACCOUNT", scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
const inputs: Schemas["DirectReceiptVoidInputs"] = { current, receipt, cash_entry_id: legacyInputs.cash_entry_id!,
  origin_portfolio_id: account.portfolio_id, scope: "DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY" };
const target = { event, account, receipt, inputs };
const body: Schemas["DirectReceiptVoidWrite"] = {
  event_id: current.event_id, observed_revision_id: current.revision_id, observed_revision_no: current.revision_no,
  original_revision_id: receipt.revision_id, original_revision_no: receipt.revision_no, account_id: current.account_id,
  portfolio_id: current.portfolio_id, broker_id: current.broker_id, account_version: current.account_version,
  portfolio_version: current.portfolio_version, broker_version: current.broker_version, expected_data_revision: current.portfolio_data_revision,
  cash_entry_id: inputs.cash_entry_id, reason: legacyBody.reason, confirmation: "VOID_RECORDED_CASH",
};
const voided: Schemas["DirectReceiptVoided"] = {
  id: receipt.id, event_id: body.event_id, observed_revision_id: body.observed_revision_id, observed_revision_no: body.observed_revision_no,
  original_revision_id: body.original_revision_id, original_revision_no: body.original_revision_no, account_id: body.account_id,
  portfolio_id: body.portfolio_id, origin_portfolio_id: inputs.origin_portfolio_id, broker_id: body.broker_id,
  account_version: body.account_version, portfolio_version: body.portfolio_version, broker_version: body.broker_version,
  portfolio_data_revision: body.expected_data_revision, cash_entry_id: inputs.cash_entry_id, reversal_entry_id: legacyVoided.reversal_entry_id,
  receipt_basis: "DIRECT_EVENT_ACCOUNT", status: "VOID", version: "2", created_at: receipt.created_at, voided_at: legacyVoided.voided_at,
  audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", scope: "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};

describe("direct receipt void input and historical response validation", () => {
  it("binds original direct cash and exact current pins without inventing entitlement", () => {
    expect(checkedDirectReceiptVoidInputs(inputs, event, account, receipt)).toBe(inputs);
    expect(validDirectReceiptVoidTarget(target)).toBe(true);
    expect(checkedDirectReceiptVoided(voided, target, body)).toBe(voided);
    expect(body).not.toHaveProperty("entitlement_id"); expect(current.account_version).toBe("9007199254740993");
  });
  it("accepts archived accounts and a distinct original portfolio for correction", () => {
    const archived = { ...account, status: "ARCHIVED" as const };
    const value = { ...inputs, origin_portfolio_id: other, current: { ...current, account_status: "ARCHIVED" as const } };
    const moved = { ...target, account: archived, inputs: value };
    expect(validDirectReceiptVoidTarget(moved)).toBe(true);
    expect(checkedDirectReceiptVoided({ ...voided, origin_portfolio_id: other }, moved, body)).toBeTruthy();
  });
  it("does not confuse new-cash withdrawal or rejection restrictions with correction scope", () => {
    const observed = { ...event, workflow_status: "WITHDRAWN" as const };
    const value = { ...inputs, current: { ...current, workflow_status: "WITHDRAWN" as const, rejection_state: "REJECTED" as const, rejection_id: other } };
    expect(validDirectReceiptVoidTarget({ ...target, event: observed, inputs: value })).toBe(true);
  });
  it("reads a void or exhausted current scope without making it actionable", () => {
    const old = { ...receipt, status: "VOID" as const, version: "2", voided_at: voided.voided_at, cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" as const };
    const value = { ...inputs, receipt: old };
    expect(checkedDirectReceiptVoidInputs(value, event, account, old)).toBe(value);
    expect(validDirectReceiptVoidTarget({ ...target, receipt: old, inputs: value })).toBe(false);
    const exhausted = { ...inputs, current: { ...current, portfolio_data_revision: "9223372036854775807" } };
    expect(checkedDirectReceiptVoidInputs(exhausted, event, account, receipt)).toBe(exhausted);
    expect(validDirectReceiptVoidTarget({ ...target, inputs: exhausted })).toBe(false);
  });
  it("compares equivalent decimal values, microsecond instants and case-insensitive UUIDs", () => {
    const value = { ...inputs, receipt: { ...receipt, id: receipt.id.toUpperCase(), revision_id: receipt.revision_id.toUpperCase(),
      recorded_amount: "006.125", recorded_fee: "0", fx_rate_hkd: "07.8", received_hkd: "047.7750", created_at: "2026-11-04T08:00:00+08:00" } };
    expect(checkedDirectReceiptVoidInputs(value, event, account, receipt)).toBe(value);
    expect(checkedDirectReceiptVoided({ ...voided, created_at: "2026-11-04T08:00:00+08:00", cash_entry_id: inputs.cash_entry_id.toUpperCase() }, target,
      { ...body, event_id: body.event_id.toUpperCase() })).toBeTruthy();
  });
  it.each([
    { recorded_amount: "-9223372036854.775807", fx_rate_hkd: null, received_hkd: null },
    { recorded_amount: "0", fx_rate_hkd: "1", received_hkd: "0", currency: "HKD" as const },
    { recorded_amount: "-6.125", received_hkd: "-47.775" },
    { received_hkd: "48.0" },
  ])("preserves unknowns, signed limits, identity and possible original overrides", change => {
    const original = { ...receipt, ...change }; const value = { ...inputs, receipt: original };
    expect(validDirectReceiptVoidTarget({ ...target, receipt: original, inputs: value })).toBe(true);
  });
  it.each([null, undefined, [], {}, { ...inputs, current: null }, { ...inputs, receipt: null },
    { ...inputs, cash_entry_id: null }, { ...inputs, origin_portfolio_id: null }, { ...inputs, cash_entry_id: "not-a-uuid" },
    { ...inputs, scope: "RECORDED_RECEIPT_VOID_INPUTS_ONLY" }, { ...inputs, entitlement_id: other },
    { ...inputs, current: { ...current, account_version: 1 } }, { ...inputs, current: { ...current, rejection_id: other } },
    { ...inputs, current: { ...current, eligibility_scope: "ESTABLISHED" } },
  ])("fails closed on malformed or legacy-shaped input envelopes", broken => {
    const value = broken as Schemas["DirectReceiptVoidInputs"];
    expect(() => checkedDirectReceiptVoidInputs(value, event, account, receipt)).toThrow(/無法安全核對/);
    expect(validDirectReceiptVoidTarget({ ...target, inputs: value })).toBe(false);
  });
  it.each([
    { id: other }, { event_id: other }, { account_id: other }, { revision_id: other }, { revision_no: receipt.revision_no + 1 },
    { current_revision_id: other }, { current_revision_no: event.version + 1 }, { received_date: "2026-11-05" },
    { recorded_amount: "6.125001" }, { recorded_tax: "0" }, { recorded_fee: null }, { fx_rate_hkd: "7.81" }, { received_hkd: "47.775001" },
    { created_at: "2026-11-04T00:00:00.000001Z" }, { currency: "HKD" }, { entitlement_id: other }, { receipt_basis: "LEGACY_ENTITLEMENT" },
    { audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" }, { payment_scope: "VERIFIED" }, { version: "2" }, { status: "VOID" },
    { source: "IMPORT" }, { cash_scope: "CASH_NOT_RECORDED" }, { extra: "synthetic private value" },
  ])("rejects any selected-receipt mismatch or unsafe nested receipt", change => {
    const value = { ...inputs, receipt: { ...receipt, ...change } } as Schemas["DirectReceiptVoidInputs"];
    expect(() => checkedDirectReceiptVoidInputs(value, event, account, receipt)).toThrow(/無法安全核對/);
    expect(validDirectReceiptVoidTarget({ ...target, inputs: value })).toBe(false);
  });
  it.each([
    { recorded_amount: "-9223372036854.775808", received_hkd: null, fx_rate_hkd: null },
    { recorded_amount: 0.1 }, { recorded_amount: "0.0000001" }, { recorded_tax: "-1" },
    { received_hkd: "-47.775" }, { fx_rate_hkd: null }, { currency: "HKD" },
    { receipt_basis: "LEGACY_ENTITLEMENT", entitlement_id: other }, { audit_scope: "VERIFIED" },
  ])("rejects internally contradictory cash or provenance even when both reads match", change => {
    const original = { ...receipt, ...change } as Schemas["CombinedRecordedReceipt"];
    expect(validDirectReceiptVoidTarget({ ...target, receipt: original, inputs: { ...inputs, receipt: original } })).toBe(false);
  });
  it("rejects independent current-head changes and stale void lifecycles", () => {
    const successor = { ...receipt, current_revision_id: other, current_revision_no: event.version + 1 };
    expect(() => checkedDirectReceiptVoidInputs({ ...inputs, receipt: successor }, event, account, successor)).toThrow();
    expect(validDirectReceiptVoidTarget({ ...target, account: { ...account, portfolio_id: other } })).toBe(false);
    expect(validDirectReceiptVoidTarget({ ...target, receipt: combinedReceipt })).toBe(false);
    const old = { ...receipt, status: "VOID" as const, version: "2", voided_at: voided.voided_at, cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" as const };
    expect(() => checkedDirectReceiptVoidInputs({ ...inputs, receipt: old }, event, account, receipt)).toThrow();
    expect(() => checkedDirectReceiptVoidInputs({ ...inputs, receipt: old }, event, account, { ...old, voided_at: "2026-11-05T00:00:00.000001Z" })).toThrow();
  });
  it.each([
    { id: other }, { event_id: other }, { observed_revision_id: other }, { observed_revision_no: 2 },
    { original_revision_id: other }, { original_revision_no: receipt.revision_no + 1 }, { account_id: other }, { portfolio_id: other }, { origin_portfolio_id: other },
    { broker_id: other }, { account_version: "1" }, { portfolio_version: "1" }, { broker_version: "1" }, { portfolio_data_revision: "0" },
    { cash_entry_id: other }, { reversal_entry_id: inputs.cash_entry_id.toUpperCase() }, { reversal_entry_id: "not-a-uuid" },
    { created_at: "2026-11-04T00:00:00.000001Z" }, { voided_at: "2026-11-03T23:59:59.999999Z" },
    { receipt_basis: "LEGACY_ENTITLEMENT" }, { status: "ACTIVE" }, { version: "1" }, { scope: "HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY" },
    { audit_scope: "VERIFIED" }, { payment_scope: "VERIFIED" }, { history_completeness: "COMPLETE" }, { current_use_scope: "GRANTED" },
    { eligibility_scope: "ESTABLISHED" }, { entitlement_id: other }, { original_revision_no: 2.1 }, { account_version: 1 },
  ])("rejects a historical response that differs from the frozen direct intent", change => {
    expect(() => checkedDirectReceiptVoided({ ...voided, ...change } as Schemas["DirectReceiptVoided"], target, body)).toThrow(/不符/);
  });
  it.each([
    { event_id: other }, { observed_revision_id: other }, { observed_revision_no: 2 }, { original_revision_id: other }, { original_revision_no: receipt.revision_no + 1 },
    { account_id: other }, { portfolio_id: other }, { broker_id: other }, { account_version: "1" }, { portfolio_version: "1" }, { broker_version: "1" },
    { expected_data_revision: "0" }, { cash_entry_id: other }, { entitlement_id: other }, { reason: "short" }, { reason: "a".repeat(1001) },
    { reason: "Synthetic\u0000invalid reason" }, { confirmation: "RECORDED_CASH_ONLY" },
  ])("rejects malformed or substituted request pins before confirming a response", change => {
    expect(() => checkedDirectReceiptVoided(voided, target, { ...body, ...change } as Schemas["DirectReceiptVoidWrite"])).toThrow(/不符/);
  });
  it.each(Object.keys(voided))("requires historical response field %s", key => {
    const broken: Record<string, unknown> = { ...voided }; delete broken[key];
    expect(() => checkedDirectReceiptVoided(broken as Schemas["DirectReceiptVoided"], target, body)).toThrow(/不符/);
  });
  it.each(Object.keys(body))("requires submitted field %s", key => {
    const broken: Record<string, unknown> = { ...body }; delete broken[key];
    expect(() => checkedDirectReceiptVoided(voided, target, broken as Schemas["DirectReceiptVoidWrite"])).toThrow(/不符/);
  });
  it.each(Object.keys(inputs))("requires input envelope field %s", key => {
    const broken: Record<string, unknown> = { ...inputs }; delete broken[key];
    expect(() => checkedDirectReceiptVoidInputs(broken as Schemas["DirectReceiptVoidInputs"], event, account, receipt)).toThrow(/無法安全核對/);
  });
  it("does not use current wall time or silently accept a missing frozen target", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2000-01-01T00:00:00Z"));
      expect(checkedDirectReceiptVoided(voided, target, body)).toBe(voided);
    } finally { vi.useRealTimers(); }
    expect(() => checkedDirectReceiptVoided(voided, { ...target, inputs: { ...inputs, current: { ...current, portfolio_data_revision: "9223372036854775807" } } }, body)).toThrow();
    expect(() => checkedDirectReceiptVoided(null as unknown as Schemas["DirectReceiptVoided"], target, body)).toThrow(/不符/);
    expect(() => checkedDirectReceiptVoided(voided, target, null as unknown as Schemas["DirectReceiptVoidWrite"])).toThrow(/不符/);
  });
});
