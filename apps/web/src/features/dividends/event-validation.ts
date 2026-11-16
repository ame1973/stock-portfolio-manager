import { z } from "zod";
import type { components } from "@spm/contracts";

type Schemas = components["schemas"];
export type RevisionTarget = { event: Schemas["DividendEventRecord"]; rejection: Schemas["RejectionDecision"] | null };

// Match Python's Unicode whitespace stripping for audited non-financial text.
// eslint-disable-next-line no-control-regex -- Python str.strip includes these information separators.
const pythonStrip = /^[\p{White_Space}\u001c-\u001f]+|[\p{White_Space}\u001c-\u001f]+$/gu;
export function normalizeDecisionText(value: string): string { return value.replace(pythonStrip, ""); }

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
