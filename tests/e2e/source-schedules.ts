import { expect, type Page } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";

type Schemas = components["schemas"];

export async function sourceScheduleApi(
  page: Page, headers: (version?: number) => Record<string, string>, securityId: string,
  portfolioId: string, accountId: string,
) {
  async function read<T>(path: string): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`);
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`),
    trades: await read<{ items: Schemas["TradeRecord"][] }>(`trades?portfolio_id=${portfolioId}&include_history=true`),
    jobs: await read<Schemas["Page_SourceJob_"]>("jobs"),
  });
  const before = await snapshot();
  expect((await read<Schemas["Page_SourceScheduleRecord_"]>("source-schedules")).items).toEqual([]);
  const input: Schemas["SourceScheduleCreate"] = {
    name: "E2E explicit source API schedule", policy: {
      schema_version: 1, scope: "SOURCE_ONLY", calendar_basis: "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR",
      security_ids: [securityId], provider_ids: ["eodhd"], operations: ["DIVIDENDS"],
      market: "US", first_date: "2026-01-01", local_run_time: "17:15", cadence: "DAILY", lookback_days: 30,
    },
  };
  const intent = headers();
  const created = await page.request.post("/api/v1/source-schedules", { headers: intent, data: input });
  expect(created.status()).toBe(201);
  expect(created.headers()["cache-control"]).toBe("no-store");
  expect(created.headers()["etag"]).toBe('"1"');
  const original = await created.json() as Schemas["SourceScheduleRecord"];
  expect(original).toMatchObject({ enabled: false, version: 1, next_run_at: null, occurrence_count: 0,
    scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN", automatic_materialization_installed: false,
    exchange_timezone: "America/New_York", initial_backfill_queued: false, last_job_id: null });
  expect(original.policy.calendar_basis).toBe("LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR");
  const path = `source-schedules/${original.id}`;
  expect(created.headers()["location"]).toBe(`/api/v1/${path}`);
  expect(await read<Schemas["SourceScheduleRecord"]>(path)).toEqual(original);
  const change: Schemas["SourceScheduleUpdate"] = {
    name: original.name, policy: original.policy, enabled: true,
    reason: "Explicit synthetic scheduling intent, not a claim of automatic dispatch.",
  };
  const enabled = await page.request.patch(`/api/v1/${path}`, { headers: headers(1), data: change });
  expect(enabled.status()).toBe(200);
  expect(enabled.headers()["etag"]).toBe('"2"');
  const active = await enabled.json() as Schemas["SourceScheduleRecord"];
  expect(active.enabled).toBe(true);
  expect(active.next_run_at).not.toBeNull();
  expect(active.automatic_materialization_installed).toBe(false);
  const stale = await page.request.patch(`/api/v1/${path}`, { headers: headers(1), data: { ...change, enabled: false } });
  expect(stale.status()).toBe(412);
  const paused = await page.request.patch(`/api/v1/${path}`, { headers: headers(2), data: { ...change, enabled: false } });
  expect(paused.status()).toBe(200);
  expect(paused.headers()["cache-control"]).toBe("no-store");
  const final = await paused.json() as Schemas["SourceScheduleRecord"];
  expect(final).toMatchObject({ enabled: false, version: 3, next_run_at: null, occurrence_count: 0, initial_backfill_queued: false });
  const history = await read<Schemas["Page_SourceScheduleOccurrence_"]>(`${path}/occurrences`);
  expect(history).toEqual({ items: [], next_cursor: null });
  const replay = await page.request.post("/api/v1/source-schedules", { headers: intent, data: input });
  expect(replay.status()).toBe(201);
  expect(await replay.json()).toEqual(original);
  const listed = await read<Schemas["Page_SourceScheduleRecord_"]>("source-schedules?enabled=false&q=E2E%20explicit");
  expect(listed.items).toEqual([final]);
  for (const field of ["owner_id", "policy_json", "policy_sha256", "profile_sha256", "request_json", "dedupe_key", "credential_version"]) {
    expect(JSON.stringify({ final, history, listed })).not.toContain(field);
  }
  expect(await snapshot()).toEqual(before);
}
