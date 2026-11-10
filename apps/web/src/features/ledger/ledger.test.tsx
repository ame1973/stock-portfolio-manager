import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { TradeForm, type Trade } from "./TradeForm";
import { TradesPage } from "./TradesPage";
import { ReferenceSelect } from "../settings/ReferenceSelect";

type Schemas = components["schemas"];
const portfolio: Schemas["Portfolio"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic portfolio", base_currency: "HKD", cash_completeness: "UNKNOWN", data_revision: 1, active_projection_version: 0, version: 1 };
const account: Schemas["Account"] = { id: "22222222-2222-4222-8222-222222222222", portfolio_id: portfolio.id, broker_id: "33333333-3333-4333-8333-333333333333", name: "Synthetic account", default_currency: "USD", opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
const security: Schemas["Security"] = { id: "44444444-4444-4444-8444-444444444444", name: "Synthetic US security", exchange_code: "NASDAQ", display_ticker: "SYN", security_type: "EQUITY", quote_currency: "USD", isin: null, status: "ACTIVE", verified: false, version: 1 };
const trade: Trade = {
  id: "55555555-5555-4555-8555-555555555555", root_id: "55555555-5555-4555-8555-555555555555", portfolio_id: portfolio.id,
  account_id: account.id, security_id: security.id, side: "BUY", trade_date: "2026-01-02", executed_at_utc: null,
  quantity: "10.00000000", price: "100.00000000", fee: "1.000000", tax: "0.000000", currency: "USD", fx_rate_hkd: "7.800000000000",
  fx_source: "MANUAL", gross: "1000.000000", settlement: "1001.000000", booked_hkd: "7807.800000", rounding_delta: "0.000000",
  settlement_override: false, hkd_override: false, override_reason: "", status: "ACTIVE", revision: 1, version: 3,
  supersedes_id: null, created_sequence: 1, note: "Original evidence", created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:00:00Z",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const rejected = (code: string, status = 409) => ({ error: { code, message: "Synthetic failure", request_id: "test", details: {} }, response: new Response(null, { status }) });

function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(items: Trade[] = []) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    switch (path) {
      case "/api/v1/auth/csrf": return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
      case "/api/v1/portfolios": return reply({ items: [portfolio], next_cursor: null });
      case "/api/v1/accounts": return reply({ items: [account], next_cursor: null });
      case "/api/v1/securities": return reply({ items: [security], next_cursor: null });
      case "/api/v1/portfolios/{id}": return reply(portfolio);
      case "/api/v1/accounts/{id}": return reply(account);
      case "/api/v1/securities/{id}": return reply(security);
      case "/api/v1/trades": return reply({ items, next_cursor: null });
      default: throw new Error(`Unexpected read: ${path}`);
    }
  });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Record<string, unknown>; params: { header: Record<string, string> } };
}
async function fillCreate() {
  await screen.findByRole("option", { name: portfolio.name });
  fireEvent.change(screen.getByLabelText("投資組合", { exact: true }), { target: { value: portfolio.id } });
  await screen.findByRole("option", { name: `${account.name} (USD)` });
  fireEvent.change(screen.getByLabelText("交易帳戶", { exact: true }), { target: { value: account.id } });
  await screen.findByRole("option", { name: "SYN / NASDAQ — Synthetic US security" });
  fireEvent.change(screen.getByLabelText("交易證券", { exact: true }), { target: { value: security.id } });
  await screen.findByText(/報價及結算幣別：USD/);
  for (const [label, value] of [["交易日期", "2026-01-02"], ["交易股數", "10"], ["每股成交價", "100"], ["交易費用", "1"], ["歷史匯率（1 USD = HKD）", "7.8"]]) {
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
  }
}

describe("trade forms preserve exact facts and intent", () => {
  it("submits strings, explicit FX, unknown time and evidence-only overrides", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(trade));
    const saved = vi.fn();
    renderUI(<TradeForm onSaved={saved} onCancel={vi.fn()} />);
    await fillCreate();
    fireEvent.change(screen.getByLabelText("交易股數"), { target: { value: "0010.00000001" } });
    fireEvent.change(screen.getByLabelText("實際原幣交收額"), { target: { value: "1001.000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/實際交收覆寫需要/)).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("實際金額覆寫依據"), { target: { value: "Synthetic broker actual statement" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(trade));
    expect(options(post).body).toMatchObject({ quantity: "0010.00000001", price: "100", fee: "1", tax: "0", currency: "USD", fx_rate_hkd: "7.8", actual_settlement: "1001.000001", actual_hkd: null, executed_at_utc: null });
  });

  it.each([["交易股數", "0.000000001"], ["每股成交價", "1e3"], ["交易費用", "0.0000001"], ["歷史匯率（1 USD = HKD）", "7.8000000000001"]])("rejects %s precision/notation without contacting the writer", async (label, value) => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<TradeForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillCreate();
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByLabelText(label!)).toHaveValue(value);
  });

  it("keeps input and one intent key after failures, changing the key only for changed input", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic network failure"));
    renderUI(<TradeForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillCreate();
    for (let attempt = 1; attempt <= 2; attempt++) {
      fireEvent.click(screen.getByRole("button", { name: "儲存" }));
      await screen.findByText(/網絡連線中斷/);
      await waitFor(() => expect(post).toHaveBeenCalledTimes(attempt));
    }
    expect(screen.getByLabelText("交易股數")).toHaveValue("10");
    expect(options(post, 0).params.header["Idempotency-Key"]).toBe(options(post, 1).params.header["Idempotency-Key"]);
    fireEvent.change(screen.getByLabelText("每股成交價"), { target: { value: "101" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
  });

  it("requires a reason and uses the observed version, preserving stale revision input", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    const saved = vi.fn();
    renderUI(<TradeForm record={trade} onSaved={saved} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("每股成交價"), { target: { value: "101.00000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/更正需要至少/)).toBeVisible();
    expect(patch).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value: "Synthetic full corrected broker record" } });
    await screen.findByText(/報價及結算幣別：USD/);
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/不會覆蓋新版/)).toBeVisible();
    expect(options(patch).params.header["If-Match"]).toBe('"3"');
    expect(options(patch).body).toMatchObject({ price: "101.00000001", quantity: "10.00000000", reason: "Synthetic full corrected broker record" });
    expect(screen.getByLabelText("每股成交價")).toHaveValue("101.00000001");
    expect(saved).not.toHaveBeenCalled();
  });

  it("retains a manual FX source label without duplicating its stored prefix", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(trade));
    renderUI(<TradeForm record={{ ...trade, fx_source: "MANUAL:Synthetic broker statement" }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByText(/報價及結算幣別：USD/);
    fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value: "Correct the synthetic broker record" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(options(patch).body.fx_source).toBe("Synthetic broker statement");
  });

  it("retains missing USD FX as null and reports server oversell without a success callback", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValueOnce(rejected("MISSING_FX", 422)).mockResolvedValueOnce(rejected("INSUFFICIENT_SHARES"));
    const saved = vi.fn();
    renderUI(<TradeForm onSaved={saved} onCancel={vi.fn()} />);
    await fillCreate();
    fireEvent.change(screen.getByLabelText("歷史匯率（1 USD = HKD）"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/不能使用現價或預設匯率/)).toBeVisible();
    expect(options(post).body.fx_rate_hkd).toBeNull();
    fireEvent.change(screen.getByLabelText("歷史匯率（1 USD = HKD）"), { target: { value: "7.9" } });
    fireEvent.change(screen.getByLabelText("買賣方向"), { target: { value: "SELL" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByText(/持倉不足/)).toBeVisible();
    expect(screen.getByLabelText("交易股數")).toHaveValue("10");
    expect(saved).not.toHaveBeenCalled();
  });

  it("clears account on portfolio/search changes and does not retain an invisible security or old FX", async () => {
    reads();
    renderUI(<TradeForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillCreate();
    fireEvent.change(screen.getByLabelText("搜尋交易證券"), { target: { value: "other" } });
    expect(screen.getByLabelText("交易證券", { exact: true })).toHaveValue("");
    expect(screen.getByLabelText("歷史匯率（1 USD = HKD）")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("搜尋投資組合"), { target: { value: "different" } });
    expect(screen.getByLabelText("投資組合", { exact: true })).toHaveValue("");
    expect(screen.getByLabelText("交易帳戶", { exact: true })).toHaveValue("");
  });
});

describe("trade list controls do not rewrite history", () => {
  it("lets the financial sheet own initial focus instead of a native form autofocus", async () => {
    reads();
    renderUI(<TradesPage />);
    const create = screen.getByRole("button", { name: "新增交易" });
    create.focus(); fireEvent.click(create);
    const sheet = await screen.findByRole("dialog", { name: "新增交易" });
    await waitFor(() => expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus());
    expect(within(sheet).getByLabelText("搜尋投資組合")).not.toHaveFocus();
  });

  it("does not call a filtered empty page a nonexistent ledger", async () => {
    reads();
    renderUI(<TradesPage />);
    await screen.findByText(/尚未建立交易/);
    fireEvent.change(screen.getByLabelText("交易起日"), { target: { value: "2026-01-01" } });
    expect(await screen.findByText(/這個篩選範圍沒有紀錄/)).toBeVisible();
    expect(screen.queryByText(/尚未建立交易/)).not.toBeInTheDocument();
  });

  it("permits archived accounts only for read filters, never as new financial targets", async () => {
    const get = vi.spyOn(api, "GET").mockResolvedValue(reply({ items: [{ ...account, status: "ARCHIVED" as const }], next_cursor: null }));
    const { unmount } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ReferenceSelect kind="accounts" label="讀取帳戶" value="" activeOnly={false} clearSelection={vi.fn()} registration={{}} /></QueryClientProvider>);
    expect(await screen.findByRole("option", { name: `${account.name} (USD)（已封存）` })).toBeEnabled();
    expect(get).toHaveBeenCalledWith("/api/v1/accounts", expect.objectContaining({ params: { query: { q: "", limit: 100, portfolio_id: undefined, status: undefined } } }));
    unmount();
    renderUI(<ReferenceSelect kind="accounts" label="寫入帳戶" value="" clearSelection={vi.fn()} registration={{}} />);
    expect(await screen.findByRole("option", { name: `${account.name} (USD)（已封存）` })).toBeDisabled();
    expect(get).toHaveBeenCalledWith("/api/v1/accounts", expect.objectContaining({ params: { query: { q: "", limit: 100, portfolio_id: undefined, status: "ACTIVE" } } }));
  });

  it("keeps an existing account visible beyond the bounded first picker page", async () => {
    vi.spyOn(api, "GET").mockImplementation(async path => path === "/api/v1/accounts/{id}" ? reply(account) : reply({ items: [], next_cursor: "more" }));
    renderUI(<ReferenceSelect kind="accounts" label="既有帳戶" value={account.id} clearSelection={vi.fn()} registration={{}} />);
    expect(await screen.findByRole("option", { name: `${account.name} (USD)` })).toBeVisible();
    expect(screen.getByLabelText("既有帳戶", { exact: true })).toHaveValue(account.id);
  });

  it("never carries mathematical HKD identity into a changed USD security as historical FX", async () => {
    const hkd = { ...security, id: "77777777-7777-4777-8777-777777777777", display_ticker: "0005", exchange_code: "HKG", quote_currency: "HKD" as const };
    vi.spyOn(api, "GET").mockImplementation(async (path, request) => {
      const id = (request as { params?: { path?: { id?: string } } })?.params?.path?.id;
      if (path === "/api/v1/securities/{id}") return reply(id === hkd.id ? hkd : security);
      if (path === "/api/v1/securities") return reply({ items: [hkd, security], next_cursor: null });
      if (path === "/api/v1/accounts/{id}") return reply(account);
      if (path === "/api/v1/portfolios/{id}") return reply(portfolio);
      if (path === "/api/v1/portfolios") return reply({ items: [portfolio], next_cursor: null });
      if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    });
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("MISSING_FX", 422));
    renderUI(<TradeForm record={{ ...trade, security_id: hkd.id, currency: "HKD", fx_rate_hkd: "1.000000000000" }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByText(/HKD 使用 1:1 記帳/);
    await screen.findByRole("option", { name: "SYN / NASDAQ — Synthetic US security" });
    fireEvent.change(screen.getByLabelText("交易證券", { exact: true }), { target: { value: security.id } });
    await screen.findByText(/報價及結算幣別：USD/);
    expect(screen.getByLabelText("歷史匯率（1 USD = HKD）")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value: "Correct synthetic quoted security identity" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/不能使用現價或預設匯率/);
    expect(options(patch).body).toMatchObject({ currency: "USD", fx_rate_hkd: null });
  });
  it("shows draft/unknown booking and old versions without claiming zero or editable history", async () => {
    reads([{ ...trade, status: "DRAFT_SCHEDULED", booked_hkd: null, fx_rate_hkd: null }, { ...trade, id: "66666666-6666-4666-8666-666666666666", status: "SUPERSEDED" }]);
    renderUI(<TradesPage />);
    expect(await screen.findByText("未來草稿，尚未入帳")).toBeVisible();
    expect(screen.getByText("未知，未確認 HKD 認列")).toBeVisible();
    expect(screen.getByText(/日期到達不會自動入帳/)).toBeVisible();
    const old = screen.getByText("已有新修訂").closest("article")!;
    expect(within(old).queryByRole("button", { name: "更正交易" })).not.toBeInTheDocument();
    expect(within(old).queryByRole("button", { name: "作廢交易" })).not.toBeInTheDocument();
  });

  it("requires a reason and second confirmation; failed void retains observed version and retry key", async () => {
    reads([trade]);
    const remove = vi.spyOn(api, "DELETE").mockResolvedValue(rejected("INSUFFICIENT_SHARES"));
    renderUI(<TradesPage />);
    fireEvent.click(await screen.findByRole("button", { name: "作廢交易" }));
    expect(screen.getByRole("button", { name: "作廢此交易" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("作廢交易依據"), { target: { value: "Synthetic dependency review before void" } });
    fireEvent.click(screen.getByRole("button", { name: "作廢此交易" }));
    expect(remove).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
    for (let attempt = 1; attempt <= 2; attempt++) {
      fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
      await within(dialog).findByText(/持倉不足/);
      await waitFor(() => expect(remove).toHaveBeenCalledTimes(attempt));
    }
    expect(options(remove).body).toEqual({ reason: "Synthetic dependency review before void" });
    expect(options(remove).params.header["If-Match"]).toBe('"3"');
    expect(options(remove, 0).params.header["Idempotency-Key"]).toBe(options(remove, 1).params.header["Idempotency-Key"]);
    expect(screen.queryByText(/交易已作廢；/)).not.toBeInTheDocument();
  });

  it("sends history/date filters to the server without resetting financial input", async () => {
    const get = reads([trade]);
    renderUI(<TradesPage />);
    await screen.findByRole("button", { name: "查看此事件修訂鏈" });
    fireEvent.click(screen.getByRole("button", { name: "查看此事件修訂鏈" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/trades", expect.objectContaining({ params: { query: expect.objectContaining({ root_id: trade.root_id, include_history: true, limit: 25 }) } })));
    fireEvent.change(screen.getByLabelText("交易起日"), { target: { value: "2026-01-01" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/trades", expect.objectContaining({ params: { query: expect.objectContaining({ from_date: "2026-01-01", root_id: trade.root_id }) } })));
  });
});
