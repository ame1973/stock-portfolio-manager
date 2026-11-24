import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ReceiptWriteForm } from "./ReceiptWriteForm";
import type { DirectReceiptTarget } from "./direct-receipt-input-validation";
import { body as legacyBody, created as legacyCreated, target as legacyTarget } from "./receipt-test-data";

type Schemas = components["schemas"];
const { event, account } = legacyTarget;
const target: DirectReceiptTarget = { event, account, inputs: {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, workflow_status: event.workflow_status,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_status: "ACTIVE",
  account_version: "9007199254740993", portfolio_version: "9223372036854775807", broker_version: "2", portfolio_data_revision: "9223372036854775806",
  rejection_state: "NOT_REJECTED", rejection_id: null, receipt_basis: "DIRECT_EVENT_ACCOUNT", scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY",
  history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
} };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function response(options: unknown, changes: Partial<Schemas["DirectReceiptCreated"]> = {}) {
  const request = options as { body: Schemas["DirectReceiptCreate"] };
  const result: Record<string, unknown> = { ...legacyCreated };
  for (const key of ["entitlement_id", "original_revision_id", "original_revision_no", "observed_revision_id", "observed_revision_no", "actual_hkd_override"]) delete result[key];
  return reply({ ...result, id: request.body.receipt_id, revision_id: target.inputs.revision_id, revision_no: target.inputs.revision_no,
    receipt_basis: "DIRECT_EVENT_ACCOUNT", actual_hkd: null, actual_hkd_reason: null,
    scope: "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY", ...changes });
}
function show(value: DirectReceiptTarget = target, disabled = false) {
  vi.spyOn(api, "GET").mockResolvedValue(reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" }));
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onSaved = vi.fn(); const onCancel = vi.fn();
  const view = render(<QueryClientProvider client={cache}><ReceiptWriteForm target={value} disabled={disabled} onSaved={onSaved} onCancel={onCancel} /></QueryClientProvider>);
  return { cache, onSaved, onCancel, view };
}
function fill(fence = true) {
  for (const [label, value] of [["實收日期", legacyBody.received_date], ["實收幣別", "USD"], ["實收淨額", legacyBody.received_amount],
    ["實際稅額", "UNKNOWN"], ["實際費用", "PROVIDED"], ["已記錄費用", "0"], ["港幣歷史匯率", "PROVIDED"], ["USD 兌 HKD 匯率", "7.8"],
    ["港幣匯率來源", legacyBody.fx_source!], ["收款備註", legacyBody.note], ["收款依據", legacyBody.reason]]) {
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
  }
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是擁有人記錄的現金/ }));
  if (fence) fireEvent.click(screen.getByRole("checkbox", { name: /我明白直接收款會持續阻擋/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄收款" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["DirectReceiptCreate"]; params: { header: { "Idempotency-Key": string; "If-Match": string } } }][];
}

describe("explicit direct receipt entry form", () => {
  it("requires an independent event-fence confirmation and never displays a fabricated basis", async () => {
    const post = vi.spyOn(api, "POST"); show();
    expect(screen.getByRole("form", { name: "記錄直接股息收款" })).toBeVisible();
    expect(screen.getByLabelText("實收幣別")).toHaveValue(""); expect(screen.getByLabelText("實收淨額")).toHaveValue("");
    expect(screen.queryByText(/舊應收 .*原修訂/)).not.toBeInTheDocument();
    fill(false); submit(); expect(await screen.findByText(/請另行確認事件修訂限制/)).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /我明白直接收款會持續阻擋/ })).toHaveAttribute("aria-invalid", "true");
    expect(post).not.toHaveBeenCalled();
  });
  it("posts exact direct pins without entitlement and keeps private values out of caches", async () => {
    const post = vi.spyOn(api, "POST").mockImplementation(async (_path, options) => response(options));
    const { onSaved, cache } = show(); fill(); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/direct-receipts"); expect(request.params.header["If-Match"]).toBe('"3"');
    expect(request.body).not.toHaveProperty("entitlement_id");
    expect(request.body).toMatchObject({ revision_id: event.current_revision_id, account_version: "9007199254740993", expected_data_revision: target.inputs.portfolio_data_revision,
      received_amount: "6.125", currency: "USD", tax: null, fee: "0", fx_rate_hkd: "7.8", confirm_revision_fence: true, confirmation: "RECORDED_CASH_ONLY" });
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("retains unchanged intent/key on uncertain retry and original target after parent refresh", async () => {
    const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockImplementationOnce(async (_path, options) => response(options));
    const { onSaved, onCancel, cache, view } = show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    view.rerender(<QueryClientProvider client={cache}><ReceiptWriteForm target={{ ...target, event: { ...event, version: 4 } }} onSaved={onSaved} onCancel={onCancel} disabled /></QueryClientProvider>);
    expect(screen.getByRole("button", { name: "等候密碼確認…" })).toBeDisabled();
    view.rerender(<QueryClientProvider client={cache}><ReceiptWriteForm target={{ ...target, event: { ...event, version: 4 } }} onSaved={onSaved} onCancel={onCancel} /></QueryClientProvider>);
    fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: "006.125000" } }); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const first = requests(post)[0]![1]; const repeated = requests(post)[1]![1];
    expect(repeated.params.header).toEqual(first.params.header); expect(repeated.body.receipt_id).toBe(first.body.receipt_id);
    expect(repeated.body.revision_id).toBe(first.body.revision_id);
  });
  it("does not confirm a mismatched response and retains its uncertain key", async () => {
    const post = vi.spyOn(api, "POST").mockImplementationOnce(async (_path, options) => response(options, { received_hkd: "47.775001" }))
      .mockImplementationOnce(async (_path, options) => response(options));
    const { onSaved } = show(); fill(); submit(); await screen.findByText(/不會假定未儲存/); expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it("blocks cancellation and repeated submission during posting", async () => {
    let release!: () => void;
    const post = vi.spyOn(api, "POST").mockImplementation((_path, options) => new Promise(resolve => { release = () => resolve(response(options)); }));
    const { onSaved, onCancel } = show(); fill(); submit(); await waitFor(() => expect(release).toBeTypeOf("function"));
    expect(screen.getByRole("button", { name: "正在記錄…" })).toBeDisabled(); expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(onCancel).not.toHaveBeenCalled(); await act(async () => release()); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1)); expect(post).toHaveBeenCalledTimes(1);
  });
  it.each(["HKD", "UNKNOWN", "OVERRIDE"] as const)("preserves explicit %s cash semantics in direct mode", async mode => {
    const change: Partial<Schemas["DirectReceiptCreated"]> = mode === "HKD"
      ? { currency: "HKD", fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY", received_hkd: "6.125000" }
      : mode === "UNKNOWN" ? { fx_rate_hkd: null, fx_source: null, received_hkd: null }
        : { actual_hkd: "48.000000", actual_hkd_reason: "Synthetic direct actual HKD reason", received_hkd: "48.000000" };
    const post = vi.spyOn(api, "POST").mockImplementation(async (_path, options) => response(options, change));
    const { onSaved } = show(); fill();
    if (mode === "HKD") fireEvent.change(screen.getByLabelText("實收幣別"), { target: { value: "HKD" } });
    else if (mode === "UNKNOWN") fireEvent.change(screen.getByLabelText("港幣歷史匯率"), { target: { value: "UNKNOWN" } });
    else {
      fireEvent.click(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }));
      fireEvent.change(screen.getByLabelText("實際港幣金額"), { target: { value: "48" } });
      fireEvent.change(screen.getByLabelText("實際港幣金額依據"), { target: { value: " Synthetic direct actual HKD reason " } });
    }
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const request = requests(post)[0]![1].body;
    expect(request).toMatchObject({ confirmation: "RECORDED_CASH_ONLY", confirm_revision_fence: true });
    if (mode === "OVERRIDE") expect(request).toMatchObject({ actual_hkd: "48", actual_hkd_reason: "Synthetic direct actual HKD reason" });
    else expect(request).toMatchObject({ fx_rate_hkd: null, fx_source: null, actual_hkd: null, actual_hkd_reason: null });
  });
  it("does not resend without the separate fence acknowledgment after uncertainty", async () => {
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response"));
    show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白直接收款會持續阻擋/ })); submit();
    await screen.findByText(/請另行確認事件修訂限制/); expect(post).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白直接收款會持續阻擋/ })); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it("keeps the receipt UUID but rotates the key for changed uncertain intent", async () => {
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response"));
    show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: "6.126" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const first = requests(post)[0]![1]; const changed = requests(post)[1]![1];
    expect(changed.body.receipt_id).toBe(first.body.receipt_id);
    expect(changed.params.header["Idempotency-Key"]).not.toBe(first.params.header["Idempotency-Key"]);
  });
  it.each(["0.0000001", "-9223372036854.775808", "9223372036854.775808", "1e6"])("rejects invalid direct cash %s without posting", async value => {
    const post = vi.spyOn(api, "POST"); show(); fill();
    fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value } }); submit();
    expect(await screen.findByText(/請輸入可沖銷的精確金額/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it.each([
    ["DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED", "組合資料已變更"],
    ["DIVIDEND_DIRECT_RECEIPT_ACCOUNT_ARCHIVED", "帳戶已封存"],
    ["DIVIDEND_DIRECT_RECEIPT_EVENT_WITHDRAWN", "事件已撤回"],
    ["DIVIDEND_DIRECT_RECEIPT_CONFLICT", "識別碼已使用"],
    ["DIVIDEND_DIRECT_RECEIPT_DATE_INVALID", "日期不能在未來"],
    ["DIVIDEND_DIRECT_RECEIPT_HISTORY_UNAVAILABLE", "直接收款歷史無法安全核對"],
    ["INVALID_DIRECT_RECEIPT_SCOPE", "直接收款範圍不符"],
  ])("retains inputs on private server failure %s", async (code, message) => {
    vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic-private-server-diagnostic", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const { onSaved } = show(); fill(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message!);
    expect(screen.queryByText(/synthetic-private-server-diagnostic/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("實收淨額")).toHaveValue("6.125"); expect(onSaved).not.toHaveBeenCalled();
  });
  it("fails closed on a rejected frozen direct scope", () => {
    const post = vi.spyOn(api, "POST"); show({ ...target, inputs: { ...target.inputs, rejection_state: "REJECTED", rejection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" } });
    expect(screen.getByRole("alert")).toHaveTextContent(/固定收款範圍無法安全核對/); expect(post).not.toHaveBeenCalled();
  });
});
