import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, price, sameMoney } from "../ledger/inputs";
import { checkedFee, checkedTax, ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { checkedProjectionPage } from "./projection-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Inputs = Schemas["ProjectionInputs"];
type Tax = Schemas["TaxSettingSelection"];
type Fee = Schemas["FeeSettingSelection"];
type Projection = Schemas["ProjectionRecord"];
export type ProjectionQuery = { as_of: string; eligibility_rule: Inputs["eligibility_rule"]; tax_effective_on: string; fee_effective_on: string };
export type ProjectionTarget = { event: Event; account: Account; inputs: Inputs; tax: Tax; fee: Fee; query: ProjectionQuery };
const id = z.uuid();
const sameId = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();
const sameOptionalId = (left: string | null, right: string | null) => left === null ? right === null : right !== null && sameId(left, right);
const selection = z.object({ effective_on: date, head_no: ordinal, head_id: id.nullable(), selected_id: id.nullable(),
  status: z.enum(["MISSING", "UNKNOWN", "ESTIMATED"]), selected_scope: z.enum(["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]),
  scope: z.literal("OWNER_ESTIMATE_ONLY") }).refine(value => (value.head_no === "0") === (value.head_id === null)
    && (value.status === "MISSING") === (value.selected_id === null) && (value.selected_scope === "NO_SELECTED_SETTING") === (value.selected_id === null)
    && (value.head_no !== "0" || value.selected_id === null));
const snapshot = z.object({ event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647), ex_date: date, currency: z.enum(["HKD", "USD"]),
  account_id: id, portfolio_id: id, broker_id: id, account_version: positiveOrdinal, broker_version: positiveOrdinal, portfolio_data_revision: ordinal,
  projection_head_no: ordinal, projection_head_id: id.nullable(), as_of: date, eligibility_rule: z.enum(["ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"]),
  tax: selection, fee: selection, rejection_state: z.enum(["NOT_REJECTED", "REJECTED"]), scope: z.literal("RECORDED_PROJECTION_INPUTS_ONLY"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), source_scope: z.literal("CURRENT_SOURCE_NOT_REVALIDATED"), eligibility_scope: z.literal("NOT_ESTABLISHED") });
const eventTerms = z.object({ ex_date: date, currency: z.enum(["HKD", "USD"]), dps: price.max(50),
  adjustment_basis: z.enum(["AS_DECLARED", "SPLIT_ADJUSTED", "UNKNOWN"]),
  workflow_status: z.enum(["PROVISIONAL", "CORROBORATED", "VERIFIED", "CONFLICT", "INCOMPLETE", "WITHDRAWN"]),
  manual_locked: z.boolean(), manual_override: z.boolean() });

function sameSelection(input: Inputs["tax"], result: Tax | Fee): boolean {
  const status = "tax_status" in result ? result.tax_status : result.fee_status;
  return input.effective_on === result.effective_on && input.head_no === result.head_no && sameOptionalId(input.head_id, result.head_id)
    && sameOptionalId(input.selected_id, result.selected?.id ?? null) && input.status === status && input.selected_scope === result.selected_scope;
}

export function checkedProjectionInputs(inputs: Inputs, event: Event, account: Account, query: ProjectionQuery): Inputs {
  const valid = validEstimateEvent(event) && eventTerms.safeParse(event).success && snapshot.safeParse(inputs).success
    && z.object({ id, portfolio_id: id, broker_id: id }).safeParse(account).success
    && sameId(inputs.event_id, event.id) && sameId(inputs.revision_id, event.current_revision_id) && inputs.revision_no === event.version
    && inputs.ex_date === event.ex_date && inputs.currency === event.currency && sameId(inputs.account_id, account.id)
    && sameId(inputs.portfolio_id, account.portfolio_id) && sameId(inputs.broker_id, account.broker_id)
    && inputs.as_of === query.as_of && inputs.eligibility_rule === query.eligibility_rule
    && inputs.tax.effective_on === query.tax_effective_on && inputs.fee.effective_on === query.fee_effective_on
    && (inputs.projection_head_no === "0") === (inputs.projection_head_id === null);
  if (!valid) throw new Error("投影輸入範圍無法安全核對；請重新載入事件、帳戶及輸入快照。");
  return inputs;
}

export function validProjectionTarget(target: ProjectionTarget): boolean {
  try {
    const { event, account, inputs, tax, fee, query } = target;
    checkedProjectionInputs(inputs, event, account, query);
    checkedTax(tax, event, account, query.tax_effective_on);
    checkedFee(fee, event, account, query.fee_effective_on);
    return BigInt(inputs.projection_head_no) < 9223372036854775807n && inputs.rejection_state === "NOT_REJECTED"
      && fee.revision_rejection === "NOT_REJECTED" && inputs.tax.selected_scope !== "ACCOUNT_SCOPE_CHANGED" && inputs.fee.selected_scope !== "ACCOUNT_SCOPE_CHANGED"
      && sameSelection(inputs.tax, tax) && sameSelection(inputs.fee, fee)
      && [tax, fee].every(value => sameId(value.portfolio_id, inputs.portfolio_id) && sameId(value.broker_id, inputs.broker_id)
        && value.account_version === inputs.account_version && value.broker_version === inputs.broker_version);
  } catch { return false; }
}

function exact(value: string, places: number): string {
  const [whole, fraction = ""] = value.split(".");
  return `${whole!.replace(/^0+(?=\d)/, "")}.${fraction.padEnd(places, "0")}`;
}
export function fxIntent(value: string | null): string | null { return value === null ? null : exact(value, 12); }

export function checkedProjectionSaved(record: Projection, target: ProjectionTarget, body: Schemas["ProjectionWrite"]): Projection {
  try {
    if (!validProjectionTarget(target)) throw new Error();
    checkedProjectionPage({ items: [record] }, target.event, target.account);
    const { inputs, event, account } = target;
    const forcedReview = !["PROVISIONAL", "CORROBORATED", "VERIFIED"].includes(event.workflow_status)
      || event.adjustment_basis !== "AS_DECLARED" || target.query.eligibility_rule === "REQUIRES_REVIEW"
      || record.retired_trade_scope === "RETIRED_TRADE_HISTORY_UNVERIFIED" || record.creation_audit_scope === "CREATION_GAP_REVIEWED_UNVERIFIED";
    if (event.workflow_status === "WITHDRAWN" ? record.state !== "CANCELLED" : record.state === "CANCELLED" || (forcedReview && record.state !== "NEEDS_REVIEW")) throw new Error();
    const tax = record.tax_provenance; const fee = record.fee_provenance;
    if (!sameId(record.event_id, event.id) || !sameId(record.revision_id, inputs.revision_id) || record.revision_no !== inputs.revision_no
      || !sameId(record.account_id, account.id) || !sameId(record.portfolio_id, inputs.portfolio_id) || !sameId(record.broker_id, inputs.broker_id)
      || record.account_version !== inputs.account_version || record.broker_version !== inputs.broker_version || record.portfolio_data_revision !== inputs.portfolio_data_revision
      || record.projection_no !== (BigInt(inputs.projection_head_no) + 1n).toString() || !sameOptionalId(record.previous_id, inputs.projection_head_id)
      || record.as_of !== body.as_of || record.ex_date !== inputs.ex_date || record.currency !== inputs.currency || record.eligibility_rule !== body.eligibility_rule
      || record.reason !== body.reason || !tax || !fee || tax.effective_on !== inputs.tax.effective_on || fee.effective_on !== inputs.fee.effective_on
      || tax.observed_head_no !== inputs.tax.head_no || fee.observed_head_no !== inputs.fee.head_no
      || !sameOptionalId(tax.observed_head_id, inputs.tax.head_id) || !sameOptionalId(fee.observed_head_id, inputs.fee.head_id)
      || !sameOptionalId(tax.selected_setting_id, inputs.tax.selected_id) || !sameOptionalId(fee.selected_setting_id, inputs.fee.selected_id)
      || tax.status !== inputs.tax.status || fee.status !== inputs.fee.status) throw new Error();
    if (record.state === "EXPECTED") {
      const selectedTax = target.tax.selected; const selectedFee = target.fee.selected;
      if (!selectedTax || selectedTax.rate === null || !selectedFee || selectedFee.fee === null || !selectedFee.fee_currency
        || record.tax_rate === null || record.fee_original === null || !sameMoney(record.tax_rate, selectedTax.rate)
        || !sameMoney(record.fee_original, selectedFee.fee) || record.fee_currency !== selectedFee.fee_currency) throw new Error();
      const hkdRate = body.fx_rate_hkd ?? (inputs.currency === "HKD" ? "1" : null);
      const hkdSource = body.fx_source ?? (inputs.currency === "HKD" ? "IDENTITY" : null);
      const feeRate = body.fee_rate_event ?? (record.fee_currency === inputs.currency ? "1" : null);
      const feeSource = body.fee_rate_source ?? (record.fee_currency === inputs.currency ? "IDENTITY" : null);
      if (fxIntent(record.fx_rate_hkd) !== fxIntent(hkdRate) || record.fx_source !== hkdSource
        || fxIntent(record.fee_rate_event) !== fxIntent(feeRate) || record.fee_rate_source !== feeSource) throw new Error();
    }
    return record;
  } catch {
    throw new Error("伺服器回覆與本次投影不符；請保留輸入並核對歷史，不會假定未儲存。");
  }
}
