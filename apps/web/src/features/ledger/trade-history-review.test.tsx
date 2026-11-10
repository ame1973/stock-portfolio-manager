import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { TradeHistoryReviewForm, TradeHistoryReviewPanel, type ReviewIntent } from "./TradeHistoryReview";
import { TradesPage } from "./TradesPage";

type Schemas = components["schemas"];
const tradeId = "55555555-5555-4555-8555-555555555555";
const reason = "Explicitly review this synthetic unverified history.";
const status: Schemas["TradeHistoryReviewStatus"] = {
  trade_id: tradeId, trade_version: 1, trade_status: "DRAFT_SCHEDULED", review_revision: "0",
  current_gap: "MISSING_CREATION_AUDIT", state: "NOT_REVIEWED", latest: null,
  scope: "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY", history_completeness: "UNVERIFIED",
};
const first: Schemas["TradeHistoryReviewRecord"] = {
  id: "66666666-6666-4666-8666-666666666666", trade_id: tradeId, revision: "1",
  decision: "ACKNOWLEDGE_UNVERIFIED", gap: "MISSING_CREATION_AUDIT",
  observed_trade_status: "DRAFT_SCHEDULED", observed_trade_version: 1,
  created_at: "2026-11-10T00:00:00.000000Z", reason,
  scope: "UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY", history_completeness: "UNVERIFIED",
};
const intent: ReviewIntent = {
  trade_id: tradeId, trade_version: 1, expected_revision: "0",
  expected_gap: "MISSING_CREATION_AUDIT", decision: "ACKNOWLEDGE_UNVERIFIED",
};
const trade: Schemas["TradeRecord"] = {
  id: tradeId, root_id: tradeId, portfolio_id: "11111111-1111-4111-8111-111111111111",
  account_id: "22222222-2222-4222-8222-222222222222", security_id: "44444444-4444-4444-8444-444444444444",
  side: "BUY", trade_date: "2999-01-02", executed_at_utc: null, quantity: "10.00000000", price: "10.00000000",
  fee: "0.000000", tax: "0.000000", currency: "USD", fx_rate_hkd: null, fx_source: "MANUAL",
  gross: "100.000000", settlement: "100.000000", booked_hkd: null, rounding_delta: "0.000000",
  settlement_override: false, hkd_override: false, override_reason: "", status: "DRAFT_SCHEDULED",
  revision: 1, version: 1, supersedes_id: null, created_sequence: 1, note: "",
  created_at: "2026-11-10T00:00:00.000000Z", updated_at: "2026-11-10T00:00:00.000000Z",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const failure = (code: string, statusCode = 409) => ({ error: { code, message: "private-untrusted", request_id: "test", details: {} }, response: new Response(null, { status: statusCode }) });
function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(current: () => Schemas["TradeHistoryReviewStatus"] = () => status) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/trades/{id}/history-review") return reply(current());
    throw new Error(`Unexpected read: ${path}`);
  });
}
function pageReads(current: () => Schemas["TradeHistoryReviewStatus"]) {
  const account: Schemas["Account"] = { id: trade.account_id, portfolio_id: trade.portfolio_id, broker_id: "33333333-3333-4333-8333-333333333333", name: "Synthetic account", default_currency: "USD", opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
  const security: Schemas["Security"] = { id: trade.security_id, name: "Synthetic security", exchange_code: "NASDAQ", display_ticker: "SYN", security_type: "EQUITY", quote_currency: "USD", isin: null, status: "ACTIVE", verified: false, version: 1 };
  const portfolio: Schemas["Portfolio"] = { id: trade.portfolio_id, name: "Synthetic portfolio", base_currency: "HKD", cash_completeness: "UNKNOWN", data_revision: 1, active_projection_version: 0, version: 1 };
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/trades/{id}/history-review") return reply(current());
    if (path === "/api/v1/trades") return reply({ items: [trade], next_cursor: null });
    if (path === "/api/v1/accounts/{id}") return reply(account);
    if (path === "/api/v1/securities/{id}") return reply(security);
    if (path === "/api/v1/portfolios/{id}") return reply(portfolio);
    if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (path === "/api/v1/securities") return reply({ items: [security], next_cursor: null });
    if (path === "/api/v1/portfolios") return reply({ items: [portfolio], next_cursor: null });
    throw new Error(`Unexpected read: ${path}`);
  });
}
async function openReview() {
  await screen.findByText("未來草稿，尚未入帳");
  fireEvent.click(screen.getByText("未驗證交易歷史審核"));
  fireEvent.click(await screen.findByRole("button", { name: "記錄未驗證審核" }));
  return screen.findByRole("dialog", { name: "記錄未驗證審核" });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as {
    body: Schemas["TradeHistoryReviewWrite"]; params: { path: { id: string }; header: Record<string, string> };
  };
}
function fill() {
  fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: reason } });
  fireEvent.click(screen.getByLabelText(/我明白這項決定/));
}
function submit() { fireEvent.submit(screen.getByRole("form", { name: "記錄未驗證審核" })); }

describe("unverified trade-history review", () => {
  it("loads only on request, identifies the missing gap and freezes its explicit intent", async () => {
    const get = reads(); const open = vi.fn();
    renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={open} />);
    expect(get).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    expect(await screen.findByText(/找不到原始建立或修訂稽核/)).toBeVisible();
    expect(screen.getByText(/不會解除交易更正或作廢限制/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "記錄未驗證審核" }));
    expect(open).toHaveBeenCalledWith(intent);
  });
  it("requires a reason and a separate explicit acknowledgment before posting", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<TradeHistoryReviewForm intent={intent} onSaved={vi.fn()} onCancel={vi.fn()} />);
    submit(); expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: "short" } });
    fireEvent.click(screen.getByLabelText(/我明白這項決定/)); submit();
    expect(await screen.findByText(/至少 10 個字元/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it.each(["9007199254740993", "9223372036854775806"])("sends review revision %s as the original exact string", async revision => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...first, revision })); const saved = vi.fn();
    const cache = renderUI(<TradeHistoryReviewForm intent={{ ...intent, expected_revision: revision }} onSaved={saved} onCancel={vi.fn()} />);
    fill(); submit(); await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(options(post).body).toEqual({ decision: "ACKNOWLEDGE_UNVERIFIED", expected_revision: revision, expected_gap: "MISSING_CREATION_AUDIT", reason });
    expect(options(post).params.header["If-Match"]).toBe('"1"');
    expect(options(post).params.path.id).toBe(tradeId);
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain(reason);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("keeps the same intent after reauthentication failure and rotates only when the submitted reason changes", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(failure("REAUTH_REQUIRED", 403));
    renderUI(<TradeHistoryReviewForm intent={intent} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fill(); submit(); expect(await screen.findByRole("alert")).toHaveTextContent("再次輸入密碼");
    fireEvent.click(screen.getByLabelText(/我明白這項決定/)); fireEvent.click(screen.getByLabelText(/我明白這項決定/)); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(options(post, 1).body).toEqual(options(post).body);
    fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: `${reason} Another decision.` } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
  });

  it("keeps an uncertain key when edits do not change the normalized submitted reason", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(failure("REAUTH_REQUIRED", 403));
    renderUI(<TradeHistoryReviewForm intent={intent} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fill(); submit(); await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: `${reason}   ` } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 1).body).toEqual(options(post).body);
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: "A temporary unsent different reason." } });
    fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: reason } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
  });

  it("offers withdrawal for changed evidence using the original decision gap", async () => {
    const observed: Schemas["TradeHistoryReviewStatus"] = {
      ...status, trade_version: 2, trade_status: "SUPERSEDED", review_revision: "1", current_gap: null,
      state: "EVIDENCE_CHANGED", latest: first,
    };
    reads(() => observed); const open = vi.fn();
    renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={open} />);
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    expect(await screen.findByText(/稽核證據已變更/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "撤回未驗證審核" }));
    expect(open).toHaveBeenCalledWith({ ...intent, trade_version: 2, expected_revision: "1", decision: "WITHDRAW" });
  });

  it.each([
    { ...status, trade_id: "99999999-9999-4999-8999-999999999999" },
    { ...status, review_revision: "01" },
    { ...status, review_revision: "1", latest: first, state: "NOT_REVIEWED" as const },
    { ...status, review_revision: "1", latest: { ...first, trade_id: "different" }, state: "ACKNOWLEDGED_UNVERIFIED" as const },
  ])("does not offer decisions for inconsistent review metadata", async inconsistent => {
    reads(() => inconsistent); const open = vi.fn();
    renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={open} />);
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    expect(await screen.findByRole("alert")).toHaveTextContent("不符");
    expect(screen.queryByRole("button", { name: "記錄未驗證審核" })).not.toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
  });

  it.each([
    { ...status, trade_version: Number.MAX_SAFE_INTEGER + 1 },
    { ...status, review_revision: "9223372036854775807", state: "WITHDRAWN" as const,
      latest: { ...first, revision: "9223372036854775807", decision: "WITHDRAW" as const } },
  ])("does not use an unsafe or exhausted version", async unsafe => {
    reads(() => unsafe); renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={vi.fn()} />);
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    expect(await screen.findByRole("alert")).toHaveTextContent("超出可精確處理");
    expect(screen.queryByRole("button", { name: "記錄未驗證審核" })).not.toBeInTheDocument();
  });

  it("does not treat a failed read as a reviewable or verified gap", async () => {
    vi.spyOn(api, "GET").mockRejectedValue(new TypeError("offline"));
    renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={vi.fn()} />);
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByRole("button", { name: "記錄未驗證審核" })).not.toBeInTheDocument();
  });

  it("refreshes changed evidence without presenting the old decision as current", async () => {
    let current = status;
    reads(() => current); const open = vi.fn();
    renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={open} />);
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    await screen.findByText(/找不到原始建立或修訂稽核/);
    current = { ...status, review_revision: "1", state: "EVIDENCE_CHANGED", current_gap: null, latest: first };
    fireEvent.click(screen.getByRole("button", { name: "重新載入審核狀態" }));
    expect(await screen.findByText(/稽核證據已變更/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "記錄未驗證審核" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "撤回未驗證審核" }));
    expect(open).toHaveBeenCalledWith({ ...intent, expected_revision: "1", decision: "WITHDRAW" });
  });

  it("loads immutable review pages on demand without searching private reasons", async () => {
    const withdrawn = { ...first, id: "77777777-7777-4777-8777-777777777777", revision: "2", decision: "WITHDRAW" as const };
    const current: Schemas["TradeHistoryReviewStatus"] = { ...status, review_revision: "2", state: "WITHDRAWN", latest: withdrawn };
    const get = vi.spyOn(api, "GET").mockImplementation(async (path, request) => {
      if (path === "/api/v1/trades/{id}/history-review") return reply(current);
      if (path === "/api/v1/trades/{id}/history-review/revisions") {
        const query = (request as { params: { query: { cursor?: string; limit: number; q?: string } } }).params.query;
        expect(query.limit).toBe(25); expect(query.q).toBeUndefined();
        return reply(query.cursor ? { items: [first], next_cursor: null } : { items: [withdrawn], next_cursor: "next-page" });
      }
      throw new Error(`Unexpected read: ${path}`);
    });
    renderUI(<TradeHistoryReviewPanel tradeId={tradeId} disabled={false} onReview={vi.fn()} />);
    fireEvent.click(screen.getByText("未驗證交易歷史審核"));
    await screen.findByText(/先前的審核決定已撤回/); expect(get).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("查看審核決定歷史"));
    expect(await screen.findByText("版本 2：撤回")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    expect(await screen.findByText("版本 1：記錄未驗證審核")).toBeVisible();
    expect(screen.getByRole("button", { name: "下一頁" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "上一頁" }));
    expect(await screen.findByText("版本 2：撤回")).toBeVisible();
  });

  it.each([
    { ...intent, trade_version: Number.MAX_SAFE_INTEGER + 1 },
    { ...intent, expected_revision: "9223372036854775807" },
    { ...intent, expected_revision: "01" },
  ])("never posts an unsafe direct form intent", async invalid => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<TradeHistoryReviewForm intent={invalid} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fill(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("超出可精確處理");
    expect(post).not.toHaveBeenCalled(); expect(screen.getByLabelText("審核依據")).toHaveValue(reason);
  });

  it("rejects a NUL reason without sending private input", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<TradeHistoryReviewForm intent={intent} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("審核依據"), { target: { value: `${reason}\0` } });
    fireEvent.click(screen.getByLabelText(/我明白這項決定/)); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("不能包含 NUL");
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps the entered reason after a stale version or changed gap", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(failure("PRECONDITION_FAILED", 412)).mockResolvedValueOnce(failure("TRADE_REVIEW_CHANGED"));
    renderUI(<TradeHistoryReviewForm intent={intent} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fill(); submit(); expect(await screen.findByRole("alert")).toHaveTextContent("不會覆蓋新版");
    submit(); expect(await screen.findByRole("alert")).toHaveTextContent("缺口或證據已變更");
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(screen.getByLabelText("審核依據")).toHaveValue(reason);
  });
});

describe("trade workspace review", () => {
  it("keeps an uncertain original intent through changed metadata and real reauthentication", async () => {
    let current = status; let attempts = 0; let offline = false;
    pageReads(() => { if (offline) throw new TypeError("synthetic offline status"); return current; });
    const post = vi.spyOn(api, "POST").mockImplementation(async path => {
      if (path === "/api/v1/auth/reauth") return reply({ ok: true, message: "OK" });
      if (path === "/api/v1/trades/{id}/history-review") {
        attempts++; if (attempts === 1) { current = { ...status, review_revision: "1", state: "ACKNOWLEDGED_UNVERIFIED", latest: first }; throw new TypeError("synthetic lost response"); }
        return reply(first);
      }
      throw new Error(`Unexpected write: ${path}`);
    });
    const cache = renderUI(<TradesPage />); const sheet = await openReview();
    await waitFor(() => expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus());
    fill(); submit(); expect(await within(sheet).findByText(/網絡連線中斷/)).toBeVisible();
    offline = true;
    await act(async () => { await cache.invalidateQueries({ queryKey: ["ledger", "trade-history-review", tradeId] }); });
    expect(within(sheet).getByLabelText("審核依據")).toHaveValue(reason);
    fireEvent.change(within(sheet).getByLabelText("目前密碼"), { target: { value: "synthetic-review-password" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "確認目前密碼" }));
    await within(sheet).findByText(/已重新驗證/); expect(within(sheet).getByLabelText("目前密碼")).toHaveValue("");
    submit(); await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(/已記錄未驗證審核；這不是持倉或股息證明/)).toBeVisible();
    expect(options(post, 0).params.header["Idempotency-Key"]).toBe(options(post, 2).params.header["Idempotency-Key"]);
    expect(options(post, 2).body).toEqual(options(post, 0).body);
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain("synthetic-review-password");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("blocks review and sheet closure while password confirmation is pending", async () => {
    pageReads(() => status); let finish = () => {};
    const waiting = new Promise<void>(resolve => { finish = resolve; });
    const post = vi.spyOn(api, "POST").mockImplementation(async path => {
      if (path !== "/api/v1/auth/reauth") throw new Error("Unexpected write");
      await waiting; return reply({ ok: true, message: "OK" });
    });
    renderUI(<TradesPage />); const sheet = await openReview(); fill();
    fireEvent.change(within(sheet).getByLabelText("目前密碼"), { target: { value: "synthetic-review-password" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "確認目前密碼" }));
    await waitFor(() => expect(within(sheet).getByRole("form", { name: "重新驗證" })).toHaveAttribute("aria-busy", "true"));
    try {
      const form = within(sheet).getByRole("form", { name: "記錄未驗證審核" });
      expect(within(form).getByRole("button", { name: "等候密碼確認…" })).toBeDisabled();
      expect(within(form).getByRole("button", { name: "取消" })).toBeDisabled();
      fireEvent.submit(form); expect(post).toHaveBeenCalledTimes(1);
      fireEvent.click(within(sheet).getByRole("button", { name: "關閉編輯" }));
      expect(screen.getByRole("dialog", { name: "記錄未驗證審核" })).toBeInTheDocument();
      expect(within(sheet).getByText(/請等候回應後再關閉/)).toBeVisible();
      expect(within(sheet).getByLabelText("審核依據")).toHaveValue(reason);
    } finally { await act(async () => { finish(); await waiting; }); }
  });

  it("blocks password confirmation and closure during an unconfirmed review write", async () => {
    pageReads(() => status); let finish = () => {};
    const waiting = new Promise<void>(resolve => { finish = resolve; });
    const post = vi.spyOn(api, "POST").mockImplementation(async path => {
      if (path !== "/api/v1/trades/{id}/history-review") throw new Error("Unexpected write");
      await waiting; return failure("REAUTH_REQUIRED", 403);
    });
    renderUI(<TradesPage />); const sheet = await openReview(); fill(); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    try {
      expect(within(sheet).getByLabelText("目前密碼")).toBeDisabled();
      fireEvent.submit(within(sheet).getByRole("form", { name: "重新驗證" }));
      expect(post).toHaveBeenCalledTimes(1);
      fireEvent.click(within(sheet).getByRole("button", { name: "關閉編輯" }));
      expect(within(sheet).getByText(/請等候回應後再關閉/)).toBeVisible();
    } finally { await act(async () => { finish(); await waiting; }); }
    expect(await within(sheet).findByText(/再次輸入密碼/)).toBeVisible();
    expect(within(sheet).getByLabelText("審核依據")).toHaveValue(reason);
  });
});
