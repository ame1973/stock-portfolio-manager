import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendReceiptPreparation } from "./DividendReceiptPreparation";
import { account, event } from "./projection-test-data";
import { current, inputs, old } from "./receipt-test-data";

const path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/inputs";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: unknown = inputs) { return vi.spyOn(api, "GET").mockImplementation(async request => {
  if (request === path) return reply(value);
  throw new Error("Unexpected read");
}); }
function show(disabled = false, selected = account) {
  const onWrite = vi.fn(); const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><DividendReceiptPreparation event={event} account={selected} onWrite={onWrite} disabled={disabled} /></QueryClientProvider>);
  return { cache, onWrite, view };
}

describe("recorded receipt preparation", () => {
  it("requires an explicit historical basis without treating old values as eligibility", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const { onWrite, cache } = show();
    const open = await screen.findByRole("button", { name: `選擇應收 ${old.id} 並記錄收款` }); await waitFor(() => expect(open).toBeEnabled());
    expect(screen.getByText(/數值及狀態未驗證/)).toBeVisible(); expect(screen.getByText(/原記錄預計淨額 USD 10.000000/)).toBeVisible();
    expect(screen.getByText(/9007199254740993/)).toBeVisible(); fireEvent.click(open);
    expect(onWrite).toHaveBeenCalledWith({ event, account, inputs, basis: old });
    expect(get).toHaveBeenCalledWith(path, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id }, query: { cursor: undefined, limit: 25 } } }));
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("shows empty legacy basis without claiming zero or manufacturing one", async () => {
    reads({ ...inputs, entitlements: { items: [], next_cursor: null } }); const { onWrite } = show();
    expect(await screen.findByText(/此頁沒有已記錄舊應收/)).toBeVisible(); expect(screen.getByText(/不代表應收為零/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /並記錄收款/ })).not.toBeInTheDocument(); expect(onWrite).not.toHaveBeenCalled();
  });
  it.each([
    { ...inputs, event_id: account.id }, { ...inputs, revision_id: old.revision_id }, { ...inputs, account_id: event.id },
    { ...inputs, eligibility_scope: "ESTABLISHED" }, { ...inputs, entitlements: { items: [current, { ...old, id: current.id }], next_cursor: null } },
  ])("fails closed on a forged or stale input response", async invalid => {
    reads(invalid); const { onWrite } = show(); expect(await screen.findByRole("alert")).toHaveTextContent(/無法安全核對/);
    expect(screen.queryByRole("button", { name: /並記錄收款/ })).not.toBeInTheDocument(); expect(onWrite).not.toHaveBeenCalled();
  });
  it("keeps archived accounts readable but cannot open a posting", async () => {
    reads({ ...inputs, account_status: "ARCHIVED" }); const { onWrite } = show(false, { ...account, status: "ARCHIVED" });
    const open = await screen.findByRole("button", { name: `選擇應收 ${old.id} 並記錄收款` }); expect(open).toBeDisabled();
    expect(screen.getByText(/此帳戶已封存/)).toBeVisible(); expect(onWrite).not.toHaveBeenCalled();
  });
  it("blocks an exhausted data revision and a parent-disabled event", async () => {
    reads({ ...inputs, portfolio_data_revision: "9223372036854775807" }); const { onWrite } = show(true);
    expect(await screen.findByRole("button", { name: `選擇應收 ${old.id} 並記錄收款` })).toBeDisabled(); expect(onWrite).not.toHaveBeenCalled();
  });
  it("does not allow a stale or offline page to open a new form", async () => {
    const get = reads(); const { onWrite } = show(); const open = await screen.findByRole("button", { name: `選擇應收 ${old.id} 並記錄收款` });
    await waitFor(() => expect(open).toBeEnabled()); let deliver!: () => void;
    get.mockImplementation(async () => new Promise(resolve => { deliver = () => resolve(reply(inputs)); }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入收款依據" })); expect(open).toBeDisabled();
    await waitFor(() => expect(deliver).toBeTypeOf("function")); await act(async () => deliver()); await waitFor(() => expect(open).toBeEnabled());
    try { act(() => onlineManager.setOnline(false)); expect(open).toBeDisabled(); }
    finally { act(() => onlineManager.setOnline(true)); }
    expect(onWrite).not.toHaveBeenCalled();
  });
  it("pages explicitly and preserves each page's selected physical revision", async () => {
    const get = reads(); get.mockImplementation(async (_request, options) => {
      const query = options as { params: { query: { cursor?: string } } };
      return reply({ ...inputs, entitlements: query.params.query.cursor ? { items: [old], next_cursor: null } : { items: [current], next_cursor: "next" } });
    });
    const { onWrite } = show(); await screen.findByRole("button", { name: `選擇應收 ${current.id} 並記錄收款` });
    fireEvent.click(screen.getByRole("button", { name: "下一頁收款依據" }));
    const open = await screen.findByRole("button", { name: `選擇應收 ${old.id} 並記錄收款` }); await waitFor(() => expect(open).toBeEnabled());
    fireEvent.click(open); expect(onWrite.mock.calls[0]![0]).toMatchObject({ basis: old, inputs: { entitlements: { items: [old] } } });
  });
  it("aborts late scoped reads when the selected account changes", async () => {
    const get = reads(); let deliver!: () => void; let signal!: AbortSignal;
    get.mockImplementation(async (_request, options) => new Promise(resolve => { signal = (options as { signal: AbortSignal }).signal; deliver = () => resolve(reply(inputs)); }));
    const { cache, onWrite, view } = show(); await waitFor(() => expect(deliver).toBeTypeOf("function"));
    const firstSignal = signal; const firstDeliver = deliver;
    const other = { ...account, id: "66666666-6666-4666-8666-666666666666" };
    view.rerender(<QueryClientProvider client={cache}><DividendReceiptPreparation event={event} account={other} onWrite={onWrite} /></QueryClientProvider>);
    expect(firstSignal.aborted).toBe(true); await act(async () => firstDeliver()); expect(onWrite).not.toHaveBeenCalled();
  });
});
