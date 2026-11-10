import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { TradeCorrectionScopePanel } from "./TradeCorrectionScope";
import type { Trade } from "./TradeForm";

type Schemas = components["schemas"];
const id = "55555555-5555-4555-8555-555555555555";
const root = "66666666-6666-4666-8666-666666666666";
const trade: Trade = {
  id, root_id: root, portfolio_id: "11111111-1111-4111-8111-111111111111", account_id: "22222222-2222-4222-8222-222222222222",
  security_id: "44444444-4444-4444-8444-444444444444", side: "BUY", trade_date: "2999-01-02", executed_at_utc: null,
  quantity: "10.00000000", price: "10.00000000", fee: "0.000000", tax: "0.000000", currency: "USD", fx_rate_hkd: null,
  fx_source: "MANUAL", gross: "100.000000", settlement: "100.000000", booked_hkd: null, rounding_delta: "0.000000",
  settlement_override: false, hkd_override: false, override_reason: "", status: "DRAFT_SCHEDULED", revision: 2,
  version: 1, supersedes_id: root, created_sequence: 1, note: "", created_at: "2026-11-10T00:00:00Z", updated_at: "2026-11-10T00:00:00Z",
};
const scope: Schemas["TradeCorrectionScopeStatus"] = {
  trade_id: id, root_id: root, trade_version: 1, trade_status: "DRAFT_SCHEDULED",
  scope: "UNVERIFIED_TRADE_CORRECTION_ONLY", history_completeness: "UNVERIFIED",
  gaps: [
    { trade_id: root, trade_version: 2, review_revision: "9007199254740993", current_gap: "MISSING_CREATION_AUDIT", state: "ACKNOWLEDGED_UNVERIFIED" },
    { trade_id: id, trade_version: 1, review_revision: "1", current_gap: "MISSING_CREATION_AUDIT", state: "ACKNOWLEDGED_UNVERIFIED" },
  ],
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const rejected = (code: string) => ({ error: { code, message: "private-untrusted", request_id: "test", details: {} }, response: new Response(null, { status: 503 }) });
function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const view = render(<QueryClientProvider client={cache}>{ui}</QueryClientProvider>);
  return { ...view, cache };
}
function reads(value: unknown = scope) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/trades/{id}/unverified-corrections/scope") return reply(value);
    throw new Error(`Unexpected read: ${path}`);
  });
}
function open() { fireEvent.click(screen.getByText("未驗證更正範圍")); }

describe("unverified correction scope", () => {
  it("loads only on demand and freezes every exact review pin for a selected operation", async () => {
    const get = reads(); const select = vi.fn();
    const { cache } = renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={select} />);
    expect(get).not.toHaveBeenCalled(); open();
    await screen.findByText(/9007199254740993/);
    expect(screen.getByText(/不是所有權或股息資格證明/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "未驗證更正交易" }));
    expect(select).toHaveBeenCalledWith("REVISE", {
      trade_version: 1,
      reviews: [
        { trade_id: root, trade_version: 2, review_revision: "9007199254740993", gap: "MISSING_CREATION_AUDIT" },
        { trade_id: id, trade_version: 1, review_revision: "1", gap: "MISSING_CREATION_AUDIT" },
      ],
    });
    fireEvent.click(screen.getByRole("button", { name: "未驗證作廢交易" }));
    expect(select.mock.calls[1]?.[0]).toBe("VOID");
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each(["9007199254740993", "9223372036854775807"])("retains the exact INT64 review revision %s", async revision => {
    const selected = vi.fn();
    reads({ ...scope, gaps: [{ ...scope.gaps[0], review_revision: revision }] });
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={selected} />); open();
    await screen.findByText(new RegExp(revision));
    fireEvent.click(screen.getByRole("button", { name: "未驗證作廢交易" }));
    expect(selected.mock.calls[0]?.[1].reviews[0].review_revision).toBe(revision);
  });

  it.each(["NOT_REVIEWED", "WITHDRAWN", "EVIDENCE_CHANGED"] as const)("does not offer a change while scope is %s", async state => {
    const gap = { ...scope.gaps[0]!, state, review_revision: state === "NOT_REVIEWED" ? "0" : "2", current_gap: state === "EVIDENCE_CHANGED" ? null : "MISSING_CREATION_AUDIT" };
    reads({ ...scope, gaps: [gap, scope.gaps[1]] });
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={vi.fn()} />); open();
    await screen.findByText(state === "EVIDENCE_CHANGED" ? /稽核證據已變更/ : state === "WITHDRAWN" ? /先前的審核已撤回/ : /尚未記錄審核/);
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "未驗證作廢交易" })).not.toBeInTheDocument();
  });

  it("shows an empty scope without implying verified ownership", async () => {
    reads({ ...scope, gaps: [] });
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={vi.fn()} />); open();
    expect(await screen.findByText(/目前沒有需要此流程處理的建立稽核缺口/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
  });

  it.each([
    ["wrong target", { ...scope, trade_id: root }],
    ["wrong root", { ...scope, root_id: id }],
    ["wrong physical version", { ...scope, trade_version: 2 }],
    ["wrong status", { ...scope, trade_status: "ACTIVE" }],
    ["false scope", { ...scope, scope: "VERIFIED" }],
    ["false completeness", { ...scope, history_completeness: "VERIFIED" }],
    ["missing label", { ...scope, history_completeness: undefined }],
    ["duplicate trade", { ...scope, gaps: [scope.gaps[0], scope.gaps[0]] }],
    ["noncanonical ordinal", { ...scope, gaps: [{ ...scope.gaps[0], review_revision: "01" }] }],
    ["overflow ordinal", { ...scope, gaps: [{ ...scope.gaps[0], review_revision: "9223372036854775808" }] }],
    ["number ordinal", { ...scope, gaps: [{ ...scope.gaps[0], review_revision: 1 }] }],
    ["false acknowledgment", { ...scope, gaps: [{ ...scope.gaps[0], review_revision: "0" }] }],
    ["wrong current gap", { ...scope, gaps: [{ ...scope.gaps[0], current_gap: "MISSING_RETIRED_ORIGIN" }] }],
    ["wrong ancestor version", { ...scope, gaps: [{ ...scope.gaps[0], trade_version: 1 }] }],
    ["too many gaps", { ...scope, gaps: Array(101).fill(scope.gaps[0]) }],
  ])("rejects an inconsistent scope: %s", async (_name, value) => {
    reads(value); const select = vi.fn();
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={select} />); open();
    expect(await screen.findByRole("alert")).toHaveTextContent(/無法安全核對/);
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
    expect(select).not.toHaveBeenCalled();
  });

  it("rechecks the scope when reopening instead of trusting a previously cached decision", async () => {
    let current: unknown = scope;
    const get = vi.spyOn(api, "GET").mockImplementation(async () => reply(current));
    const selected = vi.fn();
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={selected} />);
    open(); await screen.findByRole("button", { name: "未驗證更正交易" });
    open();
    current = { ...scope, gaps: [{ ...scope.gaps[0], state: "WITHDRAWN", review_revision: "2" }] };
    open();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/先前的審核已撤回/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
    expect(selected).not.toHaveBeenCalled();
  });

  it("disables a cached selection until the reopening refresh has completed", async () => {
    let release!: (value: ReturnType<typeof reply>) => void;
    const held = new Promise<ReturnType<typeof reply>>(resolve => { release = resolve; });
    const get = vi.spyOn(api, "GET").mockResolvedValueOnce(reply(scope)).mockImplementationOnce(async () => held);
    const selected = vi.fn();
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={selected} />);
    open(); await screen.findByRole("button", { name: "未驗證更正交易" });
    open(); open();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "未驗證更正交易" })).toBeDisabled();
    expect(selected).not.toHaveBeenCalled();
    await act(async () => release(reply({ ...scope, gaps: [{ ...scope.gaps[0], state: "WITHDRAWN", review_revision: "2" }] })));
    expect(await screen.findByText(/先前的審核已撤回/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
  });

  it("explains the server scope limit without permitting a partial correction", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(rejected("TRADE_CORRECTION_SCOPE_TOO_LARGE"));
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={vi.fn()} />); open();
    expect(await screen.findByRole("alert")).toHaveTextContent(/不能省略缺口/);
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("private-untrusted");
  });

  it("does not offer actions on a failed refresh and never shows private server text", async () => {
    let succeed = true;
    vi.spyOn(api, "GET").mockImplementation(async () => succeed ? reply(scope) : rejected("TRADE_REVIEW_UNAVAILABLE"));
    renderUI(<TradeCorrectionScopePanel record={trade} disabled={false} onSelect={vi.fn()} />); open();
    await screen.findByRole("button", { name: "未驗證更正交易" });
    succeed = false; fireEvent.click(screen.getByRole("button", { name: "重新載入更正範圍" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/無法安全核對/));
    expect(screen.queryByRole("button", { name: "未驗證更正交易" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("private-untrusted");
  });

  it("never queries retired trades or enables actions while another editor is open", async () => {
    const get = reads(); const select = vi.fn();
    const { rerender } = renderUI(<TradeCorrectionScopePanel record={{ ...trade, status: "SUPERSEDED", version: 2 }} disabled={false} onSelect={select} />);
    expect(screen.queryByText("未驗證更正範圍")).not.toBeInTheDocument(); expect(get).not.toHaveBeenCalled();
    rerender(<QueryClientProvider client={new QueryClient()}><TradeCorrectionScopePanel record={trade} disabled onSelect={select} /></QueryClientProvider>);
    open(); expect(await screen.findByRole("button", { name: "未驗證更正交易" })).toBeDisabled();
  });
});
