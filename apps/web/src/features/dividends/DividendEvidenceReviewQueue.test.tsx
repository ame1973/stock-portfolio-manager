import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendEvidenceReviewQueue } from "./DividendEvidenceReviewQueue";
import { activeReview, resolvedReview, reviewQueue } from "./evidence-review-queue-test-data";

const path = "/api/v1/dividends/evidence-reviews";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function mount(value: unknown = reviewQueue) {
  const get = vi.spyOn(api, "GET").mockImplementation(async request => {
    if (request === path) return reply(value);
    throw new Error("Unexpected private read");
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenEvent = vi.fn();
  const view = render(<QueryClientProvider client={cache}><DividendEvidenceReviewQueue onOpenEvent={onOpenEvent} /></QueryClientProvider>);
  return { get, cache, onOpenEvent, ...view };
}

describe("cross-event evidence review queue", () => {
  it("shows only retained evidence reviews and opens the chosen event", async () => {
    const { cache, onOpenEvent } = mount();
    const panel = await screen.findByRole("region", { name: "跨事件證據審核" });
    expect(await within(panel).findByText(/已記錄待核對/)).toBeVisible();
    expect(panel).toHaveTextContent("收款及重建核對不在此清單");
    expect(panel).toHaveTextContent("歷史完整性未驗證");
    expect(panel).toHaveTextContent(activeReview.event_id);
    fireEvent.click(within(panel).getAllByRole("button", { name: "查看此事件" })[0]!);
    expect(onOpenEvent).toHaveBeenCalledWith(activeReview.event_id);
    expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("filters selected rows and hides the previous page while reading", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    let release: ((value: ReturnType<typeof reply<typeof reviewQueue>>) => void) | undefined;
    get.mockImplementation(async () => new Promise(resolve => { release = resolve; }));
    fireEvent.change(screen.getByLabelText("跨事件審核狀態"), { target: { value: "RESOLVED" } });
    await waitFor(() => expect(release).toBeDefined());
    expect(screen.queryByText(/已記錄待核對/)).not.toBeInTheDocument();
    act(() => release?.(reply({ ...reviewQueue, items: [resolvedReview] })));
    expect(await screen.findByText(/已記錄後繼版本處理/)).toBeVisible();
    expect(get).toHaveBeenLastCalledWith(path, expect.objectContaining({ params: expect.objectContaining({
      query: expect.objectContaining({ status: "RESOLVED", limit: 25 }),
    }) }));
  });

  it("rejects responses that do not match selected severity", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    get.mockImplementation(async () => reply({ ...reviewQueue, items: [activeReview] }));
    fireEvent.change(screen.getByLabelText("跨事件審核嚴重度"), { target: { value: "INFO" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByText(activeReview.event_id)).not.toBeInTheDocument();
  });

  it("aborts an obsolete filtered read when the selection changes", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    let signal: AbortSignal | undefined;
    get.mockImplementation(async (_request, options) => {
      const input = options as { signal: AbortSignal; params: { query: { status?: string } } };
      if (input.params.query.status === "OPEN") return new Promise(() => { signal = input.signal; });
      return reply({ ...reviewQueue, items: [resolvedReview] });
    });
    fireEvent.change(screen.getByLabelText("跨事件審核狀態"), { target: { value: "OPEN" } });
    await waitFor(() => expect(signal).toBeDefined());
    fireEvent.change(screen.getByLabelText("跨事件審核狀態"), { target: { value: "RESOLVED" } });
    expect(signal?.aborted).toBe(true);
    expect(await screen.findByText(/已記錄後繼版本處理/)).toBeVisible();
  });

  it("disables navigation while another editor is active", async () => {
    vi.spyOn(api, "GET").mockImplementation(async () => reply(reviewQueue));
    render(<DividendEvidenceReviewQueue onOpenEvent={() => {}} disabled />);
    expect((await screen.findAllByRole("button", { name: "查看此事件" }))[0]).toBeDisabled();
  });

  it("hides offline content and revalidates after reconnecting", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    try {
      act(() => onlineManager.setOnline(false));
      expect(await screen.findByText(/連線已暫停/)).toBeVisible();
      expect(screen.queryByText(activeReview.event_id)).not.toBeInTheDocument();
      const calls = get.mock.calls.length;
      act(() => onlineManager.setOnline(true));
      await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls));
      expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    } finally { act(() => onlineManager.setOnline(true)); }
  });

  it("resets pagination when filters change", async () => {
    const many = Array.from({ length: 25 }, (_, index) => ({ ...activeReview,
      id: `a1111111-1111-4111-8111-${String(25 - index).padStart(12, "0")}`,
      portfolio_id: `b1111111-1111-4111-8111-${String(index + 1).padStart(12, "0")}` }));
    const first = { ...reviewQueue, items: many, next_cursor: "opaque" };
    const second = { ...reviewQueue, items: [resolvedReview], next_cursor: null };
    const { get } = mount(first);
    expect(await screen.findByText(/第 1 頁/)).toBeVisible();
    get.mockImplementation(async (_request, options) => {
      const query = (options as { params: { query: { status?: string; cursor?: string } } }).params.query;
      return reply(query.status === "OPEN" ? first : query.cursor ? second : first);
    });
    fireEvent.click(screen.getByRole("button", { name: "下一頁跨事件審核" }));
    expect(await screen.findByText(/第 2 頁/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("跨事件審核狀態"), { target: { value: "OPEN" } });
    expect(await screen.findByText(/第 1 頁/)).toBeVisible();
    expect(get).toHaveBeenLastCalledWith(path, expect.objectContaining({ params: expect.objectContaining({
      query: expect.objectContaining({ status: "OPEN", cursor: undefined }),
    }) }));
  });

  it("shows a fixed error instead of unexpected private diagnostics", async () => {
    const { get } = mount();
    expect(await screen.findByText(/已記錄待核對/)).toBeVisible();
    get.mockRejectedValue(new Error("PRIVATE SYNTHETIC DETAIL"));
    fireEvent.click(screen.getByRole("button", { name: "重新載入跨事件審核" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByText(/PRIVATE SYNTHETIC DETAIL/)).not.toBeInTheDocument();
    expect(screen.queryByText(activeReview.event_id)).not.toBeInTheDocument();
  });

  it("aborts a pending request on unmount", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => new Promise(() => { signal = (options as { signal: AbortSignal }).signal; }));
    const view = render(<DividendEvidenceReviewQueue onOpenEvent={() => {}} />);
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });
});
