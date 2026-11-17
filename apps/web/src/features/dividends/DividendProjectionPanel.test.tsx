import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendProjectionPanel } from "./DividendProjectionPanel";

type Schemas = components["schemas"];
const event: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 3,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null,
  pay_date: null, dps: "1.00000000", currency: "USD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false,
  manual_override: false, decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic event reference", url: null, url_withheld: false }, reason: "Synthetic projection event decision",
  created_at: "2026-11-01T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const account: Schemas["Account"] = {
  id: "44444444-4444-4444-8444-444444444444", portfolio_id: "55555555-5555-4555-8555-555555555555", broker_id: "66666666-6666-4666-8666-666666666666",
  name: "Synthetic projection account", default_currency: "USD", status: "ACTIVE", version: 1, opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN",
};
const priorId = "88888888-8888-4888-8888-888888888888";
const record: Schemas["ProjectionRecord"] = {
  id: "a7777777-7777-4777-8777-777777777777", event_id: event.id, revision_id: "99999999-9999-4999-8999-999999999999", revision_no: 2,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id,
  account_version: "9007199254740993", broker_version: "9223372036854775807", portfolio_data_revision: "9007199254740993",
  projection_no: "9007199254740993", previous_id: priorId, state: "EXPECTED", as_of: "2026-11-02", ex_date: "2026-11-03",
  timing_scope: "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY", currency: "USD", eligibility_rule: "ORDINARY_PRE_EX_DATE",
  retired_trade_scope: "NO_RETIRED_TRADES", creation_audit_scope: "NO_RECORDED_CREATION_GAP_REVIEW",
  recorded_shares: "10.00000000", estimated_amounts: { currency: "USD", gross_local: "10.000000", tax_local: "2.000000", fee_local: "0.500000",
    net_local: "7.500000", rounding_delta_local: "0.000000", estimated_hkd: null, quality: "MISSING_FX" },
  tax_rate: "0.200000", fee_original: "0.500000", fee_currency: "USD", fee_rate_event: "1.000000000000", fee_rate_source: "IDENTITY",
  fx_rate_hkd: null, fx_source: null,
  tax_provenance: { effective_on: "2026-11-03", selected_setting_id: priorId, observed_head_no: "1", observed_head_id: priorId, status: "ESTIMATED", scope: "OWNER_ESTIMATE_ONLY" },
  fee_provenance: null, created_at: "2026-11-04T00:00:00Z", reason: "Synthetic recorded projection reason",
  history_completeness: "UNVERIFIED", scope: "UNVERIFIED_PROJECTION_HISTORY_ONLY", current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED",
};
const historyPath = "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads(value: unknown = { items: [record], next_cursor: null }) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
    if (path === historyPath) return reply(value);
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderPanel() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><DividendProjectionPanel event={event} /></QueryClientProvider>);
  return cache;
}
async function selectAccount() { fireEvent.click(await screen.findByRole("button", { name: `選擇投影帳戶 ${account.name}` })); }

describe("recorded dividend projection history", () => {
  it("requires explicit account selection and displays exact unverified historical values", async () => {
    const get = reads(); const post = vi.spyOn(api, "POST"); const cache = renderPanel();
    expect(get).not.toHaveBeenCalledWith(historyPath, expect.anything());
    await selectAccount(); const panel = await screen.findByRole("region", { name: "帳戶投影歷史" });
    expect(await within(panel).findByText(/僅為除淨日前的已記錄持倉/)).toBeVisible();
    expect(panel).toHaveTextContent("與目前載入的事件版本不同");
    expect(panel).toHaveTextContent("9007199254740993"); expect(panel).toHaveTextContent("9223372036854775807");
    expect(panel).toHaveTextContent("10.00000000"); expect(panel).toHaveTextContent("7.500000");
    expect(panel).toHaveTextContent("缺少港幣匯率"); expect(panel).toHaveTextContent("不是已確認資格或實收");
    expect(panel).toHaveTextContent("舊投影沒有保留費用估算來源");
    expect(post).not.toHaveBeenCalled(); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("does not turn an empty history into zero or confirmed absence", async () => {
    reads({ items: [], next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByText(/沒有已記錄的投影；不代表持倉或股息為零/)).toBeVisible();
  });

  it.each([
    { ...record, event_id: account.id }, { ...record, account_id: event.id }, { ...record, projection_no: "01" },
    { ...record, account_version: 1 }, { ...record, previous_id: record.id }, { ...record, timing_scope: "ON_OR_AFTER_EX_DATE_RECORDED_HISTORY_ONLY" },
    { ...record, current_use_scope: "GRANTED" }, { ...record, eligibility_scope: "ESTABLISHED" },
    { ...record, state: "NO_POSITION" }, { ...record, estimated_amounts: { ...record.estimated_amounts!, net_local: "0.000000" } },
    { ...record, tax_provenance: { ...record.tax_provenance!, status: "MISSING" } },
    { ...record, reason: " Synthetic untrimmed projection reason" }, { ...record, reason: "Synthetic\0projection reason" },
  ])("rejects malformed, foreign or inconsistent projection records", async broken => {
    reads({ items: [broken], next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByRole("alert")).toHaveTextContent("投影歷史範圍無法安全核對");
    expect(screen.queryByText(/僅為除淨日前的已記錄持倉/)).not.toBeInTheDocument();
  });

  it.each([
    { ...record, fee_rate_event: "1.100000000000" },
    { ...record, fx_rate_hkd: "7.800000000000", fx_source: String.fromCharCode(0x1c) + "manual", estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "58.500000", quality: "OK" } },
    { ...record, fx_rate_hkd: "7.800000000000", fx_source: "😀".repeat(101), estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "58.500000", quality: "OK" } },
    { ...record, fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY", estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "7.500000", quality: "OK" } },
    { ...record, estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "1.000000", quality: "NEEDS_REVIEW" } },
    { ...record, tax_provenance: { ...record.tax_provenance!, observed_head_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" } },
    { ...record, fx_rate_hkd: "7.800000000000", fx_source: "MANUAL", estimated_amounts: { ...record.estimated_amounts!, gross_local: "0.000000", tax_local: "1.000000", fee_local: "0.000000", net_local: "-1.000000", rounding_delta_local: "0.000000", estimated_hkd: "-7.800000", quality: "OK" } },
  ])("rejects inconsistent FX, provenance or negative quality", async broken => {
    reads({ items: [broken], next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByRole("alert")).toHaveTextContent("投影歷史範圍無法安全核對");
  });

  it.each([String.fromCodePoint(0xfeff) + "synthetic", "😀".repeat(60)])("preserves valid Python source text %s", async source => {
    const value = { ...record, fx_rate_hkd: "7.800000000000", fx_source: source,
      estimated_amounts: { ...record.estimated_amounts!, estimated_hkd: "58.500000", quality: "OK" } };
    reads({ items: [value], next_cursor: null }); renderPanel(); await selectAccount();
    expect(await screen.findByText(/港幣估值 58.500000/)).toBeVisible();
  });

  it("rejects duplicate or out-of-order projection IDs", async () => {
    reads({ items: [record, { ...record, id: record.id.toUpperCase(), projection_no: "2" }], next_cursor: null });
    renderPanel(); await selectAccount(); expect(await screen.findByRole("alert")).toHaveTextContent("投影歷史範圍無法安全核對");
  });

  it("keeps missing and unknown settings distinct from explicit zero", async () => {
    const pending: Schemas["ProjectionRecord"] = { ...record, state: "NEEDS_REVIEW", recorded_shares: null, estimated_amounts: null,
      tax_rate: null, fee_original: null, fee_currency: null, fee_rate_event: null, fee_rate_source: null, fx_rate_hkd: null, fx_source: null,
      tax_provenance: { effective_on: "2026-11-03", selected_setting_id: null, observed_head_no: "0", observed_head_id: null, status: "MISSING", scope: "OWNER_ESTIMATE_ONLY" },
      fee_provenance: { effective_on: "2026-11-03", selected_setting_id: priorId, observed_head_no: "1", observed_head_id: priorId, status: "UNKNOWN", scope: "OWNER_ESTIMATE_ONLY" } };
    reads({ items: [pending], next_cursor: null }); renderPanel(); await selectAccount();
    const panel = await screen.findByRole("region", { name: "帳戶投影歷史" });
    expect(await within(panel).findByText(/需要核對；沒有可計算金額/)).toBeVisible();
    expect(panel).toHaveTextContent("稅率估算：未設定，不是零"); expect(panel).toHaveTextContent("費用估算：未知，不是零");
    expect(panel).not.toHaveTextContent("0.000000");
  });

  it("pages retained versions without converting their exact identifiers", async () => {
    const get = reads(); const older = { ...record, id: priorId, projection_no: "1", previous_id: null };
    get.mockImplementation(async (path, options) => {
      if (path === "/api/v1/accounts") return reply({ items: [account], next_cursor: null });
      const request = options as { params: { query: { cursor?: string } } };
      if (path === historyPath) return reply(request.params.query.cursor ? { items: [older], next_cursor: null } : { items: [record], next_cursor: "next-page" });
      throw new Error("Unexpected read");
    });
    renderPanel(); await selectAccount(); expect(await screen.findByText(/投影版本 9007199254740993/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下一頁投影歷史" }));
    expect(await screen.findByText(/投影版本 1；/)).toBeVisible();
    expect(screen.queryByText(/投影版本 9007199254740993/)).not.toBeInTheDocument();
  });

  it("discards a late history response after choosing another account", async () => {
    const other = { ...account, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", name: "Synthetic other projection account" };
    let deliver!: () => void; let signal!: AbortSignal;
    vi.spyOn(api, "GET").mockImplementation(async (path, options) => {
      if (path === "/api/v1/accounts") return reply({ items: [account, other], next_cursor: null });
      const request = options as { signal: AbortSignal; params: { path: { account_id: string } } };
      if (path === historyPath && request.params.path.account_id === account.id) return new Promise(resolve => { signal = request.signal; deliver = () => resolve(reply({ items: [record], next_cursor: null })); });
      if (path === historyPath) return reply({ items: [], next_cursor: null });
      throw new Error("Unexpected read");
    });
    renderPanel(); await selectAccount(); await waitFor(() => expect(deliver).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: `選擇投影帳戶 ${other.name}` }));
    await waitFor(() => expect(signal.aborted).toBe(true)); await act(async () => deliver());
    expect(await screen.findByText(/沒有已記錄的投影；不代表持倉或股息為零/)).toBeVisible();
    expect(screen.queryByText(/投影版本 9007199254740993/)).not.toBeInTheDocument();
  });

  it("keeps offline data visibly stale and does not allow page navigation", async () => {
    reads({ items: [record], next_cursor: "synthetic-next-page" }); renderPanel(); await selectAccount();
    expect(await screen.findByText(/僅為除淨日前的已記錄持倉/)).toBeVisible();
    act(() => onlineManager.setOnline(false));
    expect(await screen.findByText(/連線已暫停；顯示的是上次讀取/)).toBeVisible();
    expect(screen.getByRole("button", { name: "下一頁投影歷史" })).toBeDisabled();
    act(() => onlineManager.setOnline(true));
  });
});
