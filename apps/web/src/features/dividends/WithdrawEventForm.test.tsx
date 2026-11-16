import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { WithdrawEventForm } from "./CitedEventDecisionForm";
import type { RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
const original: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 5,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null,
  record_date: "2026-11-04", pay_date: null, dps: "92233720368.54775807", currency: "USD", adjustment_basis: "UNKNOWN",
  workflow_status: "INCOMPLETE", manual_locked: true, manual_override: false, decision_kind: "MANUAL", candidate_revision_id: null,
  independent_origin_groups: null, assessment: null, evidence: [], reference: { citation: "Previous event reference", url: null, url_withheld: true },
  reason: "Previous synthetic owner decision.", created_at: "2026-11-04T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const rejection: Schemas["RejectionDecision"] = {
  id: "44444444-4444-4444-8444-444444444444", event_id: original.id, revision_id: original.current_revision_id, revision_no: original.version,
  decision: "REJECT_REVISION", reference: { citation: "Previous rejection reference", url: null, url_withheld: true },
  reason: "Previous synthetic rejection decision.", created_at: "2026-11-05T00:00:00Z", scope: "OWNER_REVISION_REJECTION_ONLY",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const withdrawn: Schemas["DividendEventRecord"] = {
  ...original, current_revision_id: "55555555-5555-4555-8555-555555555555", version: 6, workflow_status: "WITHDRAWN", manual_locked: true,
  manual_override: false, decision_kind: "WITHDRAW", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic withdrawal reference", url: null, url_withheld: true }, reason: "Synthetic owner withdraws the recorded event.",
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
  const view = render(<QueryClientProvider client={cache}><MemoryRouter><WithdrawEventForm target={target} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, onSaved, view };
}
function fill() {
  fireEvent.change(screen.getByLabelText("撤回引用參考"), { target: { value: "Synthetic withdrawal reference" } });
  fireEvent.change(screen.getByLabelText("撤回引用網址（選填）"), { target: { value: "https://example.test/notice?private=synthetic#page" } });
  fireEvent.change(screen.getByLabelText("撤回決定依據"), { target: { value: "Synthetic owner withdraws the recorded event." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認撤回所示股息事件/ }));
}
function confirmRejection() { fireEvent.click(screen.getByRole("checkbox", { name: /我確認以本次撤回決定明確處理所示拒絕/ })); }
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄撤回決定" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["DividendWithdrawal"]; params: { path: { event_id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}

describe("owner withdrawal of a dividend event", () => {
  it("requires a cited reason, explicit withdrawal and independent rejection-resolution confirmation", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); submit();
    expect(await screen.findByText(/請提供.*引用參考/)).toBeVisible();
    expect(screen.getByText("請提供至少 10 個字元的具體依據。")).toBeVisible();
    expect(screen.getByText(/請確認撤回所示事件/)).toBeVisible();
    expect(screen.getByText(/請確認明確處理所示拒絕/)).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /我確認撤回所示股息事件/ })).toHaveAccessibleDescription("請確認撤回所示事件及其限制。");
    expect(screen.getByRole("checkbox", { name: /以本次撤回決定明確處理所示拒絕/ })).toHaveAccessibleDescription("請確認明確處理所示拒絕。");
    fill(); submit(); expect(await screen.findByText(/請確認明確處理所示拒絕/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("pins the event and rejection, displays exact terms and preserves the prior lock in the checked response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(withdrawn));
    const { cache, onSaved } = renderForm();
    const form = screen.getByRole("form", { name: "撤回股息事件" });
    expect(screen.getByRole("button", { name: "記錄撤回決定" })).toHaveClass("bg-danger", "text-surface");
    expect(form).toHaveTextContent("除淨日 2026-11-03；每股 USD 92233720368.54775807");
    expect(form).toHaveTextContent("特別股息"); expect(form).toHaveTextContent("記錄：2026-11-04");
    expect(form).toHaveTextContent(rejection.id); expect(form).toHaveTextContent(rejection.reason); expect(form).toHaveTextContent(rejection.reference.citation);
    expect(form).toHaveTextContent("不會沖銷實收");
    fill(); confirmRejection();
    fireEvent.change(screen.getByLabelText("撤回引用參考"), { target: { value: "\u0085Synthetic withdrawal reference\u0085" } });
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(withdrawn));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/events/{event_id}/withdraw");
    expect(request.params.path).toEqual({ event_id: original.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"5"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual({ revision_id: original.current_revision_id, resolve_rejection_id: rejection.id, confirmation: "WITHDRAW_EVENT",
      citation: "Synthetic withdrawal reference", source_url: "https://example.test/notice?private=synthetic#page", reason: "Synthetic owner withdraws the recorded event." });
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(JSON.stringify(cache.getMutationCache().getAll())).not.toContain("Synthetic withdrawal reference");
  });

  it.each(["UNKNOWN", "SPLIT_ADJUSTED", "AS_DECLARED"] as const)("allows explicit withdrawal without converting %s basis", async basis => {
    reads(); const event = { ...original, adjustment_basis: basis, manual_locked: false };
    const result = { ...withdrawn, adjustment_basis: basis, manual_locked: false };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result));
    const { onSaved } = renderForm({ event, rejection: null }); fill(); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(result));
    expect(screen.queryByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeInTheDocument();
    expect(requests(post)[0]![1].body.resolve_rejection_id).toBeNull();
  });

  it.each([
    { event: { ...original, id: "bad" }, rejection }, { event: { ...original, version: 2_147_483_647 }, rejection: null },
    { event: { ...original, workflow_status: "WITHDRAWN" }, rejection: null }, { event: { ...original, adjustment_basis: "INVALID" }, rejection: null },
    { event: { ...original, currency: "CNY" }, rejection: null }, { event: { ...original, dps: "1e2" }, rejection: null },
    { event: { ...original, ex_date: "2026-02-30" }, rejection: null }, { event: { ...original, manual_locked: "yes" }, rejection: null },
    { event: original, rejection: { ...rejection, revision_id: "66666666-6666-4666-8666-666666666666" } },
  ])("refuses invalid, already withdrawn or mismatched observed scope before any write", async broken => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(broken as RevisionTarget); fill();
    if (broken.rejection) confirmRejection();
    submit(); expect(await screen.findByText(/固定的事件或拒絕範圍不符/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("freezes the observed target and preserves body and key through an uncertain response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply(withdrawn));
    const { cache, onSaved, view } = renderForm(); fill(); confirmRejection(); submit();
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    const newer: RevisionTarget = { event: { ...original, version: 6, current_revision_id: withdrawn.current_revision_id }, rejection: null };
    view.rerender(<QueryClientProvider client={cache}><MemoryRouter><WithdrawEventForm target={newer} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(withdrawn));
    expect(requests(post)).toHaveLength(2); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
    expect(requests(post)[1]![1].body.resolve_rejection_id).toBe(rejection.id);
    expect(requests(post)[1]![1].params.header["If-Match"]).toBe('"5"');
  });

  it("rotates the key only after a submitted withdrawal intent changes", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic interruption"));
    renderForm(); fill(); confirmRejection(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("撤回決定依據"), { target: { value: "Synthetic changed withdrawal reason." } });
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3)); expect(requests(post)[2]![1]).toEqual(requests(post)[1]![1]);
  });

  it.each([
    { ...withdrawn, id: "77777777-7777-4777-8777-777777777777" }, { ...withdrawn, current_revision_id: original.current_revision_id },
    { ...withdrawn, current_revision_id: "not-a-uuid" }, { ...withdrawn, version: 7 }, { ...withdrawn, dps: "92233720368.54775806" },
    { ...withdrawn, manual_locked: false }, { ...withdrawn, manual_locked: "yes" }, { ...withdrawn, workflow_status: "VERIFIED" },
    { ...withdrawn, manual_override: undefined }, { ...withdrawn, document_verification: "ESTABLISHED" },
    { ...withdrawn, reference: { citation: "Synthetic withdrawal reference", url: "https://example.test/notice?private=synthetic#page", url_withheld: false } },
  ])("rejects substituted terms, lock, response identity or private reference without rotating the key", async broken => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply(broken as Schemas["DividendEventRecord"])).mockResolvedValueOnce(reply(withdrawn));
    const { onSaved } = renderForm(); fill(); confirmRejection(); submit();
    expect(await screen.findByText(/回覆與本次撤回決定不符/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(withdrawn)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it.each([
    ["PRECONDITION_FAILED", "紀錄已變更"], ["DIVIDEND_REJECTION_CONFIRMATION_REQUIRED", "已有拒絕"],
    ["DIVIDEND_EVENT_PROJECTION_REQUIRED", "已有應收或投影歷史"], ["DIVIDEND_EVENT_WITHDRAWN", "已撤回"],
  ])("preserves input and explains %s without leaking server details", async (code, message) => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic private response", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const { onSaved } = renderForm(); fill(); confirmRejection(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("撤回引用參考")).toHaveValue("Synthetic withdrawal reference");
    expect(screen.queryByText(/synthetic private response/)).not.toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1); expect(onSaved).not.toHaveBeenCalled();
  });
});
