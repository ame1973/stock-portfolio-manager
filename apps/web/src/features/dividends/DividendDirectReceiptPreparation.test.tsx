import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendDirectReceiptPreparation } from "./DividendDirectReceiptPreparation";
import { account, event } from "./projection-test-data";

type Inputs = components["schemas"]["DirectReceiptInputs"];
const inputs: Inputs = { event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, workflow_status: event.workflow_status,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE",
  account_version: "9007199254740993", portfolio_version: "9223372036854775807", broker_version: "2", portfolio_data_revision: "9223372036854775806",
  rejection_state: "NOT_REJECTED", rejection_id: null, receipt_basis: "DIRECT_EVENT_ACCOUNT", scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" };
const path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/inputs";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: unknown = inputs) { return vi.spyOn(api, "GET").mockImplementation(async request => {
  if (request === path) return reply(value);
  throw new Error("Unexpected read");
}); }
function show(selected = account, observed = event, disabled = false) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onWrite = vi.fn();
  const view = render(<QueryClientProvider client={cache}><DividendDirectReceiptPreparation event={observed} account={selected} onWrite={onWrite} disabled={disabled} /></QueryClientProvider>);
  return { cache, onWrite, view };
}

describe("fresh direct receipt input preparation", () => {
  it("privately reads exact pins without requiring or inventing entitlement or default cash", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const { cache, onWrite } = show();
    const open = await screen.findByRole("button", { name: "核對後記錄直接收款" }); await waitFor(() => expect(open).toBeEnabled());
    expect(screen.getByText(/9007199254740993/)).toBeVisible(); expect(screen.getByText(/不會帶入預設金額或匯率/)).toBeVisible();
    expect(screen.getByText(/作廢也不會解除限制/)).toBeVisible(); fireEvent.click(open);
    expect(onWrite).toHaveBeenCalledWith({ event, account, inputs });
    expect(onWrite.mock.calls[0]![0].inputs).not.toBe(inputs);
    expect(get).toHaveBeenCalledWith(path, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id } }, signal: expect.any(AbortSignal) }));
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it.each([
    { ...inputs, event_id: account.id }, { ...inputs, revision_id: account.id }, { ...inputs, account_id: event.id },
    { ...inputs, account_version: 1 }, { ...inputs, entitlement_id: null }, { ...inputs, received_amount: "0" },
    { ...inputs, eligibility_scope: "ESTABLISHED" }, { ...inputs, rejection_state: "REJECTED" },
  ])("fails closed on malformed or mismatched private input", async invalid => {
    reads(invalid); const { onWrite } = show(); expect(await screen.findByRole("alert")).toHaveTextContent(/無法安全核對/);
    expect(screen.queryByRole("button", { name: "核對後記錄直接收款" })).not.toBeInTheDocument(); expect(onWrite).not.toHaveBeenCalled();
  });
  it.each(["archived", "withdrawn", "rejected", "exhausted", "disabled"])("keeps %s scope non-actionable", async state => {
    const selected = state === "archived" ? { ...account, status: "ARCHIVED" as const } : account;
    const observed = state === "withdrawn" ? { ...event, workflow_status: "WITHDRAWN" as const } : event;
    reads({ ...inputs, account_status: selected.status, workflow_status: observed.workflow_status,
      ...(state === "rejected" ? { rejection_state: "REJECTED", rejection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" } : {}),
      ...(state === "exhausted" ? { portfolio_data_revision: "9223372036854775807" } : {}) });
    const { onWrite } = show(selected, observed, state === "disabled");
    expect(await screen.findByRole("button", { name: "核對後記錄直接收款" })).toBeDisabled(); expect(onWrite).not.toHaveBeenCalled();
  });
  it("disables previous values throughout refresh, error and offline recovery", async () => {
    const get = reads(); const { onWrite } = show(); const open = await screen.findByRole("button", { name: "核對後記錄直接收款" });
    await waitFor(() => expect(open).toBeEnabled()); let deliver!: () => void;
    get.mockImplementation(async () => new Promise(resolve => { deliver = () => resolve(reply(inputs)); }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入直接收款輸入" })); expect(open).toBeDisabled();
    await waitFor(() => expect(deliver).toBeTypeOf("function")); await act(async () => deliver()); await waitFor(() => expect(open).toBeEnabled());
    get.mockRejectedValueOnce(new TypeError("synthetic private error"));
    fireEvent.click(screen.getByRole("button", { name: "重新載入直接收款輸入" })); await screen.findByRole("alert"); expect(open).toBeDisabled();
    get.mockImplementation(async () => new Promise(resolve => { deliver = () => resolve(reply(inputs)); }));
    try { act(() => onlineManager.setOnline(false)); expect(open).toBeDisabled(); }
    finally { act(() => onlineManager.setOnline(true)); }
    expect(open).toBeDisabled(); await waitFor(() => expect(deliver).toBeTypeOf("function"));
    await act(async () => deliver()); await waitFor(() => expect(open).toBeEnabled()); expect(onWrite).not.toHaveBeenCalled();
  });
  it("aborts late reads when the selected scope changes or the component closes", async () => {
    const get = reads(); let deliver!: () => void; let signal!: AbortSignal;
    get.mockImplementation(async (_request, options) => new Promise(resolve => {
      signal = (options as { signal: AbortSignal }).signal; deliver = () => resolve(reply(inputs));
    }));
    const { cache, onWrite, view } = show(); await waitFor(() => expect(deliver).toBeTypeOf("function"));
    const first = signal; const firstDeliver = deliver;
    view.rerender(<QueryClientProvider client={cache}><DividendDirectReceiptPreparation event={event} account={{ ...account, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }} onWrite={onWrite} /></QueryClientProvider>);
    expect(first.aborted).toBe(true); await act(async () => firstDeliver());
    expect(screen.queryByRole("button", { name: "核對後記錄直接收款" })).not.toBeInTheDocument();
    view.unmount(); expect(signal.aborted).toBe(true); expect(onWrite).not.toHaveBeenCalled();
  });
});
