import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendEstimatePanel } from "./DividendEstimatePanel";

type Schemas = components["schemas"];
const event: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 3,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null,
  pay_date: null, dps: "0.00000001", currency: "USD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false,
  manual_override: false, decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic event reference", url: null, url_withheld: false }, reason: "Synthetic event decision for estimate review",
  created_at: "2026-11-01T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const account: Schemas["Account"] = {
  id: "44444444-4444-4444-8444-444444444444", portfolio_id: "55555555-5555-4555-8555-555555555555", broker_id: "66666666-6666-4666-8666-666666666666",
  name: "Synthetic estimate account", default_currency: "USD", status: "ACTIVE", version: 1, opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN",
};
const setting: Schemas["TaxSettingRecord"] = {
  id: "77777777-7777-4777-8777-777777777777", event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id,
  portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: "9007199254740993", broker_version: "9223372036854775807",
  setting_no: "9007199254740993", previous_id: "88888888-8888-4888-8888-888888888888", effective_from: "2026-11-01", tax_status: "ESTIMATED", rate: "0.000000",
  reason: "Synthetic explicit zero estimate", created_at: "2026-11-02T00:00:00Z", scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
};
const tax: Schemas["TaxSettingSelection"] = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id, portfolio_id: account.portfolio_id,
  broker_id: account.broker_id, account_version: setting.account_version, broker_version: setting.broker_version, effective_on: "2026-11-03",
  head_no: setting.setting_no, head_id: setting.id, tax_status: "ESTIMATED", selected: setting, selected_scope: "ACCOUNT_SCOPE_MATCH",
  scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
};
const fee: Schemas["FeeSettingSelection"] = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id, portfolio_id: account.portfolio_id,
  broker_id: account.broker_id, account_version: setting.account_version, broker_version: setting.broker_version, effective_on: "2026-11-03",
  head_no: "0", head_id: null, fee_status: "MISSING", selected: null, selected_scope: "NO_SELECTED_SETTING", revision_rejection: "NOT_REJECTED",
  scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
};
const taxPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings";
const feePath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(taxValue: unknown = tax, feeValue: unknown = fee) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (path === taxPath) return reply(taxValue);
    if (path === feePath) return reply(feeValue);
    if (path === `${taxPath}/history`) return reply({ items: [setting], next_cursor: null });
    if (path === `${feePath}/history`) return reply({ items: [], next_cursor: null });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderPanel(record = event) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><DividendEstimatePanel event={record} /></QueryClientProvider>);
  return cache;
}
async function selectScope() {
  fireEvent.click(await screen.findByRole("button", { name: `選擇估算帳戶 ${account.name}` }));
  fireEvent.change(screen.getByLabelText("查閱估算生效日"), { target: { value: "2026-11-03" } });
}

describe("account-scoped retained tax and fee estimates", () => {
  it("requires an explicit account and day, then preserves exact zero, missing and INT64 pins", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const cache = renderPanel();
    expect(get).not.toHaveBeenCalledWith(taxPath, expect.anything()); expect(get).not.toHaveBeenCalledWith(feePath, expect.anything());
    await selectScope(); const taxSection = await screen.findByRole("region", { name: "稅率估算" }); const feeSection = await screen.findByRole("region", { name: "費用估算" });
    expect(taxSection).toHaveTextContent("0.000000"); expect(taxSection).toHaveTextContent("9007199254740993");
    expect(taxSection).toHaveTextContent("9223372036854775807"); expect(taxSection).toHaveTextContent("不是已確認扣稅");
    expect(taxSection).toHaveTextContent("1 代表 100%");
    expect(feeSection).toHaveTextContent("未設定，不是零"); expect(feeSection).toHaveTextContent("未記錄拒絕");
    expect(get).toHaveBeenCalledWith(taxPath, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id }, query: { effective_on: "2026-11-03" } } }));
    expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(post).not.toHaveBeenCalled(); expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("keeps a nonempty future head separate from a missing current tax selection", async () => {
    reads({ ...tax, tax_status: "MISSING", selected: null, selected_scope: "NO_SELECTED_SETTING" }); renderPanel(); await selectScope();
    const panel = await screen.findByRole("region", { name: "稅率估算" });
    expect(await within(panel).findByText(/未設定，不是零/)).toBeVisible(); expect(panel).toHaveTextContent("9007199254740993");
  });

  it("distinguishes an explicit unknown fee with known currency from zero or missing", async () => {
    const unknown = { id: "99999999-9999-4999-8999-999999999999", event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
      account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: setting.account_version, broker_version: setting.broker_version,
      setting_no: "1", previous_id: null, effective_from: "2026-11-01", fee_status: "UNKNOWN", fee: null, fee_currency: "USD", reason: "Synthetic unknown amount",
      created_at: setting.created_at, scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED" };
    reads(tax, { ...fee, head_no: "1", head_id: unknown.id, fee_status: "UNKNOWN", selected: unknown, selected_scope: "ACCOUNT_SCOPE_MATCH" }); renderPanel(); await selectScope();
    const panel = await screen.findByRole("region", { name: "費用估算" }); expect(panel).toHaveTextContent("金額未知"); expect(panel).toHaveTextContent("USD");
    expect(panel).not.toHaveTextContent("0.000000"); expect(panel).not.toHaveTextContent("未設定，不是零");
  });

  it.each([
    { ...tax, account_version: 1 }, { ...tax, event_id: account.id }, { ...tax, revision_id: account.id }, { ...tax, revision_no: 2 },
    { ...tax, effective_on: "2026-11-04" }, { ...tax, head_no: "01" }, { ...tax, head_no: "not-an-integer" }, { ...tax, head_id: null },
    { ...tax, portfolio_id: event.security_id }, { ...tax, broker_id: event.security_id },
    { ...tax, selected_scope: "ACCOUNT_SCOPE_CHANGED" }, { ...tax, tax_status: "UNKNOWN" },
    { ...tax, selected: { ...setting, setting_no: "9007199254740994" } },
  ])("fails closed on malformed, mismatched or inconsistent tax selections", async broken => {
    reads(broken); renderPanel(); await selectScope();
    const panel = await screen.findByRole("region", { name: "稅率估算" });
    expect(await within(panel).findByRole("alert")).toHaveTextContent("稅率估算範圍無法安全核對");
    expect(within(panel).queryByText("0.000000")).not.toBeInTheDocument();
  });

  it.each([
    { ...fee, revision_rejection: "ACCEPTED" }, { ...fee, account_version: "0" }, { ...fee, head_no: "1", head_id: null },
    { ...fee, fee_status: "ESTIMATED" }, { ...fee, current_use_scope: "GRANTED" },
  ])("fails closed on invalid fee scope, status or rejection labels", async broken => {
    reads(tax, broken); renderPanel(); await selectScope(); const panel = await screen.findByRole("region", { name: "費用估算" });
    expect(await within(panel).findByRole("alert")).toHaveTextContent("費用估算範圍無法安全核對");
  });

  it("shows stale account provenance without calling it a current estimate", async () => {
    reads({ ...tax, selected: { ...setting, account_version: "1" }, selected_scope: "ACCOUNT_SCOPE_CHANGED" }); renderPanel(); await selectScope();
    const panel = await screen.findByRole("region", { name: "稅率估算" }); expect(await within(panel).findByText(/帳戶或券商範圍已變更/)).toBeVisible();
  });

  it("reads separately paged physical history without changing the current selection", async () => {
    const get = reads(); renderPanel(); await selectScope(); const panel = await screen.findByRole("region", { name: "稅率估算" });
    fireEvent.click(await within(panel).findByRole("button", { name: "查看稅率估算歷史" }));
    expect(await screen.findByRole("region", { name: "稅率估算歷史" })).toHaveTextContent("9007199254740993");
    expect(get).toHaveBeenCalledWith(`${taxPath}/history`, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id }, query: expect.objectContaining({ revision_id: event.current_revision_id, revision_no: event.version, limit: 25 }) } }));
  });

  it.each([
    [setting, setting], [setting, { ...setting, setting_no: "9007199254740994", id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }],
    [{ ...setting, account_id: event.id }], [{ ...setting, rate: "1.000001" }],
  ])("rejects invalid, repeated or out-of-order tax history", async (...items) => {
    const get = reads(); get.mockImplementation(async path => {
      if (path === `${taxPath}/history`) return reply({ items, next_cursor: null });
      if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      return reply(path === taxPath ? tax : fee);
    });
    renderPanel(); await selectScope(); const panel = await screen.findByRole("region", { name: "稅率估算" });
    fireEvent.click(await within(panel).findByRole("button", { name: "查看稅率估算歷史" }));
    const history = await screen.findByRole("region", { name: "稅率估算歷史" });
    expect(await within(history).findByRole("alert")).toHaveTextContent("估算歷史範圍無法安全核對");
  });

  it("ignores a late selection response after the explicit day changes", async () => {
    const get = reads(); let deliver!: () => void; let signal!: AbortSignal;
    get.mockImplementation(async (path, options) => {
      if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      const query = options as { signal: AbortSignal; params: { query: { effective_on: string } } };
      if (path === taxPath && query.params.query.effective_on === "2026-11-03") return new Promise(resolve => { signal = query.signal; deliver = () => resolve(reply(tax)); });
      const data = path === taxPath ? tax : fee;
      return reply({ ...data, effective_on: query.params.query.effective_on });
    });
    renderPanel(); await selectScope(); await waitFor(() => expect(deliver).toBeTypeOf("function"));
    fireEvent.change(screen.getByLabelText("查閱估算生效日"), { target: { value: "2026-11-04" } });
    await waitFor(() => expect(signal.aborted).toBe(true)); await act(async () => deliver());
    expect(await screen.findByText(/稅率估算 0.000000/)).toBeVisible();
    expect(get).toHaveBeenCalledWith(taxPath, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id }, query: { effective_on: "2026-11-04" } } }));
  });

  it("does not treat a fee rejection state as a tax acceptance or clear rejection", async () => {
    reads(tax, { ...fee, revision_rejection: "REJECTED" }); renderPanel(); await selectScope();
    const panel = await screen.findByRole("region", { name: "費用估算" }); expect(panel).toHaveTextContent("此讀取顯示事件版本已拒絕");
    expect(screen.getByText(/稅率與拒絕狀態不是同一讀取/)).toBeVisible();
  });

  it("does not let a previously loaded account be selected while offline", async () => {
    reads(); renderPanel(); const button = await screen.findByRole("button", { name: `選擇估算帳戶 ${account.name}` });
    try { await act(async () => onlineManager.setOnline(false)); expect(button).toBeDisabled(); }
    finally { await act(async () => onlineManager.setOnline(true)); }
  });
});
