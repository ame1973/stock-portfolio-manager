import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { CandidateEventMatchForm } from "./CandidateEventMatchForm";
import type { RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
type Candidate = Schemas["CandidateRecord"];
type Event = Schemas["DividendEventRecord"];
const candidate: Candidate = {
  id: "11111111-1111-4111-8111-111111111111", revision_id: "22222222-2222-4222-8222-222222222222", version: 2, state: "OPEN",
  terms: { security_id: "33333333-3333-4333-8333-333333333333", ex_date: "2026-11-01", announce_date: null, record_date: null, pay_date: null,
    event_type: "SPECIAL", dps: "92233720368.54775807", currency: "USD", adjustment_basis: "UNKNOWN" },
  matches: [{ observation_id: "44444444-4444-4444-8444-444444444444", source_sha256: "a".repeat(64), basis_assertion_id: null, reason: "Synthetic candidate association" }],
  change_reason: "Synthetic candidate source review", created_at: "2026-11-05T00:00:00Z",
  assessment: { status: "CONFLICT", independent_origins: ["EODHD"], eligible_observation_ids: [], conflicting_fields: ["dps"], incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"],
    checks: [{ observation_id: "44444444-4444-4444-8444-444444444444", supporting_fields: ["ex_date"], conflicting_fields: ["dps"], incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"], comparison_dps: "0.00000001", comparison_basis: "UNKNOWN", basis_assertion_id: null }] },
  scope: "CANDIDATE_REVIEW_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", current_use_scope: "REVALIDATION_REQUIRED",
};
const previous: Event = {
  id: "55555555-5555-4555-8555-555555555555", current_revision_id: "66666666-6666-4666-8666-666666666666", version: 3,
  security_id: candidate.terms.security_id, event_type: "REGULAR", ex_date: "2026-10-31", announce_date: null, record_date: null, pay_date: null,
  dps: "1", currency: "HKD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false, manual_override: false,
  decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic previous event", url: null, url_withheld: false }, reason: "Synthetic previous event reason", created_at: "2026-11-04T00:00:00Z",
  scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const rejection: Schemas["RejectionDecision"] = {
  id: "77777777-7777-4777-8777-777777777777", event_id: previous.id, revision_id: previous.current_revision_id, revision_no: previous.version,
  decision: "REJECT_REVISION", reference: { citation: "Synthetic rejection citation", url: null, url_withheld: false }, reason: "Synthetic rejection reason",
  created_at: "2026-11-05T00:00:00Z", scope: "OWNER_REVISION_REJECTION_ONLY", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const result: Event = {
  ...previous, current_revision_id: "88888888-8888-4888-8888-888888888888", version: 4,
  event_type: "SPECIAL", ex_date: "2026-11-01", dps: "92233720368.54775807", currency: "USD", adjustment_basis: "UNKNOWN",
  workflow_status: "CONFLICT", decision_kind: "CANDIDATE", candidate_revision_id: candidate.revision_id, independent_origin_groups: 1,
  assessment: candidate.assessment, evidence: [{ observation_id: candidate.matches[0]!.observation_id, fields: ["ex_date"] }], reference: null,
  reason: "Synthetic existing event association", created_at: "2026-11-06T00:00:00Z",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const target = (): RevisionTarget => ({ event: structuredClone(previous), rejection: structuredClone(rejection) });
function reads(items: unknown[] = [candidate]) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/dividends/candidates") return reply({ items, next_cursor: null });
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities/{id}") return reply({ id: candidate.terms.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(observed: RevisionTarget = target(), disabled = false) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const saved = vi.fn();
  render(<QueryClientProvider client={cache}><MemoryRouter><CandidateEventMatchForm target={observed} disabled={disabled} onSaved={saved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return saved;
}
async function choose() {
  const card = await screen.findByRole("article", { name: `可選候選 ${candidate.id}` });
  fireEvent.click(within(card).getByRole("button", { name: "選擇此候選" }));
}
function confirm() {
  fireEvent.change(screen.getByLabelText("候選關聯依據"), { target: { value: result.reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認將所示候選關聯至此事件/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認以此關聯明確處理所示拒絕/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這不是文件驗證/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄候選關聯" })); }
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Schemas["CandidateEventRevision"]; params: { path: { event_id: string }; header: Record<string, string> } };
}

describe("associate an explicit candidate with an existing event", () => {
  it("loads owned open candidates for the fixed security without choosing one automatically", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); renderForm();
    const card = await screen.findByRole("article", { name: `可選候選 ${candidate.id}` });
    expect(card).toHaveTextContent(candidate.revision_id); expect(card).toHaveTextContent("USD 92233720368.54775807");
    fireEvent.click(within(card).getByText("核對候選來源及歷史比較"));
    expect(card).toHaveTextContent(candidate.matches[0]!.observation_id); expect(card).toHaveTextContent(candidate.matches[0]!.source_sha256);
    expect(card).toHaveTextContent("UNRESOLVED_ADJUSTMENT"); expect(card).toHaveTextContent("不代表文件真偽或目前基準");
    expect(screen.getByText(/不是目前或過往事件關聯查詢/)).toBeVisible();
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/candidates", expect.objectContaining({ params: { query: expect.objectContaining({ security_id: previous.security_id, state: "OPEN", limit: 25 }) } }));
    expect(screen.queryByRole("form", { name: "關聯候選至現有事件" })).not.toBeInTheDocument(); expect(post).not.toHaveBeenCalled();
  });

  it("requires separate match, rejection-resolution and no-grant confirmations", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); await choose();
    fireEvent.change(screen.getByLabelText("候選關聯依據"), { target: { value: result.reason } }); submit();
    expect(await screen.findByText("請確認關聯所示候選與事件。" )).toBeVisible();
    expect(screen.getByText("請確認明確處理所示拒絕。" )).toBeVisible();
    expect(screen.getByText("請確認這不是文件驗證或財務資格。" )).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("freezes candidate, event and rejection pins and validates the successor", async () => {
    const mutable = structuredClone(candidate); reads([mutable]); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result));
    const observed = target(); const saved = renderForm(observed); await choose(); confirm();
    mutable.version = 9; mutable.revision_id = "99999999-9999-4999-8999-999999999999";
    observed.event.version = 12; observed.event.current_revision_id = "99999999-9999-4999-8999-999999999999"; observed.rejection!.id = candidate.id;
    submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(result));
    expect(post).toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/from-candidate", expect.anything());
    expect(options(post).params.path.event_id).toBe(previous.id); expect(options(post).params.header["If-Match"]).toBe('"3"');
    expect(options(post).body).toEqual({ candidate_id: candidate.id, candidate_revision_id: candidate.revision_id, candidate_version: candidate.version,
      revision_id: previous.current_revision_id, resolve_rejection_id: rejection.id, confirmation: "MATCH_EXISTING_EVENT", reason: result.reason });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each([
    { ...target(), event: { ...previous, manual_locked: true } }, { ...target(), event: { ...previous, workflow_status: "WITHDRAWN" } },
    { ...target(), event: { ...previous, dps: "1e2" } }, { ...target(), rejection: { ...rejection, revision_id: candidate.id } },
  ])("does not load a picker for an unsafe fixed event or rejection", broken => {
    const get = reads(); renderForm(broken as RevisionTarget);
    expect(screen.getByRole("alert")).toHaveTextContent("固定事件或拒絕狀態無法核對");
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/candidates", expect.anything());
  });

  it.each([
    { items: [{ ...candidate, terms: { ...candidate.terms, security_id: previous.id } }] },
    { items: [{ ...candidate, state: "DISMISSED" }] }, { items: [candidate, candidate] },
  ])("does not offer choices from a mismatched, dismissed or duplicated candidate page", async ({ items }) => {
    reads(items); renderForm();
    expect(await screen.findByRole("alert")).toHaveTextContent("候選清單無法核對");
    expect(screen.queryByRole("button", { name: "選擇此候選" })).not.toBeInTheDocument();
  });

  it("does not choose a candidate with inconsistent source checks", async () => {
    reads([{ ...candidate, assessment: { ...candidate.assessment, checks: [] } }]); renderForm();
    const card = await screen.findByRole("article", { name: `可選候選 ${candidate.id}` });
    expect(within(card).queryByRole("button", { name: "選擇此候選" })).not.toBeInTheDocument();
    expect(card).toHaveTextContent("來源比較無法核對");
  });

  it.each([
    { ...result, id: previous.current_revision_id }, { ...result, version: 3 }, { ...result, current_revision_id: previous.current_revision_id },
    { ...result, candidate_revision_id: candidate.id }, { ...result, dps: "92233720368.54775806" }, { ...result, workflow_status: "VERIFIED" },
    { ...result, manual_locked: true }, { ...result, assessment: null }, { ...result, evidence: [] }, { ...result, eligibility_scope: "ESTABLISHED" },
  ])("does not accept a substituted event successor", async broken => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue(reply(broken)); const saved = renderForm(); await choose(); confirm(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("伺服器回覆與固定候選不符"); expect(saved).not.toHaveBeenCalled();
    expect(screen.getByLabelText("候選關聯依據")).toHaveValue(result.reason);
  });

  it("preserves a clear rejection state without inventing a resolution pin", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result));
    const saved = renderForm({ event: previous, rejection: null }); await choose();
    fireEvent.change(screen.getByLabelText("候選關聯依據"), { target: { value: result.reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /我確認將所示候選關聯至此事件/ }));
    expect(screen.queryByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白這不是文件驗證/ })); submit();
    await waitFor(() => expect(saved).toHaveBeenCalledWith(result)); expect(options(post).body.resolve_rejection_id).toBeNull();
  });

  it("does not select from a stale candidate list after refresh fails", async () => {
    const get = reads(); let calls = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/candidates") return ++calls === 1 ? reply({ items: [candidate], next_cursor: null })
        : { error: { code: "CANDIDATE_HISTORY_UNAVAILABLE", message: "private synthetic error" }, response: new Response(null, { status: 503 }) };
      return reply({ id: candidate.terms.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    });
    renderForm(); const select = await screen.findByRole("button", { name: "選擇此候選" }); expect(select).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重新載入可選候選" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("候選歷史無法安全核對"); expect(select).toBeDisabled();
    expect(screen.queryByText(/private synthetic error/)).not.toBeInTheDocument();
  });

  it("pages candidate choices without treating an empty page as absence of dividends", async () => {
    const get = reads(); let calls = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/candidates") return ++calls === 1
        ? reply({ items: [candidate], next_cursor: "next-candidate-page" }) : reply({ items: [], next_cursor: null });
      return reply({ id: candidate.terms.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    });
    renderForm(); await screen.findByRole("article", { name: `可選候選 ${candidate.id}` });
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    expect(await screen.findByText(/此頁沒有待審核候選/)).toHaveTextContent("不代表沒有來源或股息");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/candidates", expect.objectContaining({ params: { query: expect.objectContaining({
      security_id: previous.security_id, state: "OPEN", cursor: "next-candidate-page", limit: 25,
    }) } }));
  });

  it("disables selection while password confirmation is pending", async () => {
    reads(); renderForm(target(), true);
    expect(await screen.findByRole("button", { name: "選擇此候選" })).toBeDisabled();
  });

  it.each([
    ["DIVIDEND_EVENT_LOCKED", "事件已人工鎖定"], ["DIVIDEND_EVENT_CANDIDATE_ALREADY_USED", "此候選已關聯另一事件"],
    ["PRECONDITION_FAILED", "紀錄已變更"], ["REAUTH_REQUIRED", "請再次輸入密碼"],
    ["DIVIDEND_EVENT_PROJECTION_REQUIRED", "此事件已有應收或投影歷史"],
  ])("preserves input on %s without exposing server details", async (code, message) => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "private synthetic detail", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const saved = renderForm(); await choose(); confirm(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message); expect(screen.queryByText(/private synthetic detail/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("候選關聯依據")).toHaveValue(result.reason); expect(saved).not.toHaveBeenCalled();
  });

  it("retains an unchanged key after an uncertain response and changes it for a new intent", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic network failure")); renderForm(); await choose(); confirm();
    submit(); expect(await screen.findByText(/網絡連線中斷/)).toBeVisible(); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 0).params.header["Idempotency-Key"]).toBe(options(post, 1).params.header["Idempotency-Key"]);
    expect(options(post, 0).body).toEqual(options(post, 1).body);
    fireEvent.change(screen.getByLabelText("候選關聯依據"), { target: { value: "Synthetic different association decision" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
  });
});
