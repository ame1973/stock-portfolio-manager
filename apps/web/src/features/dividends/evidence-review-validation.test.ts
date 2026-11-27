import { describe, expect, it } from "vitest";
import type { components } from "@spm/contracts";
import { event } from "./projection-test-data";
import { checkedEvidenceReviewPage } from "./evidence-review-validation";

type Schemas = components["schemas"];
type Review = Schemas["EvidenceReviewRecord"];
type Page = Schemas["EvidenceReviewPage"];
const olderRevision = "a3333333-3333-4333-8333-333333333333";
const nextHead = "a4444444-4444-4444-8444-444444444444";
const portfolio = "e5555555-5555-4555-8555-555555555555";
const active: Review = {
  id: "a1111111-1111-4111-8111-aaaaaaaaaaaa", event_id: event.id, revision_id: event.current_revision_id,
  revision_no: event.version, portfolio_id: portfolio, status: "OPEN", severity: "WARNING", reason_code: "DIVIDEND_INCOMPLETE",
  conflicting_fields: [], incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"], created_at: "2026-11-03T00:00:00.000002Z",
  resolved_at: null, resolved_by_revision_id: null, acknowledgement_scope: "NOT_ESTABLISHED", scope: "RETAINED_EVIDENCE_REVIEW_ONLY",
};
const resolved: Review = {
  ...active, id: "a1111111-1111-4111-8111-bbbbbbbbbbbb", revision_id: olderRevision, revision_no: 2,
  status: "RESOLVED", created_at: "2026-11-03T00:00:00.000001Z", resolved_at: "2026-11-03T00:00:00.000002Z",
  resolved_by_revision_id: event.current_revision_id,
};
const page: Page = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, items: [active, resolved], next_cursor: null,
  scope: "RETAINED_EVIDENCE_REVIEWS_ONLY", history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED",
  eligibility_scope: "NOT_ESTABLISHED", document_verification: "NOT_ESTABLISHED",
};

describe("retained evidence review page checks", () => {
  it("preserves original and immediate successor revisions without granting verification", () => {
    expect(checkedEvidenceReviewPage(page, event)).toBe(page);
    expect(checkedEvidenceReviewPage({ ...page, items: [], next_cursor: null }, event).items).toEqual([]);
  });

  it("accepts stored acknowledgments and event-level reviews without authenticating them", () => {
    const item = { ...active, status: "ACKNOWLEDGED" as const, portfolio_id: null };
    expect(checkedEvidenceReviewPage({ ...page, items: [item] }, event).items[0]).toBe(item);
  });

  it("checks returned rows against the selected filters without treating empty results as proof", () => {
    const openPage = { ...page, items: [active] };
    expect(checkedEvidenceReviewPage(openPage, event, 25, { status: "OPEN", severity: "WARNING" })).toBe(openPage);
    expect(checkedEvidenceReviewPage({ ...page, items: [] }, event, 25, { severity: "INFO" }).items).toEqual([]);
    expect(() => checkedEvidenceReviewPage(page, event, 25, { status: "OPEN" })).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage(openPage, event, 25, { severity: "CRITICAL" })).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage(openPage, event, 25, { status: "DISMISSED" } as never)).toThrow(/無法安全核對/);
  });

  it("accepts a changed head only for an explicit independent-read warning", () => {
    const value = { ...page, revision_id: nextHead, revision_no: 4,
      items: [{ ...active, revision_id: nextHead, revision_no: 4 }, resolved] };
    expect(checkedEvidenceReviewPage(value, event)).toBe(value);
    expect(() => checkedEvidenceReviewPage({ ...value, revision_no: 3 }, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage({ ...value, revision_id: event.current_revision_id }, event)).toThrow(/無法安全核對/);
  });

  it("compares timestamps to microsecond precision and UUIDs without case-sensitive duplicates", () => {
    expect(checkedEvidenceReviewPage({ ...page, event_id: event.id.toUpperCase() }, event)).toBeTruthy();
    expect(() => checkedEvidenceReviewPage({ ...page, items: [resolved, active] }, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage({ ...page, items: [active, { ...active, id: active.id.toUpperCase() }] }, event)).toThrow(/無法安全核對/);
  });

  it.each([
    { event_id: olderRevision }, { revision_id: olderRevision }, { revision_no: 2 }, { revision_no: 0 },
    { scope: "VERIFIED" }, { history_completeness: "COMPLETE" }, { current_use_scope: "AUTHORIZED" },
    { eligibility_scope: "ESTABLISHED" }, { document_verification: "VERIFIED" }, { next_cursor: "" },
    { next_cursor: "x".repeat(1501) }, { extra_private_data: "synthetic-private" },
  ])("rejects an unsafe page envelope", change => {
    expect(() => checkedEvidenceReviewPage({ ...page, ...change }, event)).toThrow(/無法安全核對/);
  });

  it.each([
    { event_id: olderRevision }, { revision_id: olderRevision }, { revision_no: 2 }, { portfolio_id: "private-invalid" },
    { status: "RESOLVED" }, { severity: "INFO" }, { reason_code: "DIVIDEND_SOURCE_CONFLICT" },
    { conflicting_fields: ["dps"] }, { incomplete_reasons: [] }, { incomplete_reasons: ["PRIVATE SECRET"] },
    { incomplete_reasons: ["Z_REASON", "A_REASON"] }, { incomplete_reasons: ["A_REASON", "A_REASON"] },
    { created_at: "not-a-date" }, { created_at: "2026-11-03T00:00:00+08:00" },
    { resolved_at: "2026-11-04T00:00:00Z" }, { resolved_by_revision_id: olderRevision },
    { acknowledgement_scope: "AUTHENTICATED" }, { scope: "ELIGIBLE" }, { extra_private_data: "synthetic-private" },
  ])("rejects inconsistent active review detail", change => {
    expect(() => checkedEvidenceReviewPage({ ...page, items: [{ ...active, ...change }] }, event)).toThrow(/無法安全核對/);
  });

  it.each([
    { revision_no: 3 }, { revision_id: event.current_revision_id }, { resolved_at: null },
    { resolved_at: "2026-11-02T23:59:59Z" }, { resolved_by_revision_id: null },
    { resolved_by_revision_id: olderRevision }, { resolved_by_revision_id: nextHead },
  ])("rejects an inconsistent historical resolution", change => {
    expect(() => checkedEvidenceReviewPage({ ...page, items: [{ ...resolved, ...change }] }, event)).toThrow(/無法安全核對/);
  });

  it("validates literal conflict fields and bounded cursor pages", () => {
    const conflict: Review = { ...active, reason_code: "DIVIDEND_SOURCE_CONFLICT", conflicting_fields: ["currency", "dps"], incomplete_reasons: ["SOURCE_MISSING"] };
    const value = { ...page, items: [conflict], next_cursor: "opaque" };
    expect(checkedEvidenceReviewPage(value, event, 1)).toBe(value);
    expect(() => checkedEvidenceReviewPage(value, event, 2)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage(value, event, 0)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage(value, event, 101)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage({ ...value, items: [{ ...conflict, conflicting_fields: ["dps", "currency"] }] }, event, 1)).toThrow(/無法安全核對/);
  });

  it("rejects conflicting physical revision and portfolio scopes", () => {
    const other = "a5555555-5555-4555-8555-555555555555";
    const earlier = { ...resolved, id: "a1111111-1111-4111-8111-cccccccccccc", revision_no: 1,
      revision_id: other, resolved_by_revision_id: olderRevision, created_at: "2026-11-02T00:00:00Z", resolved_at: resolved.created_at };
    expect(checkedEvidenceReviewPage({ ...page, items: [active, resolved, earlier] }, event)).toBeTruthy();
    expect(() => checkedEvidenceReviewPage({ ...page, items: [active, resolved, { ...earlier, resolved_by_revision_id: other }] }, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage({ ...page, items: [active, { ...resolved, resolved_at: "2026-11-03T00:00:00.000003Z" }] }, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage({ ...page, items: [active, { ...active, id: resolved.id, portfolio_id: null }] }, event)).toThrow(/無法安全核對/);
  });

  it("rejects duplicate scope, empty continued pages and private malformed data", () => {
    expect(() => checkedEvidenceReviewPage({ ...page, items: [active, { ...active, id: resolved.id }] }, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage({ ...page, items: [], next_cursor: "opaque" }, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage(null, event)).toThrow(/無法安全核對/);
    expect(() => checkedEvidenceReviewPage(page, { ...event, id: "synthetic-private" })).toThrow(/無法安全核對/);
    try { checkedEvidenceReviewPage({ ...page, private: "synthetic-private" }, event); }
    catch (error) { expect(String(error)).not.toContain("synthetic-private"); }
  });
});
