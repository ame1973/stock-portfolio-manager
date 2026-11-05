import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { TransferForm } from "./TransferForm";
import { TransferPage } from "./TransferPage";
import type { CashTransfer, StockTransfer } from "./data";

type Schemas = components["schemas"];
const portfolio: Schemas["Portfolio"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic transfer portfolio", base_currency: "HKD", cash_completeness: "UNKNOWN", data_revision: 1, active_projection_version: 0, version: 1 };
const source: Schemas["Account"] = { id: "22222222-2222-4222-8222-222222222222", portfolio_id: portfolio.id, broker_id: "33333333-3333-4333-8333-333333333333", name: "Source account", default_currency: "USD", opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
const target: Schemas["Account"] = { ...source, id: "44444444-4444-4444-8444-444444444444", name: "Destination account" };
const security: Schemas["Security"] = { id: "55555555-5555-4555-8555-555555555555", name: "Synthetic transfer stock", exchange_code: "NASDAQ", display_ticker: "STK", security_type: "EQUITY", quote_currency: "USD", isin: null, status: "ACTIVE", verified: false, version: 1 };
const cash: CashTransfer = { id: "66666666-6666-4666-8666-666666666666", root_id: "66666666-6666-4666-8666-666666666666", supersedes_id: null, portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id, transfer_date: "2026-01-03", status: "ACTIVE", revision: 1, version: 3, note: "Original pair", created_at: "2026-01-03T00:00:00Z", source_currency: "USD", target_currency: "USD", source_amount: "120.000001", target_amount: "120.000001", fx_rate_hkd: "7.900000000000", actual_hkd: "950.000001", source_booked_hkd: "950.000001", target_booked_hkd: "950.000001", booking_delta_hkd: "0.000000", quality: "OK", reason: "Original transfer evidence", updated_at: "2026-01-03T00:00:00Z" };
const stock: StockTransfer = { id: "77777777-7777-4777-8777-777777777777", root_id: "77777777-7777-4777-8777-777777777777", supersedes_id: null, portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id, transfer_date: "2026-01-03", status: "ACTIVE", revision: 1, version: 2, note: "Original shares", created_at: "2026-01-03T00:00:00Z", security_id: security.id, quantity: "4.00000000", cost_moved_hkd: "3123.120000", cost_scope: "BOOKED_AT_CREATION", created_sequence: 7, is_current: true };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const rejected = (code: string, status = 409) => ({ error: { code, message: "Synthetic failure", request_id: "test", details: {} }, response: new Response(null, { status }) });

function renderUI(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(cashRows: CashTransfer[] = [], stockRows: StockTransfer[] = []) {
  return vi.spyOn(api, "GET").mockImplementation(async (path, request) => {
    const id = (request as { params?: { path?: { id?: string } } })?.params?.path?.id;
    switch (path) {
      case "/api/v1/auth/csrf": return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
      case "/api/v1/portfolios": return reply({ items: [portfolio], next_cursor: null });
      case "/api/v1/portfolios/{id}": return reply(portfolio);
      case "/api/v1/accounts": return reply({ items: [source, target], next_cursor: null });
      case "/api/v1/accounts/{id}": return reply(id === source.id ? source : target);
      case "/api/v1/securities": return reply({ items: [security], next_cursor: null });
      case "/api/v1/securities/{id}": return reply(security);
      case "/api/v1/cash-transfers": return reply({ items: cashRows, next_cursor: null });
      case "/api/v1/security-transfers": return reply({ items: stockRows, next_cursor: null });
      default: throw new Error(`Unexpected read: ${path}`);
    }
  });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Record<string, unknown>; params: { path?: { id: string }; header: Record<string, string> } };
}
async function fillTransfer(mode: "cash" | "security") {
  await screen.findByRole("option", { name: portfolio.name });
  fireEvent.change(screen.getByLabelText("轉倉組合", { exact: true }), { target: { value: portfolio.id } });
  await screen.findAllByRole("option", { name: "Source account (USD)" });
  fireEvent.change(screen.getByLabelText("來源帳戶", { exact: true }), { target: { value: source.id } });
  fireEvent.change(screen.getByLabelText("目標帳戶", { exact: true }), { target: { value: target.id } });
  fireEvent.change(screen.getByLabelText("轉倉生效日期"), { target: { value: "2026-01-03" } });
  if (mode === "cash") {
    fireEvent.change(screen.getByLabelText("來源實際幣別"), { target: { value: "USD" } });
    fireEvent.change(screen.getByLabelText("目標實際幣別"), { target: { value: "USD" } });
    fireEvent.change(screen.getByLabelText("來源本金"), { target: { value: "100.000001" } });
    fireEvent.change(screen.getByLabelText("目標本金"), { target: { value: "100.000001" } });
    fireEvent.change(screen.getByLabelText("轉倉依據"), { target: { value: "Synthetic reviewed transfer terms" } });
  } else {
    await screen.findByRole("option", { name: "STK / NASDAQ — Synthetic transfer stock" });
    fireEvent.change(screen.getByLabelText("轉倉證券", { exact: true }), { target: { value: security.id } });
    fireEvent.change(screen.getByLabelText("轉倉股數"), { target: { value: "4" } });
  }
}

describe("atomic transfer forms", () => {
  it("retains distinct exact wire strings for equal principal and missing USD FX", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(cash));
    const saved = vi.fn();
    renderUI(<TransferForm mode="cash" onSaved={saved} onCancel={vi.fn()} />);
    await fillTransfer("cash");
    fireEvent.change(screen.getByLabelText("來源本金"), { target: { value: "00100.000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(cash));
    expect(options(post).body).toMatchObject({ source_amount: "00100.000001", target_amount: "100.000001", fx_rate_hkd: null, actual_hkd: null });
    expect(options(post).body).not.toHaveProperty("fee");
  });

  it.each([["來源本金", "0"], ["來源本金", "-1"], ["來源本金", "100.0000001"], ["目標本金", "99"], ["轉帳歷史匯率（1 USD = HKD）", "7.8000000000001"]])("rejects invalid transfer %s %s without rounding or posting", async (label, value) => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<TransferForm mode="cash" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillTransfer("cash");
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByLabelText(label!)).toHaveValue(value);
  });

  it("allows same-account explicit conversion but never a same-wallet self-transfer", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(cash));
    renderUI(<TransferForm mode="cash" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillTransfer("cash");
    fireEvent.change(screen.getByLabelText("目標帳戶", { exact: true }), { target: { value: source.id } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText("同帳戶只允許明確不同原幣的兌換。");
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("目標實際幣別"), { target: { value: "HKD" } });
    fireEvent.change(screen.getByLabelText("目標本金"), { target: { value: "781.000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body).toMatchObject({ source_account_id: source.id, target_account_id: source.id, source_currency: "USD", target_currency: "HKD", target_amount: "781.000001", fx_rate_hkd: null });
  });

  it("requires FX for designated HKD, keeps actual overrides on note-only revision, and clears them on economic input change", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    renderUI(<TransferForm mode="cash" record={cash} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("USD 腿指定 HKD（選填）")).toHaveValue("950.000001");
    fireEvent.change(screen.getByLabelText("轉倉備註"), { target: { value: "Corrected pair note" } });
    fireEvent.change(screen.getByLabelText("轉倉依據"), { target: { value: "Preserve exact original actual booking" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/不會覆蓋新版/);
    expect(options(patch).body.actual_hkd).toBe("950.000001");
    expect(options(patch).params.header["If-Match"]).toBe('"3"');
    fireEvent.change(screen.getByLabelText("來源本金"), { target: { value: "130" } });
    expect(screen.getByLabelText("USD 腿指定 HKD（選填）")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("目標本金"), { target: { value: "130.000000" } });
    fireEvent.change(screen.getByLabelText("轉帳歷史匯率（1 USD = HKD）"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("USD 腿指定 HKD（選填）"), { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText("指定 USD 腿的 HKD 仍需明確歷史 FX。");
    expect(patch).toHaveBeenCalledTimes(1);
  });

  it("never carries HKD identity into a changed USD leg as an exchange-rate default", async () => {
    reads();
    renderUI(<TransferForm mode="cash" record={{ ...cash, source_currency: "HKD", target_currency: "HKD", fx_rate_hkd: "1.000000000000", actual_hkd: null }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.queryByLabelText("轉帳歷史匯率（1 USD = HKD）")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("來源實際幣別"), { target: { value: "USD" } });
    expect(await screen.findByLabelText("轉帳歷史匯率（1 USD = HKD）")).toHaveValue("");
  });

  it("clears both account selections when portfolio/search changes", async () => {
    reads();
    renderUI(<TransferForm mode="cash" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillTransfer("cash");
    fireEvent.change(screen.getByLabelText("搜尋轉倉組合"), { target: { value: "other" } });
    expect(screen.getByLabelText("轉倉組合", { exact: true })).toHaveValue("");
    expect(screen.getByLabelText("來源帳戶", { exact: true })).toHaveValue("");
    expect(screen.getByLabelText("目標帳戶", { exact: true })).toHaveValue("");
  });

  it("posts securities quantity only, without any manufactured cash/FX/cost field", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(stock));
    renderUI(<TransferForm mode="security" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillTransfer("security");
    fireEvent.change(screen.getByLabelText("轉倉股數"), { target: { value: "0004.00000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body).toEqual({ portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id, security_id: security.id, transfer_date: "2026-01-03", quantity: "0004.00000001", note: "" });
  });

  it.each(["0", "-1", "4.000000001"])("rejects invalid stock quantity %s", async value => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<TransferForm mode="security" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillTransfer("security");
    fireEvent.change(screen.getByLabelText("轉倉股數"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByRole("alert");
    expect(post).not.toHaveBeenCalled();
  });

  it("requires separate stock accounts and a revision reason, retaining observed version/input on 412", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    const saved = vi.fn();
    renderUI(<TransferForm mode="security" record={stock} onSaved={saved} onCancel={vi.fn()} />);
    await within(screen.getByLabelText("目標帳戶", { exact: true })).findByRole("option", { name: "Source account (USD)" });
    fireEvent.change(screen.getByLabelText("目標帳戶", { exact: true }), { target: { value: source.id } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText("股票轉倉需要不同的來源與目標帳戶。");
    expect(patch).not.toHaveBeenCalled();
    await screen.findAllByRole("option", { name: "Destination account (USD)" });
    fireEvent.change(screen.getByLabelText("目標帳戶", { exact: true }), { target: { value: target.id } });
    fireEvent.change(screen.getByLabelText("轉倉股數"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("轉倉依據"), { target: { value: "Synthetic corrected custodian evidence" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/不會覆蓋新版/);
    expect(options(patch).params.header["If-Match"]).toBe('"2"');
    expect(screen.getByLabelText("轉倉股數")).toHaveValue("5");
    expect(saved).not.toHaveBeenCalled();
  });
});

describe("transfer evidence and dependency controls", () => {
  it("shows missing FX/delta as unavailable, never zero or realized FX profit", async () => {
    reads([{ ...cash, fx_rate_hkd: null, actual_hkd: null, source_booked_hkd: null, target_booked_hkd: null, booking_delta_hkd: null, quality: "MISSING_FX" }]);
    renderUI(<TransferPage mode="cash" />);
    await screen.findByText(/歷史 FX／HKD 未知/);
    expect(screen.getByText(/歷史兩腿入帳差額/)).toHaveTextContent("未知，不是零");
    expect(screen.getByText(/不是目前估值或已實現 FX 損益/)).toBeVisible();
  });

  it("labels nonzero booking delta for review, not fabricated FX profit", async () => {
    reads([{ ...cash, target_currency: "HKD", target_amount: "950.01", target_booked_hkd: "950.010000", booking_delta_hkd: "0.009999", quality: "NEEDS_REVIEW" }]);
    renderUI(<TransferPage mode="cash" />);
    expect(await screen.findByText(/歷史入帳差額需要覆核/)).toBeVisible();
    expect(screen.getByText(/歷史兩腿入帳差額/)).toHaveTextContent("HKD 0.009999");
  });

  it("keeps historical stock snapshots read-only and explicitly not latest cost or current profit", async () => {
    reads([], [{ ...stock, status: "SUPERSEDED", is_current: false }, { ...stock, id: cash.id, root_id: stock.id, supersedes_id: stock.id, quantity: "5.00000000", cost_moved_hkd: "3903.900000", revision: 2 }]);
    renderUI(<TransferPage mode="security" />);
    await screen.findByText(/3123.120000/);
    expect(screen.getByText(/3903.900000/)).toBeVisible();
    expect(screen.getAllByText(/目前回放可因先前交易更正/)).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "更正股票轉倉" })).toHaveLength(1);
    const old = screen.getByRole("article", { name: "2026-01-03 4.00000000 股轉倉" });
    expect(within(old).queryByRole("button", { name: "作廢股票轉倉" })).not.toBeInTheDocument();
  });

  it.each(["cash", "security"] as const)("confirms %s void once and preserves failed dependency/version without success", async mode => {
    reads([cash], [stock]);
    const remove = vi.spyOn(api, "DELETE").mockResolvedValue(rejected("INSUFFICIENT_SHARES"));
    const title = mode === "cash" ? "成對現金轉帳" : "股票轉倉";
    renderUI(<TransferPage mode={mode} />);
    fireEvent.click(await screen.findByRole("button", { name: `作廢${title}` }));
    expect(screen.getByRole("button", { name: `確認作廢${title}` })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("轉倉作廢依據"), { target: { value: "Synthetic source dependency correction" } });
    fireEvent.click(screen.getByRole("button", { name: `確認作廢${title}` }));
    expect(remove).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
    fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
    await within(dialog).findByText(/持倉不足/);
    expect(options(remove).params.path?.id).toBe(mode === "cash" ? cash.id : stock.id);
    expect(options(remove).params.header["If-Match"]).toBe(mode === "cash" ? '"3"' : '"2"');
    expect(screen.queryByText(/已作廢成對現金轉帳/)).not.toBeInTheDocument();
    expect(screen.queryByText(/已作廢股票轉倉；/)).not.toBeInTheDocument();
  });
});
