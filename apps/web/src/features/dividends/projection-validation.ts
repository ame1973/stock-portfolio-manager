import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money, quantity, signedMoney } from "../ledger/inputs";
import { estimateRate, ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { normalizeDecisionText } from "./event-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Projection = Schemas["ProjectionRecord"];
type Page = Schemas["Page_ProjectionRecord_"];
const id = z.uuid();
const sameId = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();
const amount = money.max(50);
const signed = signedMoney.max(50);
const quantityValue = quantity.max(50);
const fxValue = fx.max(50);
export const source = z.string().refine(value => Array.from(value).length >= 1 && Array.from(value).length <= 100
  && normalizeDecisionText(value) === value && !value.includes("\0"));
const provenance = z.object({
  effective_on: date, selected_setting_id: id.nullable(), observed_head_no: ordinal, observed_head_id: id.nullable(),
  status: z.enum(["MISSING", "UNKNOWN", "ESTIMATED"]), scope: z.literal("OWNER_ESTIMATE_ONLY"),
}).refine(value => (value.observed_head_no === "0") === (value.observed_head_id === null)
  && (value.status === "MISSING") === (value.selected_setting_id === null)
  && (value.observed_head_no !== "0" || value.selected_setting_id === null)
  && (value.observed_head_no !== "1" || value.selected_setting_id === null
    || (typeof value.selected_setting_id === "string" && typeof value.observed_head_id === "string" && sameId(value.selected_setting_id, value.observed_head_id))));
const projection = z.object({
  id, event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647), account_id: id, portfolio_id: id, broker_id: id,
  account_version: positiveOrdinal, broker_version: positiveOrdinal, portfolio_data_revision: ordinal, projection_no: positiveOrdinal,
  previous_id: id.nullable(), state: z.enum(["EXPECTED", "NO_POSITION", "NEEDS_REVIEW", "CANCELLED"]), as_of: date, ex_date: date,
  timing_scope: z.enum(["BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY", "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY"]), currency: z.enum(["HKD", "USD"]),
  eligibility_rule: z.enum(["ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"]),
  retired_trade_scope: z.enum(["NO_RETIRED_TRADES", "RETIRED_TRADE_HISTORY_UNVERIFIED", "RETIRED_TRADE_HASHES_MATCHED"]),
  creation_audit_scope: z.enum(["NO_RECORDED_CREATION_GAP_REVIEW", "CREATION_GAP_REVIEWED_UNVERIFIED"]),
  recorded_shares: quantityValue.nullable(),
  estimated_amounts: z.object({ currency: z.enum(["HKD", "USD"]), gross_local: amount, tax_local: amount, fee_local: amount,
    net_local: signed, rounding_delta_local: signed, estimated_hkd: signed.nullable(), quality: z.enum(["OK", "MISSING_QUOTE", "MISSING_FX", "NOT_COMPUTABLE", "NEEDS_REVIEW"]) }).nullable(),
  tax_rate: estimateRate.nullable(), fee_original: amount.nullable(), fee_currency: z.enum(["HKD", "USD"]).nullable(),
  fee_rate_event: fxValue.nullable(), fee_rate_source: source.nullable(), fx_rate_hkd: fxValue.nullable(), fx_source: source.nullable(),
  tax_provenance: provenance.nullable(), fee_provenance: provenance.nullable(),
  created_at: z.iso.datetime({ offset: true }), reason: z.string().refine(value => Array.from(value).length >= 10 && Array.from(value).length <= 1000
    && normalizeDecisionText(value) === value && !value.includes("\0")),
  history_completeness: z.literal("UNVERIFIED"), scope: z.literal("UNVERIFIED_PROJECTION_HISTORY_ONLY"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"),
});

function scaled(value: string, places: number): bigint {
  const negative = value.startsWith("-"); const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole! + fraction.padEnd(places, "0")) * (negative ? -1n : 1n);
}

function consistent(value: Projection): boolean {
  if ((value.projection_no === "1") !== (value.previous_id === null) || (value.previous_id !== null && sameId(value.id, value.previous_id))
    || value.timing_scope !== (value.as_of < value.ex_date ? "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY" : "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY")
    || (value.fee_rate_event === null) !== (value.fee_rate_source === null) || (value.fx_rate_hkd === null) !== (value.fx_source === null)) return false;
  const amounts = value.estimated_amounts;
  if (value.state !== "EXPECTED") return amounts === null && [value.recorded_shares, value.tax_rate, value.fee_original, value.fee_currency,
    value.fee_rate_event, value.fee_rate_source, value.fx_rate_hkd, value.fx_source].every(item => item === null);
  if (!amounts || value.recorded_shares === null || value.tax_rate === null || value.fee_original === null || value.fee_currency === null
    || amounts.currency !== value.currency || value.eligibility_rule !== "ORDINARY_PRE_EX_DATE"
    || (value.tax_provenance !== null && value.tax_provenance.status !== "ESTIMATED")
    || (value.fee_provenance !== null && value.fee_provenance.status !== "ESTIMATED")
    || (value.fx_rate_hkd === null) !== (amounts.estimated_hkd === null)
    || (amounts.quality === "OK" && amounts.estimated_hkd === null) || (amounts.quality === "MISSING_FX" && amounts.estimated_hkd !== null)) return false;
  const net = scaled(amounts.net_local, 6);
  if (net < 0n && amounts.quality !== "NEEDS_REVIEW") return false;
  if (value.currency === "HKD") {
    if (value.fx_rate_hkd === null || scaled(value.fx_rate_hkd, 12) !== 1_000_000_000_000n || value.fx_source !== "IDENTITY") return false;
  } else if (value.fx_rate_hkd === null ? !["MISSING_FX", "NEEDS_REVIEW"].includes(amounts.quality) : value.fx_source === "IDENTITY") return false;
  if (value.fee_currency === value.currency) {
    if (value.fee_rate_event === null || scaled(value.fee_rate_event, 12) !== 1_000_000_000_000n || value.fee_rate_source !== "IDENTITY"
      || scaled(value.fee_original, 6) !== scaled(amounts.fee_local, 6)) return false;
  } else if (value.fee_rate_event === null ? scaled(value.fee_original, 6) !== 0n || scaled(amounts.fee_local, 6) !== 0n : value.fee_rate_source === "IDENTITY") return false;
  return scaled(amounts.gross_local, 6) - scaled(amounts.tax_local, 6) - scaled(amounts.fee_local, 6)
    + scaled(amounts.rounding_delta_local, 6) === net;
}

export function checkedProjectionPage(value: Page, event: Event, account: Account): Page {
  const ids = new Set<string>(); let previous: bigint | null = null;
  if (!validEstimateEvent(event) || !z.object({ id, portfolio_id: id, broker_id: id }).safeParse(account).success
    || !value || !Array.isArray(value.items) || value.items.length > 25 || (value.next_cursor != null && typeof value.next_cursor !== "string")
    || value.items.some(item => {
      if (!projection.safeParse(item).success || !sameId(item.event_id, event.id) || !sameId(item.account_id, account.id)
        || ids.has(item.id.toLowerCase()) || !consistent(item)) return true;
      const number = BigInt(item.projection_no);
      if (previous !== null && number >= previous) return true;
      previous = number; ids.add(item.id.toLowerCase()); return false;
    })) throw new Error("投影歷史範圍無法安全核對；請重新載入或檢查有效備份。");
  return value;
}
