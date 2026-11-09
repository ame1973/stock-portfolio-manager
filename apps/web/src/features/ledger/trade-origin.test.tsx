import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { TradeOriginPanel, TradeOriginRecoveryForm } from "./TradeOrigin";
import { TradesPage } from "./TradesPage";

type Schemas = components["schemas"];
const original: Schemas["TradeRecord"] = {
  id: "55555555-5555-4555-8555-555555555555", root_id: "55555555-5555-4555-8555-555555555555",
  portfolio_id: "11111111-1111-4111-8111-111111111111", account_id: "22222222-2222-4222-8222-222222222222",
  security_id: "44444444-4444-4444-8444-444444444444", side: "BUY", trade_date: "2026-01-02", executed_at_utc: null,
  quantity: "10.00000000", price: "100.00000000", fee: "1.000000", tax: "0.000000", currency: "USD",
  fx_rate_hkd: "7.800000000000", fx_source: "MANUAL", gross: "1000.000000", settlement: "1001.000000",
  booked_hkd: "7807.800000", rounding_delta: "0.000000", settlement_override: false, hkd_override: false,
  override_reason: "", status: "ACTIVE", revision: 1, version: 1, supersedes_id: null, created_sequence: 1,
  note: "Synthetic historical evidence", created_at: "2026-01-02T00:00:00.000000Z", updated_at: "2026-01-02T00:00:00.000000Z",
};
const raw = JSON.stringify(original).replace('"created_sequence":1', '"created_sequence":9223372036854775807');
const reason = "Explicit synthetic retained response review.";
const missing: Schemas["TradeAuditOriginRecord"] = {
  trade_id: original.id, trade_version: 2, trade_status: "SUPERSEDED", origin: "MISSING", captured_at: null,
  scope: "DATABASE_AUDIT_ORIGIN_ONLY", history_completeness: "UNVERIFIED",
};
const retained: Schemas["TradeAuditOriginRecord"] = { ...missing, origin: "RETAINED_RESPONSE", captured_at: "2026-11-10T00:00:00.000000Z" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const failure = (code: string, status = 409) => ({ error: { code, message: "private-untrusted", request_id: "test", details: {} }, response: new Response(null, { status }) });
function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(origin: Schemas["TradeAuditOriginRecord"] = missing) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/trades/{id}/audit-origin") return reply(origin);
    throw new Error(`Unexpected read: ${path}`);
  });
}
function pageReads(current: () => Schemas["TradeAuditOriginRecord"], sequence = 1) {
  const account: Schemas["Account"] = { id: original.account_id, portfolio_id: original.portfolio_id, broker_id: "33333333-3333-4333-8333-333333333333", name: "Synthetic account", default_currency: "USD", opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
  const security: Schemas["Security"] = { id: original.security_id, name: "Synthetic security", exchange_code: "NASDAQ", display_ticker: "SYN", security_type: "EQUITY", quote_currency: "USD", isin: null, status: "ACTIVE", verified: false, version: 1 };
  const portfolio: Schemas["Portfolio"] = { id: original.portfolio_id, name: "Synthetic portfolio", base_currency: "HKD", cash_completeness: "UNKNOWN", data_revision: 1, active_projection_version: 0, version: 1 };
  return vi.spyOn(api, "GET").mockImplementation(async (path, request) => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/trades/{id}/audit-origin") return reply(current());
    if (path === "/api/v1/accounts/{id}") return reply(account);
    if (path === "/api/v1/securities/{id}") return reply(security);
    if (path === "/api/v1/portfolios/{id}") return reply(portfolio);
    if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (path === "/api/v1/securities") return reply({ items: [security], next_cursor: null });
    if (path === "/api/v1/portfolios") return reply({ items: [portfolio], next_cursor: null });
    if (path === "/api/v1/trades") {
      const query = (request as { params: { query: { include_history: boolean } } }).params.query;
      return reply({ items: query.include_history ? [{ ...original, created_sequence: sequence, status: "SUPERSEDED", version: 2 }] : [], next_cursor: null });
    }
    throw new Error(`Unexpected read: ${path}`);
  });
}
async function openRecovery() {
  fireEvent.click(screen.getByText("篩選交易")); fireEvent.click(screen.getByLabelText("包括已被取代的修訂"));
  await screen.findByText("已有新修訂"); fireEvent.click(screen.getByText("交易稽核來源"));
  fireEvent.click(await screen.findByRole("button", { name: "恢復交易稽核來源" }));
  return screen.findByRole("dialog", { name: "恢復交易稽核來源" });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as {
    body: Schemas["TradeAuditRecovery"]; bodySerializer: (body: Schemas["TradeAuditRecovery"]) => string;
    params: { path: { id: string }; header: Record<string, string> };
  };
}
function fill(response = raw) {
  fireEvent.change(screen.getByLabelText("原始交易回應 JSON"), { target: { value: response } });
  fireEvent.change(screen.getByLabelText("恢復依據"), { target: { value: reason } });
  fireEvent.click(screen.getByLabelText(/我明白比對資料庫雜湊/));
}
function submit() { fireEvent.submit(screen.getByRole("form", { name: "恢復交易稽核來源" })); }

describe("trade audit origin read", () => {
  it("loads only when requested and offers recovery for a missing retired origin", async () => {
    const get = reads(); const open = vi.fn(); renderUI(<TradeOriginPanel tradeId={original.id} disabled={false} onRecover={open} />);
    expect(get).not.toHaveBeenCalled(); fireEvent.click(screen.getByText("交易稽核來源"));
    expect(await screen.findByText(/尚未留存稽核來源/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "恢復交易稽核來源" }));
    expect(open).toHaveBeenCalledWith(missing); expect(screen.getByText(/不是外部所有權證明/)).toBeVisible();
  });
  it("never offers retired-response recovery for a current or already retained origin", async () => {
    reads({ ...missing, trade_version: 1, trade_status: "ACTIVE" });
    renderUI(<TradeOriginPanel tradeId={original.id} disabled={false} onRecover={vi.fn()} />);
    fireEvent.click(screen.getByText("交易稽核來源")); await screen.findByText(/尚未留存稽核來源/);
    expect(screen.queryByRole("button", { name: "恢復交易稽核來源" })).not.toBeInTheDocument();
  });
  it("does not offer recovery for metadata naming a different trade", async () => {
    reads({ ...missing, trade_id: "99999999-9999-4999-8999-999999999999" });
    const open = vi.fn(); renderUI(<TradeOriginPanel tradeId={original.id} disabled={false} onRecover={open} />);
    fireEvent.click(screen.getByText("交易稽核來源"));
    expect(await screen.findByText(/稽核來源的交易識別碼不符/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "恢復交易稽核來源" })).not.toBeInTheDocument(); expect(open).not.toHaveBeenCalled();
  });
  it("does not use an imprecise physical version for recovery", async () => {
    reads({ ...missing, trade_version: Number.MAX_SAFE_INTEGER + 1 });
    renderUI(<TradeOriginPanel tradeId={original.id} disabled={false} onRecover={vi.fn()} />);
    fireEvent.click(screen.getByText("交易稽核來源"));
    expect(await screen.findByText(/交易版本超出瀏覽器精確整數範圍/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "恢復交易稽核來源" })).not.toBeInTheDocument();
  });
  it("does not turn a failed read into a missing or verified origin", async () => {
    vi.spyOn(api, "GET").mockRejectedValue(new TypeError("offline"));
    renderUI(<TradeOriginPanel tradeId={original.id} disabled={false} onRecover={vi.fn()} />);
    fireEvent.click(screen.getByText("交易稽核來源"));
    expect(await screen.findByText(/無法核對稽核來源/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "恢復交易稽核來源" })).not.toBeInTheDocument();
  });
});

describe("trade workspace recovery", () => {
  it("does not display a rounded ledger sequence as the exact original", async () => {
    pageReads(() => missing, Number.MAX_SAFE_INTEGER + 1); renderUI(<TradesPage />);
    fireEvent.click(screen.getByText("篩選交易")); fireEvent.click(screen.getByLabelText("包括已被取代的修訂"));
    await screen.findByText("已有新修訂"); fireEvent.click(screen.getByText("交易原值與修訂依據"));
    expect(screen.getByText(/超過瀏覽器精確整數範圍；請以原始伺服器 JSON 為準/)).toBeVisible();
  });
  it("keeps the original retry through metadata changes and password reauthentication", async () => {
    let current = missing; let attempts = 0;
    pageReads(() => current);
    const post = vi.spyOn(api, "POST").mockImplementation(async path => {
      if (path === "/api/v1/auth/reauth") return reply({ ok: true, message: "OK" });
      if (path === "/api/v1/trades/{id}/audit-origin/recover") {
        attempts++; if (attempts === 1) { current = retained; throw new TypeError("synthetic lost response"); }
        return reply(retained);
      }
      throw new Error(`Unexpected write: ${path}`);
    });
    const cache = renderUI(<TradesPage />);
    const sheet = await openRecovery();
    await waitFor(() => expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus());
    fill(); submit(); expect(await within(sheet).findByText(/網絡連線中斷/)).toBeVisible();
    await act(async () => { await cache.invalidateQueries({ queryKey: ["ledger", "trade-audit-origin", original.id] }); });
    expect(within(sheet).getByLabelText("原始交易回應 JSON")).toHaveValue(raw);
    fireEvent.change(within(sheet).getByLabelText("目前密碼"), { target: { value: "synthetic-confirmation-password" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "確認目前密碼" }));
    await within(sheet).findByText(/已重新驗證/); expect(within(sheet).getByLabelText("目前密碼")).toHaveValue("");
    submit(); await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(/已留存與資料庫稽核相符的交易來源/)).toBeVisible();
    expect(options(post, 0).params.header["Idempotency-Key"]).toBe(options(post, 2).params.header["Idempotency-Key"]);
    expect(options(post, 2).bodySerializer(options(post, 2).body)).toContain('"created_sequence":9223372036854775807');
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain(reason);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain("synthetic-confirmation-password");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("prevents recovery and sheet closure while password confirmation is pending", async () => {
    pageReads(() => missing);
    let finish = () => {};
    const waiting = new Promise<void>(resolve => { finish = resolve; });
    vi.spyOn(api, "POST").mockImplementation(async path => {
      if (path !== "/api/v1/auth/reauth") throw new Error("Unexpected write");
      await waiting; return reply({ ok: true, message: "OK" });
    });
    renderUI(<TradesPage />); const sheet = await openRecovery(); fill();
    fireEvent.change(within(sheet).getByLabelText("目前密碼"), { target: { value: "synthetic-confirmation-password" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "確認目前密碼" }));
    await waitFor(() => expect(within(sheet).getByRole("form", { name: "重新驗證" })).toHaveAttribute("aria-busy", "true"));
    try {
      const form = within(sheet).getByRole("form", { name: "恢復交易稽核來源" });
      expect(within(form).getByRole("button", { name: "取消" })).toBeDisabled();
      expect(within(form).getByRole("button", { name: "等候密碼確認…" })).toBeDisabled();
      fireEvent.submit(form);
      fireEvent.click(within(sheet).getByRole("button", { name: "關閉編輯" }));
      expect(screen.getByRole("dialog", { name: "恢復交易稽核來源" })).toBeInTheDocument();
      expect(screen.getByText(/請等候回應後再關閉/)).toBeVisible();
      expect(within(sheet).getByLabelText("原始交易回應 JSON")).toHaveValue(raw);
    } finally { await act(async () => { finish(); await waiting; }); }
  });
  it("prevents password confirmation while recovery is pending and preserves a failed attempt", async () => {
    pageReads(() => missing);
    let finish = () => {};
    const waiting = new Promise<void>(resolve => { finish = resolve; });
    const post = vi.spyOn(api, "POST").mockImplementation(async path => {
      if (path !== "/api/v1/trades/{id}/audit-origin/recover") throw new Error("Unexpected write");
      await waiting; return failure("REAUTH_REQUIRED", 403);
    });
    renderUI(<TradesPage />); const sheet = await openRecovery(); fill(); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    try {
      expect(within(sheet).getByLabelText("目前密碼")).toBeDisabled();
      expect(within(sheet).getByRole("button", { name: "確認目前密碼" })).toBeDisabled();
      fireEvent.submit(within(sheet).getByRole("form", { name: "重新驗證" }));
      expect(post).toHaveBeenCalledTimes(1);
      fireEvent.click(within(sheet).getByRole("button", { name: "關閉編輯" }));
      expect(within(sheet).getByText(/請等候回應後再關閉/)).toBeVisible();
    } finally { await act(async () => { finish(); await waiting; }); }
    expect(await within(sheet).findByText(/再次輸入密碼/)).toBeVisible();
    await waitFor(() => expect(within(sheet).getByLabelText("目前密碼")).toBeEnabled());
    expect(within(sheet).getByLabelText("原始交易回應 JSON")).toHaveValue(raw);
  });
});

describe("retained trade response recovery", () => {
  it("requires an original JSON object, matching identity, reason and explicit acknowledgement", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderUI(<TradeOriginRecoveryForm origin={missing} onSaved={vi.fn()} onCancel={vi.fn()} />);
    submit(); await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
    fill('{"id":"different"}'); submit(); expect(await screen.findByText(/識別碼不符/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("原始交易回應 JSON"), { target: { value: "[1,2]" } }); submit();
    expect(await screen.findByText(/完整 JSON 物件/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it.each(["9007199254740993", "9223372036854775807"])("sends the original integer %s on the wire without financial input caches", async sequence => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(retained)); const saved = vi.fn();
    const cache = renderUI(<TradeOriginRecoveryForm origin={missing} onSaved={saved} onCancel={vi.fn()} />);
    const input = raw.replace("9223372036854775807", sequence);
    fill(input); submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(retained));
    const request = options(post); const wire = request.bodySerializer(request.body);
    expect(request.params.path).toEqual({ id: original.id }); expect(request.params.header["If-Match"]).toBe('"2"');
    expect(wire).toContain(`"created_sequence":${sequence}`);
    expect(wire).toContain('"quantity":"10.00000000"'); expect(JSON.parse(wire).reason).toBe(reason);
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("retains unchanged input and intent after failure; only submitted content changes the key", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(failure("REAUTH_REQUIRED", 403));
    renderUI(<TradeOriginRecoveryForm origin={missing} onSaved={vi.fn()} onCancel={vi.fn()} />); fill(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("再次輸入密碼");
    fireEvent.click(screen.getByLabelText(/我明白比對資料庫雜湊/)); fireEvent.click(screen.getByLabelText(/我明白比對資料庫雜湊/)); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(screen.getByLabelText("原始交易回應 JSON")).toHaveValue(raw);
    fireEvent.change(screen.getByLabelText("恢復依據"), { target: { value: `${reason} Another decision.` } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
  });
  it("never sends a rounded physical version even if supplied directly to the form", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<TradeOriginRecoveryForm origin={{ ...missing, trade_version: Number.MAX_SAFE_INTEGER + 1 }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fill(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("交易版本超出瀏覽器精確整數範圍");
    expect(post).not.toHaveBeenCalled(); expect(screen.getByLabelText("原始交易回應 JSON")).toHaveValue(raw);
  });
  it("keeps the original input and observed version on a stale conflict", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(failure("PRECONDITION_FAILED", 412)); const saved = vi.fn();
    renderUI(<TradeOriginRecoveryForm origin={missing} onSaved={saved} onCancel={vi.fn()} />); fill(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("不會覆蓋新版");
    expect(screen.getByLabelText("原始交易回應 JSON")).toHaveValue(raw); expect(options(post).params.header["If-Match"]).toBe('"2"'); expect(saved).not.toHaveBeenCalled();
  });
});
