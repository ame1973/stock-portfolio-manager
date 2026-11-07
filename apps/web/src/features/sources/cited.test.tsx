import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";
import { api, type components } from "@spm/contracts";
import { CitedForm, type CitedResult } from "./CitedForm";
import { ObservationsPage } from "./SourcePages";

type Schemas = components["schemas"];
const security: Schemas["Security"] = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic cited security", display_ticker: "0005", exchange_code: "HKG", quote_currency: "USD", security_type: "EQUITY", status: "DELISTED", isin: null, verified: false, version: 1 };
const alias: Schemas["ProviderSymbol"] = { id: "22222222-2222-4222-8222-222222222222", security_id: security.id, provider_id: "manual", provider_symbol: "0005.CITED", effective_from: "2020-01-01", effective_to: null, version: 1, verified: false };
const result: CitedResult = { provider_id: "manual", security_id: security.id, operation: "DIVIDENDS", observation_id: "33333333-3333-4333-8333-333333333333", provider_run_id: "44444444-4444-4444-8444-444444444444", citation_id: "55555555-5555-4555-8555-555555555555", scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", document_verification: "NOT_VERIFIED" };
const reference = "Synthetic local document page 3";
const reason = "Explicit synthetic quotation, not verified income.";
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-cited-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities") return reply({ items: [security], next_cursor: null });
    if (path === "/api/v1/securities/{id}") return reply(security);
    if (path === "/api/v1/securities/{id}/provider-symbols") return reply({ items: [alias], next_cursor: null });
    if (path === "/api/v1/dividends/observations" || path === "/api/v1/providers/split-observations") return reply({ items: [], next_cursor: null, legacy_records_omitted: 0, scope: "IMMUTABLE_SOURCE_V1" });
    throw new Error(`Unexpected synthetic citation read: ${path}`);
  });
}
function show(ui: ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
  return cache;
}
function options(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Record<string, unknown>; params: { header: Record<string, string> } };
}
async function fill(split = false, provider = "manual") {
  await screen.findByRole("option", { name: /0005 \/ HKG/ });
  fireEvent.change(screen.getByLabelText("引用證券", { exact: true }), { target: { value: security.id } });
  fireEvent.click(screen.getByRole("button", { name: "加入證券" }));
  fireEvent.change(screen.getByLabelText("引用來源", { exact: true }), { target: { value: provider } });
  fireEvent.change(screen.getByLabelText(split ? "引用因子生效日期" : "引用除淨日期"), { target: { value: "2026-01-08" } });
  fireEvent.change(screen.getByLabelText("引用來源代號"), { target: { value: "0005.CITED" } });
  fireEvent.change(screen.getByLabelText("引用文件或本機參考"), { target: { value: reference } });
  fireEvent.change(screen.getByLabelText("引用說明"), { target: { value: reason } });
  if (split) {
    fireEvent.change(screen.getByLabelText("引用比例分子"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("引用比例分母"), { target: { value: "3" } });
  }
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ }));
}
const submit = () => fireEvent.click(screen.getByRole("button", { name: "儲存引用，不採納財務" }));

it.each(["dividends", "splits"] as const)("has explicit empty identity/terms and no financial adoption in %s mode", async mode => {
  reads(); const post = vi.spyOn(api, "POST");
  show(<CitedForm mode={mode} onSaved={vi.fn()} onCancel={vi.fn()} />);
  expect(screen.getByLabelText("引用來源", { exact: true })).toHaveValue("");
  expect(screen.getByLabelText("引用來源代號")).toHaveValue("");
  expect(screen.getByLabelText(mode === "dividends" ? "引用除淨日期" : "引用因子生效日期")).toHaveValue("");
  if (mode === "dividends") {
    expect(screen.getByLabelText("引用每股金額（未知留空）")).toHaveValue("");
    expect(screen.getByLabelText("引用股息幣別")).toHaveValue("");
    expect(screen.getByLabelText("引用金額口徑")).toHaveValue("UNKNOWN");
  } else expect(screen.getByLabelText("引用比例分子")).toHaveValue("");
  expect(screen.queryByRole("option", { name: "EODHD" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /採納股數|確認已收/ })).not.toBeInTheDocument();
  submit(); await screen.findByText(/請明確加入一個穩定證券身分/);
  expect(post).not.toHaveBeenCalled();
});

it.each(["", "0", "92233720368.54775807"])("keeps unknown/zero/INT64 source DPS exact without using quote USD: %s", async dps => {
  reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result)); const saved = vi.fn();
  const cache = show(<CitedForm mode="dividends" onSaved={saved} onCancel={vi.fn()} />);
  await fill(); fireEvent.change(screen.getByLabelText("引用每股金額（未知留空）"), { target: { value: dps } });
  fireEvent.change(screen.getByLabelText("來源幣別標籤（選填）"), { target: { value: "EUR" } });
  fireEvent.change(screen.getByLabelText("來源期間標籤（選填）"), { target: { value: "QUARTERLY" } });
  fireEvent.change(screen.getByLabelText("來源頻率標籤（選填）"), { target: { value: "4" } });
  submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(result));
  expect(post).toHaveBeenCalledExactlyOnceWith("/api/v1/providers/cited-observations", expect.objectContaining({ body: {
    operation: "DIVIDENDS", provider_id: "manual", security_id: security.id, provider_symbol: "0005.CITED", ex_date: "2026-01-08", dps: dps || null, currency: null, event_type: "UNKNOWN", adjustment_basis: "UNKNOWN", announce_date: null, record_date: null, pay_date: null, source_currency: "EUR", source_period: "QUARTERLY", source_frequency: 4, citation: { citation: reference, reason, url: null },
  } }));
  expect(cache.getMutationCache().getAll()).toHaveLength(0);
  expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state.data))).not.toContain(reference);
  expect(localStorage.length + sessionStorage.length).toBe(0);
});

it.each([["1", "3"], ["9223372036854775807", "9223372036854775806"]])("submits rational %s:%s without Number or action adoption", async (numerator, denominator) => {
  reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...result, operation: "SPLITS" }));
  show(<CitedForm mode="splits" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await fill(true);
  fireEvent.change(screen.getByLabelText("引用比例分子"), { target: { value: numerator } });
  fireEvent.change(screen.getByLabelText("引用比例分母"), { target: { value: denominator } });
  submit(); await waitFor(() => expect(post).toHaveBeenCalledOnce());
  expect(options(post).body).toEqual({ operation: "SPLITS", provider_id: "manual", security_id: security.id, provider_symbol: "0005.CITED", effective_date: "2026-01-08", ratio_numerator: numerator, ratio_denominator: denominator, source_adjustment_type: null, citation: { citation: reference, reason, url: null } });
});

it.each(["hkex_evidence", "sec_evidence", "manual"])("quotes only the explicitly selected %s profile", async provider => {
  reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...result, provider_id: provider }));
  show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />);
  await fill(false, provider);
  if (provider !== "manual") fireEvent.change(screen.getByLabelText("引用網址（人工來源可留空）"), { target: { value: provider === "hkex_evidence" ? "https://www.hkexnews.hk/synthetic.pdf" : "https://www.sec.gov/Archives/synthetic.htm" } });
  submit(); await waitFor(() => expect(post).toHaveBeenCalledOnce());
  expect(options(post).body).toMatchObject({ provider_id: provider });
});

it.each(["0.100000001", "-1", "1e-8", "NaN", "92233720368.54775808", "0".repeat(81)])("rejects invalid DPS %s without rounding", async amount => {
  reads(); const post = vi.spyOn(api, "POST");
  show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill();
  fireEvent.change(screen.getByLabelText("引用每股金額（未知留空）"), { target: { value: amount } });
  submit(); await screen.findByRole("alert"); expect(post).not.toHaveBeenCalled();
});

it.each(["0", "1e3", "1.5", "9223372036854775808", "12345678901234567890"])("rejects invalid rational %s without changing input", async value => {
  reads(); const post = vi.spyOn(api, "POST");
  show(<CitedForm mode="splits" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill(true);
  fireEvent.change(screen.getByLabelText("引用比例分子"), { target: { value } });
  submit(); await screen.findByRole("alert"); expect(post).not.toHaveBeenCalled(); expect(screen.getByLabelText("引用比例分子")).toHaveValue(value);
});

it.each(["http://www.hkexnews.hk/synthetic.pdf", "https://www.sec.gov/synthetic.htm", "https://www.hkexnews.hk:444/synthetic.pdf", "https://www.hkexnews.hk.evil.test/synthetic.pdf", "https://u:p@www.hkexnews.hk/synthetic.pdf", "file:///private/reference"])("rejects unsafe/wrong official attribution %s", async url => {
  reads(); const post = vi.spyOn(api, "POST");
  show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill(false, "hkex_evidence");
  fireEvent.change(screen.getByLabelText("引用網址（人工來源可留空）"), { target: { value: url } });
  submit(); await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
});

it.each(["reason NUL", "reference NUL", "no acknowledgement", "missing official URL", "frequency", "source currency"])("rejects %s before sending a source assertion", async invalid => {
  reads(); const post = vi.spyOn(api, "POST");
  show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill(false, invalid === "missing official URL" ? "hkex_evidence" : "manual");
  if (invalid === "reason NUL") fireEvent.change(screen.getByLabelText("引用說明"), { target: { value: "Synthetic\0reason text" } });
  if (invalid === "reference NUL") fireEvent.change(screen.getByLabelText("引用文件或本機參考"), { target: { value: "Document\0page" } });
  if (invalid === "no acknowledgement") fireEvent.click(screen.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ }));
  if (invalid === "frequency") fireEvent.change(screen.getByLabelText("來源頻率標籤（選填）"), { target: { value: "366" } });
  if (invalid === "source currency") fireEvent.change(screen.getByLabelText("來源幣別標籤（選填）"), { target: { value: "eur" } });
  submit(); await screen.findAllByRole("alert"); expect(post).not.toHaveBeenCalled();
});

it("keeps failed input and exact intent across candidate search, acknowledgement, alias refresh and same choice", async () => {
  reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic citation transport interruption"));
  show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill(); submit(); await screen.findByText(/網絡連線中斷/);
  fireEvent.change(screen.getByLabelText("搜尋引用證券"), { target: { value: "unused candidate" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ })); fireEvent.click(screen.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ }));
  fireEvent.click(screen.getByText("查閱已保存代號（不會自動填入）", { exact: true }));
  fireEvent.click(await screen.findByRole("button", { name: "使用此引用代號 0005.CITED" }));
  fireEvent.change(screen.getByLabelText("搜尋引用代號參考"), { target: { value: "0005" } });
  submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  expect(options(post, 1)).toEqual(options(post, 0));
  fireEvent.change(screen.getByLabelText("引用說明"), { target: { value: "Explicit changed source assertion reason." } });
  submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
  expect(options(post, 2).params.header["Idempotency-Key"]).not.toBe(options(post, 0).params.header["Idempotency-Key"]);
});

it("does not gate an unchanged uncertain replay on unavailable current aliases or private diagnostics", async () => {
  const get = reads(); const post = vi.spyOn(api, "POST").mockRejectedValueOnce(new TypeError("Synthetic uncertain source write")).mockResolvedValueOnce(reply(result)); const saved = vi.fn();
  show(<CitedForm mode="dividends" onSaved={saved} onCancel={vi.fn()} />); await fill(); submit(); await screen.findByText(/網絡連線中斷/);
  get.mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-cited-csrf", expires_at: "2099-01-01T00:00:00Z" });
    throw new TypeError("Synthetic current metadata unavailable");
  });
  fireEvent.click(screen.getByText("查閱已保存代號（不會自動填入）", { exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "重新載入代號參考" }));
  await screen.findByRole("button", { name: "重試載入代號參考" });
  submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(result));
  expect(options(post, 1)).toEqual(options(post, 0));
});

it("clears a confirmed symbol only when source, event day or confirmed security actually changes", async () => {
  reads(); show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill();
  fireEvent.change(screen.getByLabelText("引用來源", { exact: true }), { target: { value: "hkex_evidence" } });
  expect(screen.getByLabelText("引用來源代號")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("引用來源代號"), { target: { value: "0005.CITED" } });
  fireEvent.change(screen.getByLabelText("引用除淨日期"), { target: { value: "2026-01-09" } });
  expect(screen.getByLabelText("引用來源代號")).toHaveValue("");
  expect(screen.getByRole("button", { name: "加入證券" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: `移除此引用證券 ${security.id}` }));
  expect(screen.getByLabelText("引用來源代號")).toHaveValue("");
});

it("makes an AS_DECLARED user assertion without a canonical status or client identity", async () => {
  reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(result));
  show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={vi.fn()} />); await fill();
  fireEvent.change(screen.getByLabelText("引用金額口徑"), { target: { value: "AS_DECLARED" } });
  submit(); await waitFor(() => expect(post).toHaveBeenCalledOnce());
  expect(options(post).body).toMatchObject({ adjustment_basis: "AS_DECLARED" });
  for (const forbidden of ["owner_id", "citation_id", "canonical_adoption", "http_status", "fx_rate_hkd", "cash_amount"]) expect(options(post).body).not.toHaveProperty(forbidden);
});

it("marks pending source writes busy and keeps settings navigation in another tab", async () => {
  reads(); let finish!: (value: ReturnType<typeof reply<CitedResult>>) => void;
  vi.spyOn(api, "POST").mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const cancel = vi.fn(); show(<CitedForm mode="dividends" onSaved={vi.fn()} onCancel={cancel} />); await fill(); submit();
  await waitFor(() => expect(screen.getByRole("form", { name: "引用股息來源條款" })).toHaveAttribute("aria-busy", "true"));
  expect(screen.getByLabelText("引用說明")).toBeDisabled(); expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
  expect(screen.getByRole("link", { name: "另開分頁管理來源政策" })).toHaveAttribute("target", "_blank"); expect(cancel).not.toHaveBeenCalled();
  await act(async () => finish(reply(result)));
});

it("hosts creation in the shared sheet and invalidates only sources without claiming a new current fact", async () => {
  reads(); vi.spyOn(api, "POST").mockResolvedValue(reply(result));
  const cache = show(<ObservationsPage mode="dividends" />); const invalidate = vi.spyOn(cache, "invalidateQueries");
  fireEvent.click(screen.getByRole("button", { name: "引用股息來源條款" })); await fill(); submit();
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByText(/可能是原鍵回覆/)).toBeVisible(); expect(screen.getByText(result.citation_id)).toBeVisible();
  expect(invalidate).toHaveBeenCalledExactlyOnceWith({ queryKey: ["sources"] });
  expect(cache.getMutationCache().getAll()).toHaveLength(0);
});
