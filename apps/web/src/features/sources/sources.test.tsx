import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { SyncForm } from "./SyncForm";
import { CoveragePage, ObservationsPage, ProvidersPage } from "./SourcePages";
import { JobDetailPage, JobsPage } from "./JobsPage";
import { canCancel, pollJob, refreshSources } from "./data";
import { SourceLink, SourceReadState } from "./details";

type Schemas = components["schemas"];
const security: Schemas["Security"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic source security", display_ticker: "0005", exchange_code: "HKG", quote_currency: "USD", security_type: "EQUITY", status: "DELISTED", isin: null, verified: false, version: 1 };
const provider: Schemas["ProviderInfo"] = { provider_id: "eodhd", origin_group: "EODHD", markets: ["HK", "US"], access_mode: "API_KEY", documentation_url: null, live_validation: "NOT_TESTED", data_rights: "UNVERIFIED", raw_payload_storage: false, operations: ["DIVIDENDS", "SPLITS"], coverage_start: null, has_unadjusted_dividend: true, configured: false, capabilities: ["DIVIDENDS", "SPLITS"], configuration: { provider_id: "eodhd", configured: false, state: "UNCONFIGURED", reason: "CONFIG_NOT_FOUND", scope: "CONFIGURATION_ONLY", live_validation: "NOT_TESTED", last_success_at: null, quota_remaining: null }, scope: "CANDIDATE_METADATA_AND_LOCAL_CONFIGURATION" };
const created: Schemas["JobCreated"] = { job_id: "22222222-2222-4222-8222-222222222222", status: "QUEUED", scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN" };
const job: Schemas["SourceJob"] = { id: created.job_id, status: "RUNNING", kind: "DIVIDEND_SOURCE_SYNC", progress_done: 0, progress_total: 1, attempt_count: 1, version: 2, created_at: "2026-11-07T00:00:00Z", started_at: "2026-11-07T00:00:01Z", finished_at: null, next_run_at: null, last_error_code: null, scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN", result: { scope: "SOURCE_ONLY", provider_attempts_scope: "RESERVED_OR_RECORDED_HTTP", provider_attempts: 1, provider_http_recorded: 0, provider_attempts_unresolved: 1, pages_committed: 0, observations_new: 0, scanned_symbols: 0, work_units: 1, work_units_done: 0, failures: 0, skipped_unsupported: 0 } };
const item: Schemas["SourceJobItem"] = { id: "33333333-3333-4333-8333-333333333333", security_id: security.id, provider_id: "eodhd", operation: "DIVIDENDS", status: "RUNNING", ordinal: 0, attempt_count: 1, last_error_code: null, next_run_at: null };
const dividend: Schemas["DividendObservationRecord"] = { id: "44444444-4444-4444-8444-444444444444", security_id: security.id, provider_id: "eodhd", origin_group: "EODHD", provider_run_id: "55555555-5555-4555-8555-555555555555", provider_symbol: "0005.HK", provider_event_id: null, evidence: null, source_url: "https://eodhd.com/api/div/0005.HK", normalized_sha256: "a".repeat(64), retrieved_at: "2026-11-07T00:00:00Z", sightings_count: 2, last_seen_at: "2026-11-07T00:00:30Z", provenance_version: 1, scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", dps: "92233720368.54775807", currency: null, source_currency: null, source_period: null, source_frequency: null, ex_date: null, announce_date: null, pay_date: null, record_date: null, event_type: "UNKNOWN", adjustment_basis: "UNKNOWN", incomplete_reasons: ["MISSING_EX_DATE", "MISSING_CURRENCY", "UNRESOLVED_ADJUSTMENT"] };
const split: Schemas["SplitObservationRecord"] = { id: dividend.id, security_id: security.id, provider_id: "eodhd", origin_group: "EODHD", provider_run_id: dividend.provider_run_id, provider_symbol: "0005.HK", provider_event_id: null, evidence: null, source_url: null, normalized_sha256: dividend.normalized_sha256, retrieved_at: dividend.retrieved_at, sightings_count: 1, last_seen_at: dividend.retrieved_at, provenance_version: 1, scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", effective_date: "2026-01-01", ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806", source_adjustment_type: null, effect_scope: "PROVIDER_ADJUSTMENT_ONLY" };
const coverage: Schemas["ProviderCoverage"] = { security_id: security.id, provider_id: "eodhd", origin_group: "EODHD", operation: "DIVIDENDS", provider_symbol: null, availability: "DATA_NOT_CONFIRMED", configuration: provider.configuration, coverage_basis: "NOT_ESTABLISHED", coverage_start: null, coverage_end: null, status: "NOT_TESTED", last_result: null, last_success_at: null, observations_count: 0, legacy_runs_omitted: 2, legacy_status_ignored: true, supported_market: true, supported_operation: true, live_validation: "NOT_TESTED", scope: "RECORDED_SOURCE_ONLY" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(overrides: Record<string, object> = {}) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (Object.hasOwn(overrides, path)) return reply(overrides[path]!);
    switch (path) {
      case "/api/v1/auth/csrf": return reply({ csrf_token: "synthetic-source-csrf", expires_at: "2099-01-01T00:00:00Z" });
      case "/api/v1/securities": return reply({ items: [security], next_cursor: null });
      case "/api/v1/securities/{id}": return reply(security);
      case "/api/v1/providers": return reply({ items: [provider], next_cursor: null });
      case "/api/v1/providers/{provider_id}/health": return reply({ provider_id: "eodhd", configured: false, state: "UNCONFIGURED", reason: "CONFIG_NOT_FOUND", last_result: null, last_success_at: null, last_error_code: null, legacy_runs_omitted: 0, quota_remaining: null, live_validation: "NOT_TESTED", scope: "LOCAL_CONFIGURATION_AND_RECORDED_SOURCE" });
      case "/api/v1/providers/coverage": return reply({ items: [coverage], next_cursor: null });
      case "/api/v1/jobs": return reply({ items: [job], next_cursor: null });
      case "/api/v1/jobs/{id}": return reply(job);
      case "/api/v1/jobs/{id}/items": return reply({ items: [item], next_cursor: null });
      case "/api/v1/dividends/observations":
      case "/api/v1/providers/split-observations": return reply({ items: [], next_cursor: null, legacy_records_omitted: 1, scope: "IMMUTABLE_SOURCE_V1" });
      default: throw new Error(`Unexpected source read: ${path}`);
    }
  });
}
function renderUI(ui: ReactElement, path = "/") {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body?: Record<string, unknown>; params: { header: Record<string, string> } };
}
function renderJob() {
  return renderUI(<Routes><Route path="/data-center/jobs/:id" element={<JobDetailPage />} /></Routes>, `/data-center/jobs/${job.id}`);
}
async function fillSync() {
  await screen.findByRole("option", { name: /0005 \/ HKG/ });
  fireEvent.change(screen.getByLabelText("擷取證券", { exact: true }), { target: { value: security.id } });
  fireEvent.click(screen.getByRole("button", { name: "加入證券" }));
  fireEvent.change(screen.getByLabelText("擷取起日"), { target: { value: "2026-01-01" } });
  fireEvent.change(screen.getByLabelText("擷取迄日"), { target: { value: "2026-01-31" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /^EODHD/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只擷取來源觀測/ }));
}

describe("source references enforce an inert HTTP(S)-only boundary", () => {
  it.each(["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///private/document.pdf", "ftp://example.test/evidence", "//example.test/evidence", "not an absolute URL", "https://user:synthetic-password@example.test/evidence", `https://example.test/${"a".repeat(2049)}`])("does not render an actionable unsafe reference: %s", url => {
    renderUI(<SourceLink url={url} label="來源文件" />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText(/連結未通過安全檢查/)).toBeVisible();
  });
  it.each(["https://example.test/evidence.pdf", "http://example.test/evidence.pdf"])("retains a safe explicit reference without fetching it: %s", url => {
    const get = vi.spyOn(api, "GET");
    renderUI(<SourceLink url={url} label="來源文件" />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", url);
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
    expect(get).not.toHaveBeenCalled();
  });
});

describe("explicit source-only intent and truthful observation UI", () => {
  it("has no guessed security/provider/date scope and does not submit an empty request", async () => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<SyncForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("checkbox", { name: /^EODHD/ });
    expect(screen.getByLabelText("擷取起日")).toHaveValue("");
    expect(screen.getByLabelText("擷取迄日")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: /^EODHD/ })).not.toBeChecked();
    expect(screen.getByLabelText("擷取股息觀測")).toBeChecked();
    expect(screen.getByLabelText("擷取拆股因子")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    await screen.findByText(/請至少加入一個證券/);
    expect(post).not.toHaveBeenCalled();
  });

  it("queues an explicit source-only payload and accepts historical delisted identities without finance fields", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(created));
    const saved = vi.fn();
    renderUI(<SyncForm onSaved={saved} onCancel={vi.fn()} />);
    await fillSync();
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(created));
    expect(post).toHaveBeenCalledExactlyOnceWith("/api/v1/dividends/sync", expect.objectContaining({ body: { security_ids: [security.id], provider_ids: ["eodhd"], from_date: "2026-01-01", to_date: "2026-01-31", operations: ["DIVIDENDS"], force: false } }));
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("does not call an empty v1 view zero dividends or offer fake financial adoption", async () => {
    reads();
    renderUI(<ObservationsPage mode="dividends" />);
    expect(await screen.findByText(/尚無可顯示的 v1 來源觀測/)).toBeVisible();
    expect(screen.getByText(/保留的舊來源 1 筆/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /新增股息|採納股數|確認已收/ })).not.toBeInTheDocument();
  });

  it.each(["no provider", "no operation", "no acknowledgement", "reversed dates"] as const)("rejects %s without contacting the writer", async invalid => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<SyncForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillSync();
    if (invalid === "no provider") fireEvent.click(screen.getByRole("checkbox", { name: /^EODHD/ }));
    if (invalid === "no operation") fireEvent.click(screen.getByLabelText("擷取股息觀測"));
    if (invalid === "no acknowledgement") fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只擷取來源觀測/ }));
    if (invalid === "reversed dates") fireEvent.change(screen.getByLabelText("擷取迄日"), { target: { value: "2025-12-31" } });
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    await screen.findByRole("alert");
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByLabelText("擷取起日")).toHaveValue("2026-01-01");
  });

  it("does not alter an uncertain intent for candidate search or acknowledgement, but changes it for a new request", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic transport interruption"));
    renderUI(<SyncForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillSync();
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("搜尋擷取證券"), { target: { value: "unused candidate search" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只擷取來源觀測/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只擷取來源觀測/ }));
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(options(post, 0)).toEqual(options(post, 1));
    expect(screen.getByRole("list", { name: "已加入擷取證券" })).toHaveTextContent("0005 / HKG");
    fireEvent.click(screen.getByLabelText("擷取拆股因子"));
    await screen.findByRole("button", { name: "建立擷取工作" });
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 1).params.header["Idempotency-Key"]);
    expect(options(post, 2).body).toMatchObject({ operations: ["DIVIDENDS", "SPLITS"], force: false });
  });

  it("removes only an explicitly chosen security and cannot accidentally submit when adding or removing", async () => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<SyncForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fillSync();
    fireEvent.change(screen.getByLabelText("擷取證券", { exact: true }), { target: { value: security.id } });
    expect(screen.getByRole("button", { name: "加入證券" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: `移除此擷取證券 ${security.id}` }));
    expect(screen.queryByRole("list", { name: "已加入擷取證券" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加入證券" })).toBeEnabled();
    expect(post).not.toHaveBeenCalled();
  });
});

describe("exact immutable source presentations", () => {
  const observationPage = (items: object[]) => ({ items, next_cursor: null, legacy_records_omitted: 0, scope: "IMMUTABLE_SOURCE_V1" });
  it("preserves every scaled-INT64 DPS digit and never fills dividend currency or ex date from the security", async () => {
    const get = reads({ "/api/v1/dividends/observations": observationPage([dividend]) });
    renderUI(<ObservationsPage mode="dividends" />);
    expect(await screen.findByText("每股來源金額：92233720368.54775807 幣別未知")).toBeVisible();
    expect(screen.getByText("未知，不用擷取日代替")).toBeVisible();
    expect(screen.getByText(/除淨日未知；股息幣別未知；調整基準待核對/)).toBeVisible();
    expect(screen.getByText(/來源列本身，未查詢目前或過往關聯/)).toBeVisible();
    expect(screen.getByText(dividend.normalized_sha256)).toBeVisible();
    expect(screen.getByText(/2 次 \/ 2026-11-07T00:00:30Z；不代表多筆收入/)).toBeVisible();
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/observations", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.queryByText(/USD 922337/)).not.toBeInTheDocument();
  });
  it.each([["9223372036854775807", "9223372036854775806"], ["1", "3"]])("preserves the exact %s/%s provider-only factor", async (numerator, denominator) => {
    reads({ "/api/v1/providers/split-observations": observationPage([{ ...split, ratio_numerator: numerator, ratio_denominator: denominator }]) });
    renderUI(<ObservationsPage mode="splits" />);
    expect(await screen.findByText(`精確來源因子：${numerator} : ${denominator}（未採納股數）`)).toBeVisible();
    expect(screen.getByText(/PROVIDER_ADJUSTMENT_ONLY；不改變持股/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /採納|確認|作廢/ })).not.toBeInTheDocument();
  });
  it("retains typed manual citation identity and reason while withholding its original URL", async () => {
    reads({ "/api/v1/dividends/observations": observationPage([{ ...dividend, provider_id: "manual", origin_group: "USER_VERIFIED", dps: "0.00000000", evidence: { id: item.id, citation: "Synthetic issuer document page 3", reason: "Synthetic owner asserted source, not verified income", url: null, url_withheld: true } }]) });
    renderUI(<ObservationsPage mode="dividends" />);
    const evidence = await screen.findByRole("region", { name: "人工引用證據" });
    expect(evidence).toHaveTextContent(item.id);
    expect(evidence).toHaveTextContent("Synthetic issuer document page 3");
    expect(evidence).toHaveTextContent("Synthetic owner asserted source, not verified income");
    expect(evidence).toHaveTextContent("連結未提供");
    expect(within(evidence).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("每股來源金額：0.00000000 幣別未知")).toBeVisible();
  });
  it("resets a server cursor when an event-date filter changes and keeps the unknown legacy count explicit", async () => {
    const get = reads({ "/api/v1/dividends/observations": { ...observationPage([dividend]), next_cursor: "synthetic-opaque-cursor" } });
    renderUI(<ObservationsPage mode="dividends" />);
    await screen.findByText(/每股來源金額/);
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    await screen.findByText("第 2 頁；本頁 1 筆（非全部紀錄）");
    fireEvent.change(screen.getByLabelText("除淨起日"), { target: { value: "2026-01-01" } });
    await screen.findByText("第 1 頁；本頁 1 筆（非全部紀錄）");
    expect(get).toHaveBeenLastCalledWith("/api/v1/dividends/observations", expect.objectContaining({ params: { query: expect.objectContaining({ cursor: undefined, from_date: "2026-01-01" }) } }));
    expect(screen.getByRole("button", { name: "清除此篩選：除淨起日" })).toBeVisible();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("does not declare income or expose an unsafe source when integrity validation fails", async () => {
    const get = reads();
    get.mockResolvedValue({ error: { code: "SOURCE_EVIDENCE_UNAVAILABLE", message: "Private unsafe upstream text must not appear", request_id: "test", details: {} }, response: new Response(null, { status: 503 }) });
    renderUI(<ObservationsPage mode="dividends" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("來源證據未通過完整性檢查");
    expect(screen.queryByText(/Private unsafe/)).not.toBeInTheDocument();
    expect(screen.queryByText(/尚無可顯示的 v1/)).not.toBeInTheDocument();
  });
});

describe("local configuration is not live health or coverage", () => {
  it("only loads recorded health on an explicit read and never offers fake key or schedule controls", async () => {
    const get = reads();
    renderUI(<ProvidersPage />);
    await screen.findByRole("heading", { name: "EODHD" });
    expect(get).not.toHaveBeenCalledWith("/api/v1/providers/{provider_id}/health", expect.anything());
    expect(screen.getByText("未設定／本機不可用（UNCONFIGURED）")).toBeVisible();
    expect(screen.queryByRole("button", { name: /匯入密鑰|新增排程|測試連線/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看EODHD診斷" }));
    expect(await screen.findByText(/尚無已記錄來源頁面/)).toBeVisible();
    expect(screen.getByText(/不是供應商帳單/)).toBeVisible();
    expect(get).toHaveBeenCalledWith("/api/v1/providers/{provider_id}/health", expect.objectContaining({ params: { path: { provider_id: "eodhd" } } }));
  });
  it("requires an explicit security for bounded diagnostics and retains unknown coverage and ignored legacy status", async () => {
    const get = reads();
    renderUI(<CoveragePage />);
    await screen.findByRole("option", { name: /0005 \/ HKG/ });
    expect(get).not.toHaveBeenCalledWith("/api/v1/providers/coverage", expect.anything());
    fireEvent.change(screen.getByLabelText("診斷證券", { exact: true }), { target: { value: security.id } });
    expect(await screen.findByText(/未知 \/ 未知（NOT_ESTABLISHED）/)).toBeVisible();
    expect(screen.getByText(/略過 2 筆未驗證執行；既有狀態不採信/)).toBeVisible();
    expect(screen.getByText(/未建立歷史代號；不由顯示代號猜測/)).toBeVisible();
    expect(screen.getByText(/0 筆；不是股息總額/)).toBeVisible();
    expect(get).toHaveBeenCalledWith("/api/v1/providers/coverage", expect.objectContaining({ params: { query: expect.objectContaining({ security_id: security.id, operation: "DIVIDENDS", limit: 25 }) } }));
  });
  it("labels a paused or failed retained read without dropping the previous data claim", () => {
    const retry = vi.fn().mockResolvedValue(undefined);
    renderUI(<SourceReadState label="來源診斷" result={{ isPaused: true, isPending: false, isError: true, error: new TypeError("Synthetic interruption"), data: { retained: true }, refetch: retry }} />);
    expect(screen.getByRole("status")).toHaveTextContent("連線已暫停");
    expect(screen.getByRole("alert")).toHaveTextContent("保留上次載入內容");
    fireEvent.click(screen.getByRole("button", { name: "重試載入來源診斷" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });
});

describe("owner source jobs preserve stop intent and bounded polling", () => {
  it.each([
    ["QUEUED", true, true], ["LEASED", true, true], ["RUNNING", true, true], ["CANCEL_REQUESTED", true, false], ["RETRYING", false, true],
    ["PARTIAL", false, false], ["SUCCEEDED", false, false], ["FAILED", false, false], ["CANCELLED", false, false],
  ] as const)("poll/cancel policies remain truthful for %s", (status, polling, cancellable) => {
    expect(pollJob(status)).toBe(polling ? 2000 : false);
    expect(canCancel(status)).toBe(cancellable);
  });
  it("queues through a sheet with Close focus and reports only accepted source intent", async () => {
    reads();
    vi.spyOn(api, "POST").mockResolvedValue(reply(created));
    renderUI(<JobsPage />);
    fireEvent.click(screen.getByRole("button", { name: "建立來源擷取工作" }));
    const sheet = await screen.findByRole("dialog", { name: "建立來源擷取工作" });
    await waitFor(() => expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus());
    await fillSync();
    fireEvent.click(screen.getByRole("button", { name: "建立擷取工作" }));
    expect(await screen.findByText(/已接受來源擷取工作（HTTP 202）/)).toBeVisible();
    expect(screen.getByRole("link", { name: `查看已接受工作 ${job.id}` })).toHaveAttribute("href", `/data-center/jobs/${job.id}`);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "建立來源擷取工作" })).toHaveFocus();
  });
  it("sends a stable cancel intent without If-Match or body and does not call CANCEL_REQUESTED stopped", async () => {
    const remote = { "/api/v1/jobs/{id}": job };
    reads(remote);
    const stopping = { ...job, status: "CANCEL_REQUESTED" as const, version: 3 };
    const post = vi.spyOn(api, "POST").mockImplementation(async () => { remote["/api/v1/jobs/{id}"] = stopping; return reply(stopping); });
    renderJob();
    fireEvent.click(await screen.findByRole("button", { name: "要求取消此工作" }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
    expect(await screen.findByText(/取消回覆：已提出取消，尚未確認停止/)).toBeVisible();
    expect(screen.getByRole("button", { name: "要求取消此工作" })).toBeDisabled();
    expect(options(post).body).toBeUndefined();
    expect(options(post).params.header["If-Match"]).toBeUndefined();
    expect(options(post).params.header["Idempotency-Key"]).toBeTruthy();
    expect(post).toHaveBeenCalledWith("/api/v1/jobs/{id}/cancel", expect.anything());
  });
  it("retains a cancel dialog and its retry key after an uncertain failure", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic uncertain cancel"));
    renderJob();
    fireEvent.click(await screen.findByRole("button", { name: "要求取消此工作" }));
    const dialog = screen.getByRole("alertdialog");
    for (let attempt = 1; attempt <= 2; attempt++) {
      fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
      await screen.findByRole("alert");
      await waitFor(() => expect(post).toHaveBeenCalledTimes(attempt));
    }
    expect(options(post, 0)).toEqual(options(post, 1));
    expect(screen.getByRole("alertdialog")).toBeVisible();
    expect(screen.queryByText(/取消回覆/)).not.toBeInTheDocument();
  });
  it("keeps an open cancel dialog across terminal refresh and renders the actual already-finished response", async () => {
    const remote = { "/api/v1/jobs/{id}": job };
    reads(remote);
    const finished = { ...job, status: "SUCCEEDED" as const, version: 4 };
    vi.spyOn(api, "POST").mockResolvedValue(reply(finished));
    const cache = renderJob();
    fireEvent.click(await screen.findByRole("button", { name: "要求取消此工作" }));
    remote["/api/v1/jobs/{id}"] = finished;
    await act(async () => { cache.setQueryData(["sources", "job", job.id], finished); });
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "確認" }));
    expect(await screen.findByText(/取消回覆：來源處理完成，不代表股息已驗證/)).toBeVisible();
  });
  it("displays a far-future wait unchanged and does not expose a force or quota-reset control", async () => {
    reads({ "/api/v1/jobs/{id}": { ...job, status: "RETRYING", last_error_code: "RATE_LIMITED", next_run_at: "9999-12-31T00:00:00Z" } });
    renderJob();
    expect(await screen.findByText("9999-12-31T00:00:00Z")).toBeVisible();
    expect(screen.getByText(/未知耗用不退款或重發/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /強制重試|重設配額|立即擷取/ })).not.toBeInTheDocument();
  });
  it.each(["RUNNING", "RETRYING", "SUCCEEDED"] as const)("actual query polling follows %s without sleeps", async status => {
    vi.useFakeTimers();
    try {
      const get = reads({ "/api/v1/jobs/{id}": { ...job, status, next_run_at: status === "RETRYING" ? "9999-12-31T00:00:00Z" : null } });
      renderJob();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      const count = () => (get.mock.calls as readonly (readonly unknown[])[]).filter(call => call[0] === "/api/v1/jobs/{id}").length;
      const before = count();
      expect(before).toBe(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      if (status === "RUNNING") expect(count()).toBeGreaterThan(before);
      else expect(count()).toBe(before);
    } finally { cleanup(); vi.useRealTimers(); }
  });
  it("fetches final item state on a new job version even when polling stops and old pages are fresh", async () => {
    const remote = { "/api/v1/jobs/{id}": job, "/api/v1/jobs/{id}/items": { items: [item], next_cursor: null } };
    reads(remote);
    const cache = renderJob();
    await screen.findByRole("article", { name: item.id });
    remote["/api/v1/jobs/{id}/items"] = { items: [{ ...item, status: "SKIPPED", last_error_code: "UNCONFIGURED" }], next_cursor: null };
    const finished = { ...job, version: 3, status: "FAILED" as const };
    remote["/api/v1/jobs/{id}"] = finished;
    await act(async () => { cache.setQueryData(["sources", "job", job.id], finished); });
    await waitFor(() => expect(screen.getByRole("article", { name: item.id })).toHaveTextContent("已略過，未宣稱擷取成功"));
  });
  it("invalidates only source reads, never ledger, security picker, portfolio revisions or other owners", async () => {
    const cache = new QueryClient();
    const keys = [["sources", "jobs"], ["sources", "observations"], ["ledger", "trades"], ["securities", "record", security.id], ["portfolios"]];
    for (const key of keys) cache.setQueryData(key, { synthetic: true });
    await refreshSources(cache);
    for (const key of keys) expect(cache.getQueryState(key)?.isInvalidated).toBe(key[0] === "sources");
    cache.clear();
  });
});
