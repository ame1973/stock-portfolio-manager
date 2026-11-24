import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendReceiptPanel } from "./DividendReceiptPanel";
import { account, event } from "./projection-test-data";
import { reconciliation } from "./reconciliation-test-data";
import { inputs as receiptInputs } from "./receipt-test-data";

const historyPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts";
const comparisonPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/reconciliation";
const other = { ...account, id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", name: "Synthetic other account" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function setup() {
  const get = vi.spyOn(api, "GET").mockImplementation(async request => {
    if (request === "/api/v1/accounts") return reply({ items: [account, other], next_cursor: null });
    if (request === historyPath) return reply({ items: [], next_cursor: null });
    if (request === comparisonPath) return reply(reconciliation);
    throw new Error("Unexpected private read");
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><DividendReceiptPanel event={event} /></QueryClientProvider>);
  const comparisonCalls = () => (get.mock.calls as unknown as [string][]).filter(([path]) => path === comparisonPath);
  return { get, comparisonCalls, cache, ...view };
}
async function select(name = account.name) {
  fireEvent.click(await screen.findByRole("button", { name: `選擇收款紀錄帳戶 ${name}` }));
}

describe("reconciliation in recorded receipt history", () => {
  it("requires explicit account selection and expansion before reading private comparison", async () => {
    const { comparisonCalls, cache } = setup(); const post = vi.spyOn(api, "POST");
    await screen.findByRole("button", { name: `選擇收款紀錄帳戶 ${account.name}` });
    expect(comparisonCalls()).toHaveLength(0);
    await select(); expect(comparisonCalls()).toHaveLength(0);
    const button = screen.getByRole("button", { name: "比較已記錄金額" });
    expect(button).toHaveAttribute("aria-expanded", "false"); fireEvent.click(button);
    const panel = await screen.findByRole("region", { name: "已記錄股息金額比較" });
    expect(await within(panel).findByText(/已記錄收款金額少於/)).toBeVisible();
    expect(panel).toHaveTextContent("USD 11.000000"); expect(panel).toHaveTextContent("USD 6.000000");
    expect(panel).toHaveTextContent("USD 5.000000");
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "收合金額比較" }));
    expect(screen.queryByRole("region", { name: "已記錄股息金額比較" })).not.toBeInTheDocument();
  });

  it("aborts a pending read and requires reopening after choosing another account", async () => {
    const { get, comparisonCalls } = setup(); let signal!: AbortSignal; let deliver!: () => void;
    get.mockImplementation(async (request, options) => {
      if (request === "/api/v1/accounts") return reply({ items: [account, other], next_cursor: null });
      if (request === historyPath) return reply({ items: [], next_cursor: null });
      if (request === comparisonPath) return new Promise(resolve => {
        signal = (options as { signal: AbortSignal }).signal;
        deliver = () => resolve(reply(reconciliation));
      });
      throw new Error("Unexpected private read");
    });
    await select(); fireEvent.click(screen.getByRole("button", { name: "比較已記錄金額" }));
    await waitFor(() => expect(deliver).toBeTypeOf("function"));
    await select(other.name); expect(signal.aborted).toBe(true); await act(async () => deliver());
    expect(screen.queryByRole("region", { name: "已記錄股息金額比較" })).not.toBeInTheDocument();
    expect(screen.queryByText(/USD 11\.000000/)).not.toBeInTheDocument();
    expect(comparisonCalls()).toHaveLength(1);
    expect(screen.getByRole("button", { name: "比較已記錄金額" })).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps history, comparison and preparation as distinct keyed siblings", async () => {
    const errors = vi.spyOn(console, "error");
    vi.spyOn(api, "GET").mockImplementation(async request => {
      if (request === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      if (request === historyPath) return reply({ items: [], next_cursor: null });
      if (request === comparisonPath) return reply(reconciliation);
      if (request === "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/inputs") return reply(receiptInputs);
      throw new Error("Unexpected private read");
    });
    render(<DividendReceiptPanel event={event} onWrite={vi.fn()} />);
    await select(); fireEvent.click(screen.getByRole("button", { name: "比較已記錄金額" }));
    fireEvent.click(screen.getByRole("button", { name: "準備記錄收款" }));
    expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
    expect(await screen.findByRole("region", { name: "核對收款依據" })).toBeVisible();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key/i);
  });

  it("refreshes an open comparison after a projection changes", async () => {
    const { comparisonCalls, cache, rerender } = setup(); await select();
    fireEvent.click(screen.getByRole("button", { name: "比較已記錄金額" }));
    expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
    const previous = comparisonCalls().length;
    rerender(<QueryClientProvider client={cache}><DividendReceiptPanel event={event} comparisonRefresh={1} /></QueryClientProvider>);
    await waitFor(() => expect(comparisonCalls().length).toBeGreaterThan(previous));
    expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
  });

  it("refreshes the open private comparison after an explicit parent refresh", async () => {
    const { comparisonCalls, cache, rerender } = setup(); await select();
    fireEvent.click(screen.getByRole("button", { name: "比較已記錄金額" }));
    expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
    const previous = comparisonCalls().length;
    rerender(<QueryClientProvider client={cache}><DividendReceiptPanel event={event} refresh={1} /></QueryClientProvider>);
    await waitFor(() => expect(comparisonCalls().length).toBeGreaterThan(previous));
    expect(await screen.findByText(/已記錄收款金額少於/)).toBeVisible();
  });
});
