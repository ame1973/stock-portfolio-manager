import type { ReactElement } from "react";
import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { AccountForm, BrokerForm, PortfolioForm, SecurityForm } from "./forms";
import { BrokersPage, PortfoliosPage } from "./MasterPages";
import { SecurityDetailPage } from "./SecurityDetailPage";
import type { Account, Broker, Identifier, Portfolio, Security } from "./data";

const portfolio: Portfolio = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic portfolio", base_currency: "HKD", cash_completeness: "UNKNOWN", data_revision: 0, active_projection_version: 0, version: 3 };
const broker: Broker = { id: "22222222-2222-4222-8222-222222222222", name: "Synthetic broker", dividend_fee: "0.000000", dividend_fee_currency: "HKD", status: "ACTIVE", version: 2 };
const account: Account = { id: "33333333-3333-4333-8333-333333333333", name: "Synthetic account", portfolio_id: portfolio.id, broker_id: broker.id, default_currency: "USD", opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
const security: Security = { id: "44444444-4444-4444-8444-444444444444", name: "Synthetic security", exchange_code: "HKG", display_ticker: "0005", security_type: "EQUITY", quote_currency: "HKD", isin: null, status: "ACTIVE", verified: false, version: 4 };
const identifier: Identifier = { id: "55555555-5555-4555-8555-555555555555", security_id: security.id, exchange_code: "HKG", identifier_type: "TICKER", identifier_value: "0005", effective_from: "2020-01-01", effective_to: null, version: 2 };
const csrf = { csrf_token: "synthetic-csrf-token", expires_at: "2099-01-01T00:00:00Z" };
const exchanges = { items: [
  { code: "HKG", market: "HK" as const, default_currency: "HKD" as const, display_name: "Hong Kong", timezone: "Asia/Hong_Kong" },
  { code: "NYSE", market: "US" as const, default_currency: "USD" as const, display_name: "New York", timezone: "America/New_York" },
], next_cursor: null };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const rejected = (code: string, status = 409) => ({ error: { code, message: "Synthetic failure", request_id: "test", details: {} }, response: new Response(null, { status }) });

function renderUI(ui: ReactElement, path = "/") {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function requestOptions(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  // openapi-fetch methods are generic; inspect their runtime mock arguments without erasing production types.
  const options = spy.mock.calls[index]?.[1];
  expect(options).toBeDefined();
  return options as { body?: unknown; params?: { header?: Record<string, string> } };
}
function referenceReads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/portfolios") return reply({ items: [portfolio], next_cursor: null });
    if (path === "/api/v1/brokers") return reply({ items: [broker], next_cursor: null });
    if (path === "/api/v1/exchanges") return reply(exchanges);
    if (path === "/api/v1/auth/csrf") return reply(csrf);
    throw new Error(`Unexpected read: ${path}`);
  });
}
async function accountFields() {
  await screen.findByRole("option", { name: portfolio.name });
  await screen.findByRole("option", { name: broker.name });
  fireEvent.change(screen.getByLabelText("帳戶名稱"), { target: { value: account.name } });
  fireEvent.change(screen.getByLabelText("投資組合", { exact: true }), { target: { value: portfolio.id } });
  fireEvent.change(screen.getByLabelText("券商", { exact: true }), { target: { value: broker.id } });
}
function renderDetail() {
  vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/securities/{id}") return reply(security);
    if (path === "/api/v1/securities/{id}/identifiers") return reply({ items: [identifier], next_cursor: null });
    if (path === "/api/v1/securities/{id}/provider-symbols") return reply({ items: [], next_cursor: null });
    if (path === "/api/v1/auth/csrf") return reply(csrf);
    throw new Error(`Unexpected read: ${path}`);
  });
  return renderUI(<Routes><Route path="/settings/securities/:id" element={<SecurityDetailPage />} /></Routes>, `/settings/securities/${security.id}`);
}

describe("master-data list states", () => {
  it("renders loading without inventing records", () => {
    vi.spyOn(api, "GET").mockImplementation(() => new Promise(() => {}));
    renderUI(<PortfoliosPage />);
    expect(screen.getByRole("status")).toHaveTextContent("正在載入投資組合");
    expect(screen.getByRole("button", { name: "新增投資組合" })).toBeEnabled();
    expect(screen.queryByText(portfolio.name)).not.toBeInTheDocument();
  });

  it("keeps creation available after failure and retries into a truthful empty state", async () => {
    vi.spyOn(api, "GET").mockRejectedValueOnce(new Error("讀取失敗")).mockResolvedValueOnce(reply({ items: [], next_cursor: null }));
    renderUI(<PortfoliosPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("讀取失敗");
    fireEvent.click(screen.getByRole("button", { name: "重試載入投資組合" }));
    expect(await screen.findByText(/尚未建立投資組合/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "新增投資組合" }));
    expect(screen.getByRole("form", { name: "新增投資組合" })).toBeVisible();
  });

  it("uses bounded cursor pages and resets the cursor when searching", async () => {
    const get = vi.spyOn(api, "GET")
      .mockResolvedValueOnce(reply({ items: [portfolio], next_cursor: "synthetic-cursor" }))
      .mockResolvedValueOnce(reply({ items: [{ ...portfolio, name: "Second page" }], next_cursor: null }))
      .mockResolvedValueOnce(reply({ items: [], next_cursor: null }));
    renderUI(<PortfoliosPage />);
    await screen.findByRole("heading", { name: portfolio.name });
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    await screen.findByRole("heading", { name: "Second page" });
    expect(get).toHaveBeenLastCalledWith("/api/v1/portfolios", expect.objectContaining({ params: { query: { q: "", cursor: "synthetic-cursor", limit: 25 } } }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "literal%_" } });
    await screen.findByText(/沒有符合搜尋/);
    expect(get).toHaveBeenLastCalledWith("/api/v1/portfolios", expect.objectContaining({ params: { query: { q: "literal%_", cursor: undefined, limit: 25 } } }));
    expect(screen.getByRole("button", { name: "上一頁" })).toBeDisabled();
  });

  it("shows an explicit offline state without issuing a queued read", () => {
    const get = vi.spyOn(api, "GET").mockImplementation(() => new Promise(() => {}));
    onlineManager.setOnline(false);
    try {
      renderUI(<PortfoliosPage />);
      expect(screen.getByRole("status")).toHaveTextContent("連線已暫停");
      expect(get).not.toHaveBeenCalled();
    } finally { onlineManager.setOnline(true); }
  });

  it("retains the last loaded records with a stale warning after a refresh failure", async () => {
    vi.spyOn(api, "GET").mockResolvedValueOnce(reply({ items: [portfolio], next_cursor: null })).mockRejectedValueOnce(new TypeError("Network failed"));
    renderUI(<PortfoliosPage />);
    await screen.findByRole("heading", { name: portfolio.name });
    fireEvent.click(screen.getByRole("button", { name: "重新載入投資組合" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("尚未確認最新版本");
    expect(screen.getByRole("alert")).toHaveTextContent("網絡連線中斷");
    expect(screen.getByRole("heading", { name: portfolio.name })).toBeVisible();
  });

  it("requires confirmation before soft archive and sends the observed version", async () => {
    const get = vi.spyOn(api, "GET").mockImplementation(async path => path === "/api/v1/auth/csrf" ? reply(csrf) : reply({ items: [broker], next_cursor: null }));
    const remove = vi.spyOn(api, "DELETE").mockResolvedValue(reply({ ok: true, message: "OK" }));
    renderUI(<BrokersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "封存" }));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("封存不是刪除");
    expect(document.head.querySelector("style")).toBeNull();
    expect(remove).not.toHaveBeenCalled();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "封存" }));
    get.mockImplementation(async path => path === "/api/v1/auth/csrf" ? reply(csrf) : reply({ items: [{ ...broker, status: "ARCHIVED" as const, version: 3 }], next_cursor: null }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "確認" }));
    expect(await screen.findByRole("button", { name: "重新啟用" })).toBeVisible();
    expect(remove).toHaveBeenCalledWith("/api/v1/brokers/{id}", { params: { path: { id: broker.id }, header: { "If-Match": '"2"', "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": expect.any(String) } } });
  });
});

describe("exact and versioned forms", () => {
  it("preserves edited input on 412 and does not claim success", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(reply(csrf));
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    const saved = vi.fn();
    renderUI(<PortfolioForm record={portfolio} onSaved={saved} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("組合名稱"), { target: { value: "Do not overwrite" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不會覆蓋新版");
    expect(screen.getByLabelText("組合名稱")).toHaveValue("Do not overwrite");
    expect(saved).not.toHaveBeenCalled();
    expect(patch).toHaveBeenCalledWith("/api/v1/portfolios/{id}", expect.objectContaining({ params: { path: { id: portfolio.id }, header: { "If-Match": '"3"', "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": expect.any(String) } } }));
  });

  it("retains one idempotency key for an unchanged retry and changes it for edited input", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(reply(csrf));
    const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("Network failure")).mockRejectedValueOnce(new TypeError("Network failure")).mockResolvedValueOnce(reply(broker));
    const saved = vi.fn();
    renderUI(<BrokerForm onSaved={saved} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("券商名稱"), { target: { value: broker.name } });
    fireEvent.change(screen.getByLabelText("每次股息估計代收費"), { target: { value: "0010.000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/網絡連線中斷/);
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "儲存" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("券商名稱"), { target: { value: "Changed broker" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    const keys = [0, 1, 2].map(index => requestOptions(post, index).params?.header?.["Idempotency-Key"]);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[0]);
    expect(requestOptions(post)).toMatchObject({ body: { dividend_fee: "0010.000001" } });
  });

  it("rejects excessive money precision without rounding or contacting the server", async () => {
    const post = vi.spyOn(api, "POST");
    renderUI(<BrokerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("券商名稱"), { target: { value: broker.name } });
    fireEvent.change(screen.getByLabelText("每次股息估計代收費"), { target: { value: "0.1234567" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("最多 6 位小數");
    expect(post).not.toHaveBeenCalled();
  });

  it("requires an explicit opening date and locked USD FX", async () => {
    referenceReads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(account));
    renderUI(<AccountForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await accountFields();
    fireEvent.change(screen.getByLabelText("帳戶預設幣別"), { target: { value: "USD" } });
    fireEvent.change(screen.getByLabelText("起始餘額（選填）"), { target: { value: "1000.000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText("起始餘額必須有明確日期。")).toBeVisible();
    expect(await screen.findByText("USD 起始餘額必須有當時鎖定的歷史匯率。")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("起始餘額日期"), { target: { value: "2020-01-02" } });
    fireEvent.change(screen.getByLabelText("起始歷史匯率（1 USD = HKD）"), { target: { value: "7.801234567890" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(requestOptions(post)).toMatchObject({ body: { opening_cash: "1000.000001", opening_cash_date: "2020-01-02", opening_fx_rate_hkd: "7.801234567890" } });
  });

  it("does not silently discard an opening date supplied without a balance", async () => {
    referenceReads();
    const post = vi.spyOn(api, "POST");
    renderUI(<AccountForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await accountFields();
    fireEvent.change(screen.getByLabelText("起始餘額日期"), { target: { value: "2020-01-02" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("未知餘額請全部留空");
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps an unknown opening balance absent rather than zero", async () => {
    referenceReads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(account));
    renderUI(<AccountForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await accountFields();
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(requestOptions(post)).toMatchObject({ body: { cash_completeness: "UNKNOWN" } });
    expect(requestOptions(post)?.body).not.toHaveProperty("opening_cash");
  });

  it.each(["0005", "BRK.B"])("preserves the security ticker %s", async ticker => {
    referenceReads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(security));
    renderUI(<SecurityForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("option", { name: /HKG/ });
    fireEvent.change(screen.getByLabelText("證券名稱"), { target: { value: security.name } });
    fireEvent.change(screen.getByLabelText("證券代碼"), { target: { value: ticker } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(requestOptions(post)).toMatchObject({ body: { display_ticker: ticker } });
    expect(requestOptions(post)?.body).not.toHaveProperty("verified");
  });

  it("requires evidence before manually verifying a security", async () => {
    referenceReads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply({ ...security, verified: true, version: 5 }));
    renderUI(<SecurityForm record={security} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("已人工核實證券身分"));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("依據說明");
    expect(patch).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("人工核實依據"), { target: { value: "Synthetic exchange evidence reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    expect(requestOptions(patch)).toMatchObject({ body: { verified: true, reason: "Synthetic exchange evidence reviewed" } });
  });

  it("clears a selected reference when its search changes", async () => {
    referenceReads();
    const post = vi.spyOn(api, "POST");
    renderUI(<AccountForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await accountFields();
    expect(screen.getByLabelText("投資組合", { exact: true })).toHaveValue(portfolio.id);
    fireEvent.change(screen.getByRole("searchbox", { name: "搜尋投資組合" }), { target: { value: "different search" } });
    await waitFor(() => expect(screen.getByLabelText("投資組合", { exact: true })).toHaveValue(""));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("請選擇投資組合");
    expect(post).not.toHaveBeenCalled();
  });

  it("supports reference-picker failure and retry instead of inventing a selection", async () => {
    let failed = false;
    vi.spyOn(api, "GET").mockImplementation(async path => {
      if (path === "/api/v1/portfolios" && !failed) { failed = true; return rejected("DATABASE_UNAVAILABLE", 503); }
      return path === "/api/v1/portfolios" ? reply({ items: [portfolio], next_cursor: null }) : reply({ items: [broker], next_cursor: null });
    });
    renderUI(<AccountForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("資料庫暫時無法使用");
    expect(screen.getByLabelText("投資組合", { exact: true })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重試載入投資組合" }));
    await screen.findByRole("option", { name: portfolio.name });
    expect(screen.getByLabelText("投資組合", { exact: true })).toHaveValue("");
  });
});

describe("dated security mappings", () => {
  it("retains input and shows overlapping-identity conflicts", async () => {
    renderDetail();
    const post = vi.spyOn(api, "POST").mockResolvedValue(rejected("CONFLICT"));
    await screen.findByText("TICKER: 0005");
    fireEvent.click(screen.getByRole("button", { name: "新增識別碼" }));
    fireEvent.change(screen.getByRole("textbox", { name: "識別碼" }), { target: { value: "0005" } });
    fireEvent.change(screen.getByLabelText("有效起日"), { target: { value: "2020-01-01" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("有效期重疊");
    expect(screen.getByRole("textbox", { name: "識別碼" })).toHaveValue("0005");
    expect(requestOptions(post)).toMatchObject({ body: { exchange_code: "HKG", identifier_value: "0005", effective_from: "2020-01-01" } });
  });

  it("requires an explicit correction reason and sends the mapping version", async () => {
    renderDetail();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    fireEvent.click(await screen.findByRole("button", { name: "更正有效期" }));
    fireEvent.change(screen.getByLabelText("有效迄日（選填）"), { target: { value: "2020-12-31" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("依據說明");
    expect(patch).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("更正／核實依據"), { target: { value: "Synthetic source correction reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/不會覆蓋新版/)).toBeVisible();
    expect(requestOptions(patch)).toMatchObject({ params: { header: { "If-Match": '"2"' } }, body: { effective_to: "2020-12-31" } });
    expect(screen.getByLabelText("有效迄日（選填）")).toHaveValue("2020-12-31");
  });

  it("rejects provider URLs and unsubstantiated manual verification locally", async () => {
    renderDetail();
    const post = vi.spyOn(api, "POST");
    fireEvent.click(await screen.findByRole("button", { name: "新增供應商代碼" }));
    fireEvent.change(screen.getByRole("textbox", { name: "供應商代碼" }), { target: { value: "https://invalid.example/0005" } });
    fireEvent.change(screen.getByLabelText("有效起日"), { target: { value: "2020-01-01" } });
    fireEvent.click(screen.getByLabelText("已人工核實此供應商代碼"));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/不能填入 URL/)).toBeVisible();
    expect(await screen.findByText(/需要至少 10 個字元/)).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });
});
