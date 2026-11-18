import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ReceiptWriteForm } from "./ReceiptWriteForm";
import type { ReceiptTarget } from "./receipt-input-validation";
import { body, created, target } from "./receipt-test-data";

type Schemas = components["schemas"];
const reply = <T,>(data: T) => ({ data, response: new Response() });
function csrf() { vi.spyOn(api, "GET").mockResolvedValue(reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" })); }
function show(value: ReceiptTarget = target) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onSaved = vi.fn(); const onCancel = vi.fn();
  const view = render(<QueryClientProvider client={cache}><ReceiptWriteForm target={value} onSaved={onSaved} onCancel={onCancel} /></QueryClientProvider>);
  return { cache, onSaved, onCancel, view };
}
function fill(currency: "USD" | "HKD" = "USD", fxMode: "UNKNOWN" | "PROVIDED" = "PROVIDED") {
  fireEvent.change(screen.getByLabelText("實收日期"), { target: { value: body.received_date } });
  fireEvent.change(screen.getByLabelText("實收幣別"), { target: { value: currency } });
  fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: body.received_amount } });
  fireEvent.change(screen.getByLabelText("實際稅額"), { target: { value: "UNKNOWN" } });
  fireEvent.change(screen.getByLabelText("實際費用"), { target: { value: "PROVIDED" } });
  fireEvent.change(screen.getByLabelText("已記錄費用"), { target: { value: "0" } });
  if (currency === "USD") {
    fireEvent.change(screen.getByLabelText("港幣歷史匯率"), { target: { value: fxMode } });
    if (fxMode === "PROVIDED") {
      fireEvent.change(screen.getByLabelText("USD 兌 HKD 匯率"), { target: { value: "7.8" } });
      fireEvent.change(screen.getByLabelText("港幣匯率來源"), { target: { value: body.fx_source } });
    }
  }
  fireEvent.change(screen.getByLabelText("收款備註"), { target: { value: body.note } });
  fireEvent.change(screen.getByLabelText("收款依據"), { target: { value: body.reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是擁有人記錄的現金/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄收款" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["ReceiptCreate"]; params: { header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}
function response(options: unknown, changes: Partial<Schemas["ReceiptCreated"]> = {}) {
  const request = options as { body: Schemas["ReceiptCreate"] };
  return reply({ ...created, ...changes, id: request.body.receipt_id });
}

describe("explicit recorded receipt creation", () => {
  it("requires explicit cash, unknown decisions and no-grant confirmation", async () => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); submit();
    expect(await screen.findByText(/請明確選擇實收幣別/)).toBeVisible();
    expect(screen.getAllByText(/請明確選擇未知或輸入金額/)).toHaveLength(2);
    expect(screen.getByText(/請確認收款記錄不證明付款或資格/)).toBeVisible();
    expect(screen.getByText(/記錄會新增現金流水/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("posts the selected historical basis, original decimals, unknown tax and explicit zero fee", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockImplementation(async (_path, options) => response(options));
    const { onSaved, cache } = show(); fill(); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const [path, request] = requests(post)[0]!; expect(path).toBe("/api/v1/dividends/receipts");
    expect(request.params.header).toMatchObject({ "If-Match": '"3"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual({ ...body, receipt_id: request.body.receipt_id });
    expect(request.body.receipt_id).toMatch(/^[0-9a-f-]{36}$/); expect(request.body.tax).toBeNull(); expect(request.body.fee).toBe("0");
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("keeps the receipt identity on changed uncertain input and the key on decimal-equivalent retries", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response")); show(); fill(); submit();
    await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: "006.125000" } }); submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const first = requests(post)[0]![1]; const equivalent = requests(post)[1]![1];
    expect(equivalent.params.header["Idempotency-Key"]).toBe(first.params.header["Idempotency-Key"]); expect(equivalent.body.receipt_id).toBe(first.body.receipt_id);
    fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: "6.126" } }); submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    const changed = requests(post)[2]![1]; expect(changed.params.header["Idempotency-Key"]).not.toBe(first.params.header["Idempotency-Key"]);
    expect(changed.body.receipt_id).toBe(first.body.receipt_id);
  });
  it("freezes the original target across a parent refresh and retries the same key", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockImplementationOnce(async (_path, options) => response(options));
    const { cache, onSaved, view } = show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    view.rerender(<QueryClientProvider client={cache}><ReceiptWriteForm target={{ ...target, event: { ...target.event, version: 4 } }} onSaved={onSaved} onCancel={vi.fn()} /></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it("does not treat a mismatched response as confirmation or rotate the uncertain key", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockImplementationOnce(async (_path, options) => response(options, { account_version: "1" }))
      .mockImplementationOnce(async (_path, options) => response(options));
    const { onSaved } = show(); fill(); submit(); expect(await screen.findByText(/不會假定未儲存/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it("keeps an explicit unknown USD rate distinct from mathematical HKD identity", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockImplementation(async (_path, options) => response(options, { fx_rate_hkd: null, fx_source: null, received_hkd: null }));
    const { onSaved } = show(); fill("USD", "UNKNOWN"); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(requests(post)[0]![1].body).toMatchObject({ fx_rate_hkd: null, fx_source: null, actual_hkd: null });
  });
  it("uses HKD identity without inferring a currency for the user", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockImplementation(async (_path, options) => response(options, { currency: "HKD", fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY", received_hkd: "6.125000" }));
    const { onSaved } = show(); expect(screen.getByLabelText("實收幣別")).toHaveValue(""); fill("HKD"); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(requests(post)[0]![1].body).toMatchObject({ currency: "HKD", fx_rate_hkd: null, fx_source: null });
  });
  it("clears the old FX and actual override when the currency changes", () => {
    csrf(); show(); fill(); fireEvent.click(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }));
    fireEvent.change(screen.getByLabelText("實際港幣金額"), { target: { value: "45.6" } });
    fireEvent.change(screen.getByLabelText("實際港幣金額依據"), { target: { value: "Synthetic broker actual HKD" } });
    fireEvent.change(screen.getByLabelText("實收幣別"), { target: { value: "HKD" } });
    fireEvent.change(screen.getByLabelText("實收幣別"), { target: { value: "USD" } });
    expect(screen.getByLabelText("港幣歷史匯率")).toHaveValue(""); expect(screen.queryByLabelText("實際港幣金額")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("港幣歷史匯率"), { target: { value: "PROVIDED" } });
    expect(screen.getByLabelText("USD 兌 HKD 匯率")).toHaveValue(""); expect(screen.getByLabelText("港幣匯率來源")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ })).not.toBeChecked();
  });
  it.each(["1.0000001", "1e3", "9223372036854.775808", "-9223372036854.775808", "1".repeat(51)])("rejects an invalid or nonreversible cash value %s", async amount => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); fill(); fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: amount } }); submit();
    expect(await screen.findByText(/請輸入可沖銷的精確金額/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("requires same-sign actual HKD and a separate reason", async () => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); fill(); fireEvent.click(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }));
    fireEvent.change(screen.getByLabelText("實際港幣金額"), { target: { value: "-1" } }); submit();
    expect(await screen.findByText(/正負號須與原幣一致/)).toBeVisible(); expect(screen.getByText(/實際金額依據/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("blocks repeat submit and cancellation while posting", async () => {
    csrf(); let release!: () => void; const post = vi.spyOn(api, "POST").mockImplementation((_path, options) => new Promise(resolve => { release = () => resolve(response(options)); }));
    const { onSaved, onCancel } = show(); fill(); submit(); await waitFor(() => expect(release).toBeTypeOf("function"));
    expect(screen.getByRole("button", { name: "正在記錄…" })).toBeDisabled(); expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(onCancel).not.toHaveBeenCalled(); await act(async () => release()); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(post).toHaveBeenCalledTimes(1);
  });
  it("fails closed on a changed frozen account or unselected basis", () => {
    csrf(); const post = vi.spyOn(api, "POST"); show({ ...target, basis: { ...target.basis, id: "66666666-6666-4666-8666-666666666666" } });
    expect(screen.getByRole("alert")).toHaveTextContent(/固定收款範圍無法安全核對/); expect(post).not.toHaveBeenCalled();
  });
});
