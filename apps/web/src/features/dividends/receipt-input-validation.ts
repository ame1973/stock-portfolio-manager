import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, fx, money, quantity, signedMoney } from "../ledger/inputs";
import { ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { fxIntent } from "./projection-input-validation";
import { source } from "./projection-validation";
import { sameId, timestamp } from "./receipt-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Inputs = Schemas["ReceiptInputs"];
type Basis = Schemas["ExistingReceiptBasis"];
type Write = Schemas["ReceiptCreate"];
type Created = Schemas["ReceiptCreated"];
export type ReceiptTarget = { event: Event; account: Account; inputs: Inputs; basis: Basis };
const MAX = (1n << 63n) - 1n;
const id = z.uuid();
const version = z.int().min(1).max(2_147_483_647);

function micros(value: string): bigint {
  const negative = value.startsWith("-"); const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole! + fraction.padEnd(6, "0")) * (negative ? -1n : 1n);
}
export const reversibleMoney = signedMoney.max(50).refine(value =>
  signedMoney.max(50).safeParse(value).success && micros(value) >= -MAX, "金額及其沖銷必須符合 INT64 範圍。"
);
export function receiptMoneyIntent(value: string | null): string | null {
  return value === null ? null : micros(reversibleMoney.parse(value)).toString();
}
const basis = z.object({ id, revision_id: id, revision_no: version, currency: z.enum(["HKD", "USD"]),
  recorded_shares: quantity.max(50), recorded_expected_net: signedMoney.max(50),
  recorded_quality: z.enum(["ESTIMATED", "ACCRUED", "NEEDS_REVIEW", "CONFIRMED", "MISSING_FX"]),
  created_at: timestamp, scope: z.literal("LEGACY_ENTITLEMENT_NOT_VERIFIED") });
const inputsShape = z.object({ event_id: id, revision_id: id, revision_no: version, account_id: id, portfolio_id: id, broker_id: id,
  account_status: z.enum(["ACTIVE", "ARCHIVED"]), account_version: positiveOrdinal, portfolio_version: positiveOrdinal,
  broker_version: positiveOrdinal, portfolio_data_revision: ordinal,
  entitlements: z.object({ items: z.array(basis).max(25), next_cursor: z.string().min(1).max(1500).nullable().optional() }),
  scope: z.literal("RECORDED_RECEIPT_INPUTS_ONLY"), history_completeness: z.literal("UNVERIFIED"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED") });

export function checkedReceiptInputs(inputs: Inputs, event: Event, account: Account): Inputs {
  const valid = validEstimateEvent(event) && inputsShape.safeParse(inputs).success
    && z.object({ id, portfolio_id: id, broker_id: id, status: z.enum(["ACTIVE", "ARCHIVED"]) }).safeParse(account).success
    && sameId(inputs.event_id, event.id) && sameId(inputs.revision_id, event.current_revision_id) && inputs.revision_no === event.version
    && sameId(inputs.account_id, account.id) && sameId(inputs.portfolio_id, account.portfolio_id)
    && sameId(inputs.broker_id, account.broker_id) && inputs.account_status === account.status;
  if (!valid) throw new Error("收款輸入範圍無法安全核對；請重新載入事件、帳戶及輸入快照。");
  const ids = new Set<string>(); const revisions = new Set<string>(); const numbers = new Set<number>(); let previous: Basis | null = null;
  for (const item of inputs.entitlements.items) {
    const identifier = item.id.toLowerCase(); const revision = item.revision_id.toLowerCase();
    if (ids.has(identifier) || revisions.has(revision) || numbers.has(item.revision_no) || item.revision_no > event.version
      || (item.revision_no === event.version) !== sameId(item.revision_id, event.current_revision_id)
      || (previous !== null && (item.revision_no > previous.revision_no
        || (item.revision_no === previous.revision_no && identifier >= previous.id.toLowerCase())))) {
      throw new Error("已記錄應收範圍無法安全核對；請重新載入，不會推定資格或金額。");
    }
    ids.add(identifier); revisions.add(revision); numbers.add(item.revision_no); previous = item;
  }
  return inputs;
}

export function validReceiptTarget(target: ReceiptTarget): boolean {
  try {
    const { event, account, inputs, basis: selected } = target;
    checkedReceiptInputs(inputs, event, account);
    if (!basis.safeParse(selected).success || inputs.account_status !== "ACTIVE" || BigInt(inputs.portfolio_data_revision) >= MAX) return false;
    const found = inputs.entitlements.items.find(item => sameId(item.id, selected.id));
    return !!found && sameId(found.revision_id, selected.revision_id) && found.revision_no === selected.revision_no
      && found.currency === selected.currency && found.recorded_shares === selected.recorded_shares
      && found.recorded_expected_net === selected.recorded_expected_net && found.recorded_quality === selected.recorded_quality
      && found.created_at === selected.created_at && found.scope === selected.scope;
  } catch { return false; }
}

const createdShape = z.object({ id, event_id: id, account_id: id, entitlement_id: id, original_revision_id: id, original_revision_no: version,
  observed_revision_id: id, observed_revision_no: version, portfolio_id: id, broker_id: id, account_version: positiveOrdinal,
  portfolio_version: positiveOrdinal, broker_version: positiveOrdinal, portfolio_data_revision: ordinal, cash_entry_id: id,
  received_date: date, received_amount: reversibleMoney, currency: z.enum(["HKD", "USD"]), tax: money.max(50).nullable(), fee: money.max(50).nullable(),
  fx_rate_hkd: fx.max(50).nullable(), fx_source: source.nullable(),
  received_hkd: reversibleMoney.nullable(), actual_hkd_override: z.boolean(), source: z.literal("MANUAL"), status: z.literal("ACTIVE"),
  version: z.literal("1"), created_at: timestamp, audit_scope: z.literal("RECORDED_AUDIT_MATCHED_ONLY"),
  scope: z.literal("HISTORICAL_RECEIPT_CREATION_RESPONSE_ONLY"), history_completeness: z.literal("UNVERIFIED"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"), payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED") });

export function checkedReceiptSaved(record: Created, target: ReceiptTarget, body: Write): Created {
  try {
    if (!validReceiptTarget(target) || !createdShape.safeParse(record).success) throw new Error();
    const { event, account, inputs, basis: selected } = target;
    const expectedRate = body.fx_rate_hkd ?? (body.currency === "HKD" ? "1" : null);
    const expectedSource = body.fx_source ?? (body.currency === "HKD" ? "IDENTITY" : null);
    if (!sameId(body.event_id, event.id) || !sameId(body.revision_id, inputs.revision_id) || !sameId(body.account_id, account.id)
      || !sameId(body.entitlement_id, selected.id) || !sameId(body.portfolio_id, inputs.portfolio_id) || !sameId(body.broker_id, inputs.broker_id)
      || body.account_version !== inputs.account_version || body.portfolio_version !== inputs.portfolio_version || body.broker_version !== inputs.broker_version
      || body.expected_data_revision !== inputs.portfolio_data_revision || body.confirmation !== "RECORDED_CASH_ONLY"
      || !sameId(record.id, body.receipt_id) || !sameId(record.event_id, event.id) || !sameId(record.account_id, account.id)
      || !sameId(record.entitlement_id, selected.id) || !sameId(record.original_revision_id, selected.revision_id) || record.original_revision_no !== selected.revision_no
      || !sameId(record.observed_revision_id, inputs.revision_id) || record.observed_revision_no !== inputs.revision_no
      || !sameId(record.portfolio_id, inputs.portfolio_id) || !sameId(record.broker_id, inputs.broker_id)
      || record.account_version !== inputs.account_version || record.portfolio_version !== inputs.portfolio_version
      || record.broker_version !== inputs.broker_version || record.portfolio_data_revision !== inputs.portfolio_data_revision
      || record.received_date !== body.received_date || record.currency !== body.currency
      || receiptMoneyIntent(record.received_amount) !== receiptMoneyIntent(body.received_amount)
      || receiptMoneyIntent(record.tax) !== receiptMoneyIntent(body.tax) || receiptMoneyIntent(record.fee) !== receiptMoneyIntent(body.fee)
      || fxIntent(record.fx_rate_hkd) !== fxIntent(expectedRate) || record.fx_source !== expectedSource
      || record.actual_hkd_override !== (body.actual_hkd !== null) || (record.received_hkd === null) !== (expectedRate === null)) throw new Error();
    if (body.actual_hkd !== null && receiptMoneyIntent(record.received_hkd) !== receiptMoneyIntent(body.actual_hkd)) throw new Error();
    if (body.currency === "HKD" && receiptMoneyIntent(record.received_hkd) !== receiptMoneyIntent(body.received_amount)) throw new Error();
    if (record.received_hkd !== null) {
      const amount = micros(record.received_amount); const hkd = micros(record.received_hkd);
      if ((amount === 0n && hkd !== 0n) || (amount < 0n && hkd > 0n) || (amount > 0n && hkd < 0n)) throw new Error();
    }
    return record;
  } catch {
    throw new Error("伺服器回覆與本次收款不符；請保留輸入並核對收款歷史，不會假定未儲存。");
  }
}
