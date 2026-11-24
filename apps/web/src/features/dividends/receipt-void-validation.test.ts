import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { body, combinedReceipt, inputs, receipt, target, voided } from "./receipt-void-test-data";
import { checkedReceiptVoidInputs, checkedReceiptVoided, validReceiptVoidTarget } from "./receipt-void-validation";

type Schemas = components["schemas"];
const other = "77777777-7777-4777-8777-777777777777";

describe("recorded receipt void input and response checks", () => {
  it("accepts explicit combined legacy provenance without relabeling its audit scope", () => {
    expect(checkedReceiptVoidInputs(inputs, event, account, combinedReceipt)).toBe(inputs);
    expect(validReceiptVoidTarget({ ...target, receipt: combinedReceipt })).toBe(true);
    expect(checkedReceiptVoided(voided, { ...target, receipt: combinedReceipt }, body)).toBe(voided);
    expect(() => checkedReceiptVoidInputs(inputs, event, account, { ...combinedReceipt, audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" })).toThrow();
  });
  it("rejects a direct record even if supplied with existing legacy void inputs", () => {
    const direct: Schemas["CombinedRecordedReceipt"] = { ...combinedReceipt, receipt_basis: "DIRECT_EVENT_ACCOUNT", entitlement_id: null };
    expect(() => checkedReceiptVoidInputs(inputs, event, account, direct)).toThrow();
    expect(validReceiptVoidTarget({ ...target, receipt: direct })).toBe(false);
  });
  it("binds the selected receipt to exact current and retained scope without granting payment", () => {
    expect(checkedReceiptVoidInputs(inputs, event, account, receipt)).toBe(inputs);
    expect(validReceiptVoidTarget(target)).toBe(true);
    expect(checkedReceiptVoided(voided, target, body)).toBe(voided);
    expect(inputs.account_version).toBe("9007199254740993");
    expect(inputs.portfolio_data_revision).toBe("9223372036854775806");
  });

  it("permits archived-account corrections but not missing origins, void receipts or exhausted data revisions", () => {
    const archivedAccount = { ...account, status: "ARCHIVED" as const };
    const archivedInputs = { ...inputs, account_status: "ARCHIVED" as const };
    expect(validReceiptVoidTarget({ ...target, account: archivedAccount, inputs: archivedInputs })).toBe(true);
    const legacy = { ...inputs, audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" as const, cash_entry_id: null, origin_portfolio_id: null };
    expect(checkedReceiptVoidInputs(legacy, event, account, receipt)).toBe(legacy);
    expect(validReceiptVoidTarget({ ...target, inputs: legacy })).toBe(false);
    const oldReceipt = { ...receipt, status: "VOID" as const, version: "2", voided_at: voided.voided_at,
      cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" as const };
    const oldInputs = { ...inputs, status: "VOID" as const, receipt_version: "2", voided_at: voided.voided_at };
    expect(checkedReceiptVoidInputs(oldInputs, event, account, oldReceipt)).toBe(oldInputs);
    expect(validReceiptVoidTarget({ ...target, receipt: oldReceipt, inputs: oldInputs })).toBe(false);
    expect(validReceiptVoidTarget({ ...target, inputs: { ...inputs, portfolio_data_revision: "9223372036854775807" } })).toBe(false);
  });

  it("compares exact decimal values, timestamp instants and UUIDs without rounding", () => {
    const equivalent = { ...inputs, event_id: inputs.event_id.toUpperCase(), cash_entry_id: inputs.cash_entry_id!.toUpperCase(),
      received_amount: "006.125", fee: "0", fx_rate_hkd: "07.8", received_hkd: "047.7750",
      created_at: "2026-11-04T08:00:00+08:00" };
    expect(checkedReceiptVoidInputs(equivalent, event, account, receipt)).toBe(equivalent);
    expect(validReceiptVoidTarget({ ...target, inputs: equivalent })).toBe(true);
  });

  it("retains unknown FX, signed values and a moved account's original portfolio", () => {
    const unknown = { ...inputs, fx_rate_hkd: null, received_hkd: null };
    const unknownReceipt = { ...receipt, fx_rate_hkd: null, received_hkd: null };
    expect(validReceiptVoidTarget({ ...target, inputs: unknown, receipt: unknownReceipt })).toBe(true);
    const negative = { ...inputs, received_amount: "-6.125000", received_hkd: "-47.775000", origin_portfolio_id: other };
    const negativeReceipt = { ...receipt, recorded_amount: "-6.125000", received_hkd: "-47.775000" };
    const moved = { ...target, inputs: negative, receipt: negativeReceipt };
    expect(validReceiptVoidTarget(moved)).toBe(true);
    expect(checkedReceiptVoided({ ...voided, origin_portfolio_id: other }, moved, body)).toBeTruthy();
  });

  it("reads a nonreversible legacy amount without treating it as an actionable receipt", () => {
    const amount = "-9223372036854.775808";
    const legacy = { ...inputs, received_amount: amount, received_hkd: null, fx_rate_hkd: null,
      audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" as const, cash_entry_id: null, origin_portfolio_id: null };
    const original = { ...receipt, recorded_amount: amount, received_hkd: null, fx_rate_hkd: null };
    expect(checkedReceiptVoidInputs(legacy, event, account, original)).toBe(legacy);
    expect(validReceiptVoidTarget({ ...target, inputs: legacy, receipt: original })).toBe(false);
  });

  it.each([
    { currency: "HKD", fx_rate_hkd: "7.8", received_hkd: "47.775000" },
    { currency: "HKD", fx_rate_hkd: "1", received_hkd: "6.125001" },
    { currency: "USD", fx_rate_hkd: null, received_hkd: "47.775000" },
    { currency: "USD", fx_rate_hkd: "7.8", received_hkd: "-47.775000" },
  ])("rejects contradictory matched cash and FX even when both reads agree", change => {
    const value = { ...inputs, ...change } as Schemas["ReceiptVoidInputs"];
    const original = { ...receipt, currency: value.currency, fx_rate_hkd: value.fx_rate_hkd, received_hkd: value.received_hkd };
    expect(() => checkedReceiptVoidInputs(value, event, account, original)).toThrow(/無法安全核對/);
  });

  it.each([
    { receipt_id: other }, { event_id: other }, { observed_revision_id: other }, { observed_revision_no: 2 },
    { account_id: other }, { portfolio_id: other }, { broker_id: other }, { account_status: "ARCHIVED" },
    { account_version: "01" }, { portfolio_version: "9223372036854775808" }, { broker_version: "0" },
    { portfolio_data_revision: "-1" }, { entitlement_id: other }, { original_revision_id: other },
    { cash_entry_id: null }, { origin_portfolio_id: null }, { audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" },
    { received_date: "2026-11-05" }, { received_amount: "6.125001" }, { received_amount: 0.1 }, { currency: "HKD" },
    { tax: "0" }, { fee: null }, { fx_rate_hkd: null }, { received_hkd: "47.775001" },
    { status: "VOID" }, { receipt_version: "2" }, { created_at: "2026-11-04T00:00:01Z" }, { voided_at: "2026-11-05T00:00:00Z" },
    { audit_scope: "VERIFIED" }, { scope: "ELIGIBLE" }, { history_completeness: "COMPLETE" },
    { current_use_scope: "GRANTED" }, { eligibility_scope: "ESTABLISHED" }, { payment_scope: "VERIFIED" },
  ])("rejects malformed, stale or mismatched void input fields", change => {
    const broken = { ...inputs, ...change } as Schemas["ReceiptVoidInputs"];
    expect(() => checkedReceiptVoidInputs(broken, event, account, receipt)).toThrow(/無法安全核對/);
    expect(validReceiptVoidTarget({ ...target, inputs: broken })).toBe(false);
  });

  it("rejects an independently stale event or receipt head", () => {
    expect(() => checkedReceiptVoidInputs(inputs, { ...event, current_revision_id: other }, account, receipt)).toThrow(/無法安全核對/);
    expect(() => checkedReceiptVoidInputs(inputs, event, account, { ...receipt, current_revision_id: other })).toThrow(/無法安全核對/);
    expect(validReceiptVoidTarget({ ...target, receipt: { ...receipt, entitlement_id: other } })).toBe(false);
  });

  it.each([
    { id: other }, { event_id: other }, { observed_revision_id: other }, { observed_revision_no: 2 },
    { entitlement_id: other }, { original_revision_id: other }, { account_id: other }, { portfolio_id: other },
    { origin_portfolio_id: other }, { broker_id: other }, { account_version: "1" }, { portfolio_version: "1" },
    { broker_version: "1" }, { portfolio_data_revision: "0" }, { cash_entry_id: other },
    { reversal_entry_id: inputs.cash_entry_id }, { status: "ACTIVE" }, { version: "1" },
    { created_at: "2026-11-04T00:00:00.000001Z" }, { voided_at: "2026-11-03T23:59:59.999999Z" },
    { audit_scope: "VERIFIED" }, { scope: "CURRENT" }, { history_completeness: "COMPLETE" },
    { current_use_scope: "GRANTED" }, { eligibility_scope: "ESTABLISHED" }, { payment_scope: "VERIFIED" },
  ])("rejects a void response that does not match the frozen submission", change => {
    expect(() => checkedReceiptVoided({ ...voided, ...change } as Schemas["ReceiptVoided"], target, body)).toThrow(/不符/);
  });

  it("rejects substituted request pins and a forged reversal using a case-variant primary ID", () => {
    expect(() => checkedReceiptVoided(voided, target, { ...body, cash_entry_id: other })).toThrow(/不符/);
    expect(() => checkedReceiptVoided(voided, target, { ...body, expected_data_revision: "0" })).toThrow(/不符/);
    expect(() => checkedReceiptVoided({ ...voided, reversal_entry_id: body.cash_entry_id.toUpperCase() }, target, body)).toThrow(/不符/);
  });
});
