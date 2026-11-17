import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { EstimateWriteForm, type EstimateTarget } from "./EstimateWriteForm";

type Schemas = components["schemas"];
const event: Schemas["DividendEventRecord"] = {
  id: "11111111-1111-4111-8111-111111111111", current_revision_id: "22222222-2222-4222-8222-222222222222", version: 3,
  security_id: "33333333-3333-4333-8333-333333333333", event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null,
  pay_date: null, dps: "0.00000001", currency: "USD", adjustment_basis: "AS_DECLARED", workflow_status: "PROVISIONAL", manual_locked: false,
  manual_override: false, decision_kind: "MANUAL", candidate_revision_id: null, independent_origin_groups: null, assessment: null, evidence: [],
  reference: { citation: "Synthetic event reference", url: null, url_withheld: false }, reason: "Synthetic event decision for estimate review",
  created_at: "2026-11-01T00:00:00Z", scope: "OWNER_EVENT_DECISION_ONLY", current_use_scope: "REVALIDATION_REQUIRED",
  document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const account: Schemas["Account"] = {
  id: "44444444-4444-4444-8444-444444444444", portfolio_id: "55555555-5555-4555-8555-555555555555", broker_id: "66666666-6666-4666-8666-666666666666",
  name: "Synthetic account", default_currency: "USD", status: "ACTIVE", version: 1, opening_cash: null, opening_cash_date: null, cash_completeness: "UNKNOWN",
};
const rejection: Schemas["RejectionState"] = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, state: "NOT_REJECTED", rejection: null,
  scope: "OWNER_REJECTION_STATE_ONLY", current_use_scope: "REVALIDATION_REQUIRED", document_verification: "NOT_ESTABLISHED", eligibility_scope: "NOT_ESTABLISHED",
};
const common = { event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id,
  portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: "9007199254740993", broker_version: "9223372036854775807",
  effective_on: "2026-11-03", head_no: "9007199254740992", head_id: "77777777-7777-4777-8777-777777777777",
  selected: null, selected_scope: "NO_SELECTED_SETTING" as const, scope: "OWNER_ESTIMATE_ONLY" as const, current_use_scope: "REVALIDATION_REQUIRED" as const };
const tax: Schemas["TaxSettingSelection"] = { ...common, tax_status: "MISSING" };
const fee: Schemas["FeeSettingSelection"] = { ...common, fee_status: "MISSING", revision_rejection: "NOT_REJECTED" };
const reason = "Synthetic explicit estimate decision.";
const savedBase = { id: "88888888-8888-4888-8888-888888888888", event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
  account_id: account.id, portfolio_id: account.portfolio_id, broker_id: account.broker_id, account_version: common.account_version, broker_version: common.broker_version,
  setting_no: "9007199254740993", previous_id: common.head_id, effective_from: "2026-11-01", reason, created_at: "2026-11-04T00:00:00Z",
  scope: "OWNER_ESTIMATE_ONLY" as const, current_use_scope: "REVALIDATION_REQUIRED" as const };
const savedTax: Schemas["TaxSettingRecord"] = { ...savedBase, tax_status: "ESTIMATED", rate: "0.000000" };
const savedFee: Schemas["FeeSettingRecord"] = { ...savedBase, fee_status: "UNKNOWN", fee: null, fee_currency: "USD" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const target = (kind: "tax" | "fee"): EstimateTarget => kind === "tax"
  ? { kind, event, account, selection: tax, rejection } : { kind, event, account, selection: fee, rejection };
function renderForm(value = target("tax")) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onSaved = vi.fn();
  const view = render(<QueryClientProvider client={cache}><EstimateWriteForm target={value} onSaved={onSaved} onCancel={vi.fn()} /></QueryClientProvider>);
  return { cache, onSaved, view };
}
function csrf() { vi.spyOn(api, "GET").mockResolvedValue(reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" })); }
function fill(kind: "tax" | "fee", status: "ESTIMATED" | "UNKNOWN") {
  fireEvent.change(screen.getByLabelText("估算生效日"), { target: { value: "2026-11-01" } });
  fireEvent.change(screen.getByLabelText(kind === "tax" ? "稅率估算狀態" : "費用估算狀態"), { target: { value: status } });
  if (status === "ESTIMATED") fireEvent.change(screen.getByLabelText(kind === "tax" ? "稅率估算小數" : "費用估算金額"), { target: { value: "0" } });
  if (kind === "fee") fireEvent.change(screen.getByLabelText("費用估算幣別"), { target: { value: "USD" } });
  fireEvent.change(screen.getByLabelText("估算依據"), { target: { value: reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是擁有人估算/ }));
}
function submit() { fireEvent.click(screen.getByRole("button", { name: "記錄估算" })); }
function requests(spy: { mock: { calls: unknown } }) {
  return spy.mock.calls as [string, { body: Schemas["TaxSettingWrite"] | Schemas["FeeSettingWrite"]; params: { path: { event_id: string; account_id: string }; header: { "Idempotency-Key": string; "If-Match": string; "X-CSRF-Token": string } } }][];
}

describe("explicit account dividend estimate decisions", () => {
  it("requires an explicit day, status, value, reason and no-grant acknowledgment", async () => {
    csrf(); const post = vi.spyOn(api, "POST"); renderForm(); submit();
    expect(await screen.findByText(/請填寫有效日期/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fill("tax", "ESTIMATED"); fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是擁有人估算/ })); submit();
    expect(await screen.findByText(/請確認這不是已確認扣款/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("posts a zero tax estimate with frozen exact pins, never storing private form data", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(savedTax)); const { onSaved, cache } = renderForm();
    expect(screen.getByLabelText("估算生效日")).toHaveValue(""); fill("tax", "ESTIMATED"); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(savedTax));
    const [path, request] = requests(post)[0]!;
    expect(path).toBe("/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings");
    expect(request.params.path).toEqual({ event_id: event.id, account_id: account.id });
    expect(request.params.header).toMatchObject({ "If-Match": '"3"', "X-CSRF-Token": "synthetic-csrf" });
    expect(request.body).toEqual({ revision_id: event.current_revision_id, portfolio_id: account.portfolio_id, broker_id: account.broker_id,
      account_version: common.account_version, broker_version: common.broker_version, expected_head_no: common.head_no, expected_head_id: common.head_id,
      effective_from: "2026-11-01", rate: "0", reason });
    expect(cache.getMutationCache().getAll()).toHaveLength(0); expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
  it("records unknown tax as null, never as zero", async () => {
    csrf(); const unknown = { ...savedTax, tax_status: "UNKNOWN", rate: null } as Schemas["TaxSettingRecord"];
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(unknown)); const { onSaved } = renderForm(); fill("tax", "UNKNOWN"); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(unknown)); expect(requests(post)[0]![1].body).toHaveProperty("rate", null);
  });
  it("records an unknown fee with an independently selected known currency", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(savedFee)); const { onSaved } = renderForm(target("fee")); fill("fee", "UNKNOWN"); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(savedFee));
    expect(requests(post)[0]![0]).toBe("/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings");
    expect(requests(post)[0]![1].body).toMatchObject({ fee_status: "UNKNOWN", fee: null, fee_currency: "USD" });
  });
  it("requires a known currency for an estimated fee but permits an explicitly unknown currency for an unknown fee", async () => {
    csrf(); const unknown = { ...savedFee, fee_currency: null }; const post = vi.spyOn(api, "POST").mockResolvedValue(reply(unknown));
    const { onSaved } = renderForm(target("fee")); fill("fee", "ESTIMATED");
    fireEvent.change(screen.getByLabelText("費用估算幣別"), { target: { value: "UNKNOWN" } }); submit();
    expect(await screen.findByText(/估算金額需要已知幣別/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("費用估算狀態"), { target: { value: "UNKNOWN" } }); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(unknown));
    expect(requests(post)[0]![1].body).toMatchObject({ fee_status: "UNKNOWN", fee: null, fee_currency: null });
  });
  it("keeps an explicitly estimated zero fee instead of treating it as unknown", async () => {
    csrf(); const zero = { ...savedFee, fee_status: "ESTIMATED", fee: "0.000000" } as Schemas["FeeSettingRecord"];
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(zero)); const { onSaved } = renderForm(target("fee"));
    fill("fee", "ESTIMATED"); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(zero));
    expect(requests(post)[0]![1].body).toMatchObject({ fee_status: "ESTIMATED", fee: "0", fee_currency: "USD" });
  });
  it("freezes event and account pins through a lost response and parent refresh", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("synthetic lost response")).mockResolvedValueOnce(reply(savedTax));
    const { cache, onSaved, view } = renderForm(); fill("tax", "ESTIMATED"); submit(); await screen.findByText(/網絡連線中斷/);
    const newer = { ...target("tax"), event: { ...event, version: 4, current_revision_id: "99999999-9999-4999-8999-999999999999" } } as EstimateTarget;
    view.rerender(<QueryClientProvider client={cache}><EstimateWriteForm target={newer} onSaved={onSaved} onCancel={vi.fn()} /></QueryClientProvider>);
    submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(savedTax)); expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it("reports a newly rejected event without discarding the entered estimate", async () => {
    csrf(); vi.spyOn(api, "POST").mockResolvedValue({ error: { code: "DIVIDEND_REVISION_REJECTED", message: "private" }, response: new Response(null, { status: 409 }) });
    renderForm(); fill("tax", "ESTIMATED"); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("此事件版本已記錄拒絕");
    expect(screen.getByLabelText("稅率估算小數")).toHaveValue("0"); expect(screen.getByLabelText("估算依據")).toHaveValue(reason);
  });
  it.each(["9223372036854.775808", "0.0000001", "1e2", "-1", "0".repeat(51)])("does not send an invalid or unrepresentable fee %s", async amount => {
    csrf(); const post = vi.spyOn(api, "POST"); renderForm(target("fee")); fill("fee", "ESTIMATED");
    fireEvent.change(screen.getByLabelText("費用估算金額"), { target: { value: amount } }); submit();
    expect(await screen.findByRole("alert")).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("preserves the maximum scaled INT64 fee exactly", async () => {
    csrf(); const maximum = { ...savedFee, fee_status: "ESTIMATED", fee: "9223372036854.775807" } as Schemas["FeeSettingRecord"];
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(maximum)); const { onSaved } = renderForm(target("fee")); fill("fee", "ESTIMATED");
    fireEvent.change(screen.getByLabelText("費用估算金額"), { target: { value: "9223372036854.775807" } }); submit();
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(maximum)); expect(requests(post)[0]![1].body).toHaveProperty("fee", "9223372036854.775807");
  });
  it.each(["1.000001", "0.0000001", "1e-6", "-0.1"])("does not round or send invalid tax rate %s", async value => {
    csrf(); const post = vi.spyOn(api, "POST"); renderForm(); fill("tax", "ESTIMATED"); fireEvent.change(screen.getByLabelText("稅率估算小數"), { target: { value } }); submit();
    expect(await screen.findByRole("alert")).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("preserves a retry key for equivalent decimal formatting and rotates it for changed intent", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("synthetic lost response")); renderForm(); fill("tax", "ESTIMATED"); submit();
    await screen.findByText(/網絡連線中斷/); fireEvent.change(screen.getByLabelText("稅率估算小數"), { target: { value: "0.000000" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(requests(post)[1]![1].params.header["Idempotency-Key"]).toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
    fireEvent.change(screen.getByLabelText("稅率估算小數"), { target: { value: "0.1" } }); submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(requests(post)[2]![1].params.header["Idempotency-Key"]).not.toBe(requests(post)[0]![1].params.header["Idempotency-Key"]);
  });
  it("rejects an inconsistent response but preserves the same key for retry", async () => {
    csrf(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply({ ...savedTax, account_version: "1" })).mockResolvedValueOnce(reply(savedTax));
    const { onSaved } = renderForm(); fill("tax", "ESTIMATED"); submit(); expect(await screen.findByText(/伺服器回覆與本次估算不符/)).toBeVisible();
    expect(onSaved).not.toHaveBeenCalled(); submit(); await waitFor(() => expect(onSaved).toHaveBeenCalledWith(savedTax));
    expect(requests(post)[1]![1]).toEqual(requests(post)[0]![1]);
  });
  it.each([
    { ...savedTax, event_id: account.id }, { ...savedTax, revision_id: account.id }, { ...savedTax, revision_no: 4 },
    { ...savedTax, account_id: event.id }, { ...savedTax, portfolio_id: event.security_id }, { ...savedTax, broker_id: event.security_id },
    { ...savedTax, setting_no: "9007199254740994" }, { ...savedTax, previous_id: null }, { ...savedTax, effective_from: "2026-11-02" },
    { ...savedTax, rate: "0.000001" }, { ...savedTax, tax_status: "UNKNOWN" }, { ...savedTax, reason: "A different synthetic decision." },
    { ...savedTax, scope: "GRANTED" }, { ...savedTax, current_use_scope: "GRANTED" },
  ])("rejects mismatched returned scope, terms, exact ordinal or no-grant labels", async broken => {
    csrf(); vi.spyOn(api, "POST").mockResolvedValue(reply(broken as Schemas["TaxSettingRecord"]));
    const { onSaved } = renderForm(); fill("tax", "ESTIMATED"); submit();
    expect(await screen.findByText(/伺服器回覆與本次估算不符/)).toBeVisible(); expect(onSaved).not.toHaveBeenCalled();
  });
  it.each([
    { ...target("tax"), rejection: { ...rejection, state: "REJECTED" } },
    { ...target("tax"), selection: { ...tax, account_version: "0" } },
    { ...target("fee"), selection: { ...fee, revision_rejection: "REJECTED" } },
    { ...target("tax"), selection: { ...tax, head_no: "9223372036854775807" } },
  ])("fails closed when the frozen scope or independent rejection is unsafe", broken => {
    csrf(); const post = vi.spyOn(api, "POST"); renderForm(broken as EstimateTarget);
    expect(screen.getByRole("alert")).toHaveTextContent(/固定估算範圍無法安全核對/); expect(post).not.toHaveBeenCalled();
  });
});
