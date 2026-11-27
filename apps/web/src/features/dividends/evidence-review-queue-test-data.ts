import type { components } from "@spm/contracts";
import { event } from "./projection-test-data";

type Schemas = components["schemas"];
type Review = Schemas["EvidenceReviewQueueRecord"];
export const activeReview: Review = {
  id: "a1111111-1111-4111-8111-aaaaaaaaaaaa", event_id: event.id,
  revision_id: event.current_revision_id, revision_no: event.version, portfolio_id: null,
  status: "OPEN", severity: "WARNING", reason_code: "DIVIDEND_INCOMPLETE",
  conflicting_fields: [], incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"],
  created_at: "2026-11-03T00:00:00.000002Z", resolved_at: null, resolved_by_revision_id: null,
  acknowledgement_scope: "NOT_ESTABLISHED", scope: "RETAINED_EVIDENCE_REVIEW_ONLY",
  current_revision_id: event.current_revision_id, current_revision_no: event.version,
};
export const resolvedReview: Review = {
  ...activeReview, id: "a1111111-1111-4111-8111-bbbbbbbbbbbb", revision_id: "a3333333-3333-4333-8333-333333333333",
  revision_no: 2, status: "RESOLVED", created_at: "2026-11-03T00:00:00.000001Z",
  resolved_at: activeReview.created_at, resolved_by_revision_id: event.current_revision_id,
};
export const reviewQueue: Schemas["EvidenceReviewQueuePage"] = {
  items: [activeReview, resolvedReview], next_cursor: null, scope: "RETAINED_EVIDENCE_REVIEW_QUEUE_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED",
  eligibility_scope: "NOT_ESTABLISHED", document_verification: "NOT_ESTABLISHED",
};
