import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { CandidateSourcePicker } from "./CandidateSourcePicker";

type Observation = components["schemas"]["DividendObservationRecord"];
const source: Observation = {
  id: "11111111-1111-4111-8111-111111111111", security_id: "22222222-2222-4222-8222-222222222222", normalized_sha256: "a".repeat(64),
  provider_id: "eodhd", origin_group: "EODHD", provider_run_id: "33333333-3333-4333-8333-333333333333", provider_symbol: "SYN.US", provider_event_id: null,
  source_url: null, evidence: null, retrieved_at: "2026-11-01T00:00:00Z", last_seen_at: "2026-11-01T00:00:00Z", sightings_count: 1, provenance_version: 1,
  scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE",
  ex_date: null, announce_date: null, record_date: null, pay_date: null, event_type: "UNKNOWN", dps: null, currency: null,
  adjustment_basis: "UNKNOWN", source_currency: null, source_period: null, source_frequency: null, incomplete_reasons: ["MISSING_CURRENCY"],
};
const page = { items: [source], scope: "IMMUTABLE_SOURCE_V1", legacy_records_omitted: 2, next_cursor: null };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: unknown = page) { return vi.spyOn(api, "GET").mockResolvedValue(reply(value)); }
function renderPicker(selectedIds: string[] = [], full = false) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const select = vi.fn(); const submit = vi.fn(event => event.preventDefault());
  render(<QueryClientProvider client={cache}><form onSubmit={submit}><CandidateSourcePicker securityId={source.security_id} selectedIds={selectedIds} full={full} onSelect={select} /></form></QueryClientProvider>);
  return { select, submit };
}

describe("explicit candidate source selection", () => {
  it("loads a same-security source page and passes only original identity and hash", async () => {
    const get = reads(); const { select, submit } = renderPicker();
    const card = await screen.findByRole("article", { name: `可選來源 ${source.id}` });
    expect(card).toHaveTextContent("除淨日 未知"); expect(card).toHaveTextContent("每股 幣別未知 未知");
    expect(card).toHaveTextContent("MISSING_CURRENCY"); expect(card).toHaveTextContent(source.normalized_sha256);
    expect(screen.getByText(/略過 2 筆舊來源/)).toBeVisible(); expect(card).toHaveTextContent("不是事件關聯查詢");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/observations", expect.objectContaining({ params: { query: expect.objectContaining({ security_id: source.security_id, limit: 25 }) } }));
    fireEvent.click(within(card).getByRole("button", { name: "加入此來源" }));
    expect(select).toHaveBeenCalledWith({ observation_id: source.id, source_sha256: source.normalized_sha256 }); expect(submit).not.toHaveBeenCalled();
  });

  it.each([
    { ...page, scope: "SOURCE_ONLY" }, { ...page, legacy_records_omitted: -1 },
    { ...page, items: [{ ...source, security_id: source.id }] }, { ...page, items: [{ ...source, normalized_sha256: "bad" }] },
    { ...page, items: [{ ...source, canonical_adoption_scope: "LOOKUP" }] }, { ...page, items: [{ ...source, provenance_version: 0 }] },
    { ...page, items: [{ ...source, dps: "1e2" }] }, { ...page, items: [source, source] },
    { ...page, items: [{ ...source, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }, { ...source, id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" }] },
  ])("rejects malformed, duplicated, foreign or non-v1 sources", async broken => {
    reads(broken); const { select } = renderPicker();
    expect(await screen.findByRole("alert")).toHaveTextContent("來源清單無法核對");
    expect(screen.queryByRole("button", { name: "加入此來源" })).not.toBeInTheDocument(); expect(select).not.toHaveBeenCalled();
  });

  it("does not add a duplicate or exceed the association limit", async () => {
    reads(); const { select } = renderPicker([source.id]);
    expect(await screen.findByRole("button", { name: "已列入來源" })).toBeDisabled(); expect(select).not.toHaveBeenCalled();
  });

  it("recognizes an existing source identity regardless of UUID letter case", async () => {
    const id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    reads({ ...page, items: [{ ...source, id: id.toUpperCase() }] }); renderPicker([id]);
    expect(await screen.findByRole("button", { name: "已列入來源" })).toBeDisabled();
  });

  it("does not allow selection at the association limit", async () => {
    reads(); renderPicker([], true); expect(await screen.findByRole("button", { name: "加入此來源" })).toBeDisabled();
  });

  it("does not select stale data after a failed refresh and does not submit the enclosing form", async () => {
    const get = reads(); let calls = 0;
    get.mockImplementation(async () => ++calls === 1 ? reply(page)
      : { error: { code: "SOURCE_EVIDENCE_UNAVAILABLE", message: "private synthetic detail" }, response: new Response(null, { status: 503 }) });
    const { submit } = renderPicker(); const button = await screen.findByRole("button", { name: "加入此來源" }); expect(button).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重新載入可選來源" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("來源證據未通過完整性檢查"); expect(button).toBeDisabled();
    expect(screen.queryByText(/private synthetic detail/)).not.toBeInTheDocument(); expect(submit).not.toHaveBeenCalled();
  });

  it("paginates explicit source choices without submitting the enclosing form", async () => {
    const get = reads(); let calls = 0;
    get.mockImplementation(async () => ++calls === 1 ? reply({ ...page, next_cursor: "next-source" }) : reply({ ...page, items: [] }));
    const { submit } = renderPicker(); await screen.findByRole("article", { name: `可選來源 ${source.id}` });
    fireEvent.click(screen.getByRole("button", { name: "下一頁來源" }));
    expect(await screen.findByText(/此頁沒有可選來源/)).toHaveTextContent("不代表沒有股息");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/observations", expect.objectContaining({ params: { query: expect.objectContaining({ cursor: "next-source" }) } }));
    expect(submit).not.toHaveBeenCalled();
  });

  it("does not select cached observations while offline", async () => {
    reads(); renderPicker(); await screen.findByRole("button", { name: "加入此來源" });
    try {
      await act(async () => onlineManager.setOnline(false));
      fireEvent.click(screen.getByRole("button", { name: "重新載入可選來源" }));
      expect(await screen.findByText(/連線已暫停/)).toBeVisible();
      await waitFor(() => expect(screen.getByRole("button", { name: "加入此來源" })).toBeDisabled());
    } finally { await act(async () => onlineManager.setOnline(true)); }
  });
});
