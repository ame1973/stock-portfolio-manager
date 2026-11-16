import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";

type Event = components["schemas"]["DividendEventRecord"];
type Tables = Record<string, string[]>;
const origin = process.env.SPM_E2E_BASE_URL!;
function stage(value: string) { writeFileSync(join(process.env.SPM_E2E_CREDENTIAL_ROOT!, "stage"), value); }
function database(): Tables {
  // Read only the exclusive synthetic fixture database; rows remain in process memory.
  return JSON.parse(execFileSync("uv", ["run", "python", "-I", "-c", `
import json, os, sqlite3
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0026_dividend_projection_fee',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 63
    print(json.dumps({name: [json.dumps(row, separators=(',', ':')) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')] for name in tables}))
`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })) as Tables;
}
async function headers(page: Page, version?: number) {
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  return { Origin: origin, "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": randomUUID(), ...(version ? { "If-Match": `"${version}"` } : {}) };
}
async function readEvent(page: Page, eventId: string) {
  const response = await page.request.get(`/api/v1/dividends/events/${eventId}`);
  expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
  return { data: await response.json() as Event, etag: response.headers().etag };
}

test("isolated event UI separates retained decisions from rejection and financial use", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-dividend-browser-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  const reauth = await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } });
  expect(reauth.status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: { exchange_code: "NASDAQ", display_ticker: "DIVE2E", name: "Synthetic dividend event security", quote_currency: "USD" } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  const create = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "92233720368.54775807", currency: "USD",
    adjustment_basis: "AS_DECLARED", citation: "Synthetic event reference", source_url: "https://example.test/event?private=synthetic-secret#page",
    reason: "Synthetic event terms, not a verified source or financial entitlement.",
  } });
  expect(create.status()).toBe(201); expect(create.headers()["cache-control"]).toBe("no-store");
  const event = await create.json() as Event;
  expect(event.reference).toEqual({ citation: "Synthetic event reference", url: null, url_withheld: true });
  const beforeRejection = await readEvent(page, event.id);
  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 });
  const eventList = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/events" && response.request().method() === "GET");
  await page.goto("/dividends");
  expect((await eventList).headers()["cache-control"]).toBe("no-store");
  await expect(page.getByRole("heading", { name: "股息事件", exact: true })).toBeVisible();
  const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
  await expect(article).toContainText("USD 92233720368.54775807");
  const opener = article.getByRole("button", { name: "查看事件與拒絕狀態", exact: true });
  const rejectionRead = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${event.id}/rejection`);
  await opener.focus(); await page.keyboard.press("Enter");
  expect((await rejectionRead).headers()["cache-control"]).toBe("no-store");
  const state = article.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(state).toContainText("此版本未記錄拒絕");
  await expect(state).toContainText("不是接受決定或財務資格");
  await expect(article).toContainText("引用包含查詢或片段，連結未提供");
  expect(await page.locator("body").innerText()).not.toContain("synthetic-secret");
  stage("COMMIT");
  const rejected = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejection reference",
    evidence_url: "https://example.test/rejection?private=synthetic-secret#page", reason: "Synthetic rejection requiring separate state revalidation.",
  } });
  expect(rejected.status()).toBe(200); expect(rejected.headers()["cache-control"]).toBe("no-store");
  const afterRejection = await readEvent(page, event.id);
  expect(afterRejection).toEqual(beforeRejection);
  const snapshot = database();
  expect(snapshot.dividend_event_rejections).toHaveLength(1);
  for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "upstream_attempts", "jobs"]) expect(snapshot[table]).toEqual([]);
  await state.getByRole("button", { name: "重新載入拒絕狀態" }).click();
  await expect(state).toContainText("此版本已記錄拒絕");
  await expect(state).toContainText("Synthetic rejection reference");
  await expect(state).toContainText("引用包含查詢或片段，連結未提供");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/dividends");
    if (width >= 1024) {
      await expect(page.getByRole("navigation", { name: "主要導覽" }).getByRole("link")).toHaveCount(8);
      const row = page.getByRole("row").filter({ hasText: "92233720368.54775807" });
      await row.getByRole("button", { name: "查看原值與操作" }).click();
    } else {
      const bottom = page.getByRole("navigation", { name: "手機主要導覽" });
      await expect(bottom.getByRole("link")).toHaveCount(5);
      await expect(bottom.getByRole("link", { name: "股息事件" })).toHaveAttribute("aria-current", "page");
      await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    }
    await expect(article.getByRole("region", { name: "獨立拒絕狀態" })).toContainText("此版本已記錄拒絕");
    const history = article.getByRole("button", { name: "查看事件修訂歷史" });
    await history.focus(); await page.keyboard.press("Enter");
    await expect(article.getByRole("region", { name: "事件修訂歷史" })).toContainText("歷史版本 1");
    const decisions = article.getByRole("button", { name: "查看拒絕歷史" });
    await decisions.focus(); await page.keyboard.press("Enter");
    await expect(article.getByRole("region", { name: "拒絕歷史" })).toContainText("未記錄後繼解決版本");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator("body").innerText()).not.toContain("synthetic-secret");
  }
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 360, height: 900 });
  await page.getByRole("navigation", { name: "手機主要導覽" }).getByRole("link", { name: "交易紀錄" }).click();
  await expect(page.getByRole("heading", { name: "交易紀錄", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "核對企業行動" }).click();
  await expect(page.getByRole("heading", { name: "企業行動", exact: true })).toBeVisible();
  stage("FINAL");
  expect(database()).toEqual(snapshot);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
});
