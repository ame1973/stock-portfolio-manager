import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendCandidatesPage } from "./DividendCandidatesPage";

type Candidate = components["schemas"]["CandidateRecord"];
const candidate: Candidate = {
  id: "11111111-1111-4111-8111-111111111111", revision_id: "22222222-2222-4222-8222-222222222222", version: 2, state: "OPEN",
  terms: { security_id: "33333333-3333-4333-8333-333333333333", ex_date: null, announce_date: null, record_date: null, pay_date: null,
    event_type: "UNKNOWN", dps: "92233720368.54775807", currency: null, adjustment_basis: "UNKNOWN" },
  matches: [{ observation_id: "44444444-4444-4444-8444-444444444444", source_sha256: "a".repeat(64), basis_assertion_id: null,
    reason: "Synthetic association reason" }],
  change_reason: "Synthetic candidate review", created_at: "2026-11-04T00:00:00Z",
  assessment: { status: "INCOMPLETE", independent_origins: [], eligible_observation_ids: [], conflicting_fields: ["dps"], incomplete_reasons: ["MISSING_CURRENCY"],
    checks: [{ observation_id: "44444444-4444-4444-8444-444444444444", supporting_fields: ["ex_date"], conflicting_fields: ["dps"],
      incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"], comparison_dps: null, comparison_basis: "UNKNOWN", basis_assertion_id: null }] },
  scope: "CANDIDATE_REVIEW_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", current_use_scope: "REVALIDATION_REQUIRED",
};
const older: Candidate = { ...candidate, revision_id: "55555555-5555-4555-8555-555555555555", version: 1,
  terms: { ...candidate.terms, ex_date: "2026-10-31", dps: "0", currency: "USD" }, matches: [] };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const empty = { items: [], next_cursor: null };
function reads(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (Object.hasOwn(overrides, path)) return reply(overrides[path]);
    switch (path) {
      case "/api/v1/dividends/candidates": return reply({ items: [candidate], next_cursor: null });
      case "/api/v1/dividends/candidates/{id}/revisions": return reply({ items: [candidate, older], next_cursor: null });
      case "/api/v1/securities": return reply(empty);
      case "/api/v1/securities/{id}": return reply({ id: candidate.terms.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      default: throw new Error(`Unexpected read: ${path}`);
    }
  });
}
function renderPage() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><MemoryRouter><DividendCandidatesPage /></MemoryRouter></QueryClientProvider>);
}
async function openCandidate() {
  fireEvent.click(await screen.findByRole("button", { name: "查看候選資料" }));
  return screen.getByRole("article", { name: `股息候選 ${candidate.id}` });
}

describe("dividend candidate review workspace", () => {
  it("preserves exact proposed values, unknowns and historical source boundaries", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const patch = vi.spyOn(api, "PATCH"); renderPage();
    const card = await openCandidate();
    expect(card).toHaveTextContent("除淨日 未知");
    expect(card).toHaveTextContent("幣別未知 92233720368.54775807");
    expect(card).toHaveTextContent(candidate.id); expect(card).toHaveTextContent(candidate.revision_id);
    expect(card).toHaveTextContent("不是目前或過往事件關聯查詢");
    expect(card).toHaveTextContent("Synthetic association reason"); expect(card).toHaveTextContent("a".repeat(64));
    const assessment = within(card).getByRole("region", { name: "歷史來源比較" });
    expect(assessment).toHaveTextContent("MISSING_CURRENCY"); expect(assessment).toHaveTextContent("UNRESOLVED_ADJUSTMENT");
    expect(assessment).toHaveTextContent("比較金額 未知"); expect(assessment).toHaveTextContent("不是文件驗證、持倉資格或收入");
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/candidates/{id}/revisions", expect.anything());
    expect(post).not.toHaveBeenCalled(); expect(patch).not.toHaveBeenCalled();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("opens physical revision history without conflating explicit zero with unknown", async () => {
    const get = reads(); renderPage(); const card = await openCandidate();
    fireEvent.click(within(card).getByRole("button", { name: "查看候選修訂歷史" }));
    const history = await within(card).findByRole("region", { name: "候選修訂歷史" });
    expect(await within(history).findByText(/歷史版本 1/)).toBeVisible();
    expect(history).toHaveTextContent("USD 0"); expect(history).toHaveTextContent(older.revision_id);
    expect(history).toHaveTextContent(candidate.id); expect(history).toHaveTextContent("歷史比較，不代表目前狀態");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/candidates/{id}/revisions", expect.objectContaining({ params: { path: { id: candidate.id }, query: { cursor: undefined, limit: 25 } } }));
  });

  it("offers server filtering and cursor paging without storing query text", async () => {
    const get = reads({ "/api/v1/dividends/candidates": { items: [candidate], next_cursor: "next-page" } }); renderPage();
    await screen.findByRole("article", { name: `股息候選 ${candidate.id}` });
    fireEvent.change(screen.getByLabelText("候選狀態"), { target: { value: "DISMISSED" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/dividends/candidates", expect.objectContaining({ params: { query: expect.objectContaining({ state: "DISMISSED", cursor: undefined }) } })));
    fireEvent.change(screen.getByLabelText("搜尋股息候選"), { target: { value: "synthetic" } });
    fireEvent.click(await screen.findByRole("button", { name: "下一頁" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/dividends/candidates", expect.objectContaining({ params: { query: expect.objectContaining({ q: "synthetic", cursor: "next-page" }) } })));
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("does not label an empty page as confirmed absence of dividends", async () => {
    reads({ "/api/v1/dividends/candidates": empty }); renderPage();
    expect(await screen.findByText(/尚無可顯示的候選資料/)).toHaveTextContent("不代表沒有股息");
  });

  it.each([
    { ...candidate, canonical_adoption_scope: "LOOKUP" },
    { ...candidate, scope: "CONFIRMED" },
    { ...candidate, terms: { ...candidate.terms, dps: "1e2" } },
    { ...candidate, terms: { ...candidate.terms, currency: "CNY" } },
    { ...candidate, version: 0 },
    { ...candidate, assessment: null },
    { ...candidate, matches: [{ ...candidate.matches[0], source_sha256: "wrong hash" }] },
    { ...candidate, assessment: { ...candidate.assessment, checks: [{ ...candidate.assessment.checks[0], comparison_dps: "1e2" }] } },
  ])("fails closed on inconsistent or malformed returned candidates", async broken => {
    reads({ "/api/v1/dividends/candidates": { items: [broken], next_cursor: null } }); renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("候選資料無法核對");
    expect(screen.queryByRole("article", { name: `股息候選 ${candidate.id}` })).not.toBeInTheDocument();
  });

  it.each([
    [candidate, { ...older, id: candidate.id }],
    [candidate, { ...older, id: "66666666-6666-4666-8666-666666666666", revision_id: candidate.revision_id }],
  ])("rejects duplicated stable or physical identities on a candidate page", async (...items) => {
    reads({ "/api/v1/dividends/candidates": { items, next_cursor: null } }); renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("候選資料無法核對");
    expect(screen.queryByRole("article", { name: `股息候選 ${candidate.id}` })).not.toBeInTheDocument();
  });

  it.each([
    [{ ...older, id: "66666666-6666-4666-8666-666666666666" }],
    [{ ...older, terms: { ...older.terms, security_id: "66666666-6666-4666-8666-666666666666" } }],
    [candidate, { ...older, version: candidate.version }],
    [older, candidate],
  ])("does not render a history with inconsistent candidate, security or versions", async (...items) => {
    reads({ "/api/v1/dividends/candidates/{id}/revisions": { items, next_cursor: null } });
    renderPage(); const card = await openCandidate();
    fireEvent.click(within(card).getByRole("button", { name: "查看候選修訂歷史" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent("候選資料無法核對");
    expect(within(card).queryByText(/歷史版本 1/)).not.toBeInTheDocument();
  });

  it("shows a newer history read without claiming the list and history are one snapshot", async () => {
    const successor = { ...candidate, version: 3, revision_id: "77777777-7777-4777-8777-777777777777" };
    reads({ "/api/v1/dividends/candidates/{id}/revisions": { items: [successor, candidate], next_cursor: null } });
    renderPage(); const card = await openCandidate();
    fireEvent.click(within(card).getByRole("button", { name: "查看候選修訂歷史" }));
    const history = await within(card).findByRole("region", { name: "候選修訂歷史" });
    expect(await within(history).findByText(/歷史版本 3/)).toBeVisible();
    expect(history).toHaveTextContent("不代表目前狀態或事件關聯");
  });

  it("retries an unavailable candidate history without disclosing the server message", async () => {
    const get = reads(); let attempts = 0;
    get.mockImplementation(async path => path === "/api/v1/dividends/candidates"
      ? ++attempts === 1 ? { error: { code: "CANDIDATE_HISTORY_UNAVAILABLE", message: "synthetic private reason" }, response: new Response(null, { status: 503 }) } : reply(empty)
      : reply(empty));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("候選歷史無法安全核對");
    expect(screen.queryByText(/synthetic private reason/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重試載入股息候選" }));
    expect(await screen.findByText(/尚無可顯示的候選資料/)).toBeVisible();
  });
});
