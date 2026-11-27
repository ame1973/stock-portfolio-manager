import { describe, expect, it } from "vitest";
import { checkedEvidenceReviewQueue } from "./evidence-review-validation";
import { activeReview, resolvedReview, reviewQueue } from "./evidence-review-queue-test-data";

describe("cross-event evidence review queue validation", () => {
  it("accepts retained rows without treating a queue as complete or verified", () => {
    expect(checkedEvidenceReviewQueue(reviewQueue, 25)).toBe(reviewQueue);
    expect(checkedEvidenceReviewQueue({ ...reviewQueue, items: [] }, 25, { severity: "INFO" }).items).toEqual([]);
    expect(checkedEvidenceReviewQueue({ ...reviewQueue, items: [activeReview] }, 25, { status: "OPEN" }).items).toHaveLength(1);
  });

  it.each([
    { scope: "COMPLETE" }, { history_completeness: "COMPLETE" }, { current_use_scope: "AUTHORIZED" },
    { eligibility_scope: "ESTABLISHED" }, { document_verification: "VERIFIED" }, { extra: "synthetic-private" },
    { next_cursor: "" }, { items: [], next_cursor: "opaque" },
  ])("rejects an unsafe queue envelope", change => {
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, ...change }, 25)).toThrow(/無法安全核對/);
  });

  it("enforces filtering, global order, unique review IDs and bounded continuation", () => {
    expect(() => checkedEvidenceReviewQueue(reviewQueue, 25, { status: "OPEN" })).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue(reviewQueue, 25, { severity: "INFO" })).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, items: [resolvedReview, activeReview] }, 25)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, items: [activeReview, activeReview] }, 25)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, next_cursor: "opaque" }, 25)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue(reviewQueue, 0)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue(reviewQueue, 25, { status: "DISMISSED" } as never)).toThrow(/無法安全核對/);
  });

  it("rejects mixed heads, broken successor links and revision IDs reused across events", () => {
    const otherHead = "a4444444-4444-4444-8444-444444444444";
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, items: [activeReview,
      { ...resolvedReview, current_revision_id: otherHead }] }, 25)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, items: [activeReview,
      { ...resolvedReview, resolved_by_revision_id: otherHead }] }, 25)).toThrow(/無法安全核對/);
    const foreign = { ...resolvedReview, event_id: "a5555555-5555-4555-8555-555555555555" };
    expect(() => checkedEvidenceReviewQueue({ ...reviewQueue, items: [activeReview, foreign] }, 25)).toThrow(/無法安全核對/);
  });

  it("accepts two different events with independent current heads", () => {
    const other = { ...activeReview, id: "a1111111-1111-4111-8111-cccccccccccc",
      event_id: "a5555555-5555-4555-8555-555555555555", revision_id: "a4444444-4444-4444-8444-444444444444",
      current_revision_id: "a4444444-4444-4444-8444-444444444444", created_at: "2026-11-03T00:00:00.000003Z" };
    expect(checkedEvidenceReviewQueue({ ...reviewQueue, items: [other, activeReview, resolvedReview] }, 25).items).toHaveLength(3);
  });
});
