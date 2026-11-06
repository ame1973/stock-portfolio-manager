import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ScheduleForm } from "./ScheduleForm";
import { ScheduleDetailPage, SchedulesPage } from "./SchedulesPage";

type Schemas = components["schemas"];
const security: Schemas["Security"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic schedule security", display_ticker: "0005", exchange_code: "HKG", quote_currency: "USD", security_type: "EQUITY", status: "DELISTED", isin: null, verified: false, version: 1 };
const provider: Schemas["ProviderInfo"] = { provider_id: "eodhd", origin_group: "EODHD", markets: ["HK", "US"], access_mode: "API_KEY", documentation_url: null, live_validation: "NOT_TESTED", data_rights: "UNVERIFIED", raw_payload_storage: false, operations: ["DIVIDENDS", "SPLITS"], coverage_start: null, has_unadjusted_dividend: true, configured: false, capabilities: ["DIVIDENDS", "SPLITS"], configuration: { provider_id: "eodhd", configured: false, state: "UNCONFIGURED", reason: "CONFIG_NOT_FOUND", scope: "CONFIGURATION_ONLY", live_validation: "NOT_TESTED", last_success_at: null, quota_remaining: null }, scope: "CANDIDATE_METADATA_AND_LOCAL_CONFIGURATION" };
const policy: Schemas["SourceSchedulePolicy"] = { schema_version: 1, scope: "SOURCE_ONLY", calendar_basis: "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR", security_ids: [security.id], provider_ids: ["eodhd"], operations: ["DIVIDENDS"], market: "HK", first_date: "2099-01-01", local_run_time: "17:15", cadence: "DAILY", weekday: null, lookback_days: 30, lookback_months: null, backfill_from_date: null };
const schedule: Schemas["SourceScheduleRecord"] = { id: "22222222-2222-4222-8222-222222222222", name: "Synthetic explicit schedule", policy, enabled: false, version: 1, scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN", automatic_materialization_installed: true, exchange_timezone: "Asia/Hong_Kong", next_run_at: null, occurrence_count: 0, initial_backfill_queued: false, last_job_id: null, last_due_at: null, last_materialized_at: null, created_at: "2026-11-07T00:00:00Z", updated_at: "2026-11-07T00:00:00Z" };
const occurrence: Schemas["SourceScheduleOccurrence"] = { id: "33333333-3333-4333-8333-333333333333", schedule_id: schedule.id, sequence: 1, schedule_version: 2, scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN", expected_run_at: "2099-01-01T09:15:00Z", due_at: "2099-01-01T09:15:00Z", materialized_at: "2099-01-01T09:15:01Z", next_run_at: "2099-01-02T09:15:00Z", policy, window: { start: "2098-12-03", end: "2099-01-01" }, kind: "REGULAR", outcome: "SKIPPED_IN_FLIGHT", job_id: null, blocking_job_id: "44444444-4444-4444-8444-444444444444" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(overrides: Record<string, object> = {}) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (overrides[path] instanceof Error) throw overrides[path];
    if (Object.hasOwn(overrides, path)) return reply(overrides[path]!);
    switch (path) {
      case "/api/v1/auth/csrf": return reply({ csrf_token: "synthetic-schedule-csrf", expires_at: "2099-01-01T00:00:00Z" });
      case "/api/v1/securities": return reply({ items: [security], next_cursor: null });
      case "/api/v1/securities/{id}": return reply(security);
      case "/api/v1/providers": return reply({ items: [provider], next_cursor: null });
      case "/api/v1/source-schedules": return reply({ items: [schedule], next_cursor: null });
      case "/api/v1/source-schedules/{id}": return reply(schedule);
      case "/api/v1/source-schedules/{id}/occurrences": return reply({ items: [occurrence], next_cursor: null });
      default: throw new Error(`Unexpected schedule read: ${path}`);
    }
  });
}
function renderUI(ui: ReactElement, path = "/") {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Schemas["SourceScheduleUpdate"]; params: { header: Record<string, string> } };
}
async function fill() {
  fireEvent.change(screen.getByLabelText("排程名稱"), { target: { value: schedule.name } });
  fireEvent.change(screen.getByLabelText("排程市場"), { target: { value: "HK" } });
  await screen.findByRole("option", { name: /0005 \/ HKG/ });
  fireEvent.change(screen.getByLabelText("排程證券", { exact: true }), { target: { value: security.id } });
  fireEvent.click(screen.getByRole("button", { name: "加入證券" }));
  fireEvent.change(screen.getByLabelText("最早市場日期"), { target: { value: "2099-01-01" } });
  fireEvent.change(screen.getByLabelText("市場當地執行時間"), { target: { value: "17:15" } });
  fireEvent.change(screen.getByLabelText("週期規則"), { target: { value: "DAILY" } });
  fireEvent.change(screen.getByLabelText("回看窗口單位"), { target: { value: "DAYS" } });
  fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: "30" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /^EODHD/ }));
}
function detail() {
  return renderUI(<Routes><Route path="/data-center/schedules/:id" element={<ScheduleDetailPage />} /></Routes>, `/data-center/schedules/${schedule.id}`);
}

describe("explicit source schedule forms", () => {
  it("creates no inferred scope, provider, cadence, clock or enabled intent", async () => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<ScheduleForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await screen.findByRole("checkbox", { name: /^EODHD/ });
    for (const label of ["排程名稱", "最早市場日期", "市場當地執行時間", "回看窗口數量"]) expect(screen.getByLabelText(label)).toHaveValue("");
    expect(screen.getByLabelText("排程市場")).toHaveValue("");
    expect(screen.getByLabelText("週期規則")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: /^EODHD/ })).not.toBeChecked();
    expect(screen.queryByLabelText(/啟用這個來源排程/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("擷取股息觀測")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "儲存停用排程" }));
    await screen.findByText(/請至少加入一個證券/);
    expect(post).not.toHaveBeenCalled();
  });

  it("posts only explicit disabled schedule policy without financial or credential fields", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(schedule));
    const saved = vi.fn();
    renderUI(<ScheduleForm onSaved={saved} onCancel={vi.fn()} />);
    await fill();
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "儲存停用排程" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(schedule));
    expect(options(post).body).toEqual({ name: schedule.name, policy });
    expect(post.mock.calls[0]![0]).toBe("/api/v1/source-schedules");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each(["time before 16", "fractional window", "zero window", "oversized days", "missing provider", "missing operation", "missing weekly day", "later backfill"])("rejects %s without a write", async invalid => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<ScheduleForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fill();
    if (invalid === "time before 16") fireEvent.change(screen.getByLabelText("市場當地執行時間"), { target: { value: "15:59" } });
    if (invalid === "fractional window") fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: "1.5" } });
    if (invalid === "zero window") fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: "0" } });
    if (invalid === "oversized days") fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: "3661" } });
    if (invalid === "missing provider") fireEvent.click(screen.getByRole("checkbox", { name: /^EODHD/ }));
    if (invalid === "missing operation") fireEvent.click(screen.getByLabelText("擷取股息觀測"));
    if (invalid === "missing weekly day") fireEvent.change(screen.getByLabelText("週期規則"), { target: { value: "WEEKLY" } });
    if (invalid === "later backfill") fireEvent.change(screen.getByLabelText("首次回補起日"), { target: { value: "2099-01-02" } });
    fireEvent.submit(screen.getByRole("form", { name: "建立停用來源排程" }));
    await screen.findAllByRole("alert");
    expect(post).not.toHaveBeenCalled();
  });

  it("sends an explicit weekly calendar-month window and first-backfill without converting it to days", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(schedule));
    renderUI(<ScheduleForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fill();
    fireEvent.change(screen.getByLabelText("週期規則"), { target: { value: "WEEKLY" } });
    fireEvent.change(screen.getByLabelText("每週執行星期"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("回看窗口單位"), { target: { value: "MONTHS" } });
    fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: "24" } });
    fireEvent.change(screen.getByLabelText("首次回補起日"), { target: { value: "2025-01-01" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存停用排程" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body.policy).toEqual({ ...policy, cadence: "WEEKLY", weekday: 4, lookback_days: null, lookback_months: 24, backfill_from_date: "2025-01-01" });
  });

  it("keeps an uncertain command key across candidate search and acknowledgment changes", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockRejectedValue(new TypeError("Synthetic connection loss"));
    renderUI(<ScheduleForm record={schedule} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Explicit synthetic source scheduling correction." } });
    fireEvent.click(screen.getByLabelText(/啟用這個來源排程/));
    fireEvent.click(screen.getByLabelText(/我明白已啟用排程/));
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await screen.findByText(/網絡連線中斷/);
    fireEvent.change(screen.getByLabelText("搜尋排程證券"), { target: { value: "unused candidate" } });
    fireEvent.click(screen.getByLabelText(/我明白已啟用排程/));
    fireEvent.click(screen.getByLabelText(/我明白已啟用排程/));
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
    expect(options(patch, 1)).toEqual(options(patch));
    expect(options(patch).params.header["If-Match"]).toBe('"1"');
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "A different explicit scheduling correction." } });
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(3));
    expect(options(patch, 2).params.header["Idempotency-Key"]).not.toBe(options(patch).params.header["Idempotency-Key"]);
  });

  it("pauses with full frozen policy and explicit reason, never a cancellation or delete", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(schedule));
    const post = vi.spyOn(api, "POST");
    const remove = vi.spyOn(api, "DELETE");
    renderUI(<ScheduleForm record={{ ...schedule, enabled: true }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/啟用這個來源排程/));
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Explicitly pause future automatic source intents." } });
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    expect(options(patch).body).toEqual({ name: schedule.name, policy, enabled: false, reason: "Explicitly pause future automatic source intents." });
    expect(post).not.toHaveBeenCalled(); expect(remove).not.toHaveBeenCalled();
  });

  it("locks a consumed initial backfill date while retaining it in the complete edit body", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(schedule));
    renderUI(<ScheduleForm record={{ ...schedule, initial_backfill_queued: true, policy: { ...policy, backfill_from_date: "2025-01-01" } }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("首次回補起日")).toHaveAttribute("readonly");
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Keep original consumed initial source window." } });
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    expect(options(patch).body.policy.backfill_from_date).toBe("2025-01-01");
  });
  it.each(["1e2", "0001", "121"])("rejects a noncanonical or excessive calendar-month count: %s", async count => {
    reads();
    const post = vi.spyOn(api, "POST");
    renderUI(<ScheduleForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fill();
    fireEvent.change(screen.getByLabelText("回看窗口單位"), { target: { value: "MONTHS" } });
    fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: count } });
    fireEvent.submit(screen.getByRole("form"));
    await screen.findAllByRole("alert");
    expect(post).not.toHaveBeenCalled();
  });

  it.each([["DAYS", "1"], ["DAYS", "3660"], ["MONTHS", "1"], ["MONTHS", "120"]])("preserves the exact valid integer boundary %s/%s", async (unit, count) => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(schedule));
    renderUI(<ScheduleForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fill();
    fireEvent.change(screen.getByLabelText("回看窗口單位"), { target: { value: unit } });
    fireEvent.change(screen.getByLabelText("回看窗口數量"), { target: { value: count } });
    fireEvent.click(screen.getByRole("button", { name: "儲存停用排程" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body.policy).toMatchObject(unit === "DAYS" ? { lookback_days: Number(count), lookback_months: null } : { lookback_days: null, lookback_months: Number(count) });
  });

  it("clears confirmed securities on market change, and incompatible weekday/window quantities on mode change", async () => {
    reads();
    renderUI(<ScheduleForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    await fill();
    await waitFor(() => expect(screen.getByRole("list", { name: "已加入排程證券" })).toHaveTextContent("0005 / HKG"));
    fireEvent.change(screen.getByLabelText("排程市場"), { target: { value: "US" } });
    expect(screen.queryByRole("list", { name: "已加入排程證券" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("排程證券", { exact: true })).toHaveValue("");
    fireEvent.change(screen.getByLabelText("週期規則"), { target: { value: "WEEKLY" } });
    fireEvent.change(screen.getByLabelText("每週執行星期"), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("週期規則"), { target: { value: "WEEKDAYS" } });
    fireEvent.change(screen.getByLabelText("週期規則"), { target: { value: "WEEKLY" } });
    expect(screen.getByLabelText("每週執行星期")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("回看窗口單位"), { target: { value: "MONTHS" } });
    expect(screen.getByLabelText("回看窗口數量")).toHaveValue("");
    expect(screen.getByText(/America\/New_York（隨夏令時間轉 UTC）/)).toBeVisible();
  });

  it.each(["missing reason", "missing activation acknowledgement", "invalid manual profile"])("blocks an unsafe edit with %s", async invalid => {
    reads();
    const patch = vi.spyOn(api, "PATCH");
    renderUI(<ScheduleForm record={{ ...schedule, policy: invalid === "invalid manual profile" ? { ...policy, provider_ids: ["eodhd", "manual"] } : policy }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    if (invalid !== "missing reason") fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Explicit synthetic schedule correction." } });
    if (invalid === "missing activation acknowledgement") fireEvent.click(screen.getByLabelText(/啟用這個來源排程/));
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await screen.findAllByRole("alert");
    expect(patch).not.toHaveBeenCalled();
  });

  it("keeps the complete edit inputs on authoritative scope rejection instead of inferring market from USD", async () => {
    reads();
    const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { code: "INVALID_SOURCE_SCHEDULE_SCOPE" }, response: new Response(null, { status: 422 }) });
    const saved = vi.fn();
    renderUI(<ScheduleForm onSaved={saved} onCancel={vi.fn()} />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存停用排程" }));
    await screen.findByText(/不能用報價幣別推定/);
    expect(options(post).body.policy.market).toBe("HK");
    expect(screen.getByLabelText("排程名稱")).toHaveValue(schedule.name);
    expect(screen.getByLabelText("市場當地執行時間")).toHaveValue("17:15");
    expect(saved).not.toHaveBeenCalled();
  });

  it("allows an explicit pause even when current provider diagnostics are unavailable", async () => {
    reads({ "/api/v1/providers": new TypeError("Synthetic provider diagnostics outage") });
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(schedule));
    renderUI(<ScheduleForm record={{ ...schedule, enabled: true }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/啟用這個來源排程/));
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Explicit pause without loading private provider state." } });
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    expect(options(patch).body.policy.provider_ids).toEqual(["eodhd"]);
  });

  it("does not mark a pending create saved and prevents discard until the confirmed response", async () => {
    reads();
    let complete!: (value: ReturnType<typeof reply< Schemas["SourceScheduleRecord"]>>) => void;
    vi.spyOn(api, "POST").mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    const saved = vi.fn();
    renderUI(<ScheduleForm onSaved={saved} onCancel={vi.fn()} />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "儲存停用排程" }));
    await waitFor(() => expect(screen.getByRole("form")).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(screen.getByLabelText("排程名稱")).toBeDisabled();
    expect(saved).not.toHaveBeenCalled();
    await act(async () => { complete(reply(schedule)); });
    await waitFor(() => expect(saved).toHaveBeenCalledWith(schedule));
  });
});

describe("owned schedule workspace and immutable receipts", () => {
  it("renders explicit policy and saved state without claiming health, income or trading sessions", async () => {
    reads();
    renderUI(<SchedulesPage />);
    expect(await screen.findByText(schedule.name)).toBeVisible();
    expect(screen.getByText(/不是交易所日曆/)).toBeVisible();
    expect(screen.getByText(/未啟用/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /立即執行|刪除排程|確認股息/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看排程評估紀錄" })).toHaveAttribute("href", `/data-center/schedules/${schedule.id}`);
  });

  it("freezes the observed version and inputs even if background metadata advances", async () => {
    reads();
    const patch = vi.spyOn(api, "PATCH").mockRejectedValue(new Error("Synthetic stale edit"));
    const cache = renderUI(<SchedulesPage />);
    await screen.findByText(schedule.name);
    fireEvent.click(screen.getByRole("button", { name: "編輯、啟用或暫停排程" }));
    await screen.findByRole("dialog", { name: "編輯來源排程" });
    fireEvent.change(screen.getByLabelText("排程名稱"), { target: { value: "Observed editor input" } });
    await act(async () => { cache.setQueryData(["sources", "schedules", "", "", undefined], { items: [{ ...schedule, version: 7, name: "Server changed name" }], next_cursor: null }); });
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Explicit observed version correction." } });
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    expect(options(patch).params.header["If-Match"]).toBe('"1"');
    expect(options(patch).body.name).toBe("Observed editor input");
  });

  it("shows frozen skip/window/job linkage rather than zero income or queue success", async () => {
    reads(); detail();
    expect(await screen.findByText(/SKIPPED_IN_FLIGHT/)).toBeVisible();
    expect(screen.getByText("2098-12-03 / 2099-01-01")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看阻擋中的來源工作" })).toHaveAttribute("href", `/data-center/jobs/${occurrence.blocking_job_id}`);
    expect(screen.getByText(/不是來源成功或已收收入/)).toBeVisible();
  });

  it("does not turn an empty history into no dividends and never mutates on reads", async () => {
    reads({ "/api/v1/source-schedules/{id}/occurrences": { items: [], next_cursor: null } });
    const post = vi.spyOn(api, "POST"), patch = vi.spyOn(api, "PATCH");
    detail();
    expect(await screen.findByText(/尚無排程評估紀錄/)).toBeVisible();
    expect(post).not.toHaveBeenCalled(); expect(patch).not.toHaveBeenCalled();
  });

  it("filters enabled intent with a real boolean and server cursor rather than client sorting", async () => {
    const get = reads({ "/api/v1/source-schedules": { items: [schedule], next_cursor: "opaque-schedule-page" } });
    renderUI(<SchedulesPage />);
    await screen.findByText(schedule.name);
    fireEvent.change(screen.getByLabelText("排程啟用狀態"), { target: { value: "true" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/source-schedules", expect.objectContaining({ params: { query: { q: "", cursor: undefined, limit: 25, enabled: true } }, signal: expect.any(AbortSignal) })));
    await waitFor(() => expect(screen.getByRole("button", { name: "下一頁" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/source-schedules", expect.objectContaining({ params: { query: { q: "", cursor: "opaque-schedule-page", limit: 25, enabled: true } }, signal: expect.any(AbortSignal) })));
    await screen.findByText(schedule.name);
    expect(within(screen.getByRole("region", { name: "來源排程列表" })).getByText(/非全部紀錄/)).toBeVisible();
  });

  it("renders safe integrity failure without echoing corrupt payload or offering repair", async () => {
    reads();
    const get = vi.spyOn(api, "GET").mockResolvedValueOnce({ error: { code: "SOURCE_SCHEDULE_UNAVAILABLE", message: "synthetic-private-corrupt-policy" }, response: new Response(null, { status: 503 }) });
    const patch = vi.spyOn(api, "PATCH"), post = vi.spyOn(api, "POST");
    detail();
    expect(await screen.findByText(/沒有自動修補/)).toBeVisible();
    expect(screen.queryByText(/synthetic-private-corrupt-policy/)).not.toBeInTheDocument();
    expect((get.mock.calls as readonly (readonly unknown[])[]).filter(call => call[0] === "/api/v1/source-schedules/{id}/occurrences")).toHaveLength(0);
    expect(post).not.toHaveBeenCalled(); expect(patch).not.toHaveBeenCalled();
  });

  it("preserves historical capability false without inventing healthy automatic execution", async () => {
    reads({ "/api/v1/source-schedules/{id}": { ...schedule, automatic_materialization_installed: false, enabled: true } });
    detail();
    expect(await screen.findByText(/尚未宣告安裝；已啟用也只是保存意圖/)).toBeVisible();
    expect(screen.queryByText(/已安裝程式路徑/)).not.toBeInTheDocument();
  });

  it("refetches receipt rows when a refreshed definition version advances", async () => {
    const get = reads();
    const cache = detail();
    await screen.findByText(/SKIPPED_IN_FLIGHT/);
    expect((get.mock.calls as readonly (readonly unknown[])[]).filter(call => call[0] === "/api/v1/source-schedules/{id}/occurrences")).toHaveLength(1);
    await act(async () => { cache.setQueryData(["sources", "schedule", schedule.id], { ...schedule, version: 7, occurrence_count: 2 }); });
    await waitFor(() => expect((get.mock.calls as readonly (readonly unknown[])[]).filter(call => call[0] === "/api/v1/source-schedules/{id}/occurrences")).toHaveLength(2));
  });

  it("invalidates source queries only after a confirmed schedule edit", async () => {
    reads();
    vi.spyOn(api, "PATCH").mockResolvedValue(reply({ ...schedule, version: 2 }));
    const cache = renderUI(<SchedulesPage />);
    cache.setQueryData(["ledger", "synthetic"], { original: true });
    cache.setQueryData(["portfolios", "synthetic"], { original: true });
    const invalidate = vi.spyOn(cache, "invalidateQueries");
    await screen.findByText(schedule.name);
    fireEvent.click(screen.getByRole("button", { name: "編輯、啟用或暫停排程" }));
    fireEvent.change(screen.getByLabelText("排程修訂依據"), { target: { value: "Explicit metadata-only schedule correction." } });
    fireEvent.click(screen.getByRole("button", { name: "儲存排程修訂" }));
    await screen.findByText(/已儲存來源排程版本 2/);
    expect(invalidate).toHaveBeenCalledExactlyOnceWith({ queryKey: ["sources"] });
    expect(cache.getQueryState(["ledger", "synthetic"])?.isInvalidated).toBe(false);
    expect(cache.getQueryState(["portfolios", "synthetic"])?.isInvalidated).toBe(false);
  });
});
