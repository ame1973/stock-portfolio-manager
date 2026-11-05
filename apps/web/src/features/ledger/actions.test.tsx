import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ActionForm } from "./ActionForm";
import { ActionPage } from "./ActionPage";
import type { CorporateAction } from "./data";

const security: components["schemas"]["Security"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic action security", display_ticker: "OLD", exchange_code: "NASDAQ", quote_currency: "USD", security_type: "EQUITY", status: "ACTIVE", isin: null, verified: false, version: 1 };
const action: CorporateAction = { id: "22222222-2222-4222-8222-222222222222", root_id: "22222222-2222-4222-8222-222222222222", supersedes_id: null, security_id: security.id, action_type: "SPLIT", effective_date: "2026-01-03", ratio_numerator: "3", ratio_denominator: "2", new_symbol: null, previous_symbol: null, evidence_url: null, status: "CONFIRMED", lifecycle_status: "CURRENT", is_current: true, revision: 1, version: 3, created_sequence: 1, effect_scope: "QUANTITY_TERMS", allocation_scope: "TERMS_NOT_A_VALUATION", review_required: false, reason: "Original adopted synthetic terms", note: "", created_at: "2026-01-03T00:00:00Z" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const rejected = (code: string, status = 409) => ({ error: { code, message: "Synthetic failure", request_id: "test", details: {} }, response: new Response(null, { status }) });
function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(rows: CorporateAction[] = []) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    switch (path) {
      case "/api/v1/auth/csrf": return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
      case "/api/v1/securities": return reply({ items: [security], next_cursor: null });
      case "/api/v1/securities/{id}": return reply(security);
      case "/api/v1/corporate-actions": return reply({ items: rows, next_cursor: null });
      default: throw new Error(`Unexpected read: ${path}`);
    }
  });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Record<string, unknown>; params: { path: { id: string }; header: Record<string, string> } };
}
async function fillAction() {
  await screen.findByRole("option", { name: "OLD / NASDAQ — Synthetic action security" });
  fireEvent.change(screen.getByLabelText("企業行動證券", { exact: true }), { target: { value: security.id } });
  fireEvent.change(screen.getByLabelText("企業行動生效日期"), { target: { value: "2026-01-03" } });
  fireEvent.change(screen.getByLabelText("企業行動依據"), { target: { value: "Synthetic reviewed action notice" } });
  fireEvent.change(screen.getByLabelText("比例分子"), { target: { value: "3" } });
  fireEvent.change(screen.getByLabelText("比例分母"), { target: { value: "2" } });
}

describe("corporate-action exact terms and review forms", () => {
  it("does not invent a default ratio and sends precise strings rather than Number", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(action));
    const saved = vi.fn();
    renderUI(<ActionForm onSaved={saved} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("比例分子")).toHaveValue("");
    expect(screen.getByLabelText("比例分母")).toHaveValue("");
    await fillAction();
    fireEvent.change(screen.getByLabelText("比例分子"), { target: { value: "9223372036854775807" } });
    fireEvent.change(screen.getByLabelText("比例分母"), { target: { value: "9223372036854775806" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(action));
    expect(options(post).body).toMatchObject({ ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806", status: "NEEDS_REVIEW" });
    expect(options(post).body).not.toHaveProperty("confirmed_terms");
  });

  it.each(["0", "-1", "3.0", "3e0", "9223372036854775808"])("rejects invalid rational term %s without posting", async value => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<ActionForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillAction();
    fireEvent.change(screen.getByLabelText("比例分子"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByRole("alert");
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByLabelText("比例分子")).toHaveValue(value);
  });

  it("enforces rational direction and a separate adoption acknowledgment", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(action));
    renderUI(<ActionForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillAction();
    fireEvent.change(screen.getByLabelText("比例分子"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/拆股分子必須大於分母/);
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("比例分子"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("條款狀態"), { target: { value: "CONFIRMED" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/請明確確認已核對條款/);
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("我已核對條款，理解採納不等於完整估值或現金／成本分配"));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body.status).toBe("CONFIRMED");
  });

  it.each(["CASH_IN_LIEU", "MERGER", "SPIN_OFF"] as const)("stores %s as genuine review without fake ratios, cash or allocation", async kind => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...action, action_type: kind, ratio_numerator: null, ratio_denominator: null, status: "NEEDS_REVIEW" }));
    renderUI(<ActionForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillAction();
    fireEvent.change(screen.getByLabelText("企業行動類型"), { target: { value: kind } });
    expect(screen.getByLabelText("比例分子")).toHaveValue("");
    expect(screen.getByLabelText("比例分母")).toHaveValue("");
    expect(screen.queryByRole("option", { name: "採納條款，不代表完整估值" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body).toMatchObject({ action_type: kind, status: "NEEDS_REVIEW", ratio_numerator: null, ratio_denominator: null });
    expect(options(post).body).not.toHaveProperty("cash_amount");
  });

  it("keeps symbol identity explicit, clears irrelevant ratios and preserves failed future adoption input", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValueOnce(rejected("FUTURE_SYMBOL_CHANGE", 422)).mockResolvedValueOnce(reply(action));
    const saved = vi.fn();
    renderUI(<ActionForm onSaved={saved} onCancel={vi.fn()} />);
    await fillAction();
    fireEvent.change(screen.getByLabelText("企業行動類型"), { target: { value: "SYMBOL_CHANGE" } });
    expect(screen.queryByLabelText("比例分子")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("新顯示代號"), { target: { value: "brk.b" } });
    fireEvent.change(screen.getByLabelText("企業行動生效日期"), { target: { value: "2050-01-03" } });
    fireEvent.change(screen.getByLabelText("條款狀態"), { target: { value: "CONFIRMED" } });
    fireEvent.click(screen.getByLabelText("我已核對條款，理解採納不等於完整估值或現金／成本分配"));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/未來的顯示代號只能保留待覆核/);
    expect(screen.getByLabelText("新顯示代號")).toHaveValue("brk.b");
    expect(saved).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("條款狀態"), { target: { value: "NEEDS_REVIEW" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(options(post, 1).body).toMatchObject({ new_symbol: "brk.b", ratio_numerator: null, ratio_denominator: null, status: "NEEDS_REVIEW" });
  });

  it.each(["file:///private/evidence", "https://user:password@example.test/doc", "https://example.test/ has space"])("rejects unsafe evidence reference %s without fetching it", async value => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<ActionForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillAction();
    fireEvent.change(screen.getByLabelText("證據網址（選填）"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByRole("alert");
    expect(post).not.toHaveBeenCalled();
  });

  it("freezes observed revision version, keeps full reasoned input on 412 and cannot move security", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    const saved = vi.fn();
    renderUI(<ActionForm record={action} onSaved={saved} onCancel={vi.fn()} />);
    expect(screen.queryByLabelText("企業行動證券", { exact: true })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("比例分子"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("比例分母"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("企業行動依據"), { target: { value: "Synthetic full corrected rational terms" } });
    fireEvent.click(screen.getByLabelText("我已核對條款，理解採納不等於完整估值或現金／成本分配"));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/不會覆蓋新版/);
    expect(options(patch).params.header["If-Match"]).toBe('"3"');
    expect(options(patch).body).toMatchObject({ security_id: security.id, ratio_numerator: "2", ratio_denominator: "1" });
    expect(screen.getByLabelText("比例分子")).toHaveValue("2");
    expect(saved).not.toHaveBeenCalled();
  });
});

describe("action evidence, current selection and dependencies", () => {
  it("renders full INT64 ratio digits and review scope, never a guessed 1:1 or valuation", async () => {
    reads([{ ...action, ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806", status: "NEEDS_REVIEW", effect_scope: "REVIEW_ONLY", review_required: true }]);
    renderUI(<ActionPage />);
    expect(await screen.findByText(/9223372036854775807 : 9223372036854775806/)).toBeVisible();
    expect(screen.getByText(/此穩定事件仍有未解決覆核/)).toBeVisible();
    expect(screen.getByText(/只保留覆核證據，未分配股數／成本／現金/)).toBeVisible();
  });

  it("keeps superseded/void terms read-only and exposes server history/type/date filters", async () => {
    const get = reads([{ ...action, is_current: false, lifecycle_status: "SUPERSEDED" }, { ...action, id: security.id, root_id: security.id, status: "VOID", lifecycle_status: "VOID", effect_scope: "NONE" }]);
    renderUI(<ActionPage />);
    await screen.findByText(/已被新修訂取代/);
    expect(screen.queryByRole("button", { name: "更正企業行動" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "作廢企業行動" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("包括已被取代的條款修訂"));
    fireEvent.change(screen.getByLabelText("篩選行動類型"), { target: { value: "SPLIT" } });
    fireEvent.change(screen.getByLabelText("行動生效起日"), { target: { value: "2026-01-01" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/corporate-actions", expect.objectContaining({ params: { query: expect.objectContaining({ include_history: true, action_type: "SPLIT", from_date: "2026-01-01", limit: 25 }) } })));
  });

  it("requires reasoned safe-focus void and retains dependency failures without success", async () => {
    reads([action]);
    const remove = vi.spyOn(api, "DELETE").mockResolvedValue(rejected("INSUFFICIENT_SHARES"));
    renderUI(<ActionPage />);
    fireEvent.click(await screen.findByRole("button", { name: "作廢企業行動" }));
    expect(screen.getByRole("button", { name: "確認作廢企業行動" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("企業行動作廢依據"), { target: { value: "Synthetic reviewed action dependency" } });
    fireEvent.click(screen.getByRole("button", { name: "確認作廢企業行動" }));
    const dialog = screen.getByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
    await within(dialog).findByText(/持倉不足/);
    expect(options(remove).params.header["If-Match"]).toBe('"3"');
    expect(screen.queryByText(/企業行動已作廢；/)).not.toBeInTheDocument();
  });
});
