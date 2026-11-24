import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { reconciliation } from "./reconciliation-test-data";
import { checkedReconciliation } from "./reconciliation-validation";

type Record = components["schemas"]["RecordedReconciliation"];
const other = "77777777-7777-4777-8777-777777777777";

describe("recorded reconciliation response checks", () => {
  it("checks exact amounts and pins without claiming payment or income", () => {
    expect(checkedReconciliation(reconciliation, event, account)).toBe(reconciliation);
    expect(reconciliation.projection_no).toBe("9007199254740993");
  });

  it("accepts exact equivalent decimal strings and UUID case", () => {
    const value = { ...reconciliation, event_id: event.id.toUpperCase(), account_id: account.id.toUpperCase(),
      revision_id: event.current_revision_id.toUpperCase(), recorded_expected: "0011.00", recorded_received: "006", recorded_outstanding: "05.0" };
    expect(checkedReconciliation(value, event, account)).toBe(value);
  });

  it("accepts an independently changed event head for an explicit stale warning", () => {
    const newer = { ...reconciliation, revision_id: other, revision_no: event.version + 1 };
    expect(checkedReconciliation(newer, event, account)).toBe(newer);
    expect(() => checkedReconciliation({ ...newer, revision_no: event.version }, event, account)).toThrow(/無法安全核對/);
    expect(() => checkedReconciliation({ ...newer, revision_id: event.current_revision_id }, event, account)).toThrow(/無法安全核對/);
  });

  it("preserves the full signed INT64 wire value without using Number", () => {
    const value = { ...reconciliation, recorded_expected: "11.000000", recorded_received: "9223372036854.775807",
      recorded_outstanding: "-9223372036843.775807", state: "RECORDED_OVER_RECEIVED" as const, comparison_quality: "NEEDS_REVIEW" as const };
    expect(checkedReconciliation(value, event, account)).toBe(value);
  });

  it("retains the minimum signed INT64 result without rounding", () => {
    const value = { ...reconciliation, recorded_expected: "-0.000001", recorded_received: "9223372036854.775807",
      recorded_outstanding: "-9223372036854.775808", state: "RECORDED_NEEDS_REVIEW" as const,
      projection_quality: "NEEDS_REVIEW" as const, comparison_quality: "NEEDS_REVIEW" as const };
    expect(checkedReconciliation(value, event, account)).toBe(value);
  });

  it("hides malformed input and does not echo its private diagnostic", () => {
    expect(() => checkedReconciliation(null as unknown as Record, event, account)).toThrow(/無法安全核對/);
    expect(() => checkedReconciliation(reconciliation, { ...event, id: "synthetic-private" }, account)).toThrow(/無法安全核對/);
    expect(() => checkedReconciliation(reconciliation, event, { ...account, id: "synthetic-private" })).toThrow(/無法安全核對/);
    const broken = { ...reconciliation, recorded_expected: "synthetic-private" };
    expect(() => checkedReconciliation(broken, event, account)).toThrow(/無法安全核對/);
    try { checkedReconciliation(broken, event, account); }
    catch (error) { expect(String(error)).not.toContain("synthetic-private"); }
  });

  it.each([
    { state: "NO_RECORDED_PROJECTION", projection_id: null, projection_no: null, projection_as_of: null, projection_state: null, projection_quality: null,
      comparison_quality: null, currency: null, recorded_expected: null, recorded_received: null, recorded_outstanding: null },
    { state: "STALE_RECORDED_PROJECTION", comparison_quality: null, currency: null, recorded_expected: null, recorded_received: null, recorded_outstanding: null },
    { state: "PROJECTION_NOT_COMPARABLE", projection_state: "NEEDS_REVIEW", projection_quality: null, comparison_quality: null,
      currency: null, recorded_expected: null, recorded_received: null, recorded_outstanding: null },
    { state: "CASH_LINK_UNAVAILABLE", unavailable_cash_links: "1", comparison_quality: null, recorded_received: null, recorded_outstanding: null },
    { state: "NO_RECORDED_ACTIVE_RECEIPTS", active_receipts: "0", unverified_origins: "0", comparison_quality: null, recorded_received: null, recorded_outstanding: null },
    { state: "CURRENCY_REVIEW", comparison_quality: "NEEDS_REVIEW", recorded_received: null, recorded_outstanding: null },
    { state: "RECORDED_RECEIVED", recorded_expected: "0", recorded_received: "0", recorded_outstanding: "0" },
    { state: "RECORDED_OVER_RECEIVED", recorded_received: "12", recorded_outstanding: "-1", comparison_quality: "NEEDS_REVIEW" },
    { state: "RECORDED_NEEDS_REVIEW", recorded_received: "-1", recorded_outstanding: "12", comparison_quality: "NEEDS_REVIEW" },
    { projection_quality: "MISSING_FX", comparison_quality: "MISSING_FX" },
    { projection_quality: "NEEDS_REVIEW", comparison_quality: "NEEDS_REVIEW" },
  ])("preserves distinct recorded states and quality", change => {
    const value = { ...reconciliation, ...change } as Record;
    expect(checkedReconciliation(value, event, account)).toBe(value);
  });

  it.each([
    { event_id: other }, { account_id: other }, { revision_id: other }, { revision_no: 2 },
    { projection_id: null }, { projection_no: null }, { projection_no: "0" }, { projection_no: "01" },
    { projection_no: "9223372036854775808" }, { projection_no: 9007199254740992 }, { projection_as_of: "2026-02-30" },
    { projection_state: "NEEDS_REVIEW" }, { projection_quality: null }, { comparison_quality: "MISSING_FX" },
    { recorded_expected: null }, { recorded_received: null }, { recorded_outstanding: null }, { currency: null },
    { recorded_received: 0.1 }, { recorded_received: "6.0000001" }, { recorded_received: "9223372036854.775808" },
    { recorded_received: "6.000001" }, { active_receipts: "0", unverified_origins: "0" }, { active_receipts: "01" },
    { unverified_origins: "2" }, { unavailable_cash_links: "2" }, { unavailable_cash_links: "1" },
    { state: "NO_RECORDED_PROJECTION" }, { state: "STALE_RECORDED_PROJECTION" }, { state: "PROJECTION_NOT_COMPARABLE" },
    { state: "CASH_LINK_UNAVAILABLE" }, { state: "NO_RECORDED_ACTIVE_RECEIPTS" }, { state: "CURRENCY_REVIEW" },
    { state: "RECORDED_RECEIVED" }, { state: "RECORDED_OVER_RECEIVED" }, { state: "RECORDED_NEEDS_REVIEW" },
    { recorded_expected: "-1", recorded_received: "0", recorded_outstanding: "-1" },
    { recorded_received: "-1", recorded_outstanding: "12" }, { recorded_received: "11", recorded_outstanding: "0" },
    { scope: "ELIGIBLE" }, { history_completeness: "COMPLETE" }, { current_use_scope: "GRANTED" },
    { eligibility_scope: "ESTABLISHED" }, { payment_scope: "VERIFIED" }, { extra_private_data: "synthetic-private" },
  ])("rejects inconsistent or unsafe response fields", change => {
    expect(() => checkedReconciliation({ ...reconciliation, ...change } as Record, event, account)).toThrow(/無法安全核對/);
  });
});
