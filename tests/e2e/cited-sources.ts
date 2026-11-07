import { expect, type APIResponse, type Locator, type Page, type Route } from "@playwright/test";
import { cancelSheet, sheetGeometry } from "./workspace";
import type { components } from "../../packages/contracts/src/schema";

type Schemas = components["schemas"];

export async function citedSourceApi(page: Page, headers: (version?: number) => Record<string, string>, portfolioId: string, accountId: string) {
  async function read<T>(path: string, params: Record<string, string> = {}): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`, { params });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  async function create<T>(path: string, data: object): Promise<T> {
    const response = await page.request.post(`/api/v1/${path}`, { headers: headers(), data });
    expect(response.status()).toBe(201);
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`),
    trades: await read<{ items: Schemas["TradeRecord"][] }>("trades", { portfolio_id: portfolioId, include_history: "true" }),
    jobs: await read<Schemas["Page_SourceJob_"]>("jobs"),
  });
  const before = await snapshot();
  const reason = "Synthetic source quotation and access declaration, never document verification or financial adoption.";
  const hk = await create<Schemas["Security"]>("securities", { exchange_code: "HKG", display_ticker: "0068", name: "E2E cited Hong Kong security", quote_currency: "USD", identifier_effective_from: "2020-01-01" });
  const us = await create<Schemas["Security"]>("securities", { exchange_code: "NYSE", display_ticker: "CCE.B", name: "E2E cited US security", quote_currency: "USD", identifier_effective_from: "2020-01-01" });
  const policies = new Map<string, Schemas["ProviderPolicy"]>();
  for (const [provider, stock] of [["manual", hk], ["hkex_evidence", hk], ["sec_evidence", us]] as const) {
    await create(`securities/${stock.id}/provider-symbols`, { provider_id: provider, provider_symbol: `${stock.display_ticker}.CITED`, effective_from: "2020-01-01" });
    const initial = await create<Schemas["ProviderProfileRecord"]>("provider-profiles", { policy: { provider_id: provider }, reason });
    expect(initial.policy.enabled).toBe(false);
    const policy: Schemas["ProviderPolicy"] = { ...initial.policy, enabled: true, terms_acknowledged: true, terms_reference: "Explicit synthetic human citation permission", terms_reason: reason };
    const enabled = await page.request.patch(`/api/v1/provider-profiles/${provider}`, { headers: headers(1), data: { policy, reason } });
    expect(enabled.status()).toBe(200);
    policies.set(provider, policy);
  }
  const citation = { citation: "E2E attributable quotation; document not fetched", reason };
  const inputs: (Schemas["CitedDividendCreate"] | Schemas["CitedSplitCreate"])[] = [
    { operation: "DIVIDENDS", provider_id: "manual", security_id: hk.id, provider_symbol: "0068.CITED", ex_date: "2026-01-08", dps: "0.9", currency: "USD", adjustment_basis: "UNKNOWN", event_type: "UNKNOWN", source_period: "QUARTERLY", citation },
    { operation: "DIVIDENDS", provider_id: "hkex_evidence", security_id: hk.id, provider_symbol: "0068.CITED", ex_date: "2026-01-08", dps: "92233720368.54775807", currency: null, source_currency: "EUR", adjustment_basis: "UNKNOWN", event_type: "UNKNOWN", citation: { ...citation, url: "https://www.hkexnews.hk/synthetic.pdf?private-link=fixture#page" } },
    { operation: "SPLITS", provider_id: "sec_evidence", security_id: us.id, provider_symbol: "CCE.B.CITED", effective_date: "2026-01-08", ratio_numerator: "1", ratio_denominator: "3", citation: { ...citation, url: "https://www.sec.gov/Archives/synthetic.htm" } },
    { operation: "SPLITS", provider_id: "manual", security_id: hk.id, provider_symbol: "0068.CITED", effective_date: "2026-01-08", ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806", citation },
  ];
  const path = "/api/v1/providers/cited-observations";
  const intents = inputs.map(() => headers());
  const results: Schemas["CitedSourceCreated"][] = [];
  for (const [index, input] of inputs.entries()) {
    const response = await page.request.post(path, { headers: intents[index], data: input });
    expect(response.status()).toBe(201);
    expect(response.headers()["cache-control"]).toBe("no-store");
    expect(response.headers()["location"]).toBeUndefined();
    const created = await response.json() as Schemas["CitedSourceCreated"];
    expect(created).toMatchObject({ provider_id: input.provider_id, security_id: input.security_id, operation: input.operation, scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", document_verification: "NOT_VERIFIED" });
    for (const field of ["owner_id", "context_id", "profile_json", "credential_artifact_id"]) expect(JSON.stringify(created)).not.toContain(field);
    results.push(created);
  }
  expect(new Set(results.map(row => row.citation_id)).size).toBe(4);
  const dividends = await read<Schemas["ObservationPage_DividendObservationRecord_"]>("dividends/observations", { security_id: hk.id });
  const splits = await read<Schemas["ObservationPage_SplitObservationRecord_"]>("providers/split-observations");
  expect(dividends.items).toHaveLength(2);
  const exact = dividends.items.find(row => row.provider_id === "hkex_evidence")!;
  expect(exact).toMatchObject({ dps: "92233720368.54775807", currency: null, source_currency: "EUR", adjustment_basis: "UNKNOWN", evidence: { url: null, url_withheld: true } });
  expect(dividends.items.find(row => row.provider_id === "manual")).toMatchObject({ dps: "0.90000000", currency: "USD", event_type: "UNKNOWN" });
  for (const [index, numerator, denominator] of [[2, "1", "3"], [3, "9223372036854775807", "9223372036854775806"]] as const) {
    expect(splits.items.find(row => row.id === results[index]!.observation_id)).toMatchObject({ ratio_numerator: numerator, ratio_denominator: denominator, effect_scope: "PROVIDER_ADJUSTMENT_ONLY", sightings_count: 1 });
  }
  for (const [provider, policy] of policies) {
    const paused = await page.request.patch(`/api/v1/provider-profiles/${provider}`, { headers: headers(2), data: { policy: { ...policy, enabled: false }, reason } });
    expect(paused.status()).toBe(200);
    expect((await paused.json() as Schemas["ProviderProfileRecord"]).version).toBe(3);
  }
  const replay = await page.request.post(path, { headers: intents[0], data: inputs[0] });
  expect(replay.status()).toBe(201);
  expect(await replay.json()).toEqual(results[0]);
  const unavailable = await page.request.post(path, { headers: headers(), data: inputs[0] });
  expect(unavailable.status()).toBe(503);
  expect((await unavailable.json() as Schemas["Error"]).code).toBe("CITED_SOURCE_UNCONFIGURED");
  expect(await read<Schemas["ObservationPage_DividendObservationRecord_"]>("dividends/observations", { security_id: hk.id })).toEqual(dividends);
  expect(await snapshot()).toEqual(before);

  // The read workspace renders quoted source facts without adopting their terms.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/data-center/dividends");
  const row = page.locator(`tr[data-record-id="${exact.id}"]`);
  await expect(row).toContainText("92233720368.54775807");
  await row.getByRole("button", { name: "查看原值與操作", exact: true }).click();
  await expect(page.getByRole("article").filter({ hasText: exact.id })).toContainText(citation.citation);
  await page.screenshot({ path: ".local/screenshots/cited-source-read-1440.png", fullPage: true });
  await page.setViewportSize({ width: 360, height: 900 });
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("article").filter({ hasText: exact.id })).toContainText("92233720368.54775807");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("article").filter({ hasText: exact.id }).screenshot({ path: ".local/screenshots/cited-source-read-360.png" });
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
}

export async function citedSourceBrowserForms(page: Page, headers: (version?: number) => Record<string, string>, portfolioId: string, accountId: string) {
  async function read<T>(path: string, params: Record<string, string> = {}): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`, { params });
    expect(response.status()).toBe(200);
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`),
    trades: await read<{ items: Schemas["TradeRecord"][] }>("trades", { portfolio_id: portfolioId, include_history: "true" }),
    jobs: await read<Schemas["Page_SourceJob_"]>("jobs"),
  });
  const before = await snapshot();
  const securities = await read<Schemas["Page_Security_"]>("securities", { q: "E2E cited" });
  const hk = securities.items.find(row => row.display_ticker === "0068")!;
  const us = securities.items.find(row => row.display_ticker === "CCE.B")!;
  const reason = "Explicit synthetic browser quotation, never verified document contents or financial adoption.";
  const policies = new Map<string, Schemas["ProviderPolicy"]>();
  for (const provider of ["manual", "hkex_evidence", "sec_evidence"] as const) {
    const current = await read<Schemas["ProviderProfileRecord"]>(`provider-profiles/${provider}`);
    expect(current.version).toBe(3);
    const policy = { ...current.policy, enabled: true };
    const enabled = await page.request.patch(`/api/v1/provider-profiles/${provider}`, { headers: headers(3), data: { policy, reason } });
    expect(enabled.status()).toBe(200);
    policies.set(provider, policy);
  }
  await page.goto("/data-center/dividends");
  const create = page.getByRole("button", { name: "引用股息來源條款", exact: true });
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await create.click();
    const sheet = await sheetGeometry(page, "引用股息來源條款", width === 360);
    const form = sheet.getByRole("form");
    for (const label of ["引用來源", "引用來源代號", "引用除淨日期", "引用每股金額（未知留空）", "引用股息幣別"]) await expect(form.getByLabel(label, { exact: true })).toHaveValue("");
    await expect(form.getByLabel("引用金額口徑")).toHaveValue("UNKNOWN");
    for (const label of ["引用來源", "引用來源代號", "引用除淨日期", "引用每股金額（未知留空）"]) {
      const bounds = (await form.getByLabel(label, { exact: true }).boundingBox())!;
      expect(bounds.height).toBeGreaterThanOrEqual(44); expect(bounds.height).toBeLessThanOrEqual(56);
    }
    await page.screenshot({ path: `.local/screenshots/cited-form-${width}.png` });
    await cancelSheet(page, sheet, create);
  }

  async function fill(mode: "dividends" | "splits", provider: "manual" | "hkex_evidence" | "sec_evidence", stock: Schemas["Security"], reference: string): Promise<Locator> {
    await page.goto(`/data-center/${mode}`);
    await page.getByRole("button", { name: mode === "dividends" ? "引用股息來源條款" : "引用拆股來源因子", exact: true }).click();
    const form = page.getByRole("form", { name: mode === "dividends" ? "引用股息來源條款" : "引用拆股來源因子", exact: true });
    await form.getByLabel("引用證券", { exact: true }).selectOption(stock.id);
    await form.getByRole("button", { name: "加入證券", exact: true }).click();
    await form.getByLabel("引用來源", { exact: true }).selectOption(provider);
    await form.getByLabel(mode === "dividends" ? "引用除淨日期" : "引用因子生效日期").fill("2026-01-08");
    await form.getByText("查閱已保存代號（不會自動填入）", { exact: true }).click();
    await form.getByRole("button", { name: `使用此引用代號 ${stock.display_ticker}.CITED`, exact: true }).click();
    await expect(form.getByLabel("引用來源代號")).toHaveValue(`${stock.display_ticker}.CITED`);
    await form.getByLabel("引用文件或本機參考").fill(reference);
    await form.getByLabel("引用說明").fill(reason);
    await form.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ }).check();
    return form;
  }
  const path = "/api/v1/providers/cited-observations";
  let form = await fill("dividends", "manual", hk, "E2E UI local quotation page 5");
  await form.getByLabel("引用每股金額（未知留空）").fill("92233720368.54775807");
  await form.getByLabel("來源幣別標籤（選填）").fill("EUR");
  await form.getByLabel("來源期間標籤（選填）").selectOption("QUARTERLY");
  await form.getByLabel("引用宣告日期（選填）").fill("2026-01-01");
  await form.getByLabel("引用付款日期（選填）").fill("2026-02-01");
  let release!: () => void;
  const released = new Promise<void>(resolve => { release = resolve; });
  let committed!: (response: APIResponse) => void;
  let failed!: (error: unknown) => void;
  const response = new Promise<APIResponse>((resolve, reject) => { committed = resolve; failed = reject; });
  let originalKey = "";
  const hold = async (route: Route) => {
    if (route.request().method() !== "POST") return route.continue();
    try {
      originalKey = route.request().headers()["idempotency-key"]!;
      expect(route.request().postDataJSON()).toMatchObject({ operation: "DIVIDENDS", provider_id: "manual", security_id: hk.id, provider_symbol: "0068.CITED", dps: "92233720368.54775807", currency: null, source_currency: "EUR", event_type: "UNKNOWN", adjustment_basis: "UNKNOWN" });
      const actual = await route.fetch(); committed(actual);
      await released; await route.abort("failed");
    } catch (error) { failed(error); throw error; }
  };
  await page.route(`**${path}`, hold);
  let original!: Schemas["CitedSourceCreated"];
  try {
    await form.getByRole("button", { name: "儲存引用，不採納財務", exact: true }).click();
    const actual = await response;
    expect(actual.status()).toBe(201);
    original = await actual.json() as Schemas["CitedSourceCreated"];
    await expect(form).toHaveAttribute("aria-busy", "true");
    await page.keyboard.press("Escape"); await page.getByRole("button", { name: "關閉編輯", exact: true }).click();
    await expect(form).toBeVisible(); await expect(form.getByLabel("引用說明")).toBeDisabled();
    await expect(page.getByText(/請等候回應後再關閉/)).toBeVisible();
    release(); await expect(form.getByRole("alert").filter({ hasText: "網絡連線中斷" })).toBeVisible();
  } finally { release(); await page.unroute(`**${path}`, hold); }
  await expect(form.getByLabel("引用每股金額（未知留空）")).toHaveValue("92233720368.54775807");
  const paused = await page.request.patch("/api/v1/provider-profiles/manual", { headers: headers(4), data: { policy: { ...policies.get("manual")!, enabled: false }, reason } });
  expect(paused.status()).toBe(200);
  const aliases = await read<Schemas["Page_ProviderSymbol_"]>(`securities/${hk.id}/provider-symbols`);
  const manual = aliases.items.find(row => row.provider_id === "manual")!;
  const corrected = await page.request.patch(`/api/v1/securities/${hk.id}/provider-symbols/${manual.id}`, { headers: headers(1), data: { effective_from: "2020-01-02", effective_to: null, reason } });
  expect(corrected.status()).toBe(200);
  const aliasUrl = `**/api/v1/securities/${hk.id}/provider-symbols**`;
  await page.route(aliasUrl, route => route.abort("failed"));
  try {
    await form.getByLabel("搜尋引用代號參考").fill("0068");
    await expect(form.getByRole("button", { name: "重試載入代號參考", exact: true })).toBeVisible();
    await form.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ }).uncheck();
    await form.getByRole("checkbox", { name: /我明白只保存 SOURCE_ONLY/ }).check();
    const replayed = page.waitForResponse(item => item.url().endsWith(path) && item.request().method() === "POST");
    await form.getByRole("button", { name: "儲存引用，不採納財務", exact: true }).click();
    const replay = await replayed;
    expect(replay.status()).toBe(201);
    expect(replay.request().headers()["idempotency-key"]).toBe(originalKey);
    expect(await replay.json()).toEqual(original);
    await expect(form).toHaveCount(0);
    await expect(page.getByRole("status").filter({ hasText: "伺服器已確認來源引用" })).toContainText(original.citation_id);
  } finally { await page.unroute(aliasUrl); }

  async function save(form: Locator): Promise<Schemas["CitedSourceCreated"]> {
    const completed = page.waitForResponse(item => item.url().endsWith(path) && item.request().method() === "POST");
    await form.getByRole("button", { name: "儲存引用，不採納財務", exact: true }).click();
    const actual = await completed;
    expect(actual.status()).toBe(201); expect(actual.headers()["cache-control"]).toBe("no-store");
    await expect(form).toHaveCount(0);
    return await actual.json() as Schemas["CitedSourceCreated"];
  }
  form = await fill("dividends", "hkex_evidence", hk, "E2E UI official-looking quotation page 6");
  await form.getByLabel("引用每股金額（未知留空）").fill("0");
  await form.getByLabel("引用股息幣別").selectOption("HKD");
  await form.getByLabel("引用股息類型").selectOption("SPECIAL");
  await form.getByLabel("引用金額口徑").selectOption("AS_DECLARED");
  await form.getByLabel("引用網址（人工來源可留空）").fill("http://www.hkexnews.hk/synthetic-ui.pdf");
  await form.getByRole("button", { name: "儲存引用，不採納財務", exact: true }).click();
  await expect(form.getByRole("alert").filter({ hasText: /官方引用需要/ })).toBeVisible();
  await form.getByLabel("引用網址（人工來源可留空）").fill("https://www.hkexnews.hk/synthetic-ui.pdf?reference=fixture#page6");
  const zero = await save(form);
  for (const [provider, stock, numerator, denominator] of [["sec_evidence", us, "1", "3"], ["hkex_evidence", hk, "9223372036854775807", "9223372036854775806"]] as const) {
    form = await fill("splits", provider, stock, `E2E UI exact ${provider} factor page 7`);
    await expect(form.getByLabel("引用比例分子")).toHaveValue(""); await expect(form.getByLabel("引用比例分母")).toHaveValue("");
    await form.getByLabel("引用比例分子").fill("9223372036854775808");
    await form.getByLabel("引用比例分母").fill(denominator);
    await form.getByLabel("引用網址（人工來源可留空）").fill(provider === "sec_evidence" ? "https://www.sec.gov/Archives/synthetic-ui.htm" : "https://www.hkexnews.hk/synthetic-ui.pdf");
    await form.getByRole("button", { name: "儲存引用，不採納財務", exact: true }).click();
    await expect(form.getByRole("alert").filter({ hasText: /比例必須介乎/ })).toBeVisible();
    await expect(form.getByLabel("引用比例分子")).toHaveValue("9223372036854775808");
    await form.getByLabel("引用比例分子").fill(numerator);
    const created = await save(form);
    const rows = await read<Schemas["ObservationPage_SplitObservationRecord_"]>("providers/split-observations", { security_id: stock.id });
    expect(rows.items.find(row => row.id === created.observation_id)).toMatchObject({ ratio_numerator: numerator, ratio_denominator: denominator, effect_scope: "PROVIDER_ADJUSTMENT_ONLY" });
  }
  for (const provider of ["hkex_evidence", "sec_evidence"] as const) expect((await page.request.patch(`/api/v1/provider-profiles/${provider}`, { headers: headers(4), data: { policy: { ...policies.get(provider)!, enabled: false }, reason } })).status()).toBe(200);
  const dividends = await read<Schemas["ObservationPage_DividendObservationRecord_"]>("dividends/observations", { security_id: hk.id });
  expect(dividends.items.find(row => row.id === original.observation_id)).toMatchObject({ dps: "92233720368.54775807", currency: null, source_currency: "EUR", source_period: "QUARTERLY", event_type: "UNKNOWN", sightings_count: 1 });
  expect(dividends.items.find(row => row.id === zero.observation_id)).toMatchObject({ dps: "0.00000000", currency: "HKD", event_type: "SPECIAL", adjustment_basis: "AS_DECLARED", canonical_adoption: "NOT_ADOPTED", evidence: { url: null, url_withheld: true } });
  expect(await snapshot()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
}
