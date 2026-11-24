import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money, signedMoney } from "../ledger/inputs";
import { positiveOrdinal, validEstimateEvent } from "./estimate-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Receipt = Schemas["RecordedReceipt"];
type Page = Schemas["Page_RecordedReceipt_"];
type CombinedReceipt = Schemas["CombinedRecordedReceipt"];
type CombinedPage = Schemas["Page_CombinedRecordedReceipt_"];
const id = z.uuid();
export const sameId = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();
export const timestamp = z.iso.datetime({ offset: true }).refine(value => /^(?:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value)));
const receipt = z.object({
  id, event_id: id, account_id: id, entitlement_id: id, revision_id: id, current_revision_id: id,
  revision_no: z.int().min(1).max(2_147_483_647), received_date: date,
  recorded_amount: signedMoney.max(50), currency: z.enum(["HKD", "USD"]),
  recorded_tax: money.max(50).nullable(), recorded_fee: money.max(50).nullable(), fx_rate_hkd: fx.max(50).nullable(), received_hkd: signedMoney.max(50).nullable(),
  source: z.enum(["MANUAL", "IMPORT"]), status: z.enum(["ACTIVE", "VOID"]), version: positiveOrdinal,
  created_at: timestamp, voided_at: timestamp.nullable(),
  cash_scope: z.enum(["CASH_NOT_RECORDED", "CASH_REVERSAL_NOT_RECORDED", "MATCHING_CASH_RECORDED_UNVERIFIED", "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"]),
  audit_scope: z.literal("RECEIPT_AUDIT_NOT_VERIFIED"), history_completeness: z.literal("UNVERIFIED"),
  scope: z.literal("UNVERIFIED_RECEIPT_HISTORY_ONLY"), current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"),
});

function scaled(value: string, places: number): bigint {
  const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole! + fraction.padEnd(places, "0")) * (value.startsWith("-") ? -1n : 1n);
}
const combinedReceipt = receipt.extend({
  entitlement_id: id.nullable(), receipt_basis: z.enum(["LEGACY_ENTITLEMENT", "DIRECT_EVENT_ACCOUNT"]),
  current_revision_no: z.int().min(1).max(2_147_483_647), audit_scope: z.enum(["RECEIPT_AUDIT_NOT_VERIFIED", "RECORDED_AUDIT_MATCHED_ONLY"]),
  scope: z.literal("UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY"), payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED"),
}).strict().refine(item => {
  const direct = item.receipt_basis === "DIRECT_EVENT_ACCOUNT"; const matched = item.audit_scope === "RECORDED_AUDIT_MATCHED_ONLY";
  if (direct !== (item.entitlement_id === null) || (direct && (!matched || item.source !== "MANUAL"))
    || item.revision_no > item.current_revision_no
    || (item.revision_no === item.current_revision_no) !== sameId(item.revision_id, item.current_revision_id)) return false;
  if (!matched) return true;
  if (item.version !== (item.status === "ACTIVE" ? "1" : "2")
    || item.cash_scope !== (item.status === "ACTIVE" ? "MATCHING_CASH_RECORDED_UNVERIFIED" : "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED")) return false;
  // Refinements may follow a failing child refinement; never parse malformed financial strings.
  if (!signedMoney.safeParse(item.recorded_amount).success || (item.received_hkd !== null && !signedMoney.safeParse(item.received_hkd).success)
    || (item.fx_rate_hkd !== null && !fx.safeParse(item.fx_rate_hkd).success)) return false;
  const amount = scaled(item.recorded_amount, 6); const hkd = item.received_hkd === null ? null : scaled(item.received_hkd, 6); const min = -((1n << 63n) - 1n);
  return amount >= min && (hkd === null || hkd >= min) && (item.fx_rate_hkd === null) === (hkd === null)
    && (item.currency !== "HKD" || (item.fx_rate_hkd !== null && scaled(item.fx_rate_hkd, 12) === 1_000_000_000_000n && hkd === amount))
    && (hkd === null || !((amount === 0n && hkd !== 0n) || (amount > 0n && hkd < 0n) || (amount < 0n && hkd > 0n)));
});

export function timestampMicros(value: string): bigint {
  const fraction = /\.(\d{1,6})/.exec(value)?.[1] ?? "";
  return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, "0").slice(3));
}

function consistent(item: Receipt | CombinedReceipt, event: Event): boolean {
  if (item.scope === "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY"
    && ((item.current_revision_no === event.version) !== sameId(item.current_revision_id, event.current_revision_id)
      || (item.revision_no === event.version) !== sameId(item.revision_id, event.current_revision_id))) return false;
  if (item.status === "ACTIVE") {
    if (item.voided_at !== null || ["CASH_REVERSAL_NOT_RECORDED", "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"].includes(item.cash_scope)) return false;
  } else if (item.voided_at === null || BigInt(item.version) < 2n || timestampMicros(item.voided_at) < timestampMicros(item.created_at)
    || item.cash_scope === "MATCHING_CASH_RECORDED_UNVERIFIED") return false;
  if (sameId(item.current_revision_id, event.current_revision_id)) {
    if (item.revision_no > event.version || (item.revision_no === event.version) !== sameId(item.revision_id, item.current_revision_id)) return false;
  }
  return true;
}

function checkedPage<T extends Page | CombinedPage>(value: T, event: Event, account: Account, shape: z.ZodType): T {
  const ids = new Set<string>(); const revisions = new Map<number, string>(); const revisionIds = new Map<string, number>();
  const entitlements = new Map<string, string>(); const byRevision = new Map<string, string>();
  let current: string | null = null; let currentNo: number | null = null; let previous: Receipt | CombinedReceipt | null = null;
  if (!validEstimateEvent(event) || !z.object({ id }).safeParse(account).success || !value || !Array.isArray(value.items) || value.items.length > 25
    || (value.next_cursor != null && (typeof value.next_cursor !== "string" || !value.next_cursor || value.next_cursor.length > 1500))
    || value.items.some(item => {
      if (!shape.safeParse(item).success || !sameId(item.event_id, event.id) || !sameId(item.account_id, account.id)
        || ids.has(item.id.toLowerCase()) || !consistent(item, event)) return true;
      const revision = item.revision_id.toLowerCase(); const entitlement = item.entitlement_id?.toLowerCase() ?? null;
      if (item.scope === "UNVERIFIED_COMBINED_RECEIPT_HISTORY_ONLY") {
        if (currentNo !== null && currentNo !== item.current_revision_no) return true;
        currentNo = item.current_revision_no;
      }
      if ((current !== null && !sameId(current, item.current_revision_id))
        || (revisions.has(item.revision_no) && revisions.get(item.revision_no) !== revision)
        || (revisionIds.has(revision) && revisionIds.get(revision) !== item.revision_no)
        || (entitlement !== null && ((entitlements.has(entitlement) && entitlements.get(entitlement) !== revision)
          || (byRevision.has(revision) && byRevision.get(revision) !== entitlement)))
        || (previous !== null && (item.received_date > previous.received_date
          || (item.received_date === previous.received_date && item.id.toLowerCase() >= previous.id.toLowerCase())))) return true;
      current = item.current_revision_id; previous = item; ids.add(item.id.toLowerCase());
      revisions.set(item.revision_no, revision); revisionIds.set(revision, item.revision_no);
      if (entitlement !== null) { entitlements.set(entitlement, revision); byRevision.set(revision, entitlement); }
      return false;
    })) throw new Error("收款紀錄範圍無法安全核對；請重新載入或檢查有效備份。");
  return value;
}

export function checkedReceiptPage(value: Page, event: Event, account: Account): Page {
  return checkedPage(value, event, account, receipt);
}

export function checkedCombinedReceiptPage(value: CombinedPage, event: Event, account: Account): CombinedPage {
  return checkedPage(value, event, account, combinedReceipt);
}
