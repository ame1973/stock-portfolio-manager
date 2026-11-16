import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ManualEventOverrideForm } from "./ManualEventForm";
import type { RevisionTarget } from "./event-validation";

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
const overridden: Schemas["DividendEventRecord"] = {
  ...original, current_revision_id: "55555555-5555-4555-8555-555555555555", version: 6, workflow_status: "PROVISIONAL", manual_locked: false,
  manual_override: true, decision_kind: "MANUAL", dps: "1.00000001", reference: { citation: "Synthetic override reference", url: null, url_withheld: true },
  reason: "Synthetic owner explicitly overrides these terms.",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities/{id}") return reply({ id: original.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(target: RevisionTarget = { event: original, rejection }) {
  const onSaved = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><MemoryRouter><ManualEventOverrideForm target={target} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, onSaved, view };
}
function fill() {
  fireEvent.change(screen.getByLabelText("事件每股金額"), { target: { value: "1.00000001" } });
  fireEvent.change(screen.getByLabelText("事件引用參考"), { target: { value: "Synthetic override reference" } });
  fireEvent.change(screen.getByLabelText("事件引用網址（選填）"), { target: { value: "https://example.test/notice?private=synthetic#page" } });
  fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: "Synthetic owner explicitly overrides these terms." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }));
}
function confirm() {
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認解除這個版本的人工鎖定/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認重新開啟已撤回的事件/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認以此覆寫明確處理所示拒絕/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認以人工覆寫方式建立新版本/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "儲存人工覆寫" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["ManualDividendOverride"]; params: { path: { event_id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}

describe("manual dividend event override", () => {
  it.each([
    [/我確認解除這個版本的人工鎖定/, /請確認解除人工鎖定/], [/我確認重新開啟已撤回的事件/, /請確認重新開啟已撤回事件/],
    [/我確認以此覆寫明確處理所示拒絕/, /請確認明確處理所示拒絕/], [/我明白新版本只使用本次填寫的引用/, /請確認新版本使用本次填寫的引用/],
    [/我確認以人工覆寫方式建立新版本/, /請確認人工覆寫/],
  ])("requires every applicable override confirmation independently", async (label, message) => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); fill(); confirm();
    const confirmation = screen.getByRole("checkbox", { name: label });
    fireEvent.click(confirmation); submit();
    expect(await screen.findByText(message)).toBeVisible();
    expect(confirmation).toHaveAttribute("aria-invalid", "true"); expect(confirmation).toHaveAccessibleDescription(message);
    expect(post).not.toHaveBeenCalled();
  });

  it("posts an explicit override with frozen revision/rejection pins and an evidence URL rather than source_url", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(overridden));
    const { cache, onSaved } = renderForm();
    expect(screen.getByLabelText("事件每股金額")).toHaveValue("92233720368.54775807");
    expect(screen.getByLabelText("事件股息幣別")).toHaveValue("USD");
    expect(screen.getByLabelText("事件引用參考")).toHaveValue("");
    expect(screen.queryByLabelText("人工事件證券", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByRole("form", { name: "人工覆寫股息事件" })).toHaveTextContent(rejection.reason);
    expect(screen.getByRole("form", { name: "人工覆寫股息事件" })).toHaveTextContent(rejection.reference.citation);
    fill(); confirm(); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(overridden));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/{event_id}/verify");
    expect(request.params.path).toEqual({ event_id: original.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"5"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual({ security_id: original.security_id, event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null,
      pay_date: null, dps: "1.00000001", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic override reference",
      evidence_url: "https://example.test/notice?private=synthetic#page", reason: "Synthetic owner explicitly overrides these terms.",
      revision_id: original.current_revision_id, resolve_rejection_id: rejection.id, confirm_unlock: true, confirm_reopen: true, decision: "MANUAL_OVERRIDE" });
    expect(request.body).not.toHaveProperty("source_url");
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(JSON.stringify(cache.getMutationCache().getAll())).not.toContain("Synthetic override reference");
  });

  it("uses null rather than inventing a rejection pin on a clear unlocked revision", async () => {
    reads(); const current = { ...original, workflow_status: "PROVISIONAL", manual_locked: false, decision_kind: "MANUAL" } as Schemas["DividendEventRecord"];
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(overridden));
    const { onSaved } = renderForm({ event: current, rejection: null }); fill();
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /我確認以人工覆寫方式建立新版本/ }));
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(overridden));
    expect(requests(post)[0]![1].body).toMatchObject({ resolve_rejection_id: null, confirm_unlock: false, confirm_reopen: false });
  });

  it.each([
    { event: { ...original, id: "bad" }, rejection }, { event: { ...original, version: 2_147_483_647 }, rejection: null },
    { event: { ...original, manual_locked: "yes" }, rejection }, { event: { ...original, manual_override: undefined }, rejection },
    { event: original, rejection: { ...rejection, revision_id: "66666666-6666-4666-8666-666666666666" } },
    { event: original, rejection: { ...rejection, reason: "" } },
  ])("does not send invalid or mismatched observed pins", async broken => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(broken as RevisionTarget); fill();
    if (broken.rejection) confirm();
    else {
      fireEvent.click(screen.getByRole("checkbox", { name: /我確認解除這個版本的人工鎖定/ }));
      fireEvent.click(screen.getByRole("checkbox", { name: /我確認重新開啟已撤回的事件/ }));
      fireEvent.click(screen.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }));
      fireEvent.click(screen.getByRole("checkbox", { name: /我確認以人工覆寫方式建立新版本/ }));
    }
    submit(); expect(await screen.findByText(/固定的事件或拒絕範圍不符/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("freezes the observed event and rejection through a lost response and same-key retry", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply(overridden));
    const { cache, onSaved, view } = renderForm(); fill(); confirm(); submit();
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    const newer: RevisionTarget = { event: { ...original, version: 6, current_revision_id: overridden.current_revision_id }, rejection: null };
    view.rerender(<QueryClientProvider client={cache}><MemoryRouter><ManualEventOverrideForm target={newer} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(overridden));
    expect(requests(post)).toHaveLength(2); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
    expect(requests(post)[1]![1].body.resolve_rejection_id).toBe(rejection.id); expect(requests(post)[1]![1].params.header["If-Match"]).toBe('"5"');
  });

  it("rotates the key only after the submitted replacement terms or intent change", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic interruption"));
    renderForm(); fill(); confirm(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("事件每股金額"), { target: { value: "1.00000002" } });
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3)); expect(requests(post)[2]![1]).toEqual(requests(post)[1]![1]);
  });

  it.each([
    { ...overridden, id: "77777777-7777-4777-8777-777777777777" }, { ...overridden, current_revision_id: original.current_revision_id },
    { ...overridden, version: 7 }, { ...overridden, dps: "1.00000002" }, { ...overridden, manual_override: false },
    { ...overridden, manual_override: "true" }, { ...overridden, manual_locked: undefined }, { ...overridden, manual_locked: 0 },
    { ...overridden, document_verification: "ESTABLISHED" },
    { ...overridden, reference: { citation: "Synthetic override reference", url: "https://example.test/notice?private=synthetic#page", url_withheld: false } },
  ])("rejects an inconsistent override response and retains the original key", async broken => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply(broken as Schemas["DividendEventRecord"])).mockResolvedValueOnce(reply(overridden));
    const { onSaved } = renderForm(); fill(); confirm(); submit();
    expect(await screen.findByText(/回覆與本次人工事件不符/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(overridden)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it.each([
    ["PRECONDITION_FAILED", "紀錄已變更"], ["DIVIDEND_REJECTION_CONFIRMATION_REQUIRED", "已有拒絕"],
    ["DIVIDEND_EVENT_PROJECTION_REQUIRED", "已有應收或投影歷史"], ["DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED", "人工鎖定"],
  ])("preserves input and explains %s without leaking server details", async (code, message) => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic private response", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const { onSaved } = renderForm(); fill(); confirm(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("事件引用參考")).toHaveValue("Synthetic override reference");
    expect(screen.queryByText(/synthetic private response/)).not.toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1); expect(onSaved).not.toHaveBeenCalled();
  });
});
