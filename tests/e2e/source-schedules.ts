import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, type APIResponse, type Page, type Route } from "@playwright/test";
import { cancelSheet, sheetGeometry } from "./workspace";
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

export async function sourceScheduleBrowserForms(
  page: Page, headers: (version?: number) => Record<string, string>, securityId: string,
  portfolioId: string, accountId: string,
) {
  async function read<T>(path: string, params: Record<string, string | number> = {}): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`, { params });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`),
    trades: await read<{ items: Schemas["TradeRecord"][] }>("trades", { portfolio_id: portfolioId, include_history: "true" }),
  });
  const before = await snapshot();
  const initial = await read<Schemas["Page_SourceScheduleRecord_"]>("source-schedules");
  const actualDue = initial.items.find(row => row.initial_backfill_queued)!;
  expect(actualDue.occurrence_count).toBe(1);
  await page.getByRole("navigation", { name: "資料中心分類" }).getByRole("link", { name: "來源排程", exact: true }).click();
  const create = page.getByRole("button", { name: "建立停用來源排程", exact: true });
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await create.click();
    const sheet = await sheetGeometry(page, "建立停用來源排程", width === 360);
    const form = sheet.getByRole("form");
    for (const field of ["排程名稱", "最早市場日期", "市場當地執行時間", "回看窗口數量"]) await expect(form.getByLabel(field, { exact: true })).toHaveValue("");
    await expect(form.getByLabel("排程市場", { exact: true })).toHaveValue("");
    await expect(form.getByLabel("週期規則", { exact: true })).toHaveValue("");
    await expect(form.getByRole("checkbox", { name: /^EODHD/ })).not.toBeChecked();
    await expect(form.getByLabel(/啟用這個來源排程/)).toHaveCount(0);
    for (const field of ["排程名稱", "排程市場", "市場當地執行時間", "最早市場日期"]) {
      const bounds = (await form.getByLabel(field, { exact: true }).boundingBox())!;
      expect(bounds.height).toBeGreaterThanOrEqual(44); expect(bounds.height).toBeLessThanOrEqual(56);
    }
    await page.screenshot({ path: `.local/screenshots/source-schedule-sheet-${width}.png` });
    await cancelSheet(page, sheet, create);
  }
  await create.click();
  const form = page.getByRole("form", { name: "建立停用來源排程", exact: true });
  const name = "E2E schedule UI scoped intent";
  await form.getByLabel("排程名稱", { exact: true }).fill(name);
  await form.getByLabel("排程市場", { exact: true }).selectOption("US");
  await form.getByLabel("排程證券", { exact: true }).selectOption(securityId);
  await form.getByRole("button", { name: "加入證券", exact: true }).click();
  await form.getByLabel("最早市場日期", { exact: true }).fill("2099-01-01");
  await form.getByLabel("市場當地執行時間", { exact: true }).fill("17:15");
  await form.getByLabel("週期規則", { exact: true }).selectOption("WEEKLY");
  await form.getByLabel("每週執行星期", { exact: true }).selectOption("4");
  await form.getByLabel("回看窗口單位", { exact: true }).selectOption("MONTHS");
  await form.getByLabel("回看窗口數量", { exact: true }).fill("24");
  await form.getByLabel("首次回補起日", { exact: true }).fill("2025-01-01");
  await form.getByRole("checkbox", { name: /^EODHD/ }).check();
  await form.getByLabel("擷取拆股因子", { exact: true }).check();
  let release!: () => void, committed!: (response: APIResponse) => void, failed!: (error: unknown) => void;
  const released = new Promise<void>(resolve => { release = resolve; });
  const response = new Promise<APIResponse>((resolve, reject) => { committed = resolve; failed = reject; });
  const hold = async (route: Route) => {
    if (route.request().method() !== "POST") return route.continue();
    try {
      const body = route.request().postDataJSON() as Schemas["SourceScheduleCreate"];
      expect(body).toEqual({ name, policy: { schema_version: 1, scope: "SOURCE_ONLY", calendar_basis: "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR", security_ids: [securityId], provider_ids: ["eodhd"], operations: ["DIVIDENDS", "SPLITS"], market: "US", first_date: "2099-01-01", local_run_time: "17:15", cadence: "WEEKLY", weekday: 4, lookback_days: null, lookback_months: 24, backfill_from_date: "2025-01-01" } });
      const actual = await route.fetch(); committed(actual);
      await released; await route.fulfill({ response: actual });
    } catch (error) { failed(error); throw error; }
  };
  await page.route("**/api/v1/source-schedules", hold);
  let original!: Schemas["SourceScheduleRecord"];
  try {
    await form.getByRole("button", { name: "儲存停用排程", exact: true }).click();
    const actual = await response;
    expect(actual.status()).toBe(201);
    original = await actual.json() as Schemas["SourceScheduleRecord"];
    expect(original).toMatchObject({ enabled: false, version: 1, next_run_at: null, occurrence_count: 0 });
    await expect(form).toHaveAttribute("aria-busy", "true");
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "關閉編輯", exact: true }).click();
    await expect(form).toBeVisible();
    await expect(form.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
    release();
    await expect(form).toHaveCount(0);
    await expect(create).toBeFocused();
  } finally { release(); await page.unroute("**/api/v1/source-schedules", hold); }
  await page.getByLabel("搜尋來源排程", { exact: true }).fill("E2E schedule UI");
  async function openEdit() {
    const row = page.locator(`tr[data-record-id="${original.id}"]`);
    await expect(row).toBeVisible();
    const article = page.getByRole("article", { name: original.id, exact: true });
    if (!await article.isVisible()) await row.getByRole("button", { name: "查看原值與操作", exact: true }).click();
    const opener = article.getByRole("button", { name: "編輯、啟用或暫停排程", exact: true });
    await opener.click();
    const sheet = await sheetGeometry(page, "編輯來源排程");
    return { sheet, opener, form: sheet.getByRole("form") };
  }
  let edit = await openEdit();
  await edit.form.getByLabel(/啟用這個來源排程/).check();
  await edit.form.getByLabel(/我明白已啟用排程/).check();
  await edit.form.getByLabel("排程修訂依據", { exact: true }).fill("Explicitly enable this future synthetic source schedule.");
  await edit.form.getByRole("button", { name: "儲存排程修訂", exact: true }).click();
  await expect(edit.form).toHaveCount(0);
  const enabled = await read<Schemas["SourceScheduleRecord"]>(`source-schedules/${original.id}`);
  expect(enabled).toMatchObject({ enabled: true, version: 2, occurrence_count: 0, exchange_timezone: "America/New_York" });
  expect(enabled.next_run_at).not.toBeNull();
  edit = await openEdit();
  await edit.form.getByLabel("排程名稱", { exact: true }).fill("Stale local schedule input");
  await edit.form.getByLabel("排程修訂依據", { exact: true }).fill("Preserve this observed source policy during a real conflict.");
  await edit.form.getByLabel(/我明白已啟用排程/).check();
  const other = await page.request.patch(`/api/v1/source-schedules/${original.id}`, { headers: headers(2), data: { name: enabled.name, policy: enabled.policy, enabled: false, reason: "Synthetic concurrent owner pauses the observed schedule." } });
  expect(other.status()).toBe(200);
  await edit.form.getByRole("button", { name: "儲存排程修訂", exact: true }).click();
  await expect(edit.form.getByText(/紀錄已變更；請取消編輯/)).toBeVisible();
  await expect(edit.form.getByLabel("排程名稱", { exact: true })).toHaveValue("Stale local schedule input");
  await cancelSheet(page, edit.sheet, edit.opener, true);
  await page.getByRole("button", { name: "重新載入來源排程", exact: true }).click();
  await expect(page.locator(`tr[data-record-id="${original.id}"]`).getByRole("cell").nth(1)).toHaveText("未啟用");
  edit = await openEdit();
  await expect(edit.form.getByLabel(/啟用這個來源排程/)).not.toBeChecked();
  await edit.form.getByLabel("排程名稱", { exact: true }).fill("E2E schedule UI reviewed intent");
  await edit.form.getByLabel("週期規則", { exact: true }).selectOption("WEEKDAYS");
  await edit.form.getByLabel("排程修訂依據", { exact: true }).fill("Review the latest version and retain the explicit calendar-month window.");
  await edit.form.getByRole("button", { name: "儲存排程修訂", exact: true }).click();
  await expect(edit.form).toHaveCount(0);
  expect(await read<Schemas["SourceScheduleRecord"]>(`source-schedules/${original.id}`)).toMatchObject({ version: 4, enabled: false, next_run_at: null, occurrence_count: 0, policy: { cadence: "WEEKDAYS", weekday: null, lookback_months: 24, lookback_days: null } });
  const tabs = page.getByRole("navigation", { name: "資料中心分類" });
  await tabs.getByRole("link", { name: "來源設定", exact: true }).click();
  await tabs.getByRole("link", { name: "來源排程", exact: true }).click();
  await expect(page.getByLabel("搜尋來源排程", { exact: true })).toHaveValue("E2E schedule UI");
  expect(new URL(page.url()).search).toBe("");
  await page.reload();
  await expect(page.getByLabel("搜尋來源排程", { exact: true })).toHaveValue("");

  const project = process.env.SPM_E2E_COMPOSE_PROJECT!;
  const compose = (args: string[]) => execFileSync("docker", ["compose", "-p", project, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 60_000 });
  compose(["stop", "worker"]);
  let historyId: string;
  try {
    const output = execFileSync("docker", ["run", "--rm", "--network", "none", "--read-only", "--memory", "512m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp", "--env", "SPM_SYNTHETIC_E2E=schedule-worker", "--env", "DATA_DIR=/data", "--env", `PUBLIC_ORIGIN=${process.env.SPM_E2E_BASE_URL!}`, "--mount", `type=volume,src=${project}_data,dst=/data`, "--mount", `type=bind,src=${resolve("tests/e2e/source_schedules.py")},dst=/tmp/source_schedules.py,readonly`, "spm-api:local", "python", "-I", "/tmp/source_schedules.py", securityId, "--history"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30_000 });
    const fixture = JSON.parse(output) as { schedule_id: string; scope: string; provider_io: string };
    expect(fixture.scope).toBe("SYNTHETIC_PAUSED_HISTORY"); expect(fixture.provider_io).toBe("NOT_RUN");
    historyId = fixture.schedule_id;
  } finally { compose(["start", "worker"]); }
  await page.goto(`/data-center/schedules/${historyId}`);
  const first = await read<Schemas["Page_SourceScheduleOccurrence_"]>(`source-schedules/${historyId}/occurrences`, { limit: 25 });
  const second = await read<Schemas["Page_SourceScheduleOccurrence_"]>(`source-schedules/${historyId}/occurrences`, { limit: 25, cursor: first.next_cursor! });
  const rows = page.locator("table tbody tr[data-record-id]");
  await expect(rows).toHaveCount(25);
  expect(await rows.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-record-id")))).toEqual(first.items.map(row => row.id));
  await rows.first().getByRole("button", { name: "查看原值與操作", exact: true }).click();
  await expect(page.getByRole("heading", { name: /評估 27：.*SKIPPED_IN_FLIGHT/ })).toBeVisible();
  await page.screenshot({ path: ".local/screenshots/source-schedules-history-1440.png", fullPage: true });
  await page.getByRole("button", { name: "下一頁", exact: true }).click();
  await expect(rows).toHaveCount(2);
  expect(await rows.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-record-id")))).toEqual(second.items.map(row => row.id));
  await expect(page.getByRole("button", { name: "下一頁", exact: true })).toBeDisabled();
  const queued = second.items.find(row => row.outcome === "QUEUED")!;
  await page.locator(`tr[data-record-id="${queued.id}"]`).getByRole("button", { name: "查看原值與操作", exact: true }).click();
  await expect(page.getByRole("link", { name: "查看該次排隊的來源工作", exact: true })).toHaveAttribute("href", `/data-center/jobs/${queued.job_id!}`);
  await expect.poll(async () => (await read<Schemas["SourceJob"]>(`jobs/${queued.job_id!}`)).status).toBe("FAILED");
  for (const width of [768, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole("table")).toHaveCount(0);
    const article = page.getByRole("article", { name: queued.id, exact: true });
    await expect(article).toContainText(`${queued.window.start} / ${queued.window.end}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/source-schedules-history-${width}.png`, fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/source-schedules-dark-360.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(`/data-center/schedules/${actualDue.id}`);
  await page.getByRole("button", { name: "編輯、啟用或暫停排程", exact: true }).click();
  const consumed = page.getByRole("form", { name: "編輯來源排程", exact: true });
  await expect(consumed.getByLabel("首次回補起日", { exact: true })).toHaveAttribute("readonly", "");
  await consumed.getByLabel(/啟用這個來源排程/).uncheck();
  await consumed.getByLabel("排程修訂依據", { exact: true }).fill("Explicit UI pause retains the already queued initial source evidence.");
  await consumed.getByRole("button", { name: "儲存排程修訂", exact: true }).click();
  await expect(consumed).toHaveCount(0);
  const paused = await read<Schemas["SourceScheduleRecord"]>(`source-schedules/${actualDue.id}`);
  expect(paused).toMatchObject({ enabled: false, version: 4, occurrence_count: 1, initial_backfill_queued: true, last_job_id: actualDue.last_job_id });
  expect(await snapshot()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(async () => (await indexedDB.databases()).length + (await caches.keys()).length)).toBe(0);
}
