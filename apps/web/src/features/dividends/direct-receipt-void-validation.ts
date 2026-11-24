import { z } from "zod";
import type { components } from "@spm/contracts";
import { checkedDirectReceiptInputs, type DirectReceiptTarget } from "./direct-receipt-input-validation";
import { ordinal, positiveOrdinal } from "./estimate-validation";
import { normalizeDecisionText, validDecisionText } from "./event-validation";
import { fxIntent } from "./projection-input-validation";
import { receiptMoneyIntent } from "./receipt-input-validation";
import { checkedCombinedReceiptPage, sameId, timestamp, timestampMicros } from "./receipt-validation";

type Schemas = components["schemas"];
type Receipt = Schemas["CombinedRecordedReceipt"];
type Inputs = Schemas["DirectReceiptVoidInputs"];
type Write = Schemas["DirectReceiptVoidWrite"];
type Voided = Schemas["DirectReceiptVoided"];
export type DirectReceiptVoidTarget = Omit<DirectReceiptTarget, "inputs"> & { receipt: Receipt; inputs: Inputs };
const id = z.uuid(); const version = z.int().min(1).max(2_147_483_647);
const MAX = (1n << 63n) - 1n;
const inputsShape = z.object({ current: z.unknown(), receipt: z.unknown(), cash_entry_id: id,
  origin_portfolio_id: id, scope: z.literal("DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY") }).strict();
const pins = { event_id: id, observed_revision_id: id, observed_revision_no: version, original_revision_id: id,
  original_revision_no: version, account_id: id, portfolio_id: id, broker_id: id, account_version: positiveOrdinal,
  portfolio_version: positiveOrdinal, broker_version: positiveOrdinal, cash_entry_id: id };
const writeShape = z.object({ ...pins, expected_data_revision: ordinal,
  reason: z.string().refine(value => validDecisionText(normalizeDecisionText(value), 10, 1000)), confirmation: z.literal("VOID_RECORDED_CASH") }).strict();
const voidedShape = z.object({ ...pins, id, origin_portfolio_id: id, portfolio_data_revision: ordinal, reversal_entry_id: id,
  receipt_basis: z.literal("DIRECT_EVENT_ACCOUNT"), status: z.literal("VOID"), version: z.literal("2"), created_at: timestamp, voided_at: timestamp,
  audit_scope: z.literal("RECORDED_AUDIT_MATCHED_ONLY"), scope: z.literal("HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY"),
  history_completeness: z.literal("UNVERIFIED"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
  eligibility_scope: z.literal("NOT_ESTABLISHED"), payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED") }).strict();

export function checkedDirectReceiptVoidInputs(value: Inputs, event: DirectReceiptTarget["event"], account: DirectReceiptTarget["account"], receipt: Receipt): Inputs {
  try {
    if (!inputsShape.safeParse(value).success) throw new Error();
    checkedDirectReceiptInputs(value.current, event, account);
    const observed = value.receipt;
    for (const item of [receipt, observed]) {
      checkedCombinedReceiptPage({ items: [item] }, event, account);
      if (item.receipt_basis !== "DIRECT_EVENT_ACCOUNT" || !sameId(item.current_revision_id, value.current.revision_id)
        || item.current_revision_no !== value.current.revision_no) throw new Error();
    }
    if ((["id", "event_id", "account_id", "revision_id", "current_revision_id"] as const).some(key => !sameId(receipt[key], observed[key]))
      || (["revision_no", "current_revision_no", "received_date", "currency", "status", "version"] as const).some(key => receipt[key] !== observed[key])
      || (["recorded_amount", "recorded_tax", "recorded_fee", "received_hkd"] as const).some(key => receiptMoneyIntent(receipt[key]) !== receiptMoneyIntent(observed[key]))
      || fxIntent(receipt.fx_rate_hkd) !== fxIntent(observed.fx_rate_hkd)
      || timestampMicros(receipt.created_at) !== timestampMicros(observed.created_at)
      || (receipt.voided_at === null) !== (observed.voided_at === null)
      || (receipt.voided_at !== null && observed.voided_at !== null && timestampMicros(receipt.voided_at) !== timestampMicros(observed.voided_at))) throw new Error();
    return value;
  } catch {
    throw new Error("直接收款作廢輸入範圍無法安全核對；請重新載入事件、帳戶及收款紀錄。");
  }
}

export function validDirectReceiptVoidTarget(target: DirectReceiptVoidTarget): boolean {
  try {
    const { event, account, receipt, inputs } = target;
    checkedDirectReceiptVoidInputs(inputs, event, account, receipt);
    // Correction may use an archived account; do not apply the new-cash readiness predicate.
    return inputs.receipt.status === "ACTIVE" && inputs.receipt.version === "1" && BigInt(inputs.current.portfolio_data_revision) < MAX;
  } catch { return false; }
}

export function checkedDirectReceiptVoided(record: Voided, target: DirectReceiptVoidTarget, body: Write): Voided {
  try {
    if (!validDirectReceiptVoidTarget(target) || !writeShape.safeParse(body).success || !voidedShape.safeParse(record).success) throw new Error();
    const { inputs } = target; const { current, receipt } = inputs;
    for (const value of [body, record]) {
      if (([[value.event_id, current.event_id], [value.observed_revision_id, current.revision_id], [value.original_revision_id, receipt.revision_id],
        [value.account_id, current.account_id], [value.portfolio_id, current.portfolio_id], [value.broker_id, current.broker_id],
        [value.cash_entry_id, inputs.cash_entry_id]] as const).some(([left, right]) => !sameId(left, right))
        || value.observed_revision_no !== current.revision_no || value.original_revision_no !== receipt.revision_no
        || value.account_version !== current.account_version || value.portfolio_version !== current.portfolio_version || value.broker_version !== current.broker_version) throw new Error();
    }
    if (!sameId(record.id, receipt.id) || !sameId(record.origin_portfolio_id, inputs.origin_portfolio_id)
      || sameId(record.reversal_entry_id, inputs.cash_entry_id)
      || body.expected_data_revision !== current.portfolio_data_revision || record.portfolio_data_revision !== current.portfolio_data_revision
      || timestampMicros(record.created_at) !== timestampMicros(receipt.created_at)
      || timestampMicros(record.voided_at) < timestampMicros(record.created_at)) throw new Error();
    return record;
  } catch {
    throw new Error("伺服器回覆與本次直接收款作廢不符；請保留輸入並核對收款及現金歷史，不會假定未儲存。");
  }
}
