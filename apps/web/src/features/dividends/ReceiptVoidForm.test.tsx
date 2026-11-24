import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ReceiptVoidForm } from "./ReceiptVoidForm";
import { body, inputs, target, voided } from "./receipt-void-test-data";
import type { ReceiptVoidTarget } from "./receipt-void-validation";

type Schemas = components["schemas"];
const reply = <T,>(data: T) => ({ data, response: new Response() });
function csrf() { vi.spyOn(api, "GET").mockResolvedValue(reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" })); }
function show(value: ReceiptVoidTarget = target, disabled = false) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onSaved = vi.fn(); const onCancel = vi.fn(); const onPendingChange = vi.fn();
  const view = render(<QueryClientProvider client={cache}><ReceiptVoidForm target={value} onSaved={onSaved} onCancel={onCancel} disabled={disabled} onPendingChange={onPendingChange} /></QueryClientProvider>);
  return { cache, onSaved, onCancel, onPendingChange, view };
}
function fill(reason = body.reason) {
  fireEvent.change(screen.getByLabelText("作廢依據"), { target: { value: reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我已核對原收款/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "作廢收款並沖銷現金" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["ReceiptVoidWrite"]; params: { path: { id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}

describe("explicit recorded receipt void", () => {
  it("requires a cited reason and explicit reversal confirmation", async () => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); submit();
    expect(await screen.findByText(/請提供 10 至 1000 個有效字元的作廢依據/)).toBeVisible();
    expect(screen.getByText(/請確認作廢會新增反向現金流水/)).toBeVisible();
    expect(screen.getByText(/不證明實際付款、持倉資格或收入/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("posts only the frozen receipt, scope pins, reason and explicit confirmation", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(voided)); const { onSaved, cache } = show(); fill(); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(voided));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/receipts/{id}/void");
    expect(request.params.path).toEqual({ id: target.receipt.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"1"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.params.header["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(request.body).toEqual(body);
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("retains the same key for unchanged uncertain retries and rotates it for changed intent", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response")); show(); fill(); submit();
    await screen.findByText(/網絡連線中斷/); submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
    fireEvent.change(screen.getByLabelText("作廢依據"), { target: { value: "Different synthetic correction reason" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(requests(post)[2]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    expect(requests(post)[2]![1].params.path).toEqual(requests(post)[0]![1].params.path);
  });

  it("freezes the target across parent changes and retains the key after a mismatched response", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply({ ...voided, account_version: "1" })).mockResolvedValueOnce(reply(voided));
    const { onSaved, onCancel, cache, view } = show(); fill(); submit();
    expect(await screen.findByText(/不會假定未儲存/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    view.rerender(<QueryClientProvider client={cache}><ReceiptVoidForm target={{ ...target, event: { ...target.event, version: 4 } }} onSaved={onSaved} onCancel={onCancel} /></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(voided)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it("shows preserved original values, archived scope and both portfolios after an account move", () => {
    csrf(); const moved = "77777777-7777-4777-8777-777777777777";
    show({ ...target, account: { ...target.account, status: "ARCHIVED" }, inputs: { ...inputs, account_status: "ARCHIVED", origin_portfolio_id: moved } });
    expect(screen.getByText(/帳戶已封存/)).toBeVisible(); expect(screen.getByText(/原組合與目前組合不同/)).toHaveTextContent(moved);
    expect(screen.getByText(/6.125000/)).toBeVisible(); expect(screen.getByText(/7.800000000000/)).toBeVisible();
    expect(screen.getByText(/作廢不會改寫原有金額或歷史匯率/)).toBeVisible();
  });

  it.each(["short", "💰".repeat(9), "a".repeat(1001), "synthetic\0invalid", "synthetic\ud800invalid"])("rejects an invalid reason", async reason => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); fill(reason); submit();
    expect(await screen.findByText(/請提供 10 至 1000 個有效字元的作廢依據/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("matches Python whitespace and Unicode code-point length for a valid reason", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(voided)); const { onSaved } = show();
    fill(`\u001c${"💰".repeat(10)}\u0085`); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(requests(post)[0]![1].body.reason).toBe("💰".repeat(10));
  });

  it("blocks repeat submit and cancellation while posting or password confirmation is busy", async () => {
    csrf(); let release!: () => void; const post = vi.spyOn(api, "POST").mockImplementation(() => new Promise(resolve => { release = () => resolve(reply(voided)); }));
    const { onSaved, onCancel, onPendingChange } = show(); fill(); submit(); await waitFor(() => expect(release).toBeTypeOf("function"));
    expect(screen.getByRole("button", { name: "正在作廢…" })).toBeDisabled(); expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(onCancel).not.toHaveBeenCalled(); expect(onPendingChange).toHaveBeenCalledWith(true);
    await act(async () => release()); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onPendingChange).toHaveBeenCalledWith(false); expect(post).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["REAUTH_REQUIRED", /再次輸入密碼/],
    ["DIVIDEND_RECEIPT_VOID_SCOPE_CHANGED", /相關組合已變更/],
    ["DIVIDEND_RECEIPT_VOID_CONFLICT", /不會再新增沖銷/],
    ["DIVIDEND_RECEIPT_VOID_UNAVAILABLE", /不會假定未作廢/],
    ["DIVIDEND_RECEIPT_UNVERIFIED", /缺少可核對來源/],
  ])("keeps inputs and hides server diagnostics on %s", async (code, message) => {
    csrf(); vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic-private-diagnostic" }, response: new Response() });
    const { onSaved } = show(); fill(); submit();
    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.getByLabelText("作廢依據")).toHaveValue(body.reason);
    expect(screen.queryByText(/synthetic-private-diagnostic/)).not.toBeInTheDocument(); expect(onSaved).not.toHaveBeenCalled();
  });

  it("fails closed on missing origin, an already voided receipt and a disabled password operation", () => {
    csrf(); const post = vi.spyOn(api, "POST");
    const legacy = { ...inputs, audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" as const, cash_entry_id: null, origin_portfolio_id: null };
    const first = show({ ...target, inputs: legacy }); expect(screen.getByRole("alert")).toHaveTextContent(/固定作廢範圍無法安全核對/); first.view.unmount();
    const oldInputs = { ...inputs, status: "VOID" as const, receipt_version: "2", voided_at: voided.voided_at };
    const oldReceipt = { ...target.receipt, status: "VOID" as const, version: "2", voided_at: voided.voided_at,
      cash_scope: "MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED" as const };
    const second = show({ ...target, inputs: oldInputs, receipt: oldReceipt }); expect(screen.getByRole("alert")).toHaveTextContent(/固定作廢範圍無法安全核對/); second.view.unmount();
    const third = show(target, true); expect(screen.getByRole("button", { name: "等候密碼確認…" })).toBeDisabled(); third.view.unmount();
    expect(post).not.toHaveBeenCalled();
  });
});
