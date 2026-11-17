import { z } from "zod";
import type { components } from "@spm/contracts";
import { date, price } from "../ledger/inputs";

const sourceSchema = z.object({ id: z.uuid(), security_id: z.uuid(), normalized_sha256: z.string().regex(/^[a-f0-9]{64}$/), ex_date: date,
  dps: price.nullable(), currency: z.enum(["HKD", "USD"]).nullable(), adjustment_basis: z.literal("SPLIT_ADJUSTED"), provenance_version: z.literal(1),
  scope: z.literal("SOURCE_ONLY"), canonical_adoption: z.literal("NOT_ADOPTED"), canonical_adoption_scope: z.literal("RESPONSE_ONLY_NOT_LINKAGE") });
export function validBasisSource(value: unknown): value is components["schemas"]["DividendObservationRecord"] { return sourceSchema.safeParse(value).success; }

const sourcePageSchema = z.object({ scope: z.literal("IMMUTABLE_SOURCE_V1"), legacy_records_omitted: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  next_cursor: z.string().nullable().optional(), items: z.array(z.object({ id: z.uuid(), security_id: z.uuid() })).max(25) });
export function validBasisSourcePage(value: unknown, security: string): boolean {
  const parsed = sourcePageSchema.safeParse(value); if (!parsed.success) return false;
  const ids = new Set<string>();
  return parsed.data.items.every(item => {
    const id = item.id.toLowerCase();
    if (ids.has(id) || (security && item.security_id.toLowerCase() !== security.toLowerCase())) return false;
    ids.add(id); return true;
  });
}
