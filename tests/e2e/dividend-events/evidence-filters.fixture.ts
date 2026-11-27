import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect, request } from "@playwright/test";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

test("evidence filters read actual retained reviews without granting resolution", async ({ page }) => {
  stage("EVIDENCE_FILTER_SETUP");
  const password = `synthetic-evidence-filter-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap);
  await page.getByLabel(/設定密碼/).fill(password);
  await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
    exchange_code: "NASDAQ", display_ticker: "EVIDFILTER", name: "Synthetic evidence filter security", quote_currency: "USD",
  } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  const create = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "INTERIM", ex_date: "2026-01-08", dps: "1", currency: "USD", adjustment_basis: "UNKNOWN",
    citation: "Synthetic incomplete evidence", source_url: null, reason: "Synthetic incomplete evidence filter case.",
  } });
  expect(create.status()).toBe(201);
  const first = await create.json() as Event;
  const revise = async (previous: Event, basis: "UNKNOWN" | "AS_DECLARED") => {
    const response = await page.request.patch(`/api/v1/dividends/events/${previous.id}`, { headers: await headers(page, previous.version), data: {
      security_id: security.id, revision_id: previous.current_revision_id, resolve_rejection_id: null,
      event_type: "INTERIM", ex_date: "2026-01-08", announce_date: null, record_date: null, pay_date: null,
      dps: "1", currency: "USD", adjustment_basis: basis, citation: "Synthetic evidence revision", source_url: null,
      reason: "Synthetic evidence review filtering revision.", confirm_unlock: false, confirm_reopen: false,
    } });
    expect(response.status()).toBe(200);
    return await response.json() as Event;
  };
  const resolution = await revise(first, "AS_DECLARED");
  const current = await revise(resolution, "UNKNOWN");
  const otherCreate = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-01-09", dps: "2", currency: "USD", adjustment_basis: "UNKNOWN",
    citation: "Synthetic second evidence", source_url: null, reason: "Synthetic separate evidence review queue event.",
  } });
  expect(otherCreate.status()).toBe(201);
  const other = await otherCreate.json() as Event;
  const reviewPath = `/api/v1/dividends/events/${current.id}/evidence-reviews`;
  const before = database();

  stage("EVIDENCE_FILTER_API");
  const resolved = await page.request.get(`${reviewPath}?status=RESOLVED&severity=WARNING`);
  expect(resolved.status()).toBe(200);
  expect(resolved.headers()["cache-control"]).toBe("no-store");
  const resolvedBody = await resolved.json() as { items: { status: string; revision_id: string }[]; history_completeness: string };
  expect(resolvedBody.items).toHaveLength(1);
  expect(resolvedBody.items[0]).toMatchObject({ status: "RESOLVED", revision_id: first.current_revision_id });
  expect(resolvedBody.history_completeness).toBe("UNVERIFIED");
  const empty = await page.request.get(`${reviewPath}?status=OPEN&severity=INFO`);
  expect(empty.status()).toBe(200);
  expect((await empty.json() as { items: unknown[] }).items).toEqual([]);
  stage("EVIDENCE_QUEUE_API");
  const queuePath = "/api/v1/dividends/evidence-reviews";
  const queueResponse = await page.request.get(`${queuePath}?status=OPEN&severity=WARNING`);
  expect(queueResponse.status()).toBe(200);
  expect(queueResponse.headers()["cache-control"]).toBe("no-store");
  const queued = await queueResponse.json() as { items: { event_id: string; status: string }[]; history_completeness: string };
  expect(new Set(queued.items.map(item => item.event_id))).toEqual(new Set([current.id, other.id]));
  expect(queued.items.every(item => item.status === "OPEN")).toBe(true);
  expect(queued.history_completeness).toBe("UNVERIFIED");
  const anonymous = await request.newContext({ baseURL: process.env.SPM_E2E_BASE_URL });
  try {
    const denied = await anonymous.get(`${reviewPath}?status=OPEN`);
    expect(denied.status()).toBe(401);
    expect(denied.headers()["cache-control"]).toBe("no-store");
    const queueDenied = await anonymous.get(queuePath);
    expect(queueDenied.status()).toBe(401);
    expect(queueDenied.headers()["cache-control"]).toBe("no-store");
  } finally { await anonymous.dispose(); }

  stage("EVIDENCE_QUEUE_UI");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/dividends");
    const opener = page.getByRole("button", { name: "查看跨事件證據審核" });
    await opener.focus();
    await page.keyboard.press("Enter");
    const panel = page.getByRole("region", { name: "跨事件證據審核" });
    await expect(panel.getByRole("listitem")).toHaveCount(3);
    await expect(panel).toContainText("收款及重建核對不在此清單");
    const status = panel.getByLabel("跨事件審核狀態");
    const severity = panel.getByLabel("跨事件審核嚴重度");
    expect((await status.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    expect((await severity.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    const selected = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === queuePath && url.searchParams.get("status") === "RESOLVED";
    });
    await status.selectOption("RESOLVED");
    expect((await selected).headers()["cache-control"]).toBe("no-store");
    await expect(panel.getByRole("listitem")).toHaveCount(1);
    await severity.selectOption("INFO");
    await expect(panel.getByText(/此頁沒有已記錄的證據審核/)).toContainText("不代表所有問題已處理");
    await severity.selectOption("WARNING");
    await expect(panel.getByRole("listitem")).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const eventRead = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === "/api/v1/dividends/events" && url.searchParams.get("q") === current.id;
    });
    await panel.getByRole("button", { name: "查看此事件" }).click();
    expect((await eventRead).status()).toBe(200);
    await expect(panel).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "股息事件", level: 1 })).toBeFocused();
  }

  stage("EVIDENCE_FILTER_UI");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${current.id}` });
    if (width >= 1024) await page.locator(`tr[data-record-id="${current.id}"]`).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看證據審核歷史" }).click();
    const panel = article.getByRole("region", { name: "股息證據審核歷史" });
    await expect(panel.getByRole("listitem")).toHaveCount(2);
    const status = panel.getByLabel("證據審核狀態");
    const severity = panel.getByLabel("證據審核嚴重度");
    expect((await status.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    expect((await severity.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    const selected = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === reviewPath && url.searchParams.get("status") === "RESOLVED";
    });
    await status.selectOption("RESOLVED");
    expect((await selected).headers()["cache-control"]).toBe("no-store");
    await expect(panel.getByRole("listitem")).toHaveCount(1);
    await expect(panel).toContainText("已記錄後繼版本處理");
    await severity.selectOption("INFO");
    await expect(panel.getByText(/此頁沒有已記錄的證據審核/)).toContainText("不代表歷史完整或問題已處理");
    await expect(panel.getByRole("listitem")).toHaveCount(0);
    await expect(panel).toContainText("目前證據審核只記錄為警告");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(database()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
});
