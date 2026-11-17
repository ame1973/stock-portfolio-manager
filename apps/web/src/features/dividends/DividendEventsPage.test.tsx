import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendEventsPage } from "./DividendEventsPage";

type Schemas = components["schemas"];
const event: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 2,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null,
  record_date: null, pay_date: null, dps: "92233720368.54775807", currency: "USD", adjustment_basis: "AS_DECLARED",
  workflow_status: "VERIFIED", manual_locked: true, manual_override: false, decision_kind: "VERIFY", candidate_revision_id: null,
  independent_origin_groups: null, assessment: null, evidence: [], reference: { citation: "Synthetic document reference", url: null, url_withheld: true },
  reason: "Synthetic decision, not a verification of income", created_at: "2026-11-04T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const rejection: Schemas["RejectionDecision"] = {
  id: "44444444-4444-4444-8444-444444444444", event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
  decision: "REJECT_REVISION", reference: { citation: "Synthetic rejection citation", url: "https://example.test/source", url_withheld: false },
  reason: "Synthetic rejection reason", created_at: "2026-11-05T00:00:00Z", scope: "OWNER_REVISION_REJECTION_ONLY",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const state = (rejected = false): Schemas["RejectionState"] => ({ event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
  state: rejected ? "REJECTED" : "NOT_REJECTED", rejection: rejected ? rejection : null, scope: "OWNER_REJECTION_STATE_ONLY",
  current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED" });
const revision: Schemas["DividendEventRevisionRecord"] = {
  ...event, event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const empty = { items: [], next_cursor: null };
function reads(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (Object.hasOwn(overrides, path)) return reply(overrides[path]);
    switch (path) {
      case "/api/v1/dividends/events": return reply({ items: [event], next_cursor: null });
      case "/api/v1/dividends/events/{event_id}/rejection": return reply(state());
      case "/api/v1/dividends/events/{event_id}/revisions": return reply({ items: [revision], next_cursor: null });
      case "/api/v1/dividends/events/{event_id}/rejections": return reply(empty);
      case "/api/v1/securities": return reply(empty);
      case "/api/v1/securities/{id}": return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      default: throw new Error(`Unexpected read: ${path}`);
    }
  });
}
function renderPage() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter><DividendEventsPage /></MemoryRouter></QueryClientProvider>);
  return cache;
}
async function openEvent() {
  fireEvent.click(await screen.findByRole("button", { name: "查看事件與拒絕狀態" }));
  return await screen.findByRole("region", { name: "獨立拒絕狀態" });
}

describe("dividend event workspace", () => {
  it("opens recorded projection history without creating a projection", async () => {
    reads({ "/api/v1/accounts": empty }); const post = vi.spyOn(api, "POST"); renderPage(); await openEvent();
    fireEvent.click(screen.getByRole("button", { name: "查看帳戶投影歷史" }));
    const panel = await screen.findByRole("region", { name: "已記錄股息投影" });
    expect(panel).toHaveTextContent("查閱不會建立持倉、應收或實收"); expect(post).not.toHaveBeenCalled();
  });
  it("opens account-scoped estimate reads without posting or assuming eligibility", async () => {
    reads({ "/api/v1/accounts": empty }); const post = vi.spyOn(api, "POST"); renderPage(); await openEvent();
    fireEvent.click(screen.getByRole("button", { name: "查看帳戶稅費估算" }));
    const panel = await screen.findByRole("region", { name: "帳戶稅費估算" });
    expect(panel).toHaveTextContent("查閱不會建立持倉、應收、實收或換匯"); expect(post).not.toHaveBeenCalled();
  });

  it("opens an estimate sheet outside the event card with password confirmation", async () => {
    const account: Schemas["Account"] = { id: "55555555-5555-4555-8555-555555555555", portfolio_id: "66666666-6666-4666-8666-666666666666",
      broker_id: "77777777-7777-4777-8777-777777777777", name: "Synthetic estimate account", default_currency: "USD", status: "ACTIVE", version: 1,
      opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN" };
    const scope = { event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id,
      portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: "1", broker_version: "1", effective_on: "2026-11-03",
      head_no: "0", head_id: null, selected: null, selected_scope: "NO_SELECTED_SETTING", scope: "OWNER_ESTIMATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED" };
    reads({ "/api/v1/accounts": { items: [account], next_cursor: null },
      "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings": { ...scope, tax_status: "MISSING" },
      "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings": { ...scope, fee_status: "MISSING", revision_rejection: "NOT_REJECTED" } });
    const post = vi.spyOn(api, "POST"); const cache = renderPage(); await openEvent();
    fireEvent.click(screen.getByRole("button", { name: "查看帳戶稅費估算" }));
    fireEvent.click(await screen.findByRole("button", { name: "選擇估算帳戶 Synthetic estimate account" }));
    fireEvent.change(screen.getByLabelText("查閱估算生效日"), { target: { value: "2026-11-03" } });
    fireEvent.click(screen.getByRole("button", { name: "核對拒絕狀態以記錄估算" }));
    const button = await screen.findByRole("button", { name: "記錄稅率估算" }); await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    const dialog = await screen.findByRole("dialog", { name: "記錄稅率估算" });
    expect(within(dialog).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(dialog).getByRole("form", { name: "記錄稅率估算" })).toBeVisible();
    expect(dialog).toHaveTextContent(event.current_revision_id); expect(dialog).toHaveTextContent(account.id); expect(post).not.toHaveBeenCalled();
    fireEvent.change(within(dialog).getByLabelText("估算依據"), { target: { value: "Synthetic pending estimate reason." } });
    await act(async () => cache.setQueriesData({ queryKey: ["dividends", "events"] }, { items: [{ ...event, version: 3,
      current_revision_id: "88888888-8888-4888-8888-888888888888" }], next_cursor: null }));
    expect(within(dialog).getByLabelText("估算依據")).toHaveValue("Synthetic pending estimate reason.");
    expect(dialog).toHaveTextContent(event.current_revision_id); expect(post).not.toHaveBeenCalled();
  });

  it("opens a distinct password-confirmed creation sheet without writing on open or cancel", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderPage();
    const opener = screen.getByRole("button", { name: "新增人工股息事件" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "新增人工股息事件" });
    expect(within(dialog).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(dialog).getByRole("form", { name: "新增人工股息事件" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole("button", { name: "關閉編輯" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
    expect(post).not.toHaveBeenCalled();
  });

  it("shows a safe empty state without treating missing events as zero income", async () => {
    const get = reads({ "/api/v1/dividends/events": empty });
    const post = vi.spyOn(api, "POST");
    renderPage();
    expect(await screen.findByText(/尚無可顯示的股息事件/)).toHaveTextContent("不是已確認沒有股息");
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/rejection", expect.anything());
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/rejections", expect.anything());
    expect(post).not.toHaveBeenCalled();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("shows retained field-level evidence and missing source details without interpreting them as income", async () => {
    const observationId = "88888888-8888-4888-8888-888888888888";
    const candidate: Schemas["DividendEventRecord"] = { ...event, decision_kind: "CANDIDATE", workflow_status: "INCOMPLETE", manual_locked: false, reference: null,
      evidence: [{ observation_id: observationId, fields: ["ex_date", "dps"] }], independent_origin_groups: 1,
      assessment: { status: "INCOMPLETE", independent_origins: ["EODHD"], eligible_observation_ids: [observationId], conflicting_fields: [], incomplete_reasons: ["MISSING_CURRENCY"],
        checks: [{ observation_id: observationId, supporting_fields: ["ex_date"], conflicting_fields: ["dps"], incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"], comparison_dps: "0.00000001", comparison_basis: "UNKNOWN", basis_assertion_id: null }] } };
    reads({ "/api/v1/dividends/events": { items: [candidate], next_cursor: null } });
    renderPage();
    await openEvent();
    const sources = screen.getByRole("region", { name: "已關聯來源觀測" });
    expect(within(sources).getByText(/除淨日、每股金額/)).toBeVisible();
    const assessment = screen.getByRole("region", { name: "已記錄候選評估" });
    expect(assessment).toHaveTextContent("MISSING_CURRENCY");
    expect(assessment).toHaveTextContent("UNRESOLVED_ADJUSTMENT");
    expect(assessment).toHaveTextContent("比較金額 0.00000001");
    expect(assessment).toHaveTextContent("不是收入資格");
  });

  it("offers a safe retry when the event history cannot be read", async () => {
    const get = reads();
    let attempts = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return ++attempts === 1
        ? { error: { code: "DIVIDEND_EVENT_HISTORY_UNAVAILABLE", message: "synthetic private history" }, response: new Response(null, { status: 503 }) }
        : reply(empty);
      return reply(empty);
    });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("股息事件歷史無法安全核對");
    expect(screen.queryByText(/synthetic private history/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重試載入股息事件" }));
    expect(await screen.findByText(/尚無可顯示的股息事件/)).toBeVisible();
  });

  it("keeps exact terms and independently reads rejection without claiming verification", async () => {
    const get = reads();
    const post = vi.spyOn(api, "POST");
    renderPage();
    expect(await screen.findByText("USD 92233720368.54775807")).toBeVisible();
    expect(within(screen.getByRole("article", { name: `股息事件 ${event.id}` })).getByText("人工接受（非文件驗證）")).toBeVisible();
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/rejection", expect.anything());
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/rejections", expect.anything());
    const panel = await openEvent();
    expect(await within(panel).findByText(/此版本未記錄拒絕/)).toBeVisible();
    expect(within(panel).getByText(/不是接受決定或財務資格/)).toBeVisible();
    expect(screen.getByText(/兩者分開讀取，不是同一快照/)).toBeVisible();
    expect(screen.getByText("Synthetic document reference")).toBeVisible();
    expect(screen.getByText(/引用包含查詢或片段，連結未提供/)).toBeVisible();
    expect(screen.queryByRole("link", { name: /開啟事件引用/ })).not.toBeInTheDocument();
    expect(screen.getByText(/宣告：未知；記錄：未知；付款：未知/)).toBeVisible();
    expect(post).not.toHaveBeenCalled();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("reloads rejection independently even when the event version is unchanged", async () => {
    const get = reads();
    let responses = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return reply({ items: [event], next_cursor: null });
      if (path === "/api/v1/dividends/events/{event_id}/rejection") return reply(state(++responses > 1));
      if (path === "/api/v1/securities/{id}") return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      return reply(empty);
    });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByText(/此版本未記錄拒絕/)).toBeVisible();
    fireEvent.click(within(panel).getByRole("button", { name: "重新載入拒絕狀態" }));
    expect(await within(panel).findByText(/此版本已記錄拒絕/)).toBeVisible();
    expect(within(panel).getByText("Synthetic rejection reason")).toBeVisible();
    expect(within(panel).getByRole("link", { name: /開啟拒絕引用/ })).toHaveAttribute("rel", "noopener noreferrer");
    expect(responses).toBe(2);
  });

  it("opens candidate association only for a fresh unlocked event and matching rejection state", async () => {
    const unlocked = { ...event, workflow_status: "PROVISIONAL", manual_locked: false };
    reads({ "/api/v1/dividends/events": { items: [unlocked], next_cursor: null }, "/api/v1/dividends/candidates": empty,
      "/api/v1/dividends/events/{event_id}/rejection": state(true) });
    const post = vi.spyOn(api, "POST"); renderPage();
    const panel = await openEvent();
    const match = await within(panel).findByRole("button", { name: "關聯候選至此事件" });
    match.focus(); fireEvent.click(match);
    const sheet = await screen.findByRole("dialog", { name: "關聯候選至現有事件" });
    expect(within(sheet).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(sheet).toHaveTextContent(event.current_revision_id); expect(sheet).toHaveTextContent(rejection.id);
    expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus(); expect(post).not.toHaveBeenCalled();
  });

  it.each([
    { ...event, manual_locked: true }, { ...event, manual_locked: false, workflow_status: "WITHDRAWN" },
    { ...event, manual_locked: false, dps: "1e2" },
  ])("does not offer candidate association to a locked, withdrawn or malformed event", async broken => {
    reads({ "/api/v1/dividends/events": { items: [broken], next_cursor: null } }); renderPage();
    const panel = await openEvent(); await within(panel).findByText(/此版本未記錄拒絕/);
    expect(within(panel).queryByRole("button", { name: "關聯候選至此事件" })).not.toBeInTheDocument();
  });

  it("does not offer candidate association for a mismatched rejection read", async () => {
    const unlocked = { ...event, workflow_status: "PROVISIONAL", manual_locked: false };
    reads({ "/api/v1/dividends/events": { items: [unlocked], next_cursor: null },
      "/api/v1/dividends/events/{event_id}/rejection": { ...state(), revision_no: event.version + 1 } });
    renderPage(); const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("版本不一致");
    expect(within(panel).queryByRole("button", { name: "關聯候選至此事件" })).not.toBeInTheDocument();
  });

  it("does not allow candidate association from a stale event-list read", async () => {
    const unlocked = { ...event, workflow_status: "PROVISIONAL", manual_locked: false };
    const get = reads(); let attempts = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return ++attempts === 1 ? reply({ items: [unlocked], next_cursor: null })
        : { error: { code: "DIVIDEND_EVENT_HISTORY_UNAVAILABLE", message: "private synthetic" }, response: new Response(null, { status: 503 }) };
      if (path === "/api/v1/dividends/events/{event_id}/rejection") return reply(state());
      if (path === "/api/v1/securities/{id}") return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      return reply(empty);
    });
    renderPage(); const panel = await openEvent();
    const match = await within(panel).findByRole("button", { name: "關聯候選至此事件" }); expect(match).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重新載入股息事件" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("股息事件歷史無法安全核對"); expect(match).toBeDisabled();
  });

  it("offers a password-confirmed rejection only after a fresh clear state", async () => {
    reads(); const post = vi.spyOn(api, "POST");
    renderPage();
    const panel = await openEvent();
    const reject = await within(panel).findByRole("button", { name: "拒絕此事件版本" });
    reject.focus(); fireEvent.click(reject);
    const sheet = await screen.findByRole("dialog", { name: "拒絕股息事件版本" });
    expect(within(sheet).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(sheet).getByRole("form", { name: "拒絕股息事件版本" })).toHaveTextContent(event.current_revision_id);
    expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    expect(post).not.toHaveBeenCalled();
  });

  it("does not offer a second rejection of an already rejected version", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": state(true) }); renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByText(/此版本已記錄拒絕/)).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
  });

  it("opens a password-confirmed acceptance for a fresh matching clear state", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderPage();
    const panel = await openEvent();
    const accept = await within(panel).findByRole("button", { name: "人工接受此版本" });
    accept.focus(); fireEvent.click(accept);
    const sheet = await screen.findByRole("dialog", { name: "人工接受股息事件版本" });
    expect(within(sheet).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(sheet).getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent(event.current_revision_id);
    expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    expect(within(sheet).queryByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("offers acceptance with the exact observed rejection pin and separate confirmation", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": state(true) }); renderPage();
    const panel = await openEvent();
    fireEvent.click(await within(panel).findByRole("button", { name: "人工接受此版本" }));
    const sheet = await screen.findByRole("dialog", { name: "人工接受股息事件版本" });
    expect(within(sheet).getByRole("form", { name: "人工接受股息事件版本" })).toHaveTextContent(rejection.id);
    expect(within(sheet).getByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeChecked();
  });

  it.each([
    { ...event, adjustment_basis: "UNKNOWN" }, { ...event, adjustment_basis: "SPLIT_ADJUSTED" },
    { ...event, workflow_status: "WITHDRAWN" }, { ...event, dps: "1e2" }, { ...event, currency: "CNY" },
  ])("does not offer acceptance for withdrawn, unresolved or malformed event terms", async broken => {
    reads({ "/api/v1/dividends/events": { items: [broken], next_cursor: null } }); renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByText(/不能開始人工接受/)).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
  });

  it("opens a password-confirmed withdrawal from a fresh clear state", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderPage();
    const panel = await openEvent();
    const withdraw = await within(panel).findByRole("button", { name: "撤回此事件" });
    withdraw.focus(); fireEvent.click(withdraw);
    const sheet = await screen.findByRole("dialog", { name: "撤回股息事件" });
    expect(within(sheet).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(sheet).getByRole("form", { name: "撤回股息事件" })).toHaveTextContent(event.current_revision_id);
    expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    expect(post).not.toHaveBeenCalled();
  });

  it("offers withdrawal with the exact observed rejection and a separate confirmation", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": state(true) }); renderPage();
    const panel = await openEvent();
    fireEvent.click(await within(panel).findByRole("button", { name: "撤回此事件" }));
    const sheet = await screen.findByRole("dialog", { name: "撤回股息事件" });
    expect(within(sheet).getByRole("form", { name: "撤回股息事件" })).toHaveTextContent(rejection.id);
    expect(within(sheet).getByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeChecked();
  });

  it.each(["UNKNOWN", "SPLIT_ADJUSTED"] as const)("allows withdrawal with an explicitly %s basis", async basis => {
    reads({ "/api/v1/dividends/events": { items: [{ ...event, adjustment_basis: basis }], next_cursor: null } }); renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("button", { name: "撤回此事件" })).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
  });

  it.each([
    { ...event, workflow_status: "WITHDRAWN" }, { ...event, dps: "1e2" }, { ...event, adjustment_basis: "INVALID" },
  ])("does not offer withdrawal for a withdrawn or malformed event", async broken => {
    reads({ "/api/v1/dividends/events": { items: [broken], next_cursor: null } }); renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByText(/不能開始撤回/)).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
  });

  it.each([
    { ...event, manual_locked: "yes" }, { ...event, manual_locked: undefined },
    { ...event, manual_override: undefined }, { ...event, workflow_status: "INVALID" },
  ])("does not enable decisions from malformed observed status or manual flags", async broken => {
    reads({ "/api/v1/dividends/events": { items: [broken], next_cursor: null } }); renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("範圍無法核對");
    for (const name of ["修訂人工股息事件", "人工接受此版本", "拒絕此事件版本", "撤回此事件", "人工覆寫此版本"]) {
      expect(within(panel).queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("opens a distinct password-confirmed manual override from a fresh clear state", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderPage();
    const panel = await openEvent();
    const override = await within(panel).findByRole("button", { name: "人工覆寫此版本" });
    override.focus(); fireEvent.click(override);
    const sheet = await screen.findByRole("dialog", { name: "人工覆寫股息事件" });
    expect(within(sheet).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(sheet).getByRole("form", { name: "人工覆寫股息事件" })).toHaveTextContent(event.current_revision_id);
    expect(within(sheet).getByRole("checkbox", { name: /我確認以人工覆寫方式建立新版本/ })).not.toBeChecked();
    expect(within(sheet).getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    expect(post).not.toHaveBeenCalled();
  });

  it("offers manual override with the observed rejection pin and explicit resolution", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": state(true) }); renderPage();
    const panel = await openEvent();
    fireEvent.click(await within(panel).findByRole("button", { name: "人工覆寫此版本" }));
    const sheet = await screen.findByRole("dialog", { name: "人工覆寫股息事件" });
    expect(within(sheet).getByRole("form", { name: "人工覆寫股息事件" })).toHaveTextContent(rejection.id);
    expect(within(sheet).getByRole("form", { name: "人工覆寫股息事件" })).toHaveTextContent(rejection.reason);
    expect(within(sheet).getByRole("checkbox", { name: /明確處理所示拒絕/ })).not.toBeChecked();
  });

  it("opens revision only from a fresh matching rejection read and freezes its observed pin", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": state(true) });
    const patch = vi.spyOn(api, "PATCH");
    renderPage();
    const panel = await openEvent();
    const revise = await within(panel).findByRole("button", { name: "修訂人工股息事件" });
    fireEvent.click(revise);
    const sheet = await screen.findByRole("dialog", { name: "修訂人工股息事件" });
    expect(within(sheet).getByRole("form", { name: "重新驗證" })).toBeVisible();
    expect(within(sheet).getByRole("form", { name: "修訂人工股息事件" })).toHaveTextContent(rejection.id);
    expect(within(sheet).getByLabelText("事件每股金額")).toHaveValue(event.dps);
    expect(within(sheet).getByLabelText("事件引用網址（選填）")).toHaveValue("");
    expect(patch).not.toHaveBeenCalled();
  });

  it("does not offer a revision until rejection state has actually loaded", async () => {
    const get = reads();
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return reply({ items: [event], next_cursor: null });
      if (path === "/api/v1/dividends/events/{event_id}/rejection") return new Promise(() => {});
      if (path === "/api/v1/securities/{id}") return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      return reply(empty);
    });
    renderPage();
    const panel = await openEvent();
    expect(within(panel).getByRole("status")).toHaveTextContent("正在載入拒絕狀態");
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
  });

  it("does not use a cached clear state while reopening triggers a fresh rejection read", async () => {
    const get = reads();
    let attempts = 0; let deliver!: () => void;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return reply({ items: [event], next_cursor: null });
      if (path === "/api/v1/dividends/events/{event_id}/rejection") {
        if (++attempts === 1) return reply(state());
        return new Promise(resolve => { deliver = () => resolve(reply(state(true))); });
      }
      if (path === "/api/v1/securities/{id}") return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      return reply(empty);
    });
    renderPage();
    let panel = await openEvent();
    expect(await within(panel).findByRole("button", { name: "修訂人工股息事件" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "收合事件與拒絕狀態" }));
    panel = await openEvent();
    await waitFor(() => expect(attempts).toBe(2));
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
    await act(async () => deliver());
    expect(await within(panel).findByText(/此版本已記錄拒絕/)).toBeVisible();
    expect(within(panel).getByRole("button", { name: "修訂人工股息事件" })).toBeVisible();
  });

  it("does not authorize revision from cached rejection data when the network is paused", async () => {
    reads();
    renderPage();
    let panel = await openEvent();
    expect(await within(panel).findByRole("button", { name: "修訂人工股息事件" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "收合事件與拒絕狀態" }));
    try {
      await act(async () => onlineManager.setOnline(false));
      panel = await openEvent();
      expect(await within(panel).findByText(/連線已暫停/)).toBeVisible();
      expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
      expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
      expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
      expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
      expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
    } finally {
      await act(async () => onlineManager.setOnline(true));
    }
  });

  it("does not assign a separately loaded rejection state to a different event version", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": { ...state(true), revision_id: "55555555-5555-4555-8555-555555555555", revision_no: 3 } });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("版本不一致");
    expect(within(panel).queryByText(/此版本已記錄拒絕|此版本未記錄拒絕/)).not.toBeInTheDocument();
    expect(within(panel).queryByText("Synthetic rejection reason")).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
  });

  it("fails closed for a rejection state with inconsistent physical identity", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": { ...state(true), rejection: { ...rejection, revision_id: "55555555-5555-4555-8555-555555555555" } } });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("狀態無法核對");
    expect(within(panel).queryByText("Synthetic rejection reason")).not.toBeInTheDocument();
  });

  it.each([
    { ...state(true), rejection: null },
    { ...state(false), rejection },
    { ...state(true), rejection: { ...rejection, event_id: "55555555-5555-4555-8555-555555555555" } },
  ])("does not infer a status from inconsistent rejection content", async broken => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": broken });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("狀態無法核對");
    expect(within(panel).queryByText(/此版本已記錄拒絕|此版本未記錄拒絕/)).not.toBeInTheDocument();
  });

  it("refuses to open an exhausted event version even with a matching rejection read", async () => {
    const exhausted = { ...event, version: 2_147_483_647 };
    reads({ "/api/v1/dividends/events": { items: [exhausted], next_cursor: null },
      "/api/v1/dividends/events/{event_id}/rejection": { ...state(), revision_no: exhausted.version } });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("版本已達上限");
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
  });

  it("refuses a rejection response with an altered scope even when physical pins match", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": { ...state(), eligibility_scope: "GRANTED" } });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("狀態無法核對");
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
  });

  it.each([
    { ...rejection, reason: "" },
    { ...rejection, reference: null },
    { ...rejection, reference: { citation: "", url: null, url_withheld: false } },
    { ...rejection, reference: { citation: "Synthetic citation", url: "https://example.test/?private=synthetic", url_withheld: false } },
    { ...rejection, reference: { citation: "Synthetic citation", url: "https://example.test/notice\n", url_withheld: false } },
  ])("does not offer rejection resolution when its cited decision is incomplete or private", async broken => {
    reads({ "/api/v1/dividends/events/{event_id}/rejection": { ...state(true), rejection: broken } });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("無法核對");
    expect(within(panel).queryByText(/此版本已記錄拒絕|此版本未記錄拒絕/)).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
  });

  it("does not turn a failed rejection read into an absence and allows retry", async () => {
    const get = reads();
    let attempts = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return reply({ items: [event], next_cursor: null });
      if (path === "/api/v1/dividends/events/{event_id}/rejection") return ++attempts === 1
        ? { error: { code: "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE", message: "synthetic private detail" }, response: new Response(null, { status: 503 }) }
        : reply(state());
      if (path === "/api/v1/securities/{id}") return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      return reply(empty);
    });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("拒絕歷史無法安全核對");
    expect(within(panel).queryByText(/此版本未記錄拒絕/)).not.toBeInTheDocument();
    expect(screen.queryByText(/synthetic private detail/)).not.toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "重試載入拒絕狀態" }));
    expect(await within(panel).findByText(/此版本未記錄拒絕/)).toBeVisible();
  });

  it("pages event and rejection histories separately without inventing resolution", async () => {
    const get = reads({
      "/api/v1/dividends/events/{event_id}/revisions": { items: [revision], next_cursor: "revision-next" },
      "/api/v1/dividends/events/{event_id}/rejections": { items: [{ decision: rejection, resolution: null }], next_cursor: "rejection-next" },
    });
    renderPage();
    await openEvent();
    fireEvent.click(screen.getByRole("button", { name: "查看事件修訂歷史" }));
    const revisions = await screen.findByRole("region", { name: "事件修訂歷史" });
    expect(await within(revisions).findByText(/歷史版本 2/)).toBeVisible();
    fireEvent.click(within(revisions).getByRole("button", { name: "下一頁" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/revisions", expect.objectContaining({ params: { path: { event_id: event.id }, query: { cursor: "revision-next", limit: 25 } } })));
    fireEvent.click(screen.getByRole("button", { name: "查看拒絕歷史" }));
    const history = await screen.findByRole("region", { name: "拒絕歷史" });
    expect(await within(history).findByText(/未記錄後繼解決版本/)).toBeVisible();
    fireEvent.click(within(history).getByRole("button", { name: "下一頁" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/dividends/events/{event_id}/rejections", expect.objectContaining({ params: { path: { event_id: event.id }, query: { cursor: "rejection-next", limit: 25 } } })));
  });

  it("withholds a private event citation URL even if a malformed response includes it", async () => {
    reads({ "/api/v1/dividends/events": { items: [{ ...event, reference: { citation: "Synthetic private citation", url: "https://example.test/notice?token=synthetic-secret#page", url_withheld: false } }], next_cursor: null } });
    renderPage();
    await openEvent();
    const article = screen.getByRole("article", { name: `股息事件 ${event.id}` });
    expect(within(article).getByText("Synthetic private citation", { exact: false })).toBeVisible();
    expect(within(article).getByText(/引用包含查詢或片段，連結未提供/)).toBeVisible();
    expect(within(article).queryByRole("link", { name: /開啟事件引用/ })).not.toBeInTheDocument();
    expect(article).not.toHaveTextContent("synthetic-secret");
  });

  it("reports the recorded successor in rejection history without treating it as document proof", async () => {
    reads({ "/api/v1/dividends/events/{event_id}/rejections": { items: [{ decision: rejection, resolution: { id: "66666666-6666-4666-8666-666666666666", successor_revision_id: "77777777-7777-4777-8777-777777777777", successor_revision_no: 3 } }], next_cursor: null } });
    renderPage();
    await openEvent();
    fireEvent.click(screen.getByRole("button", { name: "查看拒絕歷史" }));
    const history = await screen.findByRole("region", { name: "拒絕歷史" });
    expect(await within(history).findByText(/已記錄後繼解決版本 3/)).toBeVisible();
    expect(within(history).getByText(/不證明文件、持倉或實收/)).toBeVisible();
  });

  it("keeps a failed refresh visibly stale rather than treating it as a current status", async () => {
    const get = reads();
    let attempts = 0;
    get.mockImplementation(async path => {
      if (path === "/api/v1/dividends/events") return reply({ items: [event], next_cursor: null });
      if (path === "/api/v1/dividends/events/{event_id}/rejection") {
        if (++attempts === 1) return reply(state());
        return { error: { code: "DIVIDEND_REJECTION_HISTORY_UNAVAILABLE" }, response: new Response(null, { status: 503 }) };
      }
      if (path === "/api/v1/securities/{id}") return reply({ id: event.security_id, display_ticker: "SYN", exchange_code: "NASDAQ" });
      return reply(empty);
    });
    renderPage();
    const panel = await openEvent();
    expect(await within(panel).findByText(/此版本未記錄拒絕/)).toBeVisible();
    fireEvent.click(within(panel).getByRole("button", { name: "重新載入拒絕狀態" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("以下保留上次載入內容，尚未確認最新狀態");
    expect(within(panel).getByText(/只表示該次讀取/)).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "修訂人工股息事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "拒絕此事件版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工接受此版本" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "撤回此事件" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "人工覆寫此版本" })).not.toBeInTheDocument();
  });

  it("sends explicit date/status filters and server cursor without changing the browser URL", async () => {
    const get = reads({ "/api/v1/dividends/events": { items: [event], next_cursor: "next-event" } });
    renderPage();
    await screen.findByText("USD 92233720368.54775807");
    fireEvent.change(screen.getByLabelText("事件除淨起日"), { target: { value: "2026-01-02" } });
    fireEvent.change(screen.getByLabelText("事件狀態"), { target: { value: "CONFLICT" } });
    fireEvent.change(screen.getByLabelText("搜尋股息事件"), { target: { value: "synthetic" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/dividends/events", expect.objectContaining({ params: { query: expect.objectContaining({ from_date: "2026-01-02", workflow_status: "CONFLICT", q: "synthetic" }) } })));
    fireEvent.click(await screen.findByRole("button", { name: "下一頁" }));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/api/v1/dividends/events", expect.objectContaining({ params: { query: expect.objectContaining({ cursor: "next-event", limit: 25 }) } })));
    expect(window.location.search).toBe("");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});
