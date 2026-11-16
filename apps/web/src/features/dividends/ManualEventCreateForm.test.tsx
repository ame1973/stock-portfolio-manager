import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ManualEventCreateForm } from "./ManualEventForm";

type Schemas = components["schemas"];
const security: Schemas["Security"] = { id: "11111111-1111-4111-8111-111111111111", display_ticker: "0005", exchange_code: "HKG", name: "Synthetic historical security", quote_currency: "HKD", security_type: "EQUITY", status: "DELISTED", isin: null, verified: false, version: 1 };
const created: Schemas["DividendEventRecord"] = {
  id: "22222222-2222-4222-8222-222222222222", current_revision_id: "33333333-3333-4333-8333-333333333333", version: 1,
  security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
  dps: "1.23000000", currency: "USD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false,
  manual_override: false, decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic issuer notice", url: null, url_withheld: true }, reason: "Synthetic owner decision reason.", created_at: "2026-11-04T00:00:00Z",
  scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities") return reply({ items: [security], next_cursor: null });
    if (path === "/api/v1/securities/{id}") return reply(security);
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(onSaved = vi.fn()) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter><ManualEventCreateForm onSaved={onSaved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, onSaved };
}
async function fill() {
  await screen.findByRole("option", { name: /0005 \/ HKG/ });
  fireEvent.change(screen.getByLabelText("人工事件證券", { exact: true }), { target: { value: security.id } });
  fireEvent.change(screen.getByLabelText("事件類型"), { target: { value: "SPECIAL" } });
  fireEvent.change(screen.getByLabelText("事件除淨日"), { target: { value: "2026-11-03" } });
  fireEvent.change(screen.getByLabelText("事件每股金額"), { target: { value: "0001.23" } });
  fireEvent.change(screen.getByLabelText("事件股息幣別"), { target: { value: "USD" } });
  fireEvent.change(screen.getByLabelText("事件金額口徑"), { target: { value: "AS_DECLARED" } });
  fireEvent.change(screen.getByLabelText("事件引用參考"), { target: { value: "Synthetic issuer notice" } });
  fireEvent.change(screen.getByLabelText("事件引用網址（選填）"), { target: { value: "https://example.test/event?private=synthetic#page" } });
  fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: "Synthetic owner decision reason." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }));
}
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["ManualEventCreate"]; params: { header: { "Idempotency-Key": string; "X-CSRF-Token": string } } }][];
}

describe("manual dividend event creation", () => {
  it("requires explicit security, date, amount, currency, citation and acknowledgement", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderForm();
    expect(screen.getByLabelText("事件除淨日")).toHaveValue("");
    expect(screen.getByLabelText("事件每股金額")).toHaveValue("");
    expect(screen.getByLabelText("事件股息幣別")).toHaveValue("");
    expect(screen.getByLabelText("事件金額口徑")).toHaveValue("UNKNOWN");
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    expect(await screen.findByText("請選擇人工事件證券。", { selector: "p[role=alert]" })).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });

  it("sends explicit original decimal strings and never substitutes the security quote currency", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(created));
    const { cache, onSaved } = renderForm();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(created));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/events");
    expect(request.body).toEqual({ security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
      dps: "0001.23", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic issuer notice", source_url: "https://example.test/event?private=synthetic#page", reason: "Synthetic owner decision reason." });
    expect(request.params.header["X-CSRF-Token"]).toBe("synthetic-csrf");
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(JSON.stringify(cache.getMutationCache().getAll())).not.toContain("Synthetic issuer notice");
  });

  it("validates citation and reason lengths in Unicode code points like the server", async () => {
    reads();
    const citation = "𠮷".repeat(300);
    const explanation = "𠮷".repeat(10);
    const accepted: Schemas["DividendEventRecord"] = { ...created, reference: { ...created.reference!, citation }, reason: explanation };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(accepted));
    const { onSaved } = renderForm(); await fill();
    fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: "𠮷".repeat(5) } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("至少 10 個字元");
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("事件引用參考"), { target: { value: citation } });
    fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: explanation } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(accepted));
    expect(requests(post)[0]![1].body).toMatchObject({ citation, reason: explanation });
  });

  it("matches backend whitespace normalization without changing financial strings", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(created));
    const { onSaved } = renderForm(); await fill();
    fireEvent.change(screen.getByLabelText("事件引用參考"), { target: { value: "\u0085Synthetic issuer notice\u0085" } });
    fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: "\u0085Synthetic owner decision reason.\u0085" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(created));
    expect(requests(post)[0]![1].body).toMatchObject({ citation: "Synthetic issuer notice", reason: "Synthetic owner decision reason.", dps: "0001.23" });
  });

  it("preserves characters Python does not classify as surrounding whitespace", async () => {
    reads();
    const marker = String.fromCodePoint(0xfeff);
    const citation = `${marker}Synthetic issuer notice${marker}`;
    const explanation = `${marker}Synthetic owner decision reason.${marker}`;
    const result: Schemas["DividendEventRecord"] = { ...created, reference: { ...created.reference!, citation }, reason: explanation };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result));
    const { onSaved } = renderForm(); await fill();
    fireEvent.change(screen.getByLabelText("事件引用參考"), { target: { value: `\u0085${citation}\u0085` } });
    fireEvent.change(screen.getByLabelText("事件決定依據"), { target: { value: `\u001c${explanation}\u001f` } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(result));
    expect(requests(post)[0]![1].body).toMatchObject({ citation, reason: explanation });
  });

  it("permits an explicit Unicode reference URL within the server's code-point limit", async () => {
    reads();
    const url = `https://example.test/${"𠮷".repeat(1100)}`;
    const result: Schemas["DividendEventRecord"] = { ...created, reference: { citation: "Synthetic issuer notice", url, url_withheld: false } };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result));
    const { onSaved } = renderForm(); await fill();
    const field = screen.getByLabelText("事件引用網址（選填）") as HTMLInputElement;
    expect(field.maxLength).toBeGreaterThanOrEqual(url.length);
    fireEvent.change(field, { target: { value: url } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(result));
    expect(requests(post)[0]![1].body.source_url).toBe(url);
  });

  it("preserves explicitly entered zero and an unknown basis without fabricating a reference URL", async () => {
    reads();
    const zero: Schemas["DividendEventRecord"] = { ...created, dps: "0.00000000", adjustment_basis: "UNKNOWN", workflow_status: "INCOMPLETE", reference: { citation: "Synthetic issuer notice", url: null, url_withheld: false } };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(zero));
    const { onSaved } = renderForm(); await fill();
    fireEvent.change(screen.getByLabelText("事件每股金額"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("事件金額口徑"), { target: { value: "UNKNOWN" } });
    fireEvent.change(screen.getByLabelText("事件引用網址（選填）"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(zero));
    expect(requests(post)[0]![1].body).toMatchObject({ dps: "0", adjustment_basis: "UNKNOWN", source_url: null });
  });

  it("retains the same key and payload after a lost response and unrelated picker changes", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic dropped response")).mockResolvedValueOnce(reply(created));
    const { onSaved } = renderForm();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("搜尋人工事件證券"), { target: { value: "synthetic" } });
    await screen.findByRole("option", { name: /0005 \/ HKG/ });
    fireEvent.change(screen.getByLabelText("人工事件證券", { exact: true }), { target: { value: security.id } });
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }));
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(created));
    expect(requests(post)).toHaveLength(2);
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it("creates a different key only when a submitted decision changes", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic transport interruption"));
    renderForm(); await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("事件每股金額"), { target: { value: "1.23000001" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    expect(requests(post)[1]![1].body.dps).toBe("1.23000001");
  });

  it("rejects a substituted success response without losing the original retry intent", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply({ ...created, dps: "1.23000001" })).mockResolvedValueOnce(reply(created));
    const { onSaved } = renderForm(); await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    expect(await screen.findByText(/回覆與本次人工事件不符/)).toBeVisible();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByLabelText("事件每股金額")).toHaveValue("0001.23");
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(created));
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });

  it.each([
    null,
    { ...created, security_id: "44444444-4444-4444-8444-444444444444" },
    { ...created, version: 2 },
    { ...created, manual_locked: true },
    { ...created, reference: { ...created.reference!, url: "https://example.test/event?private=synthetic", url_withheld: false } },
    { ...created, eligibility_scope: "GRANTED" },
    { ...created, evidence: null },
  ])("does not accept an inconsistent creation response", async value => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(value as unknown as Schemas["DividendEventRecord"]));
    const { onSaved } = renderForm(); await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    expect(await screen.findByText(/回覆與本次人工事件不符/)).toBeVisible();
    expect(post).toHaveBeenCalledTimes(1);
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByLabelText("事件每股金額")).toHaveValue("0001.23");
  });

  it.each(["92233720368.54775808", "0.000000001", "1e-8", "-1", "0".repeat(51)])("rejects invalid DPS %s without a write", async value => {
    reads(); const post = vi.spyOn(api, "POST");
    renderForm(); await fill();
    fireEvent.change(screen.getByLabelText("事件每股金額"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "儲存人工事件" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });
});
