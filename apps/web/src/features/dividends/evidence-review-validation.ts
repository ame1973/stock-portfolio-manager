import { z } from "zod";
import type { components } from "@spm/contracts";
import { validEstimateEvent } from "./estimate-validation";
import { sameId, timestamp, timestampMicros } from "./receipt-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Review = Schemas["EvidenceReviewRecord"];
type Page = Schemas["EvidenceReviewPage"];
type EventHead = Pick<Event, "id" | "current_revision_id" | "version">;
type QueuePage = Schemas["EvidenceReviewQueuePage"];
export type EvidenceReviewFilters = { status?: Review["status"]; severity?: "INFO" | "WARNING" | "CRITICAL" };
const filterShape = z.strictObject({
  status: z.enum(["OPEN", "ACKNOWLEDGED", "RESOLVED"]).optional(),
  severity: z.enum(["INFO", "WARNING", "CRITICAL"]).optional(),
});
const id = z.uuid();
const revision = z.int().min(1).max(2_147_483_647);
const utc = timestamp.refine(value => value.endsWith("Z") || value.endsWith("+00:00"));
const fields = z.enum(["ex_date", "announce_date", "record_date", "pay_date", "event_type", "currency", "dps", "adjustment_basis"]);
const reason = z.string().min(1).max(100).regex(/^[A-Z][A-Z0-9_]*$/);
const itemShape = z.strictObject({
  id, event_id: id, revision_id: id, revision_no: revision, portfolio_id: id.nullable(),
  status: z.enum(["OPEN", "ACKNOWLEDGED", "RESOLVED"]), severity: z.literal("WARNING"),
  reason_code: z.enum(["DIVIDEND_SOURCE_CONFLICT", "DIVIDEND_INCOMPLETE"]),
  conflicting_fields: z.array(fields).max(8), incomplete_reasons: z.array(reason).max(32),
  created_at: utc, resolved_at: utc.nullable(), resolved_by_revision_id: id.nullable(),
  acknowledgement_scope: z.literal("NOT_ESTABLISHED"), scope: z.literal("RETAINED_EVIDENCE_REVIEW_ONLY"),
});
const pageShape = z.strictObject({
  event_id: id, revision_id: id, revision_no: revision, items: z.array(itemShape).max(100),
  next_cursor: z.string().min(1).max(1500).nullable(), scope: z.literal("RETAINED_EVIDENCE_REVIEWS_ONLY"),
  history_completeness: z.literal("UNVERIFIED"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
  eligibility_scope: z.literal("NOT_ESTABLISHED"), document_verification: z.literal("NOT_ESTABLISHED"),
});
const queueShape = z.strictObject({
  items: z.array(itemShape.extend({ current_revision_id: id, current_revision_no: revision })).max(100),
  next_cursor: z.string().min(1).max(1500).nullable(), scope: z.literal("RETAINED_EVIDENCE_REVIEW_QUEUE_ONLY"),
  history_completeness: z.literal("UNVERIFIED"), current_use_scope: z.literal("REVALIDATION_REQUIRED"),
  eligibility_scope: z.literal("NOT_ESTABLISHED"), document_verification: z.literal("NOT_ESTABLISHED"),
});

function sortedUnique(values: string[]): boolean {
  return values.every((value, index) => index === 0 || values[index - 1]! < value);
}

function consistent(page: Page, event: EventHead, limit: number, filters: EvidenceReviewFilters): boolean {
  if (!sameId(page.event_id, event.id) || (page.revision_no === event.version) !== sameId(page.revision_id, event.current_revision_id)
    || page.items.length > limit || (page.next_cursor !== null && page.items.length !== limit)) return false;
  const ids = new Set<string>(); const scopes = new Map<number, Set<string | null>>();
  const revisions = new Map<number, string>(); const numbers = new Map<string, number>(); const times = new Map<number, bigint>();
  const bindTime = (number: number, value: string) => {
    const observed = timestampMicros(value);
    if (times.has(number) && times.get(number) !== observed) return false;
    times.set(number, observed); return true;
  };
  const bindRevision = (number: number, value: string) => {
    const identity = value.toLowerCase();
    if ((revisions.has(number) && revisions.get(number) !== identity) || (numbers.has(identity) && numbers.get(identity) !== number)) return false;
    revisions.set(number, identity); numbers.set(identity, number); return true;
  };
  if (!bindRevision(page.revision_no, page.revision_id)) return false;
  let previous: Review | null = null;
  for (const item of page.items) {
    const identity = item.id.toLowerCase(); const portfolio = item.portfolio_id?.toLowerCase() ?? null;
    const prior = scopes.get(item.revision_no) ?? new Set<string | null>();
    if (!sameId(item.event_id, page.event_id) || ids.has(identity) || prior.has(portfolio)
      || (filters.status !== undefined && item.status !== filters.status)
      || (filters.severity !== undefined && item.severity !== filters.severity)
      || (prior.size > 0 && (portfolio === null || prior.has(null))) || !bindRevision(item.revision_no, item.revision_id)
      || !bindTime(item.revision_no, item.created_at) || !sortedUnique(item.conflicting_fields) || !sortedUnique(item.incomplete_reasons)
      || (item.reason_code === "DIVIDEND_SOURCE_CONFLICT") !== (item.conflicting_fields.length > 0)
      || (item.reason_code === "DIVIDEND_INCOMPLETE" && item.incomplete_reasons.length === 0)) return false;
    if (previous !== null) {
      const time = timestampMicros(item.created_at); const preceding = timestampMicros(previous.created_at);
      if (time > preceding || (time === preceding && identity >= previous.id.toLowerCase())) return false;
    }
    if (item.status === "RESOLVED") {
      if (item.resolved_at === null || item.resolved_by_revision_id === null || item.revision_no >= page.revision_no
        || sameId(item.revision_id, page.revision_id) || sameId(item.resolved_by_revision_id, item.revision_id)
        || timestampMicros(item.resolved_at) < timestampMicros(item.created_at)
        || ((item.revision_no + 1 === page.revision_no) !== sameId(item.resolved_by_revision_id, page.revision_id))
        || !bindRevision(item.revision_no + 1, item.resolved_by_revision_id) || !bindTime(item.revision_no + 1, item.resolved_at)) return false;
    } else if (item.resolved_at !== null || item.resolved_by_revision_id !== null
      || item.revision_no !== page.revision_no || !sameId(item.revision_id, page.revision_id)) return false;
    ids.add(identity); prior.add(portfolio); scopes.set(item.revision_no, prior); previous = item;
  }
  return true;
}

export function checkedEvidenceReviewPage(value: unknown, event: Event, limit = 25, filters: EvidenceReviewFilters = {}): Page {
  try {
    if (!validEstimateEvent(event) || !Number.isInteger(limit) || limit < 1 || limit > 100 || !filterShape.safeParse(filters).success
      || !pageShape.safeParse(value).success || !consistent(value as Page, event, limit, filters)) throw new Error();
    return value as Page;
  } catch {
    throw new Error("股息證據審核範圍無法安全核對；請重新載入事件及審核歷史，或檢查有效備份。");
  }
}

export function checkedEvidenceReviewQueue(value: unknown, limit = 25, filters: EvidenceReviewFilters = {}): QueuePage {
  try {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !filterShape.safeParse(filters).success
      || !queueShape.safeParse(value).success) throw new Error();
    const queue = value as QueuePage;
    if (queue.items.length > limit || (queue.next_cursor !== null && queue.items.length !== limit)) throw new Error();
    const ids = new Set<string>(); const revisions = new Map<string, string>();
    const groups = new Map<string, { head: EventHead; items: Review[] }>();
    let previous: QueuePage["items"][number] | null = null;
    for (const item of queue.items) {
      const identity = item.id.toLowerCase(); const eventId = item.event_id.toLowerCase();
      const bindRevision = (value: string) => {
        const key = value.toLowerCase();
        if (revisions.has(key) && revisions.get(key) !== eventId) return false;
        revisions.set(key, eventId); return true;
      };
      if (ids.has(identity) || !bindRevision(item.revision_id) || !bindRevision(item.current_revision_id)
        || (item.resolved_by_revision_id !== null && !bindRevision(item.resolved_by_revision_id))) throw new Error();
      if (previous !== null) {
        const time = timestampMicros(item.created_at); const preceding = timestampMicros(previous.created_at);
        if (time > preceding || (time === preceding && identity >= previous.id.toLowerCase())) throw new Error();
      }
      const { current_revision_id, current_revision_no, ...review } = item;
      const group: { head: EventHead; items: Review[] } = groups.get(eventId)
        ?? { head: { id: item.event_id, current_revision_id, version: current_revision_no }, items: [] };
      if (!sameId(group.head.current_revision_id, current_revision_id) || group.head.version !== current_revision_no) throw new Error();
      group.items.push(review); groups.set(eventId, group); ids.add(identity); previous = item;
    }
    for (const { head, items } of groups.values()) {
      const page: Page = { event_id: head.id, revision_id: head.current_revision_id, revision_no: head.version, items,
        next_cursor: null, scope: "RETAINED_EVIDENCE_REVIEWS_ONLY", history_completeness: "UNVERIFIED",
        current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", document_verification: "NOT_ESTABLISHED" };
      if (!consistent(page, head, limit, filters)) throw new Error();
    }
    return queue;
  } catch {
    throw new Error("跨事件證據審核無法安全核對；請重新載入或檢查有效備份。");
  }
}
