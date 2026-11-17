import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendProjectionPreparation } from "./DividendProjectionPreparation";

type Schemas = components["schemas"];
const event: Schemas["DividendEventRecord"] = { id: "a1111111-1111-4111-8111-111111111111", current_revision_id: "b2222222-2222-4222-8222-222222222222", version: 3,
  security_id: "c3333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
  dps: "1.00000000", currency: "USD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false, manual_override: false,
  decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [], reference: null,
  reason: "Synthetic event decision", created_at: "2026-11-01T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED" };
const account: Schemas["Account"] = { id: "d4444444-4444-4444-8444-444444444444", portfolio_id: "e5555555-5555-4555-8555-555555555555", broker_id: "f6666666-6666-4666-8666-666666666666",
  name: "Synthetic projection account", default_currency: "USD", status: "ACTIVE", version: 1, opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN" };
const common = { event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id, portfolio_id: account.portfolio_id,
  broker_id: account.broker_id, account_version: "9007199254740993", broker_version: "1" };
const tax: Schemas["TaxSettingSelection"] = { ...common, effective_on: "2026-11-03", head_no: "0", head_id: null, tax_status: "MISSING", selected: null,
  selected_scope: "NO_SELECTED_SETTING", scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED" };
const fee: Schemas["FeeSettingSelection"] = { ...common, effective_on: "2026-11-04", head_no: "0", head_id: null, fee_status: "MISSING", selected: null,
  selected_scope: "NO_SELECTED_SETTING", revision_rejection: "NOT_REJECTED", scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED" };
const inputs: Schemas["ProjectionInputs"] = { ...common, ex_date: event.ex_date, currency: event.currency, portfolio_data_revision: "2", projection_head_no: "0", projection_head_id: null,
  as_of: "2026-11-02", eligibility_rule: "ORDINARY_PRE_EX_DATE", tax: { effective_on: tax.effective_on, head_no: "0", head_id: null, selected_id: null, status: "MISSING", selected_scope: "NO_SELECTED_SETTING", scope: "OWNER_ESTIMATE_ONLY" },
  fee: { effective_on: fee.effective_on, head_no: "0", head_id: null, selected_id: null, status: "MISSING", selected_scope: "NO_SELECTED_SETTING", scope: "OWNER_ESTIMATE_ONLY" },
  rejection_state: "NOT_REJECTED", scope: "RECORDED_PROJECTION_INPUTS_ONLY", current_use_scope: "REVALIDATION_REQUIRED", source_scope: "CURRENT_SOURCE_NOT_REVALIDATED", eligibility_scope: "NOT_ESTABLISHED" };
const inputPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projection-inputs";
const taxPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings";
const feePath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(values = { inputs, tax, fee }) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === inputPath) return reply(values.inputs);
    if (path === taxPath) return reply(values.tax);
    if (path === feePath) return reply(values.fee);
    throw new Error(`Unexpected read: ${path}`);
  });
}
function show(disabled = false) {
  const onWrite = vi.fn(); const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><DividendProjectionPreparation event={event} account={account} onWrite={onWrite} disabled={disabled} /></QueryClientProvider>);
  return { onWrite, cache };
}
function specify() {
  fireEvent.change(screen.getByLabelText("投影查閱日"), { target: { value: "2026-11-02" } });
  fireEvent.change(screen.getByLabelText("投影規則"), { target: { value: "ORDINARY_PRE_EX_DATE" } });
  fireEvent.change(screen.getByLabelText("投影稅率估算生效日"), { target: { value: "2026-11-03" } });
  fireEvent.change(screen.getByLabelText("投影費用估算生效日"), { target: { value: "2026-11-04" } });
}
async function start() {
  specify(); fireEvent.click(screen.getByRole("button", { name: "核對投影輸入" }));
  const open = await screen.findByRole("button", { name: "開啟記錄投影" }); await waitFor(() => expect(open).toBeEnabled()); return open;
}

describe("explicit recorded projection preparation", () => {
  it("does not read or default dates and rule until explicitly requested", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const { onWrite, cache } = show();
    expect(screen.getByRole("button", { name: "核對投影輸入" })).toBeDisabled(); expect(get).not.toHaveBeenCalled();
    const open = await start();
    expect(get).toHaveBeenCalledWith(inputPath, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id }, query: {
      as_of: "2026-11-02", eligibility_rule: "ORDINARY_PRE_EX_DATE", tax_effective_on: "2026-11-03", fee_effective_on: "2026-11-04" } } }));
    expect(screen.getByText(/稅率查閱日.*未設定，不是零/)).toBeVisible(); expect(screen.getByText(/費用查閱日.*未設定，不是零/)).toBeVisible();
    expect(screen.getByText(/並非同一快照/)).toBeVisible(); expect(screen.getByText(/9007199254740993/)).toBeVisible();
    fireEvent.click(open); expect(onWrite).toHaveBeenCalledWith(expect.objectContaining({ event, account, inputs, tax, fee }));
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it.each([
    { inputs: { ...inputs, rejection_state: "REJECTED" }, tax, fee },
    { inputs, tax, fee: { ...fee, revision_rejection: "REJECTED" } },
    { inputs, tax: { ...tax, account_version: "1" }, fee },
    { inputs: { ...inputs, projection_head_no: "9223372036854775807", projection_head_id: event.id }, tax, fee },
  ])("blocks a rejected, stale or exhausted target %#", values => {
    reads(values as { inputs: typeof inputs; tax: typeof tax; fee: typeof fee }); const { onWrite } = show(); specify();
    fireEvent.click(screen.getByRole("button", { name: "核對投影輸入" }));
    return waitFor(() => { expect(screen.getByRole("alert")).toHaveTextContent(/不可開始新投影/); expect(screen.getByRole("button", { name: "開啟記錄投影" })).toBeDisabled(); expect(onWrite).not.toHaveBeenCalled(); });
  });
  it("blocks selection while refreshing, paused or while the event list is stale", async () => {
    const get = reads(); const { onWrite } = show(); const open = await start(); let deliver!: () => void;
    get.mockImplementation(async path => path === inputPath ? new Promise(resolve => { deliver = () => resolve(reply(inputs)); }) : reply(path === taxPath ? tax : fee));
    fireEvent.click(screen.getByRole("button", { name: "重新載入投影輸入" })); expect(open).toBeDisabled();
    await waitFor(() => expect(deliver).toBeTypeOf("function")); await act(async () => deliver()); await waitFor(() => expect(open).toBeEnabled());
    try { act(() => onlineManager.setOnline(false)); expect(open).toBeDisabled(); }
    finally { act(() => onlineManager.setOnline(true)); }
    expect(onWrite).not.toHaveBeenCalled();
  });
  it("does not start a preflight when its parent event list is stale", () => {
    const get = reads(); show(true); specify(); expect(screen.getByRole("button", { name: "核對投影輸入" })).toBeDisabled(); expect(get).not.toHaveBeenCalled();
  });
  it("aborts and discards late responses when any explicit date changes", async () => {
    const get = reads(); let deliver!: () => void; let signal!: AbortSignal;
    get.mockImplementation(async (path, options) => {
      if (path === inputPath) return new Promise(resolve => { signal = (options as { signal: AbortSignal }).signal; deliver = () => resolve(reply(inputs)); });
      return reply(path === taxPath ? tax : fee);
    });
    const { onWrite } = show(); specify(); fireEvent.click(screen.getByRole("button", { name: "核對投影輸入" }));
    await waitFor(() => expect(deliver).toBeTypeOf("function"));
    fireEvent.change(screen.getByLabelText("投影費用估算生效日"), { target: { value: "2026-11-05" } });
    expect(signal.aborted).toBe(true); await act(async () => deliver());
    expect(screen.queryByRole("button", { name: "開啟記錄投影" })).not.toBeInTheDocument(); expect(onWrite).not.toHaveBeenCalled();
  });
});
