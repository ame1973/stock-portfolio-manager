import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";
import { checkedRevisionPreview } from "./revision-preview-validation";

type Schemas = components["schemas"];
type Request = Schemas["PreviewRequest"];
type Page = Schemas["PreviewPage"];
const second = { ...account, id: "f7777777-7777-4777-8777-777777777777", name: "Synthetic empty account" };
const old = "a8888888-8888-4888-8888-888888888888";
const token = "a".repeat(64);
const estimates: Schemas["AccountEstimate"][] = [account, second].map(value => ({
  account_id: value.id, tax_rate: "0", fee: "0", fee_currency: "USD",
  fee_rate_event: null, fee_rate_source: null, fx_rate_hkd: "8", fx_source: "Synthetic explicit source",
}));
const request: Request = {
  revision_id: event.current_revision_id, limit: 1, after_account_id: null, snapshot_token: null,
  proposal: { ex_date: "2026-11-03", as_of: "2026-11-03", eligibility_rule: "ORDINARY_PRE_EX_DATE",
    dps: "1.1", currency: "USD", adjustment_basis: "AS_DECLARED", accounts: estimates },
};
const page: Page = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, security_id: event.security_id,
  terms: { ex_date: request.proposal.ex_date, as_of: request.proposal.as_of, eligibility_rule: request.proposal.eligibility_rule,
    dps: request.proposal.dps, currency: request.proposal.currency, adjustment_basis: request.proposal.adjustment_basis },
  items: [{
    account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE", broker_status: "ACTIVE",
    account_version: "9007199254740993", broker_version: "2", portfolio_version: "3", portfolio_data_revision: "4",
    retired_trade_scope: "NO_RETIRED_TRADES", creation_audit_scope: "NO_RECORDED_CREATION_GAP_REVIEW",
    eligibility_state: "RECORDED_PRE_EX_DATE", recorded_quantity: "10.00000000", eligibility_reasons: [], pending_action_ids: [], estimate: estimates[0]!,
    previous_projection_id: old, previous_revision_id: event.current_revision_id, previous_as_of: "2026-11-03", previous_currency: "USD",
    previous_expected: "10.000000", proposed_amounts: { currency: "USD", gross_local: "11.000000", tax_local: "0.000000", fee_local: "0.000000",
      net_local: "11.000000", rounding_delta_local: "0.000000", estimated_hkd: "88.000000", quality: "OK" },
    expected_delta: "1.000000", recorded_received: "6.000000", recorded_outstanding: "5.000000", active_receipts: "1", void_receipts: "0",
    unverified_origins: "0", unavailable_cash_links: "0", comparison_quality: "OK", state: "RECORDED_PARTIAL",
  }],
  snapshot_token: token, next_after_account_id: account.id, scope: "HYPOTHETICAL_RECORDED_COMPARISON_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
const accounts = [account, second];

describe("hypothetical revision preview validation", () => {
  it("binds exact amounts, observed event and account scope without granting eligibility", () => {
    expect(checkedRevisionPreview(page, event, accounts, request)).toBe(page);
    expect(page.items[0]?.account_version).toBe("9007199254740993");
    expect(page.items[0]?.recorded_outstanding).toBe("5.000000");
  });

  it("accepts equivalent decimals and case-insensitive UUIDs", () => {
    const value = structuredClone(page);
    value.event_id = event.id.toUpperCase(); value.revision_id = event.current_revision_id.toUpperCase();
    value.items[0]!.recorded_received = "006.00"; value.items[0]!.recorded_outstanding = "05";
    value.items[0]!.estimate.tax_rate = "0.0000";
    expect(checkedRevisionPreview(value, event, accounts, request)).toBe(value);
  });

  it("accepts the matching second page, but never merges changed tokens or account order", () => {
    const next = { ...request, after_account_id: account.id, snapshot_token: token };
    const item: Schemas["PreviewAccount"] = { ...page.items[0]!, account_id: second.id, estimate: estimates[1]!, recorded_quantity: "0",
      previous_projection_id: null, previous_revision_id: null, previous_as_of: null, previous_currency: null, previous_expected: null,
      proposed_amounts: null, expected_delta: null, recorded_received: null, recorded_outstanding: null, active_receipts: "0", state: "NO_RECORDED_POSITION",
      comparison_quality: null };
    const nextPage: Page = { ...page, items: [item], next_after_account_id: null };
    expect(checkedRevisionPreview(nextPage, event, accounts, next)).toBe(nextPage);
    expect(() => checkedRevisionPreview({ ...nextPage, snapshot_token: "b".repeat(64) }, event, accounts, next)).toThrow(/無法安全核對/);
    expect(() => checkedRevisionPreview({ ...nextPage, items: page.items }, event, accounts, next)).toThrow(/無法安全核對/);
    expect(() => checkedRevisionPreview({ ...nextPage, next_after_account_id: second.id }, event, accounts, next)).toThrow(/無法安全核對/);
  });

  it("retains missing HKD FX without suppressing the native-currency result", () => {
    const body = structuredClone(request); body.proposal.accounts[0]!.fx_rate_hkd = null; body.proposal.accounts[0]!.fx_source = null;
    const value = structuredClone(page); const item = value.items[0]!;
    item.estimate.fx_rate_hkd = null; item.estimate.fx_source = null; item.proposed_amounts!.estimated_hkd = null;
    item.proposed_amounts!.quality = "MISSING_FX"; item.comparison_quality = "MISSING_FX";
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
  });

  it("keeps empty receipts and mixed currencies distinct from confirmed zero", () => {
    for (const [state, active, quality] of [["NO_RECORDED_ACTIVE_RECEIPTS", "0", null], ["CURRENCY_REVIEW", "1", "NEEDS_REVIEW"]] as const) {
      const value = structuredClone(page); Object.assign(value.items[0]!, { state, active_receipts: active, comparison_quality: quality,
        recorded_received: null, recorded_outstanding: null });
      expect(checkedRevisionPreview(value, event, accounts, request)).toBe(value);
      value.items[0]!.recorded_received = "0";
      expect(() => checkedRevisionPreview(value, event, accounts, request)).toThrow(/無法安全核對/);
    }
  });

  it.each([
    { event_id: second.id }, { revision_id: second.id }, { revision_no: 2 }, { security_id: second.id },
    { snapshot_token: "not-token" }, { next_after_account_id: second.id }, { next_after_account_id: null },
    { scope: "CONFIRMED" }, { history_completeness: "COMPLETE" }, { eligibility_scope: "ESTABLISHED" },
    { current_use_scope: "GRANTED" }, { payment_scope: "VERIFIED" }, { private_extra: "synthetic-private" },
  ])("rejects a mismatched page scope", changes => {
    expect(() => checkedRevisionPreview({ ...page, ...changes } as Page, event, accounts, request)).toThrow(/無法安全核對/);
  });

  it.each([
    { account_id: second.id }, { portfolio_id: second.id }, { broker_id: second.id }, { account_version: "01" },
    { account_version: "9223372036854775808" }, { portfolio_data_revision: 5 }, { recorded_quantity: 10 },
    { recorded_quantity: "10.000000001" }, { eligibility_state: "NEEDS_REVIEW" }, { previous_projection_id: null },
    { previous_revision_id: null }, { previous_as_of: null }, { previous_currency: null }, { previous_expected: null },
    { expected_delta: "2" }, { recorded_received: "7" }, { recorded_outstanding: "4" }, { active_receipts: "0" },
    { unverified_origins: "2" }, { unavailable_cash_links: "2" }, { comparison_quality: "MISSING_FX" },
    { state: "RECORDED_RECEIVED" }, { private_extra: "synthetic-private" },
  ])("rejects inconsistent account scope, counts and comparison", changes => {
    const value = { ...page, items: [{ ...page.items[0]!, ...changes }] } as Page;
    expect(() => checkedRevisionPreview(value, event, accounts, request)).toThrow(/無法安全核對/);
  });

  it.each([
    { gross_local: "11.000001" }, { tax_local: "1" }, { fee_local: "1" }, { net_local: "11.000001" },
    { rounding_delta_local: "0.000001" }, { estimated_hkd: "87.999999" }, { quality: "NEEDS_REVIEW" }, { currency: "HKD" },
  ])("independently recomputes hypothetical amounts using exact integers", changes => {
    const value = structuredClone(page); Object.assign(value.items[0]!.proposed_amounts!, changes);
    expect(() => checkedRevisionPreview(value, event, accounts, request)).toThrow(/無法安全核對/);
  });

  it("rounds gross, tax, net and HKD independently only at the output boundary", () => {
    const body = structuredClone(request); body.proposal.dps = "0.00000050"; body.proposal.accounts[0]!.tax_rate = "0.5";
    const value = structuredClone(page); value.terms.dps = body.proposal.dps; const item = value.items[0]!;
    item.recorded_quantity = "1"; item.estimate.tax_rate = "0.5"; item.previous_projection_id = null; item.previous_revision_id = null;
    item.previous_as_of = null; item.previous_currency = null; item.previous_expected = null; item.expected_delta = null;
    item.proposed_amounts = { currency: "USD", gross_local: "0.000001", tax_local: "0", fee_local: "0", net_local: "0",
      rounding_delta_local: "-0.000001", estimated_hkd: "0.000002", quality: "OK" };
    item.active_receipts = "0"; item.recorded_received = null; item.recorded_outstanding = null; item.comparison_quality = null;
    item.state = "NO_RECORDED_ACTIVE_RECEIPTS";
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    item.proposed_amounts.estimated_hkd = "0";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("handles a negative half-micro foreign fee and HKD valuation without truncation", () => {
    const body = structuredClone(request); body.proposal.dps = "0";
    Object.assign(body.proposal.accounts[0]!, { fee: "0.000001", fee_currency: "HKD", fee_rate_event: "0.5", fee_rate_source: "Synthetic fee FX" });
    const value = structuredClone(page); value.terms.dps = "0"; const item = value.items[0]!;
    item.estimate = body.proposal.accounts[0]!; item.previous_projection_id = null; item.previous_revision_id = null;
    item.previous_as_of = null; item.previous_currency = null; item.previous_expected = null; item.expected_delta = null;
    item.proposed_amounts = { currency: "USD", gross_local: "0", tax_local: "0", fee_local: "0.000001", net_local: "-0.000001",
      rounding_delta_local: "0", estimated_hkd: "-0.000004", quality: "NEEDS_REVIEW" };
    item.recorded_received = "0"; item.recorded_outstanding = "-0.000001"; item.state = "RECORDED_NEEDS_REVIEW"; item.comparison_quality = "NEEDS_REVIEW";
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    item.proposed_amounts.net_local = "0";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("keeps twelve-place foreign fee precision through net and HKD", () => {
    const body = structuredClone(request); Object.assign(body.proposal.accounts[0]!, { fee: "1", fee_currency: "HKD",
      fee_rate_event: "0.123456789123", fee_rate_source: "Synthetic fee FX" });
    const value = structuredClone(page); const item = value.items[0]!; item.estimate = body.proposal.accounts[0]!;
    item.proposed_amounts!.fee_local = "0.123457"; item.proposed_amounts!.net_local = "10.876543"; item.proposed_amounts!.estimated_hkd = "87.012346";
    item.expected_delta = "0.876543"; item.recorded_outstanding = "4.876543";
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    item.proposed_amounts!.estimated_hkd = "87.012344";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("keeps unknown estimates separate from explicit zero", () => {
    const body = structuredClone(request); body.proposal.accounts[0]!.tax_rate = null;
    const value = structuredClone(page); const item = value.items[0]!; item.estimate.tax_rate = null;
    Object.assign(item, { state: "ESTIMATES_UNAVAILABLE", proposed_amounts: null, expected_delta: null,
      recorded_received: null, recorded_outstanding: null, comparison_quality: null });
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    item.estimate.tax_rate = "0";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("preserves review and receipt states without inferring missing amounts", () => {
    const variants = [
      { state: "RECORDED_RECEIVED", recorded_received: "11", recorded_outstanding: "0", comparison_quality: "OK" },
      { state: "RECORDED_OVER_RECEIVED", recorded_received: "12", recorded_outstanding: "-1", comparison_quality: "NEEDS_REVIEW" },
      { state: "RECORDED_NEEDS_REVIEW", recorded_received: "-1", recorded_outstanding: "12", comparison_quality: "NEEDS_REVIEW" },
      { state: "CASH_LINK_UNAVAILABLE", unavailable_cash_links: "1", recorded_received: null, recorded_outstanding: null, comparison_quality: null },
    ];
    for (const change of variants) {
      const value = structuredClone(page); Object.assign(value.items[0]!, change);
      expect(checkedRevisionPreview(value, event, accounts, request)).toBe(value);
    }
  });

  it("retains each review condition and its explicit missing inputs", () => {
    for (const kind of ["basis", "retired", "creation", "eligibility", "zero", "cash", "fee", "fee-fx"] as const) {
      const body = structuredClone(request); const value = structuredClone(page); const item = value.items[0]!;
      let state: Schemas["PreviewAccount"]["state"];
      if (kind === "basis") { body.proposal.adjustment_basis = "UNKNOWN"; value.terms.adjustment_basis = "UNKNOWN"; state = "EVENT_BASIS_REVIEW"; }
      else if (kind === "retired") { item.retired_trade_scope = "RETIRED_TRADE_HISTORY_UNVERIFIED"; state = "LEDGER_AUDIT_REVIEW"; }
      else if (kind === "creation") { item.creation_audit_scope = "CREATION_GAP_REVIEWED_UNVERIFIED"; state = "LEDGER_AUDIT_REVIEW"; }
      else if (kind === "eligibility") { item.eligibility_state = "NEEDS_REVIEW"; item.recorded_quantity = null; item.eligibility_reasons = ["UNALLOCATED_ACTION"];
        item.pending_action_ids = [old]; state = "ELIGIBILITY_REVIEW"; }
      else if (kind === "zero" || kind === "cash") { item.recorded_quantity = "0"; if (kind === "zero") item.active_receipts = "0";
        state = kind === "zero" ? "NO_RECORDED_POSITION" : "NO_RECORDED_POSITION_CASH_REVIEW"; }
      else if (kind === "fee") { item.estimate.fee = null; body.proposal.accounts[0]!.fee = null; state = "ESTIMATES_UNAVAILABLE"; }
      else { Object.assign(item.estimate, { fee: "1", fee_currency: "HKD" }); Object.assign(body.proposal.accounts[0]!, { fee: "1", fee_currency: "HKD" });
        state = "FEE_FX_UNAVAILABLE"; }
      Object.assign(item, { state, proposed_amounts: null, expected_delta: null, recorded_received: null, recorded_outstanding: null, comparison_quality: null });
      expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
      item.recorded_received = "0";
      expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
    }
  });

  it("rejects duplicate account scopes and changed estimates without revealing them", () => {
    const duplicate = structuredClone(request); duplicate.proposal.accounts[1]!.account_id = account.id.toUpperCase();
    expect(() => checkedRevisionPreview(page, event, accounts, duplicate)).toThrow(/無法安全核對/);
    expect(() => checkedRevisionPreview(page, event, [account, { ...second, id: account.id.toUpperCase() }], request)).toThrow(/無法安全核對/);
    const different = structuredClone(page); different.items[0]!.estimate.fx_source = "synthetic-private";
    try { checkedRevisionPreview(different, event, accounts, request); throw new Error("not rejected"); }
    catch (error) { expect(String(error)).not.toContain("synthetic-private"); expect(String(error)).toContain("無法安全核對"); }
  });

  it("accepts a matching archived account without inferring an active account", () => {
    const value = structuredClone(page); value.items[0]!.account_status = "ARCHIVED";
    expect(checkedRevisionPreview(value, event, [{ ...account, status: "ARCHIVED" }, second], request)).toBe(value);
    expect(() => checkedRevisionPreview(value, event, accounts, request)).toThrow(/無法安全核對/);
  });

  it("keeps HKD identity and mixed-currency receipt review explicit", () => {
    const body = structuredClone(request); body.proposal.currency = "HKD";
    for (const estimate of body.proposal.accounts) Object.assign(estimate, { fee_currency: "HKD", fx_rate_hkd: null, fx_source: null });
    const value = structuredClone(page); value.terms.currency = "HKD"; const item = value.items[0]!;
    item.estimate = body.proposal.accounts[0]!; item.proposed_amounts!.currency = "HKD"; item.proposed_amounts!.estimated_hkd = "11";
    Object.assign(item, { expected_delta: null, recorded_received: null, recorded_outstanding: null, comparison_quality: "NEEDS_REVIEW", state: "CURRENCY_REVIEW" });
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    item.proposed_amounts!.estimated_hkd = "10.999999";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("distinguishes future recorded holdings from a pre-ex-date observation", () => {
    const body = structuredClone(request); body.proposal.as_of = "2026-11-02";
    const value = structuredClone(page); value.terms.as_of = "2026-11-02";
    value.items[0]!.eligibility_state = "UPCOMING_RECORDED_HOLDINGS"; value.items[0]!.expected_delta = null;
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    value.items[0]!.eligibility_state = "RECORDED_PRE_EX_DATE";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("preserves INT64 receipt digits and a signed difference without Number", () => {
    const value = structuredClone(page); const item = value.items[0]!;
    item.recorded_received = "9223372036854.775807"; item.recorded_outstanding = "-9223372036843.775807";
    item.state = "RECORDED_OVER_RECEIVED"; item.comparison_quality = "NEEDS_REVIEW";
    expect(checkedRevisionPreview(value, event, accounts, request)).toBe(value);
    item.recorded_outstanding = "-9223372036843.775806";
    expect(() => checkedRevisionPreview(value, event, accounts, request)).toThrow(/無法安全核對/);
  });

  it("preserves review quality when negative net rounds to zero with missing FX", () => {
    const body = structuredClone(request); body.proposal.dps = "0";
    Object.assign(body.proposal.accounts[0]!, { fee: "0.000001", fee_currency: "HKD", fee_rate_event: "0.01",
      fee_rate_source: "Synthetic fee FX", fx_rate_hkd: null, fx_source: null });
    const value = structuredClone(page); value.terms.dps = "0"; const item = value.items[0]!;
    item.estimate = body.proposal.accounts[0]!; item.previous_projection_id = null; item.previous_revision_id = null;
    item.previous_as_of = null; item.previous_currency = null; item.previous_expected = null; item.expected_delta = null;
    item.proposed_amounts = { currency: "USD", gross_local: "0", tax_local: "0", fee_local: "0", net_local: "0",
      rounding_delta_local: "0", estimated_hkd: null, quality: "NEEDS_REVIEW" };
    item.recorded_received = "0"; item.recorded_outstanding = "0"; item.state = "RECORDED_NEEDS_REVIEW"; item.comparison_quality = "NEEDS_REVIEW";
    expect(checkedRevisionPreview(value, event, accounts, body)).toBe(value);
    item.proposed_amounts.quality = "MISSING_FX";
    expect(() => checkedRevisionPreview(value, event, accounts, body)).toThrow(/無法安全核對/);
  });

  it("does not put private malformed data in error messages", () => {
    const bad = { ...page, items: [{ ...page.items[0]!, recorded_received: "synthetic-private" }] } as Page;
    try { checkedRevisionPreview(bad, event, accounts, request); throw new Error("not rejected"); }
    catch (error) { expect(String(error)).not.toContain("synthetic-private"); expect(String(error)).toContain("無法安全核對"); }
  });
});
