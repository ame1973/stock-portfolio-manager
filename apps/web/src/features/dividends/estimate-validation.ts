import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, money } from "../ledger/inputs";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Tax = Schemas["TaxSettingSelection"];
type Fee = Schemas["FeeSettingSelection"];
type Setting = Schemas["TaxSettingRecord"] | Schemas["FeeSettingRecord"];
const MAX = 9223372036854775807n;
export const ordinal = z.string().regex(/^(0|[1-9][0-9]{0,18})$/).refine(value => /^(0|[1-9][0-9]{0,18})$/.test(value) && BigInt(value) <= MAX);
export const positiveOrdinal = ordinal.refine(value => value !== "0");
const id = z.uuid();
const sameId = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();
const eventScope = z.object({ id, current_revision_id: id, version: z.int().min(1).max(2_147_483_647), security_id: id,
  scope: z.literal("OWNER_EVENT_DECISION_ONLY"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
  document_verification: z.literal("NOT_ESTABLISHED"), eligibility_scope: z.literal("NOT_ESTABLISHED") });
export function validEstimateEvent(value: unknown): value is Event { return eventScope.safeParse(value).success; }
const amount = money.max(50);
export const estimateRate = amount.refine(value => {
  if (!amount.safeParse(value).success) return false;
  const [whole, fraction = ""] = value.split(".");
  return BigInt(whole!) * 1_000_000n + BigInt(fraction.padEnd(6, "0") || "0") <= 1_000_000n;
}, "稅率請輸入 0 至 1 的小數，最多 6 位小數。");
const settingBase = {
  id, event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647), account_id: id, portfolio_id: id, broker_id: id,
  account_version: positiveOrdinal, broker_version: positiveOrdinal, setting_no: positiveOrdinal, previous_id: id.nullable(), effective_from: date,
  reason: z.string().min(10).max(1000), created_at: z.iso.datetime({ offset: true }), scope: z.literal("OWNER_ESTIMATE_ONLY"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
};
const taxRecord = z.object({ ...settingBase, tax_status: z.enum(["ESTIMATED", "UNKNOWN"]), rate: estimateRate.nullable() }).refine(value =>
  (value.setting_no === "1") === (value.previous_id === null) && (value.previous_id === null || !sameId(value.previous_id, value.id))
  && (value.tax_status === "ESTIMATED") === (value.rate !== null));
const feeRecord = z.object({ ...settingBase, fee_status: z.enum(["ESTIMATED", "UNKNOWN"]), fee: amount.nullable(), fee_currency: z.enum(["HKD", "USD"]).nullable() }).refine(value =>
  (value.setting_no === "1") === (value.previous_id === null) && (value.previous_id === null || !sameId(value.previous_id, value.id))
  && (value.fee_status === "ESTIMATED" ? value.fee !== null && value.fee_currency !== null : value.fee === null));
const selectionBase = {
  event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647), account_id: id, portfolio_id: id, broker_id: id,
  account_version: positiveOrdinal, broker_version: positiveOrdinal, effective_on: date, head_no: ordinal, head_id: id.nullable(),
  selected_scope: z.enum(["NO_SELECTED_SETTING", "ACCOUNT_SCOPE_MATCH", "ACCOUNT_SCOPE_CHANGED"]),
  scope: z.literal("OWNER_ESTIMATE_ONLY"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
};
const taxSelection = z.object({ ...selectionBase, tax_status: z.enum(["MISSING", "UNKNOWN", "ESTIMATED"]), selected: taxRecord.nullable() });
const feeSelection = z.object({ ...selectionBase, fee_status: z.enum(["MISSING", "UNKNOWN", "ESTIMATED"]), selected: feeRecord.nullable(), revision_rejection: z.enum(["REJECTED", "NOT_REJECTED"]) });

function scopeMatches(value: Tax | Fee, event: Event, account: Account, effective: string): boolean {
  if (!sameId(value.event_id, event.id) || !sameId(value.revision_id, event.current_revision_id) || value.revision_no !== event.version
    || !sameId(value.account_id, account.id) || !sameId(value.portfolio_id, account.portfolio_id) || !sameId(value.broker_id, account.broker_id)
    || value.effective_on !== effective || (value.head_no === "0") !== (value.head_id === null)) return false;
  const selected = value.selected;
  if (selected === null) return value.selected_scope === "NO_SELECTED_SETTING" && ("tax_status" in value ? value.tax_status : value.fee_status) === "MISSING";
  if (!sameId(selected.event_id, event.id) || !sameId(selected.revision_id, event.current_revision_id) || selected.revision_no !== event.version
    || !sameId(selected.account_id, account.id) || selected.effective_from > effective || BigInt(selected.setting_no) > BigInt(value.head_no)
    || (selected.setting_no === value.head_no) !== (value.head_id !== null && sameId(selected.id, value.head_id))
    || ("tax_status" in value ? value.tax_status !== (selected as Schemas["TaxSettingRecord"]).tax_status : value.fee_status !== (selected as Schemas["FeeSettingRecord"]).fee_status)) return false;
  const matches = sameId(selected.portfolio_id, value.portfolio_id) && sameId(selected.broker_id, value.broker_id)
    && selected.account_version === value.account_version && selected.broker_version === value.broker_version;
  return value.selected_scope === (matches ? "ACCOUNT_SCOPE_MATCH" : "ACCOUNT_SCOPE_CHANGED");
}

export function checkedTax(value: Tax, event: Event, account: Account, effective: string): Tax {
  if (!taxSelection.safeParse(value).success || !scopeMatches(value, event, account, effective)) throw new Error("稅率估算範圍無法安全核對；請重新載入事件、帳戶及估算。");
  return value;
}
export function checkedFee(value: Fee, event: Event, account: Account, effective: string): Fee {
  if (!feeSelection.safeParse(value).success || !scopeMatches(value, event, account, effective)) throw new Error("費用估算範圍無法安全核對；請重新載入事件、帳戶及估算。");
  return value;
}

export function checkedHistory<T extends Setting>(value: { items: T[]; next_cursor?: string | null }, event: Event, account: Account, kind: "tax" | "fee"): { items: T[]; next_cursor?: string | null } {
  let previous: bigint | null = null; const ids = new Set<string>();
  if (!value || !Array.isArray(value.items) || value.items.length > 25 || (value.next_cursor != null && typeof value.next_cursor !== "string")
    || value.items.some(item => {
      if (!(kind === "tax" ? taxRecord : feeRecord).safeParse(item).success || !sameId(item.event_id, event.id)
        || !sameId(item.revision_id, event.current_revision_id) || item.revision_no !== event.version || !sameId(item.account_id, account.id)
        || ids.has(item.id.toLowerCase())) return true;
      const current = BigInt(item.setting_no);
      if (previous !== null && current >= previous) return true;
      previous = current; ids.add(item.id.toLowerCase()); return false;
    })) throw new Error("估算歷史範圍無法安全核對；請重新載入或檢查有效備份。");
  return value;
}
