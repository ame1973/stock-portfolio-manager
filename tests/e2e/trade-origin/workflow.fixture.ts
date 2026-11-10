import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import { test } from "../private-test";

type RecordId = { id: string };
type Tables = Record<string, string[]>;
const origin = process.env.SPM_E2E_BASE_URL!;
const reason = "Review this retained synthetic trade response against the database audit.";
function stage(value: string) { writeFileSync(join(process.env.SPM_E2E_CREDENTIAL_ROOT!, "stage"), value); }

function database(): Tables {
  // Keep exact row encodings in test memory; never print private database contents.
  return JSON.parse(execFileSync("uv", ["run", "python", "-I", "-c", `
import json, os, sqlite3
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0019_trade_correction_scope',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 58
    print(json.dumps({name: [json.dumps(row, separators=(',', ':')) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')] for name in tables}))
`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })) as Tables;
}

async function create(page: Page, path: string, data: object): Promise<RecordId> {
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  const response = await page.request.post(`/api/v1/${path}`, {
    headers: { Origin: origin, "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": randomUUID() }, data,
  });
  expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
  return await response.json() as RecordId;
}

async function openRecovery(page: Page, day: string) {
  const record = page.getByRole("article", { name: `${day} 買入 10.00000000` });
  await record.getByText("交易稽核來源").click();
  await record.getByRole("button", { name: "恢復交易稽核來源", exact: true }).click();
  const sheet = page.getByRole("dialog", { name: "恢復交易稽核來源" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  return sheet;
}

async function fillRecovery(page: Page, raw: string) {
  await page.getByLabel("原始交易回應 JSON").fill(raw);
  await page.getByLabel("恢復依據").fill(reason);
  await page.getByLabel(/我明白比對資料庫雜湊/).check();
}

async function reauthenticate(page: Page, password: string) {
  await page.getByLabel("目前密碼").fill(password);
  await page.getByRole("button", { name: "確認目前密碼", exact: true }).click();
  await expect(page.getByText(/已重新驗證/)).toBeVisible();
  await expect(page.getByLabel("目前密碼")).toHaveValue("");
}

test("isolated actual trade-origin UI preserves integer digits and uncertain retry", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-trade-origin-${randomUUID()}`;
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
  const portfolio = await create(page, "portfolios", { name: "Synthetic origin portfolio" });
  const broker = await create(page, "brokers", { name: "Synthetic origin broker" });
  const account = await create(page, "accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic origin account", default_currency: "USD",
    opening_cash: "1000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.7",
  });
  const security = await create(page, "securities", {
    exchange_code: "NASDAQ", display_ticker: "ORIGIN", name: "Synthetic origin security", quote_currency: "USD",
  });
  await create(page, "trades", {
    portfolio_id: portfolio.id, account_id: account.id, security_id: security.id,
    side: "BUY", trade_date: "2026-01-02", quantity: "10", price: "10", currency: "USD", fx_rate_hkd: "7.8",
  });
  stage("SEED");
  // The seed inserts new synthetic rows and audits; it never removes guards or changes old audits.
  const originals = execFileSync("uv", ["run", "python", "-I", "tests/e2e/trade-origin/seed.py"], {
    input: JSON.stringify({ owner: owner.id, portfolio: portfolio.id, account: account.id, security: security.id }),
    encoding: "utf8", stdio: ["pipe", "pipe", "pipe"],
  }).trim().split("\n");
  expect(originals).toHaveLength(2);
  const first = originals[0]!; const second = originals[1]!;
  const firstId = (JSON.parse(first) as RecordId).id;
  expect(first).toContain('"created_sequence":9007199254740993');
  expect(second).toContain('"created_sequence":9223372036854775807');
  const initial = database();
  expect(initial.trades!.some(row => row.includes("7800000000000"))).toBe(true);
  expect(initial.cash_ledger_entries!.some(row => row.includes("7700000000000"))).toBe(true);
  expect(initial.jobs).toEqual([]); expect(initial.upstream_attempts).toEqual([]);

  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/transactions"); await page.getByText("篩選交易").click();
  await page.getByLabel("包括已被取代的修訂").check();
  stage("OPEN");
  const legacy = page.getByRole("article", { name: "2998-01-02 買入 10.00000000" });
  await legacy.getByText("交易原值與修訂依據").click();
  await expect(legacy.getByText(/超過瀏覽器精確整數範圍；請以原始伺服器 JSON 為準/)).toBeVisible();
  const sheet = await openRecovery(page, "2998-01-02");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const label of ["目前密碼", "恢復依據"]) {
      const box = await sheet.getByLabel(label).boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44); expect(box!.height).toBeLessThanOrEqual(56);
    }
    expect((await sheet.getByLabel("原始交易回應 JSON").boundingBox())!.width).toBeGreaterThan(0);
    if (process.env.SPM_E2E_SAFE_SCREENSHOT === "1") {
      await expect(sheet.getByLabel("目前密碼")).toHaveValue("");
      await expect(sheet.getByLabel("原始交易回應 JSON")).toHaveValue("");
      await expect(sheet.getByLabel("恢復依據")).toHaveValue("");
      await mkdir(".local/screenshots", { recursive: true });
      await sheet.screenshot({ path: `.local/screenshots/trade-origin-recovery-empty-${width}.png` });
    }
  }
  await page.emulateMedia({ colorScheme: "dark" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 360, height: 900 });
  stage("FILL");
  await fillRecovery(page, first);

  stage("PASSWORD");
  let releasePassword!: () => void; let passwordCommitted!: () => void;
  const passwordHeld = new Promise<void>(resolve => { releasePassword = resolve; });
  const passwordSaved = new Promise<void>(resolve => { passwordCommitted = resolve; });
  await page.route("**/api/v1/auth/reauth", async route => {
    const response = await route.fetch(); passwordCommitted(); await passwordHeld; await route.fulfill({ response });
  });
  try {
    await page.getByLabel("目前密碼").fill(password);
    await page.getByRole("button", { name: "確認目前密碼", exact: true }).click(); await passwordSaved;
    stage("PASSWORD_HELD");
    await expect(sheet.getByRole("button", { name: "等候密碼確認…" })).toBeDisabled();
    await expect(sheet.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(sheet.getByText(/請等候回應後再關閉/)).toBeVisible();
    await expect(sheet.getByLabel("原始交易回應 JSON")).toHaveValue(first);
  } finally { releasePassword(); }
  await expect(page.getByText(/已重新驗證/)).toBeVisible(); await page.unroute("**/api/v1/auth/reauth");
  const beforeCommit = database();

  stage("COMMIT");
  let releaseRecovery!: () => void; let recoveryCommitted!: () => void;
  const recoveryHeld = new Promise<void>(resolve => { releaseRecovery = resolve; });
  const recoverySaved = new Promise<void>(resolve => { recoveryCommitted = resolve; });
  let wire = ""; let key = ""; let status = 0;
  await page.route(`**/api/v1/trades/${firstId}/audit-origin/recover`, async route => {
    wire = route.request().postData()!; key = route.request().headers()["idempotency-key"]!;
    const response = await route.fetch(); status = response.status(); recoveryCommitted();
    await recoveryHeld; await route.abort("failed");
  });
  let afterCommit: Tables;
  try {
    await sheet.getByRole("button", { name: "比對並留存稽核來源" }).click(); await recoverySaved;
    stage("COMMIT_HELD");
    expect(status).toBe(200); expect(wire).toBe(`{"original_response":${first},"reason":${JSON.stringify(reason)}}`);
    await expect(sheet.getByLabel("目前密碼")).toBeDisabled();
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(sheet.getByLabel("原始交易回應 JSON")).toHaveValue(first);
    const read = await page.request.get(`/api/v1/trades/${firstId}/audit-origin`);
    expect(read.status()).toBe(200); expect(read.headers()["cache-control"]).toBe("no-store");
    expect(await read.json()).toMatchObject({ origin: "RETAINED_RESPONSE", history_completeness: "UNVERIFIED" });
    afterCommit = database();
  } finally { releaseRecovery(); }
  stage("LOST");
  await expect(sheet.getByRole("alert")).toContainText("網絡連線中斷");
  await page.unroute(`**/api/v1/trades/${firstId}/audit-origin/recover`);
  stage("REAUTH");
  await reauthenticate(page, password);
  stage("REPLAY");
  const beforeReplay = database();
  const replayResponse = page.waitForResponse(response => response.url().endsWith(`/trades/${firstId}/audit-origin/recover`) && response.request().method() === "POST");
  await sheet.getByRole("button", { name: "比對並留存稽核來源" }).click();
  const replay = await replayResponse;
  expect(replay.status()).toBe(200); expect(replay.headers()["cache-control"]).toBe("no-store");
  expect(replay.request().headers()["idempotency-key"]).toBe(key); expect(replay.request().postData()).toBe(wire);
  await expect(sheet).toHaveCount(0); expect(database()).toEqual(beforeReplay);
  stage("COMPARE");
  const changed = new Set(["audit_events", "trade_audit_origins", "portfolios", "projection_invalidations", "idempotency_keys"]);
  for (const name of Object.keys(beforeCommit)) if (!changed.has(name)) expect(afterCommit![name]).toEqual(beforeCommit[name]);
  expect(afterCommit!.audit_events!.slice(0, beforeCommit.audit_events!.length)).toEqual(beforeCommit.audit_events);
  expect(afterCommit!.audit_events).toHaveLength(beforeCommit.audit_events!.length + 1);

  stage("SECOND");
  const nextSheet = await openRecovery(page, "2999-01-02");
  await fillRecovery(page, second);
  const nextId = (JSON.parse(second) as RecordId).id;
  const nextResponse = page.waitForResponse(response => response.url().endsWith(`/trades/${nextId}/audit-origin/recover`) && response.request().method() === "POST");
  await nextSheet.getByRole("button", { name: "比對並留存稽核來源" }).click(); const saved = await nextResponse;
  expect(saved.status()).toBe(200);
  expect(saved.request().postData()).toBe(`{"original_response":${second},"reason":${JSON.stringify(reason)}}`);
  await expect(nextSheet).toHaveCount(0);
  stage("FINAL");
  const final = database();
  for (const name of ["trades", "cash_ledger_entries", "ledger_sequences", "trade_history_reviews", "dividend_entitlements", "dividend_receipts", "jobs", "upstream_attempts"]) expect(final[name]).toEqual(initial[name]);
  expect(final.audit_events!.slice(0, initial.audit_events!.length)).toEqual(initial.audit_events);
  expect(final.trade_audit_origins).toHaveLength(initial.trade_audit_origins!.length + 2);
  expect(JSON.stringify(final)).not.toContain(password); expect(JSON.stringify(final)).not.toContain(bootstrap);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
});
