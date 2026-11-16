import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { CandidateEventCreateForm } from "./CandidateEventCreateForm";
import { validCandidateForEvent } from "./candidate-validation";

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
const event: Event = {
  id: "55555555-5555-4555-8555-555555555555", current_revision_id: "66666666-6666-4666-8666-666666666666", version: 1,
  security_id: candidate.terms.security_id, event_type: "SPECIAL", ex_date: "2026-11-01", announce_date: null, record_date: null, pay_date: null,
  dps: "92233720368.54775807", currency: "USD", adjustment_basis: "UNKNOWN", workflow_status: "CONFLICT", manual_locked: false, manual_override: false,
  decision_kind: "CANDIDATE", candidate_revision_id: candidate.revision_id, independent_origin_groups: 1, assessment: candidate.assessment,
  evidence: [{ observation_id: candidate.matches[0]!.observation_id, fields: ["ex_date"] }], reference: null,
  reason: "Synthetic distinct event decision", created_at: "2026-11-06T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities/{id}") return reply({ id: candidate.terms.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(target: Candidate = candidate) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const saved = vi.fn();
  render(<QueryClientProvider client={cache}><MemoryRouter><CandidateEventCreateForm target={target} onSaved={saved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return saved;
}
function confirm() {
  fireEvent.change(screen.getByLabelText("獨立事件決定依據"), { target: { value: event.reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我確認建立獨立股息事件/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這不是文件驗證/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "建立獨立事件" })); }
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Schemas["CandidateEventCreate"]; params: { header: Record<string, string> } };
}

describe("distinct event from an explicit candidate", () => {
  it("requires separate identity and no-grant confirmations before contacting the writer", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm();
    fireEvent.change(screen.getByLabelText("獨立事件決定依據"), { target: { value: event.reason } }); submit();
    expect(await screen.findByText("請確認建立獨立事件。")).toBeVisible();
    expect(screen.getByText("請確認這不是文件驗證或財務資格。")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByText(/不是目前或過往事件關聯查詢/)).toBeVisible();
    expect(screen.getByText(/每股 USD 92233720368.54775807/)).toBeVisible();
  });

  it("pins the observed candidate and verifies the exact event response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(event));
    const mutable = structuredClone(candidate); const saved = renderForm(mutable); confirm();
    mutable.version = 9; mutable.revision_id = "77777777-7777-4777-8777-777777777777";
    submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(event));
    expect(post).toHaveBeenCalledWith("/api/v1/dividends/events/from-candidate", expect.anything());
    expect(options(post).body).toEqual({ candidate_id: candidate.id, candidate_revision_id: candidate.revision_id, candidate_version: 2,
      confirmation: "CREATE_DISTINCT_EVENT", reason: event.reason });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each([
    { ...candidate, state: "DISMISSED" }, { ...candidate, terms: { ...candidate.terms, ex_date: null } },
    { ...candidate, terms: { ...candidate.terms, dps: null } }, { ...candidate, terms: { ...candidate.terms, currency: null } },
    { ...candidate, terms: { ...candidate.terms, dps: "1e2" } }, { ...candidate, canonical_adoption_scope: "LOOKUP" },
    { ...candidate, matches: [] },
    { ...candidate, matches: [...candidate.matches, candidate.matches[0]!] },
    { ...candidate, assessment: { ...candidate.assessment, checks: [] } },
    { ...candidate, assessment: { ...candidate.assessment, checks: [...candidate.assessment.checks, candidate.assessment.checks[0]!] } },
    { ...candidate, assessment: { ...candidate.assessment, checks: [{ ...candidate.assessment.checks[0]!, observation_id: "77777777-7777-4777-8777-777777777777" }] } },
    { ...candidate, assessment: { ...candidate.assessment, checks: [{ ...candidate.assessment.checks[0]!, basis_assertion_id: "77777777-7777-4777-8777-777777777777" }] } },
    { ...candidate, assessment: { ...candidate.assessment, eligible_observation_ids: ["77777777-7777-4777-8777-777777777777"] } },
    { ...candidate, assessment: { ...candidate.assessment, eligible_observation_ids: [candidate.matches[0]!.observation_id, candidate.matches[0]!.observation_id] } },
  ])("does not allow a dismissed, incomplete or malformed target", broken => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(broken as unknown as Candidate);
    expect(screen.getByRole("alert")).toHaveTextContent("固定候選無法核對"); expect(post).not.toHaveBeenCalled();
  });

  it("accepts explicit zero and empty evidence, or matching basis pins with eligible observations", () => {
    expect(validCandidateForEvent({ ...candidate, terms: { ...candidate.terms, dps: "0" }, matches: [],
      assessment: { ...candidate.assessment, checks: [], eligible_observation_ids: [] } })).toBe(true);
    const basisId = "77777777-7777-4777-8777-777777777777";
    expect(validCandidateForEvent({ ...candidate, matches: [{ ...candidate.matches[0]!, basis_assertion_id: basisId }],
      assessment: { ...candidate.assessment, checks: [{ ...candidate.assessment.checks[0]!, basis_assertion_id: basisId }],
        eligible_observation_ids: [candidate.matches[0]!.observation_id] } })).toBe(true);
  });

  it.each([
    { ...event, id: "not-an-id" }, { ...event, version: 2 }, { ...event, candidate_revision_id: candidate.id },
    { ...event, dps: "92233720368.54775806" }, { ...event, currency: "HKD" }, { ...event, event_type: "REGULAR" },
    { ...event, workflow_status: "VERIFIED" }, { ...event, manual_locked: true }, { ...event, manual_override: true },
    { ...event, assessment: null }, { ...event, independent_origin_groups: 0 }, { ...event, evidence: [] },
    { ...event, reference: { citation: "unexpected", url: null, url_withheld: false } }, { ...event, reason: "Another synthetic reason" },
    { ...event, eligibility_scope: "ESTABLISHED" },
  ])("does not treat a substituted event response as a successful creation", broken => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue(reply(broken)); const saved = renderForm(); confirm(); submit();
    return waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("伺服器回覆與固定候選不符");
      expect(screen.getByLabelText("獨立事件決定依據")).toHaveValue(event.reason); expect(saved).not.toHaveBeenCalled();
    });
  });

  it.each([
    ["DIVIDEND_EVENT_CANDIDATE_ALREADY_USED", "此候選已關聯另一事件"],
    ["CANDIDATE_DISMISSED", "候選資料已標記排除"],
    ["PRECONDITION_FAILED", "紀錄已變更"],
    ["REAUTH_REQUIRED", "請再次輸入密碼"],
  ])("preserves input and reports %s without exposing server details", async (code, message) => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic private error", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    const saved = renderForm(); confirm(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText(/synthetic private error/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("獨立事件決定依據")).toHaveValue(event.reason); expect(saved).not.toHaveBeenCalled();
  });

  it("does not submit while password confirmation is pending", () => {
    reads(); const post = vi.spyOn(api, "POST");
    const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={cache}><MemoryRouter><CandidateEventCreateForm target={candidate} disabled onSaved={vi.fn()} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
    expect(screen.getByRole("button", { name: "等候密碼確認…" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("form", { name: "由候選建立獨立股息事件" }));
    expect(post).not.toHaveBeenCalled();
  });

  it("preserves the body and idempotency key after an uncertain response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic network failure")); renderForm(); confirm();
    submit(); expect(await screen.findByText(/網絡連線中斷/)).toBeVisible(); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 0).params.header["Idempotency-Key"]).toBe(options(post, 1).params.header["Idempotency-Key"]);
    expect(options(post, 0).body).toEqual(options(post, 1).body);
    fireEvent.change(screen.getByLabelText("獨立事件決定依據"), { target: { value: "Synthetic changed distinct event decision" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
  });
});
