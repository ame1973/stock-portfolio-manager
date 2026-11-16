import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ManualEventRevisionForm } from "./ManualEventForm";

type Schemas = components["schemas"];
const original: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 5,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null,
  record_date: null, pay_date: null, dps: "92233720368.54775807", currency: "USD", adjustment_basis: "AS_DECLARED",
  workflow_status: "WITHDRAWN", manual_locked: true, manual_override: false, decision_kind: "WITHDRAW", candidate_revision_id: null,
  independent_origin_groups: null, assessment: null, evidence: [], reference: { citation: "Previous private reference", url: null, url_withheld: true },
  reason: "Previous synthetic owner decision.", created_at: "2026-11-04T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const rejection: Schemas["RejectionDecision"] = {
  id: "44444444-4444-4444-8444-444444444444", event_id: original.id, revision_id: original.current_revision_id, revision_no: original.version,
  decision: "REJECT_REVISION", reference: { citation: "Previous rejection reference", url: null, url_withheld: true },
  reason: "Previous synthetic rejection decision.", created_at: "2026-11-05T00:00:00Z", scope: "OWNER_REVISION_REJECTION_ONLY",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const revised: Schemas["DividendEventRecord"] = {
  ...original, current_revision_id: "55555555-5555-4555-8555-555555555555", version: 6, workflow_status: "PROVISIONAL", manual_locked: false,
  decision_kind: "MANUAL", reference: { citation: "Synthetic revised reference", url: null, url_withheld: false }, reason: "Synthetic revised decision reason.",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities/{id}") return reply({ id: original.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(target: { event: Schemas["DividendEventRecord"]; rejection: Schemas["RejectionDecision"] | null } = { event: original, rejection }) {
  const onSaved = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><MemoryRouter><ManualEventRevisionForm target={target} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, onSaved, view };
}
async function fill() {
  fireEvent.change(screen.getByLabelText("事件引用參考"), { target: { value: "Synthetic revised reference" } });
  fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: "Synthetic revised decision reason." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }));
}
function confirm() {
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認解除這個版本的人工鎖定/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認重新開啟已撤回的事件/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認以此修訂明確處理所示拒絕/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }));
}
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["ManualEventRevision"]; params: { path: { event_id: string }; header: { "Idempotency-Key": string; "If-Match": string } } }][];
}

describe("manual dividend event revision", () => {
  it("requires explicit unlock, reopen, rejection resolution and new citation scope", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(revised));
    const { onSaved } = renderForm();
    expect(screen.getByLabelText("事件每股金額")).toHaveValue("92233720368.54775807");
    expect(screen.getByLabelText("事件股息幣別")).toHaveValue("USD");
    expect(screen.getByLabelText("事件引用參考")).toHaveValue("");
    expect(screen.getByLabelText("事件引用網址（選填）")).toHaveValue("");
    expect(screen.queryByLabelText("人工事件證券", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByText(/先前引用的私密網址不會自動複製/)).toBeVisible();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/請確認解除人工鎖定/)).toBeVisible();
    expect(patch).not.toHaveBeenCalled();
    confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(revised));
    const [path, request] = requests(patch)[0]!;
    expect(path).toBe("/api/v1/dividends/events/{event_id}");
    expect(request.params.path).toEqual({ event_id: original.id });
    expect(request.params.header["If-Match"]).toBe('"5"');
    expect(request.body).toMatchObject({ security_id: original.security_id, revision_id: original.current_revision_id, resolve_rejection_id: rejection.id,
      confirm_unlock: true, confirm_reopen: true, currency: "USD", dps: "92233720368.54775807", source_url: null, citation: "Synthetic revised reference" });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each([
    [/我確認解除這個版本的人工鎖定/, /請確認解除人工鎖定/],
    [/我確認重新開啟已撤回的事件/, /請確認重新開啟已撤回事件/],
    [/我確認以此修訂明確處理所示拒絕/, /請確認明確處理所示拒絕/],
    [/我明白新版本只使用本次填寫的引用/, /請確認新版本使用本次填寫的引用/],
  ])("requires each applicable revision confirmation independently", async (label, message) => {
    reads(); const patch = vi.spyOn(api, "PATCH");
    renderForm(); await fill(); confirm();
    const confirmation = screen.getByRole("checkbox", { name: label });
    fireEvent.click(confirmation);
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(message)).toBeVisible();
    expect(confirmation).toHaveAttribute("aria-invalid", "true"); expect(confirmation).toHaveAccessibleDescription(message);
    expect(patch).not.toHaveBeenCalled();
  });

  it.each([
    { ...rejection, event_id: "66666666-6666-4666-8666-666666666666" },
    { ...rejection, revision_id: "66666666-6666-4666-8666-666666666666" },
    { ...rejection, revision_no: 6 },
    { ...rejection, scope: "GRANTED" },
    { ...rejection, reason: "" },
    { ...rejection, reason: "\u0085Synthetic rejection decision.\u0085" },
    { ...rejection, reference: null },
    { ...rejection, reference: { citation: "", url: null, url_withheld: false } },
    { ...rejection, reference: { citation: "Synthetic reference", url: "https://example.test/?private=synthetic", url_withheld: false } },
  ])("rejects mismatched or unverified target pins before any write", async bad => {
    reads(); const patch = vi.spyOn(api, "PATCH");
    const invalid = bad as Schemas["RejectionDecision"];
    renderForm({ event: original, rejection: invalid }); await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/固定的事件或拒絕範圍不符/)).toBeVisible();
    expect(patch).not.toHaveBeenCalled();
  });

  it.each([0, -1, 2_147_483_647, 2_147_483_648, NaN])("does not send an invalid or exhausted event version", async version => {
    reads(); const patch = vi.spyOn(api, "PATCH");
    renderForm({ event: { ...original, version }, rejection: { ...rejection, revision_no: version } });
    await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/固定的事件或拒絕範圍不符/)).toBeVisible();
    expect(patch).not.toHaveBeenCalled();
  });

  it("holds the exact observed event and rejection pins across changing props and an uncertain retry", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply(revised));
    const { onSaved, view, cache } = renderForm(); await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    const newer = { event: { ...original, version: 6, current_revision_id: revised.current_revision_id }, rejection: { ...rejection, id: "66666666-6666-4666-8666-666666666666" } };
    view.rerender(<QueryClientProvider client={cache}><MemoryRouter><ManualEventRevisionForm target={newer} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(revised));
    expect(requests(patch)).toHaveLength(2);
    expect(requests(patch)[1]![1]).toEqual(requests(patch)[0]![1]);
    expect(requests(patch)[1]![1].body.resolve_rejection_id).toBe(rejection.id);
    expect(requests(patch)[1]![1].params.header["If-Match"]).toBe('"5"');
  });

  it("preserves inputs when a competing event revision is rejected with 412", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue({ error: { code: "PRECONDITION_FAILED", message: "synthetic private response", request_id: "synthetic", details: {} }, response: new Response(null, { status: 412 }) });
    const { onSaved } = renderForm(); await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/紀錄已變更/)).toBeVisible();
    expect(screen.getByLabelText("事件引用參考")).toHaveValue("Synthetic revised reference");
    expect(screen.queryByText(/synthetic private response/)).not.toBeInTheDocument();
    expect(patch).toHaveBeenCalledTimes(1); expect(onSaved).not.toHaveBeenCalled();
  });

  it("surfaces the entitlement/projection fence instead of claiming revision or receipt reconciliation", async () => {
    reads();
    vi.spyOn(api, "PATCH").mockResolvedValue({ error: { code: "DIVIDEND_EVENT_PROJECTION_REQUIRED", message: "synthetic private response", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const { onSaved } = renderForm(); await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/已有應收或投影歷史/)).toBeVisible();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("rejects a substituted event or revision response without rotating the unchanged key", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValueOnce(reply({ ...revised, id: "77777777-7777-4777-8777-777777777777" })).mockResolvedValueOnce(reply(revised));
    const { onSaved } = renderForm(); await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/回覆與本次人工事件不符/)).toBeVisible();
    expect(onSaved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(revised));
    expect(requests(patch)[1]![1]).toEqual(requests(patch)[0]![1]);
  });

  it.each([{ ...revised, manual_override: undefined }, { ...revised, manual_locked: 0 }])("rejects missing or non-boolean manual flags on an ordinary revision", async broken => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockResolvedValueOnce(reply(broken as unknown as Schemas["DividendEventRecord"])).mockResolvedValueOnce(reply(revised));
    const { onSaved } = renderForm(); await fill(); confirm();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    expect(await screen.findByText(/回覆與本次人工事件不符/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(revised));
    expect(requests(patch)[1]![1]).toEqual(requests(patch)[0]![1]);
  });

  it("does not require or invent a rejection pin for an observed clear unlocked revision", async () => {
    reads();
    const current: Schemas["DividendEventRecord"] = { ...original, version: 1, workflow_status: "PROVISIONAL", manual_locked: false, decision_kind: "MANUAL" };
    const successor: Schemas["DividendEventRecord"] = { ...revised, version: 2 };
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(successor));
    const { onSaved } = renderForm({ event: current, rejection: null });
    await fill();
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }));
    fireEvent.click(screen.getByRole("button", { name: "儲存人工修訂" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(successor));
    expect(requests(patch)[0]![1].body).toMatchObject({ revision_id: current.current_revision_id, resolve_rejection_id: null, confirm_unlock: false, confirm_reopen: false });
  });
});
