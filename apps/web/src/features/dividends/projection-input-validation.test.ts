import type { components } from "@spm/contracts";
import { describe, expect, it } from "vitest";
import { checkedProjectionInputs, checkedProjectionSaved, fxIntent, validProjectionTarget, type ProjectionTarget } from "./projection-input-validation";
import { account, body, event, fee, feeId, inputs, priorId, query, record, target, tax, taxId } from "./projection-test-data";

type Schemas = components["schemas"];

describe("recorded projection input and response checks", () => {
  it("requires independently consistent exact event, account, projection and setting pins", () => {
    expect(checkedProjectionInputs(inputs, event, account, query)).toEqual(inputs);
    expect(validProjectionTarget(target)).toBe(true);
    expect(checkedProjectionSaved(record, target, body)).toEqual(record);
  });
  it("accepts equivalent UUID casing and retains exact INT64 versions", () => {
    const upper = { ...inputs, event_id: event.id.toUpperCase(), revision_id: event.current_revision_id.toUpperCase(), account_id: account.id.toUpperCase(),
      portfolio_id: account.portfolio_id.toUpperCase(), broker_id: account.broker_id.toUpperCase(),
      tax: { ...inputs.tax, head_id: taxId.toUpperCase(), selected_id: taxId.toUpperCase() } };
    expect(validProjectionTarget({ ...target, inputs: upper })).toBe(true);
    expect(checkedProjectionSaved({ ...record, previous_id: priorId.toUpperCase() }, target, body).projection_no).toBe("9007199254740994");
  });
  it("distinguishes an unselected future head from zero", () => {
    const futureTax: Schemas["TaxSettingSelection"] = { ...tax, tax_status: "MISSING", selected: null, selected_scope: "NO_SELECTED_SETTING" };
    const snapshot: Schemas["ProjectionInputs"] = { ...inputs, tax: { ...inputs.tax, status: "MISSING", selected_id: null, selected_scope: "NO_SELECTED_SETTING" } };
    expect(validProjectionTarget({ ...target, inputs: snapshot, tax: futureTax })).toBe(true);
  });
  it.each([
    { ...inputs, event_id: account.id }, { ...inputs, revision_id: account.id }, { ...inputs, revision_no: 2 },
    { ...inputs, account_id: event.id }, { ...inputs, broker_id: event.security_id }, { ...inputs, portfolio_id: event.security_id },
    { ...inputs, ex_date: "2026-11-04" }, { ...inputs, currency: "HKD" }, { ...inputs, as_of: "2026-11-03" },
    { ...inputs, eligibility_rule: "REQUIRES_REVIEW" }, { ...inputs, account_version: "01" }, { ...inputs, broker_version: "9223372036854775808" },
    { ...inputs, projection_head_no: "0" }, { ...inputs, projection_head_id: null },
    { ...inputs, tax: { ...inputs.tax, selected_id: null } }, { ...inputs, fee: { ...inputs.fee, effective_on: "2026-11-03" } },
    { ...inputs, current_use_scope: "GRANTED" }, { ...inputs, source_scope: "VERIFIED" }, { ...inputs, eligibility_scope: "ESTABLISHED" },
  ])("rejects malformed or mismatched input snapshot %#", broken => {
    expect(() => checkedProjectionInputs(broken as Schemas["ProjectionInputs"], event, account, query)).toThrow(/無法安全核對/);
    expect(validProjectionTarget({ ...target, inputs: broken as Schemas["ProjectionInputs"] })).toBe(false);
  });
  it.each([
    { ...target, inputs: { ...inputs, rejection_state: "REJECTED" } },
    { ...target, inputs: { ...inputs, projection_head_no: "9223372036854775807" } },
    { ...target, fee: { ...fee, revision_rejection: "REJECTED" } },
    { ...target, fee: { ...fee, account_version: "1" } },
    { ...target, tax: { ...tax, head_id: feeId } },
    { ...target, tax: { ...tax, selected_scope: "ACCOUNT_SCOPE_CHANGED", selected: { ...tax.selected!, account_version: "1" } }, inputs: { ...inputs, tax: { ...inputs.tax, selected_scope: "ACCOUNT_SCOPE_CHANGED" } } },
  ])("does not allow a stale, rejected or exhausted target %#", broken => {
    expect(validProjectionTarget(broken as ProjectionTarget)).toBe(false);
  });
  it.each([
    { ...record, revision_id: account.id }, { ...record, portfolio_id: event.id }, { ...record, account_version: "1" },
    { ...record, portfolio_data_revision: "1" }, { ...record, projection_no: "9007199254740993" }, { ...record, previous_id: taxId },
    { ...record, as_of: "2026-11-03", timing_scope: "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY" },
    { ...record, reason: "Different synthetic projection reason" }, { ...record, tax_provenance: null },
    { ...record, tax_provenance: { ...record.tax_provenance!, effective_on: query.fee_effective_on } },
    { ...record, tax_provenance: { ...record.tax_provenance!, selected_setting_id: feeId } },
    { ...record, fee_provenance: { ...record.fee_provenance!, observed_head_id: taxId } },
    { ...record, tax_rate: "0.100000" }, { ...record, fee_original: "0.250000" },
    { ...record, fx_rate_hkd: "7.8", fx_source: "SYNTHETIC", estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "58.500000", quality: "OK" } },
    { ...record, eligibility_scope: "ESTABLISHED" },
  ])("rejects a response not bound to the frozen intent %#", broken => {
    expect(() => checkedProjectionSaved(broken as Schemas["ProjectionRecord"], target, body)).toThrow(/不會假定未儲存/);
  });
  it("accepts uncomputed history without presenting submitted rates as persisted calculations", () => {
    const pending: Schemas["ProjectionRecord"] = { ...record, state: "NEEDS_REVIEW", recorded_shares: null, estimated_amounts: null,
      tax_rate: null, fee_original: null, fee_currency: null, fee_rate_event: null, fee_rate_source: null, fx_rate_hkd: null, fx_source: null };
    expect(checkedProjectionSaved(pending, target, { ...body, fx_rate_hkd: "7.8", fx_source: "SYNTHETIC" })).toEqual(pending);
  });
  it("rejects states inconsistent with the frozen event and selected rule", () => {
    const pending = { ...record, state: "NEEDS_REVIEW" as const, recorded_shares: null, estimated_amounts: null, tax_rate: null,
      fee_original: null, fee_currency: null, fee_rate_event: null, fee_rate_source: null, fx_rate_hkd: null, fx_source: null };
    expect(() => checkedProjectionSaved({ ...pending, state: "CANCELLED" }, target, body)).toThrow(/不會假定未儲存/);
    expect(() => checkedProjectionSaved({ ...pending, state: "NO_POSITION" }, { ...target, event: { ...event, workflow_status: "CONFLICT" } }, body)).toThrow(/不會假定未儲存/);
    expect(() => checkedProjectionSaved({ ...pending, state: "NO_POSITION" }, { ...target, event: { ...event, adjustment_basis: "UNKNOWN" } }, body)).toThrow(/不會假定未儲存/);
    const reviewQuery = { ...query, eligibility_rule: "REQUIRES_REVIEW" as const };
    const reviewTarget = { ...target, query: reviewQuery, inputs: { ...inputs, eligibility_rule: "REQUIRES_REVIEW" as const } };
    const reviewBody = { ...body, eligibility_rule: "REQUIRES_REVIEW" as const };
    expect(() => checkedProjectionSaved({ ...pending, eligibility_rule: "REQUIRES_REVIEW", state: "NO_POSITION" }, reviewTarget, reviewBody)).toThrow(/不會假定未儲存/);
    const withdrawn = { ...target, event: { ...event, workflow_status: "WITHDRAWN" as const } };
    expect(() => checkedProjectionSaved(pending, withdrawn, body)).toThrow(/不會假定未儲存/);
    expect(checkedProjectionSaved({ ...pending, state: "CANCELLED" }, withdrawn, body).state).toBe("CANCELLED");
  });
  it("normalizes equivalent rate spellings only for unchanged retry identity", () => {
    expect(fxIntent("0007.8")).toBe(fxIntent("7.800000000000"));
    expect(fxIntent("7.800000000001")).not.toBe(fxIntent("7.8")); expect(fxIntent(null)).toBeNull();
  });
});
