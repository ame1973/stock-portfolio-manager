import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { RejectEventForm } from "./RejectEventForm";

type Schemas = components["schemas"];
const observed: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 5,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null,
  record_date: null, pay_date: null, dps: "92233720368.54775807", currency: "USD", adjustment_basis: "AS_DECLARED",
  workflow_status: "VERIFIED", manual_locked: true, manual_override: false, decision_kind: "VERIFY", candidate_revision_id: null,
  independent_origin_groups: null, assessment: null, evidence: [], reference: { citation: "Previous private reference", url: null, url_withheld: true },
  reason: "Previous synthetic owner decision.", created_at: "2026-11-04T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities/{id}") return reply({ id: observed.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(target: Schemas["DividendEventRecord"] = observed) {
  const onSaved = vi.fn();
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><MemoryRouter><RejectEventForm target={target} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, onSaved, view };
}
function fill() {
  fireEvent.change(screen.getByLabelText("拒絕引用參考"), { target: { value: "Synthetic rejection reference" } });
  fireEvent.change(screen.getByLabelText("拒絕引用網址（選填）"), { target: { value: "https://example.test/notice?private=synthetic#page" } });
  fireEvent.change(screen.getByLabelText("拒絕決定依據"), { target: { value: "Synthetic owner rejects this revision." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認拒絕所示事件版本/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄拒絕決定" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["RejectDividendRevision"]; params: { path: { event_id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}

describe("dividend revision rejection", () => {
  it("requires a citation, reason and explicit rejection acknowledgment", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm();
    submit();
    expect(await screen.findByText(/請提供.*引用參考/)).toBeVisible();
    expect(screen.getByText("請提供至少 10 個字元的具體依據。")).toBeVisible();
    expect(screen.getByText(/請確認拒絕/)).toBeVisible();
    const confirmation = screen.getByRole("checkbox", { name: /我確認拒絕所示事件版本/ });
    expect(confirmation).toHaveAttribute("aria-invalid", "true");
    expect(confirmation).toHaveAccessibleDescription("請確認拒絕所示事件版本及其限制。");
    expect(post).not.toHaveBeenCalled();
  });

  it("pins the exact event version and physical revision and keeps private input out of storage and mutation caches", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...observed }));
    const { cache, onSaved } = renderForm();
    expect(screen.getByRole("form", { name: "拒絕股息事件版本" })).toHaveTextContent("除淨日 2026-11-03；每股 USD 92233720368.54775807");
    fill();
    fireEvent.change(screen.getByLabelText("拒絕引用參考"), { target: { value: "\u0085Synthetic rejection reference\u0085" } });
    fireEvent.change(screen.getByLabelText("拒絕決定依據"), { target: { value: "\u0085Synthetic owner rejects this revision.\u0085" } });
    submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(observed));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/{event_id}/verify");
    expect(request.params.path).toEqual({ event_id: observed.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"5"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual({ revision_id: observed.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejection reference",
      evidence_url: "https://example.test/notice?private=synthetic#page", reason: "Synthetic owner rejects this revision." });
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(JSON.stringify(cache.getMutationCache().getAll())).not.toContain("Synthetic rejection reference");
  });

  it("accepts an unchanged event response regardless of object key order", async () => {
    reads();
    const reordered = Object.fromEntries(Object.entries(observed).reverse()) as Schemas["DividendEventRecord"];
    reordered.reference = { url_withheld: true, url: null, citation: "Previous private reference" };
    vi.spyOn(api, "POST").mockResolvedValue(reply(reordered));
    const { onSaved } = renderForm(); fill(); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(reordered));
  });

  it.each([
    { ...observed, id: "bad" }, { ...observed, current_revision_id: "bad" }, { ...observed, version: 0 },
    { ...observed, version: 2_147_483_647 }, { ...observed, scope: "GRANTED" }, { ...observed, eligibility_scope: "GRANTED" },
  ])("does not write against invalid or exhausted observed pins", async broken => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(broken as Schemas["DividendEventRecord"]); fill(); submit();
    expect(await screen.findByText(/固定的事件範圍不符/)).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });

  it("freezes the observed version and preserves an unchanged key and intent after a lost response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply({ ...observed }));
    const { cache, onSaved, view } = renderForm(); fill(); submit();
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    view.rerender(<QueryClientProvider client={cache}><MemoryRouter><RejectEventForm target={{ ...observed, version: 6, current_revision_id: "44444444-4444-4444-8444-444444444444" }} onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(observed));
    expect(requests(post)).toHaveLength(2);
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
    expect(requests(post)[1]![1].body.revision_id).toBe(observed.current_revision_id);
    expect(requests(post)[1]![1].params.header["If-Match"]).toBe('"5"');
  });

  it("rotates the key only when a submitted rejection intent changes", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic interrupted response"));
    renderForm(); fill(); submit(); await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("拒絕決定依據"), { target: { value: "Synthetic changed rejection decision." } });
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(requests(post)[2]![1]).toEqual(requests(post)[1]![1]);
  });

  it.each([
    { ...observed, id: "44444444-4444-4444-8444-444444444444" }, { ...observed, version: 6 }, { ...observed, dps: "0.00000000" },
    { ...observed, reference: { ...observed.reference!, citation: "Substituted citation" } }, { ...observed, eligibility_scope: "GRANTED" },
  ])("rejects a substituted response and retains the unchanged key for retry", async broken => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply(broken as Schemas["DividendEventRecord"])).mockResolvedValueOnce(reply({ ...observed }));
    const { onSaved } = renderForm(); fill(); submit();
    expect(await screen.findByText(/回覆與固定的事件版本不符/)).toBeVisible();
    expect(onSaved).not.toHaveBeenCalled();
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(observed));
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it.each([
    ["PRECONDITION_FAILED", "紀錄已變更"],
    ["DIVIDEND_REVISION_ALREADY_REJECTED", "已有拒絕"],
    ["DIVIDEND_EVENT_PROJECTION_REQUIRED", "已有應收或投影歷史"],
    ["DIVIDEND_REJECTION_HISTORY_UNAVAILABLE", "拒絕歷史無法安全核對"],
  ])("preserves the form and explains %s without displaying private server details", async (code, message) => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic private response", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const { onSaved } = renderForm(); fill(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("拒絕引用參考")).toHaveValue("Synthetic rejection reference");
    expect(screen.queryByText(/synthetic private response/)).not.toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1); expect(onSaved).not.toHaveBeenCalled();
  });

  it("validates Unicode code points and does not accept an unsafe citation URL", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...observed }));
    const { onSaved } = renderForm(); fill();
    fireEvent.change(screen.getByLabelText("拒絕引用參考"), { target: { value: "𠮷".repeat(300) } });
    fireEvent.change(screen.getByLabelText("拒絕決定依據"), { target: { value: "𠮷".repeat(10) } });
    fireEvent.change(screen.getByLabelText("拒絕引用網址（選填）"), { target: { value: "https://user:password@example.test/" } });
    submit(); expect(await screen.findByText(/不含帳密或空白/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("拒絕引用網址（選填）"), { target: { value: "" } });
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(observed));
    expect(requests(post)[0]![1].body).toMatchObject({ citation: "𠮷".repeat(300), reason: "𠮷".repeat(10), evidence_url: null });
  });
});
