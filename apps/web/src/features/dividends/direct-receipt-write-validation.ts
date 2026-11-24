import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money } from "../ledger/inputs";
import { ordinal, positiveOrdinal } from "./estimate-validation";
import { normalizeDecisionText, validDecisionText } from "./event-validation";
import { validDirectReceiptTarget, type DirectReceiptTarget } from "./direct-receipt-input-validation";
import { fxIntent } from "./projection-input-validation";
import { source } from "./projection-validation";
import { receiptMoneyIntent, reversibleMoney } from "./receipt-input-validation";
import { sameId, timestamp } from "./receipt-validation";

type Schemas = components["schemas"];
type Write = Schemas["DirectReceiptCreate"];
type Created = Schemas["DirectReceiptCreated"];
const id = z.uuid();
const MAX = (1n << 63n) - 1n;
const FX_SCALE = 1_000_000_000_000n;
const reason = z.string().refine(value => validDecisionText(normalizeDecisionText(value), 10, 1000));
const cash = { received_date: date, received_amount: reversibleMoney, currency: z.enum(["HKD", "USD"]),
  tax: money.max(50).nullable(), fee: money.max(50).nullable(), fx_rate_hkd: fx.max(50).nullable(), fx_source: source.nullable(),
  actual_hkd: reversibleMoney.nullable(), actual_hkd_reason: reason.nullable() };
const pins = { event_id: id, revision_id: id, account_id: id, portfolio_id: id, broker_id: id,
  account_version: positiveOrdinal, portfolio_version: positiveOrdinal, broker_version: positiveOrdinal };
const writeShape = z.object({ ...cash, ...pins, receipt_id: id, expected_data_revision: ordinal,
  note: z.string().refine(value => validDecisionText(value, 0, 1000)), reason,
  confirmation: z.literal("RECORDED_CASH_ONLY"), confirm_revision_fence: z.literal(true) }).strict();
const createdShape = z.object({ ...cash, ...pins, id, revision_no: z.int().min(1).max(2_147_483_647),
  portfolio_data_revision: ordinal, cash_entry_id: id, received_hkd: reversibleMoney.nullable(),
  receipt_basis: z.literal("DIRECT_EVENT_ACCOUNT"), source: z.literal("MANUAL"), status: z.literal("ACTIVE"), version: z.literal("1"),
  created_at: timestamp, audit_scope: z.literal("RECORDED_AUDIT_MATCHED_ONLY"), scope: z.literal("HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY"),
  history_completeness: z.literal("UNVERIFIED"), current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"),
  payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED") }).strict();

function expectedHkd(body: Write): string | null {
  if ((body.fx_rate_hkd === null) !== (body.fx_source === null) || (body.actual_hkd === null) !== (body.actual_hkd_reason === null)
    || (body.currency === "HKD" && body.fx_rate_hkd !== null && (fxIntent(body.fx_rate_hkd) !== fxIntent("1") || body.fx_source !== "IDENTITY"))
    || (body.currency === "USD" && body.fx_source === "IDENTITY")) throw new Error();
  const rate = body.currency === "HKD" ? "1" : body.fx_rate_hkd;
  if (rate === null) {
    if (body.actual_hkd !== null) throw new Error();
    return null;
  }
  const amount = BigInt(receiptMoneyIntent(body.received_amount)!);
  const product = amount * BigInt(fxIntent(rate)!.replace(".", ""));
  // Match finance.lock_money: ROUND_HALF_UP only at the computed six-decimal boundary.
  const rounded = ((product < 0n ? -product : product) + FX_SCALE / 2n) / FX_SCALE * (product < 0n ? -1n : 1n);
  if (rounded > MAX || rounded < -MAX - 1n) throw new Error();
  const result = body.actual_hkd === null ? rounded : BigInt(receiptMoneyIntent(body.actual_hkd)!);
  if (result < -MAX || result > MAX || (body.currency === "HKD" && result !== amount)
    || (amount === 0n && result !== 0n) || (amount > 0n && result < 0n) || (amount < 0n && result > 0n)) throw new Error();
  return result.toString();
}

export function checkedDirectReceiptSaved(record: Created, target: DirectReceiptTarget, body: Write): Created {
  try {
    if (!validDirectReceiptTarget(target) || !writeShape.safeParse(body).success || !createdShape.safeParse(record).success) throw new Error();
    const { inputs } = target;
    for (const value of [body, record]) {
      if (!sameId(value.event_id, inputs.event_id) || !sameId(value.revision_id, inputs.revision_id)
        || !sameId(value.account_id, inputs.account_id) || !sameId(value.portfolio_id, inputs.portfolio_id) || !sameId(value.broker_id, inputs.broker_id)
        || value.account_version !== inputs.account_version || value.portfolio_version !== inputs.portfolio_version || value.broker_version !== inputs.broker_version) throw new Error();
    }
    if (!sameId(record.id, body.receipt_id) || record.revision_no !== inputs.revision_no
      || body.expected_data_revision !== inputs.portfolio_data_revision || record.portfolio_data_revision !== inputs.portfolio_data_revision
      || record.currency !== body.currency || record.received_date !== body.received_date
      || receiptMoneyIntent(record.received_amount) !== receiptMoneyIntent(body.received_amount)
      || receiptMoneyIntent(record.tax) !== receiptMoneyIntent(body.tax) || receiptMoneyIntent(record.fee) !== receiptMoneyIntent(body.fee)
      || fxIntent(record.fx_rate_hkd) !== fxIntent(body.currency === "HKD" ? "1" : body.fx_rate_hkd)
      || record.fx_source !== (body.currency === "HKD" ? "IDENTITY" : body.fx_source)
      || receiptMoneyIntent(record.actual_hkd) !== receiptMoneyIntent(body.actual_hkd)
      || record.actual_hkd_reason !== (body.actual_hkd_reason === null ? null : normalizeDecisionText(body.actual_hkd_reason))
      || receiptMoneyIntent(record.received_hkd) !== expectedHkd(body)) throw new Error();
    return record;
  } catch {
    throw new Error("伺服器回覆與本次直接收款不符；請保留輸入並核對收款及現金歷史，不會假定未儲存。");
  }
}
