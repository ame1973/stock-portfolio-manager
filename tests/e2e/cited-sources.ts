import { expect, type Page } from "@playwright/test";
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

  // The existing read workspace renders new source facts; there is still no citation-write form.
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
  await page.screenshot({ path: ".local/screenshots/cited-source-read-360.png", fullPage: true });
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
}
