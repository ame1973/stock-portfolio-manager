import { z } from "zod";
import type { components } from "@spm/contracts";
import { ordinal, positiveOrdinal, validEstimateEvent } from "./estimate-validation";
import { sameId } from "./receipt-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Inputs = Schemas["DirectReceiptInputs"];
export type DirectReceiptTarget = { event: Event; account: Account; inputs: Inputs };
const id = z.uuid();
const inputsShape = z.object({
  event_id: id, revision_id: id, revision_no: z.int().min(1).max(2_147_483_647),
  workflow_status: z.enum(["PROVISIONAL", "CORROBORATED", "VERIFIED", "CONFLICT", "INCOMPLETE", "WITHDRAWN"]),
  account_id: id, portfolio_id: id, broker_id: id, account_status: z.enum(["ACTIVE", "ARCHIVED"]),
  account_version: positiveOrdinal, portfolio_version: positiveOrdinal, broker_version: positiveOrdinal, portfolio_data_revision: ordinal,
  rejection_state: z.enum(["NOT_REJECTED", "REJECTED"]), rejection_id: id.nullable(), receipt_basis: z.literal("DIRECT_EVENT_ACCOUNT"),
  scope: z.literal("DIRECT_RECORDED_RECEIPT_INPUTS_ONLY"), history_completeness: z.literal("UNVERIFIED"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED"), eligibility_scope: z.literal("NOT_ESTABLISHED"), payment_scope: z.literal("OWNER_RECORDED_NOT_VERIFIED"),
}).strict();

export function checkedDirectReceiptInputs(inputs: Inputs, event: Event, account: Account): Inputs {
  const valid = validEstimateEvent(event) && inputsShape.safeParse(inputs).success
    && z.object({ id, portfolio_id: id, broker_id: id, status: z.enum(["ACTIVE", "ARCHIVED"]) }).safeParse(account).success
    && sameId(inputs.event_id, event.id) && sameId(inputs.revision_id, event.current_revision_id) && inputs.revision_no === event.version
    && inputs.workflow_status === event.workflow_status && sameId(inputs.account_id, account.id)
    && sameId(inputs.portfolio_id, account.portfolio_id) && sameId(inputs.broker_id, account.broker_id)
    && inputs.account_status === account.status && (inputs.rejection_state === "REJECTED") === (inputs.rejection_id !== null);
  if (!valid) throw new Error("直接收款輸入範圍無法安全核對；請重新載入事件、帳戶及輸入快照。");
  return inputs;
}

export function validDirectReceiptTarget(target: DirectReceiptTarget): boolean {
  try {
    const { event, account, inputs } = target;
    checkedDirectReceiptInputs(inputs, event, account);
    return inputs.account_status === "ACTIVE" && inputs.workflow_status !== "WITHDRAWN"
      && inputs.rejection_state === "NOT_REJECTED" && BigInt(inputs.portfolio_data_revision) < (1n << 63n) - 1n;
  } catch { return false; }
}
