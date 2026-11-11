import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";

type Profile = components["schemas"]["ProviderProfileRecord"];
async function readProfile(page: Page) {
  const response = await page.request.get("/api/v1/provider-profiles/eodhd");
  expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
  return await response.json() as Profile;
}
function database() {
  // Keep all rows in test-process memory; never log a database dump or credential-bearing values.
  return JSON.parse(execFileSync("uv", ["run", "python", "-I", "-c", `
import json, os, sqlite3
from pathlib import Path
with sqlite3.connect(f"file:{Path(os.environ['DATA_DIR']) / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0022_dividend_projection_tax',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 60
    print(json.dumps({name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall() for name in tables}))
`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })) as Record<string, unknown[][]>;
}

test("isolated actual credential UI keeps import, rotation, clear and historical replay private", async ({ page }) => {
  const root = process.env.SPM_E2E_CREDENTIAL_ROOT!;
  const password = `synthetic-credential-browser-${randomUUID()}`;
  const firstKey = `synthetic-private-${randomUUID()}`;
  const secondKey = randomUUID().replaceAll("-", "").repeat(8);
  const reason = "Explicit synthetic credential workflow, never a licensed live-provider test.";
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "建立私人帳戶" })).toBeVisible();
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click(); await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  const before = database();
  await page.goto("/settings/providers"); await page.getByRole("button", { name: "建立停用來源政策", exact: true }).click();
  let form = page.getByRole("form", { name: "建立停用來源政策" });
  await form.getByLabel("政策來源").selectOption("eodhd"); await form.getByLabel("政策變更依據").fill(reason);
  await form.getByRole("button", { name: "儲存停用政策", exact: true }).click(); await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await readProfile(page)).toMatchObject({ version: 1, credential_reference_present: false, policy: { enabled: false } });
  await page.goto("/settings/providers/eodhd"); await page.getByRole("button", { name: "匯入或輪替私密金鑰" }).click();
  await page.getByLabel("目前密碼").fill(password); await page.getByRole("button", { name: "確認目前密碼", exact: true }).click();
  await expect(page.getByText(/已重新驗證/)).toBeVisible(); await expect(page.getByLabel("目前密碼")).toHaveValue("");
  await page.getByLabel("新私密金鑰").fill(firstKey); await page.getByLabel("金鑰匯入依據").fill(reason);
  const importedResponse = page.waitForResponse(response => response.url().endsWith("/provider-profiles/eodhd/credential") && response.request().method() === "POST");
  await page.getByRole("button", { name: "匯入並儲存停用版本" }).click(); const imported = await importedResponse;
  expect(imported.status()).toBe(200); expect(imported.headers()["cache-control"]).toBe("no-store");
  const original = await imported.json() as Profile;
  expect(original).toMatchObject({ version: 2, credential_reference_present: true, policy: { enabled: false } });
  const importHeaders = imported.request().headers(); const importBody = imported.request().postDataJSON() as components["schemas"]["ProviderCredentialImport"];
  expect(importBody).toEqual({ credential: firstKey, reason });
  await expect(page.getByRole("dialog")).toHaveCount(0); await expect(page.getByLabel("新私密金鑰")).toHaveCount(0);

  await page.getByRole("button", { name: "修訂、啟用或停用政策" }).click(); form = page.getByRole("form", { name: "修訂來源政策" });
  await form.getByLabel("每分鐘軟體要求上限").fill("2"); await form.getByLabel("滾動 24 小時軟體要求上限").fill("10");
  await form.getByLabel("資料權利參考").fill("Synthetic fixture reference, not a paid plan"); await form.getByLabel("資料權利依據").fill(reason);
  await form.getByLabel("我已核對此來源的資料權利及使用條件。").check(); await form.getByLabel(/啟用此個人來源政策/).check(); await form.getByLabel(/我明白這是來源存取授權/).check();
  await form.getByLabel("政策變更依據").fill(reason); await form.getByRole("button", { name: "儲存政策修訂" }).click(); await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await readProfile(page)).toMatchObject({ version: 3, credential_reference_present: true, policy: { enabled: true, requests_per_minute: 2, requests_per_day: 10 } });

  await page.getByRole("button", { name: "匯入或輪替私密金鑰" }).click(); await page.getByLabel("新私密金鑰").fill(secondKey); await page.getByLabel("金鑰匯入依據").fill(reason);
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; }); const saved = new Promise<void>(resolve => { committed = resolve; });
  await page.route("**/api/v1/provider-profiles/eodhd/credential", async route => {
    const response = await route.fetch(); expect(response.status()).toBe(200); committed(); await held; await route.fulfill({ response });
  });
  try {
    await page.getByRole("button", { name: "匯入並儲存停用版本" }).click(); await saved;
    expect(await readProfile(page)).toMatchObject({ version: 4, credential_reference_present: true, policy: { enabled: false } });
    await page.keyboard.press("Escape"); await page.getByRole("button", { name: "關閉編輯" }).click();
    await expect(page.getByRole("dialog")).toBeVisible(); await expect(page.getByRole("button", { name: "取消", exact: true })).toBeDisabled(); await expect(page.getByLabel("新私密金鑰")).toHaveValue(secondKey);
  } finally { release(); }
  await expect(page.getByRole("dialog")).toHaveCount(0); await page.unroute("**/api/v1/provider-profiles/eodhd/credential");
  await page.getByRole("button", { name: "修訂、啟用或停用政策" }).click(); form = page.getByRole("form", { name: "修訂來源政策" });
  await form.getByLabel(/清除目前金鑰參考/).check(); await form.getByLabel("政策變更依據").fill(reason); await form.getByRole("button", { name: "儲存政策修訂" }).click(); await expect(page.getByRole("dialog")).toHaveCount(0);
  const current = await readProfile(page); expect(current).toMatchObject({ version: 5, credential_reference_present: false, policy: { enabled: false } });
  const beforeReplay = database();
  const replay = await page.request.post("/api/v1/provider-profiles/eodhd/credential", { headers: { Origin: process.env.SPM_E2E_BASE_URL!, "X-CSRF-Token": importHeaders["x-csrf-token"]!, "Idempotency-Key": importHeaders["idempotency-key"]!, "If-Match": importHeaders["if-match"]! }, data: importBody });
  expect(replay.status()).toBe(200); expect(await replay.json()).toEqual(original); expect(await readProfile(page)).toEqual(current); expect(database()).toEqual(beforeReplay);
  const after = database();
  for (const name of ["trades", "trade_audit_origins", "trade_history_reviews", "cash_ledger_entries", "expenses", "dividend_events", "dividend_event_controls", "dividend_event_revisions", "dividend_revision_decisions", "dividend_evidence_links", "dividend_candidate_revisions", "dividend_basis_assertions", "dividend_entitlements", "dividend_receipts", "provider_fetch_contexts", "provider_runs", "provider_status", "job_checkpoints", "provider_budgets", "upstream_attempts", "upstream_outcomes", "jobs"]) { expect(Object.hasOwn(before, name)).toBe(true); expect(after[name]).toEqual(before[name]); }
  expect(after.provider_profile_revisions).toHaveLength(5);
  for (const value of [password, bootstrap, firstKey, secondKey]) expect(JSON.stringify(after)).not.toContain(value);
  const files = await readdir(join(root, "managed")); const keys = files.filter(name => name.endsWith(".key")); expect(keys).toHaveLength(2);
  expect((await stat(join(root, "managed"))).mode & 0o777).toBe(0o700);
  const contents: string[] = [];
  for (const key of keys) { const path = join(root, "managed", key); expect((await stat(path)).mode & 0o777).toBe(0o600); contents.push(await readFile(path, "utf8")); }
  expect(new Set(contents)).toEqual(new Set([firstKey, secondKey])); expect(await readdir(join(root, "operator"))).toEqual([]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
});
