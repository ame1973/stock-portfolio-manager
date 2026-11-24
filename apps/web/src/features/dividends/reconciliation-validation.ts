import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, signedMoney } from "../ledger/inputs";
import { ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { sameId } from "./receipt-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Record = Schemas["RecordedReconciliation"];
const id = z.uuid();
const quality = z.enum(["OK", "MISSING_QUOTE", "MISSING_FX", "NOT_COMPUTABLE", "NEEDS_REVIEW"]);
const shape = z.strictObject({
  event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647), account_id: id,
  projection_id: id.nullable(), projection_no: positiveOrdinal.nullable(), projection_as_of: date.nullable(),
  projection_state: z.enum(["EXPECTED", "NO_POSITION", "NEEDS_REVIEW", "CANCELLED"]).nullable(),
  projection_quality: quality.nullable(), comparison_quality: quality.nullable(), currency: z.enum(["HKD", "USD"]).nullable(),
  recorded_expected: signedMoney.max(50).nullable(), recorded_received: signedMoney.max(50).nullable(), recorded_outstanding: signedMoney.max(50).nullable(),
  active_receipts: ordinal, void_receipts: ordinal, unverified_origins: ordinal, unavailable_cash_links: ordinal,
  state: z.enum(["NO_RECORDED_PROJECTION", "STALE_RECORDED_PROJECTION", "PROJECTION_NOT_COMPARABLE", "CASH_LINK_UNAVAILABLE",
    "NO_RECORDED_ACTIVE_RECEIPTS", "CURRENCY_REVIEW", "RECORDED_PARTIAL", "RECORDED_RECEIVED", "RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"]),
  scope: z.literal("UNVERIFIED_RECORDED_RECONCILIATION_ONLY"), history_completeness: z.literal("UNVERIFIED"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"), payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED"),
});

function micros(value: string): bigint {
  const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole! + fraction.padEnd(6, "0")) * (value.startsWith("-") ? -1n : 1n);
}

function consistent(value: Record): boolean {
  const active = BigInt(value.active_receipts); const total = active + BigInt(value.void_receipts);
  const missing = BigInt(value.unavailable_cash_links); const hasProjection = value.projection_id !== null;
  if (hasProjection !== (value.projection_no !== null) || hasProjection !== (value.projection_as_of !== null)
    || hasProjection !== (value.projection_state !== null) || (value.projection_state === "EXPECTED") !== (value.projection_quality !== null)
    || (value.state === "NO_RECORDED_PROJECTION") === hasProjection || BigInt(value.unverified_origins) > total || missing > total) return false;
  if (["NO_RECORDED_PROJECTION", "STALE_RECORDED_PROJECTION", "PROJECTION_NOT_COMPARABLE"].includes(value.state)) {
    return [value.currency, value.recorded_expected, value.recorded_received, value.recorded_outstanding, value.comparison_quality].every(item => item === null)
      && !(value.state === "PROJECTION_NOT_COMPARABLE" && value.projection_state === "EXPECTED");
  }
  if (value.projection_state !== "EXPECTED" || value.currency === null || value.recorded_expected === null) return false;
  if (["CASH_LINK_UNAVAILABLE", "NO_RECORDED_ACTIVE_RECEIPTS", "CURRENCY_REVIEW"].includes(value.state)) {
    return value.recorded_received === null && value.recorded_outstanding === null && (value.state === "CASH_LINK_UNAVAILABLE") === (missing > 0n)
      && (value.state !== "NO_RECORDED_ACTIVE_RECEIPTS" || active === 0n) && (value.state !== "CURRENCY_REVIEW" || active > 0n)
      && value.comparison_quality === (value.state === "CURRENCY_REVIEW" ? "NEEDS_REVIEW" : null);
  }
  if (missing !== 0n || active === 0n || value.recorded_received === null || value.recorded_outstanding === null || value.comparison_quality === null) return false;
  const expected = micros(value.recorded_expected); const received = micros(value.recorded_received); const outstanding = micros(value.recorded_outstanding);
  if (expected - received !== outstanding) return false;
  if (value.state === "RECORDED_PARTIAL" && (expected < 0n || received < 0n || outstanding <= 0n)) return false;
  if (value.state === "RECORDED_RECEIVED" && (expected < 0n || received < 0n || outstanding !== 0n)) return false;
  if (value.state === "RECORDED_OVER_RECEIVED" && (expected < 0n || received < 0n || outstanding >= 0n)) return false;
  return value.comparison_quality === (["RECORDED_OVER_RECEIVED", "RECORDED_NEEDS_REVIEW"].includes(value.state) ? "NEEDS_REVIEW" : value.projection_quality);
}

export function checkedReconciliation(value: Record, event: Event, account: Account): Record {
  try {
    if (!validEstimateEvent(event) || !z.object({ id }).safeParse(account).success || !shape.safeParse(value).success
      || !sameId(value.event_id, event.id) || !sameId(value.account_id, account.id)
      || (value.revision_no === event.version) !== sameId(value.revision_id, event.current_revision_id) || !consistent(value)) throw new Error();
    return value;
  } catch {
    throw new Error("對帳紀錄範圍無法安全核對；請重新載入事件與帳戶，或檢查有效備份。");
  }
}
