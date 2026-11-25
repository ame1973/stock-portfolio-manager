import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendDirectReceiptVoidPreparation } from "./DividendDirectReceiptVoidPreparation";
import { current, inputs, receipt, target, voided } from "./direct-receipt-void-test-data";
import { combinedReceipt } from "./receipt-void-test-data";

const path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/{receipt_id}/void-inputs";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function show(value = target, disabled = false) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onVoid = vi.fn();
  const view = render(<QueryClientProvider client={cache}><DividendDirectReceiptVoidPreparation {...value} onVoid={onVoid} disabled={disabled} /></QueryClientProvider>);
  return { cache, onVoid, view };
}
describe("fresh direct receipt void preparation", () => {
  it("reads direct inputs privately and freezes the selected original cash without writing", async () => {
    const get = vi.spyOn(api, "GET").mockResolvedValue(reply(inputs)); const post = vi.spyOn(api, "POST"); const { cache, onVoid } = show();
    const proceed = await screen.findByRole("button", { name: "繼續核對並作廢直接收款" }); await waitFor(() => expect(proceed).toBeEnabled());
    expect(get).toHaveBeenCalledWith(path, expect.objectContaining({ params: { path: { event_id: target.event.id, account_id: target.account.id, receipt_id: receipt.id } }, signal: expect.any(AbortSignal) }));
    expect(screen.getByText(/9007199254740993/)).toBeVisible(); expect(screen.getByText(/6.125000/)).toBeVisible();
    expect(screen.getByText(/作廢也不會解除/)).toBeVisible(); fireEvent.click(proceed); expect(onVoid).toHaveBeenCalledWith(target);
    expect(onVoid.mock.calls[0]![0].inputs).not.toBe(inputs); expect(post).not.toHaveBeenCalled();
    expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("does not request direct void inputs for a legacy receipt", async () => {
    const get = vi.spyOn(api, "GET"); const { onVoid } = show({ ...target, receipt: combinedReceipt });
    expect(await screen.findByRole("alert")).toHaveTextContent(/不能使用直接收款作廢流程/); expect(get).not.toHaveBeenCalled(); expect(onVoid).not.toHaveBeenCalled();
  });
  it("allows reviewed archived and moved accounts without interpreting them as new-cash permission", async () => {
    const original = "77777777-7777-4777-8777-777777777777";
    const value = { ...target, account: { ...target.account, status: "ARCHIVED" as const }, inputs: { ...inputs, origin_portfolio_id: original,
      current: { ...current, account_status: "ARCHIVED" as const } } };
    vi.spyOn(api, "GET").mockResolvedValue(reply(value.inputs)); show(value);
    await waitFor(() => expect(screen.getByRole("button", { name: "繼續核對並作廢直接收款" })).toBeEnabled());
    expect(screen.getByText(/帳戶已封存/)).toBeVisible(); expect(screen.getByText(/與目前組合/)).toHaveTextContent(original);
  });
  it.each(["void", "exhausted", "disabled"])("keeps %s scope non-actionable", async state => {
    const original = state === "void" ? { ...receipt, status: "VOID" as const, version: "2", voided_at: voided.voided_at,
      cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" as const } : receipt;
    const value = { ...inputs, receipt: original, current: { ...current, portfolio_data_revision: state === "exhausted" ? "9223372036854775807" : current.portfolio_data_revision } };
    vi.spyOn(api, "GET").mockResolvedValue(reply(value)); const { onVoid } = show({ ...target, receipt: original }, state === "disabled");
    expect(await screen.findByRole("button", { name: "繼續核對並作廢直接收款" })).toBeDisabled(); expect(onVoid).not.toHaveBeenCalled();
  });
  it.each([{ ...inputs, cash_entry_id: null }, { ...inputs, receipt: { ...receipt, recorded_amount: "6.125001" } },
    { ...inputs, current: { ...current, revision_id: "77777777-7777-4777-8777-777777777777" } }, { ...inputs, entitlement_id: receipt.id }])(
    "does not open a malformed or independently changed read", async value => {
      vi.spyOn(api, "GET").mockResolvedValue(reply(value)); const { onVoid } = show();
      expect(await screen.findByRole("alert")).toHaveTextContent(/無法安全核對/); expect(screen.queryByRole("button", { name: "繼續核對並作廢直接收款" })).not.toBeInTheDocument();
      expect(onVoid).not.toHaveBeenCalled();
    }
  );
  it("disables stale input throughout refresh, failure, pause and reconnect", async () => {
    const get = vi.spyOn(api, "GET").mockResolvedValue(reply(inputs)); const { onVoid } = show();
    const proceed = await screen.findByRole("button", { name: "繼續核對並作廢直接收款" }); await waitFor(() => expect(proceed).toBeEnabled());
    let deliver!: () => void; get.mockImplementation(async () => new Promise(resolve => { deliver = () => resolve(reply(inputs)); }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入直接作廢輸入" })); expect(proceed).toBeDisabled();
    await waitFor(() => expect(deliver).toBeTypeOf("function")); await act(async () => deliver()); await waitFor(() => expect(proceed).toBeEnabled());
    get.mockResolvedValueOnce({ error: { code: "DIRECT_RECEIPT_VOID_INPUTS_UNAVAILABLE", message: "synthetic-private-diagnostic" }, response: new Response() });
    fireEvent.click(screen.getByRole("button", { name: "重新載入直接作廢輸入" })); await screen.findByRole("alert"); expect(proceed).toBeDisabled();
    expect(screen.queryByText(/synthetic-private-diagnostic/)).not.toBeInTheDocument();
    try { act(() => onlineManager.setOnline(false)); expect(proceed).toBeDisabled(); }
    finally { act(() => onlineManager.setOnline(true)); }
    expect(proceed).toBeDisabled(); await act(async () => deliver()); await waitFor(() => expect(proceed).toBeEnabled()); expect(onVoid).not.toHaveBeenCalled();
  });
  it("aborts late responses after selection changes and unmount", async () => {
    let signal!: AbortSignal; let deliver!: () => void;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => new Promise(resolve => {
      signal = (options as { signal: AbortSignal }).signal; deliver = () => resolve(reply(inputs));
    }));
    const { view, onVoid, cache } = show(); await waitFor(() => expect(deliver).toBeTypeOf("function")); const first = signal; const old = deliver;
    view.rerender(<QueryClientProvider client={cache}><DividendDirectReceiptVoidPreparation {...target} receipt={{ ...receipt, id: "77777777-7777-4777-8777-777777777777" }} onVoid={onVoid} /></QueryClientProvider>);
    expect(first.aborted).toBe(true); await act(async () => old()); expect(screen.queryByRole("button", { name: "繼續核對並作廢直接收款" })).not.toBeInTheDocument();
    view.unmount(); expect(signal.aborted).toBe(true); expect(onVoid).not.toHaveBeenCalled();
  });
});
