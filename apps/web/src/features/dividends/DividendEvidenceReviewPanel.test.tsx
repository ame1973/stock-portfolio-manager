import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendEvidenceReviewPanel } from "./DividendEvidenceReviewPanel";
import { event } from "./projection-test-data";

type Schemas = components["schemas"];
type Review = Schemas["EvidenceReviewRecord"];
type Page = Schemas["EvidenceReviewPage"];
const oldRevision = "a3333333-3333-4333-8333-333333333333";
const path = "/api/v1/dividends/events/{event_id}/evidence-reviews";
const review: Review = {
  id: "a1111111-1111-4111-8111-aaaaaaaaaaaa", event_id: event.id, revision_id: event.current_revision_id,
  revision_no: event.version, portfolio_id: null, status: "OPEN", severity: "WARNING", reason_code: "DIVIDEND_INCOMPLETE",
  conflicting_fields: [], incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"], created_at: "2026-11-03T00:00:00Z", resolved_at: null,
  resolved_by_revision_id: null, acknowledgement_scope: "NOT_ESTABLISHED", scope: "RETAINED_EVIDENCE_REVIEW_ONLY",
};
const resolved: Review = { ...review, id: "a1111111-1111-4111-8111-bbbbbbbbbbbb", revision_id: oldRevision,
  revision_no: 2, status: "RESOLVED", created_at: "2026-11-02T00:00:00Z", resolved_at: "2026-11-03T00:00:00Z",
  resolved_by_revision_id: event.current_revision_id };
const page: Page = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, items: [review, resolved], next_cursor: null,
  scope: "RETAINED_EVIDENCE_REVIEWS_ONLY", history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED",
  eligibility_scope: "NOT_ESTABLISHED", document_verification: "NOT_ESTABLISHED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function mount(value: unknown = page) {
  const get = vi.spyOn(api, "GET").mockImplementation(async request => {
    if (request === path) return reply(value);
    throw new Error("Unexpected private read");
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><DividendEvidenceReviewPanel event={event} /></QueryClientProvider>);
  return { get, cache, ...view };
}

describe("retained evidence reviews", () => {
  it("displays recorded reviews and their revisions without granting eligibility", async () => {
    const post = vi.spyOn(api, "POST"); const { get, cache } = mount();
    const panel = await screen.findByRole("region", { name: "股息證據審核歷史" });
    expect(await within(panel).findByText(/已記錄待核對/)).toBeVisible();
    expect(panel).toHaveTextContent("歷史完整性未驗證"); expect(panel).toHaveTextContent("不是文件驗證或持股資格");
    expect(panel).toHaveTextContent("UNRESOLVED_ADJUSTMENT"); expect(panel).toHaveTextContent("原事件修訂 2");
    expect(panel).toHaveTextContent(event.current_revision_id); expect(panel).toHaveTextContent("已記錄後繼版本處理");
    expect(get).toHaveBeenCalledWith(path, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("distinguishes stored acknowledgment and an empty retained page from verified resolution", async () => {
    const acknowledged = { ...review, status: "ACKNOWLEDGED" as const };
    mount({ ...page, items: [acknowledged] });
    expect(await screen.findByText(/已記錄為知悉/)).toHaveTextContent("知悉未經驗證");
  });

  it("never treats an empty page as complete", async () => {
    mount({ ...page, items: [] });
    expect(await screen.findByText(/此頁沒有已記錄的證據審核/)).toHaveTextContent("不代表歷史完整或問題已處理");
  });

  it("withholds records when the independently read event head changes", async () => {
    const newer = "a4444444-4444-4444-8444-444444444444";
    mount({ ...page, revision_id: newer, revision_no: 4,
      items: [{ ...review, revision_id: newer, revision_no: 4 }, resolved] });
    expect(await screen.findByRole("alert")).toHaveTextContent("不是同一快照");
    expect(screen.queryByText(/UNRESOLVED_ADJUSTMENT/)).not.toBeInTheDocument();
  });

  it("rejects malformed records without exposing private detail", async () => {
    mount({ ...page, items: [{ ...review, incomplete_reasons: ["PRIVATE SECRET"] }] });
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByText(/PRIVATE SECRET/)).not.toBeInTheDocument();
  });

  it("hides stale results on failure and retries with a fixed private error", async () => {
    const { get } = mount(); expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    get.mockImplementation(async () => ({ error: { code: "DIVIDEND_EVIDENCE_REVIEW_UNAVAILABLE", message: "private detail" }, response: new Response() }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入證據審核" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("股息證據審核歷史無法安全核對");
    expect(screen.queryByText(/UNRESOLVED_ADJUSTMENT|private detail/)).not.toBeInTheDocument();
    get.mockImplementation(async () => reply(page)); fireEvent.click(screen.getByRole("button", { name: "重試載入證據審核" }));
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
  });

  it("hides results offline and revalidates after reconnect", async () => {
    const { get } = mount(); expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    try {
      act(() => onlineManager.setOnline(false));
      expect(await screen.findByText(/連線已暫停/)).toBeVisible();
      expect(screen.queryByText(/UNRESOLVED_ADJUSTMENT/)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "重新載入證據審核" })).toBeDisabled();
      const calls = get.mock.calls.length; act(() => onlineManager.setOnline(true));
      await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls));
      expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    } finally { act(() => onlineManager.setOnline(true)); }
  });

  it("aborts a pending read on close", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => new Promise(() => { signal = (options as { signal: AbortSignal }).signal; }));
    const view = render(<DividendEvidenceReviewPanel event={event} />);
    await waitFor(() => expect(signal).toBeDefined()); view.unmount(); expect(signal?.aborted).toBe(true);
  });

  it("does not fetch when the event scope is malformed", () => {
    const get = vi.spyOn(api, "GET");
    render(<DividendEvidenceReviewPanel event={{ ...event, id: "private-invalid" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("事件版本無法安全核對");
    expect(get).not.toHaveBeenCalled();
  });

  it("filters recorded status and severity without caching or retaining the previous result", async () => {
    const { get, cache } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    let release: ((value: ReturnType<typeof reply<Page>>) => void) | undefined;
    get.mockImplementation(async (_request, options) => {
      const query = (options as { params: { query: { status?: string; severity?: string } } }).params.query;
      if (query.severity === "INFO") return reply({ ...page, items: [], next_cursor: null });
      return new Promise(resolve => { release = resolve; });
    });
    fireEvent.change(screen.getByLabelText("證據審核狀態"), { target: { value: "RESOLVED" } });
    await waitFor(() => expect(release).toBeDefined());
    expect(screen.queryByText(/已記錄待核對/)).not.toBeInTheDocument();
    act(() => release?.(reply({ ...page, items: [resolved] })));
    expect(await screen.findByText(/已記錄後繼版本處理/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("證據審核嚴重度"), { target: { value: "INFO" } });
    expect(await screen.findByText(/此頁沒有已記錄的證據審核/)).toHaveTextContent("不代表歷史完整或問題已處理");
    expect(get).toHaveBeenLastCalledWith(path, expect.objectContaining({ params: expect.objectContaining({
      query: expect.objectContaining({ status: "RESOLVED", severity: "INFO", limit: 25 }),
    }) }));
    expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("aborts a pending filtered read when the selection changes", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    let signal: AbortSignal | undefined;
    get.mockImplementation(async (_request, options) => {
      const input = options as { signal: AbortSignal; params: { query: { status?: string } } };
      if (input.params.query.status === "OPEN") return new Promise(() => { signal = input.signal; });
      return reply({ ...page, items: [resolved] });
    });
    fireEvent.change(screen.getByLabelText("證據審核狀態"), { target: { value: "OPEN" } });
    await waitFor(() => expect(signal).toBeDefined());
    fireEvent.change(screen.getByLabelText("證據審核狀態"), { target: { value: "RESOLVED" } });
    expect(signal?.aborted).toBe(true);
    expect(await screen.findByText(/已記錄後繼版本處理/)).toBeVisible();
  });

  it("returns to the first page when a filter changes", async () => {
    const many = Array.from({ length: 25 }, (_, index) => ({ ...review,
      id: `a1111111-1111-4111-8111-${String(25 - index).padStart(12, "0")}`,
      portfolio_id: `b1111111-1111-4111-8111-${String(index + 1).padStart(12, "0")}` }));
    const first = { ...page, items: many, next_cursor: "opaque" };
    const second = { ...page, items: [resolved], next_cursor: null };
    const { get } = mount(first);
    expect(await screen.findByText(/第 1 頁/)).toBeVisible();
    get.mockImplementation(async (_request, options) => {
      const query = (options as { params: { query: { status?: string; cursor?: string } } }).params.query;
      return reply(query.status === "RESOLVED" || query.cursor ? second : first);
    });
    fireEvent.click(screen.getByRole("button", { name: "下一頁證據審核" }));
    expect(await screen.findByText(/第 2 頁/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("證據審核狀態"), { target: { value: "RESOLVED" } });
    expect(await screen.findByText(/第 1 頁/)).toBeVisible();
    expect(screen.getByText(/已記錄後繼版本處理/)).toBeVisible();
    expect(get).toHaveBeenLastCalledWith(path, expect.objectContaining({ params: expect.objectContaining({
      query: expect.objectContaining({ status: "RESOLVED", cursor: undefined }),
    }) }));
  });

  it("hides a response whose records do not match the selected filter", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    get.mockImplementation(async () => reply({ ...page, items: [review] }));
    fireEvent.change(screen.getByLabelText("證據審核狀態"), { target: { value: "RESOLVED" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByText(/UNRESOLVED_ADJUSTMENT/)).not.toBeInTheDocument();
  });

  it("uses a fixed message for unexpected errors instead of private diagnostics", async () => {
    const { get } = mount(); expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    get.mockRejectedValue(new Error("PRIVATE SYNTHETIC ERROR"));
    fireEvent.click(screen.getByRole("button", { name: "重新載入證據審核" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByText(/PRIVATE SYNTHETIC ERROR/)).not.toBeInTheDocument();
  });

  it("aborts the old scope when the event revision changes", async () => {
    const newer = { ...event, version: 4, current_revision_id: "a4444444-4444-4444-8444-444444444444" };
    const updated = { ...page, revision_id: newer.current_revision_id, revision_no: newer.version,
      items: [{ ...review, revision_id: newer.current_revision_id, revision_no: newer.version }] };
    let signal: AbortSignal | undefined; let calls = 0;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => {
      calls++;
      if (calls === 1) return new Promise(() => { signal = (options as { signal: AbortSignal }).signal; });
      return reply(updated);
    });
    const view = render(<DividendEvidenceReviewPanel event={event} />);
    await waitFor(() => expect(signal).toBeDefined());
    view.rerender(<DividendEvidenceReviewPanel event={newer} />);
    expect(signal?.aborted).toBe(true);
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    expect(screen.getByRole("region", { name: "股息證據審核歷史" })).toHaveTextContent(newer.current_revision_id);
  });

  it("paginates without accumulating private pages or assuming one snapshot", async () => {
    const many = Array.from({ length: 25 }, (_, index) => ({ ...review,
      id: `a1111111-1111-4111-8111-${String(25 - index).padStart(12, "0")}`,
      portfolio_id: `b1111111-1111-4111-8111-${String(index + 1).padStart(12, "0")}` }));
    const first = { ...page, items: many, next_cursor: "opaque" }; const second = { ...page, items: [resolved] };
    const { get, cache } = mount(first);
    expect(await screen.findByText(/第 1 頁/)).toBeVisible();
    get.mockImplementation(async (_request, options) => {
      const cursor = (options as { params: { query: { cursor?: string } } }).params.query.cursor;
      return reply(cursor ? second : first);
    });
    fireEvent.click(screen.getByRole("button", { name: "下一頁證據審核" }));
    expect(await screen.findByText(/第 2 頁/)).toBeVisible(); expect(screen.getByText(/已記錄後繼版本處理/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "上一頁證據審核" }));
    expect(await screen.findByText(/第 1 頁/)).toBeVisible(); expect(cache.getQueryCache().getAll()).toHaveLength(0);
  });
});
