import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { expect, type Locator, type Page } from "@playwright/test";
import { test } from "../private-test";

type RecordId = { id: string };
type Tables = Record<string, string[]>;
const origin = process.env.SPM_E2E_BASE_URL!;
const reason = "Explicitly review this synthetic missing trade history.";
const withdrawal = "Withdraw this synthetic unverified review decision.";
function stage(value: string) { writeFileSync(join(process.env.SPM_E2E_CREDENTIAL_ROOT!, "stage"), value); }

function database(): Tables {
  // Exact private row encodings stay in test memory and are never printed.
  return JSON.parse(execFileSync("uv", ["run", "python", "-I", "-c", `
import json, os, sqlite3
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0018_trade_history_reviews',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 57
    print(json.dumps({name: [json.dumps(row, separators=(',', ':')) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')] for name in tables}))
`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })) as Tables;
}

async function headers(page: Page, version?: number) {
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  return { Origin: origin, "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": randomUUID(), ...(version ? { "If-Match": `"${version}"` } : {}) };
}
async function create(page: Page, path: string, data: object): Promise<RecordId> {
  const response = await page.request.post(`/api/v1/${path}`, { headers: await headers(page), data });
  expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
  return await response.json() as RecordId;
}
async function panel(page: Page, day: string) {
  const article = page.getByRole("article", { name: `${day} 買入 10.00000000` });
  const summary = article.getByText("未驗證交易歷史審核");
  if (!await summary.evaluate(element => (element.parentElement as HTMLDetailsElement).open)) await summary.click();
  return article;
}
async function openReview(article: Locator, decision: "記錄未驗證審核" | "撤回未驗證審核", page: Page) {
  await article.getByRole("button", { name: decision, exact: true }).click();
  const sheet = page.getByRole("dialog", { name: decision });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  return sheet;
}
async function fillReview(sheet: Locator, explanation: string) {
  await sheet.getByLabel("審核依據").fill(explanation);
  await sheet.getByLabel(/我明白這項決定/).check();
}
async function reauthenticate(sheet: Locator, password: string) {
  await sheet.getByLabel("目前密碼").fill(password);
  await sheet.getByRole("button", { name: "確認目前密碼", exact: true }).click();
  await expect(sheet.getByText(/已重新驗證/)).toBeVisible();
  await expect(sheet.getByLabel("目前密碼")).toHaveValue("");
}

test("isolated actual trade review preserves unverified decisions and uncertain retry", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-trade-review-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], {
    encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
  }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap);
  await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  stage("MASTER");
  const owner = await (await page.request.get("/api/v1/auth/me")).json() as RecordId;
  const portfolio = await create(page, "portfolios", { name: "Synthetic review portfolio" });
  const broker = await create(page, "brokers", { name: "Synthetic review broker" });
  const account = await create(page, "accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic review account", default_currency: "USD", opening_cash: "1000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.7" });
  const security = await create(page, "securities", { exchange_code: "NASDAQ", display_ticker: "REVIEW", name: "Synthetic review security", quote_currency: "USD" });
  await create(page, "trades", { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "BUY", trade_date: "2026-01-02", quantity: "10", price: "10", currency: "USD", fx_rate_hkd: "7.8" });
  stage("SEED");
  const input = JSON.stringify({ owner: owner.id, portfolio: portfolio.id, account: account.id, security: security.id });
  // Both seeds insert new synthetic facts only; existing audits and guards are untouched.
  const auditless = JSON.parse(execFileSync("uv", ["run", "python", "-I", "tests/e2e/trade-review/trade_review_seed.py"], { input, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] })) as RecordId;
  const retained = execFileSync("uv", ["run", "python", "-I", "tests/e2e/trade-origin/seed.py"], { input, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] }).trim().split("\n");
  expect(retained).toHaveLength(2); const original = retained[0]!; const retiredId = (JSON.parse(original) as RecordId).id;
  expect(original).toContain('"created_sequence":9007199254740993');
  const initial = database();
  expect(initial.trades!.some(row => row.includes("7800000000000"))).toBe(true);
  expect(initial.cash_ledger_entries!.some(row => row.includes("7700000000000"))).toBe(true);
  expect(initial.trade_history_reviews).toEqual([]); expect(initial.jobs).toEqual([]); expect(initial.upstream_attempts).toEqual([]);

  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/transactions");
  let currentPanel = await panel(page, "2997-01-02");
  await expect(currentPanel.getByText(/找不到原始建立或修訂稽核/)).toBeVisible();
  await expect(currentPanel.getByText(/不會解除交易更正或作廢限制/)).toBeVisible();
  let sheet = await openReview(currentPanel, "記錄未驗證審核", page);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const label of ["目前密碼", "審核依據"]) {
      const box = await sheet.getByLabel(label).boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44); expect(box!.height).toBeLessThanOrEqual(56);
    }
    if (process.env.SPM_E2E_SAFE_SCREENSHOT === "1") {
      await expect(sheet.getByLabel("目前密碼")).toHaveValue("");
      await expect(sheet.getByLabel("審核依據")).toHaveValue("");
      await mkdir(".local/screenshots", { recursive: true });
      await sheet.screenshot({ path: `.local/screenshots/trade-history-review-empty-${width}.png` });
    }
  }
  await page.emulateMedia({ colorScheme: "dark" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.emulateMedia({ colorScheme: "light" }); await page.setViewportSize({ width: 360, height: 900 });
  stage("PASSWORD");
  await reauthenticate(sheet, password); await fillReview(sheet, reason);
  const beforeCommit = database();
  stage("COMMIT");
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let wire = ""; let key = ""; let writeStatus = 0;
  await page.route(`**/api/v1/trades/${auditless.id}/history-review`, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    wire = route.request().postData()!; key = route.request().headers()["idempotency-key"]!;
    const response = await route.fetch(); writeStatus = response.status(); committed();
    await held; await route.abort("failed");
  });
  let afterCommit: Tables;
  try {
    await sheet.getByRole("button", { name: "記錄未驗證審核", exact: true }).click(); await saved;
    stage("COMMIT_HELD"); expect(writeStatus).toBe(200);
    expect(JSON.parse(wire)).toEqual({ decision: "ACKNOWLEDGE_UNVERIFIED", expected_revision: "0", expected_gap: "MISSING_CREATION_AUDIT", reason });
    await expect(sheet.getByLabel("目前密碼")).toBeDisabled();
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(sheet.getByLabel("審核依據")).toHaveValue(reason);
    const current = await page.request.get(`/api/v1/trades/${auditless.id}/history-review`);
    expect(current.status()).toBe(200); expect(current.headers()["cache-control"]).toBe("no-store");
    expect(await current.json()).toMatchObject({ state: "ACKNOWLEDGED_UNVERIFIED", history_completeness: "UNVERIFIED" });
    afterCommit = database();
  } finally { release(); }
  stage("LOST");
  await expect(sheet.getByRole("alert")).toContainText("網絡連線中斷");
  await page.unroute(`**/api/v1/trades/${auditless.id}/history-review`);
  stage("REAUTH"); await reauthenticate(sheet, password);
  await sheet.getByLabel("審核依據").fill(`${reason}   `);
  const beforeReplay = database(); stage("REPLAY");
  const replayResponse = page.waitForResponse(response => response.url().endsWith(`/trades/${auditless.id}/history-review`) && response.request().method() === "POST");
  await sheet.getByRole("button", { name: "記錄未驗證審核", exact: true }).click();
  const replay = await replayResponse;
  expect(replay.status()).toBe(200); expect(replay.headers()["cache-control"]).toBe("no-store");
  expect(replay.request().headers()["idempotency-key"]).toBe(key); expect(replay.request().postData()).toBe(wire);
  await expect(sheet).toHaveCount(0); expect(database()).toEqual(beforeReplay);
  const changed = new Set(["audit_events", "trade_history_reviews", "portfolios", "projection_invalidations", "idempotency_keys"]);
  for (const name of Object.keys(beforeCommit)) if (!changed.has(name)) expect(afterCommit![name]).toEqual(beforeCommit[name]);
  expect(afterCommit!.audit_events!.slice(0, beforeCommit.audit_events!.length)).toEqual(beforeCommit.audit_events);

  stage("REVIEW_WITHDRAW");
  currentPanel = await panel(page, "2997-01-02");
  sheet = await openReview(currentPanel, "撤回未驗證審核", page); await fillReview(sheet, withdrawal);
  const withdrawnResponse = page.waitForResponse(response => response.url().endsWith(`/trades/${auditless.id}/history-review`) && response.request().method() === "POST");
  await sheet.getByRole("button", { name: "撤回未驗證審核", exact: true }).click();
  const withdrawn = await withdrawnResponse; expect(withdrawn.status()).toBe(200);
  expect(JSON.parse(withdrawn.request().postData()!)).toEqual({ decision: "WITHDRAW", expected_revision: "1", expected_gap: "MISSING_CREATION_AUDIT", reason: withdrawal });
  await expect(sheet).toHaveCount(0);
  currentPanel = await panel(page, "2997-01-02");
  await expect(currentPanel.getByText(/先前的審核決定已撤回/)).toBeVisible();
  await currentPanel.getByText("查看審核決定歷史").click();
  await expect(currentPanel.getByText("版本 2：撤回")).toBeVisible();
  await expect(currentPanel.getByText("版本 1：記錄未驗證審核")).toBeVisible();

  stage("CHANGED_EVIDENCE");
  await page.getByText("篩選交易").click(); await page.getByLabel("包括已被取代的修訂").check();
  let retiredPanel = await panel(page, "2998-01-02");
  await expect(retiredPanel.getByText(/已退役交易缺少可重建原始狀態/)).toBeVisible();
  sheet = await openReview(retiredPanel, "記錄未驗證審核", page); await fillReview(sheet, reason);
  const retiredResponse = page.waitForResponse(response => response.url().endsWith(`/trades/${retiredId}/history-review`) && response.request().method() === "POST");
  await sheet.getByRole("button", { name: "記錄未驗證審核", exact: true }).click();
  expect((await retiredResponse).status()).toBe(200); await expect(sheet).toHaveCount(0);
  const recovery = await page.request.post(`/api/v1/trades/${retiredId}/audit-origin/recover`, {
    headers: { ...await headers(page, 2), "Content-Type": "application/json" },
    data: `{"original_response":${original},"reason":${JSON.stringify(reason)}}`,
  });
  expect(recovery.status()).toBe(200); expect(recovery.headers()["cache-control"]).toBe("no-store");
  retiredPanel = await panel(page, "2998-01-02");
  await retiredPanel.getByRole("button", { name: "重新載入審核狀態" }).click();
  await expect(retiredPanel.getByText(/稽核證據已變更/)).toBeVisible();
  sheet = await openReview(retiredPanel, "撤回未驗證審核", page); await fillReview(sheet, withdrawal);
  const finalResponse = page.waitForResponse(response => response.url().endsWith(`/trades/${retiredId}/history-review`) && response.request().method() === "POST");
  await sheet.getByRole("button", { name: "撤回未驗證審核", exact: true }).click();
  const finalWrite = await finalResponse; expect(finalWrite.status()).toBe(200);
  expect(JSON.parse(finalWrite.request().postData()!)).toEqual({ decision: "WITHDRAW", expected_revision: "1", expected_gap: "MISSING_RETIRED_ORIGIN", reason: withdrawal });
  await expect(sheet).toHaveCount(0);

  stage("FINAL");
  const final = database();
  for (const name of ["trades", "cash_ledger_entries", "ledger_sequences", "dividend_entitlements", "dividend_receipts", "jobs", "upstream_attempts"]) expect(final[name]).toEqual(initial[name]);
  expect(final.audit_events!.slice(0, initial.audit_events!.length)).toEqual(initial.audit_events);
  expect(final.trade_history_reviews).toHaveLength(4);
  expect(JSON.stringify(final)).not.toContain(password); expect(JSON.stringify(final)).not.toContain(bootstrap);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
});
