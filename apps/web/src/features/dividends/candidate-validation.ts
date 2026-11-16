import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, price } from "../ledger/inputs";
import { eventTypes } from "./event-validation";

type Candidate = components["schemas"]["CandidateRecord"];
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

export function validCandidate(record: unknown): record is Candidate {
  return candidateSchema.safeParse(record).success;
}
