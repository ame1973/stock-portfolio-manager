import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ProviderCredentialForm, ProviderPolicyForm } from "./ProviderForms";
import { ProviderSettingsDetailPage, ProviderSettingsPage } from "./ProviderPages";
import { providerNames, type ProviderId } from "../sources/data";
import type { ProviderProfile } from "./provider-data";

type Schemas = components["schemas"];
const reason = "Explicit synthetic policy change after reviewing the source.";
const secret = "synthetic-write-only-credential";
const profile: ProviderProfile = { id: "11111111-1111-4111-8111-111111111111", version: 1, policy: { provider_id: "eodhd", enabled: false, terms_acknowledged: false, terms_reference: "", terms_reason: null, requests_per_minute: null, requests_per_day: null, raw_payload_storage: false }, credential_reference_present: false, reason, created_at: "2026-11-07T00:00:00Z", runtime_selection_installed: true, scope: "OWNED_PROVIDER_POLICY" };
const reply = <T,>(data: T) => ({ data, response: new Response() });
const failure = (code: string) => ({ error: { code, message: secret, details: { credential: secret }, request_id: "synthetic-request" }, response: new Response(null, { status: 403 }) });
function reads(overrides: Record<string, object> = {}) {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (overrides[path] instanceof Error) throw overrides[path];
    if (Object.hasOwn(overrides, path)) return reply(overrides[path]!);
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-profile-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/provider-profiles") return reply({ items: [profile], next_cursor: null });
    if (path === "/api/v1/provider-profiles/{provider_id}") return reply(profile);
    if (path === "/api/v1/provider-profiles/{provider_id}/revisions") return reply({ items: [profile], next_cursor: null });
    throw new Error(`Unexpected policy read: ${path}`);
  });
}
function renderUI(ui: ReactElement, path = "/") {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function detail() { return renderUI(<Routes><Route path="/settings/providers/:id" element={<ProviderSettingsDetailPage />} /></Routes>, "/settings/providers/eodhd"); }
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Schemas["ProviderProfileUpdate"] & Schemas["ProviderCredentialImport"]; params: { header: Record<string, string>; path?: { provider_id: ProviderId } } };
}
function change(label: string | RegExp, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
async function openImport() { fireEvent.click(await screen.findByRole("button", { name: "匯入或輪替私密金鑰" })); change("新私密金鑰", secret); change("金鑰匯入依據", reason); }
function credentialForm(record = profile) { return renderUI(<ProviderCredentialForm record={record} onSaved={vi.fn()} onCancel={vi.fn()} />); }
function rights() { change("資料權利參考", "Synthetic reviewed access reference"); change("資料權利依據", "Explicit synthetic source permission review."); fireEvent.click(screen.getByLabelText("我已核對此來源的資料權利及使用條件。")); }

// The paste check must precede native password-input newline removal and maximum-length handling.
describe("exact write-only credential forms", () => {
  it.each(["key\n", "key\r\n", " key", "key ", "key\t", "非ASCII", "x".repeat(257)])("rejects an unsafe paste without submitting the previous key: %j", async value => {
    reads(); const post = vi.spyOn(api, "POST"); credentialForm();
    change("新私密金鑰", secret); change("金鑰匯入依據", reason);
    const allowed = fireEvent.paste(screen.getByLabelText("新私密金鑰"), { clipboardData: { getData: () => value } });
    expect(allowed).toBe(false); expect(screen.getByLabelText("新私密金鑰")).toHaveValue("");
    fireEvent.submit(screen.getByRole("form", { name: "匯入私密金鑰" }));
    await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
  });

  it.each(["x", "x".repeat(256)])("sends the exact supported boundary without secret metadata or caching: length %i", async value => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...profile, version: 2, credential_reference_present: true }));
    const cache = credentialForm(); change("新私密金鑰", value); change("金鑰匯入依據", reason);
    fireEvent.click(screen.getByRole("button", { name: "匯入並儲存停用版本" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body).toEqual({ credential: value, reason });
    expect(options(post).params).toEqual({ path: { provider_id: "eodhd" }, header: { "X-CSRF-Token": "synthetic-profile-csrf", "Idempotency-Key": expect.any(String), "If-Match": '"1"' } });
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(cache.getQueryCache().getAll()).toHaveLength(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(screen.getByLabelText("新私密金鑰")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("新私密金鑰")).toHaveAttribute("autocomplete", "off");
  });

  it.each(["", " key", "key ", "x\ty", "非ASCII", "x".repeat(257)])("rejects invalid direct input before any request: %j", async value => {
    reads(); const post = vi.spyOn(api, "POST"); credentialForm(); change("新私密金鑰", value); change("金鑰匯入依據", reason);
    fireEvent.submit(screen.getByRole("form")); await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
  });

  it.each(["short", "          ", "A synthetic reason with\0a forbidden separator"])("rejects an invalid import reason without private I/O: %j", async value => {
    reads(); const post = vi.spyOn(api, "POST"); credentialForm(); change("新私密金鑰", secret); change("金鑰匯入依據", value);
    fireEvent.submit(screen.getByRole("form")); await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled(); expect(screen.getByLabelText("新私密金鑰")).toHaveValue(secret);
  });

  it.each(["eodhd", "alphavantage", "massive"] as const)("imports only through the explicit %s credential path", async provider => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(profile)); credentialForm({ ...profile, policy: { ...profile.policy, provider_id: provider } });
    change("新私密金鑰", secret); change("金鑰匯入依據", reason); fireEvent.submit(screen.getByRole("form"));
    await waitFor(() => expect(post).toHaveBeenCalledOnce()); expect(options(post).params.path).toEqual({ provider_id: provider });
  });

  it.each(["yahoo", "yfinance", "hkex_evidence", "sec_evidence", "manual"] as const)("does not offer a credential form for %s", provider => {
    const post = vi.spyOn(api, "POST"); credentialForm({ ...profile, policy: { ...profile.policy, provider_id: provider } });
    expect(screen.getByRole("alert")).toHaveTextContent("不接受 API 金鑰"); expect(screen.queryByLabelText("新私密金鑰")).not.toBeInTheDocument(); expect(post).not.toHaveBeenCalled();
  });

  it("retains uncertain input and the same intent across reauthentication, without echoing server details", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(failure("REAUTH_REQUIRED")).mockResolvedValueOnce(reply({ ok: true, message: "Confirmed" })).mockResolvedValueOnce(reply({ ...profile, version: 2, credential_reference_present: true }));
    const cache = detail(); await openImport(); fireEvent.click(screen.getByRole("button", { name: "匯入並儲存停用版本" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("再次輸入密碼"); expect(screen.getByLabelText("新私密金鑰")).toHaveValue(secret);
    expect(screen.queryByText(secret)).not.toBeInTheDocument(); change("目前密碼", "synthetic-confirmation-password"); fireEvent.click(screen.getByRole("button", { name: "確認目前密碼" }));
    await screen.findByText(/已重新驗證/); expect(screen.getByLabelText("目前密碼")).toHaveValue(""); expect(screen.getByLabelText("新私密金鑰")).toHaveValue(secret);
    fireEvent.click(screen.getByRole("button", { name: "匯入並儲存停用版本" })); await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(options(post, 2)).toEqual(options(post)); expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain(secret);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state))).not.toContain("synthetic-confirmation-password");
  });

  it("keeps a storage failure private, retries unchanged, and rotates intent only after an actual field change", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(failure("PROVIDER_CREDENTIAL_STORAGE_UNAVAILABLE")); credentialForm(); change("新私密金鑰", secret); change("金鑰匯入依據", reason);
    fireEvent.submit(screen.getByRole("form")); expect(await screen.findByRole("alert")).toHaveTextContent("儲存區不可用");
    expect(screen.getByLabelText("新私密金鑰")).toHaveValue(secret); expect(screen.queryByText(secret)).not.toBeInTheDocument();
    fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(post).toHaveBeenCalledTimes(2)); expect(options(post, 1)).toEqual(options(post));
    change("金鑰匯入依據", `${reason} Another explicit intent.`); fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post).params.header["Idempotency-Key"]);
  });
});

describe("owned provider policy forms", () => {
  it("does not infer a provider, enabled access, rights or default quota", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderUI(<ProviderPolicyForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("政策來源")).toHaveValue(""); expect(screen.queryByLabelText(/啟用此個人來源政策/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("資料權利參考")).toHaveValue(""); expect(screen.getByLabelText(/我已核對/)).not.toBeChecked();
    fireEvent.submit(screen.getByRole("form")); await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
  });

  it.each(["政策變更依據", "資料權利參考", "資料權利依據"])("rejects NUL in %s before a policy request", async label => {
    reads(); const post = vi.spyOn(api, "POST"); renderUI(<ProviderPolicyForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    change("政策來源", "eodhd"); change("政策變更依據", reason); change(label, "Explicit synthetic text with\0a forbidden separator."); fireEvent.submit(screen.getByRole("form"));
    await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
  });

  it.each(Object.keys(providerNames) as ProviderId[])("creates only an explicit disabled %s policy with no credential fields", async provider => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(profile)); renderUI(<ProviderPolicyForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    change("政策來源", provider); change("政策變更依據", reason); fireEvent.click(screen.getByRole("button", { name: "儲存停用政策" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce()); expect(options(post).body).toEqual({ policy: { ...profile.policy, provider_id: provider }, reason });
  });

  it.each(["0", "01", "1e2", "1.5", "-1", "2147483648"])("rejects an invalid integer software budget: %s", async value => {
    reads(); const post = vi.spyOn(api, "POST"); renderUI(<ProviderPolicyForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    change("政策來源", "yahoo"); change("政策變更依據", reason); change("每分鐘軟體要求上限", value); fireEvent.submit(screen.getByRole("form"));
    await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
  });

  it.each(["1", "2147483647"])("accepts exact bounded software limits without claiming billing quota: %s", async value => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(profile)); renderUI(<ProviderPolicyForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    change("政策來源", "yahoo"); change("政策變更依據", reason); change("每分鐘軟體要求上限", value); change("滾動 24 小時軟體要求上限", value); fireEvent.submit(screen.getByRole("form"));
    await waitFor(() => expect(post).toHaveBeenCalledOnce()); expect(options(post).body.policy).toMatchObject({ enabled: false, requests_per_minute: Number(value), requests_per_day: Number(value) });
  });

  it("clears provider-specific rights and quotas on an explicit provider change", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(profile)); renderUI(<ProviderPolicyForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    change("政策來源", "eodhd"); change("每分鐘軟體要求上限", "3"); change("滾動 24 小時軟體要求上限", "9"); rights(); change("政策來源", "manual");
    expect(screen.getByLabelText("資料權利參考")).toHaveValue(""); expect(screen.getByLabelText(/我已核對/)).not.toBeChecked(); expect(screen.queryByLabelText("每分鐘軟體要求上限")).not.toBeInTheDocument();
    change("政策變更依據", reason); fireEvent.submit(screen.getByRole("form")); await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(options(post).body.policy).toEqual({ ...profile.policy, provider_id: "manual" });
  });

  it.each(["rights", "quota", "acknowledgment", "credential"])("blocks activation without explicit %s", async missing => {
    reads(); const patch = vi.spyOn(api, "PATCH"); renderUI(<ProviderPolicyForm record={{ ...profile, credential_reference_present: missing !== "credential" }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    change("政策變更依據", reason); fireEvent.click(screen.getByLabelText(/啟用此個人來源政策/));
    if (missing !== "rights") rights(); if (missing !== "quota") { change("每分鐘軟體要求上限", "2"); change("滾動 24 小時軟體要求上限", "10"); }
    if (missing !== "acknowledgment") fireEvent.click(screen.getByLabelText(/我明白這是來源存取授權/));
    fireEvent.submit(screen.getByRole("form")); await screen.findAllByRole("alert"); expect(patch).not.toHaveBeenCalled();
  });

  it("keeps the observed provider/version and retries unchanged activation across client-only acknowledgement", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockRejectedValue(new TypeError("Synthetic connection loss")); renderUI(<ProviderPolicyForm record={{ ...profile, credential_reference_present: true }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText("政策來源")).toHaveAttribute("readonly"); rights(); change("每分鐘軟體要求上限", "2"); change("滾動 24 小時軟體要求上限", "10"); change("政策變更依據", reason);
    fireEvent.click(screen.getByLabelText(/啟用此個人來源政策/)); fireEvent.click(screen.getByLabelText(/我明白這是來源存取授權/)); fireEvent.submit(screen.getByRole("form")); await screen.findByText(/網絡連線中斷/);
    fireEvent.click(screen.getByLabelText(/我明白這是來源存取授權/)); fireEvent.click(screen.getByLabelText(/我明白這是來源存取授權/)); fireEvent.submit(screen.getByRole("form"));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(2)); expect(options(patch, 1)).toEqual(options(patch)); expect(options(patch).params.header["If-Match"]).toBe('"1"'); expect(options(patch).body.policy.enabled).toBe(true);
  });

  it("clears a reference by appending a disabled full policy, never deleting files or cancelling a job", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(profile)); const remove = vi.spyOn(api, "DELETE");
    renderUI(<ProviderPolicyForm record={{ ...profile, credential_reference_present: true, policy: { ...profile.policy, enabled: true } }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(/清除目前金鑰參考/)); expect(screen.getByLabelText(/啟用此個人來源政策/)).not.toBeChecked(); change("政策變更依據", reason); fireEvent.submit(screen.getByRole("form"));
    await waitFor(() => expect(patch).toHaveBeenCalledOnce()); expect(options(patch).body).toEqual({ policy: profile.policy, reason, clear_credential: true }); expect(remove).not.toHaveBeenCalled();
  });
});

describe("owned settings workspace and immutable history", () => {
  it("reads an explicit empty owner list without creating defaults or requesting diagnostics", async () => {
    const get = reads({ "/api/v1/provider-profiles": { items: [], next_cursor: null } }); const post = vi.spyOn(api, "POST"); renderUI(<ProviderSettingsPage />);
    await screen.findByText(/尚無個人政策/); expect(get).toHaveBeenCalledTimes(1); expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: /目前本機診斷/ })).toHaveAttribute("href", "/data-center/providers");
  });

  it("keeps the opening snapshot after a background update and preserves input on 412", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockResolvedValue(failure("PRECONDITION_FAILED")); const cache = detail(); fireEvent.click(await screen.findByRole("button", { name: "修訂、啟用或停用政策" }));
    change("政策變更依據", reason); act(() => cache.setQueryData(["sources", "profile", "eodhd"], { ...profile, version: 9, policy: { ...profile.policy, requests_per_minute: 8 } }));
    fireEvent.submit(screen.getByRole("form", { name: "修訂來源政策" })); expect(await screen.findByRole("alert")).toHaveTextContent("不會覆蓋新版");
    expect(options(patch).params.header["If-Match"]).toBe('"1"'); expect(options(patch).body.policy.requests_per_minute).toBeNull(); expect(screen.getByLabelText("政策變更依據")).toHaveValue(reason);
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("invalidates only source queries after confirmation instead of promoting a replay as current", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(profile)); const cache = renderUI(<ProviderSettingsPage />); const invalidated = vi.spyOn(cache, "invalidateQueries");
    cache.setQueryData(["ledger", "private"], { retained: true }); fireEvent.click(screen.getByRole("button", { name: "建立停用來源政策" })); change("政策來源", "eodhd"); change("政策變更依據", reason); fireEvent.submit(screen.getByRole("form", { name: "建立停用來源政策" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument()); expect(post).toHaveBeenCalledOnce(); expect(invalidated).toHaveBeenCalledExactlyOnceWith({ queryKey: ["sources"] }); expect(cache.getQueryData(["ledger", "private"])).toEqual({ retained: true });
    expect(screen.getByText(/政策回覆版本 1/)).toHaveTextContent("目前決定以重新載入為準");
  });

  it("returns actual physical history pages and sends only cursor/search metadata", async () => {
    const older = { ...profile, id: "22222222-2222-4222-8222-222222222222", version: 2 };
    const get = reads(); get.mockImplementation(async (path, input) => {
      if (path === "/api/v1/provider-profiles/{provider_id}") return reply({ ...profile, version: 3 });
      const query = (input as { params?: { query?: { cursor?: string; q?: string } } } | undefined)?.params?.query;
      if (path === "/api/v1/provider-profiles/{provider_id}/revisions") return reply(query?.cursor ? { items: [profile], next_cursor: null } : { items: [older], next_cursor: "opaque-history-page" });
      throw new Error("Unexpected history path");
    }); detail(); const history = await screen.findByRole("region", { name: "來源政策歷史列表" });
    await within(history).findByRole("article", { name: older.id }); fireEvent.click(within(history).getByRole("button", { name: "下一頁" }));
    await within(history).findByRole("article", { name: profile.id }); expect(within(history).getByText(/第 2 頁；本頁 1 筆/)).toBeVisible();
    expect(get).toHaveBeenLastCalledWith("/api/v1/provider-profiles/{provider_id}/revisions", expect.objectContaining({ params: { path: { provider_id: "eodhd" }, query: { q: "", cursor: "opaque-history-page", limit: 25 } } }));
    change("搜尋來源政策歷史", "specific reason"); await waitFor(() => expect(get).toHaveBeenLastCalledWith("/api/v1/provider-profiles/{provider_id}/revisions", expect.objectContaining({ params: { path: { provider_id: "eodhd" }, query: { q: "specific reason", cursor: undefined, limit: 25 } } })));
  });

  it("does not request or guess an unknown provider identity", () => {
    const get = reads(); renderUI(<Routes><Route path="/settings/providers/:id" element={<ProviderSettingsDetailPage />} /></Routes>, "/settings/providers/not-a-source");
    expect(screen.getByRole("alert")).toHaveTextContent("沒有猜測"); expect(get).not.toHaveBeenCalled();
  });

  it("renders an older capability receipt without treating it as installed health", async () => {
    reads({ "/api/v1/provider-profiles/{provider_id}": { ...profile, runtime_selection_installed: false, scope: "POLICY_DEFINITION_ONLY" } }); detail();
    expect(await screen.findByText(/此原始回覆未宣告安裝/)).toBeVisible(); expect(screen.queryByText(/金鑰有效。/)).not.toBeInTheDocument();
  });
});
