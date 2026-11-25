import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ReceiptVoidForm } from "./ReceiptVoidForm";
import type { DirectReceiptVoidTarget } from "./direct-receipt-void-validation";
import { body, current, inputs, receipt, target, voided } from "./direct-receipt-void-test-data";

type Schemas = components["schemas"];
const reply = <T,>(data: T) => ({ data, response: new Response() });
function show(value: DirectReceiptVoidTarget = target, disabled = false) {
  vi.spyOn(api, "GET").mockResolvedValue(reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" }));
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onSaved = vi.fn(); const onCancel = vi.fn(); const onPendingChange = vi.fn();
  const view = render(<QueryClientProvider client={cache}><ReceiptVoidForm target={value} onSaved={onSaved} onCancel={onCancel} disabled={disabled} onPendingChange={onPendingChange} /></QueryClientProvider>);
  return { cache, onSaved, onCancel, onPendingChange, view };
}
function fill(confirm = true) {
  fireEvent.change(screen.getByLabelText("作廢依據"), { target: { value: ` ${body.reason} ` } });
  if (confirm) fireEvent.click(screen.getByRole("checkbox", { name: /我已核對原收款/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "作廢收款並沖銷現金" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["DirectReceiptVoidWrite"]; params: { path: { id: string }; header: { "Idempotency-Key": string; "If-Match": string } } }][];
}

describe("direct recorded receipt void form", () => {
  it("posts direct original/current pins and receipt version without an entitlement", async () => {
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(voided)); const { onSaved, cache } = show();
    expect(screen.getByRole("form", { name: "作廢直接股息收款" })).toBeVisible();
    expect(screen.getByText(/作廢不會解除事件修訂/)).toBeVisible(); expect(screen.queryByText(/舊應收/)).not.toBeInTheDocument();
    fill(); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(voided));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/direct-receipts/{id}/void"); expect(request.body).toEqual(body);
    expect(request.params.path.id).toBe(receipt.id); expect(request.params.header["If-Match"]).toBe('"1"');
    expect(request.body).not.toHaveProperty("entitlement_id"); expect(request.body.account_version).toBe("9007199254740993");
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("requires explicit reversal acknowledgment with an accessible error", async () => {
    const post = vi.spyOn(api, "POST"); show(); fill(false); submit();
    expect(await screen.findByText(/請確認作廢會新增反向現金流水/)).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /我已核對原收款/ })).toHaveAttribute("aria-invalid", "true"); expect(post).not.toHaveBeenCalled();
  });
  it("freezes the target and unchanged key through uncertainty, parent refresh and reauthentication", async () => {
    const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic private diagnostic")).mockResolvedValueOnce(reply(voided));
    const { view, cache, onSaved, onCancel } = show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    view.rerender(<QueryClientProvider client={cache}><ReceiptVoidForm target={{ ...target, event: { ...target.event, version: 4 } }} onSaved={onSaved} onCancel={onCancel} disabled /></QueryClientProvider>);
    expect(screen.getByRole("button", { name: "等候密碼確認…" })).toBeDisabled();
    view.rerender(<QueryClientProvider client={cache}><ReceiptVoidForm target={{ ...target, event: { ...target.event, version: 4 } }} onSaved={onSaved} onCancel={onCancel} /></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(requests(post)[1]).toEqual(requests(post)[0]); expect(screen.queryByText(/synthetic private diagnostic/)).not.toBeInTheDocument();
  });
  it("rotates only the key when the uncertain correction reason changes", async () => {
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response"));
    show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("作廢依據"), { target: { value: "Different synthetic correction reason" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2)); const first = requests(post)[0]![1]; const next = requests(post)[1]![1];
    expect(next.params.path).toEqual(first.params.path); expect(next.params.header["If-Match"]).toBe(first.params.header["If-Match"]);
    expect(next.params.header["Idempotency-Key"]).not.toBe(first.params.header["Idempotency-Key"]);
  });
  it("blocks repeated submission and cancellation while the response is held", async () => {
    let release!: () => void; const post = vi.spyOn(api, "POST").mockImplementation(async () => new Promise(resolve => { release = () => resolve(reply(voided)); }));
    const { onSaved, onCancel, onPendingChange } = show(); fill(); submit(); await waitFor(() => expect(release).toBeTypeOf("function"));
    expect(screen.getByRole("button", { name: "正在作廢…" })).toBeDisabled(); expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(onCancel).not.toHaveBeenCalled(); expect(onPendingChange).toHaveBeenCalledWith(true);
    await act(async () => release()); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(post).toHaveBeenCalledTimes(1);
    expect(onPendingChange).toHaveBeenLastCalledWith(false);
  });
  it.each([{ id: "77777777-7777-4777-8777-777777777777" }, { reversal_entry_id: inputs.cash_entry_id },
    { original_revision_no: receipt.revision_no + 1 }, { portfolio_data_revision: "0" }, { entitlement_id: receipt.id }, { scope: "CURRENT" }])(
    "retains uncertain intent when the response is mismatched", async change => {
      const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply({ ...voided, ...change })).mockResolvedValueOnce(reply(voided));
      const { onSaved } = show(); fill(); submit(); await screen.findByText(/不會假定未儲存/); expect(onSaved).not.toHaveBeenCalled();
      submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(requests(post)[1]).toEqual(requests(post)[0]);
    }
  );
  it("retains archived and moved-account cash without repricing or creating a legacy basis", async () => {
    const origin = "77777777-7777-4777-8777-777777777777";
    const original = { ...receipt, recorded_amount: "-6.125", received_hkd: null, fx_rate_hkd: null };
    const value: DirectReceiptVoidTarget = { ...target, account: { ...target.account, status: "ARCHIVED" }, receipt: original,
      inputs: { ...inputs, receipt: original, origin_portfolio_id: origin, current: { ...current, account_status: "ARCHIVED" } } };
    vi.spyOn(api, "POST").mockResolvedValue(reply({ ...voided, origin_portfolio_id: origin })); const { onSaved } = show(value);
    expect(screen.getByText(/港幣金額未知/)).toBeVisible(); expect(screen.getByText(/原記錄港幣匯率未知/)).toBeVisible();
    expect(screen.getByText(/此帳戶已封存/)).toBeVisible(); expect(screen.getByText(/原組合與目前組合不同/)).toBeVisible();
    expect(screen.getByText(/-6.125/)).toBeVisible(); fill(); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });
  it.each([
    ["DIRECT_RECEIPT_VOID_UNAVAILABLE", "直接收款作廢無法安全核對"],
    ["DIRECT_RECEIPT_VOID_SCOPE_CHANGED", "事件、帳戶或相關組合已變更"],
    ["DIRECT_RECEIPT_VOID_CONFLICT", "直接收款已作廢"],
    ["INVALID_DIRECT_RECEIPT_VOID_SCOPE", "直接收款作廢範圍不符"],
    ["REAUTH_REQUIRED", "再次輸入密碼"], ["PRECONDITION_FAILED", "紀錄已變更"],
  ])("keeps correction inputs on private server error %s", async (code, message) => {
    vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic-private-error" }, response: new Response(null, { status: 409 }) });
    const { onSaved } = show(); fill(); submit(); expect(await screen.findByRole("alert")).toHaveTextContent(message!);
    expect(screen.queryByText(/synthetic-private-error/)).not.toBeInTheDocument(); expect(screen.getByLabelText("作廢依據")).toHaveValue(` ${body.reason} `);
    expect(onSaved).not.toHaveBeenCalled();
  });
  it.each(["short", "a".repeat(1001), "Synthetic\u0000invalid reason"])("rejects invalid correction reasons", async reason => {
    const post = vi.spyOn(api, "POST"); show(); fill(); fireEvent.change(screen.getByLabelText("作廢依據"), { target: { value: reason } }); submit();
    expect(await screen.findByText(/請提供 10 至 1000 個有效字元/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("requires acknowledgment again when it is unchecked after an uncertain response", async () => {
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response")); show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.click(screen.getByRole("checkbox", { name: /我已核對原收款/ })); submit(); await screen.findByText(/請確認作廢會新增反向現金流水/);
    expect(post).toHaveBeenCalledTimes(1);
  });
  it("rejects an exhausted frozen scope before exposing a correction form", () => {
    const post = vi.spyOn(api, "POST"); show({ ...target, inputs: { ...inputs, current: { ...current, portfolio_data_revision: "9223372036854775807" } } });
    expect(screen.getByRole("alert")).toHaveTextContent(/固定作廢範圍無法安全核對/); expect(post).not.toHaveBeenCalled();
  });
});
