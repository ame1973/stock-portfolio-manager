import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendReceiptVoidPreparation } from "./DividendReceiptVoidPreparation";
import { account, event } from "./projection-test-data";
import { combinedReceipt, inputs, receipt, voided } from "./receipt-void-test-data";

type Schemas = components["schemas"];
const path = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/{receipt_id}/void-inputs";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: Schemas["ReceiptVoidInputs"] = inputs) { return vi.spyOn(api, "GET").mockResolvedValue(reply(value)); }
function show(value: Schemas["RecordedReceipt"] = receipt, selected: Schemas["Account"] = account, disabled = false) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onVoid = vi.fn();
  const view = render(<QueryClientProvider client={cache}><DividendReceiptVoidPreparation event={event} account={selected} receipt={value} onVoid={onVoid} disabled={disabled} /></QueryClientProvider>);
  return { cache, onVoid, view };
}

describe("recorded receipt void preparation", () => {
  it("never requests legacy void inputs for a direct record", async () => {
    const get = reads(); const onVoid = vi.fn();
    const direct: Schemas["CombinedRecordedReceipt"] = { ...combinedReceipt, receipt_basis: "DIRECT_EVENT_ACCOUNT", entitlement_id: null };
    render(<DividendReceiptVoidPreparation event={event} account={account} receipt={direct} onVoid={onVoid} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("直接收款不能使用舊式作廢流程");
    expect(get).not.toHaveBeenCalled(); expect(onVoid).not.toHaveBeenCalled();
  });
  it("reads the explicit selected receipt and opens only a matched fresh snapshot", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const { cache, onVoid } = show();
    const open = await screen.findByRole("button", { name: "繼續核對並作廢收款" }); await waitFor(() => expect(open).toBeEnabled());
    expect(get).toHaveBeenCalledWith(path, expect.objectContaining({ params: { path: { event_id: event.id, account_id: account.id, receipt_id: receipt.id } } }));
    expect(screen.getByText(/已記錄來源與現金稽核相符/)).toBeVisible(); fireEvent.click(open);
    expect(onVoid).toHaveBeenCalledWith({ event, account, receipt, inputs });
    expect(onVoid.mock.calls[0]![0].inputs).not.toBe(inputs); expect(post).not.toHaveBeenCalled();
    expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("keeps a missing-origin legacy receipt visible but not actionable", async () => {
    reads({ ...inputs, audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED", cash_entry_id: null, origin_portfolio_id: null });
    const { onVoid } = show();
    expect(await screen.findByText(/缺少可核對來源/)).toBeVisible();
    expect(screen.getByRole("button", { name: "繼續核對並作廢收款" })).toBeDisabled(); expect(onVoid).not.toHaveBeenCalled();
  });

  it("allows a matched archived account but blocks an exhausted revision and a void receipt", async () => {
    const archived = { ...account, status: "ARCHIVED" as const }; reads({ ...inputs, account_status: "ARCHIVED" });
    const first = show(receipt, archived); const open = await screen.findByRole("button", { name: "繼續核對並作廢收款" });
    await waitFor(() => expect(open).toBeEnabled()); expect(screen.getByText(/帳戶已封存/)).toBeVisible(); first.view.unmount(); vi.restoreAllMocks();
    reads({ ...inputs, portfolio_data_revision: "9223372036854775807" }); const second = show();
    expect(await screen.findByText(/資料版本已達上限/)).toBeVisible(); expect(screen.getByRole("button", { name: "繼續核對並作廢收款" })).toBeDisabled(); second.view.unmount(); vi.restoreAllMocks();
    const oldReceipt = { ...receipt, status: "VOID" as const, version: "2", voided_at: voided.voided_at, cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" as const };
    reads({ ...inputs, status: "VOID", receipt_version: "2", voided_at: voided.voided_at }); show(oldReceipt);
    expect(await screen.findByText(/此收款已作廢/)).toBeVisible(); expect(screen.getByRole("button", { name: "繼續核對並作廢收款" })).toBeDisabled();
  });

  it("disables stale, offline, failed or parent-blocked inputs without losing the display", async () => {
    const get = reads(); const { onVoid, view, cache } = show();
    const open = await screen.findByRole("button", { name: "繼續核對並作廢收款" }); await waitFor(() => expect(open).toBeEnabled());
    act(() => onlineManager.setOnline(false)); expect(await screen.findByText(/連線已暫停/)).toBeVisible(); expect(open).toBeDisabled();
    act(() => onlineManager.setOnline(true)); await waitFor(() => expect(open).toBeEnabled());
    get.mockRejectedValue(new TypeError("synthetic-private-error")); fireEvent.click(screen.getByRole("button", { name: "重新載入作廢輸入" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/網絡連線中斷/); expect(screen.getByText(/上次載入內容/)).toBeVisible();
    expect(open).toBeDisabled(); expect(screen.queryByText(/synthetic-private-error/)).not.toBeInTheDocument(); expect(onVoid).not.toHaveBeenCalled();
    get.mockResolvedValue(reply(inputs)); fireEvent.click(screen.getByRole("button", { name: "重新載入作廢輸入" })); await waitFor(() => expect(open).toBeEnabled());
    view.rerender(<QueryClientProvider client={cache}><DividendReceiptVoidPreparation event={event} account={account} receipt={receipt} onVoid={onVoid} disabled /></QueryClientProvider>);
    expect(open).toBeDisabled();
  });

  it("discards a late result after a different receipt is selected", async () => {
    const other = "77777777-7777-4777-8777-777777777777"; let release!: () => void; let signal!: AbortSignal;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => {
      const read = options as { signal: AbortSignal; params: { path: { receipt_id: string } } };
      if (read.params.path.receipt_id === receipt.id) return new Promise(resolve => { signal = read.signal; release = () => resolve(reply(inputs)); });
      return reply({ ...inputs, receipt_id: other });
    });
    const { view, cache, onVoid } = show(); await waitFor(() => expect(release).toBeTypeOf("function"));
    const next = { ...receipt, id: other };
    view.rerender(<QueryClientProvider client={cache}><DividendReceiptVoidPreparation event={event} account={account} receipt={next} onVoid={onVoid} /></QueryClientProvider>);
    await waitFor(() => expect(signal.aborted).toBe(true)); await act(async () => release());
    const open = await screen.findByRole("button", { name: "繼續核對並作廢收款" }); await waitFor(() => expect(open).toBeEnabled()); fireEvent.click(open);
    expect(onVoid).toHaveBeenCalledTimes(1); expect(onVoid.mock.calls[0]![0].receipt.id).toBe(other);
  });

  it("rejects mismatched snapshot identity without exposing its content", async () => {
    reads({ ...inputs, account_id: event.id }); const { onVoid } = show();
    expect(await screen.findByRole("alert")).toHaveTextContent(/無法安全核對/);
    expect(screen.queryByRole("button", { name: "繼續核對並作廢收款" })).not.toBeInTheDocument(); expect(onVoid).not.toHaveBeenCalled();
  });
});
