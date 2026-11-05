import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { BookingForm } from "./BookingForm";
import { BookingPage } from "./BookingPage";
import { AccountCashPage } from "./AccountCashPage";
import { CompletenessForm, OpeningForm } from "./OpeningForm";
import type { Cash, Expense, Opening } from "./data";

type Schemas = components["schemas"];
const account: Schemas["Account"] = { id: "11111111-1111-4111-8111-111111111111", portfolio_id: "22222222-2222-4222-8222-222222222222", broker_id: "33333333-3333-4333-8333-333333333333", name: "Synthetic cash account", default_currency: "USD", opening_cash: "1000.000000", opening_cash_date: "2026-01-10", cash_completeness: "UNKNOWN", status: "ACTIVE", version: 1 };
const cash: Cash = { id: "44444444-4444-4444-8444-444444444444", account_id: account.id, portfolio_id: account.portfolio_id, effective_date: "2026-01-02", kind: "DEPOSIT", currency: "USD", amount: "2.000001", fx_rate_hkd: "7.800000000000", booked_hkd: "16.123456", external_flow: true, source_type: "MANUAL_CASH", source_id: "55555555-5555-4555-8555-555555555555", entry_role: "PRIMARY", reverses_entry_id: null, reversed_by_id: null, supersedes_id: null, superseded_by_id: null, status: "ACTIVE", editable: true, quality: "OK", note: "Original native evidence", version: 3, created_at: "2026-01-02T00:00:00Z" };
const expense: Expense = { id: "66666666-6666-4666-8666-666666666666", account_id: account.id, portfolio_id: account.portfolio_id, date: "2026-01-02", category: "Custody", currency: "USD", amount: "2.000001", fx_rate_hkd: "7.800000000000", hkd_amount: "16.123456", status: "ACTIVE", supersedes_id: null, quality: "OK", note: "Expense evidence", version: 4, created_at: "2026-01-02T00:00:00Z" };
const opening: Opening = { account_id: account.id, currency: "USD", opening_cash: "1000.000000", opening_cash_date: "2026-01-10", fx_rate_hkd: "7.700000000000", booked_hkd: "7700.000000", entry_id: "77777777-7777-4777-8777-777777777777", predecessor_id: null, version: 1, cash_completeness: "UNKNOWN", quality: "OK" };
const balances: Schemas["AccountCashBalances"] = { account_id: account.id, as_of: "2026-01-20", cash_completeness: "UNKNOWN", balances: [{ currency: "USD", recorded_balance: "1000.000000", balance: null, balance_quality: "UNKNOWN", historical_booked_hkd_net: null, historical_booking_quality: "MISSING_FX", unvalued_entries: 1 }, { currency: "HKD", recorded_balance: "0.000000", balance: null, balance_quality: "UNKNOWN", historical_booked_hkd_net: "0.000000", historical_booking_quality: "OK", unvalued_entries: 0 }] };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const rejected = (code: string, status = 409) => ({ error: { code, message: "Synthetic failure", request_id: "test", details: {} }, response: new Response(null, { status }) });

function renderUI(ui: ReactElement, path = "/") {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function reads(cashRows: Cash[] = [], expenses: Expense[] = [], wallets = balances, accountRecord = account) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    switch (path) {
      case "/api/v1/auth/csrf": return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
      case "/api/v1/accounts": return reply({ items: [accountRecord], next_cursor: null });
      case "/api/v1/accounts/{id}": return reply(accountRecord);
      case "/api/v1/portfolios": return reply({ items: [], next_cursor: null });
      case "/api/v1/cash-entries": return reply({ items: cashRows, next_cursor: null });
      case "/api/v1/expenses": return reply({ items: expenses, next_cursor: null });
      case "/api/v1/accounts/{id}/cash-balances": return reply(wallets);
      case "/api/v1/accounts/{id}/opening-cash": return reply(opening);
      default: throw new Error(`Unexpected read: ${path}`);
    }
  });
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Record<string, unknown>; params: { header: Record<string, string> } };
}
async function fillBooking(mode: "cash" | "expense", amount = "10.000001") {
  await screen.findByRole("option", { name: `${account.name} (USD)` });
  fireEvent.change(screen.getByLabelText("入帳帳戶", { exact: true }), { target: { value: account.id } });
  fireEvent.change(screen.getByLabelText("實際原幣", { exact: true }), { target: { value: "USD" } });
  fireEvent.change(screen.getByLabelText("實際入帳日期"), { target: { value: "2026-01-02" } });
  fireEvent.change(screen.getByLabelText("實際原幣金額"), { target: { value: amount } });
  if (mode === "cash") fireEvent.change(screen.getByLabelText("現金分類"), { target: { value: "DEPOSIT" } });
  else fireEvent.change(screen.getByLabelText("費用分類"), { target: { value: "Custody" } });
}
function accountUI() {
  return renderUI(<Routes><Route path="/cash/accounts/:id" element={<AccountCashPage />} /></Routes>, `/cash/accounts/${account.id}`);
}

describe("native cash and operating-expense forms", () => {
  it("posts exact native cash with missing FX and a non-editable external-flow classification", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(cash));
    const saved = vi.fn();
    renderUI(<BookingForm mode="cash" onSaved={saved} onCancel={vi.fn()} />);
    await fillBooking("cash", "0010.000001");
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(cash));
    expect(options(post).body).toMatchObject({ amount: "0010.000001", currency: "USD", fx_rate_hkd: null, actual_hkd: null, external_flow: true, kind: "DEPOSIT" });
    expect(screen.queryByLabelText("外部現金流")).not.toBeInTheDocument();
  });

  it.each([["DEPOSIT", "-1"], ["WITHDRAW", "1"], ["DEPOSIT", "0.000000"], ["DEPOSIT", "1.0000001"], ["DEPOSIT", "1e3"], ["ADJUSTMENT", "1"]])("rejects invalid %s amount/reason %s without writing", async (kind, amount) => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<BookingForm mode="cash" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillBooking("cash", amount);
    fireEvent.change(screen.getByLabelText("現金分類"), { target: { value: kind } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByLabelText("實際原幣金額")).toHaveValue(amount);
  });

  it("requires evidence for negative expense refunds and retains the signed six-place native amount", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(expense));
    renderUI(<BookingForm mode="expense" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillBooking("expense", "-2.000001");
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/退款或實際 HKD 需要/);
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("入帳／更正依據"), { target: { value: "Synthetic refund from reviewed statement" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body).toMatchObject({ date: "2026-01-02", category: "Custody", amount: "-2.000001", fx_rate_hkd: null });
    expect(options(post).body).not.toHaveProperty("fee");
  });

  it.each(["cash", "expense"] as const)("preserves %s booked HKD on a note-only revision and clears it for an explicit native/FX change", async mode => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    renderUI(<BookingForm mode={mode} record={mode === "cash" ? cash : expense} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("沿用／指定 HKD 金額（選填）")).toHaveValue("16.123456");
    fireEvent.change(screen.getByLabelText("入帳備註"), { target: { value: "Corrected nonfinancial note" } });
    fireEvent.change(screen.getByLabelText("入帳／更正依據"), { target: { value: "Preserve the original booked statement snapshot" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/不會覆蓋新版/);
    expect(options(patch).body.actual_hkd).toBe("16.123456");
    expect(options(patch).params.header["If-Match"]).toBe(mode === "cash" ? '"3"' : '"4"');
    expect(screen.getByLabelText("入帳備註")).toHaveValue("Corrected nonfinancial note");
    fireEvent.change(screen.getByLabelText("現金歷史匯率（1 USD = HKD）"), { target: { value: "7.9" } });
    expect(screen.getByLabelText("沿用／指定 HKD 金額（選填）")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    expect(options(patch, 1).body).toMatchObject({ actual_hkd: null, fx_rate_hkd: "7.9" });
  });

  it("requires historical FX for designated HKD but never invents a rate from the amounts", async () => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<BookingForm mode="cash" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillBooking("cash");
    fireEvent.change(screen.getByLabelText("沿用／指定 HKD 金額（選填）"), { target: { value: "78.000001" } });
    fireEvent.change(screen.getByLabelText("入帳／更正依據"), { target: { value: "Synthetic actual historical booking" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText("實際 HKD 入帳仍須保留歷史匯率。");
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByLabelText("現金歷史匯率（1 USD = HKD）")).toHaveValue("");
  });

  it("uses HKD identity without carrying an old USD rate or designated amount", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(cash));
    renderUI(<BookingForm mode="cash" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillBooking("cash");
    fireEvent.change(screen.getByLabelText("現金歷史匯率（1 USD = HKD）"), { target: { value: "7.8" } });
    fireEvent.change(screen.getByLabelText("沿用／指定 HKD 金額（選填）"), { target: { value: "78.000001" } });
    fireEvent.change(screen.getByLabelText("實際原幣", { exact: true }), { target: { value: "HKD" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body).toMatchObject({ currency: "HKD", fx_rate_hkd: null, actual_hkd: null });
  });
});

describe("cash journal and expense history", () => {
  it("shows unknown bookings honestly and never offers edits for managed/reversal cash", async () => {
    reads([{ ...cash, booked_hkd: null, fx_rate_hkd: null, quality: "MISSING_FX" }, { ...cash, id: opening.entry_id!, amount: "0.000000", booked_hkd: "0.000000", kind: "ADJUSTMENT", source_type: "OPENING_BALANCE", editable: false }, { ...cash, id: expense.id, entry_role: "REVERSAL", kind: "REVERSAL", status: "REVERSAL", editable: false }]);
    renderUI(<BookingPage mode="cash" />);
    expect(await screen.findByText(/未知，不是零（缺少歷史 FX）/)).toBeVisible();
    expect(screen.queryByText(/HKD undefined/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "更正現金紀錄" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "作廢現金紀錄" })).toHaveLength(1);
    expect(screen.getByText(/來源流程管理，不能直接改寫或作廢/)).toBeVisible();
  });

  it("uses explicit expense history/date filters and keeps stale financial facts read-only", async () => {
    const get = reads([], [{ ...expense, status: "SUPERSEDED" }]);
    renderUI(<BookingPage mode="expense" />);
    await screen.findByText(/已被新修訂取代/);
    expect(screen.queryByRole("button", { name: "更正費用紀錄" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("包括已被取代的費用修訂"));
    fireEvent.change(screen.getByLabelText("入帳起日"), { target: { value: "2026-01-01" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/expenses", expect.objectContaining({ params: { query: expect.objectContaining({ include_history: true, from_date: "2026-01-01", limit: 25 }) } })));
  });

  it.each(["cash", "expense"] as const)("requires reasoned second confirmation for %s void and never reports failed CAS as success", async mode => {
    reads([cash], [expense]);
    const remove = vi.spyOn(api, "DELETE").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    renderUI(<BookingPage mode={mode} />);
    fireEvent.click(await screen.findByRole("button", { name: mode === "cash" ? "作廢現金紀錄" : "作廢費用紀錄" }));
    const label = mode === "cash" ? "確認作廢現金紀錄" : "確認作廢費用紀錄";
    expect(screen.getByRole("button", { name: label })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("作廢入帳依據"), { target: { value: "Synthetic reviewed cash correction" } });
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(remove).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());
    fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
    await within(dialog).findByText(/不會覆蓋新版/);
    expect(options(remove).params.header["If-Match"]).toBe(mode === "cash" ? '"3"' : '"4"');
    expect(options(remove).body.reason).toBe("Synthetic reviewed cash correction");
    expect(screen.queryByText(/已作廢並精確沖銷/)).not.toBeInTheDocument();
  });
});

describe("opening assertion and native balance truth", () => {
  it.each([["PARTIAL", "只有部分現金紀錄"], ["BEFORE_OPENING", "早於目前期初日期"], ["NEEDS_REVIEW", "期初來源或餘額需要覆核"]] as const)("keeps %s balances unavailable with a specific explanation", async (quality, label) => {
    reads([], [], { ...balances, balances: [{ ...balances.balances[0]!, balance_quality: quality }] });
    accountUI();
    const wallet = await screen.findByLabelText("USD 錢包");
    expect(within(wallet).getByText(label)).toBeVisible();
    expect(within(wallet).getByText("未知，不是零")).toBeVisible();
  });

  it("shows an explicit known zero separately from a nonzero historical booking net", async () => {
    const get = reads([], [], { ...balances, cash_completeness: "COMPLETE", balances: [{ ...balances.balances[0]!, recorded_balance: "0.000000", balance: "0.000000", balance_quality: "COMPLETE", historical_booked_hkd_net: "-0.800000", historical_booking_quality: "OK", unvalued_entries: 0 }] });
    accountUI();
    const wallet = await screen.findByLabelText("USD 錢包");
    const actual = within(wallet).getByText("可確認的實際餘額").parentElement!;
    expect(within(actual).getByText("USD 0.000000")).toBeVisible();
    expect(within(wallet).getByText("HKD -0.800000")).toBeVisible();
    expect(within(wallet).queryByText("未知，不是零")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("餘額生效日（留空使用伺服器今日）"), { target: { value: "2026-01-09" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/accounts/{id}/cash-balances", expect.objectContaining({ params: { path: { id: account.id }, query: { as_of: "2026-01-09" } } })));
  });

  it("does not open a new assertion editor for an archived account", async () => {
    reads([], [], balances, { ...account, status: "ARCHIVED" });
    accountUI();
    await screen.findByText(/帳戶已封存/);
    expect(await screen.findByRole("button", { name: "設定／更正期初" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "撤回期初斷言" })).toBeDisabled();
  });

  it("separates recorded native money, unknown actual balance and historical HKD without adding wallets", async () => {
    reads();
    accountUI();
    const usd = await screen.findByLabelText("USD 錢包");
    const hkd = screen.getByLabelText("HKD 錢包");
    expect(within(usd).getByText("USD 1000.000000")).toBeVisible();
    expect(within(usd).getByText("未知，不是零")).toBeVisible();
    expect(within(usd).getByText(/未知（仍有未認列歷史 FX）/)).toBeVisible();
    const recorded = within(hkd).getByText("已記錄原幣淨額（含沖銷）").parentElement!;
    expect(within(recorded).getByText("HKD 0.000000")).toBeVisible();
    expect(within(hkd).getByText("未知，不是零")).toBeVisible();
    expect(screen.getByText(/不加總不同幣別/)).toBeVisible();
  });

  it("cannot silently clear an assertion with blank input or use missing FX for known zero", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH");
    renderUI(<OpeningForm record={opening} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("期初餘額（USD）"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("期初更正依據"), { target: { value: "Synthetic explicit reviewed assertion" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByRole("alert");
    expect(patch).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("期初餘額（USD）"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("期初歷史匯率（1 USD = HKD）"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/即使金額為零/);
    expect(patch).not.toHaveBeenCalled();
  });

  it("freezes the opening version while a background read changes, retaining stale correction input", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(rejected("PRECONDITION_FAILED", 412));
    const cache = accountUI();
    fireEvent.click(await screen.findByRole("button", { name: "設定／更正期初" }));
    fireEvent.change(screen.getByLabelText("期初餘額（USD）"), { target: { value: "1500.000001" } });
    fireEvent.change(screen.getByLabelText("期初更正依據"), { target: { value: "Synthetic replacement opening evidence" } });
    await act(async () => { cache.setQueryData(["ledger", "opening", account.id], { ...opening, version: 2, opening_cash: "2000.000000" }); });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByText(/不會覆蓋新版/);
    expect(options(patch).params.header["If-Match"]).toBe('"1"');
    expect(screen.getByLabelText("期初餘額（USD）")).toHaveValue("1500.000001");
  });

  it("clears only through an explicit reasoned confirmation with null date/rate and account CAS", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply({ ...opening, opening_cash: null, opening_cash_date: null, fx_rate_hkd: null, booked_hkd: null, quality: "UNSET", version: 2 }));
    accountUI();
    fireEvent.click(await screen.findByRole("button", { name: "撤回期初斷言" }));
    fireEvent.change(screen.getByLabelText("撤回期初依據"), { target: { value: "Withdraw unsupported starting cash evidence" } });
    fireEvent.click(screen.getByRole("button", { name: "確認撤回期初斷言" }));
    expect(patch).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    expect(options(patch).body).toEqual({ opening_cash: null, opening_cash_date: null, opening_fx_rate_hkd: null, reason: "Withdraw unsupported starting cash evidence", note: "" });
    expect(options(patch).params.header["If-Match"]).toBe('"1"');
  });

  it("requires an independent explicit completeness assertion and observed account version", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply({ ...account, cash_completeness: "COMPLETE", version: 2 }));
    const saved = vi.fn();
    renderUI(<CompletenessForm record={account} onSaved={saved} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("帳戶現金完整性"), { target: { value: "COMPLETE" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await screen.findByRole("alert");
    expect(patch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("我已核對所有原幣、現金歷史及期初來源"));
    fireEvent.click(screen.getByRole("button", { name: "儲存" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(options(patch).body).toEqual({ cash_completeness: "COMPLETE" });
    expect(options(patch).params.header["If-Match"]).toBe('"1"');
  });
});
