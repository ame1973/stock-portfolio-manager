import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, evidenceUrl, optional, price } from "../ledger/inputs";

type Schemas = components["schemas"];
export type RevisionTarget = { event: Schemas["DividendEventRecord"]; rejection: Schemas["RejectionDecision"] | null };
export const eventTypes = ["UNKNOWN", "REGULAR", "SPECIAL", "INTERIM", "FINAL", "SUPPLEMENTAL", "ETF_DISTRIBUTION", "REIT_DISTRIBUTION"] as const;
const acceptableStatuses = new Set<Schemas["DividendEventRecord"]["workflow_status"]>(["PROVISIONAL", "CORROBORATED", "VERIFIED", "CONFLICT", "INCOMPLETE"]);

// Match Python's Unicode whitespace stripping for audited non-financial text.
// eslint-disable-next-line no-control-regex -- Python str.strip includes these information separators.
const pythonStrip = /^[\p{White_Space}\u001c-\u001f]+|[\p{White_Space}\u001c-\u001f]+$/gu;
export function normalizeDecisionText(value: string): string { return value.replace(pythonStrip, ""); }

const normalizedText = z.string().transform(normalizeDecisionText);
export const decisionCitation = normalizedText.pipe(z.string().min(1, "請提供引用參考。").max(300, "引用參考最多 300 個字元。")
  .refine(value => Array.from(value).every(character => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127), "引用參考不能包含控制字元。"));
export const decisionUrl = optional(evidenceUrl.refine(value => !value.includes("\x7f"), "引用網址不能包含控制字元。"));
export const decisionReason = normalizedText.pipe(z.string().min(10, "請提供至少 10 個字元的具體依據。").max(1000, "依據最多 1000 個字元。")
  .refine(value => !value.includes("\0"), "決定依據不能包含 NUL 字元。"));

export function matchesDecisionReference(reference: Schemas["EventReference"] | null, citation: string, source: string | null): boolean {
  try {
    const url = source ? new URL(source) : null;
    const withheld = !!(url?.search || url?.hash);
    return reference?.citation === citation && reference.url_withheld === withheld && reference.url === (withheld ? null : source);
  } catch { return false; }
}

function citedRejection(rejection: Schemas["RejectionDecision"]): boolean {
  const reference = rejection.reference;
  if (!z.string().min(10).max(1000).safeParse(rejection.reason).success || rejection.reason.includes("\0")
    || normalizeDecisionText(rejection.reason) !== rejection.reason || !reference
    || !z.string().min(1).max(300).safeParse(reference.citation).success
    || normalizeDecisionText(reference.citation) !== reference.citation
    || !Array.from(reference.citation).every(character => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127)
    || typeof reference.url_withheld !== "boolean") return false;
  if (reference.url === null) return true;
  if (reference.url_withheld || typeof reference.url !== "string" || reference.url.length > 4096) return false;
  const characters = Array.from(reference.url);
  if (characters.length > 2048 || characters.some(character => normalizeDecisionText(character) === "" || character.charCodeAt(0) < 32)) return false;
  try {
    const url = new URL(reference.url);
    return (url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}

export function validRejection(rejection: Schemas["RejectionDecision"], event: Schemas["DividendEventRecord"]): boolean {
  return !!rejection && typeof rejection === "object" && z.uuid().safeParse(rejection.id).success && rejection.event_id === event.id
    && rejection.revision_id === event.current_revision_id && rejection.revision_no === event.version
    && rejection.decision === "REJECT_REVISION" && rejection.scope === "OWNER_REVISION_REJECTION_ONLY"
    && rejection.document_verification === "NOT_ESTABLISHED" && rejection.eligibility_scope === "NOT_ESTABLISHED" && citedRejection(rejection);
}

export function validTarget(target: RevisionTarget): boolean {
  if (!target || typeof target !== "object" || !target.event || typeof target.event !== "object") return false;
  const event = target.event;
  const rejection = target.rejection;
  return z.uuid().safeParse(event.id).success && z.uuid().safeParse(event.current_revision_id).success
    && z.uuid().safeParse(event.security_id).success && Number.isInteger(event.version) && event.version >= 1 && event.version < 2_147_483_647
    && event.scope === "OWNER_EVENT_DECISION_ONLY" && event.current_use_scope === "REVALIDATION_REQUIRED"
    && event.document_verification === "NOT_ESTABLISHED" && event.eligibility_scope === "NOT_ESTABLISHED"
    && (rejection === null || validRejection(rejection, event));
}

export function validAcceptanceTarget(target: RevisionTarget): boolean {
  if (!validTarget(target)) return false;
  const event = target.event;
  return acceptableStatuses.has(event.workflow_status) && event.adjustment_basis === "AS_DECLARED"
    && z.enum(eventTypes).safeParse(event.event_type).success && z.enum(["HKD", "USD"]).safeParse(event.currency).success
    && price.safeParse(event.dps).success && date.safeParse(event.ex_date).success
    && [event.announce_date, event.record_date, event.pay_date].every(value => value === null || date.safeParse(value).success)
    && typeof event.manual_locked === "boolean" && typeof event.manual_override === "boolean";
}
