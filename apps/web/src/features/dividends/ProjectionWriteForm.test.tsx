import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ProjectionWriteForm } from "./ProjectionWriteForm";
import type { ProjectionTarget } from "./projection-input-validation";
import { account, body, event, fee, inputs, query, record, target, tax } from "./projection-test-data";

type Schemas = components["schemas"];
const reply = <T,>(data: T) => ({ data, response: new Response() });
function csrf() { vi.spyOn(api, "GET").mockResolvedValue(reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" })); }
function show(value: ProjectionTarget = target) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onSaved = vi.fn(); const onCancel = vi.fn();
  const view = render(<QueryClientProvider client={cache}><ProjectionWriteForm target={value} onSaved={onSaved} onCancel={onCancel} /></QueryClientProvider>);
  return { cache, onSaved, onCancel, view };
}
function fill(mode: "UNKNOWN" | "PROVIDED" = "UNKNOWN") {
  fireEvent.change(screen.getByLabelText("港幣估值匯率"), { target: { value: mode } });
  if (mode === "PROVIDED") {
    fireEvent.change(screen.getByLabelText("USD 兌 HKD 匯率"), { target: { value: "7.8" } });
    fireEvent.change(screen.getByLabelText("港幣匯率來源"), { target: { value: "Synthetic rate reference" } });
  }
  fireEvent.change(screen.getByLabelText("投影依據"), { target: { value: body.reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是未驗證的記錄型投影/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白記錄投影後/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄投影" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["ProjectionWrite"]; params: { path: { event_id: string; account_id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}
const pending: Schemas["ProjectionRecord"] = { ...record, state: "NEEDS_REVIEW", recorded_shares: null, estimated_amounts: null, tax_rate: null,
  fee_original: null, fee_currency: null, fee_rate_event: null, fee_rate_source: null, fx_rate_hkd: null, fx_source: null };

describe("explicit recorded projection decisions", () => {
  it("separately requires the no-grant and persistent event-fence acknowledgments", async () => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); submit();
    expect(await screen.findByText(/請明確選擇是否提供港幣匯率/)).toBeVisible();
    expect(screen.getByText(/請確認投影不是財務資格或實收/)).toBeVisible(); expect(screen.getByText(/請明確確認目前的事件修訂限制/)).toBeVisible();
    expect(screen.getByText(/任何已記錄投影.*目前都會阻止此事件的修訂、撤回及拒絕/)).toBeVisible();
    fill(); fireEvent.click(screen.getByRole("checkbox", { name: /我明白記錄投影後/ })); submit();
    expect(await screen.findByText(/請明確確認目前的事件修訂限制/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("posts the frozen exact pins, explicit missing FX and strict fence confirmation", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(record)); const { onSaved, cache } = show(); fill(); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(record));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections");
    expect(request.params.path).toEqual({ event_id: event.id, account_id: account.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"3"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual(body); expect(request.body.fx_rate_hkd).toBeNull(); expect(request.body.fee_rate_event).toBeNull();
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("keeps an uncertain retry's exact body and key after the parent changes", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply(record));
    const { cache, onSaved, view } = show(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    const newer = { ...target, event: { ...event, version: 4 } };
    view.rerender(<QueryClientProvider client={cache}><ProjectionWriteForm target={newer} onSaved={onSaved} onCancel={vi.fn()} /></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(record)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it("keeps the key for equal decimal rates and rotates it for changed intent", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response")); show(); fill("PROVIDED"); submit();
    await screen.findByText(/網絡連線中斷/); fireEvent.change(screen.getByLabelText("USD 兌 HKD 匯率"), { target: { value: "7.800000000000" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    fireEvent.change(screen.getByLabelText("USD 兌 HKD 匯率"), { target: { value: "7.800000000001" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(requests(post)[2]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
  });
  it("does not interpret an inconsistent reply as a failed write and retains its key", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply({ ...record, account_version: "1" })).mockResolvedValueOnce(reply(record));
    const { onSaved } = show(); fill(); submit(); expect(await screen.findByText(/不會假定未儲存/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(record)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it.each(["0", "-1", "1e-6", "7.0000000000001", "9223372.036854775808", "0".repeat(51)])("does not send an invalid or rounded FX rate %s", async rate => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); fill("PROVIDED"); fireEvent.change(screen.getByLabelText("USD 兌 HKD 匯率"), { target: { value: rate } }); submit();
    expect(await screen.findByText(/請輸入有效正數/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it.each(["", "IDENTITY", "a".repeat(101), "bad\0source", "\ud800"]) ("requires a valid non-identity independent FX source", async source => {
    csrf(); const post = vi.spyOn(api, "POST"); show(); fill("PROVIDED"); fireEvent.change(screen.getByLabelText("港幣匯率來源"), { target: { value: source } }); submit();
    expect(await screen.findByText(/不能使用 IDENTITY/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("handles Python-compatible source whitespace and preserves a non-ASCII edge", async () => {
    csrf(); const given = `${String.fromCodePoint(0xfeff)}synthetic rate`; const result = { ...record, fx_rate_hkd: "7.800000000000", fx_source: given,
      estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "58.500000", quality: "OK" as const } };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result)); const { onSaved } = show(); fill("PROVIDED");
    fireEvent.change(screen.getByLabelText("港幣匯率來源"), { target: { value: `  ${given}  ` } }); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(result)); expect(requests(post)[0]![1].body.fx_source).toBe(given);
  });
  it("allows at most INT64 FX without silently rounding", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(pending)); const { onSaved } = show(); fill("PROVIDED");
    fireEvent.change(screen.getByLabelText("USD 兌 HKD 匯率"), { target: { value: "9223372.036854775807" } }); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(pending)); expect(requests(post)[0]![1].body.fx_rate_hkd).toBe("9223372.036854775807");
  });
  it("requires a separate fee conversion choice for different currencies", async () => {
    const foreign = { ...target, fee: { ...fee, selected: { ...fee.selected!, fee_currency: "HKD" as const } } };
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(pending)); const { onSaved } = show(foreign); fill("PROVIDED"); submit();
    expect(await screen.findByText(/請明確選擇是否提供費用換算率/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("費用兌事件幣別匯率"), { target: { value: "PROVIDED" } });
    fireEvent.change(screen.getByLabelText("HKD 兌 USD 匯率"), { target: { value: "0.128205128205" } });
    fireEvent.change(screen.getByLabelText("費用匯率來源"), { target: { value: "Synthetic independent fee rate" } }); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(pending));
    expect(requests(post)[0]![1].body).toMatchObject({ fx_rate_hkd: "7.8", fx_source: "Synthetic rate reference", fee_rate_event: "0.128205128205", fee_rate_source: "Synthetic independent fee rate" });
  });
  it("uses only mathematical identity for same-currency rates", async () => {
    const hkdEvent = { ...event, currency: "HKD" as const }; const hkdInputs = { ...inputs, currency: "HKD" as const };
    const hkdFee = { ...fee, selected: { ...fee.selected!, fee_currency: "HKD" as const } }; const hkdTarget = { ...target, event: hkdEvent, inputs: hkdInputs, fee: hkdFee };
    const saved = { ...record, currency: "HKD" as const, fee_currency: "HKD" as const, fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY",
      estimated_amounts: { ...record.estimated_amounts!, currency: "HKD" as const, estimated_hkd: "7.500000", quality: "OK" as const } };
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(saved)); const { onSaved } = show(hkdTarget);
    expect(screen.queryByLabelText("港幣估值匯率")).not.toBeInTheDocument(); expect(screen.queryByLabelText("費用兌事件幣別匯率")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("投影依據"), { target: { value: body.reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是未驗證的記錄型投影/ })); fireEvent.click(screen.getByRole("checkbox", { name: /我明白記錄投影後/ })); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(saved)); expect(requests(post)[0]![1].body).toMatchObject({ fx_rate_hkd: null, fee_rate_event: null });
  });
  it("keeps the form and values when current inputs race", async () => {
    csrf(); vi.spyOn(api, "POST").mockResolvedValue({ error: { code: "DIVIDEND_PROJECTION_INPUT_CHANGED", message: "private detail" }, response: new Response(null, { status: 409 }) });
    show(); fill(); submit(); expect(await screen.findByRole("alert")).toHaveTextContent("投影輸入已變更");
    expect(screen.getByLabelText("投影依據")).toHaveValue(body.reason); expect(screen.getByLabelText("港幣估值匯率")).toHaveValue("UNKNOWN");
  });
  it("blocks another submit and cancel while a write is pending", async () => {
    csrf(); let release!: () => void; const post = vi.spyOn(api, "POST").mockImplementation(() => new Promise(resolve => { release = () => resolve(reply(record)); }));
    const { onSaved, onCancel } = show(); fill(); submit(); await waitFor(() => expect(release).toBeTypeOf("function"));
    expect(screen.getByRole("button", { name: "正在記錄…" })).toBeDisabled(); expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(onCancel).not.toHaveBeenCalled(); await act(async () => release()); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(record)); expect(post).toHaveBeenCalledTimes(1);
  });
  it.each([
    { ...target, inputs: { ...inputs, rejection_state: "REJECTED" } }, { ...target, fee: { ...fee, revision_rejection: "REJECTED" } },
    { ...target, tax: { ...tax, account_version: "1" } }, { ...target, query: { ...query, as_of: "2026-11-05" } },
    { ...target, account: { ...account, id: event.id } },
  ])("fails closed on an unsafe frozen scope %#", broken => {
    csrf(); const post = vi.spyOn(api, "POST"); show(broken as ProjectionTarget); expect(screen.getByRole("alert")).toHaveTextContent(/固定投影範圍無法安全核對/); expect(post).not.toHaveBeenCalled();
  });
});
