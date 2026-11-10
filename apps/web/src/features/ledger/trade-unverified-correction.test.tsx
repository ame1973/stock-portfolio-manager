import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { TradeForm, type Trade } from "./TradeForm";
import { UnverifiedVoidForm } from "./TradeCorrection";
import { checkedCorrection, type CorrectionScope } from "./trade-correction-validation";
import { TradesPage } from "./TradesPage";

type Schemas = components["schemas"];
const root = "66666666-6666-4666-8666-666666666666";
const tradeId = "55555555-5555-4555-8555-555555555555";
const resultId = "77777777-7777-4777-8777-777777777777";
const reason = "Explicit synthetic unverified trade correction.";
const portfolio: Schemas["Portfolio"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic portfolio", base_currency: "HKD", cash_completeness: "UNKNOWN", data_revision: 1, active_projection_version: 0, version: 1 };
const account: Schemas["Account"] = { id: "22222222-2222-4222-8222-222222222222", portfolio_id: portfolio.id, broker_id: "33333333-3333-4333-8333-333333333333", name: "Synthetic account", default_currency: "USD", opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
const security: Schemas["Security"] = { id: "44444444-4444-4444-8444-444444444444", name: "Synthetic security", exchange_code: "NASDAQ", display_ticker: "SYN", security_type: "EQUITY", quote_currency: "USD", isin: null, status: "ACTIVE", verified: false, version: 1 };
const trade: Trade = {
  id: tradeId, root_id: root, portfolio_id: portfolio.id, account_id: account.id, security_id: security.id,
  side: "BUY", trade_date: "2999-01-02", executed_at_utc: null, quantity: "10.00000000", price: "10.00000000",
  fee: "0.000000", tax: "0.000000", currency: "USD", fx_rate_hkd: null, fx_source: "MANUAL", gross: "100.000000",
  settlement: "100.000000", booked_hkd: null, rounding_delta: "0.000000", settlement_override: false,
  hkd_override: false, override_reason: "", status: "DRAFT_SCHEDULED", revision: 2, version: 1, supersedes_id: root,
  created_sequence: 1, note: "Synthetic private trade note", created_at: "2026-11-10T00:00:00Z", updated_at: "2026-11-10T00:00:00Z",
};
const scope: CorrectionScope = { trade_version: 1, reviews: [
  { trade_id: root, trade_version: 2, review_revision: "9007199254740993", gap: "MISSING_CREATION_AUDIT" },
  { trade_id: tradeId, trade_version: 1, review_revision: "1", gap: "MISSING_CREATION_AUDIT" },
] };
const status: Schemas["TradeCorrectionScopeStatus"] = {
  trade_id: tradeId, root_id: root, trade_version: 1, trade_status: "DRAFT_SCHEDULED",
  scope: "UNVERIFIED_TRADE_CORRECTION_ONLY", history_completeness: "UNVERIFIED",
  gaps: scope.reviews.map(pin => ({ trade_id: pin.trade_id, trade_version: pin.trade_version, review_revision: pin.review_revision,
    current_gap: "MISSING_CREATION_AUDIT", state: "ACKNOWLEDGED_UNVERIFIED" })),
};
const voided: Schemas["TradeUnverifiedCorrectionRecord"] = {
  authorization_id: "88888888-8888-4888-8888-888888888888", scope: "UNVERIFIED_TRADE_CORRECTION_ONLY",
  history_completeness: "UNVERIFIED", trade: { ...trade, status: "VOID", version: 2 },
};
const revised: Schemas["TradeUnverifiedCorrectionRecord"] = {
  authorization_id: "99999999-9999-4999-8999-999999999999", scope: "UNVERIFIED_TRADE_CORRECTION_ONLY",
  history_completeness: "UNVERIFIED", trade: { ...trade, id: resultId, supersedes_id: tradeId, revision: 3, quantity: "11.00000001" },
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(current: () => Schemas["TradeCorrectionScopeStatus"] = () => status) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/trades/{id}/unverified-corrections/scope") return reply(current());
    if (path === "/api/v1/trades") return reply({ items: [trade], next_cursor: null });
    if (path === "/api/v1/securities/{id}") return reply(security);
    if (path === "/api/v1/accounts/{id}") return reply(account);
    if (path === "/api/v1/portfolios/{id}") return reply(portfolio);
    if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (path === "/api/v1/securities") return reply({ items: [security], next_cursor: null });
    if (path === "/api/v1/portfolios") return reply({ items: [portfolio], next_cursor: null });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Record<string, unknown>; params: { path: { id: string }; header: Record<string, string> } };
}
function voidInput(value = reason) {
  fireEvent.change(screen.getByLabelText("未驗證作廢依據"), { target: { value } });
  fireEvent.click(screen.getByLabelText(/我明白這次作廢使用未驗證/));
}
function voidSubmit() { fireEvent.submit(screen.getByRole("form", { name: "未驗證作廢交易" })); }
async function revisionInput(value = reason) {
  await screen.findByText(/報價及結算幣別：USD/);
  fireEvent.change(screen.getByLabelText("交易股數"), { target: { value: "0011.00000001" } });
  fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value } });
  fireEvent.click(screen.getByLabelText(/我明白這次更正使用未驗證/));
}
function revisionSubmit() { fireEvent.submit(screen.getByRole("form", { name: "未驗證更正交易" })); }
async function openEditor(action: "未驗證作廢交易" | "未驗證更正交易") {
  await screen.findByText("未來草稿，尚未入帳");
  fireEvent.click(screen.getByText("未驗證更正範圍"));
  fireEvent.click(await screen.findByRole("button", { name: action }));
  return screen.findByRole("dialog", { name: action });
}

describe("explicit unverified trade correction", () => {
  it("requires a specific reason and separate confirmation before voiding", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<UnverifiedVoidForm record={trade} scope={scope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    voidSubmit(); expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("未驗證作廢依據"), { target: { value: "short" } });
    fireEvent.click(screen.getByLabelText(/我明白這次作廢使用未驗證/)); voidSubmit();
    expect(await screen.findByText(/至少 10 個字元/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("keeps exact pins, private input and one key across uncertain void retries", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("private-untrusted"));
    const cache = renderUI(<UnverifiedVoidForm record={trade} scope={scope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    voidInput(); voidSubmit(); expect(await screen.findByRole("alert")).toHaveTextContent("網絡連線中斷");
    fireEvent.change(screen.getByLabelText("未驗證作廢依據"), { target: { value: `${reason}   ` } }); voidSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(options(post).params.header["If-Match"]).toBe('"1"');
    expect(options(post).params.path.id).toBe(tradeId);
    expect(options(post).body).toEqual({ confirmation: "UNVERIFIED_TRADE_CORRECTION_ONLY", reviews: scope.reviews, reason });
    expect(document.body.textContent).not.toContain("private-untrusted");
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain(reason);
    expect(localStorage.length + sessionStorage.length).toBe(0);
    fireEvent.change(screen.getByLabelText("未驗證作廢依據"), { target: { value: `${reason} Different.` } }); voidSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
  });

  it("keeps the exact original void intent after reauthentication is required", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValueOnce({ error: { code: "REAUTH_REQUIRED", message: "private-untrusted", request_id: "test", details: {} }, response: new Response(null, { status: 403 }) }).mockResolvedValueOnce(reply(voided));
    const saved = vi.fn();
    renderUI(<UnverifiedVoidForm record={trade} scope={scope} onSaved={saved} onCancel={vi.fn()} />);
    voidInput(); voidSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/再次輸入密碼/);
    voidSubmit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(voided.trade));
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(options(post, 1).body).toEqual(options(post).body);
  });

  it("shows changed scope without leaking server text or clearing the submitted inputs", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { code: "TRADE_REVIEW_SCOPE_CHANGED", message: "private-untrusted", request_id: "test", details: {} }, response: new Response(null, { status: 409 }) });
    renderUI(<UnverifiedVoidForm record={trade} scope={scope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    voidInput(); voidSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/交易缺口範圍已變更/);
    expect(screen.getByLabelText("未驗證作廢依據")).toHaveValue(reason);
    expect(document.body.textContent).not.toContain("private-untrusted");
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("keeps the original key when a successful response cannot be validated", async () => {
    reads(); const bad = { ...voided, history_completeness: undefined };
    const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply(bad)).mockResolvedValueOnce(reply(voided));
    const saved = vi.fn();
    renderUI(<UnverifiedVoidForm record={trade} scope={scope} onSaved={saved} onCancel={vi.fn()} />);
    voidInput(); voidSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/回應無法安全核對/);
    expect(saved).not.toHaveBeenCalled();
    voidSubmit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(voided.trade));
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(options(post, 1).body).toEqual(options(post).body);
  });

  it("rejects NUL reasons without sending a correction", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<UnverifiedVoidForm record={trade} scope={scope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    voidInput("Synthetic private reason\0invalid"); voidSubmit();
    expect(await screen.findByText(/不能包含 NUL/)).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });

  it("requires explicit confirmation and submits only exact strings for an unverified revision", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(revised)); const saved = vi.fn();
    const cache = renderUI(<TradeForm record={trade} unverifiedScope={scope} onSaved={saved} onCancel={vi.fn()} />);
    await screen.findByText(/報價及結算幣別：USD/);
    fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value: reason } }); revisionSubmit();
    expect(await screen.findByText(/請先確認這次未驗證更正/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("交易股數"), { target: { value: "0011.00000001" } });
    fireEvent.click(screen.getByLabelText(/我明白這次更正使用未驗證/)); revisionSubmit();
    await waitFor(() => expect(saved).toHaveBeenCalledWith(revised.trade));
    expect(options(post).params.path.id).toBe(tradeId);
    expect(options(post).params.header["If-Match"]).toBe('"1"');
    expect(options(post).body).toMatchObject({ confirmation: "UNVERIFIED_TRADE_CORRECTION_ONLY", reviews: scope.reviews,
      revision: { quantity: "0011.00000001", price: "10.00000000", currency: "USD", fx_rate_hkd: null, reason } });
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain(reason);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("rejects a NUL revision reason and a stale frozen scope before posting", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<TradeForm record={trade} unverifiedScope={{ ...scope, trade_version: 2 }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    await revisionInput("Synthetic private revision\0invalid"); revisionSubmit();
    expect(await screen.findByText(/不能包含 NUL/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value: reason } }); revisionSubmit();
    expect(await screen.findByText(/固定的交易或審核範圍不符/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("does not duplicate the stored manual FX provenance prefix on revision", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(revised));
    renderUI(<TradeForm record={{ ...trade, fx_source: "MANUAL:Synthetic broker statement" }} unverifiedScope={scope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    await revisionInput(); revisionSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(options(post).body).toMatchObject({ revision: { fx_source: "Synthetic broker statement" } });
  });

  it("preserves an unchanged revision key after lost response and rotates it only after a changed intent", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("private-untrusted"));
    renderUI(<TradeForm record={trade} unverifiedScope={scope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    await revisionInput(); revisionSubmit(); expect(await screen.findByRole("alert")).toHaveTextContent("網絡連線中斷");
    fireEvent.change(screen.getByLabelText("交易更正依據"), { target: { value: `${reason}   ` } }); revisionSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post).params.header["Idempotency-Key"]);
    expect(options(post, 1).body).toEqual(options(post).body);
    fireEvent.change(screen.getByLabelText("交易股數"), { target: { value: "12" } }); revisionSubmit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
    expect(screen.getByLabelText("交易股數")).toHaveValue("12");
  });

  it.each([
    ["missing", { ...scope, reviews: [] }],
    ["duplicate", { ...scope, reviews: [scope.reviews[0], scope.reviews[0]] }],
    ["zero ordinal", { ...scope, reviews: [{ ...scope.reviews[0], review_revision: "0" }] }],
    ["rounded ordinal", { ...scope, reviews: [{ ...scope.reviews[0], review_revision: 9007199254740992 }] }],
    ["wrong trade version", { ...scope, trade_version: 2 }],
    ["wrong gap", { ...scope, reviews: [{ ...scope.reviews[0], gap: "MISSING_RETIRED_ORIGIN" }] }],
  ])("refuses an invalid frozen scope: %s", async (_label, bad) => {
    reads(); const post = vi.spyOn(api, "POST");
    renderUI(<UnverifiedVoidForm record={trade} scope={bad as CorrectionScope} onSaved={vi.fn()} onCancel={vi.fn()} />);
    voidInput(); voidSubmit();
    expect(await screen.findByRole("alert")).toHaveTextContent(/範圍不符/); expect(post).not.toHaveBeenCalled();
  });

  it.each([
    ["missing label", { ...voided, history_completeness: undefined }],
    ["wrong scope", { ...voided, scope: "VERIFIED" }],
    ["wrong root", { ...voided, trade: { ...voided.trade, root_id: tradeId } }],
    ["wrong target", { ...voided, trade: { ...voided.trade, id: resultId } }],
    ["wrong operation", { ...voided, trade: { ...voided.trade, status: "ACTIVE" } }],
    ["financial number", { ...voided, trade: { ...voided.trade, quantity: 10 } }],
  ])("does not treat an inconsistent cached response as success: %s", (_label, value) => {
    expect(() => checkedCorrection(value as Schemas["TradeUnverifiedCorrectionRecord"], trade, "VOID")).toThrow(/無法安全核對|與原交易或操作不符/);
  });

  it.each([
    ["wrong target", { ...revised, trade: { ...revised.trade, supersedes_id: root } }],
    ["wrong root", { ...revised, trade: { ...revised.trade, root_id: tradeId } }],
    ["same physical record", { ...revised, trade: { ...revised.trade, id: tradeId } }],
    ["retired result", { ...revised, trade: { ...revised.trade, status: "VOID" } }],
    ["rounded amount", { ...revised, trade: { ...revised.trade, fee: 0 } }],
    ["missing scope", { ...revised, scope: undefined }],
  ])("rejects an inconsistent revision response: %s", (_label, value) => {
    expect(() => checkedCorrection(value as Schemas["TradeUnverifiedCorrectionRecord"], trade, "REVISE")).toThrow(/無法安全核對|與原交易或操作不符/);
  });

  it("blocks both the form and close actions while password confirmation is pending", async () => {
    reads(); let release!: (value: ReturnType<typeof reply>) => void;
    const held = new Promise<ReturnType<typeof reply>>(resolve => { release = resolve; });
    vi.spyOn(api, "POST").mockImplementation(async path => path === "/api/v1/auth/reauth" ? held : reply(voided));
    renderUI(<TradesPage />); const sheet = await openEditor("未驗證作廢交易");
    expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    expect(within(sheet).getByText(/9007199254740993/)).toBeVisible();
    fireEvent.change(within(sheet).getByLabelText("目前密碼"), { target: { value: "synthetic-private-password" } });
    fireEvent.submit(within(sheet).getByRole("form", { name: "重新驗證" }));
    await waitFor(() => expect(within(sheet).getByLabelText("未驗證作廢依據")).toBeDisabled());
    fireEvent.keyDown(document, { key: "Escape" }); fireEvent.click(within(sheet).getByRole("button", { name: "關閉編輯" }));
    expect(sheet).toBeVisible();
    await act(async () => release(reply({ ok: true, message: "OK" })));
    expect(await within(sheet).findByText(/已重新驗證/)).toBeVisible();
    expect(within(sheet).getByLabelText("目前密碼")).toHaveValue("");
  });

  it("freezes the observed scope and guards a pending void until the original response arrives", async () => {
    let current = status;
    reads(() => current);
    let release!: (value: ReturnType<typeof reply>) => void;
    const held = new Promise<ReturnType<typeof reply>>(resolve => { release = resolve; });
    const post = vi.spyOn(api, "POST").mockImplementation(async path => path === "/api/v1/trades/{id}/unverified-corrections/void" ? held : reply({ ok: true }));
    const cache = renderUI(<TradesPage />); const sheet = await openEditor("未驗證作廢交易");
    current = { ...status, trade_version: 2, gaps: [{ ...status.gaps[0]!, review_revision: "4", state: "WITHDRAWN" }] };
    voidInput(); voidSubmit();
    await waitFor(() => expect(within(sheet).getByLabelText("目前密碼")).toBeDisabled());
    fireEvent.keyDown(document, { key: "Escape" }); fireEvent.click(within(sheet).getByRole("button", { name: "關閉編輯" }));
    expect(sheet).toBeVisible();
    expect(options(post).body).toEqual({ confirmation: "UNVERIFIED_TRADE_CORRECTION_ONLY", reviews: scope.reviews, reason });
    expect(options(post).params.header["If-Match"]).toBe('"1"');
    await act(async () => release(reply(voided)));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "未驗證作廢交易" })).not.toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent(/不是持倉或股息資格證明/);
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});
