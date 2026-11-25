import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money, price, signedMoney } from "../ledger/inputs";
import { estimateRate, ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { source } from "./projection-validation";
import { sameId } from "./receipt-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Request = Schemas["PreviewRequest"];
type Page = Schemas["PreviewPage"];
type Item = Schemas["PreviewAccount"];
type Estimate = Schemas["AccountEstimate"];
const id = z.uuid();
const currency = z.enum(["HKD", "USD"]);
const amount = money.max(50);
const signed = signedMoney.max(50);
const shares = price.max(50);
const rate = fx.max(50);
const quality = z.enum(["OK", "MISSING_QUOTE", "MISSING_FX", "NOT_COMPUTABLE", "NEEDS_REVIEW"]);
const token = z.string().regex(/^[0-9a-f]{64}$/);
const estimateShape = z.strictObject({
  account_id: id, tax_rate: estimateRate.nullable(), fee: amount.nullable(), fee_currency: currency.nullable(),
  fee_rate_event: rate.nullable(), fee_rate_source: source.nullable(), fx_rate_hkd: rate.nullable(), fx_source: source.nullable(),
});
const termsShape = z.strictObject({
  ex_date: date, as_of: date, eligibility_rule: z.enum(["ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"]),
  dps: shares, currency, adjustment_basis: z.enum(["AS_DECLARED", "SPLIT_ADJUSTED", "UNKNOWN"]),
});
const requestShape = z.strictObject({
  revision_id: id, proposal: termsShape.extend({ accounts: z.array(estimateShape) }), limit: z.int().min(1).max(100).optional(),
  after_account_id: id.nullable(), snapshot_token: token.nullable(),
});
const amountsShape = z.strictObject({ currency, gross_local: amount, tax_local: amount, fee_local: amount,
  net_local: signed, rounding_delta_local: signed, estimated_hkd: signed.nullable(), quality });
const itemShape = z.strictObject({
  account_id: id, portfolio_id: id, broker_id: id, account_status: z.enum(["ACTIVE", "ARCHIVED"]), broker_status: z.enum(["ACTIVE", "ARCHIVED"]),
  account_version: positiveOrdinal, broker_version: positiveOrdinal, portfolio_version: positiveOrdinal, portfolio_data_revision: ordinal,
  retired_trade_scope: z.enum(["NO_RETIRED_TRADES", "RETIRED_TRADE_HISTORY_UNVERIFIED", "RETIRED_TRADE_HASHES_MATCHED"]),
  creation_audit_scope: z.enum(["NO_RECORDED_CREATION_GAP_REVIEW", "CREATION_GAP_REVIEWED_UNVERIFIED"]),
  eligibility_state: z.enum(["RECORDED_PRE_EX_DATE", "UPCOMING_RECORDED_HOLDINGS", "NEEDS_REVIEW"]), recorded_quantity: shares.nullable(),
  eligibility_reasons: z.array(z.enum(["ELIGIBILITY_RULE_REQUIRES_REVIEW", "UNALLOCATED_ACTION", "SPLIT_FRACTIONAL_RESIDUE"])),
  pending_action_ids: z.array(id), estimate: estimateShape,
  previous_projection_id: id.nullable(), previous_revision_id: id.nullable(), previous_as_of: date.nullable(), previous_currency: currency.nullable(),
  previous_expected: signed.nullable(), proposed_amounts: amountsShape.nullable(), expected_delta: signed.nullable(),
  recorded_received: signed.nullable(), recorded_outstanding: signed.nullable(), active_receipts: ordinal, void_receipts: ordinal,
  unverified_origins: ordinal, unavailable_cash_links: ordinal, comparison_quality: quality.nullable(),
  state: z.enum(["EVENT_BASIS_REVIEW", "LEDGER_AUDIT_REVIEW", "ELIGIBILITY_REVIEW", "NO_RECORDED_POSITION", "NO_RECORDED_POSITION_CASH_REVIEW",
    "ESTIMATES_UNAVAILABLE", "FEE_FX_UNAVAILABLE", "CASH_LINK_UNAVAILABLE", "NO_RECORDED_ACTIVE_RECEIPTS", "CURRENCY_REVIEW",
    "RECORDED_PARTIAL", "RECORDED_RECEIVED", "RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"]),
});
const pageShape = z.strictObject({
  event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647), security_id: id, terms: termsShape,
  items: z.array(itemShape).max(100), snapshot_token: token, next_after_account_id: id.nullable(),
  scope: z.literal("HYPOTHETICAL_RECORDED_COMPARISON_ONLY"), history_completeness: z.literal("UNVERIFIED"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"),
  payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED"),
});
const accountShape = z.object({ id, portfolio_id: id, broker_id: id, status: z.enum(["ACTIVE", "ARCHIVED"]) });
const E6 = 1_000_000n;
const E12 = 1_000_000_000_000n;
const pow = (places: number) => 10n ** BigInt(places);

function scaled(value: string, places: number): bigint {
  const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole! + fraction.padEnd(places, "0")) * (value.startsWith("-") ? -1n : 1n);
}

function rounded(value: bigint, denominator: bigint): bigint {
  const magnitude = (value < 0n ? -value : value) + denominator / 2n;
  return magnitude / denominator * (value < 0n ? -1n : 1n);
}

function sameAmount(left: string | null, right: string | null, scale: number): boolean {
  return left === null || right === null ? left === right : scaled(left, scale) === scaled(right, scale);
}

function validEstimate(value: Estimate, unit: "HKD" | "USD"): boolean {
  if ((value.fee !== null && value.fee_currency === null) || (value.fee_rate_event === null) !== (value.fee_rate_source === null)
    || (value.fx_rate_hkd === null) !== (value.fx_source === null) || (value.fee_currency === null && value.fee_rate_event !== null)) return false;
  if (unit === "HKD") {
    if (value.fx_rate_hkd !== null && (scaled(value.fx_rate_hkd, 12) !== E12 || value.fx_source !== "IDENTITY")) return false;
  } else if (value.fx_source === "IDENTITY") return false;
  if (value.fee_currency === unit) {
    if (value.fee_rate_event !== null && (scaled(value.fee_rate_event, 12) !== E12 || value.fee_rate_source !== "IDENTITY")) return false;
  } else if (value.fee_rate_source === "IDENTITY") return false;
  return true;
}

function equalEstimate(left: Estimate, right: Estimate): boolean {
  return sameId(left.account_id, right.account_id) && sameAmount(left.tax_rate, right.tax_rate, 6)
    && sameAmount(left.fee, right.fee, 6) && left.fee_currency === right.fee_currency
    && sameAmount(left.fee_rate_event, right.fee_rate_event, 12) && left.fee_rate_source === right.fee_rate_source
    && sameAmount(left.fx_rate_hkd, right.fx_rate_hkd, 12) && left.fx_source === right.fx_source;
}

function consistentItem(item: Item, terms: Page["terms"], revision: string): boolean {
  const estimate = item.estimate; const active = BigInt(item.active_receipts);
  const missing = BigInt(item.unavailable_cash_links); const total = active + BigInt(item.void_receipts);
  const projection = item.previous_projection_id !== null;
  if (!sameId(estimate.account_id, item.account_id) || !validEstimate(estimate, terms.currency)
    || projection !== (item.previous_revision_id !== null) || projection !== (item.previous_as_of !== null)
    || (item.previous_currency === null) !== (item.previous_expected === null) || (!projection && item.previous_expected !== null)
    || BigInt(item.unverified_origins) > total || missing > total
    || new Set(item.pending_action_ids.map(value => value.toLowerCase())).size !== item.pending_action_ids.length) return false;
  if (item.eligibility_state === "NEEDS_REVIEW") {
    if (item.recorded_quantity !== null || item.eligibility_reasons.length !== 1
      || (item.eligibility_reasons[0] === "UNALLOCATED_ACTION") !== (item.pending_action_ids.length > 0)) return false;
  } else if (item.recorded_quantity === null || item.eligibility_reasons.length || item.pending_action_ids.length
    || terms.eligibility_rule !== "ORDINARY_PRE_EX_DATE"
    || item.eligibility_state !== (terms.ex_date <= terms.as_of ? "RECORDED_PRE_EX_DATE" : "UPCOMING_RECORDED_HOLDINGS")) return false;

  let unavailable: Item["state"] | null = null;
  if (terms.adjustment_basis !== "AS_DECLARED") unavailable = "EVENT_BASIS_REVIEW";
  else if (item.retired_trade_scope === "RETIRED_TRADE_HISTORY_UNVERIFIED" || item.creation_audit_scope === "CREATION_GAP_REVIEWED_UNVERIFIED") unavailable = "LEDGER_AUDIT_REVIEW";
  else if (item.eligibility_state === "NEEDS_REVIEW" || item.recorded_quantity === null) unavailable = "ELIGIBILITY_REVIEW";
  else if (scaled(item.recorded_quantity, 8) === 0n) unavailable = active || missing ? "NO_RECORDED_POSITION_CASH_REVIEW" : "NO_RECORDED_POSITION";
  else if (estimate.tax_rate === null || estimate.fee === null || estimate.fee_currency === null) unavailable = "ESTIMATES_UNAVAILABLE";
  else if (estimate.fee_currency !== terms.currency && scaled(estimate.fee, 6) !== 0n && estimate.fee_rate_event === null) unavailable = "FEE_FX_UNAVAILABLE";
  if (unavailable) return item.state === unavailable && [item.proposed_amounts, item.expected_delta, item.recorded_received,
    item.recorded_outstanding, item.comparison_quality].every(value => value === null);
  if (item.recorded_quantity === null || estimate.tax_rate === null || estimate.fee === null || estimate.fee_currency === null || !item.proposed_amounts) return false;

  // Preserve the full fee and FX precision until each six-decimal output boundary.
  const gross = scaled(item.recorded_quantity, 8) * scaled(terms.dps, 8);
  const tax = scaled(estimate.tax_rate, 6); const fee = scaled(estimate.fee, 6);
  const feeRate = estimate.fee_currency === terms.currency ? E12 : estimate.fee_rate_event === null ? 0n : scaled(estimate.fee_rate_event, 12);
  const net = gross * (E6 - tax) - fee * feeRate * pow(4);
  const grossMicro = rounded(gross, pow(10)); const taxMicro = rounded(gross * tax, pow(16));
  const feeMicro = rounded(fee * feeRate, E12); const netMicro = rounded(net, pow(16));
  const hkdRate = terms.currency === "HKD" ? E12 : estimate.fx_rate_hkd === null ? null : scaled(estimate.fx_rate_hkd, 12);
  const hkd = hkdRate === null ? null : rounded(net * hkdRate, pow(28));
  const expectedQuality = net < 0n ? "NEEDS_REVIEW" : hkd === null ? "MISSING_FX" : "OK";
  const values = item.proposed_amounts;
  if (values.currency !== terms.currency || scaled(values.gross_local, 6) !== grossMicro || scaled(values.tax_local, 6) !== taxMicro
    || scaled(values.fee_local, 6) !== feeMicro || scaled(values.net_local, 6) !== netMicro
    || scaled(values.rounding_delta_local, 6) !== netMicro - (grossMicro - taxMicro - feeMicro)
    || (hkd === null ? values.estimated_hkd !== null : values.estimated_hkd === null || scaled(values.estimated_hkd, 6) !== hkd)
    || values.quality !== expectedQuality) return false;
  const comparable = item.previous_expected !== null && item.previous_revision_id !== null && sameId(item.previous_revision_id, revision)
    && item.previous_as_of === terms.as_of && item.previous_currency === terms.currency;
  if (comparable ? item.expected_delta === null || netMicro - scaled(item.previous_expected!, 6) !== scaled(item.expected_delta, 6)
    : item.expected_delta !== null) return false;
  if (missing || active === 0n || item.state === "CURRENCY_REVIEW") {
    const state = missing ? "CASH_LINK_UNAVAILABLE" : active === 0n ? "NO_RECORDED_ACTIVE_RECEIPTS" : "CURRENCY_REVIEW";
    return item.state === state && item.recorded_received === null && item.recorded_outstanding === null
      && item.comparison_quality === (state === "CURRENCY_REVIEW" ? "NEEDS_REVIEW" : null);
  }
  if (item.recorded_received === null || item.recorded_outstanding === null
    || !["RECORDED_PARTIAL", "RECORDED_RECEIVED", "RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"].includes(item.state)) return false;
  const received = scaled(item.recorded_received, 6); const outstanding = scaled(item.recorded_outstanding, 6);
  if (netMicro - received !== outstanding) return false;
  if (item.state === "RECORDED_PARTIAL" && (netMicro < 0n || received < 0n || outstanding <= 0n)) return false;
  if (item.state === "RECORDED_RECEIVED" && (netMicro < 0n || received < 0n || outstanding !== 0n)) return false;
  if (item.state === "RECORDED_OVER_RECEIVED" && (netMicro < 0n || received < 0n || outstanding >= 0n)) return false;
  return item.comparison_quality === (["RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"].includes(item.state) ? "NEEDS_REVIEW" : expectedQuality);
}

export function checkedRevisionPreview(value: Page, event: Event, accounts: Account[], request: Request): Page {
  try {
    if (!validEstimateEvent(event) || !pageShape.safeParse(value).success || !requestShape.safeParse(request).success
      || !Array.isArray(accounts) || accounts.some(account => !accountShape.safeParse(account).success)
      || !sameId(request.revision_id, event.current_revision_id) || !sameId(value.event_id, event.id)
      || !sameId(value.revision_id, event.current_revision_id) || value.revision_no !== event.version
      || !sameId(value.security_id, event.security_id)
      || (request.after_account_id === null) !== (request.snapshot_token === null)
      || (request.snapshot_token !== null && request.snapshot_token !== value.snapshot_token)) throw new Error();
    const proposed = request.proposal; const terms = value.terms;
    if (terms.ex_date !== proposed.ex_date || terms.as_of !== proposed.as_of || terms.eligibility_rule !== proposed.eligibility_rule
      || terms.currency !== proposed.currency || terms.adjustment_basis !== proposed.adjustment_basis || !sameAmount(terms.dps, proposed.dps, 8)) throw new Error();
    const accountMap = new Map(accounts.map(account => [account.id.toLowerCase(), account]));
    const estimates = new Map(proposed.accounts.map(estimate => [estimate.account_id.toLowerCase(), estimate]));
    if (accountMap.size !== accounts.length || estimates.size !== proposed.accounts.length || accountMap.size !== estimates.size
      || [...estimates.keys()].some(key => !accountMap.has(key)) || proposed.accounts.some(item => !validEstimate(item, proposed.currency))) throw new Error();
    const ids = [...estimates.keys()].sort(); const after = request.after_account_id?.toLowerCase();
    const offset = after === undefined ? 0 : ids.indexOf(after) + 1;
    if (after !== undefined && offset === 0) throw new Error();
    const limit = request.limit ?? 25; const expected = ids.slice(offset, offset + limit);
    const next = offset + limit < ids.length ? expected.at(-1) : undefined;
    if (value.items.length !== expected.length || (next === undefined ? value.next_after_account_id !== null
      : value.next_after_account_id === null || !sameId(value.next_after_account_id, next))) throw new Error();
    for (const [index, item] of value.items.entries()) {
      const key = item.account_id.toLowerCase(); const account = accountMap.get(key); const estimate = estimates.get(key);
      if (key !== expected[index] || !account || !estimate || !sameId(item.portfolio_id, account.portfolio_id)
        || !sameId(item.broker_id, account.broker_id) || item.account_status !== account.status
        || !equalEstimate(item.estimate, estimate) || !consistentItem(item, terms, value.revision_id)) throw new Error();
    }
    return value;
  } catch {
    throw new Error("修訂預覽範圍或金額無法安全核對；請保留輸入並重新載入事件及所有帳戶。");
  }
}
