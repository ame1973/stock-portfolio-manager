import type { components } from "@spm/contracts";
import { describe, expect, it } from "vitest";
import { account, event } from "./projection-test-data";
import { receipt } from "./receipt-void-test-data";
import { checkedCombinedReceiptPage } from "./receipt-validation";

type Receipt = components["schemas"]["CombinedRecordedReceipt"];
type Page = components["schemas"]["Page_CombinedRecordedReceipt_"];
const legacy: Receipt = { ...receipt, receipt_basis: "LEGACY_ENTITLEMENT", current_revision_no: event.version,
  scope: "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" };
const direct: Receipt = { ...legacy, id: "ffffffff-ffff-4fff-8fff-ffffffffffff", receipt_basis: "DIRECT_EVENT_ACCOUNT", entitlement_id: null,
  revision_id: event.current_revision_id, revision_no: event.version, audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", version: "1" };
const check = (items: unknown, next_cursor: unknown = null) => checkedCombinedReceiptPage({ items, next_cursor } as Page, event, account);

describe("combined recorded receipt response validation", () => {
  it("preserves direct and legacy identities without inventing entitlement or summing amounts", () => {
    const items = [direct, legacy]; const page = { items, next_cursor: null };
    expect(checkedCombinedReceiptPage(page, event, account)).toBe(page);
    expect(page.items[0]!.entitlement_id).toBeNull(); expect(page.items[1]!.entitlement_id).toBe(receipt.entitlement_id);
  });
  it("keeps exact INT64 strings, zero and unknown separate", () => {
    const item = { ...direct, recorded_amount: "9223372036854.775807", fx_rate_hkd: null, received_hkd: null, recorded_tax: null, recorded_fee: "0" };
    expect(check([item]).items[0]).toBe(item);
    expect(check([{ ...legacy, version: "9223372036854775807" }]).items[0]!.version).toBe("9223372036854775807");
    expect(check([]).items).toEqual([]);
  });
  it("accepts exact signed identity and original-FX void values without revaluing them", () => {
    expect(check([{ ...direct, currency: "HKD", recorded_amount: "-1", received_hkd: "-1.000000", fx_rate_hkd: "1" }]).items).toHaveLength(1);
    expect(check([{ ...direct, status: "VOID", version: "2", voided_at: "2026-11-07T00:00:00Z",
      cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" }]).items).toHaveLength(1);
  });
  it("accepts a consistent independently changed head without granting current use", () => {
    const item = { ...direct, current_revision_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", current_revision_no: event.version + 1 };
    expect(check([item]).items[0]).toBe(item);
  });
  it.each([
    { receipt_basis: "UNKNOWN" }, { entitlement_id: receipt.entitlement_id }, { audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" }, { source: "IMPORT" },
    { version: "2" }, { version: "01" }, { version: 1 }, { version: "9223372036854775808" }, { event_id: account.id }, { account_id: event.id },
    { revision_no: 0 }, { revision_no: "3" }, { revision_no: event.version + 1 }, { current_revision_no: event.version + 1 },
    { current_revision_no: "3" }, { current_revision_no: 2_147_483_648 }, { current_revision_id: account.id },
    { recorded_amount: 1 }, { recorded_amount: "NaN" }, { recorded_amount: "1e1" }, { recorded_amount: "0.0000001" },
    { recorded_amount: "9223372036854.775808" }, { recorded_amount: "-9223372036854.775808" }, { received_hkd: "-9223372036854.775808" },
    { recorded_tax: "-1" }, { recorded_fee: 0 }, { currency: "CNY" }, { fx_rate_hkd: "0" }, { fx_rate_hkd: "1", received_hkd: null },
    { fx_rate_hkd: null, received_hkd: "1" }, { fx_rate_hkd: "1", received_hkd: "-1" },
    { currency: "HKD", fx_rate_hkd: "2", received_hkd: direct.recorded_amount },
    { currency: "HKD", fx_rate_hkd: "1", received_hkd: "999" }, { recorded_amount: "0", fx_rate_hkd: "1", received_hkd: "1" },
    { cash_scope: "CASH_NOT_RECORDED" }, { cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" },
    { status: "VOID", version: "2" }, { voided_at: "2026-11-07T00:00:00Z" },
    { status: "VOID", version: "2", voided_at: "2020-01-01T00:00:00Z", cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" },
    { payment_scope: "VERIFIED" }, { eligibility_scope: "ESTABLISHED" }, { history_completeness: "COMPLETE" }, { current_use_scope: "GRANTED" },
    { scope: "UNVERIFIED_RECEIPT_HISTORY_ONLY" }, { created_at: "private" }, { extra_private_field: "private" },
  ])("rejects malformed or contradictory direct history", change => {
    expect(() => check([{ ...direct, ...change }])).toThrow("收款紀錄範圍無法安全核對");
  });
  it.each([
    { entitlement_id: null }, { receipt_basis: "DIRECT_EVENT_ACCOUNT" },
    { audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", fx_rate_hkd: "1", received_hkd: "-1" },
    { audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", cash_scope: "CASH_NOT_RECORDED" },
    { audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", version: "9007199254740993" },
  ])("does not relabel invalid legacy provenance as matched", change => {
    expect(() => check([{ ...legacy, ...change }])).toThrow();
  });
  it.each([
    [direct, { ...direct, id: direct.id.toUpperCase() }], [legacy, direct],
    [direct, { ...legacy, current_revision_no: event.version + 1 }],
    [direct, { ...legacy, revision_no: event.version }],
    [direct, { ...legacy, revision_id: direct.revision_id }],
    [legacy, { ...legacy, id: "11111111-1111-4111-8111-111111111111", entitlement_id: account.id }],
    [direct, { ...legacy, revision_id: account.id, revision_no: event.version + 1 }],
  ])("rejects duplicate, unordered and contradictory page identities", (...items) => {
    expect(() => check(items)).toThrow();
  });
  it.each(["", 1, "a".repeat(1501)])("rejects unusable cursors", cursor => { expect(() => check([direct], cursor)).toThrow(); });
  it("rejects invalid pages and scope instead of throwing private parser details", () => {
    for (const items of [null, {}, [null], Array(26).fill(direct)]) expect(() => check(items)).toThrow("收款紀錄範圍無法安全核對");
    expect(() => checkedCombinedReceiptPage({ items: [] }, { ...event, version: 0 }, account)).toThrow();
  });
});
