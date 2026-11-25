import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { DividendRevisionPreviewPanel } from "./DividendRevisionPreviewPanel";
import { account, event } from "./projection-test-data";

type Schemas = components["schemas"];
const other = { ...account, id: "f7777777-7777-4777-8777-777777777777", name: "Synthetic other account", status: "ARCHIVED" as const };
const estimate = (id: string): Schemas["AccountEstimate"] => ({ account_id: id, tax_rate: null, fee: null, fee_currency: null,
  fee_rate_event: null, fee_rate_source: null, fx_rate_hkd: null, fx_source: null });
const result: Schemas["PreviewPage"] = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, security_id: event.security_id,
  terms: { ex_date: event.ex_date, as_of: "2026-11-03", eligibility_rule: "ORDINARY_PRE_EX_DATE", dps: "1.00000000", currency: "USD", adjustment_basis: "AS_DECLARED" },
  items: [account, other].map((value, index) => ({ account_id: value.id, portfolio_id: value.portfolio_id, broker_id: value.broker_id,
    account_status: value.status, broker_status: "ACTIVE", account_version: "1", broker_version: "1", portfolio_version: "1", portfolio_data_revision: "2",
    retired_trade_scope: "NO_RETIRED_TRADES", creation_audit_scope: "NO_RECORDED_CREATION_GAP_REVIEW", eligibility_state: "RECORDED_PRE_EX_DATE",
    recorded_quantity: index === 0 ? "10.00000000" : "0.00000000", eligibility_reasons: [], pending_action_ids: [], estimate: estimate(value.id),
    previous_projection_id: null, previous_revision_id: null, previous_as_of: null, previous_currency: null, previous_expected: null, proposed_amounts: null,
    expected_delta: null, recorded_received: null, recorded_outstanding: null, active_receipts: "0", void_receipts: "0", unverified_origins: "0",
    unavailable_cash_links: "0", comparison_quality: null, state: index === 0 ? "ESTIMATES_UNAVAILABLE" : "NO_RECORDED_POSITION" })),
  snapshot_token: "a".repeat(64), next_after_account_id: null, scope: "HYPOTHETICAL_RECORDED_COMPARISON_ONLY", history_completeness: "UNVERIFIED",
  current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
const endpoint = "/api/v1/dividends/events/{event_id}/revision-preview";
function mount(readAccounts: (cursor: string | undefined) => { items: Schemas["Account"][]; next_cursor: string | null } = () => ({ items: [account, other], next_cursor: null }), onClose?: () => void) {
  const get = vi.spyOn(api, "GET").mockImplementation(async (request, options) => {
    if (request === "/api/v1/accounts") return reply(readAccounts((options as { params: { query: { cursor?: string } } }).params.query.cursor));
    if (request === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf-token-123456789" });
    throw new Error("Unexpected private read");
  });
  const post = vi.spyOn(api, "POST").mockImplementation(async request => {
    if (request === endpoint) return reply(result);
    throw new Error("Unexpected write");
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={cache}><DividendRevisionPreviewPanel event={event} onClose={onClose} /></QueryClientProvider>);
  return { get, post, cache, ...view };
}

async function chooseUnknowns() {
  fireEvent.change(screen.getByLabelText("查閱日"), { target: { value: "2026-11-03" } });
  fireEvent.change(screen.getByLabelText("持股規則"), { target: { value: "ORDINARY_PRE_EX_DATE" } });
  for (const name of [account.name, other.name]) {
    const group = screen.getByRole("group", { name: `估算 ${name}` });
    fireEvent.change(within(group).getByLabelText("稅率估算"), { target: { value: "UNKNOWN" } });
    fireEvent.change(within(group).getByLabelText("費用估算"), { target: { value: "UNKNOWN" } });
    fireEvent.change(within(group).getByLabelText("費用幣別"), { target: { value: "UNKNOWN" } });
    fireEvent.change(within(group).getByLabelText("港幣估值匯率"), { target: { value: "UNKNOWN" } });
  }
}

describe("hypothetical revision preview", () => {
  it("allows an invalid event to be closed without reading accounts", async () => {
    const get = vi.spyOn(api, "GET"); const close = vi.fn();
    render(<DividendRevisionPreviewPanel event={{ ...event, version: 0 }} onClose={close} />);
    expect(screen.getByRole("alert")).toHaveTextContent("事件版本無法安全核對");
    expect(screen.getByRole("heading", { name: "擬議股息修訂預覽" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "取消" })); expect(close).toHaveBeenCalledOnce(); expect(get).not.toHaveBeenCalled();
  });

  it("keeps a malformed frozen event closed to reads if later props become valid", () => {
    const get = vi.spyOn(api, "GET");
    const bad = { ...event, id: null as unknown as string };
    const view = render(<DividendRevisionPreviewPanel event={bad} />);
    view.rerender(<DividendRevisionPreviewPanel event={event} />);
    expect(screen.getByRole("alert")).toHaveTextContent("事件版本無法安全核對");
    expect(get).not.toHaveBeenCalled();
  });

  it("requires explicit choices for every account and sends no invented values", async () => {
    const { post, cache } = mount();
    expect(await screen.findByRole("group", { name: `估算 ${account.name}` })).toBeVisible();
    expect(screen.getByRole("group", { name: `估算 ${other.name}` })).toHaveTextContent("已封存");
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findAllByText(/請明確選擇/)).not.toHaveLength(0); expect(post).not.toHaveBeenCalled();
    await chooseUnknowns(); fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByRole("region", { name: "擬議修訂比較結果" })).toHaveTextContent("不代表付款、持倉資格或收入已確認");
    expect(screen.getByText(/稅費估算未提供/)).toBeVisible(); expect(screen.getByText(/沒有已記錄持倉/)).toBeVisible();
    const calls = post.mock.calls as [string, { body: Schemas["PreviewRequest"] }][]; const submitted = calls[0]![1].body;
    expect(submitted.proposal.accounts).toEqual([estimate(account.id), estimate(other.id)]);
    expect(submitted.after_account_id).toBeNull(); expect(submitted.snapshot_token).toBeNull();
    expect(cache.getQueryCache().getAll()).toHaveLength(0); expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("discards results when edited and never displays a substituted response", async () => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("region", { name: "擬議修訂比較結果" });
    fireEvent.change(screen.getByLabelText("擬議每股股息"), { target: { value: "2" } });
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
    post.mockImplementation(async () => reply({ ...result, payment_scope: "VERIFIED" }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
  });

  it("stops while offline and rereads accounts after reconnecting", async () => {
    const view = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` });
    try {
      act(() => onlineManager.setOnline(false));
      expect(await screen.findByText(/連線已暫停/)).toBeVisible(); expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
      const calls = view.get.mock.calls.length; act(() => onlineManager.setOnline(true));
      await waitFor(() => expect(view.get.mock.calls.length).toBeGreaterThan(calls));
    } finally { view.unmount(); act(() => onlineManager.setOnline(true)); }
  });

  it("aborts an in-flight account read when the preview closes", async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(api, "GET").mockImplementation(async (_request, options) => new Promise(() => { signal = (options as { signal: AbortSignal }).signal; }));
    const view = render(<DividendRevisionPreviewPanel event={event} />);
    await waitFor(() => expect(signal).toBeDefined()); view.unmount(); expect(signal?.aborted).toBe(true);
  });

  it("reads every account page before presenting the form", async () => {
    const read = vi.fn((cursor: string | undefined) => cursor ? { items: [other], next_cursor: null } : { items: [account], next_cursor: "next-page" });
    mount(read);
    expect(await screen.findByRole("group", { name: `估算 ${other.name}` })).toBeVisible();
    expect(screen.getByRole("group", { name: `估算 ${account.name}` })).toBeVisible();
    expect(read.mock.calls.map(call => call[0])).toEqual([undefined, "next-page"]);
  });

  it.each(["duplicate", "cursor cycle", "empty page"])('rejects an unsafe account list: %s', async problem => {
    mount(cursor => {
      if (problem === "duplicate") return cursor ? { items: [account], next_cursor: null } : { items: [account], next_cursor: "next" };
      if (problem === "empty page") return { items: [], next_cursor: "next" };
      return cursor ? { items: [other], next_cursor: "next" } : { items: [account], next_cursor: "next" };
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(screen.queryByRole("form", { name: "擬議修訂預覽" })).not.toBeInTheDocument();
  });

  it("keeps input but withholds results when reloaded accounts change", async () => {
    let changed = false; const view = mount(() => ({ items: changed ? [account] : [account, other], next_cursor: null }));
    await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("region", { name: "擬議修訂比較結果" });
    changed = true; fireEvent.click(screen.getByRole("button", { name: "重新讀取全部帳戶" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("帳戶清單已變更");
    expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
    expect(screen.getByRole("group", { name: `估算 ${other.name}` })).toBeVisible();
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
    expect(view.post).toHaveBeenCalledTimes(1);
  });

  it("replaces a stale-account error with the refreshed scope warning", async () => {
    let changed = false; const view = mount(() => ({ items: changed ? [account] : [account, other], next_cursor: null }));
    await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    view.post.mockImplementation(async () => ({ error: { code: "DIVIDEND_PREVIEW_ACCOUNT_SCOPE_CHANGED" }, response: new Response(null, { status: 409 }) }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("帳戶範圍已變更");
    changed = true; fireEvent.click(screen.getByRole("button", { name: "重新讀取全部帳戶" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("帳戶清單已變更"));
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
  });

  it("keeps input across offline rereads, but discards the old comparison", async () => {
    const view = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("region", { name: "擬議修訂比較結果" });
    try {
      act(() => onlineManager.setOnline(false));
      expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
      expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
      act(() => onlineManager.setOnline(true));
      await waitFor(() => expect(screen.getByRole("button", { name: "計算預覽" })).toBeEnabled());
      expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
      expect(within(screen.getByRole("group", { name: `估算 ${account.name}` })).getByLabelText("稅率估算")).toHaveValue("UNKNOWN");
      expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
    } finally { view.unmount(); act(() => onlineManager.setOnline(true)); }
  });

  it("sends only explicitly provided tax, foreign-fee and HKD FX values", async () => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    const group = within(screen.getByRole("group", { name: `估算 ${account.name}` }));
    fireEvent.change(group.getByLabelText("稅率估算"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("稅率小數"), { target: { value: "0.1" } });
    fireEvent.change(group.getByLabelText("費用估算"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("估算費用"), { target: { value: "0.25" } });
    fireEvent.change(group.getByLabelText("費用幣別"), { target: { value: "HKD" } });
    fireEvent.change(group.getByLabelText("費用換算率"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("費用兌事件幣別匯率"), { target: { value: "0.128205128205" } });
    fireEvent.change(group.getByLabelText("費用匯率來源"), { target: { value: "synthetic fee source" } });
    fireEvent.change(group.getByLabelText("港幣估值匯率"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("美元兌港幣匯率"), { target: { value: "7.812345678901" } });
    fireEvent.change(group.getByLabelText("港幣匯率來源"), { target: { value: "synthetic FX source" } });
    const explicit = { ...estimate(account.id), tax_rate: "0.1", fee: "0.25", fee_currency: "HKD" as const,
      fee_rate_event: "0.128205128205", fee_rate_source: "synthetic fee source", fx_rate_hkd: "7.812345678901", fx_source: "synthetic FX source" };
    const response = structuredClone(result); Object.assign(response.items[0]!, { estimate: explicit, eligibility_state: "NEEDS_REVIEW", recorded_quantity: null,
      eligibility_reasons: ["ELIGIBILITY_RULE_REQUIRES_REVIEW"], state: "ELIGIBILITY_REVIEW" });
    post.mockImplementation(async () => reply(response));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("region", { name: "擬議修訂比較結果" });
    const calls = post.mock.calls as [string, { body: Schemas["PreviewRequest"] }][];
    expect(calls[0]![1].body.proposal.accounts).toEqual([explicit, estimate(other.id)]);
  });

  it("shows exact hypothetical components and separately labelled comparison quality", async () => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    const group = within(screen.getByRole("group", { name: `估算 ${account.name}` }));
    fireEvent.change(group.getByLabelText("稅率估算"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("稅率小數"), { target: { value: "0.2" } });
    fireEvent.change(group.getByLabelText("費用估算"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("估算費用"), { target: { value: "0.5" } });
    fireEvent.change(group.getByLabelText("費用幣別"), { target: { value: "USD" } });
    const response = structuredClone(result);
    Object.assign(response.items[0]!, { estimate: { ...estimate(account.id), tax_rate: "0.2", fee: "0.5", fee_currency: "USD" },
      previous_projection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", previous_revision_id: event.current_revision_id,
      previous_as_of: "2026-11-03", previous_currency: "USD", previous_expected: "7.000000", expected_delta: "0.500000",
      proposed_amounts: { currency: "USD", gross_local: "10.000000", tax_local: "2.000000", fee_local: "0.500000",
        net_local: "7.500000", rounding_delta_local: "0.000000", estimated_hkd: null, quality: "MISSING_FX" },
      active_receipts: "1", recorded_received: "6.000000", recorded_outstanding: "1.500000", comparison_quality: "MISSING_FX", state: "RECORDED_PARTIAL" });
    post.mockImplementation(async () => reply(response));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    const preview = await screen.findByRole("region", { name: "擬議修訂比較結果" });
    expect(preview).toHaveTextContent("毛額 10.000000；稅額 2.000000；費用 0.500000；淨額 7.500000");
    expect(preview).toHaveTextContent("擬議減舊投影 0.500000"); expect(preview).toHaveTextContent("已記錄差額 1.500000");
    expect(preview).toHaveTextContent("投影品質：缺少港幣匯率"); expect(preview).toHaveTextContent("比較品質：缺少港幣匯率");
  });

  it("rejects excess precision, missing sources and invented identity rates", async () => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    const group = within(screen.getByRole("group", { name: `估算 ${account.name}` }));
    fireEvent.change(group.getByLabelText("稅率估算"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("稅率小數"), { target: { value: "0.1234567" } });
    fireEvent.change(group.getByLabelText("港幣估值匯率"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("美元兌港幣匯率"), { target: { value: "7.8" } });
    fireEvent.change(group.getByLabelText("港幣匯率來源"), { target: { value: "IDENTITY" } });
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByText("請輸入 0 至 1 的稅率小數。")).toBeVisible();
    expect(screen.getByText("請輸入明確匯率來源。")).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("never displays private exception text", async () => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    post.mockRejectedValue(new Error("PRIVATE-SYNTHETIC-EXCEPTION"));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(document.body).not.toHaveTextContent("PRIVATE-SYNTHETIC-EXCEPTION");
  });

  it("never displays private exception text from an account read", async () => {
    mount(() => { throw new Error("PRIVATE-ACCOUNT-EXCEPTION"); });
    expect(await screen.findByRole("alert")).toHaveTextContent("無法安全核對");
    expect(document.body).not.toHaveTextContent("PRIVATE-ACCOUNT-EXCEPTION");
  });

  it("hides an old comparison when the parent rereads the same event", async () => {
    const view = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("region", { name: "擬議修訂比較結果" });
    view.rerender(<QueryClientProvider client={view.cache}><DividendRevisionPreviewPanel event={event} enabled={false} /></QueryClientProvider>);
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    view.rerender(<QueryClientProvider client={view.cache}><DividendRevisionPreviewPanel event={event} eventReadVersion={1} /></QueryClientProvider>);
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03"); expect(screen.getByRole("button", { name: "計算預覽" })).toBeEnabled();
  });

  it("withholds a late preview after a manual account reread", async () => {
    const view = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    let finish: ((value: ReturnType<typeof reply<Schemas["PreviewPage"]>>) => void) | undefined;
    view.post.mockImplementationOnce(async () => new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await waitFor(() => expect(finish).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "重新讀取全部帳戶" }));
    await waitFor(() => expect(view.get).toHaveBeenCalledTimes(3));
    await act(async () => { finish!(reply(result)); });
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByRole("region", { name: "擬議修訂比較結果" })).toBeVisible();
  });

  it.each(["DIVIDEND_PREVIEW_ACCOUNT_SCOPE_CHANGED", "DIVIDEND_PREVIEW_SNAPSHOT_CHANGED"])('requires a fresh account read after %s', async code => {
    const view = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    view.post.mockImplementation(async () => ({ error: { code }, response: new Response(null, { status: 409 }) }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("擬議每股股息"), { target: { value: "1.3" } });
    expect(screen.getByRole("alert")).toBeVisible();
    view.rerender(<QueryClientProvider client={view.cache}><DividendRevisionPreviewPanel event={event} eventReadVersion={1} /></QueryClientProvider>);
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新讀取全部帳戶" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "計算預覽" })).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); expect(screen.getByLabelText("擬議每股股息")).toHaveValue("1.3");
  });

  it("requires a fresh event read, not just accounts, after a version conflict", async () => {
    const view = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    view.post.mockImplementation(async () => ({ error: { code: "PRECONDITION_FAILED" }, response: new Response(null, { status: 412 }) }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新讀取全部帳戶" }));
    await waitFor(() => expect(view.get.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(screen.getByRole("button", { name: "計算預覽" })).toBeDisabled();
    view.rerender(<QueryClientProvider client={view.cache}><DividendRevisionPreviewPanel event={event} eventReadVersion={1} /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByRole("button", { name: "計算預覽" })).toBeEnabled());
    expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
  });

  it("requires no invented identity rate when the proposed currency is HKD", async () => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    const group = within(screen.getByRole("group", { name: `估算 ${account.name}` }));
    fireEvent.change(group.getByLabelText("港幣估值匯率"), { target: { value: "PROVIDED" } });
    fireEvent.change(group.getByLabelText("美元兌港幣匯率"), { target: { value: "7.8123" } });
    fireEvent.change(group.getByLabelText("港幣匯率來源"), { target: { value: "synthetic source" } });
    fireEvent.change(screen.getByLabelText("擬議幣別"), { target: { value: "HKD" } });
    const response = structuredClone(result); response.terms.currency = "HKD"; post.mockImplementation(async () => reply(response));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByRole("region", { name: "擬議修訂比較結果" });
    const calls = post.mock.calls as [string, { body: Schemas["PreviewRequest"] }][];
    expect(calls[0]![1].body.proposal.accounts).toEqual([estimate(account.id), estimate(other.id)]);
    expect(group.getByText(/恆等匯率 1/)).toBeVisible();
  });

  it("labels an empty account list without inventing confirmed zero", async () => {
    const { post } = mount(() => ({ items: [], next_cursor: null }));
    expect(await screen.findByText(/目前沒有帳戶；這不是已確認零持倉/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("查閱日"), { target: { value: "2026-11-03" } });
    fireEvent.change(screen.getByLabelText("持股規則"), { target: { value: "ORDINARY_PRE_EX_DATE" } });
    post.mockImplementation(async () => reply({ ...result, items: [] }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByText(/此頁沒有帳戶；不代表已確認零持倉/)).toBeVisible();
    const calls = post.mock.calls as [string, { body: Schemas["PreviewRequest"] }][];
    expect(calls[0]![1].body.proposal.accounts).toEqual([]);
  });

  it.each([
    ["DIVIDEND_PREVIEW_ACCOUNT_SCOPE_CHANGED", "帳戶範圍已變更"],
    ["DIVIDEND_PREVIEW_SNAPSHOT_CHANGED", "比較資料已變更"],
    ["PRECONDITION_FAILED", "事件版本已變更"],
    ["DIVIDEND_REVISION_PREVIEW_UNAVAILABLE", "無法安全核對"],
  ])("keeps input and reports a safe error for %s", async (code, message) => {
    const { post } = mount(); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    post.mockImplementation(async () => ({ error: { code }, response: new Response(null, { status: 409 }) }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("查閱日")).toHaveValue("2026-11-03");
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
  });

  it("can close and abort a pending read-only preview", async () => {
    const close = vi.fn(); const view = mount(undefined, close); await screen.findByRole("group", { name: `估算 ${account.name}` }); await chooseUnknowns();
    let signal: AbortSignal | undefined;
    view.post.mockImplementation(async (_request, options) => new Promise(() => { signal = (options as { signal: AbortSignal }).signal; }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" }));
    await waitFor(() => expect(signal).toBeDefined()); fireEvent.click(screen.getByRole("button", { name: "中止並關閉預覽" }));
    expect(close).toHaveBeenCalledOnce(); view.unmount(); expect(signal?.aborted).toBe(true);
  });

  it("keeps the same token across pages and rejects a changed first page", async () => {
    const many = Array.from({ length: 26 }, (_, index) => ({ ...account,
      id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`, name: `Synthetic account ${index + 1}` }));
    const { post } = mount(() => ({ items: many, next_cursor: null }));
    await screen.findByRole("group", { name: "估算 Synthetic account 26" });
    fireEvent.change(screen.getByLabelText("查閱日"), { target: { value: "2026-11-03" } });
    fireEvent.change(screen.getByLabelText("持股規則"), { target: { value: "ORDINARY_PRE_EX_DATE" } });
    for (const item of many) {
      const group = within(screen.getByRole("group", { name: `估算 ${item.name}` }));
      for (const name of ["稅率估算", "費用估算", "費用幣別", "港幣估值匯率"]) fireEvent.change(group.getByLabelText(name), { target: { value: "UNKNOWN" } });
    }
    const items = many.map(value => ({ ...result.items[1]!, account_id: value.id, portfolio_id: value.portfolio_id, broker_id: value.broker_id,
      account_status: value.status, estimate: estimate(value.id) }));
    const first = { ...result, items: items.slice(0, 25), next_after_account_id: many[24]!.id };
    const second = { ...result, items: items.slice(25), next_after_account_id: null };
    post.mockImplementationOnce(async () => reply(first)).mockImplementationOnce(async () => reply(second))
      .mockImplementationOnce(async () => reply({ ...first, snapshot_token: "b".repeat(64) }));
    fireEvent.click(screen.getByRole("button", { name: "計算預覽" })); await screen.findByText(/第 1 頁/);
    fireEvent.click(screen.getByRole("button", { name: "下一頁預覽" })); await screen.findByText(/第 2 頁/);
    const calls = post.mock.calls as [string, { body: Schemas["PreviewRequest"] }][];
    expect(calls[1]![1].body.after_account_id).toBe(many[24]!.id); expect(calls[1]![1].body.snapshot_token).toBe("a".repeat(64));
    fireEvent.click(screen.getByRole("button", { name: "上一頁預覽" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("預覽已變更");
    expect(calls[2]![1].body.after_account_id).toBeNull(); expect(calls[2]![1].body.snapshot_token).toBeNull();
    expect(screen.queryByRole("region", { name: "擬議修訂比較結果" })).not.toBeInTheDocument();
  });
});
