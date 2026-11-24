import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money, signedMoney } from "../ledger/inputs";
import { ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { reversibleMoney } from "./receipt-input-validation";
import { checkedReceiptPage, sameId, timestamp, timestampMicros } from "./receipt-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Receipt = Schemas["RecordedReceipt"];
type Inputs = Schemas["ReceiptVoidInputs"];
type Write = Schemas["ReceiptVoidWrite"];
type Voided = Schemas["ReceiptVoided"];
export type ReceiptVoidTarget = { event: Event; account: Account; receipt: Receipt; inputs: Inputs };

const id = z.uuid();
const version = z.int().min(1).max(2_147_483_647);
const MAX = (1n << 63n) - 1n;
const scope = { history_completeness: z.literal("UNVERIFIED"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
  eligibility_scope: z.literal("NOT_ESTABLISHED"), payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED") };
const inputsShape = z.object({
  receipt_id: id, event_id: id, observed_revision_id: id, observed_revision_no: version, account_id: id, portfolio_id: id, broker_id: id,
  account_status: z.enum(["ACTIVE", "ARCHIVED"]), account_version: positiveOrdinal, portfolio_version: positiveOrdinal,
  broker_version: positiveOrdinal, portfolio_data_revision: ordinal, entitlement_id: id, original_revision_id: id,
  cash_entry_id: id.nullable(), origin_portfolio_id: id.nullable(), received_date: date, received_amount: signedMoney.max(50),
  currency: z.enum(["HKD", "USD"]), tax: money.max(50).nullable(), fee: money.max(50).nullable(),
  fx_rate_hkd: fx.max(50).nullable(), received_hkd: signedMoney.max(50).nullable(), status: z.enum(["ACTIVE", "VOID"]),
  receipt_version: positiveOrdinal, created_at: timestamp, voided_at: timestamp.nullable(),
  audit_scope: z.enum(["RECEIPT_AUDIT_NOT_VERIFIED", "RECORDED_AUDIT_MATCHED_ONLY"]), scope: z.literal("RECORDED_RECEIPT_VOID_INPUTS_ONLY"), ...scope,
});
const voidedShape = z.object({
  id, event_id: id, observed_revision_id: id, observed_revision_no: version, entitlement_id: id, original_revision_id: id,
  account_id: id, portfolio_id: id, origin_portfolio_id: id, broker_id: id, account_version: positiveOrdinal,
  portfolio_version: positiveOrdinal, broker_version: positiveOrdinal, portfolio_data_revision: ordinal, cash_entry_id: id,
  reversal_entry_id: id, status: z.literal("VOID"), version: z.literal("2"), created_at: timestamp, voided_at: timestamp,
  audit_scope: z.literal("RECORDED_AUDIT_MATCHED_ONLY"), scope: z.literal("HISTORICAL_RECEIPT_VOID_RESPONSE_ONLY"), ...scope,
});

function scaled(value: string, places: number): bigint {
  const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole! + fraction.padEnd(places, "0")) * (value.startsWith("-") ? -1n : 1n);
}
function sameDecimal(left: string | null, right: string | null, places: number): boolean {
  return left === null || right === null ? left === right : scaled(left, places) === scaled(right, places);
}

export function checkedReceiptVoidInputs(value: Inputs, event: Event, account: Account, receipt: Receipt): Inputs {
  try {
    if (!validEstimateEvent(event) || !inputsShape.safeParse(value).success
      || !z.object({ id, portfolio_id: id, broker_id: id, status: z.enum(["ACTIVE", "ARCHIVED"]) }).safeParse(account).success) throw new Error();
    checkedReceiptPage({ items: [receipt] }, event, account);
    const matched = value.audit_scope === "RECORDED_AUDIT_MATCHED_ONLY";
    if (!sameId(value.receipt_id, receipt.id) || !sameId(value.event_id, event.id)
      || !sameId(value.observed_revision_id, event.current_revision_id) || value.observed_revision_no !== event.version
      || !sameId(receipt.current_revision_id, event.current_revision_id) || !sameId(value.account_id, account.id)
      || !sameId(value.portfolio_id, account.portfolio_id) || !sameId(value.broker_id, account.broker_id) || value.account_status !== account.status
      || !sameId(value.entitlement_id, receipt.entitlement_id) || !sameId(value.original_revision_id, receipt.revision_id)
      || value.received_date !== receipt.received_date || value.currency !== receipt.currency
      || !sameDecimal(value.received_amount, receipt.recorded_amount, 6) || !sameDecimal(value.tax, receipt.recorded_tax, 6)
      || !sameDecimal(value.fee, receipt.recorded_fee, 6) || !sameDecimal(value.fx_rate_hkd, receipt.fx_rate_hkd, 12)
      || !sameDecimal(value.received_hkd, receipt.received_hkd, 6) || value.status !== receipt.status || value.receipt_version !== receipt.version
      || timestampMicros(value.created_at) !== timestampMicros(receipt.created_at)
      || (value.voided_at === null) !== (receipt.voided_at === null)
      || (value.voided_at !== null && receipt.voided_at !== null && timestampMicros(value.voided_at) !== timestampMicros(receipt.voided_at))
      || (value.cash_entry_id === null) !== (value.origin_portfolio_id === null) || matched !== (value.cash_entry_id !== null)
      || (matched && (value.receipt_version !== (value.status === "ACTIVE" ? "1" : "2")
        || receipt.cash_scope !== (value.status === "ACTIVE" ? "MATCHING_CASH_RECORDED_UNVERIFIED" : "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED")))) throw new Error();
    if (matched) {
      const native = scaled(value.received_amount, 6); const hkd = value.received_hkd === null ? null : scaled(value.received_hkd, 6);
      if ((value.fx_rate_hkd === null) !== (hkd === null)
        || (value.currency === "HKD" && (value.fx_rate_hkd === null || scaled(value.fx_rate_hkd, 12) !== 1_000_000_000_000n || hkd !== native))
        || (hkd !== null && ((native === 0n && hkd !== 0n) || (native > 0n && hkd < 0n) || (native < 0n && hkd > 0n)))) throw new Error();
    }
    return value;
  } catch {
    throw new Error("收款作廢輸入範圍無法安全核對；請重新載入事件、帳戶及收款紀錄。");
  }
}

export function validReceiptVoidTarget(target: ReceiptVoidTarget): boolean {
  try {
    const { event, account, receipt, inputs } = target;
    checkedReceiptVoidInputs(inputs, event, account, receipt);
    return inputs.status === "ACTIVE" && inputs.receipt_version === "1" && inputs.audit_scope === "RECORDED_AUDIT_MATCHED_ONLY"
      && inputs.cash_entry_id !== null && inputs.origin_portfolio_id !== null && BigInt(inputs.portfolio_data_revision) < MAX
      && reversibleMoney.safeParse(inputs.received_amount).success
      && (inputs.received_hkd === null || reversibleMoney.safeParse(inputs.received_hkd).success);
  } catch { return false; }
}

export function checkedReceiptVoided(record: Voided, target: ReceiptVoidTarget, body: Write): Voided {
  try {
    if (!validReceiptVoidTarget(target) || !voidedShape.safeParse(record).success) throw new Error();
    const { inputs } = target; const cash = inputs.cash_entry_id; const origin = inputs.origin_portfolio_id;
    if (cash === null || origin === null) throw new Error();
    const pairs: [string, string][] = [
      [body.event_id, inputs.event_id], [body.observed_revision_id, inputs.observed_revision_id], [body.entitlement_id, inputs.entitlement_id],
      [body.original_revision_id, inputs.original_revision_id], [body.account_id, inputs.account_id], [body.portfolio_id, inputs.portfolio_id],
      [body.broker_id, inputs.broker_id], [body.cash_entry_id, cash], [record.id, inputs.receipt_id], [record.event_id, inputs.event_id],
      [record.observed_revision_id, inputs.observed_revision_id], [record.entitlement_id, inputs.entitlement_id],
      [record.original_revision_id, inputs.original_revision_id], [record.account_id, inputs.account_id], [record.portfolio_id, inputs.portfolio_id],
      [record.origin_portfolio_id, origin], [record.broker_id, inputs.broker_id], [record.cash_entry_id, cash],
    ];
    if (pairs.some(([left, right]) => !sameId(left, right)) || sameId(record.reversal_entry_id, cash)
      || body.observed_revision_no !== inputs.observed_revision_no || record.observed_revision_no !== inputs.observed_revision_no
      || body.account_version !== inputs.account_version || record.account_version !== inputs.account_version
      || body.portfolio_version !== inputs.portfolio_version || record.portfolio_version !== inputs.portfolio_version
      || body.broker_version !== inputs.broker_version || record.broker_version !== inputs.broker_version
      || body.expected_data_revision !== inputs.portfolio_data_revision || record.portfolio_data_revision !== inputs.portfolio_data_revision
      || body.confirmation !== "VOID_RECORDED_CASH" || timestampMicros(record.created_at) !== timestampMicros(inputs.created_at)
      || timestampMicros(record.voided_at) < timestampMicros(record.created_at)) throw new Error();
    return record;
  } catch {
    throw new Error("伺服器回覆與本次作廢不符；請保留輸入並核對收款及現金歷史，不會假定未儲存。");
  }
}
