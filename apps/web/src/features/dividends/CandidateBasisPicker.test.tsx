import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { CandidateBasisPicker } from "./CandidateBasisPicker";

type Basis = components["schemas"]["DividendBasisRecord"];
const securityId = "11111111-1111-4111-8111-111111111111";
const observationId = "22222222-2222-4222-8222-222222222222";
const sourceSha256 = "a".repeat(64);
const basis: Basis = {
  id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", observation_id: observationId, source_sha256: sourceSha256, security_id: securityId,
  ex_date: "2026-11-01", share_basis_date: "2026-11-02", source_basis: "AFTER_EFFECTIVE_DATE_ACTIONS", target_basis: "PRE_EX_DATE_EVENTS",
  complete: true, split_only: true, created_at: "2026-11-05T00:00:00Z", scope: "BASIS_DECLARATION_ONLY", canonical_adoption: "NOT_ADOPTED",
  canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", current_use_scope: "REVALIDATION_REQUIRED_FOR_NEW_ADOPTION", market_history: "OWNER_ASSERTED_NOT_VERIFIED",
  citation: { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", citation: "Synthetic declaration reference", reason: "Synthetic split-only scope declaration", url: null, url_withheld: true },
  actions: [{ action_id: "44444444-4444-4444-8444-444444444444", action_root_id: "55555555-5555-4555-8555-555555555555", security_id: securityId,
    observed_version: 9_007_199_254_740_992, action_type: "SPLIT", effective_date: "2026-11-01", ratio_numerator: "9223372036854775807",
    ratio_denominator: "9223372036854775806", observed_status: "CONFIRMED", observed_lifecycle_status: "CURRENT" }],
};
const page = { items: [basis], next_cursor: null };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: unknown = page) { return vi.spyOn(api, "GET").mockResolvedValue(reply(value)); }
function renderPicker(selectedId = "", usedIds: string[] = []) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const select = vi.fn(); const submit = vi.fn(event => event.preventDefault());
  render(<QueryClientProvider client={cache}><form onSubmit={submit}><CandidateBasisPicker securityId={securityId} observationId={observationId}
    sourceSha256={sourceSha256} selectedId={selectedId} usedIds={usedIds} onSelect={select} /></form></QueryClientProvider>);
  return { select, submit, cache };
}

describe("explicit candidate basis declaration selection", () => {
  it("loads only the matching source and passes the declaration identity without numeric rounding", async () => {
    const get = reads(); const { select, submit, cache } = renderPicker();
    const card = await screen.findByRole("article", { name: `可選基準 ${basis.id}` });
    expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(card).toHaveTextContent(basis.id); expect(card).toHaveTextContent(basis.observation_id); expect(card).toHaveTextContent(sourceSha256);
    expect(card).toHaveTextContent("9223372036854775807 / 9223372036854775806");
    expect(card).not.toHaveTextContent("9007199254740992"); expect(card).toHaveTextContent("行動版本由伺服器重新核對");
    expect(card).toHaveTextContent("不是市場歷史或文件驗證"); expect(card).not.toHaveTextContent("synthetic-secret");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/basis-assertions", expect.objectContaining({ params: { query: expect.objectContaining({
      observation_id: observationId, security_id: securityId, limit: 25,
    }) } }));
    fireEvent.click(within(card).getByRole("button", { name: "使用此基準聲明" }));
    expect(select).toHaveBeenCalledWith(basis.id); expect(submit).not.toHaveBeenCalled();
  });

  it.each([
    { ...page, items: [{ ...basis, security_id: basis.id }] }, { ...page, items: [{ ...basis, observation_id: basis.id }] },
    { ...page, items: [{ ...basis, source_sha256: "b".repeat(64) }] }, { ...page, items: [{ ...basis, market_history: "VERIFIED" }] },
    { ...page, items: [{ ...basis, current_use_scope: "GRANTED" }] }, { ...page, items: [{ ...basis, complete: false }] },
    { ...page, items: [{ ...basis, actions: [{ ...basis.actions[0], ratio_numerator: "1e3" }] }] },
    { ...page, items: [{ ...basis, actions: [{ ...basis.actions[0], security_id: basis.id }] }] },
    { ...page, items: [{ ...basis, actions: [{ ...basis.actions[0], observed_version: 1.5 }] }] },
    { ...page, items: [{ ...basis, actions: [{ ...basis.actions[0], ratio_numerator: "9223372036854775806", ratio_denominator: "9223372036854775807" }] }] },
    { ...page, items: [{ ...basis, actions: [{ ...basis.actions[0], effective_date: "2026-10-31" }] }] },
    { ...page, items: [{ ...basis, actions: [basis.actions[0], { ...basis.actions[0], action_id: "77777777-7777-4777-8777-777777777777" }] }] },
    { ...page, items: [{ ...basis, actions: [basis.actions[0], basis.actions[0]] }] },
    { ...page, items: [{ ...basis, citation: { ...basis.citation, id: observationId } }] },
    { ...page, items: [basis, basis] },
    { ...page, items: [{ ...basis, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }, { ...basis, id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" }] },
  ])("rejects a foreign, malformed, mismatched or duplicated declaration", async broken => {
    reads(broken); const { select } = renderPicker(); expect(await screen.findByRole("alert")).toHaveTextContent("基準聲明清單無法核對");
    expect(screen.queryByRole("button", { name: "使用此基準聲明" })).not.toBeInTheDocument(); expect(select).not.toHaveBeenCalled();
  });

  it("accepts an exact reverse split strictly between an earlier basis day and ex-date", async () => {
    reads({ ...page, items: [{ ...basis, share_basis_date: "2026-10-30", actions: [{ ...basis.actions[0]!, action_type: "REVERSE_SPLIT",
      effective_date: "2026-10-31", ratio_numerator: "9223372036854775806", ratio_denominator: "9223372036854775807" }] }] });
    renderPicker(); expect(await screen.findByRole("button", { name: "使用此基準聲明" })).toBeEnabled();
  });

  it("does not select an already selected or otherwise used declaration", async () => {
    reads(); renderPicker(basis.id.toUpperCase()); expect(await screen.findByRole("button", { name: "已選此基準" })).toBeDisabled();
  });

  it("prevents reusing a declaration selected for another observation", async () => {
    reads(); renderPicker("", [basis.id.toUpperCase()]); expect(await screen.findByRole("button", { name: "其他來源已選此基準" })).toBeDisabled();
  });

  it("does not select stale data after a failed refresh or submit the enclosing form", async () => {
    const get = reads(); let calls = 0;
    get.mockImplementation(async () => ++calls === 1 ? reply(page)
      : { error: { code: "BASIS_ASSERTION_UNAVAILABLE", message: "private synthetic detail" }, response: new Response(null, { status: 503 }) });
    const { submit } = renderPicker(); const button = await screen.findByRole("button", { name: "使用此基準聲明" }); expect(button).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重新載入可選基準" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("基準聲明無法安全核對"); expect(button).toBeDisabled();
    expect(screen.queryByText(/private synthetic detail/)).not.toBeInTheDocument(); expect(submit).not.toHaveBeenCalled();
  });

  it("paginates without submitting the enclosing form or inferring missing basis", async () => {
    const get = reads(); let calls = 0;
    get.mockImplementation(async () => ++calls === 1 ? reply({ ...page, next_cursor: "next-basis" }) : reply({ ...page, items: [] }));
    const { submit } = renderPicker(); await screen.findByRole("article", { name: `可選基準 ${basis.id}` });
    fireEvent.click(screen.getByRole("button", { name: "下一頁基準" }));
    expect(await screen.findByText(/此頁沒有基準聲明/)).toHaveTextContent("不代表沒有拆股");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/basis-assertions", expect.objectContaining({ params: { query: expect.objectContaining({ cursor: "next-basis" }) } }));
    expect(submit).not.toHaveBeenCalled();
  });

  it("does not select cached declarations while offline", async () => {
    reads(); renderPicker(); await screen.findByRole("button", { name: "使用此基準聲明" });
    try {
      await act(async () => onlineManager.setOnline(false)); fireEvent.click(screen.getByRole("button", { name: "重新載入可選基準" }));
      expect(await screen.findByText(/連線已暫停/)).toBeVisible(); await waitFor(() => expect(screen.getByRole("button", { name: "使用此基準聲明" })).toBeDisabled());
    } finally { await act(async () => onlineManager.setOnline(true)); }
  });
});
