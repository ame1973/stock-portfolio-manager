import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { checkedDirectReceiptInputs, validDirectReceiptTarget } from "./direct-receipt-input-validation";
import { account, event } from "./projection-test-data";

type Inputs = components["schemas"]["DirectReceiptInputs"];
const inputs: Inputs = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, workflow_status: event.workflow_status,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE",
  account_version: "9007199254740993", portfolio_version: "9223372036854775807", broker_version: "2",
  portfolio_data_revision: "9223372036854775806", rejection_state: "NOT_REJECTED", rejection_id: null,
  receipt_basis: "DIRECT_EVENT_ACCOUNT", scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED",
  eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
const target = { event, account, inputs };
const other = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

describe("direct receipt input pins", () => {
  it("keeps exact strings without requiring or inventing entitlement or cash", () => {
    expect(checkedDirectReceiptInputs(inputs, event, account)).toBe(inputs);
    expect(validDirectReceiptTarget(target)).toBe(true);
    expect(inputs.account_version).toBe("9007199254740993");
    expect(inputs).not.toHaveProperty("entitlement_id");
    expect(inputs).not.toHaveProperty("received_amount");
  });
  it.each([
    ["event_id", other], ["revision_id", other], ["revision_no", event.version + 1], ["revision_no", "2"],
    ["account_id", other], ["portfolio_id", other], ["broker_id", other], ["account_status", "ARCHIVED"],
    ["account_version", 1], ["account_version", "0"], ["account_version", "01"], ["account_version", "9223372036854775808"],
    ["portfolio_version", "0"], ["broker_version", "1e2"], ["portfolio_data_revision", 1], ["portfolio_data_revision", "-1"],
    ["portfolio_data_revision", "9223372036854775808"], ["workflow_status", "WITHDRAWN"], ["workflow_status", "READY"],
    ["rejection_state", "REJECTED"], ["rejection_id", other], ["rejection_id", undefined], ["receipt_basis", "LEGACY_ENTITLEMENT"],
    ["scope", "RECORDED_RECEIPT_INPUTS_ONLY"], ["history_completeness", "COMPLETE"], ["current_use_scope", "GRANTED"],
    ["eligibility_scope", "ESTABLISHED"], ["payment_scope", "VERIFIED"], ["entitlement_id", null], ["entitlements", []], ["received_amount", "0"],
  ])("rejects malformed or contradictory %s=%s", (field, value) => {
    const changed = { ...inputs, [field]: value } as Inputs;
    expect(() => checkedDirectReceiptInputs(changed, event, account)).toThrow(/無法安全核對/);
    expect(validDirectReceiptTarget({ ...target, inputs: changed })).toBe(false);
  });
  it.each(Object.keys(inputs))("requires explicit %s", field => {
    const changed = { ...inputs } as Record<string, unknown>; delete changed[field];
    expect(() => checkedDirectReceiptInputs(changed as Inputs, event, account)).toThrow();
  });
  it("matches UUID case without normalizing or replacing returned values", () => {
    const selected = { ...event, id: other };
    const changed = { ...inputs, event_id: other.toUpperCase(), revision_id: inputs.revision_id.toUpperCase() };
    expect(checkedDirectReceiptInputs(changed, selected, account)).toBe(changed);
  });
  it.each([null, undefined, [], 1, true, "private", {}])("rejects a malformed input envelope", value => {
    expect(() => checkedDirectReceiptInputs(value as Inputs, event, account)).toThrow(/無法安全核對/);
    expect(validDirectReceiptTarget({ ...target, inputs: value as Inputs })).toBe(false);
  });
  it("keeps rejected, archived, withdrawn and exhausted reads explicit but not actionable", () => {
    const rejected = { ...inputs, rejection_state: "REJECTED" as const, rejection_id: other };
    expect(checkedDirectReceiptInputs(rejected, event, account)).toBe(rejected);
    expect(validDirectReceiptTarget({ ...target, inputs: rejected })).toBe(false);
    const archived = { ...inputs, account_status: "ARCHIVED" as const };
    const oldAccount = { ...account, status: "ARCHIVED" as const };
    expect(checkedDirectReceiptInputs(archived, event, oldAccount)).toBe(archived);
    expect(validDirectReceiptTarget({ event, account: oldAccount, inputs: archived })).toBe(false);
    const withdrawn = { ...inputs, workflow_status: "WITHDRAWN" as const };
    const oldEvent = { ...event, workflow_status: "WITHDRAWN" as const };
    expect(checkedDirectReceiptInputs(withdrawn, oldEvent, account)).toBe(withdrawn);
    expect(validDirectReceiptTarget({ event: oldEvent, account, inputs: withdrawn })).toBe(false);
    const exhausted = { ...inputs, portfolio_data_revision: "9223372036854775807" };
    expect(checkedDirectReceiptInputs(exhausted, event, account)).toBe(exhausted);
    expect(validDirectReceiptTarget({ ...target, inputs: exhausted })).toBe(false);
  });
  it("accepts explicit zero data revision and does not infer currency or payment", () => {
    expect(validDirectReceiptTarget({ ...target, inputs: { ...inputs, portfolio_data_revision: "0" } })).toBe(true);
  });
  it("rejects malformed selected event/account values without throwing from the target predicate", () => {
    for (const value of [null, undefined, {}, { ...event, version: 2_147_483_648 }, { ...event, eligibility_scope: "ESTABLISHED" }]) {
      expect(validDirectReceiptTarget({ ...target, event: value as typeof event })).toBe(false);
    }
    for (const value of [null, undefined, {}, { ...account, portfolio_id: other }, { ...account, status: "unknown" }]) {
      expect(validDirectReceiptTarget({ ...target, account: value as typeof account })).toBe(false);
    }
  });
});
