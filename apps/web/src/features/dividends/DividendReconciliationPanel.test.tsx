import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendReconciliationPanel } from "./DividendReconciliationPanel";
import { account, event } from "./projection-test-data";
import { reconciliation } from "./reconciliation-test-data";

type Record = components["schemas"]["RecordedReconciliation"];
const path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/reconciliation";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function mount(value: unknown = reconciliation) {
  const get = vi.spyOn(api, "GET").mockImplementation(async request => {
    if (request === path) return reply(value);
    throw new Error("Unexpected private read");
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><DividendReconciliationPanel event={event} account={account} /></QueryClientProvider>);
  return { get, cache, ...view };
}

describe("recorded dividend reconciliation", () => {
  it("shows exact unverified comparison and quality without writes or browser caches", async () => {
    const post = vi.spyOn(api, "POST"); const { get, cache } = mount();
    const panel = await screen.findByRole("region", { name: "已記錄股息金額比較" });
    expect(await within(panel).findByText(/已記錄收款金額少於/)).toBeVisible();
    expect(panel).toHaveTextContent("USD 11.000000"); expect(panel).toHaveTextContent("USD 6.000000");
    expect(panel).toHaveTextContent("USD 5.000000"); expect(panel).toHaveTextContent("9007199254740993");
    expect(panel).toHaveTextContent("收款不以投影日期篩選"); expect(panel).toHaveTextContent("付款、資格或收入已確認");
    expect(panel).toHaveTextContent("差額為已記錄預計淨額減已記錄收款");
    expect(panel).toHaveTextContent("歷史完整性未驗證");
    expect(get).toHaveBeenCalledWith(path, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("keeps explicit zero separate from an empty history", async () => {
    mount({ ...reconciliation, state: "RECORDED_RECEIVED", recorded_expected: "0.000000", recorded_received: "0.000000", recorded_outstanding: "0.000000" });
    const panel = await screen.findByRole("region", { name: "已記錄股息金額比較" });
    expect(await within(panel).findByText(/已記錄金額相等/)).toBeVisible();
    expect(panel).toHaveTextContent("USD 0.000000"); expect(panel).toHaveTextContent("不代表付款已確認");
  });

  it("shows absent amounts as unavailable, never as zero", async () => {
    mount({ ...reconciliation, state: "NO_RECORDED_ACTIVE_RECEIPTS", active_receipts: "0", unverified_origins: "0",
      comparison_quality: null, recorded_received: null, recorded_outstanding: null });
    const panel = await screen.findByRole("region", { name: "已記錄股息金額比較" });
    expect(await within(panel).findByText(/沒有已記錄的未作廢收款/)).toBeVisible();
    expect(panel).toHaveTextContent("USD 11.000000"); expect(panel).toHaveTextContent("未提供，不是零");
    expect(panel).not.toHaveTextContent("USD 0.000000");
  });

  it("does not show values when independently read event heads differ", async () => {
    mount({ ...reconciliation, revision_id: "77777777-7777-4777-8777-777777777777", revision_no: event.version + 1 });
    expect(await screen.findByRole("alert")).toHaveTextContent("兩者不是同一快照");
    expect(screen.queryByText(/USD 11\.000000/)).not.toBeInTheDocument();
  });

  it("rejects malformed response without displaying private values", async () => {
    mount({ ...reconciliation, recorded_received: "synthetic-private" });
    expect(await screen.findByRole("alert")).toHaveTextContent("對帳紀錄範圍無法安全核對");
    expect(screen.queryByText(/synthetic-private/)).not.toBeInTheDocument();
    expect(screen.queryByText(/USD 11\.000000/)).not.toBeInTheDocument();
  });

  it("labels stale content after a failed refresh and hides private diagnostics", async () => {
    const { get } = mount(); expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
    get.mockImplementation(async () => ({ error: { code: "DIVIDEND_RECONCILIATION_UNAVAILABLE", message: "synthetic-private" }, response: new Response() }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入金額比較" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("對帳紀錄無法安全核對");
    expect(screen.getByText(/以下是上次讀取，尚未重新核對/)).toBeVisible();
    expect(screen.queryByText(/synthetic-private/)).not.toBeInTheDocument();
  });

  it("labels offline content as stale and requires a fresh read after reconnecting", async () => {
    const { get } = mount(); expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
    act(() => onlineManager.setOnline(false));
    expect(await screen.findByText(/連線已暫停；顯示的是上次讀取/)).toBeVisible();
    expect(screen.getByRole("button", { name: "重新載入金額比較" })).toBeDisabled();
    const calls = get.mock.calls.length; act(() => onlineManager.setOnline(true));
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls));
  });

  it("does not claim to show a previous read when first opened offline", async () => {
    act(() => onlineManager.setOnline(false));
    const view = mount();
    try {
      expect(await screen.findByText(/尚未載入金額比較，不能推定金額為零/)).toBeVisible();
      expect(screen.queryByText(/顯示的是上次讀取/)).not.toBeInTheDocument();
      expect(view.get).not.toHaveBeenCalled();
    } finally {
      view.unmount(); act(() => onlineManager.setOnline(true));
    }
  });

  it("aborts a pending private request when the panel closes", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => new Promise(() => {
      signal = (options as { signal: AbortSignal }).signal;
    }));
    const view = render(<DividendReconciliationPanel event={event} account={account} />);
    await waitFor(() => expect(signal).toBeDefined()); view.unmount(); expect(signal?.aborted).toBe(true);
  });

  it.each([
    ["NO_RECORDED_PROJECTION", "沒有已記錄投影；不代表預計金額為零", { projection_id: null, projection_no: null, projection_as_of: null,
      projection_state: null, projection_quality: null, comparison_quality: null, currency: null, recorded_expected: null, recorded_received: null, recorded_outstanding: null }],
    ["STALE_RECORDED_PROJECTION", "投影與事件修訂不同", { comparison_quality: null, currency: null, recorded_expected: null, recorded_received: null, recorded_outstanding: null }],
    ["PROJECTION_NOT_COMPARABLE", "投影無法比較", { projection_state: "NEEDS_REVIEW", projection_quality: null, comparison_quality: null,
      currency: null, recorded_expected: null, recorded_received: null, recorded_outstanding: null }],
    ["CASH_LINK_UNAVAILABLE", "收款或沖銷缺少現金關聯", { unavailable_cash_links: "1", comparison_quality: null, recorded_received: null, recorded_outstanding: null }],
    ["CURRENCY_REVIEW", "幣別不同，不能直接相減", { comparison_quality: "NEEDS_REVIEW", recorded_received: null, recorded_outstanding: null }],
    ["RECORDED_OVER_RECEIVED", "已記錄金額超過預計值", { recorded_received: "12", recorded_outstanding: "-1", comparison_quality: "NEEDS_REVIEW" }],
    ["RECORDED_NEEDS_REVIEW", "已記錄金額需要核對", { recorded_received: "-1", recorded_outstanding: "12", comparison_quality: "NEEDS_REVIEW" }],
  ])("explains %s without converting missing values to zero", async (state, label, changes) => {
    mount({ ...reconciliation, state, ...changes } as Record);
    expect(await screen.findByText(new RegExp(label))).toBeVisible();
  });
});
