import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, seedCandidateSource, stage } from "./helpers";

type Candidate = components["schemas"]["CandidateRecord"];
const financialTables = ["dividend_events", "dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("candidate source picker selects exact observation pins without inferring terms", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-source-picker-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const createSecurity = async (ticker: string) => {
    const response = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
      exchange_code: "NASDAQ", display_ticker: ticker, name: `Synthetic ${ticker} picker security`, quote_currency: "USD",
    } });
    expect(response.status()).toBe(201); return await response.json() as { id: string };
  };
  const security = await createSecurity("PICKA"); const other = await createSecurity("PICKB");
  stage("SEED"); const { source } = await seedCandidateSource(page, security.id);
  const before = database(); for (const table of financialTables) expect(before[table]).toEqual([]);
  stage("NAVIGATE"); await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends/candidates");
  await page.getByRole("button", { name: "新增股息候選" }).click();
  const sheet = page.getByRole("dialog", { name: "新增股息候選" });
  const form = sheet.getByRole("form", { name: "新增股息候選" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  await expect(form.getByRole("button", { name: "瀏覽來源觀測" })).toBeDisabled();
  await form.getByLabel("候選證券", { exact: true }).selectOption(security.id);
  const response = page.waitForResponse(item => new URL(item.url()).pathname === "/api/v1/dividends/observations" && item.request().method() === "GET");
  await form.getByRole("button", { name: "瀏覽來源觀測" }).click();
  const sourceRead = await response; expect(sourceRead.status()).toBe(200); expect(sourceRead.headers()["cache-control"]).toBe("no-store");
  expect(new URL(sourceRead.url()).searchParams.get("security_id")).toBe(security.id);
  const card = form.getByRole("article", { name: `可選來源 ${source.id}` });
  await expect(card).toContainText(source.normalized_sha256); await expect(card).toContainText("每股 USD 0");
  await expect(card).toContainText("不是事件關聯查詢");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  const beforeRefresh = database();
  const refreshed = page.waitForResponse(item => new URL(item.url()).pathname === "/api/v1/dividends/observations" && item.request().method() === "GET");
  await form.getByRole("button", { name: "重新載入可選來源" }).click(); expect((await refreshed).status()).toBe(200); expect(database()).toEqual(beforeRefresh);
  await card.getByRole("button", { name: "加入此來源" }).focus(); await page.keyboard.press("Enter");
  await expect(form.getByLabel("來源 1 觀測識別碼")).toHaveValue(source.id); await expect(form.getByLabel("來源 1 原始雜湊")).toHaveValue(source.normalized_sha256);
  await expect(form.getByLabel("來源 1 基準聲明識別碼（選填）")).toHaveValue(""); await expect(form.getByLabel("來源 1 關聯依據")).toHaveValue("");
  await expect(form.getByLabel("候選除淨日（選填）")).toHaveValue(""); await expect(form.getByLabel("候選每股金額（選填）")).toHaveValue("");
  await expect(form.getByLabel("候選股息幣別")).toHaveValue(""); await expect(card.getByRole("button", { name: "已列入來源" })).toBeDisabled();
  stage("FILL");
  await form.getByLabel("來源 1 關聯依據").fill("Synthetic explicit source association for review.");
  await form.getByLabel("候選變更依據").fill("Synthetic candidate created from a manually selected source.");
  await form.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; }); const saved = new Promise<void>(resolve => { committed = resolve; });
  let first!: Candidate; let firstBody = ""; let firstKey = ""; let retryBody = ""; let retryKey = ""; let attempts = 0;
  const path = "**/api/v1/dividends/candidates";
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++; const result = await route.fetch(); expect(result.status()).toBe(201); expect(result.headers()["cache-control"]).toBe("no-store");
    const body = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    if (attempts === 1) { first = await result.json() as Candidate; firstBody = body; firstKey = key; committed(); await held; await route.abort("failed"); }
    else { retryBody = body; retryKey = key; await route.fulfill({ response: result }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "儲存候選資料" }).click(); await saved;
    expect(first.matches[0]!.observation_id).toBe(source.id); expect(first.matches[0]!.source_sha256).toBe(source.normalized_sha256);
    expect(first.terms.dps).toBeNull(); expect(first.terms.currency).toBeNull();
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toBeVisible();
  } finally { release(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  const body = JSON.parse(firstBody) as components["schemas"]["CandidateWrite"];
  expect(body.terms).toMatchObject({ security_id: security.id, ex_date: null, dps: null, currency: null, event_type: "UNKNOWN", adjustment_basis: "UNKNOWN" });
  expect(body.matches).toEqual([{ observation_id: source.id, source_sha256: source.normalized_sha256, basis_assertion_id: null, reason: "Synthetic explicit source association for review." }]);
  const successor = await page.request.patch(`/api/v1/dividends/candidates/${first.id}`, { headers: await headers(page, first.version), data: {
    ...body, state: "DISMISSED", change_reason: "Synthetic later dismissal after selected source response was lost.",
  } });
  expect(successor.status()).toBe(200); const beforeReplay = database(); stage("REPLAY");
  await form.getByRole("button", { name: "儲存候選資料" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(path);
  expect(attempts).toBe(2); expect(retryKey).toBe(firstKey); expect(retryBody).toBe(firstBody); expect(database()).toEqual(beforeReplay);

  stage("CHANGED_EVIDENCE"); await page.getByRole("button", { name: "新增股息候選" }).click();
  const changedSheet = page.getByRole("dialog", { name: "新增股息候選" }); const changed = changedSheet.getByRole("form", { name: "新增股息候選" });
  await changed.getByLabel("候選證券", { exact: true }).selectOption(security.id); await changed.getByRole("button", { name: "瀏覽來源觀測" }).click();
  await changed.getByRole("article", { name: `可選來源 ${source.id}` }).getByRole("button", { name: "加入此來源" }).click();
  await changed.getByLabel("來源 1 關聯依據").fill("Synthetic association retained after changing security.");
  await changed.getByLabel("候選證券", { exact: true }).selectOption(other.id);
  await expect(changed.getByLabel("來源 1 觀測識別碼")).toHaveValue(source.id);
  await expect(changed.getByLabel("來源 1 原始雜湊")).toHaveValue(source.normalized_sha256);
  await changed.getByLabel("候選變更依據").fill("Synthetic candidate with a deliberately mismatched security.");
  await changed.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  const beforeDenied = database(); const denied = page.waitForResponse(item => new URL(item.url()).pathname === "/api/v1/dividends/candidates" && item.request().method() === "POST");
  await changed.getByRole("button", { name: "儲存候選資料" }).click(); expect((await denied).status()).toBe(409);
  await expect(changed.getByRole("alert")).toContainText("候選來源與證券或雜湊不相符"); expect(database()).toEqual(beforeDenied);
  const after = database(); for (const table of financialTables) expect(after[table]).toEqual(before[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
