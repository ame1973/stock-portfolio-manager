import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
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
      market: "US", first_date: "2099-01-01", local_run_time: "17:15", cadence: "DAILY", lookback_days: 30,
    },
  };
  const intent = headers();
  const created = await page.request.post("/api/v1/source-schedules", { headers: intent, data: input });
  expect(created.status()).toBe(201);
  expect(created.headers()["cache-control"]).toBe("no-store");
  expect(created.headers()["etag"]).toBe('"1"');
  const original = await created.json() as Schemas["SourceScheduleRecord"];
  expect(original).toMatchObject({ enabled: false, version: 1, next_run_at: null, occurrence_count: 0,
    scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN", automatic_materialization_installed: true,
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
  expect(active.automatic_materialization_installed).toBe(true);
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

  const project = process.env.SPM_E2E_COMPOSE_PROJECT!;
  const compose = (args: string[]) => execFileSync("docker", ["compose", "-p", project, ...args], {
    encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 60_000,
  });
  compose(["stop", "worker"]);
  let dueId: string;
  try {
    // Only the fixture freezes old intent; the real deployed daemon uses its unmodified clock.
    const output = execFileSync("docker", [
      "run", "--rm", "--network", "none", "--read-only", "--memory", "512m", "--cap-drop", "ALL",
      "--security-opt", "no-new-privileges", "--tmpfs", "/tmp", "--env", "SPM_SYNTHETIC_E2E=schedule-worker",
      "--env", "DATA_DIR=/data", "--env", `PUBLIC_ORIGIN=${process.env.SPM_E2E_BASE_URL!}`,
      "--mount", `type=volume,src=${project}_data,dst=/data`,
      "--mount", `type=bind,src=${resolve("tests/e2e/source_schedules.py")},dst=/tmp/source_schedules.py,readonly`,
      "spm-api:local", "python", "-I", "/tmp/source_schedules.py", securityId,
    ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30_000 });
    const seeded = JSON.parse(output) as { schedule_id: string; scope: string; provider_io: string };
    expect(seeded.scope).toBe("SYNTHETIC_EXPLICIT_DUE_INTENT");
    expect(seeded.provider_io).toBe("NOT_RUN");
    dueId = seeded.schedule_id;
  } finally { compose(["start", "worker"]); }
  const duePath = `source-schedules/${dueId}`;
  await expect.poll(async () => (await read<Schemas["SourceScheduleRecord"]>(duePath)).occurrence_count).toBe(1);
  const evaluated = await read<Schemas["SourceScheduleRecord"]>(duePath);
  expect(evaluated).toMatchObject({ enabled: true, version: 3, occurrence_count: 1,
    initial_backfill_queued: true, automatic_materialization_installed: true });
  const occurred = await read<Schemas["Page_SourceScheduleOccurrence_"]>(`${duePath}/occurrences`);
  expect(occurred.items).toHaveLength(1);
  expect(occurred.items[0]).toMatchObject({ sequence: 1, schedule_version: 2, outcome: "QUEUED",
    kind: "INITIAL_BACKFILL", scope: "SOURCE_ONLY", canonical_reconciliation: "NOT_RUN", job_id: evaluated.last_job_id });
  const jobPath = `jobs/${evaluated.last_job_id!}`;
  await expect.poll(async () => (await read<Schemas["SourceJob"]>(jobPath)).status).toBe("FAILED");
  const completed = await read<Schemas["SourceJob"]>(jobPath);
  expect(completed.attempt_count).toBe(0);
  expect(completed.result).toMatchObject({ provider_attempts: 0, provider_http_recorded: 0, pages_committed: 0 });
  const units = await read<Schemas["Page_SourceJobItem_"]>(`${jobPath}/items`);
  expect(units.items).toHaveLength(1);
  expect(units.items[0]).toMatchObject({ status: "SKIPPED", last_error_code: "UNCONFIGURED", attempt_count: 0 });
  const afterEvaluation = await snapshot();
  expect({ ...afterEvaluation, jobs: before.jobs }).toEqual(before);
  compose(["stop", "worker"]);
  compose(["up", "-d", "--wait", "worker"]);
  expect(await read<Schemas["SourceScheduleRecord"]>(duePath)).toEqual(evaluated);
  expect(await read<Schemas["Page_SourceScheduleOccurrence_"]>(`${duePath}/occurrences`)).toEqual(occurred);
  expect(await snapshot()).toEqual(afterEvaluation);
}
