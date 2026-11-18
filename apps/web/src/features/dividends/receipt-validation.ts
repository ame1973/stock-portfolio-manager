import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money, signedMoney } from "../ledger/inputs";
import { positiveOrdinal, validEstimateEvent } from "./estimate-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Receipt = Schemas["RecordedReceipt"];
type Page = Schemas["Page_RecordedReceipt_"];
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

function micros(value: string): bigint {
  const fraction = /\.(\d{1,6})/.exec(value)?.[1] ?? "";
  return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, "0").slice(3));
}

function consistent(item: Receipt, event: Event): boolean {
  if (item.status === "ACTIVE") {
    if (item.voided_at !== null || ["CASH_REVERSAL_NOT_RECORDED", "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED"].includes(item.cash_scope)) return false;
  } else if (item.voided_at === null || BigInt(item.version) < 2n || micros(item.voided_at) < micros(item.created_at)
    || item.cash_scope === "MATCHING_CASH_RECORDED_UNVERIFIED") return false;
  if (sameId(item.current_revision_id, event.current_revision_id)) {
    if (item.revision_no > event.version || (item.revision_no === event.version) !== sameId(item.revision_id, item.current_revision_id)) return false;
  }
  return true;
}

export function checkedReceiptPage(value: Page, event: Event, account: Account): Page {
  const ids = new Set<string>(); const revisions = new Map<number, string>(); const revisionIds = new Map<string, number>();
  const entitlements = new Map<string, string>(); const byRevision = new Map<string, string>(); let current: string | null = null; let previous: Receipt | null = null;
  if (!validEstimateEvent(event) || !z.object({ id }).safeParse(account).success || !value || !Array.isArray(value.items) || value.items.length > 25
    || (value.next_cursor != null && (typeof value.next_cursor !== "string" || !value.next_cursor || value.next_cursor.length > 1500))
    || value.items.some(item => {
      if (!receipt.safeParse(item).success || !sameId(item.event_id, event.id) || !sameId(item.account_id, account.id)
        || ids.has(item.id.toLowerCase()) || !consistent(item, event)) return true;
      const revision = item.revision_id.toLowerCase(); const entitlement = item.entitlement_id.toLowerCase();
      if ((current !== null && !sameId(current, item.current_revision_id))
        || (revisions.has(item.revision_no) && revisions.get(item.revision_no) !== revision)
        || (revisionIds.has(revision) && revisionIds.get(revision) !== item.revision_no)
        || (entitlements.has(entitlement) && entitlements.get(entitlement) !== revision)
        || (byRevision.has(revision) && byRevision.get(revision) !== entitlement)
        || (previous !== null && (item.received_date > previous.received_date
          || (item.received_date === previous.received_date && item.id.toLowerCase() >= previous.id.toLowerCase())))) return true;
      current = item.current_revision_id; previous = item; ids.add(item.id.toLowerCase());
      revisions.set(item.revision_no, revision); revisionIds.set(revision, item.revision_no); entitlements.set(entitlement, revision); byRevision.set(revision, entitlement);
      return false;
    })) throw new Error("收款紀錄範圍無法安全核對；請重新載入或檢查有效備份。");
  return value;
}
