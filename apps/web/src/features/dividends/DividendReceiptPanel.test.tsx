import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendReceiptPanel } from "./DividendReceiptPanel";
import { account, event } from "./projection-test-data";

type Schemas = components["schemas"];
const path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts";
const receipt: Schemas["RecordedReceipt"] = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", event_id: event.id, account_id: account.id,
  entitlement_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", revision_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", revision_no: 2,
  current_revision_id: event.current_revision_id, received_date: "2026-11-05", recorded_amount: "6.000000", currency: "USD",
  recorded_tax: "0.000000", recorded_fee: "0.000000", fx_rate_hkd: "7.800000000000", received_hkd: "46.800000",
  source: "MANUAL", status: "ACTIVE", version: "9007199254740993", created_at: "2026-11-06T00:00:00Z", voided_at: null,
  cash_scope: "MATCHING_CASH_RECORDED_UNVERIFIED", audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED", history_completeness: "UNVERIFIED",
  scope: "UNVERIFIED_RECEIPT_HISTORY_ONLY", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: unknown = { items: [receipt], next_cursor: null }) {
  return vi.spyOn(api, "GET").mockImplementation(async request => {
    if (request === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (request === path) return reply(value);
    throw new Error(`Unexpected read: ${request}`);
  });
}
function renderPanel() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><DividendReceiptPanel event={event} /></QueryClientProvider>);
  return cache;
}
async function selectAccount() { fireEvent.click(await screen.findByRole("button", { name: `選擇收款紀錄帳戶 ${account.name}` })); }

describe("recorded dividend receipt history", () => {
  it("requires an explicit account and shows original unverified receipt values without writes", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const cache = renderPanel();
    expect(get).not.toHaveBeenCalledWith(path, expect.anything());
    await selectAccount(); const panel = await screen.findByRole("region", { name: "帳戶收款紀錄歷史" });
    expect(await within(panel).findByText(/原事件修訂 2/)).toBeVisible();
    expect(panel).toHaveTextContent("6.000000"); expect(panel).toHaveTextContent("46.800000");
    expect(panel).toHaveTextContent("7.800000000000"); expect(panel).toHaveTextContent("9007199254740993");
    expect(panel).toHaveTextContent("已記錄現金關聯，尚未驗證");
    expect(panel).toHaveTextContent("歷史稽核及付款均未驗證");
    expect(panel).toHaveTextContent("稅費欄位僅為已記錄數值，不代表已確認扣款");
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("keeps unknown tax distinct from an explicitly recorded zero fee", async () => {
    reads({ items: [{ ...receipt, recorded_tax: null, recorded_fee: "0.000000" }], next_cursor: null });
    renderPanel(); await selectAccount(); const panel = await screen.findByRole("region", { name: "帳戶收款紀錄歷史" });
    expect(await within(panel).findByText(/記錄稅額未知/)).toBeVisible();
    expect(panel).toHaveTextContent("記錄費用 USD 0.000000");
    expect(panel).not.toHaveTextContent("記錄稅額 USD 0.000000");
  });

  it("does not interpret an empty page as confirmed zero or complete history", async () => {
    reads({ items: [], next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByText(/此頁沒有已記錄的收款；不代表實收為零或歷史完整/)).toBeVisible();
  });

  it("shows a void, absent reversal and negative recorded value without rewriting them", async () => {
    reads({ items: [{ ...receipt, status: "VOID", version: "2", voided_at: "2026-11-07T00:00:00Z",
      cash_scope: "CASH_REVERSAL_NOT_RECORDED", recorded_amount: "-1.250000", fx_rate_hkd: null, received_hkd: null }], next_cursor: null });
    renderPanel(); await selectAccount(); const panel = await screen.findByRole("region", { name: "帳戶收款紀錄歷史" });
    expect(await within(panel).findByText(/已作廢/)).toBeVisible();
    expect(panel).toHaveTextContent("未記錄現金沖銷"); expect(panel).toHaveTextContent("-1.250000");
    expect(panel).toHaveTextContent("港幣金額未知"); expect(panel).toHaveTextContent("記錄匯率未提供");
  });

  it("keeps different currencies as separate records and accepts exact timestamp offsets", async () => {
    const later = { ...receipt, status: "VOID", version: "2", created_at: "2026-11-06T00:00:00.000001Z",
      voided_at: "2026-11-06T08:00:00.000002+08:00", cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" };
    const other = { ...receipt, id: "99999999-9999-4999-8999-999999999999", received_date: "2026-11-04", currency: "HKD",
      recorded_amount: "46.800000", fx_rate_hkd: null, received_hkd: null, cash_scope: "CASH_NOT_RECORDED" };
    reads({ items: [later, other], next_cursor: null }); renderPanel(); await selectAccount();
    const panel = await screen.findByRole("region", { name: "帳戶收款紀錄歷史" });
    expect(await within(panel).findByText(/USD 6.000000/)).toBeVisible();
    expect(within(panel).getByText(/HKD 46.800000/)).toBeVisible();
    expect(panel).toHaveTextContent("已記錄現金沖銷關聯，尚未驗證");
    expect(panel).toHaveTextContent("不同幣別及歷史版本不在此相加");
  });

  it("visibly distinguishes an independently changed event head", async () => {
    reads({ items: [{ ...receipt, current_revision_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd" }], next_cursor: null });
    renderPanel(); await selectAccount();
    expect(await screen.findByText(/收款讀取與事件清單的目前版本不同/)).toBeVisible();
    expect(screen.getByText(/原事件修訂 2/)).toBeVisible();
  });

  it.each([
    { ...receipt, event_id: account.id }, { ...receipt, account_id: event.id },
    { ...receipt, recorded_amount: 0.1 }, { ...receipt, recorded_amount: "0.0000001" },
    { ...receipt, recorded_amount: "9223372036854.775808" }, { ...receipt, recorded_tax: "-1" },
    { ...receipt, currency: "CNY" }, { ...receipt, version: "01" }, { ...receipt, version: "9223372036854775808" },
    { ...receipt, audit_scope: "VERIFIED" }, { ...receipt, history_completeness: "COMPLETE" },
    { ...receipt, current_use_scope: "GRANTED" }, { ...receipt, eligibility_scope: "ESTABLISHED" },
    { ...receipt, status: "VOID", version: "1", voided_at: "2026-11-07T00:00:00Z", cash_scope: "CASH_REVERSAL_NOT_RECORDED" },
    { ...receipt, status: "VOID", voided_at: null }, { ...receipt, status: "ACTIVE", voided_at: "2026-11-07T00:00:00Z" },
    { ...receipt, status: "ACTIVE", cash_scope: "CASH_REVERSAL_NOT_RECORDED" },
    { ...receipt, status: "VOID", voided_at: "2026-11-07T00:00:00Z", cash_scope: "MATCHING_CASH_RECORDED_UNVERIFIED" },
    { ...receipt, status: "VOID", version: "2", created_at: "2026-11-07T00:00:00.000002Z", voided_at: "2026-11-07T08:00:00.000001+08:00", cash_scope: "CASH_REVERSAL_NOT_RECORDED" },
    { ...receipt, revision_id: event.current_revision_id, revision_no: 2 },
    { ...receipt, revision_no: event.version, revision_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" },
  ])("rejects unsafe or inconsistent receipt fields", async broken => {
    reads({ items: [broken], next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByRole("alert")).toHaveTextContent("收款紀錄範圍無法安全核對");
    expect(screen.queryByText(/原事件修訂 2/)).not.toBeInTheDocument();
  });

  it.each([
    [receipt, { ...receipt, id: receipt.id.toUpperCase() }],
    [{ ...receipt, received_date: "2026-11-04" }, { ...receipt, id: "99999999-9999-4999-8999-999999999999" }],
    [receipt, { ...receipt, id: "99999999-9999-4999-8999-999999999999", current_revision_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd" }],
    [receipt, { ...receipt, id: "99999999-9999-4999-8999-999999999999", entitlement_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd" }],
  ])("rejects duplicate, unordered or mismatched page records", async (...items) => {
    reads({ items, next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByRole("alert")).toHaveTextContent("收款紀錄範圍無法安全核對");
  });

  it("pages without adding receipt versions or converting decimal strings", async () => {
    const get = reads(); const older = { ...receipt, id: "99999999-9999-4999-8999-999999999999", received_date: "2026-11-04",
      recorded_amount: "9223372036854.775807", fx_rate_hkd: null, received_hkd: null, cash_scope: "CASH_NOT_RECORDED" };
    get.mockImplementation(async (request, options) => {
      if (request === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      const query = options as { params: { query: { cursor?: string } } };
      if (request === path) return reply(query.params.query.cursor ? { items: [older], next_cursor: null } : { items: [receipt], next_cursor: "next" });
      throw new Error("Unexpected read");
    });
    renderPanel(); await selectAccount(); expect(await screen.findByText(/原事件修訂 2/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下一頁收款紀錄" }));
    expect(await screen.findByText(/9223372036854.775807/)).toBeVisible();
    expect(screen.getByText(/未記錄現金關聯/)).toBeVisible();
    expect(screen.queryByText(/6.000000/)).not.toBeInTheDocument();
  });

  it("discards a late receipt response after another account is selected", async () => {
    const other = { ...account, id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", name: "Synthetic other account" };
    let deliver!: () => void; let signal!: AbortSignal;
    vi.spyOn(api, "GET").mockImplementation(async (request, options) => {
      if (request === "/api/v1/accounts") return reply({ items: [account, other], next_cursor: null });
      const read = options as { signal: AbortSignal; params: { path: { account_id: string } } };
      if (request === path && read.params.path.account_id === account.id) return new Promise(resolve => { signal = read.signal; deliver = () => resolve(reply({ items: [receipt], next_cursor: null })); });
      if (request === path) return reply({ items: [], next_cursor: null });
      throw new Error("Unexpected read");
    });
    renderPanel(); await selectAccount(); await waitFor(() => expect(deliver).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: `選擇收款紀錄帳戶 ${other.name}` }));
    await waitFor(() => expect(signal.aborted).toBe(true)); await act(async () => deliver());
    expect(await screen.findByText(/此頁沒有已記錄的收款/)).toBeVisible();
    expect(screen.queryByText(/原事件修訂 2/)).not.toBeInTheDocument();
  });

  it("keeps an earlier page visibly stale when refresh fails", async () => {
    const get = reads({ items: [receipt], next_cursor: "next" }); renderPanel(); await selectAccount();
    expect(await screen.findByText(/原事件修訂 2/)).toBeVisible();
    get.mockImplementation(async request => request === path
      ? { error: { code: "DIVIDEND_RECEIPT_HISTORY_UNAVAILABLE", message: "synthetic-private-diagnostic" }, response: new Response() }
      : reply({ items: [account], next_cursor: null }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入收款紀錄" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("收款歷史無法安全核對");
    expect(screen.getByText(/以下是上次讀取，尚未重新核對/)).toBeVisible();
    expect(screen.getByRole("button", { name: "下一頁收款紀錄" })).toBeDisabled();
    expect(screen.queryByText(/synthetic-private-diagnostic/)).not.toBeInTheDocument();
  });

  it("labels offline results as stale and disables pagination", async () => {
    reads({ items: [receipt], next_cursor: "next" }); renderPanel(); await selectAccount();
    expect(await screen.findByText(/原事件修訂 2/)).toBeVisible();
    act(() => onlineManager.setOnline(false));
    expect(await screen.findByText(/連線已暫停；顯示的是上次讀取/)).toBeVisible();
    expect(screen.getByRole("button", { name: "下一頁收款紀錄" })).toBeDisabled();
    act(() => onlineManager.setOnline(true));
  });
});
