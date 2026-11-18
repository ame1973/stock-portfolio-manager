import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { checkedReceiptInputs, checkedReceiptSaved, receiptMoneyIntent, reversibleMoney, validReceiptTarget } from "./receipt-input-validation";
import { body, created, current, inputs, old, target } from "./receipt-test-data";

type Schemas = components["schemas"];

describe("recorded receipt input and response checks", () => {
  it("keeps exact scope pins and separately selected unverified historical basis", () => {
    expect(checkedReceiptInputs(inputs, event, account)).toBe(inputs);
    expect(validReceiptTarget(target)).toBe(true);
    expect(checkedReceiptSaved(created, target, body)).toBe(created);
    expect(inputs.account_version).toBe("9007199254740993"); expect(inputs.portfolio_version).toBe("9223372036854775807");
  });
  it.each([
    { event_id: account.id }, { account_id: event.id }, { revision_id: old.revision_id }, { revision_no: 2 },
    { portfolio_id: event.id }, { broker_id: event.id }, { account_status: "ARCHIVED" }, { account_version: "01" },
    { portfolio_version: "9223372036854775808" }, { portfolio_data_revision: "-1" },
    { scope: "ELIGIBLE" }, { eligibility_scope: "ESTABLISHED" }, { history_completeness: "COMPLETE" },
    { entitlements: { items: [old, current], next_cursor: null } },
    { entitlements: { items: [current, { ...old, id: current.id.toUpperCase() }], next_cursor: null } },
    { entitlements: { items: [{ ...old, revision_no: 4 }], next_cursor: null } },
    { entitlements: { items: [{ ...old, revision_id: event.current_revision_id }], next_cursor: null } },
    { entitlements: { items: [{ ...old, recorded_shares: "0" }], next_cursor: null } },
    { entitlements: { items: [{ ...old, recorded_expected_net: 0.1 }], next_cursor: null } },
    { entitlements: { items: [{ ...old, scope: "VERIFIED" }], next_cursor: null } },
    { entitlements: { items: [], next_cursor: "" } },
  ])("rejects malformed, stale, mismatched or invented input scope", change => {
    expect(() => checkedReceiptInputs({ ...inputs, ...change } as Schemas["ReceiptInputs"], event, account)).toThrow(/無法安全核對/);
  });
  it("requires one actually selected physical basis, active account and advanceable data revision", () => {
    expect(validReceiptTarget({ ...target, basis: { ...old, recorded_expected_net: "11.000000" } })).toBe(false);
    expect(validReceiptTarget({ ...target, basis: { ...old, id: "66666666-6666-4666-8666-666666666666" } })).toBe(false);
    expect(validReceiptTarget({ ...target, inputs: { ...inputs, portfolio_data_revision: "9223372036854775807" } })).toBe(false);
    expect(validReceiptTarget({ ...target, account: { ...account, status: "ARCHIVED" }, inputs: { ...inputs, account_status: "ARCHIVED" } })).toBe(false);
  });
  it("normalizes only exact decimal spellings and requires reversible INT64 amounts", () => {
    expect(receiptMoneyIntent("0006.1250")).toBe(receiptMoneyIntent("6.125000"));
    expect(receiptMoneyIntent("-0.000000")).toBe(receiptMoneyIntent("0")); expect(receiptMoneyIntent(null)).toBeNull();
    expect(receiptMoneyIntent("-9223372036854.775807")).toBe("-9223372036854775807");
    expect(reversibleMoney.safeParse("-9223372036854.775808").success).toBe(false);
    expect(reversibleMoney.safeParse("1.0000001").success).toBe(false);
  });
  it.each([
    { id: account.id }, { event_id: account.id }, { entitlement_id: current.id }, { original_revision_id: current.revision_id },
    { original_revision_no: 2 }, { observed_revision_id: old.revision_id }, { observed_revision_no: 2 }, { portfolio_id: event.id },
    { broker_id: event.id }, { account_version: "9007199254740992" }, { portfolio_version: "1" }, { portfolio_data_revision: "0" },
    { received_amount: "6.125001" }, { currency: "HKD" }, { received_date: "2026-11-05" }, { tax: "0" }, { fee: null },
    { fx_rate_hkd: null }, { fx_source: "Other source" }, { received_hkd: null }, { received_hkd: "-1" },
    { actual_hkd_override: true }, { status: "VOID" }, { version: "2" }, { audit_scope: "VERIFIED" },
    { eligibility_scope: "ESTABLISHED" }, { payment_scope: "VERIFIED" },
  ])("rejects a response that does not match the frozen submission", change => {
    expect(() => checkedReceiptSaved({ ...created, ...change } as Schemas["ReceiptCreated"], target, body)).toThrow(/不符/);
  });
  it("uses the server's Unicode code-point and whitespace rules for FX sources", () => {
    for (const value of ["💰".repeat(100), String.fromCharCode(0xfeff) + "Synthetic source"]) {
      expect(checkedReceiptSaved({ ...created, fx_source: value }, target, { ...body, fx_source: value })).toBeTruthy();
    }
    expect(() => checkedReceiptSaved({ ...created, fx_source: "\u0085Synthetic source" }, target,
      { ...body, fx_source: "\u0085Synthetic source" })).toThrow(/不符/);
  });
  it("distinguishes unknown FX, a reasoned override and HKD identity", () => {
    expect(checkedReceiptSaved({ ...created, fx_rate_hkd: null, fx_source: null, received_hkd: null }, target,
      { ...body, fx_rate_hkd: null, fx_source: null })).toBeTruthy();
    expect(checkedReceiptSaved({ ...created, actual_hkd_override: true, received_hkd: "45.600000" }, target,
      { ...body, actual_hkd: "45.6", actual_hkd_reason: "Synthetic broker actual HKD" })).toBeTruthy();
    expect(checkedReceiptSaved({ ...created, currency: "HKD", fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY", received_hkd: "6.125000" }, target,
      { ...body, currency: "HKD", fx_rate_hkd: null, fx_source: null })).toBeTruthy();
  });
});
