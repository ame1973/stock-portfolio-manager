import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { BasisCreateForm } from "./BasisCreateForm";

type Schemas = components["schemas"];
const source: Schemas["DividendObservationRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", security_id: "22222222-2222-4222-8222-222222222222", normalized_sha256: "a".repeat(64),
  provider_id: "manual", origin_group: "USER_VERIFIED", provider_run_id: "33333333-3333-4333-8333-333333333333", provider_symbol: "SYN.US", provider_event_id: null,
  source_url: null, evidence: null, retrieved_at: "2026-11-01T00:00:00Z", last_seen_at: "2026-11-01T00:00:00Z", sightings_count: 1, provenance_version: 1,
  scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", ex_date: "2026-11-01",
  announce_date: null, record_date: null, pay_date: null, event_type: "SPECIAL", dps: "0.6", currency: "USD", adjustment_basis: "SPLIT_ADJUSTED",
  source_currency: null, source_period: null, source_frequency: null, incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"],
};
const action: Schemas["CorporateAction"] = {
  id: "44444444-4444-4444-8444-444444444444", root_id: "55555555-5555-4555-8555-555555555555", supersedes_id: null, security_id: source.security_id,
  action_type: "SPLIT", effective_date: "2026-11-01", ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806",
  status: "CONFIRMED", lifecycle_status: "CURRENT", is_current: true, revision: 1, version: 9007199254740992, version_exact: "9007199254740993", created_sequence: 1,
  new_symbol: null, previous_symbol: null, evidence_url: null, effect_scope: "QUANTITY_TERMS", allocation_scope: "TERMS_NOT_A_VALUATION",
  review_required: false, reason: "Synthetic exact split terms", note: "", created_at: "2026-11-01T00:00:00Z",
};
const basis: Schemas["DividendBasisRecord"] = {
  id: "66666666-6666-4666-8666-666666666666", observation_id: source.id, source_sha256: source.normalized_sha256, security_id: source.security_id,
  ex_date: source.ex_date!, share_basis_date: "2026-11-02", source_basis: "AFTER_EFFECTIVE_DATE_ACTIONS", target_basis: "PRE_EX_DATE_EVENTS",
  complete: true, split_only: true, actions: [{ action_id: action.id, action_root_id: action.root_id, security_id: source.security_id,
    observed_version: action.version, observed_version_exact: action.version_exact, action_type: "SPLIT", effective_date: action.effective_date,
    ratio_numerator: action.ratio_numerator!, ratio_denominator: action.ratio_denominator!, observed_status: "CONFIRMED", observed_lifecycle_status: "CURRENT" }],
  citation: { id: "66666666-6666-4666-8666-666666666666", citation: "Synthetic document page 2", reason: "Synthetic explicit complete split-only scope", url: null, url_withheld: true },
  created_at: "2026-11-05T00:00:00Z", scope: "BASIS_DECLARATION_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE",
  current_use_scope: "REVALIDATION_REQUIRED_FOR_NEW_ADOPTION", market_history: "OWNER_ASSERTED_NOT_VERIFIED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const actionPage = { items: [action], next_cursor: null };
function reads(page: unknown = actionPage) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/corporate-actions") return reply(page);
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(record = source) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const saved = vi.fn();
  render(<QueryClientProvider client={cache}><MemoryRouter><BasisCreateForm record={record} onSaved={saved} onCancel={vi.fn()} /></MemoryRouter></QueryClientProvider>);
  return { cache, saved };
}
function details() {
  fireEvent.change(screen.getByLabelText("基準引用參考"), { target: { value: basis.citation.citation } });
  fireEvent.change(screen.getByLabelText("基準聲明依據"), { target: { value: basis.citation.reason } });
  fireEvent.change(screen.getByLabelText("基準引用網址（選填）"), { target: { value: "https://example.test/basis?private=synthetic#scope" } });
}
function confirmations() {
  fireEvent.click(screen.getByRole("checkbox", { name: /我已核對此範圍的已記錄行動/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明確聲明此範圍只涉及拆股與合股/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這不是市場完整性、文件驗證或收入/ }));
}
async function setDayAndSelect() {
  fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-11-02" } });
  const card = await screen.findByRole("article", { name: `可選企業行動 ${action.id}` });
  fireEvent.click(within(card).getByRole("button", { name: "加入此行動" }));
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Schemas["DividendBasisCreate"]; params: { header: Record<string, string> } };
}

describe("explicit cited split basis creation", () => {
  it("freezes source and explicit action with exact version and ratio, without query caching", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(basis)); const { cache, saved } = renderForm();
    expect(screen.getByLabelText("來源股數基準日")).toHaveValue("");
    expect(get).not.toHaveBeenCalledWith("/api/v1/corporate-actions", expect.anything());
    await setDayAndSelect();
    expect(screen.getByRole("list", { name: "已選基準行動" })).toHaveTextContent("9007199254740993");
    expect(screen.getByRole("list", { name: "已選基準行動" })).toHaveTextContent("9223372036854775807 / 9223372036854775806");
    expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(get).toHaveBeenCalledWith("/api/v1/corporate-actions", expect.objectContaining({ params: { query: expect.objectContaining({
      security_id: source.security_id, from_date: "2026-11-01", to_date: "2026-11-02", include_history: false, limit: 25,
    }) } }));
    details(); confirmations(); fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(basis));
    expect(options(post).body).toEqual({ observation_id: source.id, source_sha256: source.normalized_sha256, share_basis_date: "2026-11-02",
      source_basis: "AFTER_EFFECTIVE_DATE_ACTIONS", target_basis: "PRE_EX_DATE_EVENTS", complete: true, split_only: true,
      actions: [{ action_id: action.id, version: "9007199254740993" }], citation: { citation: basis.citation.citation, reason: basis.citation.reason,
        url: "https://example.test/basis?private=synthetic#scope" } });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("requires separate completeness, split-only and no-grant confirmations", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); await setDayAndSelect(); details();
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    expect(await screen.findByText(/請確認已核對完整的已記錄行動範圍/)).toBeVisible();
    expect(screen.getByText(/請明確聲明只涉及拆股與合股/)).toBeVisible();
    expect(screen.getByText(/請確認這不是市場、文件或收入驗證/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it.each(["基準引用參考", "基準聲明依據", "基準引用網址（選填）"])("rejects an invalid Unicode scalar in %s before posting", async label => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); await setDayAndSelect(); details(); confirmations();
    const prefix = label.includes("網址") ? "https://example.test/" : "Synthetic basis reference ";
    fireEvent.change(screen.getByLabelText(label), { target: { value: `${prefix}\ud800` } });
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("無效的 Unicode 字元"); expect(post).not.toHaveBeenCalled();
  });

  it("requires an explicit empty-scope declaration instead of inventing a chain", async () => {
    reads({ items: [], next_cursor: null }); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...basis, actions: [] })); const { saved } = renderForm();
    fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-11-02" } });
    await screen.findByText(/此頁沒有可選企業行動/); details(); confirmations();
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    expect(await screen.findByText(/請明確確認本次不列拆股行動/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /我確認本次明確不列拆股行動/ }));
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" })); await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(options(post).body.actions).toEqual([]);
  });

  it.each([
    { ...source, adjustment_basis: "UNKNOWN" as const }, { ...source, ex_date: null }, { ...source, normalized_sha256: "bad" },
    { ...source, provenance_version: 0 }, { ...source, canonical_adoption_scope: "LOOKUP" as const },
  ])("does not open an unsafe or undated source", async record => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(record as Schemas["DividendObservationRecord"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("來源範圍無法安全核對"); expect(post).not.toHaveBeenCalled();
  });

  it.each([
    { ...action, security_id: source.id }, { ...action, version_exact: undefined }, { ...action, version_exact: "03" },
    { ...action, ratio_numerator: "1", ratio_denominator: "2" }, { ...action, is_current: false },
    { ...action, effective_date: "2026-10-31" }, { ...action, version: 1, version_exact: "2" },
  ])("does not select malformed, foreign or inconsistent action pages", async broken => {
    reads({ items: [broken], next_cursor: null }); renderForm();
    fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-11-02" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("企業行動清單無法核對");
    expect(screen.queryByRole("button", { name: "加入此行動" })).not.toBeInTheDocument();
  });

  it.each([
    { items: [action, action], next_cursor: null },
    { items: [action, { ...action, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }], next_cursor: null },
    { items: [action], next_cursor: 123 },
  ])("rejects duplicated action identities or malformed page state", async page => {
    reads(page); renderForm(); fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-11-02" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("企業行動清單無法核對");
    expect(screen.queryByRole("button", { name: "加入此行動" })).not.toBeInTheDocument();
  });

  it("retains explicit selections across bounded pages without submitting the enclosing form", async () => {
    const second = { ...action, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", root_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", effective_date: "2026-11-02", version: 2, version_exact: "2" };
    const get = reads(); get.mockImplementation(async (path, options) => {
      if (path === "/api/v1/corporate-actions") {
        const cursor = (options as { params: { query: { cursor?: string } } }).params.query.cursor;
        return reply(cursor ? { items: [second], next_cursor: null } : { items: [action], next_cursor: "next-actions" });
      }
      return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    });
    const post = vi.spyOn(api, "POST"); renderForm(); await setDayAndSelect();
    fireEvent.click(screen.getByRole("button", { name: "下一頁行動" }));
    const card = await screen.findByRole("article", { name: `可選企業行動 ${second.id}` }); fireEvent.click(within(card).getByRole("button", { name: "加入此行動" }));
    expect(screen.getByRole("list", { name: "已選基準行動" })).toHaveTextContent(action.id);
    expect(screen.getByRole("list", { name: "已選基準行動" })).toHaveTextContent(second.id);
    fireEvent.click(screen.getByRole("button", { name: "重新載入企業行動" })); expect(post).not.toHaveBeenCalled();
  });

  it("does not select the ex-date action for an earlier source basis", async () => {
    reads(); renderForm(); fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-10-30" } });
    const card = await screen.findByRole("article", { name: `可選企業行動 ${action.id}` });
    expect(within(card).getByRole("button", { name: "加入此行動" })).toBeDisabled();
    expect(card).toHaveTextContent("不能加入本聲明");
  });

  it("allows an explicit reverse split strictly between an earlier basis and ex-date", async () => {
    const reverse = { ...action, action_type: "REVERSE_SPLIT", effective_date: "2026-10-31", ratio_numerator: "9223372036854775806", ratio_denominator: "9223372036854775807" };
    reads({ items: [reverse], next_cursor: null }); renderForm();
    fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-10-30" } });
    const card = await screen.findByRole("article", { name: `可選企業行動 ${action.id}` });
    fireEvent.click(within(card).getByRole("button", { name: "加入此行動" }));
    expect(screen.getByRole("list", { name: "已選基準行動" })).toHaveTextContent("9223372036854775806 / 9223372036854775807");
  });

  it("shows unallocated recorded actions without treating them as selectable splits", async () => {
    reads({ items: [{ ...action, action_type: "MERGER", ratio_numerator: null, ratio_denominator: null, status: "NEEDS_REVIEW", effect_scope: "REVIEW_ONLY", review_required: true }], next_cursor: null });
    renderForm(); fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-11-02" } });
    const card = await screen.findByRole("article", { name: `可選企業行動 ${action.id}` });
    expect(card).toHaveTextContent("未解決覆核"); expect(card).toHaveTextContent("未提供，不代表 1:1");
    expect(within(card).getByRole("button", { name: "加入此行動" })).toBeDisabled();
  });

  it("preserves selected pins when the basis day changes, and requires the scope to be reviewed again", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(); await setDayAndSelect(); details(); confirmations();
    fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-10-30" } });
    expect(screen.getByRole("list", { name: "已選基準行動" })).toHaveTextContent(action.id);
    expect(screen.getByRole("checkbox", { name: /我已核對此範圍的已記錄行動/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /我明確聲明此範圍只涉及拆股與合股/ })).not.toBeChecked();
    expect(screen.getByText(/已選行動不屬於目前基準範圍/)).toBeVisible();
    confirmations(); fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    expect(post).not.toHaveBeenCalled();
  });

  it("preserves unchanged request and key after a lost response, even when action reload fails", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("private transport detail")).mockResolvedValueOnce(reply(basis));
    const { saved } = renderForm(); await setDayAndSelect(); details(); confirmations();
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" })); await screen.findByText(/網絡連線中斷/);
    get.mockImplementation(async path => path === "/api/v1/corporate-actions"
      ? { error: { code: "DATABASE_UNAVAILABLE", message: "private detail" }, response: new Response(null, { status: 503 }) }
      : reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" }));
    fireEvent.click(screen.getByRole("button", { name: "重新載入企業行動" })); await screen.findByText(/資料庫暫時無法使用/);
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" })); await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(options(post, 1).body).toEqual(options(post, 0).body);
    expect(options(post, 1).params.header["Idempotency-Key"]).toBe(options(post, 0).params.header["Idempotency-Key"]);
  });

  it("rotates the key only after a different basis intent is submitted", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic dropped response"));
    renderForm(); await setDayAndSelect(); details(); confirmations(); fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("基準聲明依據"), { target: { value: "Synthetic changed owner scope explanation" } });
    fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" })); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 1).params.header["Idempotency-Key"]).not.toBe(options(post, 0).params.header["Idempotency-Key"]);
    expect(options(post, 1).body.citation.reason).toBe("Synthetic changed owner scope explanation");
  });

  it("does not accept an altered exact action version in the success response", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...basis, actions: [{ ...basis.actions[0], observed_version_exact: "9007199254740992" }] }));
    const { saved } = renderForm(); await setDayAndSelect(); details(); confirmations(); fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("伺服器回覆與本次基準聲明不符"); expect(post).toHaveBeenCalledOnce(); expect(saved).not.toHaveBeenCalled();
  });

  it.each([
    { ...basis, source_sha256: "b".repeat(64) },
    { ...basis, ex_date: "2026-11-02" },
    { ...basis, share_basis_date: "2026-11-03" },
    { ...basis, canonical_adoption_scope: "LINKAGE_CHECKED" },
    { ...basis, citation: { ...basis.citation, url: "https://example.test/basis?private=synthetic#scope", url_withheld: false } },
    { ...basis, actions: [] },
    { ...basis, actions: [{ ...basis.actions[0], ratio_numerator: "3" }] },
  ])("does not accept a success response with substituted scope, citation or action", async broken => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue(reply(broken)); const { saved } = renderForm();
    await setDayAndSelect(); details(); confirmations(); fireEvent.click(screen.getByRole("button", { name: "記錄基準聲明" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("伺服器回覆與本次基準聲明不符"); expect(saved).not.toHaveBeenCalled();
    expect(screen.getByLabelText("基準聲明依據")).toHaveValue(basis.citation.reason);
  });

  it("does not allow new action selection while offline", async () => {
    reads(); renderForm(); fireEvent.change(screen.getByLabelText("來源股數基準日"), { target: { value: "2026-11-02" } });
    const button = await screen.findByRole("button", { name: "加入此行動" });
    try { await act(async () => onlineManager.setOnline(false)); expect(button).toBeDisabled(); }
    finally { await act(async () => onlineManager.setOnline(true)); }
  });
});
