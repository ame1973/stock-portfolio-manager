import { expect, type Page } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";

type Schemas = components["schemas"];

export async function sourceJobApi(
  page: Page, headers: () => Record<string, string>, securityId: string,
  portfolioId: string, accountId: string,
) {
  async function read<T>(path: string, params: Record<string, string> = {}): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`, { params });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`, { as_of: "2026-10-31" }),
    trades: await read<{ items: Schemas["TradeRecord"][] }>("trades", { portfolio_id: portfolioId, include_history: "true" }),
  });
  const before = await snapshot();
  const intent = headers();
  const input: Schemas["SyncDividendRequest"] = {
    security_ids: [securityId], provider_ids: ["eodhd"], from_date: "2026-01-01", to_date: "2026-01-31",
    operations: ["DIVIDENDS"], force: false,
  };
  const accepted = await page.request.post("/api/v1/dividends/sync", { headers: intent, data: input });
  expect(accepted.status()).toBe(202);
  expect(accepted.headers()["cache-control"]).toBe("no-store");
  const created = await accepted.json() as Schemas["JobCreated"];
  expect(created.scope).toBe("SOURCE_ONLY");
  expect(created.canonical_reconciliation).toBe("NOT_RUN");
  expect(created.status).toBe("QUEUED");
  const path = `jobs/${created.job_id}`;
  expect(accepted.headers()["location"]).toBe(`/api/v1/${path}`);
  const replay = await page.request.post("/api/v1/dividends/sync", { headers: intent, data: input });
  expect(replay.status()).toBe(202);
  expect(await replay.json()).toEqual(created);
  // The fresh deployment has no keys/profiles: observe the real worker's bounded skip, not fake income.
  await expect.poll(async () => (await read<Schemas["SourceJob"]>(path)).status, { timeout: 10_000 }).toBe("FAILED");
  const job = await read<Schemas["SourceJob"]>(path);
  expect(job.scope).toBe("SOURCE_ONLY");
  expect(job.result.provider_attempts).toBe(0);
  expect(job.result.provider_http_recorded).toBe(0);
  expect(job.result.observations_new).toBe(0);
  expect(job.result.pages_committed).toBe(0);
  expect(job.canonical_reconciliation).toBe("NOT_RUN");
  const items = await read<{ items: Schemas["SourceJobItem"][] }>(`${path}/items`);
  expect(items.items).toHaveLength(1);
  expect(items.items[0]).toMatchObject({ security_id: securityId, provider_id: "eodhd", status: "SKIPPED", attempt_count: 0, last_error_code: "UNCONFIGURED" });
  const listed = await read<{ items: Schemas["SourceJob"][] }>("jobs", { q: created.job_id, status: "FAILED" });
  expect(listed.items.map(row => row.id)).toEqual([created.job_id]);
  const providers = await read<{ items: Schemas["ProviderInfo"][] }>("providers");
  expect(providers.items).toHaveLength(8);
  expect(providers.items.every(row => !row.configured && row.live_validation === "NOT_TESTED" && !row.raw_payload_storage)).toBe(true);
  expect(providers.items.find(row => row.provider_id === "yahoo")?.origin_group).toBe("YAHOO");
  expect(providers.items.find(row => row.provider_id === "yfinance")?.origin_group).toBe("YAHOO");
  const health = await read<Schemas["ProviderHealth"]>("providers/eodhd/health");
  expect(health).toMatchObject({ configured: false, state: "UNCONFIGURED", last_result: null, last_success_at: null, quota_remaining: null, live_validation: "NOT_TESTED", legacy_runs_omitted: 0 });
  const coverage = await read<{ items: Schemas["ProviderCoverage"][] }>("providers/coverage", { security_id: securityId });
  expect(coverage.items).toHaveLength(8);
  for (const row of coverage.items) {
    expect(row).toMatchObject({ security_id: securityId, status: "NOT_TESTED", coverage_start: null, coverage_end: null, coverage_basis: "NOT_ESTABLISHED", last_result: null, last_success_at: null, observations_count: 0, legacy_runs_omitted: 0, legacy_status_ignored: false });
  }
  const data = JSON.stringify({ job, items, providers, health, coverage });
  for (const privateField of ["request_json", "result_json", "profile_sha256", "profile_json", "lease_owner", "cursor_in", "credential_version", "terms_reference"]) expect(data).not.toContain(privateField);
  expect(await snapshot()).toEqual(before);
}
