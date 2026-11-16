import { z } from "zod";
import { replaceEqualDeep } from "@tanstack/react-query";
import type { components } from "@spm/contracts";
import { date, price, samePrice } from "../ledger/inputs";
import { decisionReason, eventTypes } from "./event-validation";

type Candidate = components["schemas"]["CandidateRecord"];
type Event = components["schemas"]["DividendEventRecord"];
const fieldNames = ["ex_date", "announce_date", "record_date", "pay_date", "event_type", "currency", "dps", "adjustment_basis"] as const;
const fields = z.array(z.enum(fieldNames));
const uuid = z.uuid();
const basis = z.enum(["AS_DECLARED", "SPLIT_ADJUSTED", "UNKNOWN"]);
const optionalDate = date.nullish();
const candidateSchema = z.object({
  id: uuid, revision_id: uuid, version: z.int().min(1).max(2_147_483_647), state: z.enum(["OPEN", "DISMISSED"]),
  terms: z.object({ security_id: uuid, ex_date: optionalDate, announce_date: optionalDate, record_date: optionalDate, pay_date: optionalDate,
    event_type: z.enum(eventTypes), dps: price.nullish(), currency: z.enum(["HKD", "USD"]).nullish(), adjustment_basis: basis }),
  matches: z.array(z.object({ observation_id: uuid, source_sha256: z.string().regex(/^[a-f0-9]{64}$/), basis_assertion_id: uuid.nullish(), reason: z.string() })).max(100),
  change_reason: z.string(), created_at: z.string(),
  assessment: z.object({ status: z.enum(["PROVISIONAL", "CORROBORATED", "CONFLICT", "INCOMPLETE"]), independent_origins: z.array(z.string()),
    eligible_observation_ids: z.array(uuid), conflicting_fields: fields, incomplete_reasons: z.array(z.string()),
    checks: z.array(z.object({ observation_id: uuid, supporting_fields: fields, conflicting_fields: fields, incomplete_reasons: z.array(z.string()),
      comparison_dps: price.nullable(), comparison_basis: basis, basis_assertion_id: uuid.nullable() })) }),
  scope: z.literal("CANDIDATE_REVIEW_ONLY"), canonical_adoption: z.literal("NOT_ADOPTED"),
  canonical_adoption_scope: z.literal("RESPONSE_ONLY_NOT_LINKAGE"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
});

export const candidateReason = decisionReason.refine(value => Array.from(value).every(character => {
  const point = character.codePointAt(0)!;
  return point < 0xd800 || point > 0xdfff;
}), "依據含有無效的 Unicode 字元，請重新輸入。");

export function validCandidate(record: unknown): record is Candidate {
  return candidateSchema.safeParse(record).success;
}

export function validCandidateForEvent(record: unknown): record is Candidate {
  if (!validCandidate(record) || record.state !== "OPEN" || record.terms.ex_date == null
    || record.terms.dps == null || record.terms.currency == null) return false;
  const matches = new Map(record.matches.map(match => [match.observation_id.toLowerCase(), match.basis_assertion_id?.toLowerCase() ?? null]));
  const checks = new Set(record.assessment.checks.map(check => check.observation_id.toLowerCase()));
  const basisIds = record.matches.flatMap(match => match.basis_assertion_id ? [match.basis_assertion_id.toLowerCase()] : []);
  const eligible = new Set(record.assessment.eligible_observation_ids.map(id => id.toLowerCase()));
  return matches.size === record.matches.length && checks.size === record.assessment.checks.length && new Set(basisIds).size === basisIds.length
    && matches.size === checks.size && record.assessment.checks.every(check => matches.has(check.observation_id.toLowerCase())
      && matches.get(check.observation_id.toLowerCase()) === (check.basis_assertion_id?.toLowerCase() ?? null))
    && eligible.size === record.assessment.eligible_observation_ids.length && [...eligible].every(id => checks.has(id));
}

export function checkedCandidateEvent(record: Event, target: Candidate, reason: string, previous?: Event): Event {
  if (!validCandidateForEvent(target)) throw new Error("固定候選無法核對；請關閉並重新載入。");
  const terms = target.terms;
  const evidence = target.assessment.checks.map(check => ({ observation_id: check.observation_id, fields: check.supporting_fields }));
  const identity = previous ? record?.id === previous.id && record.version === previous.version + 1
    && record.current_revision_id !== previous.current_revision_id : record?.version === 1;
  const valid = record && typeof record === "object" && z.uuid().safeParse(record.id).success
    && z.uuid().safeParse(record.current_revision_id).success && identity && record.security_id === terms.security_id
    && record.event_type === terms.event_type && record.ex_date === terms.ex_date && record.announce_date === (terms.announce_date ?? null)
    && record.record_date === (terms.record_date ?? null) && record.pay_date === (terms.pay_date ?? null)
    && terms.dps != null && samePrice(record.dps, terms.dps) && record.currency === terms.currency && record.adjustment_basis === terms.adjustment_basis
    && record.workflow_status === target.assessment.status && record.decision_kind === "CANDIDATE" && record.candidate_revision_id === target.revision_id
    && record.manual_locked === false && record.manual_override === false && record.reference === null && record.reason === reason
    && record.independent_origin_groups === target.assessment.independent_origins.length
    && replaceEqualDeep(target.assessment, record.assessment) === target.assessment && replaceEqualDeep(evidence, record.evidence) === evidence
    && record.scope === "OWNER_EVENT_DECISION_ONLY" && record.current_use_scope === "REVALIDATION_REQUIRED"
    && record.document_verification === "NOT_ESTABLISHED" && record.eligibility_scope === "NOT_ESTABLISHED";
  if (!valid) throw new Error("伺服器回覆與固定候選不符；請保留輸入並核對事件歷史，不會假定未儲存。");
  return record;
}
