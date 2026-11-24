import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendReceiptPanel } from "./DividendReceiptPanel";
import { account, event } from "./projection-test-data";
import { inputs, receipt, voided } from "./receipt-void-test-data";

const historyPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts";
const inputPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/{receipt_id}/void-inputs";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async request => {
    if (request === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (request === historyPath) return reply({ items: [receipt], next_cursor: null });
    if (request === inputPath) return reply(inputs);
    throw new Error("Unexpected read");
  });
}
function show() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onVoid = vi.fn();
  render(<QueryClientProvider client={cache}><DividendReceiptPanel event={event} onVoid={onVoid} /></QueryClientProvider>);
  return { cache, onVoid };
}
async function chooseAccount() { fireEvent.click(await screen.findByRole("button", { name: `選擇收款紀錄帳戶 ${account.name}` })); }
async function chooseReceipt() {
  const select = await screen.findByRole("button", { name: `核對作廢收款 ${receipt.id}` }); await waitFor(() => expect(select).toBeEnabled()); fireEvent.click(select);
}

describe("selecting a recorded receipt for void", () => {
  it("requires explicit account and receipt selection before reading void inputs", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const { cache, onVoid } = show();
    expect(get).not.toHaveBeenCalledWith(historyPath, expect.anything()); expect(get).not.toHaveBeenCalledWith(inputPath, expect.anything());
    await chooseAccount(); await screen.findByText(/原事件修訂/); expect(get).not.toHaveBeenCalledWith(inputPath, expect.anything());
    await chooseReceipt(); const preparation = await screen.findByRole("region", { name: "核對收款作廢輸入" });
    const open = within(preparation).getByRole("button", { name: "繼續核對並作廢收款" }); await waitFor(() => expect(open).toBeEnabled()); fireEvent.click(open);
    expect(onVoid).toHaveBeenCalledWith({ event, account, receipt, inputs }); expect(post).not.toHaveBeenCalled();
    expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("disables a selected scope if refreshed history changes without changing its ID", async () => {
    const get = reads(); const { onVoid } = show(); await chooseAccount(); await chooseReceipt();
    const open = await screen.findByRole("button", { name: "繼續核對並作廢收款" }); await waitFor(() => expect(open).toBeEnabled());
    get.mockImplementation(async request => {
      if (request === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      if (request === historyPath) return reply({ items: [{ ...receipt, recorded_amount: "6.126000" }], next_cursor: null });
      if (request === inputPath) return reply(inputs);
      throw new Error("Unexpected read");
    });
    fireEvent.click(screen.getByRole("button", { name: "重新載入收款紀錄" }));
    expect(await screen.findByText(/所選收款與目前歷史不同/)).toBeVisible(); expect(open).toBeDisabled();
    expect(onVoid).not.toHaveBeenCalled();
  });

  it("disables actions while history is offline or independently reports another event head", async () => {
    const get = reads(); show(); await chooseAccount(); await chooseReceipt();
    const open = await screen.findByRole("button", { name: "繼續核對並作廢收款" }); await waitFor(() => expect(open).toBeEnabled());
    act(() => onlineManager.setOnline(false)); await waitFor(() => expect(open).toBeDisabled()); act(() => onlineManager.setOnline(true));
    await waitFor(() => expect(open).toBeEnabled());
    get.mockImplementation(async request => request === historyPath
      ? reply({ items: [{ ...receipt, current_revision_id: "77777777-7777-4777-8777-777777777777" }], next_cursor: null })
      : request === inputPath ? reply(inputs) : reply({ items: [account], next_cursor: null }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入收款紀錄" }));
    expect(await screen.findByText(/收款讀取與事件清單的目前版本不同/)).toBeVisible(); expect(open).toBeDisabled();
  });

  it("does not allow selecting an already-voided record", async () => {
    const get = reads(); get.mockImplementation(async request => request === historyPath
      ? reply({ items: [{ ...receipt, status: "VOID", version: "2", voided_at: voided.voided_at,
        cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" }], next_cursor: null })
      : reply({ items: [account], next_cursor: null }));
    show(); await chooseAccount(); expect(await screen.findByRole("button", { name: `核對作廢收款 ${receipt.id}` })).toBeDisabled();
    expect(get).not.toHaveBeenCalledWith(inputPath, expect.anything());
  });
});
