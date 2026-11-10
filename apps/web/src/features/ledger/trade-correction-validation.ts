import { z } from "zod";
import type { components } from "@spm/contracts";
import { fx, money, price, quantity, signedMoney } from "./inputs";
import type { Trade } from "./TradeForm";

type Pin = components["schemas"]["TradeCorrectionReviewPin"];
export type CorrectionScope = { trade_version: number; reviews: Pin[] };
export type CorrectionOperation = "REVISE" | "VOID";

const identifier = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const version = z.union([z.literal(1), z.literal(2)]);
const ordinal = z.string().regex(/^(0|[1-9][0-9]{0,18})$/).refine(value =>
  /^(0|[1-9][0-9]{0,18})$/.test(value) && BigInt(value) <= 9223372036854775807n);
const gapSchema = z.object({
  trade_id: identifier, trade_version: version, review_revision: ordinal,
  current_gap: z.enum(["MISSING_CREATION_AUDIT", "MISSING_RETIRED_ORIGIN"]).nullable(),
  state: z.enum(["NOT_REVIEWED", "ACKNOWLEDGED_UNVERIFIED", "WITHDRAWN", "EVIDENCE_CHANGED"]),
}).strict();
export const correctionScopeSchema = z.object({
  trade_id: identifier, root_id: identifier, trade_version: version,
  trade_status: z.enum(["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID"]),
  gaps: z.array(gapSchema).max(100), scope: z.literal("UNVERIFIED_TRADE_CORRECTION_ONLY"),
  history_completeness: z.literal("UNVERIFIED"),
}).strict();
type ScopeStatus = z.infer<typeof correctionScopeSchema>;
const selectionSchema = z.object({
  trade_version: version,
  reviews: z.array(z.object({ trade_id: identifier, trade_version: version,
    review_revision: ordinal.refine(value => value !== "0"), gap: z.literal("MISSING_CREATION_AUDIT") }).strict()).min(1).max(100),
}).strict();
const responseSchema = z.object({
  authorization_id: identifier,
  scope: z.literal("UNVERIFIED_TRADE_CORRECTION_ONLY"), history_completeness: z.literal("UNVERIFIED"),
  trade: z.object({
    id: identifier, root_id: identifier, supersedes_id: identifier.nullable(),
    status: z.enum(["ACTIVE", "DRAFT_SCHEDULED", "PENDING_FX", "SUPERSEDED", "VOID"]),
    version, quantity, price, fee: money, tax: money, gross: signedMoney, settlement: signedMoney,
    booked_hkd: signedMoney.nullable(), rounding_delta: signedMoney, fx_rate_hkd: fx.nullable(),
  }).passthrough(),
}).strict();

export function validCorrectionScope(record: Trade, value: CorrectionScope) {
  const parsed = selectionSchema.safeParse(value);
  if (!parsed.success || !identifier.safeParse(record.id).success || !identifier.safeParse(record.root_id).success) return false;
  const scope = parsed.data;
  if (record.status === "SUPERSEDED" || record.status === "VOID" || scope.trade_version !== 1 || scope.trade_version !== record.version) return false;
  return new Set(scope.reviews.map(pin => pin.trade_id)).size === scope.reviews.length && scope.reviews.every(pin =>
    pin.trade_version === (pin.trade_id === record.id ? scope.trade_version : 2));
}

export function coherentCorrectionScope(scope: ScopeStatus, record: Trade) {
  if (scope.trade_id !== record.id || scope.root_id !== record.root_id || scope.trade_version !== record.version || scope.trade_status !== record.status) return false;
  if (scope.trade_status === "SUPERSEDED" || scope.trade_status === "VOID" || scope.trade_version !== 1) return false;
  if (new Set(scope.gaps.map(gap => gap.trade_id)).size !== scope.gaps.length) return false;
  return scope.gaps.every(gap => {
    if (gap.trade_version !== (gap.trade_id === scope.trade_id ? scope.trade_version : 2)) return false;
    if (gap.state === "EVIDENCE_CHANGED") return gap.review_revision !== "0";
    if (gap.current_gap !== "MISSING_CREATION_AUDIT") return false;
    return gap.state === "NOT_REVIEWED" ? gap.review_revision === "0" : gap.review_revision !== "0";
  });
}

export function checkedCorrection(
  result: components["schemas"]["TradeUnverifiedCorrectionRecord"], record: Trade, operation: CorrectionOperation,
): Trade {
  const parsed = responseSchema.safeParse(result);
  if (!parsed.success) throw new Error("更正回應無法安全核對；請保留原操作並檢查交易歷史。");
  const trade = parsed.data.trade;
  const matches = trade.root_id === record.root_id && (operation === "REVISE"
    ? trade.id !== record.id && trade.supersedes_id === record.id && trade.version === 1 && (trade.status === "ACTIVE" || trade.status === "DRAFT_SCHEDULED")
    : trade.id === record.id && trade.version === 2 && trade.status === "VOID");
  if (!matches) throw new Error("更正回應與原交易或操作不符；請保留原操作並檢查交易歷史。");
  return result.trade;
}
