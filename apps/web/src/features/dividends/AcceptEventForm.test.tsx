import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { AcceptEventForm } from "./AcceptEventForm";
import type { RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
const original: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 5,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null,
  record_date: null, pay_date: null, dps: "92233720368.54775807", currency: "USD", adjustment_basis: "AS_DECLARED",
  workflow_status: "CONFLICT", manual_locked: false, manual_override: false, decision_kind: "CANDIDATE", candidate_revision_id: "66666666-6666-4666-8666-666666666666",
  independent_origin_groups: 1, assessment: null, evidence: [], reference: null,
  reason: "Previous synthetic owner decision.", created_at: "2026-11-04T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const rejection: Schemas["RejectionDecision"] = {
  id: "44444444-4444-4444-8444-444444444444", event_id: original.id, revision_id: original.current_revision_id, revision_no: original.version,
  decision: "REJECT_REVISION", reference: { citation: "Previous rejection reference", url: null, url_withheld: true },
  reason: "Previous synthetic rejection decision.", created_at: "2026-11-05T00:00:00Z", scope: "OWNER_REVISION_REJECTION_ONLY",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const accepted: Schemas["DividendEventRecord"] = {
  ...original, current_revision_id: "55555555-5555-4555-8555-555555555555", version: 6, workflow_status: "VERIFIED", manual_locked: true,
  manual_override: false, decision_kind: "VERIFY", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic acceptance reference", url: null, url_withheld: true }, reason: "Synthetic owner accepts the recorded terms.",
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
  const view = render(<QueryClientProvider client={cache}><MemoryRouter><AcceptEventForm target={target} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, onSaved, view };
}
function fill() {
  fireEvent.change(screen.getByLabelText("接受引用參考"), { target: { value: "Synthetic acceptance reference" } });
  fireEvent.change(screen.getByLabelText("接受引用網址（選填）"), { target: { value: "https://example.test/notice?private=synthetic#page" } });
  fireEvent.change(screen.getByLabelText("接受決定依據"), { target: { value: "Synthetic owner accepts the recorded terms." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認以人工決定接受所示事件條款/ }));
}
function confirmRejection() { fireEvent.click(screen.getByRole("checkbox", { name: /我確認以本次接受決定明確處理所示拒絕/ })); }
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄人工接受" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["AcceptDividendRevision"]; params: { path: { event_id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}

describe("owner acceptance of a dividend revision", () => {
  it("requires citation, reason and explicit acceptance and rejection-resolution confirmations", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); submit();
    expect(await screen.findByText(/請提供.*引用參考/)).toBeVisible();
    expect(screen.getByText("請提供至少 10 個字元的具體依據。")).toBeVisible();
    expect(screen.getByText(/請確認人工接受/)).toBeVisible();
    expect(screen.getByText(/請確認明確處理所示拒絕/)).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /我確認以人工決定接受所示事件條款/ })).toHaveAccessibleDescription("請確認人工接受所示條款及其限制。");
    expect(screen.getByRole("checkbox", { name: /我確認以本次接受決定明確處理所示拒絕/ })).toHaveAccessibleDescription("請確認明確處理所示拒絕。");
    fill(); submit();
    expect(await screen.findByText(/請確認明確處理所示拒絕/)).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });

  it("pins the observed rejection and terms, sends a new cited decision and verifies the successor", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(accepted));
    const { cache, onSaved } = renderForm();
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent("除淨日 2026-11-03；每股 USD 92233720368.54775807");
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent("特別股息");
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent("來源條款有衝突");
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent("宣告：未知；記錄：未知；付款：未知");
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent(rejection.reason);
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent(rejection.reference.citation);
    expect(screen.getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent(rejection.id);
    fill(); confirmRejection();
    fireEvent.change(screen.getByLabelText("接受引用參考"), { target: { value: "\u0085Synthetic acceptance reference\u0085" } });
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(accepted));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/{event_id}/verify");
    expect(request.params.path).toEqual({ event_id: original.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"5"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual({ revision_id: original.current_revision_id, resolve_rejection_id: rejection.id, decision: "ACCEPT_REVISION",
      citation: "Synthetic acceptance reference", evidence_url: "https://example.test/notice?private=synthetic#page", reason: "Synthetic owner accepts the recorded terms." });
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(JSON.stringify(cache.getMutationCache().getAll())).not.toContain("Synthetic acceptance reference");
  });

  it("does not invent a rejection pin when a clear state was observed", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(accepted));
    const { onSaved } = renderForm({ event: original, rejection: null }); fill(); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(accepted));
    expect(screen.queryByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeInTheDocument();
    expect(requests(post)[0]![1].body.resolve_rejection_id).toBeNull();
  });

  it.each([
    { event: { ...original, id: "bad" }, rejection }, { event: { ...original, version: 2_147_483_647 }, rejection: null },
    { event: { ...original, adjustment_basis: "UNKNOWN" }, rejection: null }, { event: { ...original, workflow_status: "WITHDRAWN" }, rejection: null },
    { event: { ...original, workflow_status: "INVALID" }, rejection: null }, { event: { ...original, currency: "CNY" }, rejection: null },
    { event: { ...original, event_type: "INVALID" }, rejection: null }, { event: { ...original, dps: "1e2" }, rejection: null },
    { event: { ...original, ex_date: "2026-02-30" }, rejection: null }, { event: { ...original, pay_date: "2026-13-01" }, rejection: null },
    { event: { ...original, manual_locked: "yes" }, rejection: null },
    { event: original, rejection: { ...rejection, revision_id: "66666666-6666-4666-8666-666666666666" } },
    { event: original, rejection: { ...rejection, eligibility_scope: "GRANTED" } },
  ])("does not send invalid, unresolved or mismatched observed scope", async broken => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(broken as RevisionTarget); fill();
    if (broken.rejection) confirmRejection();
    submit(); expect(await screen.findByText(/固定的事件或拒絕範圍不符/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("freezes the observed target and retains the exact body and key after a lost response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply(accepted));
    const { cache, onSaved, view } = renderForm(); fill(); confirmRejection(); submit();
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    const newer: RevisionTarget = { event: { ...original, version: 6, current_revision_id: accepted.current_revision_id }, rejection: null };
    view.rerender(<QueryClientProvider client={cache}><MemoryRouter><AcceptEventForm target={newer} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(accepted));
    expect(requests(post)).toHaveLength(2); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
    expect(requests(post)[1]![1].body.resolve_rejection_id).toBe(rejection.id);
    expect(requests(post)[1]![1].params.header["If-Match"]).toBe('"5"');
  });

  it("rotates the key only after the submitted acceptance intent changes", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic interruption"));
    renderForm(); fill(); confirmRejection(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("接受決定依據"), { target: { value: "Synthetic changed acceptance reason." } });
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3)); expect(requests(post)[2]![1]).toEqual(requests(post)[1]![1]);
  });

  it.each([
    { ...accepted, id: "77777777-7777-4777-8777-777777777777" }, { ...accepted, current_revision_id: original.current_revision_id },
    { ...accepted, current_revision_id: "not-a-uuid" }, { ...accepted, version: 7 }, { ...accepted, dps: "92233720368.54775806" },
    { ...accepted, security_id: "77777777-7777-4777-8777-777777777777" }, { ...accepted, manual_locked: false },
    { ...accepted, manual_locked: "yes" }, { ...accepted, manual_override: undefined },
    { ...accepted, workflow_status: "PROVISIONAL" }, { ...accepted, document_verification: "ESTABLISHED" },
    { ...accepted, reference: { citation: "Synthetic acceptance reference", url: "https://example.test/notice?private=synthetic#page", url_withheld: false } },
  ])("does not trust a substituted successor or private reference and keeps the original retry key", async broken => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply(broken as Schemas["DividendEventRecord"])).mockResolvedValueOnce(reply(accepted));
    const { onSaved } = renderForm(); fill(); confirmRejection(); submit();
    expect(await screen.findByText(/回覆與本次接受決定不符/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(accepted));
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it.each([
    ["PRECONDITION_FAILED", "紀錄已變更"], ["DIVIDEND_REJECTION_CONFIRMATION_REQUIRED", "已有拒絕"],
    ["DIVIDEND_EVENT_PROJECTION_REQUIRED", "已有應收或投影歷史"], ["DIVIDEND_EVENT_SOURCE_CHANGED", "來源"],
    ["DIVIDEND_EVENT_UNRESOLVED_BASIS", "口徑"],
  ])("preserves input and explains %s without exposing server details", async (code, message) => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic private response", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const { onSaved } = renderForm(); fill(); confirmRejection(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("接受引用參考")).toHaveValue("Synthetic acceptance reference");
    expect(screen.queryByText(/synthetic private response/)).not.toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1); expect(onSaved).not.toHaveBeenCalled();
  });
});
